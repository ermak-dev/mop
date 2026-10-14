"""Общий лаунчер ролей (#586): `mop <роль> [origin]`.

Не команда сам по себе (имя с «_»): сюда ведёт диспетчер незнакомое ему
имя, годное именем роли, и `mop master` -- явный алиас мастер-роли. Запуск
по умолчанию -- новый папет пула с ролью (docs/SOUL.md, «CLI, имена,
упаковка»): регистрация с ролью, подъём, bootstrap, tmux-сессия в теле,
затем терминал по ssh. Подключение к живому -- только `mop attach <имя>`;
лаунчер к существующему не подключается и по одной роли сессию не
выбирает.
"""
import os
import subprocess
import time
import uuid

from mop import cli
from mop.cli import lib
from mop.cli.core import _common
from mop.cli.pool import login
from mop.client import roles
from mop.common import bus, config, context, ops, puppets, soul, state

# Сколько раз повторить регистрацию, ответ которой не пришёл (#586): тем же
# request_id и только у сервера, сказавшего в рукопожатии, что сводит
# повторы к одному джобу и гранту (#582). Иначе -- одна попытка и честная
# неизвестность.
ADD_ATTEMPTS = 3
# Сколько ждать tmux-сессии в теле после running-аллокации и bootstrap'а:
# тот же запас, что ростер считает «поднимается» (state.STARTING_AFTER).
READY_WAIT = state.STARTING_AFTER


def main(argv):
    """argv диспетчера: роль первым словом, дальше аргументы лаунчера."""
    return launch(argv[0], argv[1:])


def launch(role, argv, hint=None):
    """Поднять нового папета с ролью и подключить терминал -> код выхода.

    Всё, что может отказать без пула (каталог, имя, аргументы, терминал,
    вход, бандл роли), отказывает до регистрации: аллокации нет. Отказ после
    регистрации называет созданного папета и путь назад (`mop attach`), и
    второго не создаёт."""
    try:
        found = roles.catalogue(cli.command_names())
    except ValueError as e:
        raise RuntimeError(f"mop: {e}") from None
    if role not in found:
        cli.unknown(role)
        return 1
    try:
        origin = roles.options(argv)
    except ValueError as e:
        # Ошибка вызова -- usage, как у прочих команд: отказ с подсказкой.
        lib.usage(f"mop {role}: {e}" + (f"\n{hint}" if hint else ""))
    try:
        # Бандл -- до регистрации: сломанная роль не стоит аллокации.
        # Сверку с закреплённым digest делают сервер (#582) и обёртка (#583).
        soul.load(found[role])
    except ValueError as e:
        raise RuntimeError(f"mop {role}: {e}") from None
    if not (isatty(0) and isatty(1)):
        raise RuntimeError(f"mop {role}: no terminal: a role launch attaches one "
                           f"to the new puppet; run it from a terminal")
    login.ensure()
    return _launch(role, origin)


def isatty(fd):
    return os.isatty(fd)


def _launch(role, origin):
    # Настройки кластера -- до первого сетевого вызова, как lib.cluster.
    config.require("MOP_SERVER_LAN")
    origin = lib.origin(origin, __doc__)
    project = puppets.project_of(origin)
    if not lib.project_ready(project):
        raise RuntimeError(f"project {project} isn't on the bus yet.\n"
                           f"Register it on the server: mop project add {origin}")
    try:
        reconciles = roles.protocol(bus.call_cluster("ping"))
    except ValueError as e:
        raise RuntimeError(f"mop {role}: {e}") from None
    p = lib.Progress(project)
    try:
        created = _create(role, origin, reconciles, p)
        if created is None:
            return 1
        name, triple = created
        node = _wait_ready(name, triple, p)
    finally:
        p.clear()
    print(f"{name}: role {role} on {node}", flush=True)
    rc = run_ssh(_common.attach_argv(name))
    if rc:
        raise RuntimeError(f"{name}: ssh to {node} exited {rc}; the puppet keeps "
                           f"running: mop attach {name}")
    return 0


