"""Операторы установки: люди, у которых есть доступ к пулу. Данные, без печати.

Оператор представлялся шине ролью — `master-<проект>`, с паролем, общим на
всех операторов проекта, и привозил его копированием каталога по ssh. Отсюда
два следствия: отозвать доступ одному человеку было нельзя, не сменив пароль
остальным, и сама установка с раздельными сервером и контроллером по
документации не заводилась (#88).

Теперь оператор — человек из провайдера личностей (#205, #219): файла
операторов (`mop user`) или каталога LDAP. Здесь -- его роль и проекты и
права на шине, которые по ним выдаёт auth callout (#206).

Роль и проекты записываются (в строке файла операторов, #205) как
`имя:роль[:проекты]`; разбор -- parse, та же строка на человека через точку
с запятой, запятая между проектами:

    anton:admin                 весь пул плюс машинные глаголы (mop.admin.*)
    ivan:user:rugent,cloudpub   только названные проекты
    olga:user:*                 все проекты, но не машины

Запись без роли (`имя:*`, `имя:проект,проект`) читается так, как работала до
#106: `*` давал весь mop.>, то есть admin.
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
            raise ValueError(f"operators: {name} has no projects; "
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
        raise ValueError(f"operators: {name} is admin, which is the whole "
                         f"pool; drop the projects, or make {name} a user")
    if role == USER and len(fields) == 2:
        return {"role": USER, "projects": projects_of(fields[1])}
    raise ValueError(f"operators: {name}: expected {name}:admin or "
                     f"{name}:user:<projects>, got {':'.join(fields)!r}")


def parse(setting):
    """`имя:роль[:проекты]; ...` -> {имя: {role, projects}}. Пустая строка -> {}.

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
            raise ValueError(f"operators: no name in {chunk!r}")
        if not fields or not fields[0]:
            raise ValueError(f"operators: {name} has no role; "
                             f"write {name}:user:<projects> or {name}:admin")
        if name in RESERVED or name.startswith(RESERVED_PREFIXES):
            raise ValueError(f"operators: {name!r} is a role on the bus, "
                             f"not a person — pick another name")
        out[name] = entry(name, fields)
    return out


def permissions(op, login=None):
    """Права пользователя субъектами. -> {allow, deny: подписка;
    publish, publish_deny: публикация} -- списки масок.

    `_INBOX.>` обязателен всем, иначе request-reply молча не работает -- на
    этом однажды стоял целый вечер разбора (docs/BUS.md). user на весь пул
    получает mop.> без mop.admin.>: иначе вместе с проектами ему достались
    бы узлы -- disk, drain, forget.

    Публикация -- явным списком, а не mop.<p>.> (#207): в rpc агента и
    сервиса кластера логин вызывающего -- токен субъекта, и публиковать с
    чужим логином человеку нельзя. Остальное, что люди публикуют, -- как
    было: публичный канал, all, инбоксы мастеров и опрос who, события,
    сервер, сборщик (admin). Прежние rpc без логина -- на время перехода,
    уходят с уборкой. Логин в субъекте -- токеном busnames.login_token
    (anton.ermak -> anton%2Eermak).

    Подписка -- тоже явным списком (#212): подписанный на rpc агента или
    сервиса кластера человек отвечал бы первым вместо них, и логин в
    субъекте ничего бы не стоил. Клиент подписывается ровно на инбоксы
    мастеров, события проекта (дашборд) и _INBOX (ответы, поток сборщика).
    Инбоксы -- только свои (#213): адрес мастера несёт логин владельца
    первым токеном, и человек слушает mop.<p>.master.<свой токен>.> плюс
    опрос who (master.all.inbox), а не master.> -- там лежат чужие отчёты.

    op -- identity.Identity (#205) или словарь разбора {role, projects};
    login -- логин, если op его не несёт (словарь)."""
    role, projects = ((op["role"], op["projects"]) if isinstance(op, dict)
                      else (op.role, op.projects))
    login = login or getattr(op, "login", None)
    if not busnames.valid_login(login):
        # Точку, пробел и маски кодирует busnames.login_token; пустой логин и
        # управляющие символы токеном не станут.
        raise ValueError(f"operators: login {login!r} is empty or has "
                         f"control characters")
    if ALL in projects:
        deny = [] if role == ADMIN else [busnames.everything(busnames.ADMIN)]
        scope = [busnames.ANY]
    else:
        deny, scope = [], sorted(projects)
    allow = [s for p in scope for s in (busnames.own_masters(p, login),
                                        busnames.inbox(p, busnames.ALL_MASTERS),
                                        busnames.events(p))]
    allow.append(busnames.INBOX)
    publish = []
    for p in scope:
        publish += [busnames.node(p, "*", "rpc", login=login),
                    busnames.cluster(p, login=login),
                    # Переход (#207): прежние субъекты без логина.
                    busnames.node(p, "*", "rpc"), busnames.cluster(p),
                    busnames.node(p, "*", "msg"), busnames.broadcast(p),
                    busnames.masters(p), busnames.events(p), busnames.server(p)]
    if role == ADMIN:
        publish.append(busnames.build())
    publish.append(busnames.INBOX)
    return {"allow": allow, "deny": deny, "publish": publish, "publish_deny": deny}


def pass_file(name):
    """Имя файла пароля оператора в secrets/ — как у всех остальных."""
    return busnames.operator_pass_file(name)
