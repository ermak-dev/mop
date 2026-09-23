"""mop driver run <name>: raise the body and run the puppet inside it

What the job spec calls (the outer wrapper). Runs on the node that owns the
puppet: ensure the body, bootstrap the sandbox through the server (#62),
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
from mop import bootstrap, bus, config, driver

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
CARRY = ("PU_NAME", "PU_ORIGIN", "PU_PROJECT", "PU_PROJECT", "PU_SEED",
         "PU_CONTINUE", "PU_LLM", "PU_LLM_ENV", "PU_LLM_KEY_VAR",
         "PU_LLM_AUTH_VAR", "HOME", "PATH")


def _inner_script():
    """Внутренний врапер с прелюдией из окружения задачи.

    Прелюдия обязательна: переменные спеки живут в процессе задачи на узле, а
    врапер исполняется в теле — без переливки он не узнал бы ни имени папета,
    ни origin, ни имени ключа LLM-профиля."""
    blob = os.environ.get("PU_WRAPPER") or ""
    if not blob:
        sys.exit("no PU_WRAPPER in the task environment — this job spec predates "
                 "the wrapper split; re-register the puppet: mop recycle <name>")
    prelude = "".join(f"export {k}={shlex.quote(os.environ.get(k, ''))}\n"
                      for k in CARRY)
    return prelude + base64.b64decode(blob).decode()


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

    r = asyncio.run(d.ensure(name, {"project": driver.project_of_name(name)}))
    if r.get("error"):
        sys.exit(f"no body for {name}: {r['error']}")
    print(f"{name}: body {r.get('body') or 'the node itself'}"
          + (f" at {r['address']}" if r.get("address") else ""), flush=True)

    # Bootstrap песочницы (#62): сервер играет .mop/bootstrap.yaml проекта, а
    # узел ждёт ответа до того, как откроет tmux. Отказ — это отказ: выход
    # ненулём, Nomad перезапускает, ростер показывает падение. Папет не
    # поднимается «наполовину» с окружением, которого нет.
    try:
        b = bootstrap.run(d, name, driver.project_of_name(name))
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
    path = f"{driver.HOME}/.config/mop/bus-{project}.json"
    w = asyncio.run(d.push(name, path, json.dumps(b["bus"]).encode() + b"\n"))
    if w.get("error"):
        sys.exit(f"bus credentials did not reach the body of {name}: {w['error']}")
    print(f"{name}: bus credentials in {path}", flush=True)

    inner = os.path.join(config.get("MOP_HOME"), INNER)
    w = asyncio.run(d.push(name, inner, _inner_script().encode()))
    if w.get("error"):
        sys.exit(f"the wrapper did not reach the body of {name}: {w['error']}")

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
