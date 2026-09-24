"""Auth callout NATS (#206): кто входит на шину, решает сервис, а не список.

Людей и папетов в users.conf нет (#219): NATS на каждом входе того, кого
нет в auth_users, публикует запрос на $SYS.REQ.USER.AUTH, и отвечает этот
сервис:

  * человек -- провайдер личностей (#205, identity.provider): логин и пароль;
    права -- operators.permissions(Identity): публикация с его логином токеном субъекта (#207),
    подписка -- маски проектов. Только через WebSocket, как статическая запись с
    allowed_connection_types (#105): поле в JWT пользователя nats-server
    2.14.6 из ответа callout не применяет (стенд #203), поэтому -- отказом
    здесь, по client_info.type;
  * папет (puppet-<проект>) -- файл пароля сервера, тот же, из которого
    natsconf рендерил его статическую запись; права --
    natsconf.puppet_permissions. Новому проекту reload nats больше не нужен:
    файл читается на каждом входе;
  * машины (service, node-*, callout) -- в auth_users, сюда не приходят;
    пришедшая -- отказ с этим словом.

Запрос и ответ -- JWT (mop/nkjwt.py), в ящиках xkey: пароль иначе шёл бы
по шине открытым текстом. Ответ подписывает ключ аккаунта-издателя; оба
seed лежат рядом с /etc/nats и не покидают сервер.

JWT пользователя без exp: срок закрывает соединение в момент истечения
(стенд #206), и при лежащем callout человек не вернулся бы -- а переход
обещает, что живые соединения живут. Отзыв доступа действует со
следующего входа.

Данные, без печати: печатает `mop callout serve`.
"""
import asyncio
import os
import re

from . import busnames, config, fsutil, identity, natsconf, nkjwt, operators

SUBJECT = "$SYS.REQ.USER.AUTH"
USER = busnames.CALLOUT     # машинный пользователь сервиса
ACCOUNT = "$G"              # куда сажать вошедших: туда же, где машины
# Ключи -- рядом с конфигом nats (0600, пользователь пула).
ISSUER_FILE = os.path.join(natsconf.DIR, "callout-issuer.seed")
XKEY_FILE = os.path.join(natsconf.DIR, "callout-xkey.seed")
# Имя проекта в логине папета: то, что годится в имя файла пароля и
# ничего больше -- логин приходит от входящего.
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class Keys:
    """Seed издателя и xkey. Публичные ключи -- для callout.conf."""

    def __init__(self, issuer_seed, xkey_seed):
        self.issuer_seed, self.xkey_seed = issuer_seed, xkey_seed
        self.issuer, self.xkey = nkjwt.public_of(issuer_seed), nkjwt.public_of(xkey_seed)


def keys(issuer_file=ISSUER_FILE, xkey_file=XKEY_FILE):
    """Ключи сервиса; недостающий заводится, имеющийся не меняется -- как
    пароли папетов (natsconf.passwords): смена издателя без рестарта nats
    закрыла бы вход всем."""
    out = []
    for path, new in ((issuer_file, nkjwt.new_account_key),
                      (xkey_file, lambda: nkjwt.new_xkey()[0])):
        try:
            with open(path) as f:
                seed = f.read().strip()
        except FileNotFoundError:
            seed = ""
        if not seed:
            seed = new()
            fsutil.write_private(path, seed + "\n")
        out.append(seed)
    return Keys(*out)


class PuppetProvider:
    """Папеты: пароль проекта из файла сервера (natsconf.passwords)."""

    def __init__(self, root):
        self.root = root

    def authenticate(self, user, password):
        """-> проект, либо identity.Refused."""
        project = user[len(busnames.PUPPET_PREFIX):]
        if not _PROJECT.match(project):
            raise identity.Refused(f"{user!r} is not a puppet user")
        path = os.path.join(self.root, busnames.pass_file(user))
        try:
            with open(path) as f:
                stored = f.read().strip()
        except FileNotFoundError:
            stored = ""
        if not stored:
            raise identity.Refused(f"{user}: no such project on this server")
        if not _same(password, stored):
            raise identity.Refused(f"{user}: wrong password")
        return project


def _same(a, b):
    import hmac
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def _side(allow, deny=()):
    out = {"allow": list(allow)}
    if deny:
        out["deny"] = list(deny)
    return out


