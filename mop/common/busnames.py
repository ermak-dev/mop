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
from . import paths

ROOT = "mop"             # первый токен всех субъектов пула
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


def node(project, name, channel="rpc", login=None):
    """Агент узла. login -- токен вызывающего (#207), только в rpc."""
    subj = f"{ROOT}.{project}.node.{name}.{channel}"
    return f"{subj}.{_token(login)}" if login and channel == CALLER_CHANNEL else subj


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
    """Сервис кластера. login -- токен вызывающего (#207)."""
    subj = f"{ROOT}.{project}.{CLUSTER_CHANNEL}.{CALLER_CHANNEL}"
    return f"{subj}.{_token(login)}" if login else subj


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
    """Логин вызывающего из субъекта, либо None: прежний субъект без логина,
    публичный канал или не наш субъект."""
    parts = (subject or "").split(".")
    if len(parts) == 6 and parts[0] == ROOT and parts[2] == "node" \
            and parts[4] == CALLER_CHANNEL:
        login = parts[5]
    elif len(parts) == 5 and parts[0] == ROOT and parts[2] == CLUSTER_CHANNEL \
            and parts[3] == CALLER_CHANNEL:
        login = parts[4]
    elif len(parts) == 3 and parts[0] == JOIN_ROOT and parts[2] == CALLER_CHANNEL:
        login = parts[1]
    else:
        return None
    return login_of(login)


def without_caller(subject):
    """Тот же адрес без логина: прежний субъект, на который отвечает агент
    или сервис до #207 (переход)."""
    return subject.rsplit(".", 1)[0] if caller(subject) else subject


def server(project):
    return f"{ROOT}.{project}.server.rpc"


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


def master_user(project):
    return f"{MASTER_PREFIX}{project}"


def puppet_user(project):
    return f"{PUPPET_PREFIX}{project}"


def node_user(name):
    return f"{NODE_PREFIX}{name}"


def is_puppet(user):
    return user.startswith(PUPPET_PREFIX)


def is_machine(user):
    """Не человек: сервис сервера, callout, папет или узел."""
    return user in (SERVICE, CALLOUT) or user.startswith((PUPPET_PREFIX, NODE_PREFIX))


# Имена ролей на шине: человеку их не выдать (operators.RESERVED).
RESERVED = (ADMIN, SERVICE, CALLOUT)
RESERVED_PREFIXES = (MASTER_PREFIX, PUPPET_PREFIX, NODE_PREFIX)


# ─── файлы паролей: те же имена, что в secrets/ сервера ──────────────────
def pass_file(user):
    return f"nats-{user}.pass"


def operator_pass_file(name):
    return f"nats-op-{name}.pass"


SERVICE_PASS_FILE = pass_file(SERVICE)

# Сколько узел ждёт от сервера ответа на bootstrap песочницы (#62): одна
# цифра на обе стороны, иначе сервер играл бы дольше, чем узел ждёт.
BOOTSTRAP_TIMEOUT = 300
