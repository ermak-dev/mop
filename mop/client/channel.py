"""Канал сообщений: кому и как доставить, и что вышло.

Три пути, и граница между ними -- адрес (route):
  папет пула    `pu-<проект>-<n>`: через агента его узла, сокет host-local;
  сессия рядом  имя или pid сессии claude этого хоста: прямо в её сокет;
  мастер        всё остальное: его инбокс на шине `<логин>.<хост>-<pid>` (#213).

Говорящих три вида (#585, docs/SOUL.md): человек, легаси-папет проекта
(`puppet-<проект>`) и делегированный принципал роли (`grant-<id>`). Роль
-- папет: её адрес -- имя джоба, а не инбокс процесса MCP, и сколько бы
процессов MCP ни жило в теле, адрес у тела один.

Жил во фронтенде MCP, а потолок ожидания и тексты отказа -- ещё и копией в
`mop send` (#148). Здесь вердикт доставки -- данные; фронтенды его только
рисуют. Протокол сокета -- mop/session.py (CHANNEL.md), субъекты -- BUS.md.
"""
import functools
import os
import re

from mop.common import bus, busnames, ops, puppets, soul, state
from mop import session

# Дольше десяти минут ждать простоя незачем: запрос держит соединение, а
# уведомление о простое (notify_when_idle) не держит ничего.
MAX_WAIT = 600


def clamp_wait(seconds):
    """Сколько ждать простоя: 0..MAX_WAIT."""
    return min(max(seconds or 0, 0), MAX_WAIT)


# ─── кто говорит (#585) ──────────────────────────────────────────────────
# Вид говорящего -- по имени на шине, а не по флагу и не по имени роли:
# человек (свой логин в субъекте, аренда при send, #161/#207), легаси-папет
# проекта (публикует только node.*.msg, natsconf.puppet_permissions) и
# принципал роли (свой токен, права -- из реестра на сервере, #579).
HUMAN, LEGACY, GRANT = "human", "legacy", "grant"


def actor(user):
    """Имя на шине -> HUMAN | LEGACY | GRANT. Без кредов -- легаси: так
    профиль узла и читался всегда (mcp.is_master)."""
    if soul.is_grant(user):
        return GRANT
    if not user or busnames.is_puppet(user):
        return LEGACY
    return HUMAN


def bus_user():
    """Имя этого процесса на шине или None."""
    try:
        return bus.config().get("user") or None
    except Exception:
        return None


def human_login():
    """Владелец задания (#161) -- только человек. bus.login() отдаёт и
    grant-<id>: is_machine нового префикса не знает, и до общего
    разделения логина и токена вызывающего (#585, C1) отсекаем здесь --
    иначе имя принципала поехало бы в owner ложной привязкой аренды."""
    return bus.login() if actor(bus_user()) == HUMAN else None


def send_channel(kind):
    """Канал send к агенту узла. Легаси-папету шина пускает только .msg:
    rpc с его кредами -- permissions violation (живой send pu2 -> pu4)."""
    return "msg" if kind == LEGACY else "rpc"


def send_fields(kind, me, inbox, force, owner):
    """Поля send по виду говорящего. Роль шлёт простое сообщение: ни
    владельца, ни force -- аренды у send нет (docs/SOUL.md, dispatch ≠
    send). reply_to остаётся транспортом вестей (notify) в инбокс процесса,
    адрес роли для ответа -- имя джоба (from_name), а не инбокс. Служебное
    получатель всё равно срезает (#584); не слать его -- не надеяться на
    срезание."""
    if kind == GRANT:
        return {"from_name": me, "reply_to": inbox}
    return {"from_name": me, "owner": owner if kind == HUMAN else None,
            "force": force, "reply_to": inbox}


