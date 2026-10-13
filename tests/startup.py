#!/usr/bin/env python3
"""Приватный ход инициализации до tmux, без пула (#612)."""
import dataclasses
import json
import os
import sys
import tempfile
import types

import hermetic  # noqa: F401,E402
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus, puppets  # noqa: E402
from mop.server import bootstrap, cluster, websession  # noqa: E402
from mop.cli.server import web as webcli  # noqa: E402
import web as webchecks  # noqa: E402


# HYPOTHESIS: отсутствие ещё не созданного tmux ошибочно превращается в 502;
# bootstrap прячет ход subprocess до его завершения.
# SOLUTION: приватная проекция стадий доступна во время прогона и связана с
# текущей аллокацией/попыткой; stdout обёртки и задачи bootstrap читает один
# read-only глагол, а экран предпочитает появившийся tmux.
# RESULT: HTTP/CLI/progress были красными; дополнительно воспроизведены
# устаревшая попытка, несопоставимые часы, утечка raw exception через RPC
# и неверная структура приватного ответа. Узкие проверки теперь зелёные.
# STATUS: FIXED — see #612
def check_screen(c):
    store = websession.SessionStore(now=lambda: 1000.0)
    admin = store.open(webchecks.identity.Identity("anton", "admin", ()))
    user = store.open(webchecks.identity.Identity("olga", "user", ()))
    row = dataclasses.replace(webchecks.CARD_ROW, kind="starting", state="starting")
    calls = []
    mode = {"pane": False, "broken": False, "refused": False, "malformed": False}

    def pane(node, name):
        calls.append(("pane", node, name))
        if not mode["pane"]:
            raise RuntimeError("no tmux session")
        return ["Claude ready"]

    def startup(name):
        calls.append(("startup", name))
        if mode["malformed"]:
            return {"lines": ["safe stage"], "note": {"password": "PRIVATE-NOTE"}}
        if mode["refused"]:
            raise bus.Refused("initialization log unavailable: Nomad client is unreachable")
        if mode["broken"]:
            raise RuntimeError("PRIVATE-TOKEN /private/error.env")
        return {"lines": ["body ready", "TASK [Configure database]", "MOP_PROXY_KEY=hush-hush"],
                "truncated": False, "note": None}

    def get(name="pu-mop-1", token=admin):
        return webchecks._fake_handler(webcli, "GET", f"/api/puppet/{name}/tail",
            headers={"Cookie": f"mop_session={token}"} if token else None)

    with patched(webcli, SESSIONS=store, COLLECTOR=webchecks._card_collector([row])), \
         patched(websession, provider=lambda: webchecks.FakeProvider()), \
         patched(webcli.config, get=lambda key, default=None: {
             "MOP_SERVER_LAN": "10.0.0.1", "MOP_HTTPS_PORT": "443"}.get(key, default)), \
         patched(puppets, pane_lines=pane, node_of=lambda name: "mate", startup_lines=startup):
        headers, raw = get()
        data = json.loads(raw)
        c.expect("#612 starting screen returns its initialization log, not 502", headers[0][1], 200)
        c.expect("#612 startup source is explicit", data.get("source"), "initialization")
        c.check("#612 actual initialization stages reach the screen",
                "TASK [Configure database]" in data.get("lines", []), data)
        c.check("#612 startup secrets do not reach browser", "hush-hush" not in raw.decode(), raw)
        c.check("#612 initialization read carries no-store",
                ("Cache-Control", "no-store") in headers, headers)
        with patched(webcli, COLLECTOR=webchecks._card_collector([
                dataclasses.replace(row, kind="failing", alloc_status="failing")])):
            headers, raw = get()
            c.expect("#612 initialization failures keep their process log visible",
                     (headers[0][1], json.loads(raw).get("source")), (200, "initialization"))
        mode["pane"] = True
        calls.clear()
        headers, raw = get()
        data = json.loads(raw)
        c.expect("#612 next poll prefers a newly available live screen", data.get("source"), "screen")
        c.expect("#612 live screen lines replace initialization", data.get("lines"), ["Claude ready"])
        c.check("#612 live screen needs no startup log lookup", not any(x[0] == "startup" for x in calls), calls)
        mode["pane"] = False
        mode["broken"] = True
        headers, raw = get()
        c.expect("#612 a genuinely failed initialization reader is an error", headers[0][1], 502)
        c.check("#612 raw reader exceptions are not published", "PRIVATE-TOKEN" not in raw.decode(), raw)
        mode["refused"] = True
        headers, raw = get()
        c.expect("#612 an already safe library refusal keeps its reason",
                 json.loads(raw).get("error"), "initialization log unavailable: Nomad client is unreachable")
        c.expect("#612 a private log refusal names its source", json.loads(raw).get("source"), "initialization")
        mode["malformed"] = True
        headers, raw = get()
        c.expect("#612 unexpected note structure is not published as process data", headers[0][1], 502)
        c.check("#612 credentials in a malformed note stay private", "PRIVATE-NOTE" not in raw.decode(), raw)
        for label, token, code in (("guest", None, 401), ("user", user, 403)):
            calls.clear()
            headers, raw = get(token=token)
            c.expect(f"#612 {label} cannot read process log", headers[0][1], code)
            c.expect(f"#612 {label} touches no process reader", calls, [])
        calls.clear()
        headers, raw = get("../../private")
        c.expect("#612 unknown target is refused before any reader", (headers[0][1], calls), (404, []))


