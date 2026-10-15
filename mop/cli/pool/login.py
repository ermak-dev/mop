"""log in to a pool server: mop login [login] [--server host] [--https-port port]

Server and login come from --server/LOGIN, the process environment or the
working copy's binding; if missing, they are asked for. The bus password is
asked without echo (or read from MOP_BUS_PASSWORD) and verified before
saving. A valid previous login is reused.

The HTTPS port uses --https-port, the saved client port, MOP_HTTPS_PORT or
443, in that order, without prompting; a sole joined server is selected when
none is named. Join also fetches the server's LLM proxy URL and client key
and verifies them before saving; rejoining refreshes a rotated key without
replacing working credentials.

The server's certificate is pinned before the bus password is sent; compare
the fingerprint on the controller: openssl x509 -noout -fingerprint -sha256
-in ~/.config/mop/secrets/tls.pem

Credentials stay in separate private files under
~/.config/mop/servers/<server>/. The clone remembers only server and login
in local git config. --user NAME is the old spelling of LOGIN.
"""
import getpass
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from mop.cli import lib
from mop.common import bus, busnames, context, creds, paths


def parse(argv):
    """-> (сервер|None, логин|None, HTTPS-порт|None). Отказ -- usage."""
    server, login, https, rest = None, None, None, list(argv)
    while rest:
        a = rest.pop(0)
        if a in ("--server", "--user", "--https-port"):
            if not rest:
                lib.usage(__doc__)
            v = rest.pop(0)
        elif a.startswith(("--server=", "--user=", "--https-port=")):
            a, v = a.split("=", 1)
        elif a.startswith("-") or login is not None:
            lib.usage(__doc__)
        else:
            login = a
            continue
        if a == "--server":
            server = v
        elif a == "--https-port":
            https = v
        else:
            login = v
    return server, login, https


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def serving(origin):
    """Серверы, где у машины уже есть вход и чей реестр знает origin."""
    out = []
    for host in creds.joined_dirs():
        try:
            c = bus.server_config(host)
            # Логин -- тех кредов, которыми спрашиваем (#207), а не этой машины.
            ans = bus.ask_once(c, bus.cluster_subject(bus.ADMIN, as_login=c.get("user")),
                               "projects")
        except Exception:
            continue
        if origin in (ans.get("origins") or []):
            out.append(host)
    return out


def ask(prompt, secret=False, default=None):
    """Спросить недостающее до записи; секрет не отзывается в терминал."""
    if not sys.stdin.isatty():
        raise RuntimeError(f"{prompt.rstrip(': ')} is required; no terminal to ask on")
    value = getpass.getpass(prompt) if secret else input(prompt)
    value = value.strip() or default
    if not value:
        raise RuntimeError(f"{prompt.rstrip(': ')} is required")
    return value


def _probe(url, key, directory, port=None):
    """Проверить ключ без передачи его по открытой LAN или через redirect.

    port -- ПРОВЕРЕННЫЙ порт этого входа (совпадает с портом URL записи,
    сверка в _fetch), не порт самого URL: он сам себе не доказательство.
    Его отсутствие -- неauthority-режим: легаси-префикс остаётся, /v1
    надстраивается как есть (nginx /llm жив до перехода, #529/#547)."""
    from ..common import llm
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError("the proxy key needs HTTPS outside localhost")
    pin = creds.cafile(directory) if parsed.hostname == os.path.basename(directory) else None
    tls = ssl.create_default_context(cafile=pin)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            raise RuntimeError("proxy verification must not redirect the client key")

    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls))
    # Канонический /v1/models (#547): легаси /llm ВЫДАННОГО адреса снимается
    # authority-проверкой (этот сервер + проверенный порт), чужой путь --
    # включая отсутствие подсказок -- не режется.
    request = urllib.request.Request(
        llm.endpoint(url, "models", host=os.path.basename(directory), port=port),
        headers={"Authorization": f"Bearer {key}"})
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(f"proxy verification returned HTTP {response.status}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"proxy verification returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"proxy verification failed: {e.reason}") from e


