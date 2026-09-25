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
SOLUTION: frozen dataclass на каждую сущность (mop/common/domain.py: Project, Owner,
Verb; PuppetRow -- рядом с State в mop/common/state.py), JSON наружу -- через
to_dict/from_dict в одном месте на тип. Поведение не меняется.
Характеризация: tests/values_snapshot.json снят с кода ДО правки (--capture)
-- всё, что уходит из процесса: ответы агента по шине (факты клона с
владельцем, states), строка `.git/mop-owner` и откат аренды, ростер и строки
мастера, снимок дашборда, строки `mop list` и таблица MCP, ответы сервиса кластера про
проекты и лимит. После правки -- байт в байт.
STATUS: FIXED — see #204
"""
import asyncio
import contextlib
import copy
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(HERE))

from _lib import Checks, FakeNomad, bash, patched, restored  # noqa: E402
from mop.node import agent  # noqa: E402
from mop.common import bus, puppets, projects  # noqa: E402
from mop.server import cluster, web  # noqa: E402
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


@contextlib.contextmanager
def fake_bus():
    """Ростер и узлы без шины на время блока."""

    def request_many(verb, asked, timeout=None, **fields):
        return {n: (bus.BusError(STATES[n]) if isinstance(STATES[n], str)
                    else copy.deepcopy(STATES[n])) for n in asked}

    def request_stream(verb, asked, timeout=None, **fields):
        for name in asked:
            got = SIZES.get(name)
            yield name, (Exception(got) if isinstance(got, str) else copy.deepcopy(got))

    with patched(puppets, items=lambda project=None, stale=False: copy.deepcopy(ITEMS)), \
            patched(puppets.time, time=lambda: NOW), \
            patched(bus, request_many=request_many, request_stream=request_stream):
        yield


def roster_forms():
    with fake_bus():
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
    return out


# ─── агент: факты клона и аренда ─────────────────────────────────────────
OWNER_LINES = ["anton\t1000000\n", "anton\t1000000", "  anton \t 5", "garbage",
               "", "\t5", "anton\tsoon", "a\tb\tc"]


def agent_forms():
    """Факты клона с владельцем -- ответ агента по шине; файл аренды --
    настоящий шелл над временным клоном, как tests/agent.py (#189)."""
    out = {}
    root = tempfile.mkdtemp(prefix="mop-test-204-")
    os.makedirs(os.path.join(root, ".git"))
    path = os.path.join(root, ".git", "mop-owner")

    async def canned(name, script, timeout=20):
        line = OWNER_LINES[int(name.rsplit("-", 1)[1])]
        return (f"cur=master\ndef=origin/master\norigin={MOP}\ndirty=0\nahead=0\n"
                f"owner={line.splitlines()[0] if line else ''}\n"), 0

    async def nothing(*a, **k):
        return None

    async def yes(*a, **k):
        return True

    async def probe(name):
        return "idle 1 1"

    with restored(agent, "bsh", "clone_dir", "session_json", "_event", "tmux_alive",
                  "session_probe", "_mine"), restored(agent.time, "time"):
        agent.bsh, agent._mine = canned, yes
        agent.tmux_alive, agent.session_probe = yes, probe
        names = [f"pu-mop-{i}" for i in range(len(OWNER_LINES))]
        out["clone_facts"] = dump([asyncio.run(agent.clone_facts(n)) for n in names])
        out["states"] = dump(asyncio.run(agent.v_states(None, {"names": names})))

        # Аренда: запись, отказ, откат -- байты файла после каждого шага.
        agent.bsh, agent._event = bash, nothing
        agent.clone_dir = lambda name: root
        agent.time.time = lambda: NOW

        async def ok(name, cmd, timeout=20):
            return {"msg_id": "m-1"}

        async def refused(name, cmd, timeout=20):
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
        step(None, refused, {"owner": "dave"})
        step("carol\t1000\n", refused, {"owner": "dave"})
        step("garbage", ok, {"owner": "dave"})
        step(f"olga\t{int(NOW) - 60}\n", ok, {})
        out["lease"] = dump(steps)
    return out


# ─── сервис кластера: проекты ────────────────────────────────────────────
def project_forms():
    """Ответы глаголов про проекты и лимит -- то, что уходит мастеру и
    оператору по шине, и файлы, которые пишет сервис."""
    out = {}
    d = tempfile.mkdtemp(prefix="mop-test-204-")
    reg, lim = os.path.join(d, "projects"), os.path.join(d, "limits.json")
    with restored(projects, "FILE", "LIMITS"), \
            restored(projects.read, "__defaults__"), restored(projects.write, "__defaults__"), \
            restored(projects.read_limits, "__defaults__"), \
            restored(projects.write_limits, "__defaults__"), \
            restored(cluster, "_live_count", "next_name", "store_workspace"), \
            restored(cluster.spec, "job_spec"):
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
        cluster.spec.job_spec = lambda name, origin, profile=None, cont=False, branch=None: \
            {"Job": {"ID": name, "origin": origin}}
        # Nomad -- поддельный, параметром (#275): что зарегистрировано, видно
        # по его вызовам.
        fake = FakeNomad()
        with cluster.using(fake):
            out["add_over"] = dump(cluster._add("rugent", {"origin": RUGENT}))
            out["add_free"] = dump(cluster._add("mop", {"origin": MOP}))
        registered = [call[1] for call in fake.calls if call[0] == "register"]
        out["registered"] = dump(registered)
    return out


def forms():
    return {**roster_forms(), **agent_forms(), **project_forms()}


def check_characterization_204(c):
    """Каждая сериализованная форма -- та же, что до правки, байт в байт."""
    with open(SNAPSHOT) as f:
        want = json.load(f)
    got = forms()
    for key in sorted(set(want) | set(got)):
        c.expect(f"{key} drifted", got.get(key), want.get(key))


# ─── сами типы ───────────────────────────────────────────────────────────
def frozen(value, field):
    """Присваивание полю замороженного значения бросает."""
    import dataclasses
    try:
        setattr(value, field, None)
    except dataclasses.FrozenInstanceError:
        return True
    return False


def check_owner_204(c):
    """Владелец: строка файла и словарь шины -- два вида одного значения,
    и оба обратимы; мусор -- не владелец."""
    from mop.common.domain import Owner
    o = Owner("anton", 1000000)
    c.expect("Owner.render", o.render(), "anton\t1000000\n")
    c.expect("Owner.parse(render)", Owner.parse(o.render()), o)
    for junk in ("", None, "garbage", "\t5", "anton\tsoon", "  \t1"):
        c.check(f"Owner.parse({junk!r}) must be None", not (Owner.parse(junk) is not None),
                f"got {Owner.parse(junk)!r}")
    c.expect("Owner.to_dict", o.to_dict(), {"user": "anton", "at": 1000000})
    c.check("Owner.from_dict must invert to_dict and keep None as None",
            not (Owner.from_dict(o.to_dict()) != o or Owner.from_dict(None) is not None))
    c.check("Owner must be frozen", frozen(o, "user"))


def check_project_204(c):
    """Проект: origin, имя по driver.project_of, лимит и просьбы -- одно
    значение, а не три словаря по имени."""
    from mop import driver
    from mop.common.domain import Project
    limits = {"mop": 2}
    asks = {"mop": {"MOP_MEM_MB": "6144"}, "rugent": {}}
    p = Project.of(MOP, limits, asks)
    want = Project(MOP, 2, {"MOP_MEM_MB": "6144"})
    c.expect("Project.of", p, want)
    c.expect("Project.name", p.name, driver.project_of(MOP))
    bare = Project.of(RUGENT)
    c.expect("Project.of without tables", (bare.limit, bare.asks, bare.name),
             (None, {}, "rugent"))
    p.asks["MOP_CORES"] = "1"
    c.check("Project.of must copy the project's asks, not share the table's",
            not (asks["mop"] != {"MOP_MEM_MB": "6144"}))
    c.check("Project must be frozen", frozen(p, "limit"))


def check_puppet_row_204(c):
    """Строка ростера: поля и их порядок -- прежние ключи словаря; словарь
    обратим; незнакомый ключ -- отказ, а не молча лишнее поле."""
    from mop.common.state import PuppetRow
    keys = ["name", "node", "alloc_status", "state", "kind", "owner", "llm",
            "origin", "disk_kb"]
    d = dict(zip(keys, ["pu-mop-1", "mate", "running", "free", "free", "-",
                        "claude", MOP, 12]))
    r = PuppetRow.from_dict(d)
    c.check("PuppetRow.to_dict", not (list(r.to_dict()) != keys or r.to_dict() != d),
            f"got {r.to_dict()!r}, wanted {d!r}")
    try:
        PuppetRow.from_dict(dict(d, alloc="running"))
        c.fail("PuppetRow.from_dict must refuse an unknown key")
    except TypeError:
        pass
    c.check("PuppetRow must be frozen", frozen(r, "state"))


def check_one_verb_204(c):
    """Один Verb на агента и сервис кластера; у агента acting не бывает."""
    from mop.common import domain
    c.check("agent.Verb and cluster.Verb must be domain.Verb",
            agent.Verb is cluster.Verb is domain.Verb)
    fields = [f for f in getattr(domain.Verb, "__dataclass_fields__", {})]
    c.expect("Verb fields", fields, ["fn", "scope", "named", "acting"])
    c.check("no agent verb is acting", not any(v.acting for v in agent.VERBS.values()))
    c.check("Verb must be frozen", not (domain.Verb.__dataclass_params__.frozen is not True))


CHECKS = (check_characterization_204, check_owner_204, check_project_204,
          check_puppet_row_204, check_one_verb_204)


def main():
    if sys.argv[1:] == ["--capture"]:
        with open(SNAPSHOT, "w") as f:
            json.dump(forms(), f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        print(f"captured {SNAPSHOT}")
        return 0
    c = Checks()
    for check in CHECKS:
        try:
            check(c)
        except Exception as e:
            c.fail(check.__name__, f"{type(e).__name__}: {e}")
    return c.report("values")


if __name__ == "__main__":
    sys.exit(main())
