#!/usr/bin/env python3
"""Изоляция и фикстуры интерактивной проверки без CLI, tmux и сети."""
import importlib.util
import json
import os
import sys

import hermetic  # noqa: F401
from _lib import Checks, patched, run_command
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

# HYPOTHESIS: headless/resume не проверяет живой foreground и байты терминала.
# SOLUTION: отдельный CLI, tmux и loopback Messages; чистые части проверяем здесь.
# RESULT: сначала отсутствие возможности — явный провал, не ошибка импорта.
# STATUS: FIXED — see #613


def main():
    c = Checks()
    if not c.check("#613 interactive probe library exists", importlib.util.find_spec(
            "mop.client.chatprobe") is not None):
        return c.report("chatprobe")
    from mop.client import chatprobe as p
    # HYPOTHESIS: #729 выдуманный ACK не подтверждается штатным native wire.
    # Фикстура фиксирует только конечный набор полей и точную корреляцию;
    # отсутствие status/idle остаётся unknown, а не успешным native прогоном.
    # SOLUTION: whitelist projection и отдельный normal-only Unix callback.
    # RESULT: RED — projection отсутствует; GREEN — чистая логика и owned normal
    # Docker 2.1.302: msgV=1 idle, dropped наблюдались; delivered остаётся unknown.
    # STATUS: FIXED — see #729
    if not c.check("#729 native receipt projection exists", callable(getattr(p, "peer_receipt_projection", None))):
        return c.report("chatprobe")
    user_id, subscription = "user-id", "subscription-id"
    receipt = dict(msgV=1, type="control", action="peer_message_status", orig_msg_id=user_id,
                   status="held", reason="private detail", from_="private path", token="secret")
    project = lambda frame: p.peer_receipt_projection((json.dumps(frame)+"\n").encode(), user_id, subscription)
    c.expect("#729 exact native status projection", project(receipt),
        dict(action="peer_message_status", orig_msg_id=user_id, status="held", msgV=1))
    for status in ("held", "denied", "expired", "delivered", "refused", "dropped"):
        c.expect("#729 finite status " + status, project({**receipt, "status": status})["status"], status)
    for change in ({"action": "message_accepted"}, {"status": "accepted"}, {"orig_msg_id": subscription},
                   {"orig_msg_id": "other"}, {"status": []}, {"msgV": True}, {"type": "user"}):
        c.expect("#729 invented or uncorrelated receipt", project({**receipt, **change}), None)
    idle = dict(type="control", action="peer_idle_notice", orig_msg_id=subscription, state="idle")
    c.expect("#729 observes absent idle version without silently authorizing it", project(idle),
        dict(action="peer_idle_notice", orig_msg_id=subscription, state="idle", msgV=None))
    c.expect("#729 observes versioned idle", project({**idle, "msgV": 1})["msgV"], 1)
    c.expect("#729 user id is never idle subscription", project({**idle, "orig_msg_id": user_id}), None)
    for state in ("busy", "unknown", [], None):
        c.expect("#729 unknown idle state", project({**idle, "state": state}), None)
    duplicate = b'{"type":"control","action":"peer_message_status","orig_msg_id":"user-id","status":"held","status":"delivered","msgV":1}\n'
    c.expect("#729 ambiguous wire is not evidence", p.peer_receipt_projection(duplicate, user_id, subscription), None)
    c.expect("#729 bounded native projection", p.peer_receipt_projection(b" "*8193, user_id, subscription), None)
    dirty = {"PATH": "/usr/bin", "HOME": "/real", "ANTHROPIC_API_KEY": "secret",
             "ANTHROPIC_AUTH_TOKEN": "secret", "HTTP_PROXY": "https://real",
             "MOP_BUS_CONFIG": "secret", "CLAUDE_CODE_OAUTH_TOKEN": "secret",
             "NODE_OPTIONS": "--require secret", "SSH_AUTH_SOCK": "/real",
             "AWS_PROFILE": "real", "UNEXPECTED_SECRET": "secret", "TMUX": "real"}
    env = p.isolated_env(dirty, "/isolated", "http://127.0.0.1:1234")
    c.expect("#613 fresh home", env["HOME"], "/isolated")
    c.expect("#613 own tmux socket directory", env.get("TMUX_TMPDIR"), "/isolated/tmp")
    c.expect("#613 fixture key", env["ANTHROPIC_API_KEY"], "chat-probe-dummy-key")
    c.expect("#613 loopback provider", env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:1234")
    c.check("#613 unknown secrets not inherited", not any(v == "secret" for v in env.values()))
    c.check("#613 MOP/auth/loader/socket removed", not any(k in env for k in
            ("MOP_BUS_CONFIG", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
             "NODE_OPTIONS", "SSH_AUTH_SOCK", "AWS_PROFILE", "TMUX", "UNEXPECTED_SECRET")))
    c.expect("#613 external HTTP diverted to refusing fixture", env["HTTPS_PROXY"],
             "http://127.0.0.1:1234")
    for url in ("https://api.anthropic.com", "http://localhost:1234", "http://127.0.0.1:1234/x",
                "http://user:pw@127.0.0.1:1234", "http://127.0.0.1:1234?x=y"):
        try:
            p.isolated_env({}, "/isolated", url)
            c.fail("#613 nonfixture endpoint refused", url)
        except ValueError:
            c.check("#613 endpoint refused", True)
    block = {"type": "text", "text": "fixture-backed answer"}
    events = p.message_events("fixture-model", block)
    c.expect("#613 SSE event order", [e["type"] for e in events],
             ["message_start", "content_block_start", "content_block_delta",
              "content_block_stop", "message_delta", "message_stop"])
    c.expect("#613 streamed text starts empty", events[1]["content_block"],
             {"type": "text", "text": ""})
    c.expect("#613 text delta", events[2]["delta"],
             {"type": "text_delta", "text": "fixture-backed answer"})
    tool = {"type": "tool_use", "id": "probe_tool", "name": "Bash",
            "input": {"command": "sleep 90", "description": "Controlled probe"}}
    events = p.message_events("fixture-model", tool)
    c.expect("#613 tool delta is JSON", json.loads(events[2]["delta"]["partial_json"]), tool["input"])
    c.expect("#613 tool stop reason", events[-2]["delta"]["stop_reason"], "tool_use")
    # HYPOTHESIS: /model делает обычный Messages вызов без stream; SSE вместо
    # JSON ломает валидацию модели, хотя основной поток уже работает.
    if not c.check("#613 nonstream Messages fixture exists", callable(getattr(p, "message_response", None))):
        return c.report("chatprobe")
    response = p.message_response("fixture-model", block)
    c.expect("#613 nonstream content", response["content"], [block])
    c.expect("#613 nonstream metadata", (response["type"], response["role"], response["model"],
             response["stop_reason"], response["usage"]),
             ("message", "assistant", "fixture-model", "end_turn", {"input_tokens": 10, "output_tokens": 10}))
    c.expect("#613 nonstream tool stop", p.message_response("fixture-model", tool)["stop_reason"], "tool_use")
    wire = p.sse(events[0])
    c.check("#613 SSE framing has event/data and blank delimiter",
            wire.startswith(b"event: message_start\ndata: {") and wire.endswith(b"\n\n"))
    body = {"messages": [{"role": "user", "content": "probe:stream"},
                         {"role": "assistant", "content": [tool]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "probe_tool",
                                                       "content": "probe:stream"}]}]}
    c.expect("#613 tool result never retriggers fixture", p.prompt_kind(body), "result")
    c.expect("#613 interrupted tool plus fresh user text", p.prompt_kind({"messages": [{"role": "user",
        "content": [{"type": "tool_result", "content": "probe:bash"},
                    {"type": "text", "text": "[Request interrupted by user]\\nprobe:recover bash"}]}]}), "recover")
    c.expect("#613 explicit prompt", p.prompt_kind({"messages": [{"role": "user", "content":
             [{"type": "text", "text": "probe:stream"}]}]}), "stream")
    c.expect("#613 no messages", p.prompt_kind({}), "text")
    # HYPOTHESIS: реальный CLI добавляет effort-system после user и отдельно
    # запрашивает заголовок; эти вызовы не доказывают основной ход.
    c.expect("#613 trailing system does not hide user", p.prompt_kind({"messages": [
        {"role": "user", "content": "probe:framing"},
        {"role": "system", "content": []}]}), "framing")
    c.check("#613 title call is auxiliary", not p.primary_request({"tools": []}))
    c.check("#613 interactive tool-bearing call is primary", p.primary_request({"tools": [{"name": "Bash"}]}))
    data = b"\x1b[?2004h\x00\xff\xe2\x82"
    frame = p.terminal_frame(7, data)
    import base64
    c.expect("#613 partial UTF8/ANSI/NUL bytes are lossless", base64.b64decode(frame["data"]), data)
    c.expect("#613 frame sequence", (frame["seq"], frame["size"]), (7, len(data)))
    c.expect("#613 paste input uses terminal framing", p.paste("hello\nworld"),
             b"\x1b[200~hello\nworld\x1b[201~")
    c.expect("#613 unknown is not pass", p.outcome(False, False), "unknown")
    c.expect("#613 unsupported is separate", p.outcome(False, True), "unsupported")
    c.expect("#613 evidence is observed", p.outcome(True, False), "observed")
    # HYPOTHESIS: прежний recover в scrollback не доказывает восстановление
    # после второго interrupt; нужен новый основной запрос и его уникальный ответ.
    if not c.check("#613 fresh completion parser exists", callable(getattr(p, "completion_seen", None))):
        return c.report("chatprobe")
    requests = [{"kind": "recover", "response_text": "fixture-backed recover complete old"}]
    c.check("#613 stale completion is not recovery", not p.completion_seen(requests, "recover", 1,
            "fixture-backed recover complete old"))
    requests.append({"kind": "recover", "response_text": "fixture-backed recover complete new"})
    c.check("#613 old screen is not recovery", not p.completion_seen(requests, "recover", 1,
            "fixture-backed recover complete old"))
    c.check("#613 fresh response is recovery", p.completion_seen(requests, "recover", 1,
            "fixture-backed recover complete new"))
    # HYPOTHESIS: HOME не меняет системную SSH identity и длинный cwd ломает сокет.
    # SOLUTION: запрет git transport, короткий собственный runtime, явные ворота.
    # RESULT: сначала проверки отсутствующих ворот и неверного окружения красные.
    # STATUS: FIXED — see #613
    for key, value in {"GIT_ALLOW_PROTOCOL": "file", "GIT_SSH_COMMAND": "false",
                       "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CEILING_DIRECTORIES": "/"}.items():
        c.expect("#613 git guard " + key, env.get(key), value)
    injected = p.isolated_env({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.sshCommand",
        "GIT_CONFIG_VALUE_0": "ssh", "GIT_CONFIG_PARAMETERS": "bad"}, "/isolated", "http://127.0.0.1:1234")
    c.check("#613 git injection removed", not any(k in injected for k in
        ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_PARAMETERS")))
    functions = ("tmux_socket", "unexpected_home_paths", "fixture_auth_ok", "cleanup_verdict",
                 "snapshot_projection", "valid_history_argument", "managed_preflight")
    available = {name: c.check("#613 safety function " + name,
                              callable(getattr(p, name, None))) for name in functions}
    if available["tmux_socket"]:
        c.check("#613 long output cannot lengthen tmux socket", len(p.tmux_socket("/tmp/mp-123456")) < 108)
        c.expect("#613 explicit private socket", p.tmux_socket("/tmp/mp-123456"), "/tmp/mp-123456/s")
    if available["unexpected_home_paths"]:
        c.expect("#613 ordinary own transcript allowed", p.unexpected_home_paths([
            ".claude/projects/project/session.jsonl", ".claude/sessions/123.key", ".claude/settings.json"]), [])
        bad = [".claude/plugins/marketplaces/official/.git/config", ".git/config", ".ssh/id_ed25519",
               ".claude/.credentials.json", ".git-credentials", ".claude/plugins/installed_plugins.json"]
        c.expect("#613 marketplace/git/credential touches fail", p.unexpected_home_paths(bad), sorted(bad))
    if available["fixture_auth_ok"]:
        c.check("#613 dummy fixture auth allowed", p.fixture_auth_ok({"x-api-key": "chat-probe-dummy-key"}))
        for headers in ({}, {"x-api-key": "real"}, {"x-api-key": "chat-probe-dummy-key", "authorization": "Bearer real"}):
            c.check("#613 other fixture auth refused", not p.fixture_auth_ok(headers))
    if available["cleanup_verdict"]:
        c.check("#613 no leftovers is clean", p.cleanup_verdict(True, []) )
        c.check("#613 leftover not cleanup success", not p.cleanup_verdict(True, [{"pid": 12, "start_ticks": "33"}]))
        c.check("#613 kill error not cleanup success", not p.cleanup_verdict(False, []))
    if available["snapshot_projection"]:
        projected = p.snapshot_projection({"pid": 123, "sessionId": "sid", "cwd": "/own",
            "messagingSocketPath": "/tmp/own/s", "private": "secret"},
            {"peerToken": "secret", "procStart": "12345", "extra": "secret"}, "/own/.claude",
            {"pid": 123, "start_ticks": "12345"}, True)
        c.check("#613 snapshot never leaks peer token/unknown fields", "secret" not in json.dumps(projected))
        c.expect("#613 key raw procStart contract", projected["key"],
            {"fields": ["extra", "peerToken", "procStart"], "peer_token_present": True,
             "procStart": "12345", "procStart_type": "str"})
        c.expect("#613 actual registry session id", projected["sessionId"], "sid")
        c.expect("#613 raw numeric procStart stays numeric", p.snapshot_projection({}, {"procStart": 123},
            "/own", None, False)["key"]["procStart_type"], "int")
    if available["valid_history_argument"]:
        c.check("#613 explicit source argument", p.valid_history_argument("/explicit/mop/session.py"))
        for value in ("session.py", "https://example/session.py", "/explicit/other.py", "/a/../session.py"):
            c.check("#613 history argument refused", not p.valid_history_argument(value))
    if available["managed_preflight"]:
        c.check("#613 absent managed settings allowed", p.managed_preflight([]))
        c.check("#613 present managed settings refused without contents", not p.managed_preflight(["/etc/claude-code/managed-settings.json"]))
    # HYPOTHESIS: bare не публикует inbox без явного пути, а error_present скрывает причину.
    # SOLUTION: явный короткий messaging socket и безопасная проекция history.
    # RESULT: воспроизведено в новом живом прогоне, history отказал без сокета.
    # STATUS: FIXED — see #613
    if c.check("#613 probe command builder exists", callable(getattr(p, "probe_command", None))):
        built = p.probe_command("/bin/claude", "/own/settings.json", "sid", "/tmp/mp-own/c.sock")
        c.check("#613 probe still uses bare isolation", "--bare" in built)
        c.expect("#613 explicit messaging socket", built[built.index("--messaging-socket-path") + 1], "/tmp/mp-own/c.sock")
    if c.check("#613 history projection exists", callable(getattr(p, "history_projection", None))):
        failed = p.history_projection({"error": "session registry socket is unknown"}, 1)
        c.expect("#613 history refusal reason retained", failed["error"], "session registry socket is unknown")
        safe = p.history_projection({"records": [{"type": "user", "private": "secret"}],
            "cursor": "secret", "epoch": "secret", "session_id": "sid", "reset": True,
            "has_more": False, "partial": False}, 0)
        c.check("#613 history private values never projected", "secret" not in json.dumps(safe))
        c.expect("#613 actual history records count", safe["records_count"], 1)
    # HYPOTHESIS: пустой scaffold marketplaces не равен clone/install.
    # SOLUTION: отдельно учитывать тип entry, не исключая вложенные установки.
    # RESULT: первый guarded прогон ошибочно отказал на одном пустом каталоге.
    # STATUS: FIXED — see #613
    import inspect
    if c.check("#613 directory-aware marketplace evidence", "directories" in inspect.signature(p.unexpected_home_paths).parameters):
        scaffold = ".claude/plugins/marketplaces"
        c.expect("#613 empty scaffold is not installation", p.unexpected_home_paths([scaffold], directories=[scaffold]), [])
        c.expect("#613 nested marketplace directory still fails", p.unexpected_home_paths(
            [scaffold, scaffold + "/official"], directories=[scaffold, scaffold + "/official"]), [scaffold + "/official"])
        c.expect("#613 file at scaffold path is unexpected", p.unexpected_home_paths([scaffold]), [scaffold])
    if c.check("#613 process config projection exists", callable(getattr(p, "process_config_projection", None))):
        c.expect("#613 own config environment projected", p.process_config_projection(
            b"HOME=/own\x00CLAUDE_CONFIG_DIR=/own/.claude\x00ANTHROPIC_API_KEY=secret\x00"),
            {"HOME": "/own", "CLAUDE_CONFIG_DIR": "/own/.claude"})
    if c.check("#613 live contract verdict exists", callable(getattr(p, "live_contract_observed", None))):
        row = {"pid": 123, "sessionId": "sid", "process_config_matches": True,
            "process": {"pid": 123, "start_ticks": "12345", "state": "S"},
            "socket_path_present": True, "socket_peer": {"pid": 123, "start_ticks": "12345"},
            "key": {"peer_token_present": True, "procStart": "12345", "procStart_type": "str"}}
        c.check("#613 proven live contract observed", p.live_contract_observed([row]))
        for modified in ({"socket_path_present": False}, {"socket_peer": None}, {"process_config_matches": False},
                         {"key": {"procStart": 12345, "procStart_type": "int", "peer_token_present": True}},
                         {"key": {"procStart": "54321", "procStart_type": "str", "peer_token_present": True}}):
            c.check("#613 missing/mismatched contract unknown", not p.live_contract_observed([{**row, **modified}]))
        c.check("#613 empty live capture unknown", not p.live_contract_observed([]))
    # HYPOTHESIS: одинаковый /model может не породить PostModelSwitch; наличие
    # сокета, --model и passive model.id не доказывают подтверждение модели.
    # SOLUTION: opt-in fixture sink до exec, активация только после live proof;
    # строгая bounded проекция и отдельное причинное окно каждой команды.
    # RESULT: RED сначала на отсутствующих parser/settings/verdict функциях.
    # STATUS: FIXED — see #643
    import shlex
    import subprocess
    import tempfile
    from pathlib import Path
    features = ("model_hook_projection", "probe_settings", "capture_model_hook",
                "model_switch_observed")
    feature_available = {name: c.check("#643 function " + name,
        callable(getattr(p, name, None))) for name in features}
    payload = dict(hook_event_name="PostModelSwitch", session_id="sid",
                   to_model=p.MODEL, from_model=p.SECOND_MODEL, prompt="private-prompt",
                   transcript_path="private-transcript", token="private-token", at_ns=1)
    if feature_available["model_hook_projection"]:
        project = lambda body: p.model_hook_projection(json.dumps(body).encode(), 50)
        projected = project(payload)
        c.expect("#643 exact bounded projection", projected, dict(event="PostModelSwitch",
            session_id="sid", to_model=p.MODEL, from_model=p.SECOND_MODEL, at_ns=50))
        c.check("#643 raw private fields never retained", "private-" not in json.dumps(projected))
        for raw in (b"", b"{", b"[]", b"null", b"\xff", b"{}" + b" " * 4094,
                    b'{"hook_event_name":"Stop","hook_event_name":"PostModelSwitch"}',
                    b"[" * 1000 + b"0" + b"]" * 1000):
            c.expect("#643 malformed/at-limit/duplicate input dropped", p.model_hook_projection(raw, 50), None)
        for modified in ({"session_id": "../foreign"}, {"session_id": "x" * 129},
                {"session_id": []}, {"to_model": {}}, {"to_model": ""},
                {"to_model": "a\nb"}, {"from_model": "x" * 129},
                {"hook_event_name": "Statusline"}, {"hook_event_name": "Stop"}):
            c.expect("#643 invalid identity/model/event dropped", project({**payload, **modified}), None)
        c.expect("#643 passive model cannot stand in for switch", project(dict(
            hook_event_name="PostModelSwitch", session_id="sid", model={"id": p.MODEL})), None)
        c.expect("#643 absent from-model stays unknown", project(dict(hook_event_name="PostModelSwitch",
            session_id="sid", to_model=p.MODEL))["from_model"], None)
        submit = project(dict(hook_event_name="UserPromptSubmit", session_id="sid", prompt="private"))
        c.expect("#643 hook positive control contains no prompt", submit, dict(
            event="UserPromptSubmit", session_id="sid", to_model=None, from_model=None, at_ns=50))
    if feature_available["probe_settings"]:
        c.expect("#643 default settings unchanged", p.probe_settings(), {"permissions": {"defaultMode": "default"}})
        target = "/private/with space ' and $/model-hooks.jsonl"
        settings = p.probe_settings(target)
        c.expect("#643 only fixed explicit hooks", sorted(settings["hooks"]),
                 ["PostModelSwitch", "SessionStart", "UserPromptSubmit"])
        command_hook = settings["hooks"]["PostModelSwitch"][0]["hooks"][0]
        argv = shlex.split(command_hook["command"])
        c.expect("#643 quoted trusted interpreter and fixed module", argv,
                 [str(Path(sys.executable).resolve()), "-I", str(Path(p.__file__).resolve()),
                  "--capture-model-hook", target])
        c.check("#643 command hook has finite timeout", 0 < command_hook["timeout"] <= 2)
        c.check("#643 no statusline model authority", "statusLine" not in settings)
    if feature_available["capture_model_hook"]:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "model-hooks.jsonl"
            c.check("#643 inactive output drops hook", not p.capture_model_hook(output, json.dumps(payload).encode(), 50))
            c.check("#643 hook cannot create authority/output file", not output.exists())
            output.touch(mode=0o600)
            c.check("#643 valid active sink captures", p.capture_model_hook(output, json.dumps(payload).encode(), 50))
            c.expect("#643 sink contains safe projection only", json.loads(output.read_text()), dict(
                event="PostModelSwitch", session_id="sid", to_model=p.MODEL,
                from_model=p.SECOND_MODEL, at_ns=50))
            before = output.read_bytes()
            c.check("#643 malformed sink write dropped", not p.capture_model_hook(output, b"{", 51))
            c.expect("#643 malformed does not change evidence", output.read_bytes(), before)
            alias = Path(directory) / "alias"
            alias.symlink_to(output)
            try:
                accepted = p.capture_model_hook(alias, json.dumps(payload).encode(), 52)
            except OSError:
                accepted = False
            c.check("#643 output symlink refused", not accepted)
            c.expect("#643 symlink target unchanged", output.read_bytes(), before)
            # Настоящий pipe проверяет stdin-timeout, но это не native acceptance.
            argv = [sys.executable, "-I", p.__file__, "--capture-model-hook", str(output)]
            child = subprocess.run(argv, input=json.dumps(payload).encode(), capture_output=True, timeout=2)
            c.expect("#643 fixed hook subprocess is silent", (child.returncode, child.stdout, child.stderr), (0, b"", b""))
            c.expect("#643 fixed subprocess writes one safe record", len(output.read_text().splitlines()), 2)
            child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                child.stdin.write(b"{")
                child.stdin.flush()
                child.wait(timeout=1)
                c.expect("#643 stalled stdin quietly expires", child.returncode, 0)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate()
            c.expect("#643 stalled stdin never becomes evidence", len(output.read_text().splitlines()), 2)
    if feature_available["model_switch_observed"]:
        rows = [dict(pid=123, sessionId="sid", process_config_matches=True,
            socket_path_present=True, process=dict(pid=123, start_ticks="456", state="S"),
            socket_peer=dict(pid=123, start_ticks="456"),
            key=dict(peer_token_present=True, procStart="456", procStart_type="str"))]
        event = dict(event="PostModelSwitch", session_id="sid", to_model=p.MODEL,
                     from_model=p.SECOND_MODEL, at_ns=50)
        observe = lambda events, before=rows, after=rows: p.model_switch_observed(
            events, before, after, 40, 60, 30, p.MODEL, p.SECOND_MODEL)
        c.check("#643 scoped timed native event observed", observe([event]))
        c.check("#643 same-model without hook is not confirmation", not observe([]))
        for modified in ({"at_ns": 39}, {"at_ns": 61}, {"session_id": "foreign"},
                         {"event": "UserPromptSubmit"}, {"to_model": p.SECOND_MODEL},
                         {"from_model": None}):
            c.check("#643 stale/foreign/non-switch event unknown", not observe([{**event, **modified}]))
        c.check("#643 missing live process unknown", not observe([event], after=[]))
        c.check("#643 changed registry unknown", not observe([event], after=[{**rows[0], "sessionId": "new"}]))
        c.check("#643 activation after command unknown", not p.model_switch_observed(
            [event], rows, rows, 40, 60, 45, p.MODEL, p.SECOND_MODEL))
    command = importlib.import_module("mop.cli.dev.chat-probe")
    calls = []
    def fake_run(root, timeout, history_module=None):
        calls.append((str(root), timeout) if history_module is None else (str(root), timeout, history_module))
        return {"report_path": "/private/report.json", "scenarios": {
            "a": {"outcome": "observed"}, "b": {"outcome": "unsupported"},
            "c": {"outcome": "unknown"}}, "errors": []}
    with patched(p, run=fake_run):
        out, err, code = run_command(command.main, ["--output", "/private", "--timeout", "7"])
        c.expect("#613 CLI independent verdicts", (out, err, code),
                 ("/private/report.json\nfixture-backed; paid provider not tested\na: observed\nb: unsupported\nc: unknown\n", "", 0))
        c.expect("#613 CLI delegates once", calls, [("/private", 7)])
        for args in (["--timeout", "0"], ["--timeout", "31"], ["--output"], ["--unknown"]):
            _, _, code = run_command(command.main, args)
            c.check("#613 CLI refuses invalid arguments", bool(code))
        c.expect("#613 invalid CLI never launches process", len(calls), 1)
        _, _, code = run_command(command.main, ["--output", "/private", "--history-module", "/explicit/mop/session.py"])
        c.expect("#613 CLI accepts explicit history source", code, 0)
        c.expect("#613 CLI forwards explicit history source", calls[-1], ("/private", 20, "/explicit/mop/session.py"))
        count = len(calls)
        for value in ("session.py", "/explicit/other.py", "https://example/session.py"):
            _, _, code = run_command(command.main, ["--history-module", value])
            c.check("#613 CLI rejects unsafe history source", bool(code))
        c.expect("#613 rejected history never launches", len(calls), count)
    # HYPOTHESIS: 2.1.302 подавляет даже explicit hooks в --bare; пустой sink
    # не различает suppression и неподдержанную same-model семантику.
    # SOLUTION: выводить unsupported лишь по явному native --help contract;
    # неизвестный контракт остаётся unknown, --bare никогда не удаляется.
    # RESULT: owned run a: пустой sink при трёх успешных fixture turns; RED
    # на отсутствующем разборщике capability до изменения verdict.
    # STATUS: FIXED — see #643
    if c.check("#643 native bare capability parser exists", callable(getattr(p, "bare_skips_hooks", None))):
        native = ("  --bare   Minimal mode: skip hooks (those defined\n"
                  "             in settings and by installed plugins).\n  --betas <x> other")
        c.check("#643 wrapped native skip contract recognized", p.bare_skips_hooks(native))
        for text in ("", "--help", "  --bare Minimal mode\n  --other skip hooks", "--not-bare skip hooks"):
            c.check("#643 absent contract not invented", not p.bare_skips_hooks(text))
    hook_calls = []
    def fake_hooks(root, timeout, history_module=None, model_hooks=False):
        hook_calls.append((str(root), timeout, history_module, model_hooks))
        return {"report_path": "/private/report.json", "scenarios": {}, "errors": []}
    with patched(p, run=fake_hooks):
        _, _, code = run_command(command.main, ["--output", "/private", "--model-hooks"])
        c.expect("#643 CLI opt-in accepted", code, 0)
        c.expect("#643 CLI forwards explicit hook mode", hook_calls, [("/private", 20, None, True)])
    # HYPOTHESIS: --bare отключает settings hooks; снять его на host небезопасно.
    # SOLUTION: отдельный local-only Docker fixture, whitelist snapshot и две
    # независимые проверки: inspect до старта и kernel proof до native exec.
    # RESULT: RED на отсутствующих mode/isolation воротах, не import failure.
    # STATUS: FIXED — see #643
    normal_calls = []
    def fake_normal(root, timeout, history_module=None, model_hooks=False, isolated_normal=False):
        normal_calls.append((model_hooks, isolated_normal))
        return {"report_path": "/private/report.json", "scenarios": {}, "errors": []}
    with patched(p, run=fake_normal):
        _, _, code = run_command(command.main, ["--model-hooks", "--isolated-normal"])
        c.expect("#643 explicit isolated normal CLI accepted", code, 0)
        c.expect("#643 normal mode delegated, never host flag", normal_calls, [(True, True)])
        count = len(normal_calls)
        for args in (["--isolated-normal"], ["--isolated-normal", "--model-hooks",
                     "--history-module", "/explicit/session.py"]):
            _, _, code = run_command(command.main, args)
            c.check("#643 isolated normal only accepts fixed hook experiment", bool(code))
        c.expect("#643 invalid mode never launches", len(normal_calls), count)
    name = "mop.client.chatprobe_isolation"
    if not c.check("#643 isolated normal boundary exists", importlib.util.find_spec(name) is not None):
        return c.report("chatprobe")
    d = importlib.import_module(name)
    required = ("fixture_sources", "local_docker_endpoint", "container_create_args",
                "container_config_errors", "runtime_isolation_errors", "runtime_projection")
    if not all([c.check("#643 boundary function " + name, callable(getattr(d, name, None)))
                for name in required]):
        return c.report("chatprobe")
    sources = ("mop/client/chatprobe.py", "mop/client/chatprobe_isolation.py")
    c.expect("#643 snapshot copies only two tracked fixture modules", d.fixture_sources(
        [*sources, ".env", "mop/session.py", "CLAUDE.md", ".claude/settings.json", "inventory.yaml"]), sources)
    try:
        d.fixture_sources([sources[0]])
        c.fail("#643 missing tracked fixture refused", "accepted")
    except ValueError:
        c.check("#643 missing tracked fixture refused", True)
    c.expect("#643 local Unix daemon accepted", d.local_docker_endpoint("unix:///var/run/docker.sock"),
             "unix:///var/run/docker.sock")
    for endpoint in ("", "tcp://127.0.0.1:2375", "ssh://localhost", "unix://remote/run/docker.sock",
                     "unix:///run/../run/docker.sock", "unix:///run/docker.sock?x=y", "unix:///run/s\n"):
        try:
            d.local_docker_endpoint(endpoint)
            c.fail("#643 remote or ambiguous Docker refused", endpoint)
        except ValueError:
            c.check("#643 remote or ambiguous Docker refused", True)
    built = d.container_create_args("sha256:image", "mop-chatprobe-owned", "nonce", 26430)
    for key, value in (("--network", "none"), ("--cgroupns", "private"), ("--ipc", "private"),
                       ("--user", "26430:26430"), ("--cap-drop", "ALL"),
                       ("--security-opt", "no-new-privileges=true")):
        c.expect("#643 container isolation argument " + key, built[built.index(key) + 1], value)
    c.check("#643 no bind/socket/host namespace flags", not any(arg in built for arg in
            ("-v", "--volume", "--mount", "--volumes-from", "--privileged", "--pid", "--device", "--env-file")))
    c.check("#643 fixture UID is unprivileged", "0:0" not in built)
    info = {"Id": "owned-id", "Image": "sha256:image", "Mounts": [],
        "Config": {"User": "26430:26430", "Labels": {d.OWNER_LABEL: "nonce"}, "Volumes": None},
        "HostConfig": {"NetworkMode": "none", "PidMode": "", "CgroupnsMode": "private", "IpcMode": "private",
            "Privileged": False, "CapDrop": ["ALL"], "CapAdd": None,
            "SecurityOpt": ["no-new-privileges=true"], "Binds": None, "Mounts": None,
            "Devices": [], "DeviceRequests": None, "VolumesFrom": None}}
    c.expect("#643 independent inspect accepts owned fixture", d.container_config_errors(info, "sha256:image", "nonce", 26430), [])
    import copy
    mutations = [("NetworkMode", "host"), ("PidMode", "host"), ("PidMode", "container:other"),
        ("CgroupnsMode", "host"), ("IpcMode", "host"), ("Privileged", True),
        ("CapAdd", ["SYS_ADMIN"]), ("CapDrop", []), ("SecurityOpt", []),
        ("Binds", ["/home:/host:ro"]), ("Mounts", [{"Source": "/run"}]),
        ("Devices", [{"PathOnHost": "/dev/sda"}]), ("VolumesFrom", ["other"])]
    for key, value in mutations:
        bad = copy.deepcopy(info)
        bad["HostConfig"][key] = value
        c.check("#643 unsafe inspect refused " + key, bool(d.container_config_errors(bad, "sha256:image", "nonce", 26430)))
    for changed in ({"Mounts": [{"Source": "/var/run/docker.sock"}]}, {"Image": "other"},
                    {"Config": {**info["Config"], "User": "0"}},
                    {"Config": {**info["Config"], "Labels": {d.OWNER_LABEL: "foreign"}}},
                    {"Config": {**info["Config"], "Volumes": {"/host": {}}}}):
        c.check("#643 foreign/mounted/root fixture refused", bool(d.container_config_errors(
            {**info, **changed}, "sha256:image", "nonce", 26430)))
    namespaces = {name: name + ":[100]" for name in ("pid", "mnt", "net", "cgroup", "ipc")}
    evidence = dict(uid=26430, euid=26430, groups=[26430], cap_eff="0000000000000000", no_new_privs=1,
        namespaces={name: name + ":[200]" for name in namespaces}, interfaces=["lo"],
        proc_private=True, cgroup_readonly=True, cgroup_root="/", run_sockets_absent=True,
        external_network_unreachable=True)
    c.expect("#643 kernel evidence proves distinct namespaces", d.runtime_isolation_errors(evidence, namespaces, 26430), [])
    for key, value in (("uid", 0), ("euid", 0), ("groups", [0, 26430]), ("cap_eff", "1"),
            ("no_new_privs", 0), ("interfaces", ["lo", "eth0"]), ("proc_private", False),
            ("cgroup_readonly", False), ("cgroup_root", "/host/cgroup"), ("run_sockets_absent", False),
            ("external_network_unreachable", False), ("namespaces", namespaces), ("namespaces", {})):
        c.check("#643 incomplete/unsafe kernel proof refused " + key,
            bool(d.runtime_isolation_errors({**evidence, key: value}, namespaces, 26430)))
    projected = d.runtime_projection({**evidence, "environ": "secret", "mountinfo": "secret", "token": "secret"})
    c.expect("#643 runtime projection exact allowlist", projected, evidence)
    c.check("#643 runtime projection contains no raw secrets", "secret" not in json.dumps(projected))
    nested = {**evidence, "namespaces": {**evidence["namespaces"], "unknown_token": "secret"}}
    c.check("#643 nested runtime metadata also whitelisted", "secret" not in json.dumps(d.runtime_projection(nested)))
    default_calls = []
    with patched(p, _run_fixture=lambda *args: default_calls.append(args)):
        p.run("/private", 7, model_hooks=True)
    c.expect("#643 default still delegates to bare without normal override", default_calls, [("/private", 7, None, True)])
    with tempfile.TemporaryDirectory() as directory:
        normal_calls = []
        if c.check("#643 internal normal guard exists", callable(getattr(p, "_normal_guard", None))):
            with patched(p, _normal_guard=lambda: (_ for _ in ()).throw(RuntimeError("kernel blocker"))):
                try:
                    p._run_fixture(Path(directory) / "unsafe", model_hooks=True, _normal=True)
                    c.fail("#643 internal normal mode refuses missing kernel proof", "accepted")
                except RuntimeError as error:
                    c.expect("#643 kernel blocker reaches caller", str(error), "kernel blocker")
        c.check("#643 kernel refusal happens before output/native fixture", not (Path(directory) / "unsafe").exists())
    # HYPOTHESIS: обычный tracked source имеет 0664 в shared worktree;
    # это не metadata доверия native install и не повод ослаблять ELF guard.
    # SOLUTION: source whitelist разрешает group-write, native — никогда.
    # RESULT: RED после реального отказа до build; host CLI не запущен.
    # STATUS: FIXED — see #643
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.py"
        source.write_text("trusted fixture code")
        source.chmod(0o664)
        try:
            result = d._copy_regular(source, Path(directory) / "copied.py")
            c.expect("#643 tracked nonsensitive source permits ordinary worktree mode", result["size"], source.stat().st_size)
        except RuntimeError as error:
            c.fail("#643 tracked nonsensitive source permits ordinary worktree mode", str(error))
        for data, mode in ((b"#!/bin/sh\n", 0o755), (b"\x7fELFnative", 0o775)):
            source.write_bytes(data)
            source.chmod(mode)
            try:
                d._copy_regular(source, Path(directory) / ("native-" + str(mode)), native=True)
                c.fail("#643 script/group-writable native refused", "accepted")
            except RuntimeError:
                c.check("#643 script/group-writable native refused", True)
    # Host managed policy должна останавливать даже metadata/copy/build.
    with tempfile.TemporaryDirectory() as directory:
        actions = []
        with patched(p, managed_paths=lambda: ["/etc/claude-code/managed-settings.json"]), patched(d,
                _docker_endpoint=lambda *args: actions.append("docker")):
            report = p.run(Path(directory) / "refused", model_hooks=True, isolated_normal=True)
        c.check("#643 host managed policy refuses normal fixture", bool(report["errors"]))
        c.expect("#643 policy checked before Docker/copy", actions, [])
    # HYPOTHESIS: policy может появиться, пока проверяется Docker/build;
    # проверки лишь при входе недостаточно для copy и actual start.
    # SOLUTION: повторные ворота непосредственно перед snapshot и start.
    # RESULT: backend ниже синтетический, native/daemon не запускаются.
    # STATUS: FIXED — see #643
    from types import SimpleNamespace
    for policy_phase in ("after-version", "after-inspect"):
        actions = []
        state = {"policy": False, "owner": None, "created": False, "removed": False, "image": False}
        cid = "c" * 64
        def fake_docker(argv, **kwargs):
            if argv[0] == "git":
                return SimpleNamespace(returncode=0, stdout=("\0".join(sources) + "\0").encode(), stderr=b"")
            words = argv[3:]
            actions.append(tuple(words[:2]))
            output = ""
            if words[0] == "version":
                state["policy"] = policy_phase == "after-version"
            elif words[0] == "build":
                state["owner"] = words[words.index("--label") + 1].split("=", 1)[1]
                state["image"] = True
            elif words[:2] == ["image", "inspect"]:
                output = json.dumps([{"Id": "sha256:image", "Config": {"Labels": {d.OWNER_LABEL: state["owner"]}}}])
            elif words[0] == "create":
                state["created"] = True
                output = cid
            elif words[:2] == ["container", "inspect"]:
                config = copy.deepcopy(info)
                config.update(Id=cid, State={"Running": False, "Pid": 0})
                config["Config"]["Labels"][d.OWNER_LABEL] = state["owner"]
                output = json.dumps([config])
                state["policy"] = policy_phase == "after-inspect"
            elif words[0] == "start":
                raise RuntimeError("fake backend refuses native execution")
            elif words[:2] == ["image", "ls"]:
                if state["image"] and not state["removed"]:
                    output = "sha256:image"
            elif words[:2] == ["image", "rm"]:
                state["removed"] = True
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        def fake_copy(*args, **kwargs):
            actions.append(("copy",))
            return {"sha256": "synthetic-test-only"}
        with tempfile.TemporaryDirectory() as directory, patched(p,
                managed_paths=lambda: ["managed"] if state["policy"] else []), patched(d,
                _docker_endpoint=lambda *args: "unix:///var/run/docker.sock", _copy_regular=fake_copy), patched(
                d.shutil, which=lambda name: "/usr/bin/" + name), patched(d.subprocess, run=fake_docker):
            report = p.run(Path(directory) / "report", model_hooks=True, isolated_normal=True)
        c.check("#643 late policy reports refusal " + policy_phase, bool(report["errors"]))
        c.check("#643 late policy never starts native " + policy_phase, not any(a[0] == "start" for a in actions))
        if policy_phase == "after-version":
            c.check("#643 late policy refuses before source/native copy", ("copy",) not in actions)
        else:
            c.check("#643 refused created fixture is cleaned", report["cleanup"].get("container_removed"))
    # HYPOTHESIS: bookworm apt python3 даёт 3.11, хотя owned fixture нужен 3.12.
    # SOLUTION: публичный Python 3.12 image и его interpreter, без host install.
    # RESULT: RED на трёх свойствах старого image; GREEN после смены base/entry.
    # STATUS: FIXED — see #743
    dockerfile = d._dockerfile(26430, "owned")
    c.check("#743 owned fixture uses Python 3.12 base", dockerfile.startswith("FROM python:3.12-slim-bookworm\n"))
    c.check("#743 apt cannot select a different fixture Python", " install -y --no-install-recommends python3 " not in dockerfile)
    c.check("#743 entry uses the image Python", '"/usr/local/bin/python3", "-I"' in dockerfile)
    return c.report("chatprobe")


if __name__ == "__main__":
    sys.exit(main())