def _restore_pin(dest, der):
    """Вернуть прежний TLS-пин после неуспешного входа."""
    if der is not None:
        creds.write_cert(dest, der)
    else:
        path = creds.cafile(dest)
        if path:
            os.remove(path)


def _fetch(host, login, password, dest, https):
    """Запросить настройки от имени уже проверенного человека, не узла."""
    c = creds.wss_config(host, https, None, password, user=login,
                         cafile=creds.cafile(dest))
    reply = bus.ask_once(c, busnames.join_config(login), "client_config")
    if not isinstance(reply, dict):
        raise RuntimeError("invalid proxy configuration response")
    if reply.get("error"):
        raise RuntimeError(reply["error"])
    try:
        record = creds._client_values(reply["https_port"], reply["proxy_url"],
                                      reply["proxy_key"])
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError("invalid proxy configuration from server") from e
    if record["https_port"] != https:
        raise RuntimeError("server proxy port differs from the verified bus port")
    return record


def _https_port(explicit, saved):
    """Явный выбор > сохранённый > окружение > 443, до соединения."""
    https = (explicit if explicit is not None else
             (saved or {}).get("https_port") or os.environ.get("MOP_HTTPS_PORT") or "443")
    if not creds.valid_https_port(https):
        raise ValueError("server HTTPS port must be between 1 and 65535")
    return https


def complete(host, login, dest, https):
    """Один проверенный вход: старый пароль можно использовать, новая запись
    появляется только после обеих сетевых проверок -- и запись одна (#533).

    Каталог классифицируется ОДНИМ снимком creds.snapshot -- НА ВХОДЕ этой
    фазы (прямые вызовы и командлеты входят сюда, а не в main/ensure), и
    классификация одна на весь вход: machine (service) и испорченный/
    неизвестный connection.json закрыты человеку ДО сети и записи -- первый
    вход молча сменил бы личность сервисов, второй -- перезаписал бы чужой
    формат. После классификации файл НЕ перечитывается: два чтения увидели
    бы два поколения каталога."""
    if not creds.valid_https_port(https):
        raise ValueError("server HTTPS port must be between 1 and 65535")
    try:
        kind, profile = creds.snapshot(dest)
    except creds.ProfileError as e:
        # Испорченный/неизвестный новый файл не перезаписывается и не
        # читается как вход: чинить его может только человек, а не вход.
        raise RuntimeError(str(e)) from e
    if kind == "machine":
        # Служебный каталог: конфликт и здесь, до сети, TLS-пина и записи.
        raise RuntimeError("machine service identity and a human login conflict: "
                           "use a separate operator account or connection directory")
    if kind != "human":
        # Нет целого профиля -- вход с нуля (#662: остатки прежней пары
        # подсказкой не читаются, пароль спрашивается у человека).
        profile = None
    pin = _pinned(creds.cafile(dest))
    try:
        valid = profile and profile.get("user") == login and \
            _still_valid(host, https, dest, profile)
        password = profile["password"] if valid else _login(host, login, dest, https)
        record = _fetch(host, login, password, dest, https)
        _probe(record["proxy_url"], record["proxy_key"], dest, port=https)
        # Одна атомарная запись: провал между двумя прежними оставлял смесь --
        # пароль одного входа с портом и ключом другого (#533).
        creds.write_connection(dest, login, password, record["https_port"],
                               record["proxy_url"], record["proxy_key"])
    except Exception:
        _restore_pin(dest, pin)
        raise
    # После записи профиль новый и целый: провал очистки -- не отказ входа и
    # не откат пина, а честный остаток старых файлов; вход уже состоялся.
    # Вся фаза очистки под одной защитой: и список, и удаления.
    try:
        gone = creds.legacy(os.listdir(dest))
        if os.path.exists(os.path.join(dest, creds.TOKEN_FILE)) and \
                not os.path.isdir(paths.local(paths.SECRETS)):
            gone.append(creds.TOKEN_FILE)
        for name in gone:
            os.remove(os.path.join(dest, name))
    except OSError as e:
        print(f"login complete, cleanup incomplete: {e}", file=sys.stderr)


