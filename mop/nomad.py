"""Связь с Nomad: клиент, джобы, аллокации.

Только REST (python-nomad). Внутрь узла Nomad больше не ходит: команды там
исполняет агент через шину (mop/bus.py, mop/agent.py), а ssh нужен лишь для
attach, ради живого терминала.

Раньше здесь жил `alloc exec` по websocket, и он был единственной дорогой на
узел. Команда ехала в query-строке URL, nginx перед Nomad резал URI на 8 КБ —
отсюда были и лимит на длину сообщения, и двухфазная установка session.py на
узел. Шина сняла и то и другое: ничего из этого в коде больше нет.
"""
import json
import os
import sys

try:
    import nomad as _nomad
    import nomad.api.exceptions
except ImportError:
    sys.exit("нужны библиотеки API: pip install --user --break-system-packages "
             "python-nomad")

from . import config  # noqa: E402

ADDR = config.get("NOMAD_ADDR")
TASK = "claude"          # имя задачи внутри группы папета
# Два датацентра. В `home` живут рабочие узлы, туда планировщик ставит папетов;
# джобы папетов объявляют home, поэтому на рабочую станцию оператора папет не
# сядет. В `control` — одна лишь управляющая машина, и делит их теперь только
# раздача: креды и ключи едут на пул, а не на их источник.
#
# Изначально управляющую машину вводили в кластер по другой причине: `alloc
# exec` ходит только внутрь аллокаций, и без своей аллокации мастер был для
# папета недосягаем. С переездом на шину эта причина отпала — папет пишет в
# mop.master.inbox, и членство мастера в кластере ему больше ни к чему.
POOL_DC = config.get("MOP_POOL_DC")
CONTROL_DC = config.get("MOP_CONTROL_DC")
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


def ready_nodes(datacenter=POOL_DC):
    """Имена узлов, на которые Nomad вообще станет что-то ставить.

    По умолчанию только рабочий пул: раздавать креды и ключи на управляющую
    машину не надо — она их источник."""
    return {n["Name"] for n in client().nodes.get_nodes()
            if n["Status"] == "ready"
            and n.get("SchedulingEligibility") != "ineligible"
            and (datacenter is None or n.get("Datacenter") == datacenter)}


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
