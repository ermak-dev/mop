#!/usr/bin/env python3
"""История живой сессии без пула (#597).

HYPOTHESIS: экран терминала не является транскриптом, а путь из запроса
позволяет читать чужие файлы. История должна читать только файл живой
идентичности и переносить объекты JSON без интерпретации состояния tools.
SOLUTION: ограниченные страницы JSONL, курсор с идентичностью и byte offset,
отдельные ворота проекта на узле. Входы здесь синтетические; живой контракт
и его поля принимает #596, это не проверка работающего Claude Code.
RESULT: проверки обнаружили разрыв страницы, отсутствие process-start fence,
перезапись snapshot и эхо приватного stdout; каждое исправление проверено
сначала красным тестом. Незаконченная строка не двигает обычный cursor,
слишком длинная пропускается с notice, ответ перепроверяется перед выдачей.
STATUS: FIXED — see #597
"""
import asyncio
import base64
import contextlib
import hashlib
import json
import re
import os
import shlex
import sys
import tempfile

import hermetic  # noqa: F401,E402
from _lib import Checks, Msg, patched, patched_env, run_command
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop import session
from mop.node import agent

SID = "12345678-1234-1234-1234-123456789abc"
OTHER = "22345678-1234-1234-1234-123456789abc"


@contextlib.contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as root:
        cwd = os.path.join(root, "work.with_under-score")
        os.mkdir(cwd)
        config = os.path.join(root, "config")
        os.mkdir(config)
        identity = {"session_id": SID, "pid": 123, "start": "boot:456",
                    "config": config, "project_dir": "-work", "cwd": cwd,
                    "socket": [1, 2]}
        project = os.path.join(config, "projects", "-work")
        os.makedirs(project)
        path = os.path.join(project, SID + ".jsonl")
        with open(path, "wb"):
            pass
        with patched(session, _history_identity=lambda target: dict(identity)):
            yield cwd, path, identity


def put(path, records):
    with open(path, "wb") as f:
        for d in records:
            f.write(json.dumps(d, ensure_ascii=False).encode() + b"\n")


def refused(c, label, fn):
    try:
        fn()
    except (ValueError, LookupError, OSError, RuntimeError):
        c.check(label, True)
    else:
        c.fail(label)


def check_history_preserves_unknown_tools_meta_and_reconnect(c):
    with fixture() as (cwd, path, identity):
        want = [{"type": kind, "uuid": str(i), "arbitrary": {"text": "Русский", "n": None}}
                for i, kind in enumerate(("user", "assistant", "tool", "subagent",
                                          "system", "compact_boundary", "future-kind"))]
        put(path, want)
        out = session.history(cwd)
        c.expect("original JSON objects, including unknown kinds", out["records"], want)
        c.expect("exact history response contract", set(out),
                 {"epoch", "session_id", "cursor", "records", "reset", "has_more", "partial"})
        c.check("first request has no reset or partial", not out["reset"] and not out["partial"])
        c.expect("session id", out["session_id"], SID)
        c.check("bounded opaque cursor", isinstance(out["cursor"], str) and len(out["cursor"]) <= 2048)
        quiet = session.history(cwd, out["cursor"])
        c.expect("reconnect does not duplicate records", quiet["records"], [])
        c.expect("same epoch across reconnect", quiet["epoch"], out["epoch"])
        more = {"type": "assistant", "uuid": "append", "message": {"tool_use": []}}
        with open(path, "ab") as f:
            f.write(json.dumps(more).encode() + b"\n")
        got = session.history(cwd, out["cursor"])
        c.expect("append preserves epoch", got["epoch"], out["epoch"])
        c.expect("append returned once", got["records"], [more])
        c.expect("replayed request is deterministic", session.history(cwd, out["cursor"]), got)
        c.expect("read logs no transcript", run_command(session.main, ["history", cwd])[1], "")


def check_partial_utf8_and_final_line_are_not_consumed(c):
    with fixture() as (cwd, path, _identity):
        line = json.dumps({"type": "user", "text": "Ж"}, ensure_ascii=False).encode()
        split = line.index("Ж".encode()) + 1
        with open(path, "wb") as f:
            f.write(b'{"type":"meta"}\n' + line[:split])
        out = session.history(cwd)
        c.expect("only complete lines parsed", out["records"], [{"type": "meta"}])
        c.check("unfinished UTF8 reports partial, not more ready data", out["partial"] and not out["has_more"])
        with open(path, "ab") as f:
            f.write(line[split:])
        waiting = session.history(cwd, out["cursor"])
        c.expect("JSON without newline remains pending", waiting["records"], [])
        c.check("final line pending", waiting["partial"])
        with open(path, "ab") as f:
            f.write(b"\n")
        got = session.history(cwd, waiting["cursor"])
        c.expect("UTF8 append is lossless", got["records"], [{"type": "user", "text": "Ж"}])
        c.check("append of partial line is not reset", not got["reset"])