def rights(login, ctype, password, humans, puppets):
    """Кто входит -> (имя, права JWT {pub, sub}), либо identity.Refused.
    Чистая функция, кроме провайдеров."""
    if not login:
        raise identity.Refused("no login given")
    if busnames.is_puppet(login):
        perms = natsconf.puppet_permissions(puppets.authenticate(login, password))
        return login, {"pub": _side(perms["publish"]), "sub": _side(perms["subscribe"])}
    if busnames.is_machine(login):
        raise identity.Refused(f"{login} is a machine user: it belongs in auth_users, "
                               f"not in the callout")
    if ctype != "websocket":
        # #105: пароль человека не идёт по LAN открытым текстом -- только
        # WebSocket через TLS-прокси. До проверки пароля: scrypt не зря.
        raise identity.Refused(f"{login}: people connect over WebSocket only "
                               f"(mop join, wss through the proxy), not {ctype or 'this'}")
    who = humans.authenticate(login, password)
    # Две стороны (#207): публикация -- явным списком с логином вызывающего
    # токеном субъекта, подписка -- маски проектов. Та же функция, что
    # рендерит статическую запись: своей сборки прав здесь нет.
    p = operators.permissions(who)
    return who.login, {"pub": _side(p["publish"], p["publish_deny"]),
                       "sub": _side(p["allow"], p["deny"])}


def respond(req, humans, puppets, keys_, now=None):
    """Тело запроса сервера -> JWT ответа (строка). Отказ -- nats.error."""
    n = req.get("nats") or {}
    opts = n.get("connect_opts") or {}
    base = {"sub": n.get("user_nkey", ""), "aud": (n.get("server_id") or {}).get("id", ""),
            "nats": {"type": "authorization_response", "version": 2}}
    try:
        if n.get("type") != "authorization_request":
            raise identity.Refused(f"not an authorization request: {n.get('type')!r}")
        name, perms = rights(opts.get("user") or "", (n.get("client_info") or {}).get("type"),
                             opts.get("pass") or "", humans, puppets)
    except identity.Refused as e:
        base["nats"]["error"] = str(e)
        return nkjwt.encode(base, keys_.issuer_seed, now)
    user = {"sub": base["sub"], "aud": ACCOUNT, "name": name,
            "nats": {**perms, "type": "user", "version": 2}}
    base["nats"]["jwt"] = nkjwt.encode(user, keys_.issuer_seed, now)
    return nkjwt.encode(base, keys_.issuer_seed, now)


def handle(data, server_xkey, keys_, humans, puppets):
    """Запечатанный запрос сервера -> (запечатанный ответ, строка журнала).

    Подпись запроса проверяется: ящик открывается только нашим ключом, но
    подписывает его сервер, и чужая подпись -- не запрос. Пароль в журнал
    не идёт: только логин, тип соединения и вердикт."""
    req = nkjwt.decode(nkjwt.xkey_open(keys_.xkey_seed, server_xkey, data).decode())
    n = req.get("nats") or {}
    if req.get("iss") != (n.get("server_id") or {}).get("id"):
        raise ValueError("the request is not signed by the server it names")
    token = respond(req, humans, puppets, keys_)
    error = (nkjwt.decode(token).get("nats") or {}).get("error")
    line = (f"{(n.get('connect_opts') or {}).get('user') or '-'} via "
            f"{(n.get('client_info') or {}).get('type') or '-'}: "
            + (f"refused: {error}" if error else "ok"))
    return nkjwt.xkey_seal(keys_.xkey_seed, server_xkey, token.encode()), line


def answer(data, server_xkey, keys_, humans, puppets):
    """Запечатанный запрос -> запечатанный ответ."""
    return handle(data, server_xkey, keys_, humans, puppets)[0]


async def serve(url, password, keys_, humans, puppets, log):
    """Подписчик: отвечает на запросы авторизации, пока жив процесс.

    Под своим пользователем callout (его право -- ровно этот субъект и
    ответы на него), на петле: пароль входящего уже в ящике xkey, но
    соединению сервиса незачем выходить за сервер."""
    import nats
    nc = await nats.connect(url, user=USER, password=password, name="mop-callout",
                            allow_reconnect=True, max_reconnect_attempts=-1,
                            reconnect_time_wait=2)
    loop = asyncio.get_running_loop()

    async def on_msg(msg):
        server_xkey = (msg.headers or {}).get("Nats-Server-Xkey", "")
        try:
            # scrypt -- десятки миллисекунд: в исполнителе, чтобы петля
            # отвечала остальным входящим.
            out, line = await loop.run_in_executor(None, handle, msg.data, server_xkey,
                                                   keys_, humans, puppets)
        except Exception as e:
            # Без ответа сервер откажет входящему сам, по таймауту.
            log(f"callout: unreadable request: {type(e).__name__}: {e}")
            return
        log(f"callout: {line}")
        await msg.respond(out)

    await nc.subscribe(SUBJECT, cb=on_msg)
    await nc.flush()
    log(f"mop-callout: answering {SUBJECT} as {USER}, issuer {keys_.issuer}")
    await asyncio.Event().wait()


async def run(log):
    """Сервис целиком, из настроек сервера."""
    from . import bootstrap   # PUPPET_CREDS: пароли папетов пишет natsconf
    password = natsconf.read_base().get(USER)
    if not password:
        raise RuntimeError(f"{natsconf.BASE} has no {USER} password -- run mop deploy")
    await serve(f"nats://127.0.0.1:{config.get('MOP_NATS_PORT')}", password, keys(),
                identity.server_provider(), PuppetProvider(bootstrap.PUPPET_CREDS), log)
