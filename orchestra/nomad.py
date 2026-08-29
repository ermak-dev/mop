"""Связь с Nomad: клиент, exec внутрь аллокации, поиск аллокаций.

Всё, кроме attach, ходит через API (REST — python-nomad, команды внутри
аллокаций — exec-websocket). ssh нужен только attach, ради живого терминала.
"""
import base64
import json
import os
import sys
from urllib.parse import quote

try:
    import nomad as _nomad
    import nomad.api.exceptions
    import websocket
except ImportError:
    sys.exit("нужны библиотеки API: pip install --user --break-system-packages "
             "python-nomad websocket-client")

ADDR = os.environ.get("NOMAD_ADDR", "https://nomad.ermak.dev")
TASK = "claude"          # имя задачи внутри группы воркера
MAX_EXEC_URL = 7800      # nginx перед Nomad режет URI на 8 КБ
NotFound = _nomad.api.exceptions.URLNotFoundNomadException
ApiError = _nomad.api.exceptions.BaseNomadException

_client = None


def token():
    t = os.environ.get("NOMAD_TOKEN")
    if t:
        return t
    with open(os.path.expanduser("~/.config/nomad/bootstrap.json")) as f:
        return json.load(f)["SecretID"]


def client():
    global _client
    if _client is None:
        try:
            _client = _nomad.Nomad(address=ADDR, token=token(), timeout=30, verify=True)
        except Exception as e:
            raise ConnectionError(f"нет связи с {ADDR}: {e}")
    return _client


def describe_error(e):
    """Человеческая причина отказа Nomad — одинаково во всех вызывающих."""
    return f"ошибка API Nomad: {e}" if isinstance(e, ApiError) else f"ошибка связи с Nomad: {e}"


def alloc_exec(alloc_id, argv, task=TASK, timeout=30):
    """nomad alloc exec: argv исполняется внутри аллокации через
    exec-websocket API, возвращает (stdout+stderr, exit_code)."""
    url = (ADDR.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
           + f"/v1/client/allocation/{alloc_id}/exec"
           + f"?task={quote(task)}&tty=false&command={quote(json.dumps(argv))}")
    # Команда едет в query-строке, а nginx перед Nomad режет URI примерно на
    # 8 КБ и отвечает 414 ещё до Nomad. Без этой проверки длинное сообщение
    # падало бы невнятным WebSocketBadStatusException.
    if len(url) > MAX_EXEC_URL:
        raise ValueError(
            f"команда для alloc exec длиннее лимита URL ({len(url)} > {MAX_EXEC_URL} байт) — "
            f"сократи сообщение или передай его файлом")
    ws = websocket.create_connection(
        url, header={"X-Nomad-Token": token()}, timeout=timeout)
    # без закрытия stdin сервер шлёт только data-кадры и никогда — exited
    ws.send(json.dumps({"stdin": {"close": True}}))
    out, code = [], None
    try:
        while True:
            try:
                frame = ws.recv()
            except websocket.WebSocketConnectionClosedException:
                break
            if not frame:
                break
            msg = json.loads(frame)
            for stream in ("stdout", "stderr"):
                data = (msg.get(stream) or {}).get("data")
                if data:
                    out.append(base64.b64decode(data).decode(errors="replace"))
            if msg.get("exited"):
                code = (msg.get("result") or {}).get("exit_code", 0)
                break
    finally:
        ws.close()
    return "".join(out), code


def sh(alloc, script, timeout=30):
    """Шелл внутри аллокации — обвязка, которая иначе повторяется в каждом вызове."""
    return alloc_exec(alloc["ID"], ["/bin/bash", "-c", script], timeout=timeout)


def alloc_restart(alloc_id):
    """POST /v1/client/allocation/:id/restart — в python-nomad такого нет."""
    import requests
    r = requests.post(f"{ADDR}/v1/client/allocation/{alloc_id}/restart",
                      headers={"X-Nomad-Token": token()}, json={}, timeout=30)
    r.raise_for_status()


def alloc_stop(alloc_id):
    client().allocation.stop_allocation(alloc_id)


def latest_alloc(job_id):
    try:
        allocs = client().job.get_allocations(job_id)
    except NotFound:
        return None
    run = [a for a in allocs if a["DesiredStatus"] == "run"] or allocs
    return max(run, key=lambda a: a["CreateIndex"]) if run else None


def ready_nodes():
    """Имена узлов, на которые Nomad вообще станет что-то ставить."""
    return {n["Name"] for n in client().nodes.get_nodes()
            if n["Status"] == "ready" and n.get("SchedulingEligibility") != "ineligible"}


def node_capacity(node_summary):
    """(свободно МБ, всего МБ) на узле. Reserved — память, отрезанная под
    остальных жильцов хоста, в бюджет пула она не входит."""
    node = client().node.get_node(node_summary["ID"])
    total = node["NodeResources"]["Memory"]["MemoryMB"]
    total -= ((node.get("ReservedResources") or {}).get("Memory") or {}).get("MemoryMB") or 0
    used = sum(a["Resources"]["MemoryMB"]
               for a in client().node.get_allocations(node_summary["ID"])
               if a["ClientStatus"] == "running")
    return total - used, total


def register(spec):
    client().jobs.register_job(spec)


def deregister(job_id, purge=True):
    client().job.deregister_job(job_id, purge=purge)


def get_job(job_id):
    return client().job.get_job(job_id)