def check_stale_epoch_rotation_truncation_and_regrowth(c):
    with fixture() as (cwd, path, identity):
        put(path, [{"type": "old", "padding": "a" * 200}])
        first = session.history(cwd)
        identity["start"] = "boot:457"
        restart = session.history(cwd, first["cursor"])
        c.check("process restart resets epoch", restart["reset"] and restart["epoch"] != first["epoch"])
        identity["session_id"] = OTHER
        path2 = os.path.join(os.path.dirname(path), OTHER + ".jsonl")
        put(path2, [{"type": "new-session"}])
        changed = session.history(cwd, restart["cursor"])
        c.expect("session switch reads new session only", changed["records"], [{"type": "new-session"}])
        c.check("session switch resets", changed["reset"])
        os.rename(path2, path2 + ".old")
        put(path2, [{"type": "rotated"}])
        rotated = session.history(cwd, changed["cursor"])
        c.check("file replacement resets", rotated["reset"])
        put(path2, [{"type": "x"}])
        truncated = session.history(cwd, rotated["cursor"])
        c.check("truncation resets epoch", truncated["reset"] and truncated["epoch"] != rotated["epoch"])
        c.expect("truncation starts at zero", truncated["records"], [{"type": "x"}])
        put(path2, [{"type": "regrown", "padding": "b" * 400}])
        regrown = session.history(cwd, truncated["cursor"])
        c.check("truncate and regrow detected by byte fences", regrown["reset"])
        c.check("new generation stays stable", not session.history(cwd, regrown["cursor"])["reset"])
        put(path2, [{"type": "regrown", "padding": "b" * 400}])
        identical_rewrite = session.history(cwd, regrown["cursor"])
        c.check("same-size truncate and rewrite resets even with identical bytes", identical_rewrite["reset"])


def check_limits_escaping_and_giant_record_progress(c):
    with fixture() as (cwd, path, _identity):
        want = [{"type": "unknown", "i": i, "text": "\x00Ж\\\"" * 500} for i in range(100)]
        # DEL занимает один byte в UTF-8, шесть при ensure_ascii: худшее
        # расширение, которое заставляет ограничивать не только исходник.
        want += [{"type": "escape-boundary", "i": i, "text": "\x7f" * 25_000} for i in range(4)]
        put(path, want)
        cursor, collected, epochs = None, [], set()
        for _ in range(150):
            out = session.history(cwd, cursor)
            c.check("wire JSON well below 900KB, even with ASCII escaping",
                    len(json.dumps(out, ensure_ascii=True).encode()) < 300_000)
            collected.extend(out["records"])
            epochs.add(out["epoch"])
            cursor = out["cursor"]
            if not out["has_more"]:
                break
        c.check("chunk paging loses and duplicates nothing", collected == want,
                f"got {len(collected)} records, wanted {len(want)}")
        c.expect("paging epoch stable", len(epochs), 1)
        with open(path, "wb") as f:
            f.write(b'{"type":"giant","text":"' + b"x" * 500_000 + b'"}\n{"type":"after"}\n')
        cursor, notices, after = None, [], []
        for _ in range(20):
            out = session.history(cwd, cursor)
            c.check("giant paging is bounded", len(json.dumps(out).encode()) < 300_000)
            notices.extend(d for d in out["records"] if d.get("type") == "mop_history_notice")
            after.extend(d for d in out["records"] if d.get("type") == "after")
            c.check("giant skip always advances", cursor != out["cursor"] or not out["has_more"])
            cursor = out["cursor"]
            if not out["has_more"]:
                break
        c.check("giant omission is explicit", bool(notices) and all(d["reason"] == "record_too_large" for d in notices))
        c.expect("giant does not trap following records", after, [{"type": "after"}])