# ─── тело роли: кто я и где стою ─────────────────────────────────────────
# Окружение тела ставит доставка бандла (#590): MOP_PUPPET -- имя джоба,
# MOP_SOUL_DIR -- каталог закреплённого бандла (soul.yaml и промпт) внутри
# инкарнации (root/bundle), у корня инкарнации -- расписка готовности
# лаунчера (driver.READY). Раскладку держат Published-константы driver,
# а не строки здесь: узел и `mop driver run` кладут файлы по ней же.
def role_name(environ=None):
    """Адрес роли -- имя её джоба. Без него роль не знает, как ей ответят."""
    env = os.environ if environ is None else environ
    name = env.get("MOP_PUPPET")
    if name:
        return name
    cwd = (my_session() or {}).get("cwd") or os.getcwd()
    base = os.path.basename(os.path.realpath(cwd))
    if is_puppet(base):
        return base
    raise LookupError("this role body does not know its job name (MOP_PUPPET)")


def own_bundle(environ=None):
    """Бандл роли тела -> (Bundle | None, причина | None). Каталог
    инструментов из него -- удобство модели, не защита: права проверяет
    сервер. Бандл принимается, только когда тело подтверждает его
    распиской готовности у корня инкарнации (ready/role/soul_inc/
    soul_digest сходятся с загруженным бандлом), а корень -- ровно та
    инкарнация этого джоба, которую держит Published-раскладка
    (driver.soul_dir): самообъявленный MOP_SOUL_DIR чужого каталога ничего
    не открывает, и «нет бандла» честнее подделки. Нет бандла -- каталог
    закрыт до обнаружения адресатов."""
    from mop import driver
    env = os.environ if environ is None else environ
    path = env.get("MOP_SOUL_DIR")
    if not path:
        return None, "no role bundle in this body (MOP_SOUL_DIR)"
    root, sub = os.path.split(os.path.abspath(path))
    if sub != driver.SOUL_BUNDLE:
        return None, (f"{path}: the body bundle lives at "
                      f"<incarnation>/{driver.SOUL_BUNDLE}, not here")
    try:
        bundle = soul.load(path)
    except (OSError, ValueError) as e:
        return None, str(e)
    # Джоб и инкарнация: имя джоба знает тело (MOP_PUPPET), инкарнацию --
    # место корня в раскладке. Расписка лаунчера -- последним написанный
    # маркер доставки -- обязана называть ровно эту пару и этот бандл.
    try:
        name = role_name(env)
    except LookupError as e:
        return None, f"{e} -- no bundle is verified without the job name"
    inc = os.path.basename(root)
    if os.path.realpath(root) != os.path.realpath(driver.soul_dir(name, inc)):
        return None, (f"{path}: a self-declared bundle outside the body's "
                      f"own incarnation layout opens nothing")
    try:
        with open(os.path.join(root, driver.READY), encoding="utf-8") as f:
            import json
            receipt = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"{root}/{driver.READY}: the ready receipt does not read: {e}"
    for key, want in (("ready", True), ("role", bundle.role),
                      ("soul_inc", inc), ("soul_digest", bundle.digest)):
        if receipt.get(key) != want:
            return None, (f"{root}/{driver.READY}: {key} is "
                          f"{receipt.get(key)!r}, the body pins {want!r}")
    return bundle, None


def own_inbox(user, master_id):
    """Субъект инбокса ЭТОГО процесса для вестей и reply_to. Человеку --
    мастер-инбокс шины (его и слушает ACL); принципалу роли -- субъект под
    его приватным префиксом ответов: подписка принципала (#581) -- только
    свой private_scope, мастер-форму шина отказала бы молча. master_id
    (хвост <хост>-<pid> или адрес мастера) едет хвостом субъекта -- адресом
    для ответа остаётся имя джоба роли (send_fields)."""
    if soul.is_grant(user):
        return f"{busnames.private_prefix(user)}.{master_id}"
    return bus.inbox(master_id)


def candidates(rows):
    """Строки ростера (PuppetRow) -> словари для pick. receives_dispatch --
    данные ростера ролей (#587); нет поля -- None, и цель не годится."""
    return [{"name": r.name, "receives_dispatch": getattr(r, "receives_dispatch", None),
             "project": puppets.project_of(r.origin) if r.origin else None,
             "free": state.is_free(r.kind), "owner": r.owner} for r in rows]


