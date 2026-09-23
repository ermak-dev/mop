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

Формат настройки — `имя:проект,проект; имя:*`. Точка с запятой между людьми,
запятая между проектами, `*` — весь пул (права `admin`, но своим именем).
"""
# Имена, которыми на шине зовутся роли. Человек с таким именем — не второй
# оператор, а тихая подмена роли: права роли и права человека сложились бы в
# одного пользователя, и понять по конфигу, чьи они, стало бы нельзя.
RESERVED = ("admin",)
RESERVED_PREFIXES = ("master-", "puppet-", "node-")
ALL = "*"


def parse(setting):
    """`MOP_OPERATORS` -> {имя: [проекты]}. Пустая настройка -> {}.

    Отказы громкие и на разбор, а не на прогон: список едет в конфиг NATS, и
    ошибка в нём — это либо доступ, которого никто не давал, либо мастер,
    который не подключится."""
    out = {}
    for chunk in (setting or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, rights = chunk.partition(":")
        name = name.strip()
        projects = [p.strip() for p in rights.split(",") if p.strip()]
        if not name:
            raise ValueError(f"MOP_OPERATORS: no name in {chunk!r}")
        if not projects:
            # Пустые права — не «на всё»: прав по умолчанию не бывает, а
            # опечатка `anton:` иначе выдала бы весь пул.
            raise ValueError(f"MOP_OPERATORS: {name} has no projects; "
                             f"write {name}:* for the whole pool")
        if name in RESERVED or name.startswith(RESERVED_PREFIXES):
            raise ValueError(f"MOP_OPERATORS: {name!r} is a role on the bus, "
                             f"not a person — pick another name")
        out[name] = projects
    return out


def subjects(projects):
    """Права пользователя субъектами. -> [маски].

    `_INBOX.>` обязателен обеим сторонам, иначе request-reply молча не
    работает — на этом однажды стоял целый вечер разбора (docs/BUS.md)."""
    if ALL in projects:
        return ["mop.>", "_INBOX.>"]
    return [f"mop.{p}.>" for p in sorted(projects)] + ["_INBOX.>"]


def pass_file(name):
    """Имя файла пароля оператора в secrets/ — как у всех остальных."""
    return f"nats-op-{name}.pass"