def check_hostile_cursor_and_incompatible_json_fail_closed(c):
    with fixture() as (cwd, path, _identity):
        for cursor in ("", "../secrets", "a" * 2049, 1, {}, "W10", "e30", "!!!!"):
            refused(c, f"hostile cursor {type(cursor).__name__} refused", lambda: session.history(cwd, cursor))
        for raw in (b"[]\n", b"null\n", b"not json\n", b'{"x":NaN}\n', b'{"x":1,"x":2}\n', b'{"x":"\xff"}\n'):
            with open(path, "wb") as f:
                f.write(raw)
            refused(c, "incompatible complete JSON fails closed", lambda: session.history(cwd))
        put(path, [{"type": "valid"}])
        out = session.history(cwd)
        token = json.loads(base64.urlsafe_b64decode(out["cursor"] + "=" * (-len(out["cursor"]) % 4)))
        token["offset"] = -1
        hostile = base64.urlsafe_b64encode(json.dumps(token).encode()).decode().rstrip("=")
        refused(c, "negative decoded cursor offset refused", lambda: session.history(cwd, hostile))
        token["offset"] = True
        hostile = base64.urlsafe_b64encode(json.dumps(token).encode()).decode().rstrip("=")
        refused(c, "boolean decoded cursor offset refused", lambda: session.history(cwd, hostile))


def check_path_and_symlink_confinement(c):
    with fixture() as (cwd, path, identity):
        outside = os.path.join(os.path.dirname(identity["config"]), "outside.jsonl")
        put(outside, [{"type": "secret"}])
        for field in ("session_id", "project_dir"):
            old = identity[field]
            for bad in ("../outside", "/outside", "a/b", "", ".", ".."):
                identity[field] = bad
                refused(c, f"unsafe {field} refused", lambda: session.history(cwd))
            identity[field] = old
        os.unlink(path)
        os.symlink(outside, path)
        refused(c, "transcript symlink cannot escape", lambda: session.history(cwd))
        os.unlink(path)
        project = os.path.dirname(path)
        os.rmdir(project)
        os.symlink(os.path.dirname(outside), project)
        refused(c, "project directory symlink cannot escape", lambda: session.history(cwd))


def check_identity_and_file_are_rechecked_before_return(c):
    with fixture() as (cwd, path, identity):
        put(path, [{"type": "old"}])
        calls = []
        def switch(_cwd):
            calls.append(1)
            return dict(identity, start="boot:changed") if len(calls) > 1 else dict(identity)
        with patched(session, _history_identity=switch):
            refused(c, "identity switch during read must not return old data", lambda: session.history(cwd))
        c.check("identity is rechecked", len(calls) >= 2)
        old = session._history_identity
        def rotate(_cwd):
            if os.path.exists(path + ".moved"):
                return old(_cwd)
            os.rename(path, path + ".moved")
            put(path, [{"type": "replacement"}])
            return old(_cwd)
        calls.clear()
        def late_rotate(_cwd):
            calls.append(1)
            return rotate(_cwd) if len(calls) > 1 else old(_cwd)
        with patched(session, _history_identity=late_rotate):
            refused(c, "rotation during read must not return old data", lambda: session.history(cwd))


def check_config_slug_live_identity_missing_and_duplicate(c):
    with tempfile.TemporaryDirectory() as root:
        home = os.path.join(root, "home")
        config = os.path.join(root, "config")
        cwd = os.path.join(root, "work.with_under-score")
        os.makedirs(os.path.join(config, "sessions"))
        os.mkdir(home)
        os.mkdir(cwd)
        env = {"HOME": home, "CLAUDE_CONFIG_DIR": config, "CLAUDE_CODE_PROJECT_DIR_NAME": "named-project"}
        process = {"start": "boot:123", "proc_start": "123", "env": env, "cwd": cwd, "uid": os.getuid()}
        record = {"pid": 456, "cwd": cwd, "sessionId": SID, "messagingSocketPath": "/unused.sock"}
        registry = os.path.join(config, "sessions", "456.json")
        def write(d):
            with open(registry, "w") as f:
                json.dump(d, f)
        write(record)
        digest = hashlib.sha256(os.path.realpath(record["messagingSocketPath"]).encode()).hexdigest()
        with open(os.path.join(config, "sessions", f"456.{digest}.key"), "w") as f:
            json.dump({"procStart": "123", "peerToken": "fixture-only"}, f)
        with patched_env(**env), patched(session, _history_process=lambda _pid: dict(process),
                                         _history_peer=lambda _path: (456, os.getuid(), [2, 3])):
            got = session._history_identity(cwd)
            c.expect("config home bound to live process", got["config"], config)
            c.expect("custom project directory name with explicit config", got["project_dir"], "named-project")
            process["env"] = {"HOME": home, "CLAUDE_CONFIG_DIR": config}
            got = session._history_identity(cwd)
            c.expect("current slug punctuation rule", got["project_dir"], re.sub(r"[^A-Za-z0-9]", "-", cwd))
            for missing in ("pid", "cwd", "sessionId", "messagingSocketPath"):
                write({k: v for k, v in record.items() if k != missing})
                refused(c, f"missing live registry {missing} refuses", lambda: session._history_identity(cwd))
            write(record)
            for field in ("start", "env", "cwd", "uid"):
                keep = process.pop(field)
                refused(c, f"unknown live process {field} refuses", lambda: session._history_identity(cwd))
                process[field] = keep
            with open(os.path.join(config, "sessions", "duplicate.json"), "w") as f:
                json.dump(record, f)
            refused(c, "duplicate live candidates refused instead of freshest", lambda: session._history_identity(cwd))
            os.unlink(os.path.join(config, "sessions", "duplicate.json"))
            with patched(session, _history_peer=lambda _path: (457, os.getuid(), [2, 3])):
                refused(c, "socket peer PID must match registry", lambda: session._history_identity(cwd))


