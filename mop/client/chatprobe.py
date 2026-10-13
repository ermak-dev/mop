"""Проверка установленного интерактивного Claude на собственном tmux и фикстурах.

Не адаптер чата и не тест платного провайдера. Байты attach сохраняются без
декодирования; снимки pane нужны только для наблюдения UI. Каждый вывод требует
своего свидетельства, неполный прогон не превращается в «всё работает».
"""
import base64
import errno
import fcntl
import http.server
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import threading
import time
from urllib.parse import urlsplit
import uuid

MODEL = "claude-opus-5-5"
SECOND_MODEL = "claude-sonnet-5-5"


def isolated_env(source, home, endpoint):
    """Белый список вместо удаления известных секретов: новые тоже не наследуются."""
    url = urlsplit(endpoint)
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
            or url.username or url.password or url.path or url.query or url.fragment):
        raise ValueError("chat probe requires a loopback HTTP fixture endpoint")
    return {"PATH": source.get("PATH", os.defpath), "HOME": str(home),
            "XDG_CONFIG_HOME": str(Path(home) / ".config"),
            "XDG_CACHE_HOME": str(Path(home) / ".cache"),
            "XDG_DATA_HOME": str(Path(home) / ".local/share"),
            "TMPDIR": str(Path(home) / "tmp"), "TMUX_TMPDIR": str(Path(home) / "tmp"),
            "SHELL": "/bin/sh",
            "TERM": "xterm-256color", "LANG": "C.UTF-8",
            "CLAUDE_CONFIG_DIR": str(Path(home) / ".claude"),
            "ANTHROPIC_API_KEY": "chat-probe-dummy-key", "ANTHROPIC_BASE_URL": endpoint,
            "HTTP_PROXY": endpoint, "HTTPS_PROXY": endpoint, "ALL_PROXY": endpoint,
            "http_proxy": endpoint, "https_proxy": endpoint, "all_proxy": endpoint,
            "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
            "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def prompt_kind(body):
    """Результат инструмента не является новой командой фикстуре."""
    message = next((m for m in reversed(body.get("messages", [])) if m.get("role") == "user"), None)
    if message is None:
        return "text"
    content = message.get("content", "")
    if isinstance(content, list):
        has_result = any(b.get("type") == "tool_result" for b in content)
        content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        if has_result and "probe:recover" not in content:
            return "result"
    for kind in ("stream", "bash", "ask", "after-clear", "after-model", "recover", "framing"):
        if f"probe:{kind}" in content:
            return kind
    return "text"


def primary_request(body):
    """Основной ход имеет инструменты; фоновые заголовки не дают свидетельства UI."""
    return bool(body.get("tools"))


def message_events(model, block):
    """Messages SSE по официальной схеме, включая потоковый JSON инструмента."""
    tool = block["type"] == "tool_use"
    start = dict(block, input={}) if tool else {"type": "text", "text": ""}
    delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
             if tool else {"type": "text_delta", "text": block["text"]})
    return [
        {"type": "message_start", "message": {"id": "msg_probe_" + uuid.uuid4().hex,
         "type": "message", "role": "assistant", "model": model, "content": [],
         "stop_reason": None, "stop_sequence": None,
         "usage": {"input_tokens": 10, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": start},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn",
         "stop_sequence": None}, "usage": {"output_tokens": 10}},
        {"type": "message_stop"}]


def message_response(model, block):
    """Полное сообщение для нестриминговой проверки модели самим CLI."""
    message = dict(message_events(model, block)[0]["message"])
    message.update(content=[block], stop_reason="tool_use" if block["type"] == "tool_use" else "end_turn",
                   usage={"input_tokens": 10, "output_tokens": 10})
    return message


def sse(event):
    """Одно событие, отделённое пустой строкой."""
    return ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()


def terminal_frame(seq, data):
    """Бинарный кусок не обязан кончаться на границе UTF-8 или escape."""
    return {"seq": seq, "size": len(data), "data": base64.b64encode(data).decode("ascii")}


def paste(text):
    """Bracketed paste, Enter посылается отдельно после закрывающего маркера."""
    return b"\x1b[200~" + text.encode() + b"\x1b[201~"


def outcome(observed, unsupported=False):
    """Неподдерживаемое и неизвестное не выдаются за успех."""
    return "observed" if observed else "unsupported" if unsupported else "unknown"


def completion_seen(requests, kind, since, screen):
    """Свежий основной запрос и его ответ, не старый текст в scrollback."""
    matching = [r for r in requests if r["kind"] == kind]
    return len(matching) > since and matching[-1]["response_text"] in screen


class Fixture(http.server.ThreadingHTTPServer):
    """Только loopback; внешние HTTP/CONNECT через proxy явно отклоняются."""
    daemon_threads = True

    def __init__(self, work):
        super().__init__(("127.0.0.1", 0), Handler)
        self.work = work
        self.requests = []
        self.denied = []
        self.stream_started = threading.Event()
        self.stream_closed = threading.Event()
        self.release = threading.Event()
        self.unsupported = set()
        self.errors = []

    def handle_error(self, request, client_address):
        error = sys.exception()
        self.errors.append(f"fixture handler failed: {type(error).__name__}: {error}")


class Handler(http.server.BaseHTTPRequestHandler):
    """Заголовки авторизации не пишутся даже с фиктивным ключом."""
    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        self.server.denied.append(self.path)
        self.send_error(403, "External network disabled by chat probe")

    def do_GET(self):
        self.server.denied.append(self.path)
        self.send_error(404, "No such fixture")

    def do_POST(self):
        if not self.path.startswith("/v1/messages") or self.path.startswith("http"):
            self.server.denied.append(self.path)
            self.send_error(403, "No such fixture")
            return
        size = int(self.headers.get("Content-Length", "0"))
        if size > 8 * 1024 * 1024:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(size))
        if self.path.split("?")[0] == "/v1/messages/count_tokens":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        kind = prompt_kind(body) if primary_request(body) else "auxiliary"
        response_text = "fixture-backed " + kind + " complete " + uuid.uuid4().hex[:12]
        self.server.requests.append({"kind": kind, "body": body, "response_text": response_text})
        block = {"type": "text", "text": response_text}
        if kind in ("bash", "ask"):
            name = "Bash" if kind == "bash" else "AskUserQuestion"
            if not any(t.get("name") == name for t in body.get("tools", [])):
                self.server.unsupported.add(kind)
                block["text"] = "fixture-backed tool unsupported: " + name
            else:
                work = shlex.quote(str(self.server.work))
                args = ({"command": f"echo $$ > {work}/bash.pid; touch {work}/bash.started; "
                         f"sleep 90 & child=$!; echo $child > {work}/sleep.pid; "
                         f'wait "$child"; touch {work}/bash.finished', "timeout": 120000,
                         "description": "Controlled chat probe interrupt"} if kind == "bash" else
                        {"questions": [{"question": "Fixture-backed choice?", "header": "Probe",
                          "options": [{"label": "Alpha", "description": "First fixture choice"},
                                      {"label": "Beta", "description": "Second fixture choice"}],
                          "multiSelect": False}]})
                block = {"type": "tool_use", "id": "tool_probe_" + uuid.uuid4().hex,
                         "name": name, "input": args}
        if not body.get("stream"):
            data = json.dumps(message_response(body.get("model", MODEL), block)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        events = message_events(body.get("model", MODEL), block)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(sse(event))
                self.wfile.flush()
                if kind == "stream" and event["type"] == "content_block_delta":
                    self.server.stream_started.set()
                    until = time.monotonic() + 115
                    while not self.server.release.wait(.2) and time.monotonic() < until:
                        self.wfile.write(sse({"type": "ping"}))
                        self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            if kind == "stream":
                self.server.stream_closed.set()


class Terminal:
    """Собственный attach PTY: ни default server, ни чужие сессии не открываются."""
    def __init__(self, tmux, label, env, work, command, root):
        self.tmux, self.label, self.env = tmux, label, env
        self.root = root
        self.seq = 0
        self.input_seq = 0
        self.raw = bytearray()
        self.frames = (root / "terminal.jsonl").open("w")
        self.inputs = (root / "input.jsonl").open("w")
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        self.proc = None
        try:
            self.call("new-session", "-d", "-s", "probe", "-x", "140", "-y", "40",
                      "-c", str(work), *command)
            self.call("set-option", "-g", "escape-time", "0")
            self.call("set-option", "-g", "focus-events", "on")
            self.call("set-option", "-g", "status", "off")
            def controlling_tty():
                os.setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            self.proc = subprocess.Popen([tmux, "-L", label, "-f", "/dev/null", "attach-session",
                                          "-t", "probe"], env=env, cwd=work,
                                         stdin=slave, stdout=slave, stderr=slave,
                                         preexec_fn=controlling_tty)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(slave)

    def call(self, *args):
        r = subprocess.run([self.tmux, "-L", self.label, "-f", "/dev/null", *args],
                           env=self.env, capture_output=True, timeout=10)
        if r.returncode:
            raise RuntimeError("own tmux operation failed: " + r.stderr.decode(errors="replace"))
        return r.stdout.decode(errors="replace").rstrip()

    def pump(self, seconds=.2):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            ready, _, _ = select.select([self.master], [], [], min(.1, max(0, until-time.monotonic())))
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError as e:
                if e.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            self.raw.extend(chunk)
            self.frames.write(json.dumps(terminal_frame(self.seq, chunk)) + "\n")
            self.frames.flush()
            self.seq += 1
            # Только ответы на terminal queries, не второй источник пользовательского ввода.
            if b"\x1b[6n" in chunk:
                self.send(b"\x1b[1;1R")
            if b"\x1b[c" in chunk:
                self.send(b"\x1b[?1;2c")

    def send(self, data):
        self.inputs.write(json.dumps(terminal_frame(self.input_seq, data)) + "\n")
        self.inputs.flush()
        self.input_seq += 1
        os.write(self.master, data)

    def text(self, text):
        self.send(paste(text))
        self.pump(.2)
        self.send(b"\r")

    def screen(self):
        self.pump()
        return self.call("capture-pane", "-p", "-t", "probe", "-S", "-200")

    def wait(self, predicate, timeout=20):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.pump(.2)
            if predicate():
                return True
        return False

    def identity(self):
        pid = int(self.call("display-message", "-p", "-t", "probe", "#{pane_pid}"))
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "start_ticks": stat[19], "foreground_pgid": stat[5],
                "command": self.call("display-message", "-p", "-t", "probe", "#{pane_current_command}")}

    def close(self):
        # Удаляется только случайно названный собственный сервер, включая детей pane.
        stopped = subprocess.run([self.tmux, "-L", self.label, "-f", "/dev/null", "kill-server"],
                                 env=self.env, capture_output=True, timeout=10)
        cleanup_error = stopped.returncode and b"no server running" not in stopped.stderr
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        os.close(self.master)
        self.frames.close()
        self.inputs.close()
        if cleanup_error:
            raise RuntimeError("own tmux cleanup failed: " + stopped.stderr.decode(errors="replace"))
        return True


