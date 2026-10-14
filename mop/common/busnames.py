"""Имена на шине: субъекты, пользователи NATS, файлы их паролей (#144).

Одно определение на всех. Раньше те же шаблоны были набраны руками в шести
модулях: права (natsconf, operators) отдельно от подписок (agent, дашборд,
сервисы сервера), и переименование субъекта в одном месте молча разводило
права и подписки -- отказ прав NATS приезжает в error_cb, а не в ответ, и
читается как «агент молчит» (docs/BUS.md).

Только stdlib, а из пакета -- только paths (тоже stdlib): модуль нужен и
тем, кто nats не импортирует (natsconf, operators), и агенту узла. Карта субъектов и прав --
в docs/BUS.md.
"""
import re

from . import paths

ROOT = "mop"             # проектные субъекты; личный join живёт вне этой маски
INBOX = "_INBOX.>"       # ответы request-reply: без него запрос молча не работает
ANY = "*"                # любой проект: маска подписчика, не адрес
ADMIN = "admin"          # псевдопроект оператора: все проекты плюс узловое
ALL_MASTERS = "all"      # псевдо-id мастера: инбокс, на котором отвечают все
CLUSTER_CHANNEL = "cluster"   # токен субъекта сервиса кластера (mop/server/cluster.py)
# Креды агента узла: их кладёт плейбук. Путь здесь, а не в bus, потому что
# драйвер host берёт из их url адрес сервера (#200), а nats ему не нужен.
NODE_FILE = f"~/{paths.DIR}/bus.json"
# Канал, в котором человек называет себя токеном субъекта (#207): управляющий
# rpc агента и сервис кластера. Публичный msg -- папетов, логина там нет.
CALLER_CHANNEL = "rpc"
JOIN_ROOT = "mopjoin"       # вне mop.>: проектная маска не выдаёт ключ прокси


# ─── субъекты ────────────────────────────────────────────────────────────
def everything(project=None):
    """Весь пул, либо весь проект."""
    return f"{ROOT}.>" if project is None else f"{ROOT}.{project}.>"


DISPATCH_CHANNEL = "dispatch"  # канал dispatch (#579): операция, не «send с флагом»
# Канал принципалов роли (#603): грант не ездит по человеческому rpc. Старый
# агент, подписанный на `...rpc.*`, получил бы запрос гранта и прочитал бы
# его как человеческий без резолвера -- смешанная раскатка. Своего канала --
# и агента (#584), и ACL (#581) подписывают только на новый шаблон.
ROLE_CHANNEL = "role"
# Канал старта своего узла (#603): первую identity роль запрашивает у своего
# же агента до входа принципала; человек и чужой узел туда не пишут.
STARTUP_CHANNEL = "startup"


# Приватные ответы машин нового мира (#603): _MOP_REPLY.<user>.<NUID>.
# Публичный _INBOX.> унаследован от легаси и обслуживает всех подряд; ответ
# узла и принципала несёт служебные данные (креды роли), и приватный
# префикс с собственной ACL-строкой (#581) отделяет его от человеческих
# ответов. Люди на _INBOX и остаются.
REPLY_PREFIX = "_MOP_REPLY"


def private_prefix(user):
    """Префикс приватных ответов машины: _MOP_REPLY.<user>. user -- полное
    машинное имя (узел node-<имя> или принципал grant-<id>); человек
    приватного префикса не имеет."""
    return f"{REPLY_PREFIX}.{user}"


def private_scope(user):
    """Маска подписки на свои приватные ответы: _MOP_REPLY.<user>.>."""
    return f"{private_prefix(user)}.>"


def valid_private_reply(subject, user):
    """Это -- приватный ответ ИМЕННО этому пользователю? Чистая. Свой
    префикс и непустой хвост-NUID; всё остальное -- нет: чужой приватный
    префикс и публичный _INBOX не читаются как свой (#581 проверяет то же
    ACL-строкой, здесь -- разбором)."""
    if not isinstance(subject, str) or not subject.startswith(
            private_prefix(user) + "."):
        return False
    return bool(subject[len(private_prefix(user)) + 1:])


