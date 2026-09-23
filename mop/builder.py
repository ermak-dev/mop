"""Сборщик образов: `mop driver build` за шиной (#123).

Сборка образа -- ansible по гипервизорам (deploy/pve-build.yml) и вызовы
Nomad (снять тела проекта, поднять заново, объявить образ узлам). Инвентарь,
ssh к гипервизорам и токен Nomad есть только на сервере, поэтому сборка
живёт здесь, а оператор просит её глаголом `build` в `mop.admin.build.rpc`
с любой машины.

Подписчик -- юнит mop-builder, под пользователем контроллера: плейбук идёт
его инвентарём и его ключами. На шину он ходит как `service`, кредами из
каталога сервера пользователя пула (MOP_SERVER_DIR), а не как человек.

Ход сборки -- потоком в инбокс просителя (bus.ask_stream): шаг (задача
ansible или этап над телами) и сердцебиение; итог -- последним. Сборки идут
по одной: шаблоны на гипервизоре делят зеркало (#100).

Данные и события, без печати: печатает проситель.
"""
import asyncio
import collections
import json
import re
import threading
import time

from . import bus, driver, image, nomad, puppets

MODES = ("missing", "update", "rebuild")
HEARTBEAT = 15
TAIL = 40

_TASK = re.compile(r"^TASK \[(?:[^\]]*? : )?(.+?)\] \*+\s*$")
_one_at_a_time = threading.Lock()


# ─── чистое ──────────────────────────────────────────────────────────────
def step_of(line):
    """Строка вывода ansible -> имя задачи или None. Только TASK: хендлеры и
    строки узлов шагом не являются."""
    m = _TASK.match(line or "")
    return m.group(1) if m else None


def needs_build(mode, serving, project):
    """Собирать ли образ. serving -- {контейнерный узел: [проекты с образом]}.

    missing -- только если образа нет хотя бы на одном контейнерном узле;
    update и rebuild -- всегда. Контейнерных узлов нет -- собирать нечего:
    на host-узле тело и есть узел."""
    if mode not in MODES:
        raise ValueError(f"no build mode {mode}; modes: {', '.join(MODES)}")
    if not serving:
        return False
    if mode == "missing":
        return any(project not in have for have in serving.values())
    return True


# ─── сервер ──────────────────────────────────────────────────────────────
def serving_now():
    """{контейнерный узел: [проекты, чей образ на нём объявлен]}."""
    out = {}
    for name, meta in nomad.nodes_meta().items():
        if meta.get("mop_driver", driver.DEFAULT) == driver.DEFAULT:
            continue
        have = nomad.node_dynamic_meta(name).get("mop_projects") or ""
        out[name] = [s for s in have.split(",") if s]
    return out


def run(req, send):
    """Одна сборка по запросу. send(**событие) -- шаг просителю.
    -> итог (dict), без done: его ставит подписчик."""
    origin = req.get("origin") or ""
    mode = req.get("mode") or "missing"
    if not puppets.looks_like_origin(origin):
        return {"error": f"{origin!r} doesn't look like a git-origin"}
    project = puppets.project_of(origin)
    try:
        needs_build(mode, {}, project)          # проверка режима до замка
    except ValueError as e:
        return {"error": str(e)}
    if not _one_at_a_time.acquire(blocking=False):
        send(step="waiting for another build to finish")
        _one_at_a_time.acquire()
    try:
        send(step="reading the project's .mop")
        got = image.prepare(origin)
        if not needs_build(mode, serving_now(), project):
            return {"ok": True, "skipped": True, "project": project}
        state = {"step": "starting", "since": time.time(), "alive": True}
        tail = collections.deque(maxlen=TAIL)

        def step(s):
            state["step"] = s
            send(step=s)

        def line(text):
            tail.append(text)
            s = step_of(text)
            if s:
                step(s)

        def beat():
            while state["alive"]:
                time.sleep(HEARTBEAT)
                if state["alive"]:
                    send(step=state["step"], elapsed=int(time.time() - state["since"]))
        threading.Thread(target=beat, daemon=True).start()
        try:
            r = image.build(origin, got, fresh=(mode == "rebuild"),
                            force=bool(req.get("force")), on_line=line, on_step=step)
        finally:
            state["alive"] = False
        out = {"ok": r["rc"] == 0, "rc": r["rc"], "project": project,
               "gone": [p["name"] for p in r["gone"]]}
        if r["rc"]:
            out["error"] = f"image build of {project} failed (ansible exit {r['rc']})"
            out["tail"] = list(tail)
        return out
    except Exception as e:
        return {"error": f"{project}: {nomad.describe_error(e)}"}
    finally:
        _one_at_a_time.release()


async def _handle(nc, msg):
    loop = asyncio.get_running_loop()
    try:
        req = json.loads(msg.data.decode())
    except ValueError:
        req = {}

    def send(**ev):
        data = json.dumps(ev, ensure_ascii=False).encode()
        asyncio.run_coroutine_threadsafe(nc.publish(msg.reply, data), loop)

    if req.get("verb") != "build":
        out = {"error": f"no such verb {req.get('verb')}; available: build"}
    else:
        send(step="accepted")
        out = await loop.run_in_executor(None, run, req, send)
    print(f"build {req.get('origin', '')} {req.get('mode', '')}: "
          f"{out.get('error') or ('skipped' if out.get('skipped') else 'ok')}", flush=True)
    await nc.publish(msg.reply, json.dumps({**out, "done": True}, ensure_ascii=False).encode())


async def serve():
    import nats
    nc = await nats.connect(**bus.auth(bus.config()), name="mop-builder",
                            allow_reconnect=True, max_reconnect_attempts=-1,
                            reconnect_time_wait=2)

    async def on_msg(msg):
        asyncio.create_task(_handle(nc, msg))

    await nc.subscribe(bus.build_subject(), cb=on_msg)
    print(f"mop-builder: subscribed to {bus.build_subject()}", flush=True)
    await asyncio.Event().wait()