def _bind(host, user):
    """Запомнить только адрес и имя в локальном клоне, без паролей."""
    if git("rev-parse", "--show-toplevel") is None:
        return
    clone = context.clone_binding()
    for name, value in (("server", host), ("user", user)):
        if clone.get(name) != value:
            git("config", "--local", context.FIELDS[name][0], value)


def ensure():
    """Дополнить отсутствующий вход перед запуском master или code.

    Каталог без целого connection.json -- не вход (#662: остатки прежней
    пары подсказкой не читаются): восстановление одно, полный повторный
    вход (проверка шины, свежий proxy-конфиг, проба ключа)."""
    ctx = context.current()
    host = ctx.server
    if not host:
        joined = creds.joined_dirs()
        if len(joined) > 1:
            raise RuntimeError("multiple joined servers: name one with --server")
        old = sorted(name for name in _dirs()
                     if creds.operator(os.path.join(creds.ROOT, name)) is not None)
        choices = joined or old
        if len(choices) > 1:
            raise RuntimeError("multiple server logins: name one with --server")
        if choices:
            host = choices[0]
        elif sys.stdin.isatty():
            host = ask("server address: ")
        else:
            raise RuntimeError("no server and no terminal: run mop login --server ADDRESS")
    dest = creds.server_dir(host)
    # Один снимок на весь ensure (#533): классификация и решение -- из
    # одного поколения каталога, без поштучных чтений (два чтения видели
    # бы два поколения, а битый operator.json молча терял имя на полпути
    # к отказу).
    try:
        kind, hints = creds.snapshot(dest)
    except creds.ProfileError as e:
        # Конфликт и испорченный/неизвестный новый файл -- отказ: повторный
        # вход молча перезаписал бы чужой формат.
        raise RuntimeError(str(e)) from e
    if kind == "human":
        return host          # целый вход: не трогаем
    if kind == "machine":
        raise RuntimeError("machine service identity and a human login conflict: "
                           "use a separate operator account or connection directory")
    # Нет целого профиля -- полный вход с нуля (#662: подсказок из остатка
    # прежней пары больше нет).
    user = creds.pick_login(ctx.user, None, None)
    if not user:
        if not sys.stdin.isatty():
            raise RuntimeError(f"no login for {host}: run mop login --server {host}")
        user = ask(f"login on {host}: ")
    https = _https_port(None, None)
    with context.use(context.resolve({"server": host}, {}, {})):
        complete(host, user, dest, https)
    _bind(host, user)
    return host


def _dirs():
    """Каталоги серверов, что есть; пусто -- когда корня ещё нет."""
    try:
        return sorted(os.listdir(creds.ROOT))
    except FileNotFoundError:
        return []