def run_ssh(argv):
    """ssh процессом, а не exec: провал входа обязан назвать созданного
    папета, а exec унёс бы это знание вместе с процессом."""
    return subprocess.call(argv)


def _create(role, origin, reconciles, p):
    """Регистрация с ролью общей операцией ops.add -> (имя, тройка роли) |
    None (отказ уже напечатан). request_id -- один на попытку создания.

    Поля роли идут общим ops.add(soul=, request_id=) (#603): провод
    регистрации собирает общий слой, клиент не держит копии. Повтор --
    только у сервера, сказавшего в рукопожатии, что сводит повторы к
    одному джобу по request_id (#582), тем же идентификатором."""
    request_id = uuid.uuid4().hex
    attempts = ADD_ATTEMPTS if reconciles else 1
    seen = {}

    def transport(verb, **kw):
        if verb != "add":
            return bus.call_cluster(verb, **kw)
        for attempt in range(1, attempts + 1):
            try:
                got = bus.call_cluster(verb, **kw)
                break
            except bus.BusError as e:
                if attempt == attempts:
                    raise RuntimeError(
                        f"mop {role}: registration outcome unknown after "
                        f"{attempts} attempt{'s' if attempts > 1 else ''} "
                        f"(request {request_id}): {e}; mop list shows whether "
                        f"the puppet was created") from None
        seen.update(got)
        # Безролевой папет -- не запуск роли: дальше не идём, второго не
        # создаём и созданного не удаляем -- его судьбу решает человек.
        try:
            seen["triple"] = roles.echo(got, role)
        except ValueError as e:
            raise RuntimeError(f"{got.get('name') or 'the new puppet'}: {e}; "
                               f"nothing is created again") from None
        return got

    p.step("registering")
    text, sent = _common.workspace_text(origin)
    print(_common.sent_line(sent), flush=True)
    v = ops.add(transport, origin, (text, sent), soul=role,
                request_id=request_id, branch=context.current().branch,
                step=p.step, sleep=time.sleep, clock=time.time)
    name = v.get("name") or seen.get("name")
    kind = v.get("outcome")
    p.clear()
    # Итог bootstrap'а не увиден -- неизвестность, а не провал (#334):
    # строкой, и решает готовность сессии.
    unseen = kind == ops.PARTIAL and v.get("bootstrap") is None
    if kind in (ops.REGISTERED, ops.ACCEPTED) or unseen:
        if v.get("bootstrap") is not None or unseen:
            line = _common.outcome_line(v.get("bootstrap"),
                                        v.get("waited", _common.BOOTSTRAP_WAIT))
            if line:
                print(line, flush=True)
        return name, seen["triple"]
    error = v.get("error") or f"outcome {kind}"
    if name and name not in error:
        error = f"{name}: {error}"
    if name:
        error += f"; nothing is created again: mop attach {name} once it runs"
    lib.fail(error)
    return None


def _wait_ready(name, triple, p):
    """Ждать сессию роли в теле -> узел. Running-аллокация и итог
    bootstrap'а -- ещё не сессия, а голый tmux -- ещё не роль: ждём метку
    сверенного бандла (roles.session_ready)."""
    node = puppets.node_of(name)
    p.step(f"{name}: waiting for the session on {node}")
    deadline = clock() + READY_WAIT
    last = None
    while True:
        try:
            answers = bus.request_many("states", {node: {"names": [name]}})
            if roles.session_ready(answers, node, name, triple):
                return node
        except (bus.BusError, RuntimeError) as e:
            # Молчащий агент -- не приговор папету (AGENT SILENT): ждём до
            # срока и называем последнюю причину.
            last = str(e)
        if clock() >= deadline:
            raise RuntimeError(
                f"{name}: no session of the role on {node} after {READY_WAIT}s"
                + (f" ({last})" if last else "")
                + f"; nothing is created again: mop list, mop attach {name}")
        sleep(2)


def sleep(s):
    time.sleep(s)


def clock():
    return time.time()