def is_grant(user):
    """Это имя -- делегированный принципал роли (grant-<32 hex>)? Грамматика
    живёт здесь, у префикса (#603): busnames не может импортировать soul
    (soul импортирует busnames), а субъекту роль опознаёт наравне с логином
    человека. soul.is_grant -- тот же ответ делегированием: грамматика одна."""
    return isinstance(user, str) \
        and user.startswith(GRANT_PREFIX) \
        and bool(_GRANT_ID.match(user[len(GRANT_PREFIX):]))


def node(project, name, channel="rpc", login=None):
    """Агент узла. login -- токен вызывающего: человек в rpc (#207),
    принципал роли -- в своём канале role, диспетчер -- полным принципалом
    в dispatch (#579). Грант в rpc переводится на role сам: вызывающий
    говорит «обычный запрос» (bus.request без канала), а по какому субъекту
    он едет -- решает имя, не вызывающий (#603). Публичный msg для гранта
    -- ValueError: это забор протокола, а не неудобство."""
    subj = f"{ROOT}.{project}.node.{name}.{channel}"
    if not login:
        return subj
    if channel == DISPATCH_CHANNEL:
        return dispatch_subject(project, name, login)
    if is_grant(login):
        if channel in (CALLER_CHANNEL, ROLE_CHANNEL):
            return f"{ROOT}.{project}.node.{name}.{ROLE_CHANNEL}.{login}"
        raise ValueError(
            f"a delegated principal rides only the {ROLE_CHANNEL} and "
            f"{DISPATCH_CHANNEL} channels, not {channel}")
    # Человек своё имя возит только в rpc (#207); прочие каналы публичны, и
    # токен там молча не ставился и раньше.
    return f"{subj}.{_token(login)}" if channel == CALLER_CHANNEL else subj


def dispatch_subject(project, name, user):
    """Субъект dispatch (#579): node.<p>.<узел>.dispatch.<принципал>.

    Токен -- полный принципал grant-<id> (#603): голый hex на проводе не
    появляется, и субъект узла (#584) опознаёт диспетчера одним правилом с
    каналом role. Не принципал -- ValueError: человек канал dispatch не
    получает, режим определяет резолвер, а не роль отправителя."""
    if not is_grant(user):
        raise ValueError(f"dispatch carries the full principal "
                         f"{GRANT_PREFIX}<id>, got {user!r}")
    return f"{ROOT}.{project}.node.{name}.{DISPATCH_CHANNEL}.{user}"


def broadcast(project):
    """Все агенты разом: в субъект с маской послать нельзя."""
    return f"{ROOT}.{project}.all.msg"


def inbox(project, master_id):
    return f"{ROOT}.{project}.master.{master_id}.inbox"


# Адрес мастера (#213) -- <токен логина>.<хост>-<pid>: логин владельца
# впереди, чтобы подписку человека сузить до своих инбоксов
# (mop.<p>.master.<токен>.>). Прежний <хост>-<pid> логина не нёс, и любой
# мастер проекта слушал все инбоксы, то есть чужие отчёты папетов. Хост
# бывает с точками -- поэтому логин первым токеном, а не последним; в самом
# токене логина точек нет (login_token).
def master_address(login, local):
    """Логин владельца и <хост>-<pid> -> адрес мастера (его from-name)."""
    return f"{login_token(login)}.{local}"


def own_masters(project, login):
    """Инбоксы мастеров одного человека: всё, что ему можно слушать из
    master.* кроме опроса who (inbox(project, ALL_MASTERS))."""
    return f"{ROOT}.{project}.master.{_token(login)}.>"


def masters(project):
    """Инбоксы всех мастеров проекта, включая опрос `who`: куда можно
    ПИСАТЬ (папеты, узлы, люди). Слушать так -- никому из людей (#213)."""
    return f"{ROOT}.{project}.master.>"


def events(project):
    return f"{ROOT}.{project}.events"


def cluster(project, login=None):
    """Сервис кластера. login -- токен вызывающего: человек в rpc (#207),
    принципал роли -- в своём канале role (#603): тот же забор, что у агента
    узла (node)."""
    if is_grant(login):
        return f"{ROOT}.{project}.{CLUSTER_CHANNEL}.{ROLE_CHANNEL}.{login}"
    subj = f"{ROOT}.{project}.{CLUSTER_CHANNEL}.{CALLER_CHANNEL}"
    return f"{subj}.{_token(login)}" if login else subj


