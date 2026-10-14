"""create a puppet: mop add [git-origin] [--role <role>]

Without origin, the origin of the current working copy is used. The name is
picked automatically: <project>-<number>; placement over the pool and the
8 GB reservation are not chosen here. Silent when the puppet is running; on
a terminal it shows the current step. The new puppet is in mop list.
Use mop models to see model IDs offered by the selected proxy.

--role names the new puppet's role instead of a legacy puppet: the
creation carries a request_id, the answer is checked against the server's
echo of the role triple (a server without the role protocol refuses before
anything is created). A role principal (a grant master) must name the role
and sends no bootstrap policy of its own: the child inherits the parent's
approved snapshot.
"""

import uuid

from mop.cli import lib
from mop.cli.core import _common
from mop.client import roles
from mop.common import bus, context, ops, puppets


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "node_verbs": [], "cluster_verbs": ['add', 'alloc', 'bootstrap_result', 'ping'], "args": [
    {"name": "origin", "type": "string", "help": "git origin; without it, the origin of the master's working copy"},
    {"name": "role", "type": "string", "flag": "--role",
     "help": "role of the new puppet (soul name); required for a role principal"},
    ]}


def main(argv):
    role, argv = lib.parse_value(argv, "--role", once=True)
    if len(argv) > 1:
        lib.usage(__doc__)
    grant = bus.principal()
    if grant and not role:
        lib.usage("a role principal names the role of the puppet it adds: "
                  "mop add [git-origin] --role <role>")
    origin = lib.origin(argv[0] if argv else None, __doc__)
    project = puppets.project_of(origin)
    # Курица и яйцо: у нового проекта ещё нет пользователя в конфиге NATS, и
    # папет поднимется, но к шине не подключится — прочитается как «агент
    # молчит» на пустом месте. Лучше отказать здесь, чем разбираться там.
    if not lib.project_ready(project):
        lib.usage(f"project {project} isn't on the bus yet.\n"
                  f"Register it on the server: mop project add {origin}")
    p = lib.Progress(project)
    try:
        return _add(origin, project, bool(argv), p, role)
    finally:
        p.clear()


def _add(origin, project, named, p, role=None):
    """Долгая команда (#124): на терминале -- текущий шаг, при успехе --
    ничего; отказ и не вставший папет -- ошибкой. Workflow -- общая
    операция (#459): командлет строит вход и рисует итог.

    Роль (#618): ping-предзапрос сводит повторы и отказывает старому
    серверу ДО создания (roles.protocol); грант-мастер политику не читает
    и не везет -- ребёнок наследует одобренный снапшот родителя (#582),
    человек со своей политикой заводит как сегодня."""
    if role:
        try:
            reconciles = roles.protocol(bus.call_cluster("ping"))
        except ValueError as e:
            raise RuntimeError(f"mop add --role {role}: {e}") from None
        request_id = uuid.uuid4().hex
        return _role_add(origin, role, request_id, reconciles, p)
    p.step("registering")
    text, sent = _common.workspace_text(origin)
    print(_common.sent_line(sent), flush=True)
    v = ops.add(bus.call_cluster, origin, (text, sent),
                branch=context.current().branch,
                step=lambda s: p.step(s), place_wait=120)
    return _common.render(v, p)


def _role_add(origin, role, request_id, reconciles, p):
    """Роль-создание (#618): человек везет свою политику, грант -- ничего
    (наследование снапшота родителя #582); эхо тройки -- обязательная
    часть успеха: безролевой ответ -- не запуск роли, второго не создаём.
    Эхо сверяется ВНУТРИ транспорта, сразу после ответа регистрации --
    до аллокаций и ожиданий (#618-ревью): отказ сервера (spawn, чужой
    request_id) -- его причина, эхо ее не подменяет; безречный ответ --
    провал с именем созданного. Попытка одна, без слепых повторов
    (#586): request_id в отказе неизвестности -- ручной повтор тем же
    идентификатором сведет джобы у сервера со сводом, слепой второй --
    развел бы двух."""
    if bus.principal() is None:
        text, sent = _common.workspace_text(origin)
        print(_common.sent_line(sent), flush=True)
        workspace = (text, sent)
    else:
        workspace = None
    audit = {}

    def transport(verb, **kw):
        got = bus.call_cluster(verb, **kw)
        if verb == "add" and got.get("ok"):
            try:
                triple = roles.echo(got, role)
            except ValueError as e:
                who = got.get("name") or "the new puppet"
                raise RuntimeError(f"{who}: {e}; the job is created, but "
                                   f"this is not a role start — check it: "
                                   f"mop list, mop attach {who}") from None
            audit["name"] = got["name"]
            print(f"{got['name']}: role {role} "
                  f"({triple['soul_digest'][:12]})", flush=True)
        return got

    try:
        v = ops.add(transport, origin, workspace, soul=role,
                    request_id=request_id, branch=context.current().branch,
                    step=lambda s: p.step(s), place_wait=120,
                    bootstrap_wait=_common.BOOTSTRAP_WAIT)
    except bus.BusError as e:
        # Два разных «нет ответа» (#618-ревью). Регистрация молчит --
        # исход неизвестен: повтор тем же request_id сведет джобы у
        # сервера, слепой второй развел бы двух. Регистрация ОТВЕТИЛА
        # (имя уже в аудите операции #575), молчит чтение после неё --
        # папет создан, и отказ зовет его по имени с первопричиной,
        # а не «эха нет».
        name = audit.get("name")
        if name:
            raise RuntimeError(f"{name} is created (role {role}, request "
                               f"{request_id}), but reading its start "
                               f"failed: {e}; check mop list, "
                               f"mop attach {name}") from None
        raise RuntimeError(f"add {role}: registration answer unknown "
                           f"({e}); do not repeat blindly — retry with the "
                           f"same request_id {request_id} to reconcile, "
                           f"and check mop list first") from None
    return _common.render(v, p)


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
