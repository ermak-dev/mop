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
    sys.exit("API library required: pip install --user --break-system-packages "
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
            raise ConnectionError(f"no connection to {ADDR}: {e}")
    return _client


def describe_error(e):
    """Человеческая причина отказа Nomad — одинаково во всех вызывающих."""
    return f"Nomad API error: {e}" if isinstance(e, ApiError) else f"Nomad connection error: {e}"


def _raw(method, path, **kw):
    """Ручка API, которой python-nomad не знает: сырой HTTP с токеном.
    -> ответ requests, уже проверенный на код."""
    import requests
    r = requests.request(method, f"{ADDR}{path}",
                         headers={"X-Nomad-Token": token()}, timeout=30, **kw)
    r.raise_for_status()
    return r


def alloc_restart(alloc_id):
    """POST /v1/client/allocation/:id/restart — в python-nomad такого нет."""
    _raw("POST", f"/v1/client/allocation/{alloc_id}/restart", json={})


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


# ─── жизненный цикл узла ─────────────────────────────────────────────────
# Состав пула перестал быть неизменным вместе с появлением драйверов: узел
# теперь выводят, чтобы отдать его память телам, и вводят обратно. Токен Nomad
# есть только у управляющей машины, поэтому и живёт это здесь.
LIVE_STATUSES = ("running", "pending")


def forget_refusal(node, allocs):
    """Почему этот узел нельзя убрать из ростера; None — можно.

    Чистая функция и проверяется без кластера (tests/node.py), потому что
    ошибиться тут можно ровно один раз: узел, забытый из-под живого папета,
    исчезает из ростера, а claude в нём продолжает работать — и мастер о нём
    больше никогда не узнает.

    Два запрета, и второй не очевиден: узел, открытый для планирования,
    забывать нельзя даже пустым — планировщик поставит на него папета между
    проверкой и сносом. Исключение — мёртвый узел: его как раз затем и
    забывают, чтобы не мозолил ростер, и поставить на него всё равно нечего."""
    live = [a["JobID"] for a in allocs if a.get("ClientStatus") in LIVE_STATUSES]
    if live:
        return (f"{node['Name']} still runs {', '.join(sorted(live))} — "
                f"drain it first: mop node drain {node['Name']}")
    if node.get("Status") != "down" and node.get("SchedulingEligibility") == "eligible":
        return (f"{node['Name']} is still open to the scheduler — "
                f"drain it first: mop node drain {node['Name']}")
    return None


def _node_post(node_name, path, body=None):
    """POST в узловую ручку Nomad; python-nomad таких не знает."""
    nid = require_node_id(node_name)
    _raw("POST", f"/v1/node/{nid}/{path}", json={"NodeID": nid, **(body or {})})
    return nid


def node_eligibility(node_name, eligible):
    """Открыть или закрыть узел для планировщика."""
    _node_post(node_name, "eligibility",
               {"Eligibility": "eligible" if eligible else "ineligible"})


def node_drain(node_name, deadline=300):
    """Увести папетов с узла и закрыть его для планирования.

    Deadline — не «сколько ждать ответа», а сколько Nomad даёт задачам уйти
    по-хорошему, прежде чем снимет их силой. Врапер по TERM гасит сессию, так
    что уход по-хорошему — это закрытая сессия, а не убитый claude."""
    _node_post(node_name, "drain",
               {"DrainSpec": {"Deadline": deadline * 10**9,
                              "IgnoreSystemJobs": False}})


def node_allocs(node_name):
    nid = node_id(node_name)
    return client().node.get_allocations(nid) if nid else []


def node_forget(node_name):
    """Убрать узел из ростера. Предохранитель — у вызывающего (forget_refusal)."""
    _node_post(node_name, "purge")


def node_summary(node_name):
    """Узел из ростера по имени; None, если такого нет. Ineligible тоже
    считается: узел, выведенный из планирования, остаётся узлом."""
    return next((n for n in client().nodes.get_nodes() if n["Name"] == node_name),
                None)


def node_id(node_name):
    """ID узла по имени; None, если такого нет."""
    return (node_summary(node_name) or {}).get("ID")


def require_node_id(node_name):
    """ID узла по имени; громкий отказ, если узла нет."""
    nid = node_id(node_name)
    if nid is None:
        raise RuntimeError(f"no node {node_name} in the cluster")
    return nid


def set_node_meta(node_name, updates):
    """Динамическая `meta` узла: то, что меняется без перезаписи client.hcl.

    Здесь живёт перечень шардов, чьи образы на узле собраны, — его ведёт
    `mop driver build`. Статическую строку из client.hcl динамическая
    перекрывает и переживает перезапуск клиента, поэтому прогон deploy
    собранных образов не забывает.

    Ходит отсюда, с управляющей машины: токен Nomad есть только у неё, и
    выдавать его узлам ради одной записи значило бы вернуть то, ради чего
    заводили шину."""
    _raw("POST", f"/v1/client/metadata?node_id={require_node_id(node_name)}",
         json={"Meta": updates})


def node_dynamic_meta(node_name):
    """Динамическая `meta` узла прямо с узла, а не из серверной копии.

    Серверная отстаёт: после записи она догоняет секундами, и прочитанный в
    это окно перечень шардов оказался бы пустым. Дописать к нему новый шард
    значит стереть ранее объявленные образы — узел молча перестал бы
    обслуживать половину своих шардов, а увидели бы это по папетам, зависшим
    в queued."""
    r = _raw("GET", f"/v1/client/metadata?node_id={require_node_id(node_name)}")
    return r.json().get("Dynamic") or {}


def node_meta(node_name):
    """`meta` клиента Nomad по имени узла. Пусто, если узла нет.

    Здесь лежит драйвер узла: мастеру он нужен, чтобы знать, чем входить в
    тело папета (`mop attach`), а спрашивать об этом сам узел нельзя — ответ
    пришёл бы по той же шине, которой может и не быть, когда как раз и
    понадобился аварийный вход. Значение кладёт `mop deploy` из той же
    переменной инвентаря, что и в юнит агента."""
    nid = node_id(node_name)
    return (client().node.get_node(nid).get("Meta") or {}) if nid else {}


def nodes_meta():
    """{имя узла: его meta} по всему кластеру, одним обходом ростера —
    включая выведенные из планирования: узел, снятый с раздачи, остаётся
    узлом, и образы на нём лежат."""
    c = client()
    return {n["Name"]: (c.node.get_node(n["ID"]).get("Meta") or {})
            for n in c.nodes.get_nodes()}
