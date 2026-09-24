#!/usr/bin/env python3
"""Доменные значения без пула: python3 tests/values.py [--capture]

Проект, владелец задания, строка ростера и глагол жили словарями и
кортежами: строковые ключи расходились молча -- тот же класс дефектов, что
закрыли #145 (State) и #154 (origin). Проект -- строки реестра плюс отдельный
словарь лимитов плюс отдельный словарь просьб (#197); строку ростера читают
puppets, web, mcp и cli; владелец -- строка файла и словарь {user, at};
`Verb` -- два разных namedtuple у агента и у сервиса кластера (#204).

HYPOTHESIS: словарь без схемы не ловит опечатку ключа -- ни при записи, ни
при чтении; два `Verb` расходятся полями.
SOLUTION: frozen dataclass на каждую сущность (mop/domain.py: Project, Owner,
Verb; PuppetRow -- рядом с State в mop/state.py), JSON наружу -- через
to_dict/from_dict в одном месте на тип. Поведение не меняется.
Характеризация: tests/values_snapshot.json снят с кода ДО правки (--capture)
-- всё, что уходит из процесса: ответы агента по шине (факты клона с
владельцем, states), строка `.git/mop-owner` и откат аренды, ростер и строки
мастера, снимок дашборда, строки `mop list` и таблица MCP, ответы сервиса кластера про
проекты и лимит. После правки -- байт в байт.
STATUS: FIXED — see #204
"""
import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(HERE))

from mop import agent, bus, cluster, puppets, projects, web  # noqa: E402
from mop.cli.core import list as mop_list  # noqa: E402
from mop.cli.service import mcp  # noqa: E402

SNAPSHOT = os.path.join(HERE, "values_snapshot.json")
NOW = 1_000_000.5
MOP = "git@git.example.dev:someone/mop.git"
RUGENT = "git@git.example.dev:rugent/rugent.git"
CLEAN = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
WORK = {"cur": "bug/12", "def": "master", "dirty": 2, "ahead": 1}


def dump(value):
    """Как это уйдёт из процесса: json.dumps без сортировки -- порядок ключей
    тоже часть формы. Значение с to_dict сериализуется им: иначе JSON не
    собрать вовсе."""
    return json.dumps(value, ensure_ascii=False,
                      default=lambda v: v.to_dict())


# ─── ростер мастера ──────────────────────────────────────────────────────
def job(name, origin="", status="running", llm="claude"):
    meta = {"origin": origin, "llm": llm} if origin else None
    return {"ID": name, "Status": status, "Meta": meta}


def alloc(name, node, status="running"):
    return {"ID": f"a-{name}", "JobID": name, "NodeName": node,
            "ClientStatus": status, "DesiredStatus": "run"}


def item(j, a=None, error=None, task=None, reason=None):
    return {"job": j, "alloc": a, "error": error, "task": task, "reason": reason}


ITEMS = [
    item(job("pu-mop-1", MOP), alloc("pu-mop-1", "mate")),
    item(job("pu-mop-2", MOP, llm="glm"), alloc("pu-mop-2", "mate")),
    item(job("pu-mop-3", MOP), alloc("pu-mop-3", "mate")),
    item(job("pu-mop-4", MOP), alloc("pu-mop-4", "gpu")),
    item(job("pu-mop-5", MOP), alloc("pu-mop-5", "mate")),
    item(job("pu-rugent-1", RUGENT, status="pending")),
    item(job("pu-rugent-2", RUGENT), alloc("pu-rugent-2", "hyper", "pending"),
         task={"state": "pending", "restarts": 3, "exit": 1, "next_s": 20,
               "failed": False}, reason="clone failed: no route"),
    item(job("pu-rugent-3", RUGENT), error="no connection to nomad"),
    item(job("pu-x-1")),
]


def facts(session, clone, owner=None, screen="Herding bytes"):
    c = dict(clone, origin=MOP, owner=owner) if clone is not None else None
    return {"present": True, "screen": screen, "session": session, "clone": c}


STATES = {
    "mate": {"puppets": {
        # Свежая чужая аренда на чистом клоне -- держит окно диспатча.
        "pu-mop-1": facts("idle 1 1", CLEAN, {"user": "anton", "at": int(NOW) - 10}),
        # Старая аренда, но в клоне работа -- держит.
        "pu-mop-2": facts("busy 1 1", WORK, {"user": "olga", "at": int(NOW) - 99999}),
        # Старая аренда на чистом клоне -- ничья.
        "pu-mop-3": facts("idle 1 1", CLEAN, {"user": "olga", "at": int(NOW) - 99999}),
        # pu-mop-5 узел не прислал вовсе.
    }},
    "gpu": "agent silent",
}