def check_live_process_start_must_match_confined_key_file(c):
    """HYPOTHESIS: peer PID без start fence допускает остатки после PID reuse.

    Имя и procStart key-файла взяты из офлайн-кода 2.1.296; токен не нужен
    чтению и нигде не должен выходить. Живое подтверждение — отдельно #596.
    """
    with tempfile.TemporaryDirectory() as root:
        config = os.path.join(root, "config")
        cwd = os.path.join(root, "work")
        os.makedirs(os.path.join(config, "sessions"))
        os.mkdir(cwd)
        env = {"HOME": root, "CLAUDE_CONFIG_DIR": config}
        process = {"start": "boot:123", "proc_start": "123", "env": env,
                   "cwd": cwd, "uid": os.getuid()}
        record = {"pid": 456, "cwd": cwd, "sessionId": SID, "messagingSocketPath": "/unused.sock"}
        with open(os.path.join(config, "sessions", "456.json"), "w") as f:
            json.dump(record, f)
        digest = hashlib.sha256(os.path.realpath(record["messagingSocketPath"]).encode()).hexdigest()
        key = os.path.join(config, "sessions", f"456.{digest}.key")
        with patched_env(**env), patched(session, _history_process=lambda _pid: dict(process),
                                         _history_peer=lambda _path: (456, os.getuid(), [2, 3])):
            refused(c, "missing process-start key refuses live identity", lambda: session._history_identity(cwd))
            for data in ({"peerToken": "not-read"}, {"procStart": "124"}, {"procStart": 123}):
                with open(key, "w") as f:
                    json.dump(data, f)
                refused(c, "missing or mismatched key start refuses", lambda: session._history_identity(cwd))
            with open(key, "w") as f:
                json.dump({"procStart": "123", "peerToken": "never-return-this"}, f)
            got = session._history_identity(cwd)
            c.expect("matched start accepted", got["start"], "boot:123")
            c.check("key token never enters identity", "never-return-this" not in json.dumps(got))
            outside = os.path.join(root, "outside")
            os.rename(key, outside)
            os.symlink(outside, key)
            refused(c, "key symlink refuses identity", lambda: session._history_identity(cwd))


def check_snapshot_rewrite_is_rejected_but_append_is_allowed(c):
    with fixture() as (cwd, path, _identity):
        put(path, [{"type": "before", "text": "a" * 1000}])
        original = session._history_page
        def append(*args):
            out = original(*args)
            with open(path, "ab") as f:
                f.write(b'{"type":"append"}\n')
            return out
        with patched(session, _history_page=append):
            out = session.history(cwd)
        c.expect("concurrent append returns snapshot only", len(out["records"]), 1)
        c.expect("concurrent append is available on next cursor", session.history(cwd, out["cursor"])["records"], [{"type": "append"}])
        def rewrite(*args):
            out = original(*args)
            with open(path, "r+b") as f:
                f.seek(200)
                f.write(b"b")
            return out
        with patched(session, _history_page=rewrite):
            refused(c, "same-size rewrite during read refuses mixed snapshot", lambda: session.history(cwd))


def check_standalone_history_arguments(c):
    with fixture() as (cwd, path, _identity):
        put(path, [{"type": "meta"}])
        text, err, code = run_command(session.main, ["history", cwd])
        c.expect("standalone history success", code, 0)
        c.expect("standalone records", json.loads(text)["records"], [{"type": "meta"}])
        cursor = json.loads(text)["cursor"]
        text, err, code = run_command(session.main, ["history", cwd, "--cursor", cursor])
        c.expect("standalone cursor continuation", json.loads(text)["records"], [])
        for argv in (["history", cwd, "--path", path], ["history", cwd, "--cursor"], ["history", cwd, "extra"]):
            text, err, code = run_command(session.main, argv)
            c.check("standalone unexpected arguments refused", code != 0)
            c.check("standalone refusal is structured JSON", isinstance(json.loads(text).get("error"), str))


