"""mop driver run <name>: raise the body and run the puppet inside it

What the job spec calls (the outer wrapper). Runs on the node that owns the
puppet: ensure the body, bootstrap the sandbox through the server,
push the inner wrapper in and hold its session until it ends.
"""
import asyncio
import base64
import json
import os
import shlex
import signal
import subprocess
import sys
import time

from mop.cli import lib
from mop.common import bus, busnames, config
from mop.common.domain import Body
from mop import driver

# Куда внешний врапер кладёт внутренний внутри тела. В $HOME, а не в /tmp:
# /tmp в теле бывает общим или вычищаемым, а этот файл обязан прожить ровно
# столько, сколько живёт папет.
INNER = ".cache/mop-wrapper.sh"

# Проба ssh перед врапером (#73): около минуты на то, чтобы сеть свежего тела
# догнала, дольше -- уже не гонка, а поломка, и отказ должен быть громким.
SSH_PROBE_TRIES = 12
SSH_PROBE_PAUSE = 5
SSH_PROBE_TIMEOUT = 20

# Что внешний врапер переливает внутрь тела. Список закрыт: открытый означал бы
# дыру, через которую в тело уехало бы окружение узла целиком — вместе с тем,
# чего папету видеть не положено.
#
# Закрыт он самой спекой (#155): job_spec кладёт в PU_CARRY ключи своего же
# окружения. Раньше список здесь перепечатывался руками и отставал от спеки —
# новая переменная доезжала до задачи и молча не доезжала до тела. Импортировать
# спеку отсюда нельзя: mop.spec тянет python-nomad, а на узле его нет.
#
# LEGACY_CARRY — список до #155, для спек, зарегистрированных раньше: их
# папет обязан подняться после раскатки и до перерегистрации.
LEGACY_CARRY = ("PU_NAME", "PU_ORIGIN", "PU_PROJECT", "PU_SEED", "PU_CONTINUE",
                "PU_LLM", "PU_LLM_ENV", "PU_LLM_KEY_VAR", "PU_LLM_AUTH_VAR",
                "HOME", "PATH")


def carry(env):
    """Ключи окружения задачи, которые едут в тело: список самой спеки."""
    keys = env.get("PU_CARRY") or ""
    return keys.split(",") if keys else list(LEGACY_CARRY)


def _inner_script():
    """Внутренний врапер с прелюдией из окружения задачи.

    Прелюдия обязательна: переменные спеки живут в процессе задачи на узле, а
    врапер исполняется в теле — без переливки он не узнал бы ни имени папета,
    ни origin, ни имени ключа LLM-профиля."""
    blob = os.environ.get("PU_WRAPPER") or ""
    if not blob:
        sys.exit("no PU_WRAPPER in the task environment — this job spec predates "
                 "the wrapper split; re-register the puppet: mop recycle <name>")
    return _prelude(os.environ) + base64.b64decode(blob).decode()


def _prelude(environ):
    """Экспорт окружения задачи для скрипта в теле: переменные спеки живут
    в процессе задачи на узле, а скрипт исполняется в теле."""
    return "".join(f"export {k}={shlex.quote(environ.get(k, ''))}\n"
                   for k in carry(environ))


