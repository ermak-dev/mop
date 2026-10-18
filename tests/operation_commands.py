#!/usr/bin/env python3
"""Конечные command schemas до DRF/ORM admission (#625).

HYPOTHESIS: произвольные поля owner/shell и неоднозначный JSON попадают
в команды, а plaintext сообщения отражается в ошибках/диагностике.
SOLUTION: строгие конечные схемы restart/send, immutable значения и keyed
canonical intent без самостоятельной авторизации или проверки deadline now.
RESULT: наблюдаемый red отсутствующего API, затем строгие схемы/JSON/HMAC
и прежние lifecycle/request/layers проверки зелёные; DRF/ORM admission отдельно.
STATUS: FIXED — see #625 (только command values/validation).
"""
import dataclasses
import importlib
import importlib.util
import json
import os
import sys
from uuid import UUID

import hermetic  # noqa: F401
from _lib import Checks

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

EPOCH = "d7ee4c63-2a6b-4e6f-a9cd-c2990bf3a05f"
REQUEST = "8e6449b9-65db-4148-aa68-a8b4020bd8b5"
TARGET = "12e6d946-82b4-436c-af2a-f22176bdfb31"
INCARNATION = "bb53a021-cf49-472f-bca2-f11737b24c02"
MESSAGE = "private-test-message-not-in-errors"
KEY = b"private-key-for-intent-digest-only" * 2


def body(**changes):
    result = dict(schema_version=1, epoch=EPOCH, request_id=REQUEST,
                  incarnation=INCARNATION, expected_revision="9007199254740993",
                  not_after="2000-01-01T00:00:00Z", params={"fresh": False})
    result.update(changes)
    return result


def main():
    c = Checks()
    present = importlib.util.find_spec("mop.server.operations.commands") is not None
    if not c.check("finite operation command API exists", present):
        return c.report("operation_commands")
    commands = importlib.import_module("mop.server.operations.commands")

    def refuses(what, fn, code=None):
        try:
            fn()
        except commands.InvalidCommand as error:
            if code is not None:
                c.expect(what, error.code, code)
            else:
                c.check(what, True)
            c.check(f"{what} does not retain raw error context", error.__context__ is None)
            for text in (str(error), repr(error), repr(error.args)):
                c.check(f"{what} redacts diagnostics", MESSAGE not in text and KEY.decode() not in text)
        else:
            c.fail(what, "invalid command accepted")

    parsed = commands.parse_restart(TARGET, body())
    c.expect("restart target", parsed.intent.target_id, UUID(TARGET))
    c.expect("large revision stays exact", parsed.intent.expected_revision, 9007199254740993)
    c.expect("restart mode explicit", parsed.fresh, False)
    c.expect("past deadline parses for request replay", parsed.intent.not_after.year, 2000)
    try:
        parsed.fresh = True
    except dataclasses.FrozenInstanceError:
        c.check("command is immutable", True)
    else:
        c.fail("command is immutable")
    send_body = body(target_id=TARGET, params={"message": MESSAGE, "priority": "later",
                                              "wait_seconds": 2, "notify_when_idle": True})
    sent = commands.parse_send(send_body)
    c.expect("send keeps exact message", sent.message, MESSAGE)
    c.expect("send priority", sent.priority.value, "later")
    c.check("message not in command repr", MESSAGE not in repr(sent))
    send_body["params"]["message"] = "caller mutation"
    c.expect("command owns its values", sent.message, MESSAGE)

    first = commands.intent_digest(parsed, key=KEY)
    c.check("digest canonical hex", len(first) == 64 and all(ch in "0123456789abcdef" for ch in first))
    c.expect("equivalent UUID/time encodings retain intent",
             commands.intent_digest(commands.parse_restart(TARGET.upper(), body(
                 epoch=EPOCH.upper(), not_after="2000-01-01T00:00:00.000000Z")), key=KEY), first)
    # HYPOTHESIS: один Z ошибочно исключает корректный UTC +00:00 из RFC3339.
    # SOLUTION: принять обе записи и нормализовать instant для keyed intent.
    # RESULT: red invalid not_after, затем эквивалентные digests без нового эффекта.
    c.expect("RFC3339 explicit UTC offset is the same intent as Z",
             commands.intent_digest(commands.parse_restart(TARGET, body(
                 not_after="2000-01-01T00:00:00+00:00")), key=KEY), first)
    c.expect("request key is identity, not intent",
             commands.intent_digest(commands.parse_restart(TARGET, body(
                 request_id="00000000-0000-4000-8000-000000000000")), key=KEY), first)
    for other in (commands.parse_restart(TARGET, body(params={"fresh": True})),
                  commands.parse_restart("00000000-0000-4000-8000-000000000001", body()),
                  commands.parse_restart(TARGET, body(expected_revision="1"))):
        c.check("different semantic command changes intent", commands.intent_digest(other, key=KEY) != first)
    c.check("digest key domain remains private", commands.intent_digest(parsed, key=b"z" * 32) != first)

    for field in ("owner", "holder", "_caller", "project", "shell", "argv", "subject", "dispatch"):
        refuses(f"unknown authority/RPC field {field}",
                lambda field=field: commands.parse_restart(TARGET, body(**{field: MESSAGE})), "unknown_fields")
    for changes in (dict(schema_version=True), dict(schema_version=2), dict(epoch=MESSAGE),
                    dict(request_id=MESSAGE), dict(incarnation=MESSAGE), dict(expected_revision=1),
                    dict(expected_revision="01"), dict(expected_revision="-1"),
                    dict(not_after=MESSAGE), dict(not_after="2000-01-01T00:00:00+02:00"),
                    dict(not_after="2000-01-01T00:00:00-00:00"),
                    dict(params={}), dict(params={"fresh": 1}), dict(params={"fresh": False, "force": True})):
        refuses("invalid restart fields", lambda changes=changes: commands.parse_restart(TARGET, body(**changes)))
    for params in ({"message": ""}, {"message": "   "}, {"message": MESSAGE + "\ud800"},
                   {"message": MESSAGE, "priority": "shell"}, {"message": MESSAGE, "wait_seconds": True},
                   {"message": MESSAGE, "notify_when_idle": 1}, {"message": MESSAGE, "owner": "root"}):
        refuses("invalid send values", lambda params=params: commands.parse_send(body(target_id=TARGET, params=params)))
    for value in (None, [], MESSAGE, True):
        refuses("command body must be an object", lambda value=value: commands.parse_send(value))
    for raw in (b'{"schema_version":1,"schema_version":2}', b'{"params":{"fresh":true,"fresh":false}}',
                b'{"x":NaN}', b'{"x":1e999}', MESSAGE.encode(), b'\xff',
                b'[' * 100 + b'0' + b']' * 100):
        refuses("malformed or ambiguous JSON", lambda raw=raw: commands.decode_body(raw))
    refuses("bounded raw command body", lambda: commands.decode_body(b" " * 65537), "body_limit")
    c.expect("strict JSON produces the expected object", commands.decode_body(json.dumps(body()).encode()), body())
    for bad in (None, b"short", bytearray(b"x" * 32)):
        refuses("digest key must be protected key material", lambda bad=bad: commands.intent_digest(parsed, key=bad))
    refuses("direct constructor cannot bypass invariants", lambda: dataclasses.replace(parsed, fresh=1))
    check_create_643(c, commands, refuses)
    return c.report("operation_commands")