SIZES = {"pu-mop-1": {"sizes": {"pu-mop-1": 1234}},
         "pu-mop-2": "du timed out",
         "pu-mop-3": {"sizes": {}},
         "pu-mop-5": {"sizes": {"pu-mop-5": 3 * 2**20}}}


def fake_bus():
    """Ростер и узлы без шины. -> функция отката."""
    saved = (puppets.items, puppets.time.time, bus.request_many, bus.request_stream)

    def request_many(asked, timeout=None):
        return {n: (bus.BusError(STATES[n]) if isinstance(STATES[n], str)
                    else copy.deepcopy(STATES[n])) for n in asked}

    def request_stream(asked, timeout=None):
        for name in asked:
            got = SIZES.get(name)
            yield name, (Exception(got) if isinstance(got, str) else copy.deepcopy(got))

    puppets.items = lambda project=None, stale=False: copy.deepcopy(ITEMS)
    puppets.time.time = lambda: NOW
    bus.request_many, bus.request_stream = request_many, request_stream

    def undo():
        puppets.items, puppets.time.time, bus.request_many, bus.request_stream = saved
    return undo


def roster_forms():
    undo = fake_bus()
    try:
        rows = puppets.puppet_rows(sizes=False)
        sizes = puppets.puppet_sizes(rows)
        out = {
            "roster": dump(puppets.roster()),
            "rows": dump(rows),
            "stream": dump(list(puppets.puppet_rows_stream())),
            "rows_sized": dump(puppets.puppet_rows()),
            "sizes": dump(sizes),
            "list": "\n".join(mop_list.line(r) for r in puppets.puppet_rows()),
            "mcp": "\n".join(mcp._roster("") + mcp._roster("rugent")),
            "dashboard": dump(web.snapshot(
                web.with_sizes(rows, sizes), [{"name": "mate"}], [], [], [],
                [{"at": 1.0, "event": "send"}], ["states: x"], NOW)),
            "owner_of": dump([puppets.owner_of(STATES["mate"]["puppets"].get(n), NOW)
                              for n in ("pu-mop-1", "pu-mop-2", "pu-mop-3", "pu-mop-5")]
                             + [puppets.owner_of(None, NOW),
                                puppets.owner_of({"clone": None}, NOW)]),
        }
    finally:
        undo()
    return out


# ─── агент: факты клона и аренда ─────────────────────────────────────────
OWNER_LINES = ["anton\t1000000\n", "anton\t1000000", "  anton \t 5", "garbage",
               "", "\t5", "anton\tsoon", "a\tb\tc"]


def agent_forms():
    """Факты клона с владельцем -- ответ агента по шине; файл аренды --
    настоящий шелл над временным клоном, как tests/agent.py (#189)."""
    out = {}
    saved = (agent.bsh, agent.clone_dir, agent.session_json, agent._event,
             agent.time.time, agent.tmux_alive, agent.session_probe,
             agent._mine)
    root = tempfile.mkdtemp(prefix="mop-test-204-")
    os.makedirs(os.path.join(root, ".git"))
    path = os.path.join(root, ".git", "mop-owner")

    async def canned(name, script, timeout=20):
        line = OWNER_LINES[int(name.rsplit("-", 1)[1])]
        return (f"cur=master\ndef=origin/master\norigin={MOP}\ndirty=0\nahead=0\n"
                f"owner={line.splitlines()[0] if line else ''}\n"), 0

    async def real(name, script, timeout=20):
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        return r.stdout + r.stderr, r.returncode

    async def nothing(*a, **k):
        return None

    async def yes(*a, **k):
        return True

    async def probe(name):
        return "idle 1 1"

    try:
        agent.bsh, agent._mine = canned, yes
        agent.tmux_alive, agent.session_probe = yes, probe
        names = [f"pu-mop-{i}" for i in range(len(OWNER_LINES))]
        out["clone_facts"] = dump([asyncio.run(agent.clone_facts(n)) for n in names])
        out["states"] = dump(asyncio.run(agent.v_states(None, {"names": names})))

        # Аренда: запись, отказ, откат -- байты файла после каждого шага.
        agent.bsh, agent._event = real, nothing
        agent.clone_dir = lambda name: root
        agent.time.time = lambda: NOW

        async def ok(name, cmd, timeout=20):
            return {"msg_id": "m-1"}

        async def fail(name, cmd, timeout=20):
            return {"error": "not delivered"}

        def file():
            return open(path).read() if os.path.exists(path) else None

        steps = []

        def step(before, deliver, req):
            if before is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                open(path, "w").write(before)
            agent.session_json = deliver
            got = asyncio.run(agent.v_send(None, dict({"name": "pu-mop-1",
                                                       "message": "m"}, **req)))
            steps.append({"before": before, "reply": got, "after": file()})
        step(None, ok, {"owner": "anton", "from_name": "a"})
        step(f"olga\t{int(NOW) - 60}\n", ok, {"owner": "anton", "from_name": "a"})
        step(f"olga\t{int(NOW) - 60}\n", ok, {"owner": "anton", "force": True})
        step(f"olga\t{int(NOW) - 9999}\n", ok, {"owner": "anton"})
        step(f"anton\t{int(NOW) - 60}\n", ok, {"owner": "anton"})
        step(None, fail, {"owner": "dave"})
        step("carol\t1000\n", fail, {"owner": "dave"})
        step("garbage", ok, {"owner": "dave"})
        step(f"olga\t{int(NOW) - 60}\n", ok, {})
        out["lease"] = dump(steps)
    finally:
        (agent.bsh, agent.clone_dir, agent.session_json, agent._event,
         agent.time.time, agent.tmux_alive, agent.session_probe,
         agent._mine) = saved
    return out