def check_bootstrap_progress(c):
    reader = getattr(bootstrap, "read_progress", None)
    if not c.check("#612 bootstrap progress is readable while the play is running", callable(reader)):
        return
    root = tempfile.mkdtemp(prefix="startup-progress-")
    name = "pu-mop-1"
    bootstrap.store(root, name, "- name: play\n  tasks:\n    - name: Install\n      ansible.builtin.command: true\n")
    observed = []

    def run(cmd, **kwargs):
        out = kwargs.get("stdout")
        if not c.check("#612 bootstrap writes into a private live log, not capture_output", hasattr(out, "write")):
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        out.write("PLAY [Bootstrap]\nTASK [Install packages] ************************\nchanged: [body]\n")
        out.write('stdout: PRIVATE-VALUE\nfatal: [body]: FAILED! => {"password":"PRIVATE-VALUE"}\n')
        out.flush()
        current = reader(root, name, "alloc-a")
        observed.append(current)
        c.expect("#612 live progress belongs to its allocation", current.get("alloc"), "alloc-a")
        c.expect("#612 progress is running before subprocess returns", current.get("running"), True)
        c.expect("#612 attempt time comes from the node clock, not the controller clock", current.get("at"), 1234.5)
        c.check("#612 task appears before the process is complete", "TASK [Install packages]" in current.get("lines", []), current)
        c.check("#612 arbitrary stdout and Ansible result values stay private", "PRIVATE-VALUE" not in json.dumps(current), current)
        c.expect("#612 a previous allocation's log is not shown as current", reader(root, name, "alloc-b").get("lines"), [])
        return types.SimpleNamespace(returncode=0)

    with patched(bootstrap, ROOT=root), patched(bootstrap.playvars, playbook_vars=lambda: {}), \
         patched(bootstrap, argv=lambda *args: ["fake-bootstrap"]), patched(bootstrap.subprocess, run=run):
        result = bootstrap.play({"name": name, "address": "body", "alloc": "alloc-a", "started_at": 1234.5}, "mop")
    c.expect("#612 progress capture preserves successful bootstrap semantics", result.get("ok"), True)
    c.check("#612 the live subprocess seam was exercised", bool(observed))
    c.expect("#612 completed process no longer marked running", reader(root, name, "alloc-a").get("running"), False)
    progress_path = os.path.join(root, f"{name}-progress.log")
    c.expect("#612 private process output file mode", os.stat(progress_path).st_mode & 0o777, 0o600)
    bootstrap.store(root, name, "")
    c.expect("#612 deleting workspace removes its private progress", reader(root, name, "alloc-a").get("lines"), [])