def check_node_history_is_master_project_read_and_cursor_only(c):
    c.check("history verb exists", "history" in agent.VERBS)
    if "history" not in agent.VERBS:
        return
    c.expect("history closed master scope", agent.VERBS["history"].scope, agent.MASTER)
    c.check("history is named for project authorization", agent.VERBS["history"].named)
    c.check("history not public", "history" not in agent.PUBLIC_VERBS)
    c.check("tail still public", "tail" in agent.PUBLIC_VERBS)
    calls = []
    async def body(name, command, timeout=20, private=False):
        c.check("history transport does not echo unparsable private stdout", private)
        calls.append((name, shlex.split(command)))
        return {"epoch": "e", "session_id": SID, "cursor": "c", "records": [],
                "reset": False, "has_more": False, "partial": False}
    async def project(_name):
        return "test-project"
    async def no_owner(*args, **kwargs):
        raise AssertionError("history read must not claim an owner")
    with patched(agent, session_json=body, clone_dir=lambda _name: "/body/clone",
                 puppet_project=project, _claim=no_owner, owner_profile=no_owner, _event=no_owner):
        req = {"verb": "history", "name": "pu-test-project-1", "cursor": "safe-token"}
        msg = Msg("mop.test-project.node.test.rpc.operator", body=req)
        asyncio.run(agent.handle(None, msg, public=False))
        c.check("authorized history goes inside body", bool(calls))
        if calls:
            c.expect("body command cursor only", calls[-1][1][-4:], ["history", "/body/clone", "--cursor", "safe-token"])
        count = len(calls)
        for bad in ({"path": "/outside"}, {"config": "/outside"}, {"cursor": {}}, {"cursor": "x" * 2049}):
            msg = Msg("mop.test-project.node.test.rpc.operator", body=dict(req, **bad))
            asyncio.run(agent.handle(None, msg, public=False))
            c.check("unsafe history request refused", "error" in msg.replies[-1])
        c.expect("unsafe input never reaches body", len(calls), count)
        for subject, public in (("mop.foreign.node.test.rpc.operator", False), ("mop.test-project.node.test.msg", True)):
            msg = Msg(subject, body=req)
            asyncio.run(agent.handle(None, msg, public=public))
            c.check("foreign or public transcript request refused", "error" in msg.replies[-1])
        c.expect("authorization denial never reaches body", len(calls), count)


def check_history_json_requires_utf8_and_finite_numbers(c):
    for raw in (b'{"n":1e999}', b'{"text":"\\ud800"}', '{"text":"Ж"}'.encode("utf-16")):
        refused(c, "history JSON rejects non-finite, surrogate, or non-UTF8 input",
                lambda raw=raw: session._history_json(raw))


def check_private_transport_never_echoes_partial_transcript_or_incompatible_reply(c):
    """HYPOTHESIS: общий session_json превращал оборванный stdout в error text."""
    async def body(_name, _script, _timeout=20):
        return '{"records":[{"type":"user","text":"private-synthetic-marker"}', 1
    with patched(agent, bsh=body, clone_dir=lambda _name: "/body/clone"):
        out = asyncio.run(agent.v_history(None, {"verb": "history", "name": "pu-test-1"}))
        c.check("partial transcript must be refused", "error" in out)
        c.check("private stdout never appears in refusal", "private-synthetic-marker" not in json.dumps(out))
    async def incompatible(*_args, **_kwargs):
        return {"records": ["not a transcript response"]}
    with patched(agent, session_json=incompatible, clone_dir=lambda _name: "/body/clone"):
        out = asyncio.run(agent.v_history(None, {"verb": "history", "name": "pu-test-1"}))
        c.check("incompatible body JSON contract fails closed", "error" in out)
    async def wrong_error(*_args, **_kwargs):
        return {"error": "unexpected shape", "records": [{"text": "private-synthetic-marker"}]}
    with patched(agent, session_json=wrong_error, clone_dir=lambda _name: "/body/clone"):
        out = asyncio.run(agent.v_history(None, {"verb": "history", "name": "pu-test-1"}))
        c.check("an error cannot smuggle transcript fields", "private-synthetic-marker" not in json.dumps(out))


def main():
    c = Checks()
    checks = [fn for name, fn in globals().items() if name.startswith("check_")]
    if not c.check("session.history(cwd, cursor=None) is missing", callable(getattr(session, "history", None))):
        check_node_history_is_master_project_read_and_cursor_only(c)
        return c.report("transcript")
    for fn in checks:
        try:
            fn(c)
        except Exception as e:
            c.fail(fn.__name__, f"{type(e).__name__}: {e}")
    return c.report("transcript")


if __name__ == "__main__":
    sys.exit(main())