# ─── сервис кластера: проекты ────────────────────────────────────────────
def project_forms():
    """Ответы глаголов про проекты и лимит -- то, что уходит мастеру и
    оператору по шине, и файлы, которые пишет сервис."""
    out = {}
    d = tempfile.mkdtemp(prefix="mop-test-204-")
    reg, lim = os.path.join(d, "projects"), os.path.join(d, "limits.json")
    saved = (projects.FILE, projects.LIMITS, projects.read.__defaults__,
             projects.write.__defaults__, projects.read_limits.__defaults__,
             projects.write_limits.__defaults__, cluster._live_count,
             cluster.next_name, cluster.store_workspace, cluster.nomad.register,
             cluster.spec.job_spec)
    registered = []
    try:
        projects.read.__defaults__ = projects.write.__defaults__ = (reg,)
        projects.read_limits.__defaults__ = projects.write_limits.__defaults__ = (lim,)
        projects.write({MOP, RUGENT, "legacy"})
        projects.write_limits({"mop": 2})
        out["projects"] = dump(cluster._projects("admin", {}))
        out["limit_set"] = dump(cluster._project_limit("admin", {"name": "rugent",
                                                                "value": "5"}))
        out["limit_clear"] = dump(cluster._project_limit("admin", {"name": "mop",
                                                                  "value": None}))
        out["limit_foreign"] = dump(cluster._project_limit("admin", {"name": "nope",
                                                                    "value": 1}))
        out["limits_file"] = open(lim).read()
        out["registry_file"] = open(reg).read()
        cluster._live_count = lambda target: 5
        cluster.next_name = lambda target: f"pu-{target}-6"
        cluster.store_workspace = lambda root, name, req: None
        cluster.nomad.register = registered.append
        cluster.spec.job_spec = lambda name, origin, profile=None, cont=False: \
            {"Job": {"ID": name, "origin": origin}}
        out["add_over"] = dump(cluster._add("rugent", {"origin": RUGENT}))
        out["add_free"] = dump(cluster._add("mop", {"origin": MOP}))
        out["registered"] = dump(registered)
    finally:
        (projects.FILE, projects.LIMITS, projects.read.__defaults__,
         projects.write.__defaults__, projects.read_limits.__defaults__,
         projects.write_limits.__defaults__, cluster._live_count,
         cluster.next_name, cluster.store_workspace, cluster.nomad.register,
         cluster.spec.job_spec) = saved
    return out


def forms():
    return {**roster_forms(), **agent_forms(), **project_forms()}


def check_characterization_204():
    """Каждая сериализованная форма -- та же, что до правки, байт в байт."""
    with open(SNAPSHOT) as f:
        want = json.load(f)
    got = forms()
    out = []
    for key in sorted(set(want) | set(got)):
        if got.get(key) != want.get(key):
            out.append(f"{key} drifted:\n    got    {got.get(key)!r}\n"
                       f"    wanted {want.get(key)!r}")
    return out


# ─── сами типы ───────────────────────────────────────────────────────────
def frozen(value, field):
    """Присваивание полю замороженного значения бросает."""
    import dataclasses
    try:
        setattr(value, field, None)
    except dataclasses.FrozenInstanceError:
        return True
    return False


