"""Роли для лаунчера клиента (#586): каталог, зарезервированные имена,
аргументы `mop <роль>`, ожидание готовности тела.

Разбор soul.yaml, бандл и каталог -- общий модуль #580 (mop/common/soul.py);
здесь только то, что нужно стороне оператора: из какого каталога брать
роли, какие имена роли взять не может и как понять, что в теле уже есть
сессия. Библиотека молчит: отказ -- исключение с причиной, печатает
командлет.
"""
import os

from mop.common import busnames, soul

# Каталог ролей пакета (#580): skills/<роль>/ рядом с пакетом, а не с
# клоном -- у клиента из git skills/ клона нет (#351).
PACKAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__))), "skills")

# Имена шины, которые роль не берёт (docs/SOUL.md, «CLI, имена»): роль с
# таким именем читалась бы как служебный пользователь или принципал.
RESERVED = busnames.RESERVED
RESERVED_PREFIXES = busnames.RESERVED_PREFIXES

# Встроенная команда, которая одновременно имя роли: `mop master` -- явный
# алиас мастер-роли в переходе (docs/SOUL.md), а не затенение.
ROLE_COMMANDS = ("master",)


def dirs():
    """Каталоги ролей по старшинству. Пока общий модуль (#580) не назвал
    корни установки одним местом, клиент берёт только каталог пакета: своя
    таблица внешних корней здесь разошлась бы с серверной."""
    return [PACKAGE_DIR]


def collisions(roles, commands):
    """Имена ролей, которые роль взять не может -> [(роль, почему)].
    Чистая функция. commands -- имена встроенных команд, групп и прежних
    алиасов диспетчера."""
    out = []
    for role in sorted(roles):
        if role in commands and role not in ROLE_COMMANDS:
            out.append((role, "a built-in mop command"))
        elif role in RESERVED:
            out.append((role, "a reserved bus name"))
        elif role.startswith(RESERVED_PREFIXES):
            out.append((role, "a reserved bus prefix"))
    return out


def catalogue(commands, where=None):
    """Каталог ролей, проверенный на коллизии -> {роль: каталог}.

    Сломанный soul.yaml или коллизия -- ValueError на весь каталог, а не на
    одну роль: роль, молча пропавшая или затенившая команду, хуже громкого
    отказа (docs/SOUL.md)."""
    try:
        found = soul.catalogue(dirs() if where is None else where)
    except ValueError as e:
        raise ValueError(f"role catalogue: {e}") from None
    bad = collisions(found, commands)
    if bad:
        raise ValueError("role catalogue: " + "; ".join(
            f"role {r} ({found[r]}) collides with {why}" for r, why in bad))
    return found


def options(argv):
    """argv лаунчера -> origin | None. Чистая функция. Роль берёт только
    [origin]: промпт, настройки, MCP, права и продолжение разговора --
    дело роли и её обёртки (#583), а модель и усилие пасстру не довозит,
    пока их не несут сервер и обёртка (#582/#583). Любой флаг -- ValueError,
    отказ до аллокации, а не молча потерянная опция."""
    origin = None
    for a in argv:
        if a.startswith("-"):
            raise ValueError(f"{a}: a role launch takes only [origin]")
        if origin is not None:
            raise ValueError(f"{a}: one origin only")
        origin = a
    return origin


# Рукопожатие (#586): ответ ping сервиса кластера с ролями (#582) несёт
# версию протокола ролей и признак сведения повторов по request_id.
PROTOCOL_KEY = "soul_protocol"


def protocol(reply):
    """Ответ ping -> сводит ли сервер повторы по request_id. Чистая.
    Сервер без протокола ролей или другой версии -- ValueError до создания:
    старый сервер молча завёл бы безролевого папета."""
    got = (reply or {}).get(PROTOCOL_KEY)
    if not isinstance(got, dict) or got.get("version") != soul.VERSION:
        raise ValueError(f"the server does not support roles (protocol "
                         f"{soul.VERSION} wanted, got {got!r}); nothing is created")
    return got.get("request_id") is True


TRIPLE = ("soul", "soul_digest", "soul_inc")


def echo(got, role):
    """Ответ add -> тройка роли {soul, soul_digest, soul_inc}. Чистая.
    Неполная или чужая тройка -- ValueError: сервер не подтвердил роль."""
    triple = {k: (got or {}).get(k) for k in TRIPLE}
    if triple["soul"] != role or not all(triple.values()):
        raise ValueError(f"the server did not confirm role {role} "
                         f"(got {triple!r})")
    return triple


def session_ready(answers, node, name, triple):
    """Ответ узловых states -> готова ли сессия роли. Чистая функция.

    Готова -- tmux-сессия есть И в фактах стоит тройка роли, совпавшая с
    эхом регистрации: метку ставит обёртка только после сверки бандла
    (#583/#590), и голый tmux может быть прежней ролью или bootstrap'ом
    без неё. Узел отказал или молчит -- RuntimeError."""
    got = (answers or {}).get(node)
    if isinstance(got, Exception):
        raise RuntimeError(f"agent on {node}: {got}")
    facts = ((got or {}).get("puppets") or {}).get(name) or {}
    if facts.get("error"):
        raise RuntimeError(f"{name} on {node}: {facts['error']}")
    return bool(facts.get("present")) and facts.get("soul") == triple