def pick(rows, project, owner):
    """Autopick цели dispatch -> имя | None. Годится только принимающая
    роль (receives_dispatch: мастер-роль не годится, даже чистая), со
    свободным клоном, своего проекта, с аренды никого или своего же
    владельца. Порядок -- по имени: один и тот же ростер -- один выбор."""
    fit = [r for r in rows
           if r.get("receives_dispatch") is True and r.get("free") is True
           and r.get("project") == project and r.get("owner") in (None, owner)]
    return min((r["name"] for r in fit), default=None)


# ─── каталог инструментов (#585) ─────────────────────────────────────────
# Что инструменту нужно от роли. Каталог -- не граница прав (её держат
# сервер и узел по резолверу), а честный список того, что роль может
# позвать: инструмент, на который сервер ответит отказом, модели не нужен.
HAND_TOOLS = {"agents": {"cluster_verbs": ("roster",)},
              "send": {"node_verbs": ("send",)},
              "tail": {"node_verbs": ("tail",)}, "slash": {"node_verbs": ("type",)},
              "dispatch": {"dispatch": True}}
CHANNEL_TOOLS = ("agents", "send", "tail")


def _permits(caps, need):
    return (set(need.get("node_verbs") or ()) <= set(caps.node_verbs)
            and set(need.get("cluster_verbs") or ()) <= set(caps.cluster_verbs)
            and (not need.get("dispatch") or caps.dispatch))


def tools_for(kind, caps, decls):
    """Вид говорящего, capabilities бандла, {имя: объявление MCP
    командлета} -> имена инструментов. Человек -- всё, как прежде, кроме
    dispatch (у него send с арендой); легаси-папет -- канал; роль -- по
    capabilities, командлет -- только назвавший свои глаголы (молчащее
    объявление роли не открывает); роль без бандла -- ничего. Обнаружение
    адресатов (agents) -- читающий глагол кластера roster, выданный
    декларативно, а не тайком всем ролям."""
    if kind == HUMAN:
        return (set(HAND_TOOLS) - {"dispatch"}) | set(decls)
    if kind == LEGACY:
        return set(CHANNEL_TOOLS)
    if caps is None:
        return set()
    out = {n for n, need in HAND_TOOLS.items() if _permits(caps, need)}
    return out | {n for n, d in decls.items() if _declares(d) and _permits(caps, d)}


def _declares(decl):
    """Назвал ли командлет, что ему нужно от роли (#585)."""
    return any(k in decl for k in ("node_verbs", "cluster_verbs", "dispatch"))


# ─── адрес ───────────────────────────────────────────────────────────────
def is_puppet(to):
    """Папет узнаётся префиксом джоба -- это единственное имя, которое
    строит сам пул."""
    return to.startswith(puppets.JOB_PREFIX)


def local_session(target):
    """Сессия на этой машине или None, если её здесь нет.

    `Ambiguous` не глушим и наверх пускаем: «здесь такой нет» разрешает искать
    адресата дальше, на шине, а «их тут несколько» обязано остановить —
    молча взять первую значит однажды написать не тому."""
    try:
        return session.find(target)
    except session.Ambiguous:
        raise
    except LookupError:
        return None


# Адрес мастера-человека: <токен логина>.<хост>-<pid> (#213) или прежний
# <хост>-<pid>. Роль пишет мастерам-людям в переходе только по такому.
MASTER_ADDRESS = re.compile(r"^(?:[^.\s]+\.)?\S+-\d+$")


