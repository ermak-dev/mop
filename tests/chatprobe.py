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
    command = importlib.import_module("mop.cli.dev.chat-probe")
    calls = []
    def fake_run(root, timeout):
        calls.append((str(root), timeout))
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
    return c.report("chatprobe")


if __name__ == "__main__":
    sys.exit(main())