def check_cluster_log(c):
    verb = cluster.VERBS.get("startup")
    if not c.check("#612 a shared read-only startup verb exists", verb is not None):
        return
    c.check("#612 startup log is operator-only and nonmutating",
            verb.scope == cluster.ADMIN and verb.named and not verb.acting)
    alloc = {"ID": "alloc-a", "NodeName": "mate", "ClientStatus": "running",
             "TaskStates": {"claude": {"State": "running", "Events": [
                 {"Type": "Started", "Time": 100_000_000_000}]}}}
    calls = []
    api = types.SimpleNamespace(latest_alloc=lambda name: alloc,
        alloc_stdout=lambda aid, task, tail=8000: calls.append(("stdout", aid, task)) or "pu-mop-1: body ready\npu-mop-1: MOP_PROXY_KEY=secret-value\n",
        alloc_stderr=lambda aid, task, tail=8000: calls.append(("stderr", aid, task)) or "Authorization: Bearer hidden-token\n")
    with cluster.using(api), patched(bootstrap, read_progress=lambda root, name, aid: {
            "lines": ["TASK [Configure database]"], "running": True, "alloc": aid,
            "at": 110, "truncated": False}):
        data = verb.fn(bus.ADMIN, {"name": "pu-mop-1", "lines": 100})
    c.check("#612 wrapper and bootstrap logs share a single confirmed allocation",
            calls == [("stdout", "alloc-a", "claude"), ("stderr", "alloc-a", "claude")], calls)
    c.check("#612 wrapper and bootstrap are both in the combined process log",
            "pu-mop-1: body ready" in data.get("lines", [])
            and "TASK [Configure database]" in data.get("lines", []), data)
    c.check("#612 redaction preserves credential context instead of hiding all wrapper output",
            "pu-mop-1: MOP_PROXY_KEY=[redacted]" in data.get("lines", []), data)
    c.check("#612 raw credentials are redacted before CLI or HTTP consume the log",
            all(x not in json.dumps(data) for x in ("secret-value", "hidden-token")), data)
    alloc["TaskStates"]["claude"]["Events"] = []
    with cluster.using(api), patched(bootstrap, read_progress=lambda root, name, aid: {
            "lines": ["TASK [Unmatched attempt]"], "running": True, "alloc": aid,
            "at": 110, "truncated": False}):
        unknown = verb.fn(bus.ADMIN, {"name": "pu-mop-1"})
    c.check("#612 missing task start time does not validate a current bootstrap attempt",
            "TASK [Unmatched attempt]" not in unknown.get("lines", []), unknown)
    alloc["TaskStates"]["claude"]["Events"] = [{"Type": "Started", "Time": 200_000_000_000}]
    with cluster.using(api), patched(bootstrap, read_progress=lambda root, name, aid: {
            "lines": ["TASK [Previous attempt]"], "running": False, "alloc": aid,
            "at": 100, "truncated": False}):
        retry = verb.fn(bus.ADMIN, {"name": "pu-mop-1"})
    c.check("#612 a new attempt in the same allocation does not show the previous bootstrap as current",
            "TASK [Previous attempt]" not in retry.get("lines", []), retry)
    for at in (None, True, "yesterday", float("nan"), float("inf")):
        with cluster.using(api), patched(bootstrap, read_progress=lambda root, name, aid, at=at: {
                "lines": ["TASK [Unverified attempt]"], "running": True,
                "alloc": aid, "at": at, "truncated": False}):
            unverified = verb.fn(bus.ADMIN, {"name": "pu-mop-1"})
        c.check(f"#612 invalid attempt time {at!r} does not validate a progress record",
                "TASK [Unverified attempt]" not in unverified.get("lines", []), unverified)
    with cluster.using(types.SimpleNamespace(latest_alloc=lambda name: None)):
        pending = verb.fn(bus.ADMIN, {"name": "pu-mop-1"})
    c.expect("#612 no allocation has no invented process output", pending.get("lines"), [])
    c.check("#612 no allocation describes real placement wait", "placement" in (pending.get("note") or ""), pending)
    api.alloc_stderr = lambda *args: "Warning: Permanently added body identity\n"
    with cluster.using(api), patched(bootstrap, read_progress=lambda *args: {
            "lines": [], "running": False, "alloc": "alloc-a", "at": 250, "truncated": False}):
        diagnostic = verb.fn(bus.ADMIN, {"name": "pu-mop-1"})
    c.check("#612 stderr alone is not proof the initialization failed",
            "reported an error" not in json.dumps(diagnostic), diagnostic)
    api.latest_alloc = lambda name: (_ for _ in ()).throw(RuntimeError("PRIVATE-TOKEN in Nomad read failure"))
    with cluster.using(api), patched(cluster, _owner=lambda name: ("git@example.test:mop.git", True)):
        refusal = cluster.answer(bus.ADMIN, {"verb": "startup", "name": "pu-mop-1"})
    c.check("#612 raw I/O exceptions never gain public trust by crossing the RPC boundary",
            "PRIVATE-TOKEN" not in json.dumps(refusal), refusal)
    c.check("#612 a read failure identifies its safe failed stage",
            "allocation lookup" in refusal.get("error", ""), refusal)
    with cluster.using(api):
        try:
            data = verb.fn(bus.ADMIN, {"name": "pu-mop-1"})
        except RuntimeError:
            pass
        else:
            c.check("#612 Nomad read failure is not mistaken for no allocation", bool(data.get("error")), data)