def run(root, timeout=30):
    """Возвращает независимые наблюдения и пути доказательств; ничего не печатает."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.chmod(root, 0o700)
    home, work = root / "home", root / "work"
    work.mkdir()
    (home / "tmp").mkdir(parents=True)
    (home / ".claude").mkdir()
    fixture = Fixture(work)
    endpoint = f"http://127.0.0.1:{fixture.server_port}"
    env = isolated_env(os.environ, home, endpoint)
    claude, tmux = shutil.which("claude"), shutil.which("tmux")
    if not claude or not tmux:
        fixture.server_close()
        raise RuntimeError("chat probe requires installed claude and tmux")
    (home / ".claude" / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True,
        "theme": "dark", "customApiKeyResponses": {"approved": ["chat-probe-dummy-key"], "rejected": []}}))
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"permissions": {"defaultMode": "default"}}))
    server_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    server_thread.start()
    label = "mop-chat-probe-" + uuid.uuid4().hex
    session_id = str(uuid.uuid4())
    report = {"fixture_backed": True, "paid_provider_tested": False,
              "tmux_server": label, "requested_session_id": session_id,
              "report_path": str(root / "report.json"), "scenarios": {},
              "terminal_path": str(root / "terminal.jsonl"), "errors": []}
    terminal = None
    def record(name, observed, evidence, unsupported=False):
        report["scenarios"][name] = {"outcome": outcome(observed, unsupported), "evidence": evidence}
    def snapshot(name):
        screen = terminal.screen()
        (root / (name + ".txt")).write_text(screen)
        return screen
    def sessions():
        return sorted(p.stem for p in (home / ".claude" / "projects").glob("**/*.jsonl")
                      if p.parent.name != "subagents")
    def request(kind):
        return [r for r in fixture.requests if r["kind"] == kind]
    def turn(kind, text=None):
        since = len(request(kind))
        terminal.text(text or "probe:" + kind)
        return terminal.wait(lambda: completion_seen(fixture.requests, kind, since, terminal.screen()), timeout)
    try:
        report["claude_version"] = subprocess.run([claude, "--version"], env=env, cwd=work,
            capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        help_text = subprocess.run([claude, "--help"], env=env, cwd=work,
            capture_output=True, text=True, timeout=10, check=True).stdout
        (root / "claude-help.txt").write_text(help_text)
        report["claude_executable"] = claude
        if "--bare" not in help_text:
            raise RuntimeError("installed claude lacks --bare isolation; probe not started")
        report["isolation_mode"] = "bare; explicit temporary settings; default permissions"
        report["limitations"] = ["Fixture-backed responses only; no paid provider evidence.",
            "Bare mode skips discovery/keychain and may expose fewer tools than normal mode.",
            "External HTTP/HTTPS proxy attempts are refused; this is not an OS network sandbox.",
            "Linux /proc identity and English UI observations; unknown means insufficient evidence."]
        command = [claude, "--bare", "--model", MODEL, "--session-id", session_id,
                   "--permission-mode", "default", "--tools", "Bash,AskUserQuestion",
                   "--settings", str(settings),
                   "--setting-sources", "user", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        report["command"] = command
        terminal = Terminal(tmux, label, env, work, command, root)
        terminal.pump(3)
        startup = snapshot("startup")
        if "trust this folder" in startup.lower() or "trust this directory" in startup.lower():
            terminal.send(b"\x1b[B\r")
            terminal.pump(2)
        startup = snapshot("ready")
        before = terminal.identity()
        report["initial_identity"] = before
        framing_text = "probe:framing\nsecond line with Unicode: Проба"
        framing = turn("framing", framing_text)
        sent = request("framing")
        exact_input = bool(sent) and framing_text in json.dumps(sent[-1]["body"]["messages"], ensure_ascii=False).replace("\\n", "\n")
        record("terminal-framing", framing and exact_input and b"\x1b[" in terminal.raw,
               {"request_seen": bool(sent), "exact_multiline_unicode_input": exact_input,
                "fresh_answer_rendered": framing, "raw_bytes": len(terminal.raw), "snapshot": "framing.txt"})
        snapshot("framing")
        initial_sessions = sessions()
        help_requests_before = len(fixture.requests)
        terminal.text("/help")
        terminal.pump(2)
        help_screen = snapshot("slash-help")
        help_requests_after = len(fixture.requests)
        record("foreground-slash", "Help  General" in help_screen and terminal.identity() == before
               and str(before["pid"]) == before["foreground_pgid"]
               and help_requests_before == help_requests_after,
               {"before": before, "after": terminal.identity(), "snapshot": "slash-help.txt",
                "requests_before": help_requests_before, "requests_after": help_requests_after})
        terminal.send(b"\x1b")
        terminal.pump(.5)
        terminal.text("/model " + SECOND_MODEL)
        terminal.pump(2)
        model_screen = snapshot("model")
        model_confirmation = "Switch model?" in model_screen
        if model_confirmation:
            terminal.send(b"\r")
            terminal.pump(1)
            snapshot("model-applied")
        model_answer = turn("after-model")
        model_requests = request("after-model")
        record("model-identity", model_answer and bool(model_requests) and
               model_requests[-1]["body"].get("model") == SECOND_MODEL and terminal.identity() == before
               and sessions() == initial_sessions,
               {"before": before, "after": terminal.identity(), "sessions": sessions(),
                "switch_confirmation_seen": model_confirmation, "fresh_answer_rendered": model_answer,
                "request_model": model_requests[-1]["body"].get("model") if model_requests else None})
        terminal.text("/clear")
        terminal.pump(2)
        snapshot("clear")
        clear_answer = turn("after-clear")
        new_sessions = sessions()
        clear_requests = request("after-clear")
        history = json.dumps(clear_requests[-1]["body"].get("messages", [])) if clear_requests else ""
        record("clear-identity", clear_answer and terminal.identity() == before and
               bool(set(new_sessions) - set(initial_sessions)) and "probe:framing" not in history,
               {"before_sessions": initial_sessions, "after_sessions": new_sessions,
                "before": before, "after": terminal.identity(), "old_prompt_in_request": "probe:framing" in history})
        terminal.text("probe:stream")
        stream_started = terminal.wait(fixture.stream_started.is_set, timeout)
        snapshot("stream-before-interrupt")
        terminal.send(b"\x1b")
        stream_closed = terminal.wait(fixture.stream_closed.is_set, min(timeout, 10))
        fixture.release.set()
        terminal.pump(.5)
        recovered = turn("recover", "probe:recover stream")
        record("stream-interrupt", stream_started and stream_closed and recovered and terminal.identity() == before,
               {"fixture_stream_started": stream_started, "fixture_connection_closed": stream_closed,
                "next_turn_completed": recovered, "snapshot": "stream-after-interrupt.txt"})
        snapshot("stream-after-interrupt")
        terminal.text("probe:bash")
        terminal.wait(lambda: bool(request("bash")), timeout)
        permission = terminal.wait(lambda: "Do you want to proceed" in terminal.screen()
                                   or (work / "bash.started").exists(), timeout)
        permission_screen = snapshot("permission")
        prompted = "Do you want to proceed" in permission_screen
        record("permissions-default", prompted and not (work / "bash.started").exists(),
               {"prompt_seen": prompted, "started_before_approval": (work / "bash.started").exists(),
                "snapshot": "permission.txt"}, "bash" in fixture.unsupported)
        if prompted:
            terminal.send(b"\r")
        bash_started = terminal.wait(lambda: (work / "bash.started").exists(), min(timeout, 10))
        terminal.send(b"\x1b")
        terminal.pump(2)
        bash_pid = int((work / "bash.pid").read_text()) if bash_started else None
        sleep_pid = int((work / "sleep.pid").read_text()) if (work / "sleep.pid").exists() else None
        bash_stopped = terminal.wait(lambda: not Path(f"/proc/{bash_pid}").exists()
            and sleep_pid is not None and not Path(f"/proc/{sleep_pid}").exists(), 10) if bash_pid else False
        bash_recovery = turn("recover", "probe:recover bash")
        record("bash-interrupt", bash_started and bash_stopped and not (work / "bash.finished").exists()
               and bash_recovery and terminal.identity() == before,
               {"started": bash_started, "pid": bash_pid, "sleep_pid": sleep_pid,
                "shell_and_sleep_gone": bash_stopped,
                "finished": (work / "bash.finished").exists(), "next_turn_completed": bash_recovery},
               "bash" in fixture.unsupported)
        terminal.text("probe:ask")
        terminal.wait(lambda: bool(request("ask")), timeout)
        ask_shown = terminal.wait(lambda: "Fixture-backed choice?" in terminal.screen(), min(timeout, 10))
        snapshot("ask")
        if ask_shown:
            terminal.send(b"\r")
            terminal.pump(.5)
            terminal.send(b"\r")
            terminal.pump(1)
        ask_result = terminal.wait(lambda: any("tool_result" in json.dumps(r["body"]["messages"])
            and "Alpha" in json.dumps(r["body"]["messages"]) for r in request("result")), min(timeout, 10))
        record("ask-user-question", ask_shown and ask_result,
               {"question_shown": ask_shown, "answer_in_tool_result": ask_result, "snapshot": "ask.txt",
                "advertised_tools": [t.get("name") for t in request("ask")[-1]["body"].get("tools", [])]
                if request("ask") else []},
               "ask" in fixture.unsupported)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        report["errors"].append(str(e))
    finally:
        fixture.release.set()
        tmux_stopped = terminal is None
        if terminal:
            try:
                tmux_stopped = terminal.close()
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                report["errors"].append(str(e))
        fixture.shutdown()
        fixture.server_close()
        server_thread.join(timeout=5)
        (root / "requests.json").write_text(json.dumps(fixture.requests, indent=2, ensure_ascii=False))
        report["errors"].extend(fixture.errors)
        report["denied_network_requests"] = fixture.denied
        report["cleanup"] = {"own_tmux_server_stopped": tmux_stopped,
                             "fixture_listener_closed": fixture.socket.fileno() == -1,
                             "fixture_thread_stopped": not server_thread.is_alive()}
        for name in ("terminal-framing", "foreground-slash", "model-identity", "clear-identity",
                     "stream-interrupt", "permissions-default", "bash-interrupt", "ask-user-question"):
            if name not in report["scenarios"]:
                record(name, False, {"reason": "scenario not reached"})
        (root / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report
