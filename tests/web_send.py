#!/usr/bin/env python3
"""Доставка GUI через настоящий обработчик воркера, с подменой только шины.

HYPOTHESIS: HTTP принимает send, но библиотечного обработчика у воркера нет.
SOLUTION: общий send_to_puppet, проект из спеки, HTTP-владелец и safety до
доставки; msg_id подтверждает доставку, потерянный ответ означает unknown.
RESULT: проверены настоящий обработчик, HTTP-результат и ограничения шины.
STATUS: FIXED — see #493; с #499 путь -- run воркера напрямую (приём и
статусы -- слой adminapi, проверяется tests/adminapi.py).
"""
import os
import sys
import tempfile

import hermetic  # noqa: F401
from _lib import Checks, patched
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.common import bus, ops, puppets
from mop.server import opworker


def main():
    c = Checks()
    clean = {"present": True, "clone": {"cur": "master", "def": "master", "dirty": 0,
                                        "ahead": 0, "owner": None}}
    target = "pu-untrusted-name-1"

    def run(message="hello", reply=None, facts=clean, origin="git@example.test:g/actual.git",
            role="admin", params=None):
        calls = []

        def transport(node, verb, **fields):
            calls.append((node, verb, fields))
            if verb == "state":
                return facts
            if verb == "send":
                if isinstance(reply, Exception):
                    raise reply
                return {"msg_id": "message-1"} if reply is None else reply
            raise AssertionError(verb)

        with tempfile.TemporaryDirectory() as root, \
                patched(puppets, _cluster=lambda verb, **kw: {"meta": {"origin": origin}},
                        node_of=lambda name: "node-a"), \
                patched(bus, request=transport):
            worker = opworker.Worker()
            got = worker.run("send", target,
                             {"message": message} if params is None else params,
                             login="alice", role=role)
        return got, calls

    got, calls = run()
    c.expect("#493 the real handler confirms a receipt", got.get("status"), "succeeded")
    c.check("#493 public result contains no message",
            "message" not in got and "params" not in got, got)
    sent = [entry for entry in calls if entry[1] == "send"]
    c.check("#493 delivery uses the shared node send once", len(sent) == 1, calls)
    if sent:
        node, verb, fields = sent[0]
        c.expect("#493 target project comes from origin, not target prefix",
                 fields.get("project"), "actual")
        c.expect("#493 authenticated HTTP actor owns the message", fields.get("owner"), "alice")
        c.expect("#493 no force in a web send", fields.get("force"), False)
        c.expect("#493 exact target and body reach the node",
                 (node, fields.get("name"), fields.get("message")),
                 ("node-a", target, "hello"))
        c.check("#493 web does not invent a reply inbox",
                fields.get("from_name", "mop") == "mop", fields)
    c.check("#493 clone is inspected before delivery",
            [v for _, v, _ in calls] == ["state", "send"], calls)

    for name, facts in (("unknown clone", {}), ("agent error", {"error": "silent"}),
                        ("dirty clone", {"clone": dict(clean["clone"], dirty=1)}),
                        ("foreign lease", {"clone": dict(clean["clone"],
                                                         owner={"user": "bob", "at": 9999999999})})):
        got, calls = run(facts=facts)
        c.check(f"#493 {name} refuses before send", got.get("status") == "refused"
                and not any(v == "send" for _, v, _ in calls), (got, calls))
    for origin in (None, "git@example.test:g/admin.git", "/"):
        got, calls = run(origin=origin)
        c.check(f"#493 missing or privileged project {origin!r} fails closed",
                got.get("status") == "refused" and not calls, (got, calls))
    got, calls = run(role="user")
    c.check("#493 non-admin cannot send", got.get("status") == "refused" and not calls,
            (got, calls))
    for message in (None, "", "   ", 123):
        got, calls = run(message=message)
        c.check(f"#493 invalid message {message!r} has no node effect",
                got.get("status") == "refused" and not calls, (got, calls))
    got, calls = run(params={"message": "hello", "force": True})
    c.check("#493 extra params cannot expand send authority",
            got.get("status") == "refused" and not calls, (got, calls))

    for reply, status in (({"error": "held by bob"}, "refused"), ({}, "unknown"),
                          ({"msg_id": ""}, "unknown"),
                          (bus.BusError("timed out after delivery"), "unknown")):
        got, calls = run(reply=reply)
        c.expect(f"#493 receipt {reply!r} maps to {status}", got.get("status"), status)
        c.expect("#493 uncertain delivery is never replayed",
                 sum(v == "send" for _, v, _ in calls), 1)
    # HYPOTHESIS: общие административные ворота запрещают владельцу
    # продолжать работу через send, хотя узловая аренда его пропускает.
    # SOLUTION: send явно разрешает продолжение своей известной работы;
    # общие ворота для остальных мутаций остаются закрыты.
    # RESULT: RED -- 7 отказов вместо доставки; GREEN -- матрица владельцев.
    # STATUS: FIXED — see #595
    mine = {"user": "alice", "at": 1}
    foreign = {"user": "bob", "at": 1}
    for work in ({"dirty": 1}, {"ahead": 1}, {"dirty": 2, "ahead": 3}):
        owned = {"present": True, "session": "busy 1 1",
                 "clone": dict(clean["clone"], owner=mine, **work)}
        got, calls = run(facts=owned)
        c.check(f"#595 owned follow-up with {work} reaches delivery once",
                got.get("status") == "succeeded"
                and [v for _, v, _ in calls] == ["state", "send"], (got, calls))
        sent = [fields for _, verb, fields in calls if verb == "send"]
        c.check(f"#595 owned follow-up with {work} keeps principal and no force",
                len(sent) == 1 and sent[0].get("owner") == "alice"
                and sent[0].get("force") is False
                and sent[0].get("project") == "actual", calls)
        for disruptive in (False, True):
            safe, why = ops.safety(target, "actual", owner="alice",
                                   facts=owned, disruptive=disruptive)
            c.check(f"#595 administrative safety still refuses {work}, disruptive={disruptive}",
                    not safe and bool(why), (safe, why))
        for owner in (None, foreign):
            rejected = dict(owned, clone=dict(owned["clone"], owner=owner))
            got, calls = run(facts=rejected)
            c.check(f"#595 work {work} owned by {owner} cannot be taken over",
                    got.get("status") == "refused"
                    and not any(v == "send" for _, v, _ in calls), (got, calls))

    unknown = {"clone": dict(clean["clone"], owner=mine, dirty=None, ahead=None)}
    for facts in (None, {}, {"error": "silent"}, unknown):
        got, calls = run(facts=facts)
        c.check(f"#595 owned continuation does not bypass unknown facts {facts}",
                got.get("status") == "refused"
                and not any(v == "send" for _, v, _ in calls), (got, calls))

    owned = {"clone": dict(clean["clone"], owner=mine, dirty=1)}
    for extra in ({"owner": "alice"}, {"project": "actual"},
                  {"continuation": True}, {"force": True}):
        got, calls = run(facts={"clone": dict(owned["clone"], owner=foreign)},
                         params={"message": "hello", **extra})
        c.check(f"#595 payload {extra} cannot grant follow-up authority",
                got.get("status") == "refused" and not calls, (got, calls))
    got, calls = run(facts=owned, reply=bus.BusError("timed out after delivery"))
    c.check("#595 an uncertain owned follow-up is not replayed",
            got.get("status") == "unknown"
            and sum(v == "send" for _, v, _ in calls) == 1, (got, calls))
    return c.report("web_send")


if __name__ == "__main__":
    sys.exit(main())
