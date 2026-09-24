"""Имена на шине: субъекты, пользователи NATS, файлы их паролей (#144).

Одно определение на всех. Раньше те же шаблоны были набраны руками в шести
модулях: права (natsconf, operators) отдельно от подписок (agent, дашборд,
сервисы сервера), и переименование субъекта в одном месте молча разводило
права и подписки -- отказ прав NATS приезжает в error_cb, а не в ответ, и
читается как «агент молчит» (docs/BUS.md).

Только stdlib и без импортов из пакета: модуль нужен и тем, кто nats не
импортирует (natsconf, operators), и агенту узла. Карта субъектов и прав --
в docs/BUS.md.
"""
ROOT = "mop"             # первый токен всех субъектов пула
INBOX = "_INBOX.>"       # ответы request-reply: без него запрос молча не работает
ANY = "*"                # любой проект: маска подписчика, не адрес
ADMIN = "admin"          # псевдопроект оператора: все проекты плюс узловое
ALL_MASTERS = "all"      # псевдо-id мастера: инбокс, на котором отвечают все
CLUSTER_CHANNEL = "cluster"   # токен субъекта сервиса кластера (mop/cluster.py)
# Креды агента узла: их кладёт плейбук. Путь здесь, а не в bus, потому что
# драйвер host берёт из их url адрес сервера (#200), а nats ему не нужен.
NODE_FILE = "~/.config/mop/bus.json"


# ─── субъекты ────────────────────────────────────────────────────────────
def everything(project=None):
    """Весь пул, либо весь проект."""
    return f"{ROOT}.>" if project is None else f"{ROOT}.{project}.>"


def node(project, name, channel="rpc"):
    return f"{ROOT}.{project}.node.{name}.{channel}"


def broadcast(project):
    """Все агенты разом: в субъект с маской послать нельзя."""
    return f"{ROOT}.{project}.all.msg"


def inbox(project, master_id):
    return f"{ROOT}.{project}.master.{master_id}.inbox"


def masters(project):
    """Инбоксы всех мастеров проекта, включая опрос `who`."""
    return f"{ROOT}.{project}.master.>"


def events(project):
    return f"{ROOT}.{project}.events"


def cluster(project):
    return f"{ROOT}.{project}.{CLUSTER_CHANNEL}.rpc"


def server(project):
    return f"{ROOT}.{project}.server.rpc"


def build():
    """Сборщик образов (#123): только оператору."""
    return f"{ROOT}.{ADMIN}.build.rpc"


def agent_subscriptions(name):
    """На что подписан агент узла: {rpc: [...], msg: [...]}. Маска по проекту
    -- агент обслуживает всех жильцов узла, а подписка только на свой узел:
    в NATS она не эксклюзивна, и соседний агент мог бы ответить первым."""
    return {"rpc": [node(ANY, name, "rpc")],
            "msg": [node(ANY, name, "msg"), broadcast(ANY)]}


def service_subscriptions():
    """На что подписаны сервисы сервера под `service`: bootstrap, кластер,
    сборщик, журнал дашборда."""
    return [server(ANY), cluster(ANY), build(), events(ANY)]


# ─── пользователи ────────────────────────────────────────────────────────
SERVICE = "service"      # машинный пользователь сервисов сервера (#104)
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
    """Не человек: сервис сервера, папет или узел."""
    return user == SERVICE or user.startswith((PUPPET_PREFIX, NODE_PREFIX))


# Имена ролей на шине: человеку их не выдать (operators.RESERVED).
RESERVED = (ADMIN, SERVICE)
RESERVED_PREFIXES = (MASTER_PREFIX, PUPPET_PREFIX, NODE_PREFIX)


# ─── файлы паролей: те же имена, что в secrets/ сервера ──────────────────
def pass_file(user):
    return f"nats-{user}.pass"


def operator_pass_file(name):
    return f"nats-op-{name}.pass"


SERVICE_PASS_FILE = pass_file(SERVICE)