def check_cli_reader(c):
    fn = getattr(puppets, "startup_lines", None)
    if not c.check("#612 startup reader is shared with the commandlet", callable(fn)):
        return
    seen = []
    with patched(puppets, _cluster=lambda verb, **kw: seen.append((verb, kw)) or {"lines": ["startup"]}):
        c.expect("#612 library returns startup data without printing", fn("pu-mop-1").get("lines"), ["startup"])
    c.expect("#612 library uses the operator read-only protocol", seen,
             [("startup", {"project": bus.ADMIN, "name": "pu-mop-1", "lines": 500})])
    from mop.cli.core import tail
    from _lib import run_command
    seen.clear()
    with patched(tail.lib.config, require=lambda *keys: None), \
         patched(tail.lib, guard=lambda name: {}), \
         patched(puppets, startup_lines=lambda name, lines=500: seen.append((name, lines)) or {
             "lines": ["TASK [Configure database]"], "note": None}):
        out, err, code = run_command(tail.main, ["pu-mop-1", "--startup", "-n", "12"])
    c.expect("#612 CLI uses the same startup reader", seen, [("pu-mop-1", 12)])
    c.expect("#612 CLI prints the actual private initialization stages",
             (out, code), ("TASK [Configure database]\n", 0))


def check_nomad_stdout(c):
    from mop.server import nomad
    reader = getattr(nomad, "alloc_stdout", None)
    if not c.check("#612 bounded stdout reader exists on the Nomad interface", callable(reader)):
        return
    calls = []
    with patched(nomad, _raw=lambda method, path, **kw: calls.append((method, path, kw))
                 or types.SimpleNamespace(content="готово\n".encode())):
        c.expect("#612 stdout retains UTF-8", reader("alloc-a", "claude", tail=1234), "готово\n")
    c.expect("#612 stdout uses only the allocation log endpoint and bounded end-offset",
             calls, [("GET", "/v1/client/fs/logs/alloc-a", {"params": {
                 "task": "claude", "type": "stdout", "origin": "end", "offset": 1234, "plain": "true"}})])


def main():
    c = Checks()
    check_nomad_stdout(c)
    check_screen(c)
    check_bootstrap_progress(c)
    check_cluster_log(c)
    check_cli_reader(c)
    return c.report("startup")


if __name__ == "__main__":
    sys.exit(main())