def clone_script(environ):
    """Стадия клона (#247): что узел исполняет в теле ДО bootstrap. Чистая
    функция. Прелюдия из окружения задачи, отказ на первой ошибке, охрана
    пустого PU_CLONE (rm -rf в сниппете над пустой строкой снёс бы тело),
    затем driver.CLONE_SH -- тот же текст, что стоит во врапере спеки."""
    head = (_prelude(environ) + "set -e\n"
            ': "${PU_CLONE:?no PU_CLONE in the task environment}"\n'
            'd="$PU_CLONE"\n')
    # Ветка мастера (#256) -- из меты джоба: Nomad отдаёт Meta задаче как
    # NOMAD_META_<ключ>. Checkout только у свежего клона (не было .git или
    # origin сменился и сниппет снесёт каталог): существующий клон не
    # переключается, там может быть работа. Нет такой ветки в origin --
    # завести локально: мастер создаст её на origin первым landing.
    #
    # Дом клона (#272) -- на каждом старте, не только у свежего клона:
    # существующий клон узнаёт его тоже. Ветка мастера, а без неё ветка по
    # умолчанию из origin/HEAD; не вычислить -- записи нет, и дом тогда
    # решает агент по той же ветке по умолчанию. Живёт здесь, а не в
    # driver.CLONE_SH: тот едет во врапере, и правка его -- перерегистрация
    # всего пула.
    branch = (environ.get("NOMAD_META_branch") or "").strip()
    if not branch:
        return (head + driver.CLONE_SH
                + 'home=$(git -C "$d" rev-parse --abbrev-ref origin/HEAD 2>/dev/null || true)\n'
                'if [ -n "$home" ]; then git -C "$d" config mop.home "${home#origin/}"\n'
                'else git -C "$d" config --unset mop.home || true; fi\n')
    return (head + f"export PU_BRANCH={shlex.quote(branch)}\n"
            'fresh=1\n'
            'if [ -d "$d/.git" ] && [ "$(git -C "$d" remote get-url origin)" = "$PU_ORIGIN" ]; then fresh=0; fi\n'
            + driver.CLONE_SH +
            'if [ "$fresh" = 1 ]; then\n'
            '    git -C "$d" checkout -q "$PU_BRANCH" 2>/dev/null || git -C "$d" checkout -q -b "$PU_BRANCH"\n'
            'fi\n'
            'git -C "$d" config mop.home "$PU_BRANCH"\n')


# ─── узел ────────────────────────────────────────────────────────────────
def bootstrap_sandbox(d, name, project):
    """Bootstrap песочницы папета с этого узла: впустить сервер, позвать,
    дождаться, выпустить. -> ответ сервера; отказ — RuntimeError/BusError.

    Зовётся из `mop driver run` до внутреннего врапера. Дверь закрывается в
    любом исходе: ключ сервера в теле живёт ровно столько, сколько идёт
    bootstrap. Где сервер найдёт тело и надо ли его впускать, решает драйвер
    (#151): у host тело — сам узел, и дорога туда есть всегда."""
    address = d.address(name)
    r = asyncio.run(d.admit(name, True))
    if r.get("error"):
        raise RuntimeError(f"cannot let the server into the body: {r['error']}")
    try:
        bus.connect(bus.NODE_FILE)
        out = bus.ask_server("bootstrap", timeout=busnames.BOOTSTRAP_TIMEOUT, project=project,
                             name=name, address=address)
    finally:
        asyncio.run(d.admit(name, False))
    if out.get("error"):
        raise RuntimeError(out["error"])
    if not out.get("ok"):
        raise RuntimeError(f"bootstrap failed (ansible exit {out.get('rc')}):\n"
                           f"{out.get('tail', '')}")
    return out


def ensure_params(name, environ):
    """Что ensure узнаёт о папете из окружения задачи. Чистая функция.

    mem -- потолок памяти из спеки (PU_MEM_MB, #197); спека до #197 его не
    несёт, и тогда память тела не трогается."""
    params = {"project": driver.project_of_name(name)}
    if environ.get("PU_MEM_MB"):
        params["mem"] = environ["PU_MEM_MB"]
    return params