def route(to, kind=HUMAN):
    """Адрес -> ("puppet", имя) | ("local", файл сессии) | ("master", инбокс).

    Порядок не произвольный. Дальше папета пробуем свой хост: сессия рядом
    дешевле и не занимает шину. Всё остальное — мастер, потому что больше
    адресов не бывает, и «не нашёл здесь» обязано вести к нему, а не в
    LookupError: ровно этим отчёты папетов и терялись.

    Роль (#585) адресует папетов по имени джоба и мастеров-людей по их
    адресу; сокеты соседей по хосту -- не её путь (на host тела -- соседи с
    общим HOME, docs/SOUL.md), а имя роли («builder») -- не адрес: таких
    папетов бывает несколько, и молча выбрать одного значит написать не
    тому."""
    if is_puppet(to):
        return "puppet", to
    if kind == GRANT:
        if MASTER_ADDRESS.match(to):
            return "master", to
        raise LookupError(f"{to}: not an address — name a puppet by its job name "
                          f"(pu-<project>-<n>) or a master by its address from agents "
                          f"or a received message")
    here = local_session(to)
    if here:
        return "local", here
    return "master", to


def puppet_node(name, master):
    """Узел папета — адрес на шине.

    У мастера узел берётся из ростера пула. На узле ростера нет, и мы
    спрашиваем сам пул: чей агент признаёт этот папет своим."""
    if master:
        return puppets.node_of(name)
    for answer in bus.gather("local"):
        if name in (answer.get("puppets") or {}):
            return answer["node"]
    raise LookupError(f"{name}: no pool agent claims this puppet as its own")


# ─── своя сессия ─────────────────────────────────────────────────────────
def master_socket():
    """Инбокс сессии, которая нас запустила, — для асинхронных уведомлений.

    Основной путь — переменная окружения, которую claude кладёт потомкам.
    Запасной нужен, если окружение вычистили: идём вверх по цепочке
    родителей и ищем pid, у которого есть файл сессии."""
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if sock:
        return sock
    by_pid = {str(d.get("pid")): d for d in session.sessions()}
    pid = os.getpid()
    for _ in range(24):
        try:
            with open(f"/proc/{pid}/status") as f:
                ppid = next(l.split()[1] for l in f if l.startswith("PPid:"))
        except (OSError, StopIteration):
            return None
        if ppid in by_pid:
            return by_pid[ppid]["messagingSocketPath"]
        pid = int(ppid)
        if pid <= 1:
            return None
    return None


def my_session():
    """Файл сессии, которая нас запустила: имя, каталог, pid."""
    sock = master_socket()
    if not sock:
        return None
    for d in session.sessions():
        if d.get("messagingSocketPath") == sock:
            return d
    return None


def my_name(master, master_id):
    """Как я представляюсь адресату — и каким именем он мне ответит.

    У мастера это его инбокс на шине (`master_id`): имя сессии claude адресом
    быть не может — оно выводится из каталога, не уникально и шине неизвестно.
    Оператор, спрашивавший «пингани их, пусть ответят», назвал папетам именно
    имя сессии, и ответить по нему было некуда.

    У папета это имя его джоба: оно же имя клона, им же папет адресуется в
    send. Берём из каталога сессии, а не из cwd процесса: сервер запускают
    откуда угодно, а сессия папета всегда стоит в своём клоне."""
    if master:
        return master_id
    cwd = (my_session() or {}).get("cwd") or os.getcwd()
    base = os.path.basename(os.path.realpath(cwd))
    return base if is_puppet(base) else master_id


# ─── доставка: вердикт, а не факт отправки ───────────────────────────────
# Вердикт -- dict: kind (puppet|local|master), to, и либо msg_id, либо error
# (отказ агента или мастера, в том числе владельца задания, #161), либо dead
# (инбокс сессии не слушает). Плюс owner_note, idle, wait, notify, pid.
# Сбой шины -- bus.BusError, как везде в библиотеке: `mop send` отдаёт его
# диспетчеру, MCP -- в вердикт через undelivered.


def send_to_puppet(node, name, message, priority="next", wait=0, notify=None,
                   via="rpc", **fields):
    """Доставка через агента узла. -> вердикт | bus.BusError.

    Тонкая обёртка (#459): тело -- общая операция ops.send_to_puppet
    (#458), транспорт -- bus.request этого слоя. Ожидание простоя целиком
    на узле; notify=None -- поле в запрос не идёт вовсе; остальные поля
    (owner, force, from_name, reply_to) едут агенту как есть. via --
    канал агента (send_channel): rpc идёт прежним вызовом, без поля."""
    transport = bus.request if via == "rpc" else functools.partial(bus.request, channel=via)
    return ops.send_to_puppet(transport, node, name, message,
                              priority=priority, wait=wait, notify=notify,
                              **fields)


