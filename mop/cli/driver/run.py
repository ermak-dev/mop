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

from mop.cli import lib
from mop import bootstrap, bus, config, driver

# Куда внешний врапер кладёт внутренний внутри тела. В $HOME, а не в /tmp:
# /tmp в теле бывает общим или вычищаемым, а этот файл обязан прожить ровно
# столько, сколько живёт папет.
INNER = ".cache/mop-wrapper.sh"

# Что внешний врапер переливает внутрь тела. Список закрыт: открытый означал бы
# дыру, через которую в тело уехало бы окружение узла целиком — вместе с тем,
# чего папету видеть не положено.
CARRY = ("PU_NAME", "PU_ORIGIN", "PU_PROJECT", "PU_SHARD", "PU_SEED",
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

    r = asyncio.run(d.ensure(name, {"shard": driver.shard_of_name(name)}))
    if r.get("error"):
        sys.exit(f"no body for {name}: {r['error']}")
    print(f"{name}: body {r.get('body') or 'the node itself'}"
          + (f" at {r['address']}" if r.get("address") else ""), flush=True)

    # Bootstrap песочницы (#62): сервер играет .mop/bootstrap.yaml шарда, а
    # узел ждёт ответа до того, как откроет tmux. Отказ — это отказ: выход
    # ненулём, Nomad перезапускает, ростер показывает падение. Папет не
    # поднимается «наполовину» с окружением, которого нет.
    try:
        b = bootstrap.run(d, name, driver.shard_of_name(name))
    except (RuntimeError, bus.BusError) as e:
        sys.exit(f"bootstrap of {name} failed: {e}")
    print(f"{name}: bootstrap "
          + (f"played in {b.get('seconds')}s" if b.get("played")
             else "none for the shard"), flush=True)

    # Кред папета приезжает тем же ответом (#83): узел перестаёт хранить его
    # в покое — файл появляется в теле ровно на время жизни папета и ровно
    # для его проекта. Пока прогон кладёт bus-<проект>.json на узлы сам,
    # ответ без кредов — не отказ: врапер возьмёт файл оттуда, и это переход.
    if b.get("bus"):
        project = driver.shard_of_name(name)
        path = f"{driver.HOME}/.config/mop/bus-{project}.json"
        w = asyncio.run(d.push(name, path,
                               json.dumps(b["bus"]).encode() + b"\n"))
        if w.get("error"):
            # Не валимся: файл мог уже лежать от прогона. Не молчим: если не
            # лежал, врапер сейчас откажет, и причина должна быть в логе.
            print(f"{name}: bus credentials did not reach the body: "
                  f"{w['error']}", flush=True)
        else:
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
