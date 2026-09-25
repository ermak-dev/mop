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
ansible или этап над телами), строки вывода пачками (#140) и сердцебиение;
итог -- последним. Сборки идут
по одной: шаблоны на гипервизоре делят зеркало (#100).

Данные и события, без печати: печатают проситель и журнал `mop server cluster builder`.
"""
import collections
import re
import threading
import time

from ..common import bus, puppets, service
from .. import driver
from . import image, nomad

MODES = ("missing", "update", "rebuild")
HEARTBEAT = 15
TAIL = 40
# Строки вывода -- пачкой не чаще LINES_EVERY секунд и не больше LINES_MOST
# за раз (#140): publish на строку заваливал бы шину на apt, а сообщение
# конечно. Строка длиннее LINE_WIDTH -- это json результата задачи, а не лог.
LINES_EVERY = 0.5
LINES_MOST = 200
LINE_WIDTH = 2000

_TASK = re.compile(r"^TASK \[(?:[^\]]*? : )?(.+?)\] \*+\s*$")
_one_at_a_time = threading.Lock()


# ─── чистое ──────────────────────────────────────────────────────────────
def step_of(line):
    """Строка вывода ansible -> имя задачи или None. Только TASK: хендлеры и
    строки узлов шагом не являются."""
    m = _TASK.match(line or "")
    return m.group(1) if m else None


class Batch:
    """Строки вывода, копящиеся до отправки пачкой (#140).

    add -- с новой строкой, due -- по таймеру: после строки может стоять
    минутная задача, и её строку нельзя держать до следующей. Оба отдают
    пачку или None. Под замком: add зовёт поток ansible, due -- таймер."""

    def __init__(self, interval=LINES_EVERY, most=LINES_MOST, width=LINE_WIDTH):
        self.interval, self.most, self.width = interval, most, width
        self.lines, self.since = [], None
        self._lock = threading.Lock()

    def add(self, line, now):
        with self._lock:
            if len(line) > self.width:
                line = line[:self.width] + "…"
            if not self.lines:
                self.since = now
            self.lines.append(line)
            if len(self.lines) >= self.most or now - self.since >= self.interval:
                return self._take()
            return None

    def due(self, now):
        with self._lock:
            if self.lines and now - self.since >= self.interval:
                return self._take()
            return None

    def take(self):
        with self._lock:
            return self._take()

    def _take(self):
        out, self.lines = self.lines, []
        return out or None


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


def failure(e):
    """Исключение сборки -> причина отказа просителю (#142).

    RuntimeError -- отказ, сформулированный нами (манифест, занятые тела):
    как есть. Nomad -- через describe_error. Прочее -- с типом, но не под
    чужим именем: «Nomad connection error» над ключом хоста гнал искать
    поломку не там. Имени проекта здесь нет: его ставит клиент."""
    import requests
    if isinstance(e, RuntimeError):
        return str(e)
    if isinstance(e, (nomad.ApiError, requests.exceptions.RequestException)):
        return nomad.describe_error(e)
    return f"{type(e).__name__}: {e}"


# ─── сервер ──────────────────────────────────────────────────────────────
def serving_now(refused=None):
    """{контейнерный узел: [проекты, чей образ на нём объявлен]}.

    Узел с неизвестным драйвером в решение не входит, а отказ по нему
    дописывается в refused (#175): опечатка одного узла не останавливает
    сборку для остальных."""
    out = {}
    for name, meta in nomad.nodes_meta().items():
        try:
            container = driver.is_container(driver.of_node(meta, name))
        except RuntimeError as e:
            if refused is not None:
                refused.append(str(e))
            continue
        if not container:
            continue
        have = nomad.node_dynamic_meta(name).get("mop_projects") or ""
        out[name] = [s for s in have.split(",") if s]
    return out


def run(req, send):
    """Одна сборка по запросу. send(**событие) -- шаг просителю.
    -> итог (dict), без done: его ставит answer."""
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
        refused = []
        serving = serving_now(refused)
        for why in refused:
            send(step=f"skipping {why}")
        if not needs_build(mode, serving, project):
            return {"ok": True, "skipped": True, "project": project}
        state = {"step": "starting", "since": time.time(), "alive": True}
        tail = collections.deque(maxlen=TAIL)

        def step(s):
            state["step"] = s
            send(step=s)

        batch, sending = Batch(), threading.Lock()

        def lines(take):
            # Взять и отправить -- под одним замком: пачки таймера и потока
            # ansible иначе могли бы уйти в шину не в том порядке.
            with sending:
                got = take()
                if got:
                    send(lines=got)

        def line(text):
            tail.append(text)
            lines(lambda: batch.add(text, time.time()))
            s = step_of(text)
            if s:
                lines(batch.take)       # строки задачи -- раньше её шага
                step(s)

        def beat():
            last = time.time()
            while state["alive"]:
                time.sleep(LINES_EVERY)
                if not state["alive"]:
                    break
                lines(lambda: batch.due(time.time()))
                if time.time() - last >= HEARTBEAT:
                    last = time.time()
                    send(step=state["step"], elapsed=int(last - state["since"]))
        threading.Thread(target=beat, daemon=True).start()
        try:
            r = image.build(origin, got, fresh=(mode == "rebuild"),
                            force=bool(req.get("force")), on_line=line, on_step=step)
        finally:
            state["alive"] = False
            lines(batch.take)
        out = {"ok": r["rc"] == 0, "rc": r["rc"], "project": project,
               "gone": [p["name"] for p in r["gone"]]}
        if r["rc"]:
            out["error"] = f"image build failed (ansible exit {r['rc']})"
            out["tail"] = list(tail)
        return out
    except Exception as e:
        return {"error": failure(e)}
    finally:
        _one_at_a_time.release()


def answer(_project, req, send):
    """Итог сборки, последним событием потока: done отличает его от шагов."""
    if req.get("verb") != "build":
        return {"error": f"no such verb {req.get('verb')}; available: build", "done": True}
    send(step="accepted")
    return {**run(req, send), "done": True}


def journal(_project, req, out):
    return [f"build {req.get('origin', '')} {req.get('mode', '')}: "
            f"{out.get('error') or ('skipped' if out.get('skipped') else 'ok')}"]


def banner(subject):
    return f"mop-builder: subscribed to {subject}"


async def serve(log):
    subj = bus.build_subject()
    await service.serve("mop-builder", subj, answer, log, journal, lambda: banner(subj))