# HYPOTHESIS (#643): у журнала нет typed команды создания папета: create не на
# чем принимать, и admission пришлось бы читать target/owner/shell из тела.
# SOLUTION: CreateCommand -- проект из URL, в теле только epoch/request_id/
# not_after и params {node_id, definition_id}; target, инкарнация и владелец
# назначаются сервером, а не запросом.
# STATUS: FIXED — see #643
PROJECT = "5b2f1c1e-35a7-4f0e-8d55-0c1d6b0d7a21"
NODE = "0f6b1a5e-6c2d-4a4b-9a1e-3b8f7d2c4e10"
DEFINITION = "7a1d9c3e-2b4f-4e6a-8c5d-1f0e9b8a7c62"


def create_body(**changes):
    result = dict(schema_version=1, epoch=EPOCH, request_id=REQUEST, not_after="2000-01-01T00:00:00Z",
                  params={"node_id": NODE, "definition_id": DEFINITION})
    result.update(changes)
    return result


def check_create_643(c, commands, refuses):
    if not c.check("#643 a typed create command exists", hasattr(commands, "parse_create")):
        return
    parsed = commands.parse_create(PROJECT, create_body())
    c.expect("#643 create names its project from the route", parsed.intent.project_id, UUID(PROJECT))
    c.expect("#643 create carries the node and the approved definition",
             (parsed.node_id, parsed.definition_id), (UUID(NODE), UUID(DEFINITION)))
    c.expect("#643 create keeps the request key", parsed.intent.request_id, UUID(REQUEST))
    for field in ("target_id", "incarnation", "expected_revision", "owner", "shell", "argv", "spec"):
        refuses(f"#643 create refuses a client-chosen {field}",
                lambda field=field: commands.parse_create(PROJECT, create_body(**{field: MESSAGE})), "unknown_fields")
    for params in ({}, {"node_id": NODE}, {"definition_id": DEFINITION},
                   {"node_id": NODE, "definition_id": DEFINITION, "origin": "git@h:g/x.git"},
                   {"node_id": MESSAGE, "definition_id": DEFINITION},
                   {"node_id": NODE, "definition_id": 1}):
        refuses("#643 create params are exactly node and definition",
                lambda params=params: commands.parse_create(PROJECT, create_body(params=params)))
    for changes in (dict(schema_version=2), dict(epoch=MESSAGE), dict(request_id=MESSAGE),
                    dict(not_after="2000-01-01T00:00:00+02:00")):
        refuses("#643 invalid create intent", lambda changes=changes: commands.parse_create(PROJECT, create_body(**changes)))
    refuses("#643 create project is a UUID", lambda: commands.parse_create(MESSAGE, create_body()))
    try:
        parsed.node_id = UUID(PROJECT)
    except dataclasses.FrozenInstanceError:
        c.check("#643 create command is immutable", True)
    else:
        c.fail("#643 create command is immutable")


if __name__ == "__main__":
    sys.exit(main())
