#!/usr/bin/env python3
"""Остановка задания без удаления тела и bootstrap (#608).

HYPOTHESIS: остановка allocation создаёт следующий запуск, а GUI не имеет
операторского stop-job для pending/starting/failing с неизвестным клоном.
SOLUTION: общий stop workflow и операторский job_stop без purge; allocation-stop
не меняется, GUI подтверждает последствия до запроса.
RESULT: отрицательные проверки до реализации, затем общие CLI/HTTP/authorization
и транспортные проверки проходят.
STATUS: FIXED — see #608
"""
import os
import sys

import hermetic  # noqa: F401
from _lib import Checks, patched
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.common import bus, ops
from mop.server import cluster, opworker


class Nomad:
    def __init__(self, job):
        self.job = job
        self.calls = []

    def get_job(self, name):
        self.calls.append(("get_job", name))
        return self.job

    def deregister(self, name, purge=True):
        self.calls.append(("deregister", name, purge))
        self.job = {**self.job, "Stop": True}


def main():
    c = Checks()
    c.check("#608 shared stop workflow exists", callable(getattr(ops, "stop", None)))
    c.check("#608 operator stop-job verb is registered", "job_stop" in cluster.VERBS)
    if "job_stop" in cluster.VERBS:
        row = cluster.VERBS["job_stop"]
        c.check("#608 stop-job is operator-only and names an existing job",
                row.scope == cluster.ADMIN and row.named and row.acting)
        c.check("#608 project identity cannot stop a job without operator authority",
                cluster.refusal("mop", "job_stop", origin="git@example.test:mop.git",
                                name="pu-mop-1", job_exists=True) is not None)
        for status in ("pending", "running", "dead"):
            api = Nomad({"ID": "pu-mop-1", "Status": status,
                         "Meta": {"origin": "git@example.test:mop.git"}})
            with cluster.using(api), patched(cluster.bootstrap, store=lambda *a: c.check(
                    "#608 stop does not erase bootstrap", False)):
                got = row.fn(bus.ADMIN, {"name": "pu-mop-1"})
            c.check(f"#608 {status} stops the job without allocation or node access",
                    got.get("ok") and got.get("stopped") is True
                    and api.calls == [("get_job", "pu-mop-1"),
                                      ("deregister", "pu-mop-1", False)])
        api = Nomad({"ID": "pu-mop-1", "Meta": {"origin": "git@example.test:mop.git"}})
        with cluster.using(api), patched(cluster, _owner=lambda n: ("git@example.test:mop.git", True)):
            got = cluster.answer("mop", {"verb": "job_stop", "name": "pu-mop-1"})
        c.check("#608 project bus requests are refused at the real dispatcher",
                bool(got.get("error")) and not api.calls)
        with cluster.using(api), patched(cluster, _owner=lambda n: ("git@example.test:mop.git", True),
                                         _clone_of=lambda n: c.check("#608 operator stop needs no clone", False)):
            got = cluster.answer(bus.ADMIN, {"verb": "job_stop", "name": "pu-mop-1"})
        c.check("#608 authorized dispatcher stops the exact retained job",
                got.get("stopped") is True
                and api.calls == [("get_job", "pu-mop-1"), ("deregister", "pu-mop-1", False)])
        for job in (None, {"ID": "pu-mop-1", "Meta": {}}):
            api = Nomad(job)
            with cluster.using(api):
                got = row.fn(bus.ADMIN, {"name": "pu-mop-1"})
            c.check("#608 absent or unmarked target is refused before mutation",
                    bool(got.get("error")) and api.calls == [("get_job", "pu-mop-1")])
    if callable(getattr(ops, "stop", None)):
        calls = []
        def transport(verb, **fields):
            calls.append((verb, fields))
            return {"ok": True, "name": fields["name"], "stopped": True,
                    "project": "mop"}
        result = ops.stop(transport, "pu-mop-1", login="anton")
        c.check("#608 stop invokes the shared stop-job transport only",
                calls == [("job_stop", {"name": "pu-mop-1", "owner": "anton"})]
                and result.get("outcome") == ops.REGISTERED
                and result.get("project") == "mop")
        def lost(*args, **fields):
            raise TimeoutError("response lost")
        result = ops.stop(lost, "pu-mop-1", login="anton")
        c.check("#608 an unconfirmed stop is unknown, never auto-replayed",
                result.get("outcome") == ops.UNKNOWN)
        result = ops.stop(lambda *a, **k: {}, "pu-mop-1", login="anton")
        c.check("#608 an empty answer does not claim a stop",
                result.get("outcome") == ops.UNKNOWN)
        def refused(*args, **fields):
            raise bus.Refused("operator required")
        result = ops.stop(refused, "pu-mop-1", login="anton")
        c.check("#608 a confirmed stop refusal preserves the reason",
                result.get("outcome") == ops.REFUSED
                and result.get("error") == "operator required")
        worker = opworker.Worker()
        result = worker.run("stop", "pu-mop-1", {}, "anton", transport=transport)
        c.check("#608 web stop uses the same workflow without clean-clone safety",
                result.get("status") == "succeeded")
        before = len(calls)
        result = worker.run("stop", "pu-mop-1", {}, "eve", role="master",
                            transport=transport)
        c.check("#608 a non-admin web caller cannot stop a puppet",
                result.get("status") == "refused" and len(calls) == before)
        result = worker.run("stop", "pu-mop-1", {"force": True}, "anton",
                            transport=transport)
        c.check("#608 web stop does not accept force or hidden parameters",
                result.get("status") == "refused" and len(calls) == before)
    from mop.cli.server import web as webmod
    http_calls = []
    def web_transport(verb, project=None, **fields):
        http_calls.append((verb, project, fields))
        return {"ok": True, "stopped": True, "project": "mop"}
    with patched(webmod, WORKER=opworker.Worker()), \
            patched(bus, call_cluster=web_transport,
                    request=lambda *a, **k: c.check("#608 stop does not inspect or mutate the clone", False)):
        result = webmod._execute("stop", "pu-mop-1", {}, ("anton", "admin"))()
    c.check("#608 assembled HTTP executor stops without allocation or clone preflight",
            result.get("status") == "succeeded"
            and http_calls == [("job_stop", bus.ADMIN,
                                {"name": "pu-mop-1", "owner": "anton"})])
    from mop.cli.core import stop as command
    from _lib import run_command
    cli_calls = []
    with patched(command.lib.config, require=lambda *a: None), \
            patched(bus, login=lambda: "anton", call_cluster=lambda verb, **fields:
                    cli_calls.append((verb, fields)) or {"stopped": True}):
        out, err, code = run_command(command.main, ["pu-mop-1"])
        c.check("#608 CLI uses the operator stop-job workflow silently",
                code == 0 and not out and not err
                and cli_calls == [("job_stop", {"project": bus.ADMIN,
                                               "name": "pu-mop-1", "owner": "anton"})])
        before = len(cli_calls)
        _, _, code = run_command(command.main, ["pu-mop-1", "--force"])
        c.check("#608 CLI rejects implicit force without any RPC",
                code != 0 and len(cli_calls) == before)
    c.check("#608 MCP is declared by the stop commandlet without force",
            [a["name"] for a in command.MCP["args"]] == ["name"])
    def call(verb, **fields):
        return {"verb": verb, **fields}
    with patched(opworker.bus, call_cluster=call):
        got = opworker.default_transport(lambda n: "mop")("job_stop", name="pu-mop-1")
    c.check("#608 privileged stop reaches only the operator subject",
            got.get("project") == bus.ADMIN)
    return c.report("stop")


if __name__ == "__main__":
    sys.exit(main())