def check_owner_204():
    """Владелец: строка файла и словарь шины -- два вида одного значения,
    и оба обратимы; мусор -- не владелец."""
    from mop.domain import Owner
    out = []
    o = Owner("anton", 1000000)
    if o.render() != "anton\t1000000\n":
        out.append(f"Owner.render -> {o.render()!r}")
    if Owner.parse(o.render()) != o:
        out.append(f"Owner.parse(render) -> {Owner.parse(o.render())!r}, wanted {o!r}")
    for junk in ("", None, "garbage", "\t5", "anton\tsoon", "  \t1"):
        if Owner.parse(junk) is not None:
            out.append(f"Owner.parse({junk!r}) must be None, got {Owner.parse(junk)!r}")
    if o.to_dict() != {"user": "anton", "at": 1000000}:
        out.append(f"Owner.to_dict -> {o.to_dict()!r}")
    if Owner.from_dict(o.to_dict()) != o or Owner.from_dict(None) is not None:
        out.append("Owner.from_dict must invert to_dict and keep None as None")
    if not frozen(o, "user"):
        out.append("Owner must be frozen")
    return out


def check_project_204():
    """Проект: origin, имя по driver.project_of, лимит и просьбы -- одно
    значение, а не три словаря по имени."""
    from mop import driver
    from mop.domain import Project
    out = []
    limits = {"mop": 2}
    asks = {"mop": {"MOP_MEM_MB": "6144"}, "rugent": {}}
    p = Project.of(MOP, limits, asks)
    want = Project(MOP, 2, {"MOP_MEM_MB": "6144"})
    if p != want:
        out.append(f"Project.of -> {p!r}, wanted {want!r}")
    if p.name != driver.project_of(MOP):
        out.append(f"Project.name -> {p.name!r}")
    bare = Project.of(RUGENT)
    if (bare.limit, bare.asks, bare.name) != (None, {}, "rugent"):
        out.append(f"Project.of without tables -> {bare!r}")
    p.asks["MOP_CORES"] = "1"
    if asks["mop"] != {"MOP_MEM_MB": "6144"}:
        out.append("Project.of must copy the project's asks, not share the table's")
    if not frozen(p, "limit"):
        out.append("Project must be frozen")
    return out


def check_puppet_row_204():
    """Строка ростера: поля и их порядок -- прежние ключи словаря; словарь
    обратим; незнакомый ключ -- отказ, а не молча лишнее поле."""
    from mop.state import PuppetRow
    out = []
    keys = ["name", "node", "alloc_status", "state", "kind", "owner", "llm",
            "origin", "disk_kb"]
    d = dict(zip(keys, ["pu-mop-1", "mate", "running", "free", "free", "-",
                        "claude", MOP, 12]))
    r = PuppetRow.from_dict(d)
    if list(r.to_dict()) != keys or r.to_dict() != d:
        out.append(f"PuppetRow.to_dict -> {r.to_dict()!r}, wanted {d!r}")
    try:
        PuppetRow.from_dict(dict(d, alloc="running"))
        out.append("PuppetRow.from_dict must refuse an unknown key")
    except TypeError:
        pass
    if not frozen(r, "state"):
        out.append("PuppetRow must be frozen")
    return out


def check_one_verb_204():
    """Один Verb на агента и сервис кластера; у агента acting не бывает."""
    from mop import domain
    out = []
    if not (agent.Verb is cluster.Verb is domain.Verb):
        out.append("agent.Verb and cluster.Verb must be domain.Verb")
    fields = [f for f in getattr(domain.Verb, "__dataclass_fields__", {})]
    if fields != ["fn", "scope", "named", "acting"]:
        out.append(f"Verb fields -> {fields}")
    if any(v.acting for v in agent.VERBS.values()):
        out.append("no agent verb is acting")
    if domain.Verb.__dataclass_params__.frozen is not True:
        out.append("Verb must be frozen")
    return out


CHECKS = (check_characterization_204, check_owner_204, check_project_204,
          check_puppet_row_204, check_one_verb_204)


def main():
    if sys.argv[1:] == ["--capture"]:
        with open(SNAPSHOT, "w") as f:
            json.dump(forms(), f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        print(f"captured {SNAPSHOT}")
        return 0
    out = []
    for check in CHECKS:
        try:
            out += check()
        except Exception as e:
            out.append(f"{check.__name__}: {type(e).__name__}: {e}")
    for line in out:
        print(f"FAILED  {line}")
    print("values:", "FAILED" if out else "ok")
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main())
