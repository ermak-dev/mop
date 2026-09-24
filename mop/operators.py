"""Операторы установки: люди, у которых есть доступ к пулу. Данные, без печати.

Оператор представлялся шине ролью — `master-<проект>`, с паролем, общим на
всех операторов проекта, и привозил его копированием каталога по ssh. Отсюда
два следствия: отозвать доступ одному человеку было нельзя, не сменив пароль
остальным, и сама установка с раздельными сервером и контроллером по
документации не заводилась (#88).

Теперь оператор — пользователь NATS с именем человека и правами ровно на свои
проекты. Список живёт в настройке `MOP_OPERATORS`, пароли заводит
`lookup('password')` там же, где и все остальные, а `mop join` спрашивает
логин и пароль вместо того, чтобы везти чужой каталог.

Формат настройки (#106) -- `имя:роль[:проекты]`, точка с запятой между
людьми, запятая между проектами:

    anton:admin                 весь пул плюс машинные глаголы (mop.admin.*)
    ivan:user:rugent,cloudpub   только названные проекты
    olga:user:*                 все проекты, но не машины

Прежняя запись без роли (`имя:*`, `имя:проект,проект`) читается так, как
работала до #106: `*` давал весь mop.>, то есть admin.
"""
from . import busnames

# Имена, которыми на шине зовутся роли. Человек с таким именем — не второй
# оператор, а тихая подмена роли: права роли и права человека сложились бы в
# одного пользователя, и понять по конфигу, чьи они, стало бы нельзя.
RESERVED = busnames.RESERVED
RESERVED_PREFIXES = busnames.RESERVED_PREFIXES
ALL = "*"


# Роль, а не псевдопроект busnames.ADMIN: совпадает только написание.
ADMIN, USER = "admin", "user"
ROLES = (ADMIN, USER)


def entry(name, fields):
    """Поля записи после имени -> {role, projects}. Отказ громкий."""
    def projects_of(text):
        got = [p.strip() for p in text.split(",") if p.strip()]
        if not got:
            # Пустые права -- не «на всё»: прав по умолчанию не бывает, а
            # опечатка `anton:` иначе выдала бы весь пул.
            raise ValueError(f"MOP_OPERATORS: {name} has no projects; "
                             f"write {name}:user:<projects> or {name}:admin")
        return got
    if len(fields) == 1 and fields[0].strip() not in ROLES:
        # Прежняя запись без роли: `*` был весь mop.>, то есть admin.
        projects = projects_of(fields[0])
        if ALL in projects:
            return {"role": ADMIN, "projects": [ALL]}
        return {"role": USER, "projects": projects}
    role = fields[0].strip()
    if role == ADMIN and len(fields) == 1:
        return {"role": ADMIN, "projects": [ALL]}
    if role == ADMIN:
        # admin с проектами -- противоречие: запись говорила бы одно, а
        # права были бы другими.
        raise ValueError(f"MOP_OPERATORS: {name} is admin, which is the whole "
                         f"pool; drop the projects, or make {name} a user")
    if role == USER and len(fields) == 2:
        return {"role": USER, "projects": projects_of(fields[1])}
    raise ValueError(f"MOP_OPERATORS: {name}: expected {name}:admin or "
                     f"{name}:user:<projects>, got {':'.join(fields)!r}")


def parse(setting):
    """`MOP_OPERATORS` -> {имя: {role, projects}}. Пустая настройка -> {}.

    Отказы громкие и на разбор, а не на прогон: список едет в конфиг NATS, и
    ошибка в нём -- это либо доступ, которого никто не давал, либо мастер,
    который не подключится."""
    out = {}
    for chunk in (setting or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, *fields = [f.strip() for f in chunk.split(":")]
        if not name:
            raise ValueError(f"MOP_OPERATORS: no name in {chunk!r}")
        if not fields or not fields[0]:
            raise ValueError(f"MOP_OPERATORS: {name} has no role; "
                             f"write {name}:user:<projects> or {name}:admin")
        if name in RESERVED or name.startswith(RESERVED_PREFIXES):
            raise ValueError(f"MOP_OPERATORS: {name!r} is a role on the bus, "
                             f"not a person — pick another name")
        out[name] = entry(name, fields)
    return out


def permissions(op):
    """Права пользователя субъектами. -> {allow: [маски], deny: [маски]}.

    `_INBOX.>` обязателен всем, иначе request-reply молча не работает -- на
    этом однажды стоял целый вечер разбора (docs/BUS.md). user на весь пул
    получает mop.> без mop.admin.>: иначе вместе с проектами ему достались
    бы узлы -- disk, drain, forget.

    op -- identity.Identity (#205) или словарь разбора {role, projects}."""
    role, projects = ((op["role"], op["projects"]) if isinstance(op, dict)
                      else (op.role, op.projects))
    if ALL in projects:
        deny = [] if role == ADMIN else [busnames.everything(busnames.ADMIN)]
        return {"allow": [busnames.everything(), busnames.INBOX], "deny": deny}
    return {"allow": [busnames.everything(p) for p in sorted(projects)]
            + [busnames.INBOX], "deny": []}


def pass_file(name):
    """Имя файла пароля оператора в secrets/ — как у всех остальных."""
    return busnames.operator_pass_file(name)