def startup_subject(project, node):
    """Канал старта своего узла (#603): mop.<p>.node.<узел>.startup.

    Первую identity роль запрашивает у своего же агента до входа
    принципала: узел отвечает здесь на старт тела, человек и чужой узел не
    пишут (проверяет bus.request, ACL -- #581)."""
    return f"{ROOT}.{project}.node.{node}.{STARTUP_CHANNEL}"


def join_config(login):
    """Личный адрес запроса конфигурации: вне проектных масок и server.rpc."""
    return f"{JOIN_ROOT}.{_token(login)}.{CALLER_CHANNEL}"


# ─── вызывающий в субъекте (#207) ────────────────────────────────────────
# Кто просит агента и сервис кластера, раньше называло тело запроса -- то
# есть сам проситель. Теперь логин человека -- последний токен субъекта:
#   mop.<проект>.node.<узел>.rpc.<логин>
#   mop.<проект>.cluster.rpc.<логин>
# Публиковать туда NATS даёт только этому человеку (operators.permissions),
# машинам -- никому (natsconf). Прежние субъекты без логина живут до уборки
# перехода: логин там по-прежнему называет тело (self-declared).
#
# Логин в токене -- закодированный: в LDAP/AD anton.ermak -- норма (#208), а
# точка разрезала бы его на два токена. NATS пускает в токен любой UTF-8,
# кроме пробельных; `.` -- разделитель, `*` и `>` -- маски (docs.nats.io,
# Subject-Based Messaging). Эти символы и сам `%` кодируются процентом по
# байтам UTF-8: anton.ermak -> anton%2Eermak. `%` кодируется всегда, поэтому
# кодирование инъективно: «a.b» и буквальное «a%2Eb» -- разные токены.
_ESCAPED = set(".*>%")


def valid_login(login):
    """Годится ли логин: непустая строка без управляющих символов (таб,
    перевод строки тоже управляющие). Остальное кодирует login_token."""
    return bool(login) and isinstance(login, str) and \
        not any(ord(c) < 32 or 127 <= ord(c) < 160 for c in login)


def login_token(login):
    """Логин -> токен субъекта. Обратная -- login_of."""
    return "".join("".join(f"%{b:02X}" for b in c.encode())
                   if c in _ESCAPED or c.isspace() else c for c in login)


def login_of(token):
    """Токен субъекта -> логин, либо None: токен не канонический (строчные
    цифры, лишнее кодирование, битый `%`) -- не логин, иначе один логин
    читался бы из двух токенов."""
    import urllib.parse
    try:
        login = urllib.parse.unquote(token, errors="strict")
    except (UnicodeDecodeError, TypeError):
        return None
    return login if valid_login(login) and login_token(login) == token else None


def _token(login):
    """Токен вызывающего в адресе: маска подписчика (`*`) -- как есть."""
    return login if login == ANY else login_token(login)


def caller(subject):
    """Вызывающий из субъекта, либо None: прежний субъект без токена,
    публичный канал или не наш субъект.

    Человек -- в rpc (#207), принципал роли -- в role и dispatch (#603),
    узел -- в квалифицированном server.rpc (#603). Забор с обеих сторон
    (#603): грант, проехавший по человеческому каналу, вызывающим не
    читается -- старый агент принимает его без резолвера за человека, --
    и наоборот, не-принципал в каналах роли не читается вовсе."""
    parts = (subject or "").split(".")
    if len(parts) == 6 and parts[0] == ROOT and parts[2] == "node" and parts[5]:
        if parts[4] in (ROLE_CHANNEL, DISPATCH_CHANNEL):
            return parts[5] if is_grant(parts[5]) else None
        if parts[4] == CALLER_CHANNEL:
            login = login_of(parts[5])
            return None if is_grant(login) else login
    elif len(parts) == 5 and parts[0] == ROOT and parts[2] == CLUSTER_CHANNEL:
        if parts[3] == ROLE_CHANNEL:
            return parts[4] if is_grant(parts[4]) else None
        if parts[3] == CALLER_CHANNEL:
            login = login_of(parts[4])
            return None if is_grant(login) else login
    elif len(parts) == 5 and parts[0] == ROOT and parts[2] == "server" \
            and parts[3] == CALLER_CHANNEL:
        # Квалифицированный server.rpc -- узловой (#603): токен -- машинное
        # имя узла, человек там не вызывающий.
        return parts[4] if parts[4].startswith(NODE_PREFIX) else None
    elif len(parts) == 3 and parts[0] == JOIN_ROOT and parts[2] == CALLER_CHANNEL:
        return login_of(parts[1])
    return None