def dispatch_to_puppet(node, name, message, priority="next", **fields):
    """Задание роли (#579): отдельная операция в своём канале, не «send с
    флагом». Аренду и git identity ставит узел по резолверу (#584), если
    сервер разрешит; в теле запроса авторитета нет -- кто просит, узел
    берёт из субъекта. Вердикт -- та же форма, что у send."""
    def transport(node, verb, **kw):
        return bus.request(node, "dispatch", channel=busnames.DISPATCH_CHANNEL, **kw)
    return ops.send_to_puppet(transport, node, name, message, priority=priority,
                              **fields)


def send_local(sess, message, priority="next", wait=0, **fields):
    """Доставка в сессию этого хоста, прямо в её сокет. -> вердикт."""
    v = {"kind": "local", "to": sess.get("name"), "pid": sess.get("pid"),
         "wait": clamp_wait(wait)}
    sock = sess["messagingSocketPath"]
    if not session.socket_alive(sock):
        return dict(v, dead=True)
    r = session.send(sock, message, priority=priority, **fields, wait_idle=v["wait"])
    return dict(v, msg_id=r["msg_id"], idle=r["idle"])


def send_to_master(name, message, priority, from_name):
    """Обратный канал: папет -> мастер, в его инбокс на шине. -> вердикт |
    bus.BusError.

    Адрес мастера папет не придумывает: он приезжает в конверте каждого
    сообщения (from-name) и лежит в ростере. Ответа ждём — вердикт доставки
    здесь важнее, чем где-либо ещё: отчёт папета и есть главный сигнал петли,
    и «отправлено» вместо «доставлено» означало бы ровно то молчание, из-за
    которого мастер идёт читать чужой экран глазами."""
    # from — поле, а не имя параметра: в питоне это ключевое слово.
    r = bus.ask(name, "message", text=message, priority=priority, **{"from": from_name})
    v = {"kind": "master", "to": name}
    if "error" in r:
        return dict(v, error=r["error"])
    return dict(v, msg_id=r.get("msg_id"))


def undelivered(kind, to, error):
    """Вердикт из сбоя шины: для отправителя это тот же отказ."""
    return {"kind": kind, "to": to, "error": str(error)}


# ─── вердикт -> текст ────────────────────────────────────────────────────
def failure(v):
    """Строка отказа, либо None, если доставлено.

    Неопределённая доставка (timeout, #574 review-3 п.5) -- своей строкой:
    «неизвестно» не «отказано», сообщение могло доехать -- повтор мог бы
    продублировать уже выполненное и автоматически не делается."""
    if v.get("dead"):
        return f"{v['to']}: inbox not listening — session is dead"
    if v.get("error") is not None:
        if v.get("timeout"):
            return f"{v['to']}: DELIVERY UNKNOWN — {v['error']}"
        return f"{v['to']}: NOT DELIVERED — {v['error']}"
    return None


def text(v):
    """Вердикт одной строкой -- так его читает модель."""
    why = failure(v)
    if why:
        return why
    out = f"{v['to']}: delivered (msg_id={v.get('msg_id')})"
    wait = v.get("wait")
    if v["kind"] == "puppet":
        if v.get("owner_note"):
            out += f"; {v['owner_note']}"
        if wait:
            # Причина ошибки наблюдения (#574) -- вместо безымянного «не
            # дождался»: узел сказал, чем кончилось ожидание.
            out += f", idle: {v.get('idle_error') or v.get('idle') or 'did not wait it out in ' + str(wait) + 's'}"
        if v.get("notify"):
            out += "; will notify when it frees up"
    elif v["kind"] == "local" and wait:
        out += f", idle: {(v.get('idle') or {}).get('state') or 'did not wait it out'}"
    return out