def main(argv):
    """Поднять тело и отработать в нём внутренний врапер. Зовётся из спеки.

    Смерть врапера обязана убивать сессию. На «живой папет = живая tmux-сессия
    с именем джоба» стоит и ростер, и сбор сирот: умри внешний врапер молча,
    Nomad считал бы папета остановленным, а в теле остался бы живой claude, с
    которым уже никто не разговаривает. Гасим внутри тела: pid сессии живёт в
    его namespace, и снаружи такого pid либо нет, либо это чужой процесс."""
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    if not driver.valid_name(name):
        sys.exit(f"{name!r} doesn't look like a puppet name")
    d = driver.current()

    r = asyncio.run(d.ensure(name, ensure_params(name, os.environ)))
    body = Body.from_dict(r)
    if body is None:
        sys.exit(f"no body for {name}: {r.get('error')}")
    print(f"{name}: {body.describe()}", flush=True)

    # Врапер идёт своим соединением (run_argv), а гашение сессии — обычным
    # (argv): первое живёт столько же, сколько папет, и мультиплексировать его
    # нельзя, второе коротко и платить за него рукопожатием незачем.
    hold, probe = d.run_argv(name), d.argv(name)

    def inside(prefix, script):
        return prefix + [script] if prefix else ["bash", "-c", script]

    # Тело должно пустить врапер тем же соединением, которым он пойдёт (#73):
    # сеть свежего клона догоняет не сразу, и первый ssh уходил в Connection
    # timed out при исправном теле. Промах здесь -- пауза, а не падение
    # задачи и круг рестарта Nomad. У host префикса нет, и пробовать нечего.
    # Проба стоит до клона (#247): клон идёт в тело тем же ssh.
    if hold:
        def attempt():
            try:
                r = subprocess.run(hold + ["true"], capture_output=True,
                                   text=True, timeout=SSH_PROBE_TIMEOUT)
            except subprocess.TimeoutExpired:
                return False, f"no answer in {SSH_PROBE_TIMEOUT}s"
            return r.returncode == 0, (r.stderr.strip() or f"exit {r.returncode}")
        ok, why, n = driver.until_ok(attempt, SSH_PROBE_TRIES, SSH_PROBE_PAUSE,
                                     time.sleep)
        if not ok:
            sys.exit(f"the body of {name} did not let ssh in after {n} tries: {why}")
        if n > 1:
            print(f"{name}: ssh into the body passed on try {n}", flush=True)

    # Клон -- до bootstrap (#247): сервер играет манифест проекта в
    # песочницу, где клон уже стоит, и mop_clone означает то, что написано в
    # docs/BOOTSTRAP.md. Раньше клонировал внутренний врапер после ответа
    # сервера, и на первом старте контейнерного тела задача проекта с chdir
    # на клон падала «No such file or directory». Тем же путём в тело, что и
    # гашение сессии (probe): у host это сам узел, у pve -- ssh под ключом
    # узла. Отказ -- отказ, а не полуподнятый папет.
    r = subprocess.run(inside(probe, clone_script(os.environ)),
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"the clone of {name} did not come up: "
                 f"{driver.why(r.stderr or r.stdout, r.returncode)}")
    print(f"{name}: clone in {driver.clone_dir(name)}", flush=True)

    # Bootstrap песочницы (#62): сервер играет .mop/bootstrap.yaml проекта, а
    # узел ждёт ответа до того, как откроет tmux. Отказ — это отказ: выход
    # ненулём, Nomad перезапускает, ростер показывает падение. Папет не
    # поднимается «наполовину» с окружением, которого нет.
    try:
        b = bootstrap_sandbox(d, name, driver.project_of_name(name))
    except (RuntimeError, bus.BusError) as e:
        sys.exit(f"bootstrap of {name} failed: {e}")
    print(f"{name}: bootstrap "
          + (f"played in {b.get('seconds')}s" if b.get("played")
             else "none for the project"), flush=True)

    # Кред папета приезжает тем же ответом (#83), и только им (#114): узел
    # его не хранит, файл появляется в теле на подъёме папета и для его
    # проекта. Ответ без кредов сервер отдаёт отказом, так что здесь его нет
    # -- но старый сервер ещё может так ответить, и тогда падаем громко:
    # папет без шины мастеру читается живым, но молчащим.
    if not b.get("bus"):
        sys.exit(f"bootstrap of {name} brought no bus credentials -- "
                 f"is the project registered? mop project add <origin>")
    project = driver.project_of_name(name)
    path = driver.project_creds(project)
    w = asyncio.run(d.push(name, path, json.dumps(b["bus"]).encode() + b"\n"))
    if w.get("error"):
        sys.exit(f"bus credentials did not reach the body of {name}: {w['error']}")
    print(f"{name}: bus credentials in {path}", flush=True)

    inner = os.path.join(config.get("MOP_HOME"), INNER)
    w = asyncio.run(d.push(name, inner, _inner_script().encode()))
    if w.get("error"):
        sys.exit(f"the wrapper did not reach the body of {name}: {w['error']}")

    state = {"child": None, "asked": False}

    def stop(_sig, _frm):
        state["asked"] = True
        subprocess.run(inside(probe, f"tmux -L {name} kill-session -t {name}"),
                       capture_output=True)
        if state["child"]:
            state["child"].terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state["child"] = subprocess.Popen(inside(hold, f"bash {shlex.quote(inner)}"))
    code = state["child"].wait()
    # Остановка по TERM — штатное завершение: так Nomad снимает задачу, и
    # ненулевой код здесь читался бы как падение папета.
    return 0 if state["asked"] else code