def main(argv):
    """Сервер и логин -- из контекста команды (#131): клон < окружение <
    командная строка; `--server` снимает диспетчер, логин -- здесь."""
    explicit_server, login, explicit_https = parse(argv)
    ctx = context.current()
    if explicit_server:                       # `mop login --server` мимо диспетчера
        ctx = context.resolve({"server": explicit_server}, os.environ,
                              context.clone_binding())
    in_clone = git("rev-parse", "--show-toplevel") is not None
    origin = lib.cwd_origin() if in_clone else None
    src = ctx.sources.get("server")
    explicit = ctx.server if src in ("cli", "env") else None
    bound = ctx.server if src == "clone" else None
    found = serving(origin) if origin and not ctx.server else []
    try:
        if not (explicit or bound or found):
            found = creds.joined_dirs()   # следы входов направляют выбор (#533)
            if len(found) > 1:
                raise RuntimeError(f"multiple joined servers ({', '.join(found)}): "
                                   f"name one with --server")
        try:
            host = creds.pick_server(explicit, bound, found, None)
        except ValueError:
            if len(found) > 1:
                raise
            host = ask("server address: ")
        # Дальше весь процесс -- про этот сервер: каталог кредов, TLS-прокси.
        with context.use(context.resolve({"server": host}, {}, {})):
            dest = creds.server_dir(host)
            # Подсказки -- из ОДНОГО снимка каталога (#533): целый профиль
            # знает логин и порт сам. Явные аргументы и контекст старше
            # любой подсказки. broken/conflict отказывают ЗДЕСЬ, до сети,
            # вопросов и записи: вход такой каталог не ремонтирует.
            try:
                kind, who = creds.snapshot(dest)
            except creds.ProfileError as e:
                lib.fail(str(e))
                return 1
            if kind == "machine":
                lib.fail("machine service identity and a human login conflict: "
                         "use a separate operator account or connection directory")
                return 1
            # kind здесь human | None: профиль несёт подсказки, каталог без
            # целого входа -- нет (#662: остатки прежней пары молча не
            # читаются).
            hint = who or {}
            login = creds.pick_login(login or ctx.user, hint.get("user"), None)
            if not login:
                login = ask(f"login on {host}: ")
            # Порт -- подсказка из целого профиля; каталог без входа и
            # машинный каталог своего порта не хранят.
            https = _https_port(explicit_https,
                                {"https_port": hint["https_port"]}
                                if hint.get("https_port") else None)
            complete(host, login, dest, https)
    except (RuntimeError, ValueError, OSError) as e:
        lib.fail(str(e))
        return 1
    _bind(host, login)
    return 0


def _still_valid(host, https, dest, stored):
    """Пускает ли шина по уже лежащим кредам: тогда пароль не спрашиваем."""
    try:
        bus.check(creds.wss_config(host, https, None, stored["password"],
                                   user=stored["user"], cafile=creds.cafile(dest)))
        return True
    except Exception:
        return False


def _pinned(path):
    """DER закреплённого сертификата, либо None."""
    if not path:
        return None
    import ssl
    try:
        with open(path) as f:
            return ssl.PEM_cert_to_DER_cert(f.read())
    except (OSError, ValueError):
        return None


def _login(host, user, dest, https):
    """Проверить пароль шиной; сохранение -- после проверки прокси в complete.

    Молча положенный неверный пароль читался бы потом как «агент не отвечает»
    через двадцать секунд таймаута."""
    # Шина -- через TLS-прокси (#97). Самоподписанный сертификат закрепляем
    # при первом входе, до пароля: пароль уходит только туда, чей сертификат
    # уже закреплён. Настоящий не закрепляем, прежний пин снимаем.
    try:
        der = creds.untrusted_cert(host, https)
    except OSError as e:
        raise RuntimeError(f"no TLS proxy at {host}:{https}: {e}")
    pin = creds.cafile(dest)
    previous = _pinned(pin)
    if der is None and pin:
        os.remove(pin)
    if der is not None:
        new = _pinned(pin) != der
        pin = creds.write_cert(dest, der)
        # Единственная строка успешного входа: сверить отпечаток -- дело
        # человека, и молча закреплённый сертификат этого не даёт.
        if new:
            print(f"{host}: self-signed certificate {creds.fingerprint(der)}")
    try:
        # Пароль -- после прокси: опечатка в адресе сервера отказывает сразу, а
        # не после вопроса о пароле.
        password = os.environ.get("MOP_BUS_PASSWORD")
        if not password:
            if not sys.stdin.isatty():
                raise RuntimeError("no MOP_BUS_PASSWORD and no terminal to ask on")
            password = getpass.getpass(f"password for {user} on {host}: ")
        if not password:
            raise RuntimeError("empty password")
        # Сначала проверяем, потом кладём: каталог не должен запомнить того, кого
        # шина не пустила.
        c = creds.wss_config(host, https, None, password, user=user,
                             cafile=pin if der is not None else None)
        bus.check(c)
        return password
    except Exception as e:
        _restore_pin(dest, previous)
        raise RuntimeError(str(e)) from e