def without_caller(subject):
    """Тот же адрес без вызывающего: прежний субъект, на который отвечает
    агент или сервис до #207 (переход)."""
    return subject.rsplit(".", 1)[0] if caller(subject) else subject


def server(project, caller=None):
    """Сервер как адресат (#62). caller -- машинное имя узла (#603):
    квалифицированный субъект mop.<p>.server.rpc.node-<узел> отличает
    запрос узла от анонимного легаси-пути; человек и сервис остаются на
    прежнем субъектe."""
    subj = f"{ROOT}.{project}.server.rpc"
    return f"{subj}.{caller}" if caller else subj


def build():
    """Сборщик образов (#123): только оператору."""
    return f"{ROOT}.{ADMIN}.build.rpc"


def agent_subscriptions(name):
    """На что подписан агент узла: {rpc: [...], msg: [...]}. Маска по проекту
    -- агент обслуживает всех жильцов узла, а подписка только на свой узел:
    в NATS она не эксклюзивна, и соседний агент мог бы ответить первым."""
    return {"rpc": [node(ANY, name, "rpc"), node(ANY, name, "rpc", login=ANY)],
            "msg": [node(ANY, name, "msg"), broadcast(ANY)]}


def service_subscriptions():
    """На что подписаны сервисы сервера под `service`: bootstrap, кластер,
    сборщик, журнал дашборда."""
    return [server(ANY), cluster(ANY), cluster(ANY, login=ANY), join_config(ANY),
            build(), events(ANY)]


# ─── пользователи ────────────────────────────────────────────────────────
SERVICE = "service"      # машинный пользователь сервисов сервера (#104)
CALLOUT = "callout"      # сервис auth callout (#206): отвечает NATS, кто входит
MASTER_PREFIX, PUPPET_PREFIX, NODE_PREFIX = "master-", "puppet-", "node-"
# Принципал делегированной роли (#579): grant-<id>, id -- 32 знака hex.
# Префикс новый и зарезервированный: имени проекта не суффиксируется, и с
# легаси-пользователем puppet-<проект> не сталкивается. Кодировки id --
# mop/common/soul.py (grant_user/grant_id), здесь только имя префикса.
GRANT_PREFIX = "grant-"
# Ид той же грамматики, что у soul.HEX32 (#580): busnames не импортирует
# soul (обратный импорт), а субъекту грант опознавать надо. Расхождение
# ловит tests/soul.py и tests/transport.py -- грамматика одна.
_GRANT_ID = re.compile(r"^[0-9a-f]{32}$")


def master_user(project):
    return f"{MASTER_PREFIX}{project}"


def puppet_user(project):
    return f"{PUPPET_PREFIX}{project}"


def node_user(name):
    return f"{NODE_PREFIX}{name}"


def is_puppet(user):
    return user.startswith(PUPPET_PREFIX)


def is_machine(user):
    """Не человек: сервис сервера, callout, папет, узел или принципал
    делегированной роли (grant-<id>, #579)."""
    return user in (SERVICE, CALLOUT) or user.startswith(
        (PUPPET_PREFIX, NODE_PREFIX, GRANT_PREFIX))


# Имена ролей на шине: человеку их не выдать (operators.RESERVED).
RESERVED = (ADMIN, SERVICE, CALLOUT)
RESERVED_PREFIXES = (MASTER_PREFIX, PUPPET_PREFIX, NODE_PREFIX, GRANT_PREFIX)


# ─── файлы паролей: те же имена, что в secrets/ сервера ──────────────────
def pass_file(user):
    return f"nats-{user}.pass"


def operator_pass_file(name):
    return f"nats-op-{name}.pass"


SERVICE_PASS_FILE = pass_file(SERVICE)

# Сколько узел ждёт от сервера ответа на bootstrap песочницы (#62): одна
# цифра на обе стороны, иначе сервер играл бы дольше, чем узел ждёт.
BOOTSTRAP_TIMEOUT = 300
