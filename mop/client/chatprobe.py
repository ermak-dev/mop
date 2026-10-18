"""Проверка установленного интерактивного Claude на собственном tmux и фикстурах.

Не адаптер чата и не тест платного провайдера. Байты attach сохраняются без
декодирования; снимки pane нужны только для наблюдения UI. Каждый вывод требует
своего свидетельства, неполный прогон не превращается в «всё работает».
"""
import base64
import errno
import fcntl
import http.server
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import struct
import socket
import stat
import signal
import tempfile
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
            "GIT_ALLOW_PROTOCOL": "file", "GIT_SSH_COMMAND": "false",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CEILING_DIRECTORIES": "/",
            "XDG_RUNTIME_DIR": str(Path(home) / "tmp"),
            "SHELL": "/bin/sh",
            "TERM": "xterm-256color", "LANG": "C.UTF-8",
            "CLAUDE_CONFIG_DIR": str(Path(home) / ".claude"),
            "ANTHROPIC_API_KEY": "chat-probe-dummy-key", "ANTHROPIC_BASE_URL": endpoint,
            "HTTP_PROXY": endpoint, "HTTPS_PROXY": endpoint, "ALL_PROXY": endpoint,
            "http_proxy": endpoint, "https_proxy": endpoint, "all_proxy": endpoint,
            "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
            "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def tmux_socket(runtime):
    """Не зависит от длины каталога свидетельств или cwd вызывающего."""
    path = str(Path(runtime) / "s")
    if len(os.fsencode(path)) >= 108:
        raise ValueError("private tmux socket path is too long")
    return path


def unexpected_home_paths(paths, directories=()):
    """Пустой корневой scaffold не установка; вложенные entries всё ещё запрещены."""
    paths = [path for path in paths if not
             (path == ".claude/plugins/marketplaces" and path in directories)]
    return sorted(path for path in paths if
        any(part in (".git", ".ssh", ".gitconfig", ".git-credentials", ".credentials.json")
            for part in Path(path).parts)
        or any(part in ("marketplaces", "installed_plugins.json", "known_marketplaces.json")
               for part in Path(path).parts)
        or "credential" in Path(path).name.lower())


def fixture_auth_ok(headers):
    """Проверяется только равенство; значение заголовка не выходит наружу."""
    headers = {key.lower(): value for key, value in headers.items()}
    return (headers.get("x-api-key") == "chat-probe-dummy-key"
            and not headers.get("authorization"))


def cleanup_verdict(server_stopped, leftovers):
    """kill-server сам по себе не доказывает, что дети завершились."""
    return bool(server_stopped and not leftovers)


def snapshot_projection(registry, key, config, peer, present):
    """Белый список реестра и ключа: peerToken никогда не копируется."""
    return {**{name: registry.get(name) for name in
               ("pid", "sessionId", "cwd", "messagingSocketPath")},
            "config_home": str(config), "socket_path_present": present, "socket_peer": peer,
            "key": {"fields": sorted(key), "peer_token_present": bool(key.get("peerToken")),
                    "procStart": key.get("procStart"),
                    "procStart_type": type(key.get("procStart")).__name__}}


def live_contract_observed(rows):
    """Наличие JSON не доказывает socket/key identity: нужны все совпадения."""
    if len(rows) != 1:
        return False
    row = rows[0]
    process, peer, key = row.get("process"), row.get("socket_peer"), row.get("key", {})
    return bool(process and peer and row.get("sessionId") and row.get("process_config_matches")
        and row.get("socket_path_present") and key.get("peer_token_present")
        and row.get("pid") == process.get("pid") == peer.get("pid")
        and process.get("state") not in ("Z", "X") and key.get("procStart_type") == "str"
        and isinstance(key.get("procStart"), str) and key["procStart"].isascii()
        and key["procStart"].isdigit()
        and key["procStart"] == process.get("start_ticks") == peer.get("start_ticks"))


def valid_history_argument(value):
    """Источник явно указан оператором; URL и относительный импорт запрещены."""
    path = Path(value)
    return path.is_absolute() and path.name == "session.py" and ".." not in path.parts


def managed_preflight(present):
    """Любая системная policy — неизвестное переопределение, запуск запрещён."""
    return not present


def process_identity(pid):
    """start ticks плюс state защищают от чужого переиспользованного PID."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "start_ticks": fields[19], "state": fields[0]}
    except FileNotFoundError:
        return None


def process_config_projection(raw):
    """Из собственного environ выходят только пути, не auth или loader значения."""
    return {key.decode(): value.decode() for item in raw.split(b"\x00")
            for key, sep, value in [item.partition(b"=")]
            if sep and key in (b"HOME", b"CLAUDE_CONFIG_DIR")}


def live_snapshot(config, work, expected_pid):
    """Снимается при живом Claude: реестр, ключ и SO_PEERCRED без кадров."""
    rows = []
    for path in sorted((config / "sessions").glob("*.json")):
        registry = json.loads(path.read_text())
        if registry.get("cwd") != str(work):
            continue
        if registry.get("pid") != expected_pid:
            raise RuntimeError("own registry PID differs from own tmux pane; snapshot refused")
        identity_before = process_identity(expected_pid)
        config_env = process_config_projection(Path(f"/proc/{expected_pid}/environ").read_bytes())
        identity_after = process_identity(expected_pid)
        if (not identity_before or not identity_after or
                identity_before["start_ticks"] != identity_after["start_ticks"]):
            raise RuntimeError("own process changed during config snapshot")
        socket_path = registry.get("messagingSocketPath")
        present = bool(socket_path and Path(socket_path).exists())
        peer, peer_error = None, None
        if present:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(1)
                    connection.connect(socket_path)
                    pid, uid, _ = struct.unpack("3i", connection.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                    peer = process_identity(pid)
                    if peer:
                        peer["uid"] = uid
            except OSError as e:
                peer_error = type(e).__name__
        # Читаются только ключи собственного временного home; токен не сохраняется.
        keys = list((config / "sessions").glob(str(registry.get("pid")) + ".*.key"))
        for key_path in keys or [None]:
            key = json.loads(key_path.read_text()) if key_path else {}
            row = snapshot_projection(registry, key, config, peer, present)
            row["process_config"] = config_env
            row["process_config_matches"] = config_env.get("CLAUDE_CONFIG_DIR") == str(config)
            row["registry_file"] = path.name
            row["key_file"] = key_path.name if key_path else None
            row["process"] = process_identity(registry.get("pid"))
            row["socket_peer_error"] = peer_error
            rows.append(row)
    return rows


def model_hook_projection(raw, now_ns):
    """Никаких сырых prompts/paths/tokens: только узкие поля и время приёмника."""
    if not isinstance(raw, bytes) or not 0 < len(raw) < 4096:
        return None
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate hook field")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("nonfinite hook value")
    try:
        body = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(body, dict) or type(now_ns) is not int or now_ns < 0:
        return None
    event, sid = body.get("hook_event_name"), body.get("session_id")
    if (event not in ("PostModelSwitch", "SessionStart", "UserPromptSubmit")
            or not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid)):
        return None
    to_model = from_model = None
    if event == "PostModelSwitch":
        to_model, from_model = body.get("to_model"), body.get("from_model")
        if not isinstance(to_model, str):
            return None
        for value in (to_model, from_model):
            if value is not None and (not isinstance(value, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}", value)):
                return None
    return dict(event=event, session_id=sid, to_model=to_model, from_model=from_model, at_ns=now_ns)


def bare_skips_hooks(help_text):
    """Только явный контракт установленного CLI, не догадка по пустому sink."""
    option = re.search(r"^\s+--bare\b(.*?)(?=^\s+--[a-z]|\Z)", help_text, re.MULTILINE | re.DOTALL)
    return bool(option and "skip hooks" in " ".join(option[1].lower().split()))


def probe_settings(hook_output=None):
    """Opt-in fixed command: argv не берётся ни из stdin, ни из окружения."""
    settings = {"permissions": {"defaultMode": "default"}}
    if hook_output is not None:
        command = shlex.join([str(Path(sys.executable).resolve()), "-I", str(Path(__file__).resolve()),
                              "--capture-model-hook", str(hook_output)])
        settings["hooks"] = {event: [{"hooks": [{"type": "command", "command": command, "timeout": 2}]}]
            for event in ("SessionStart", "UserPromptSubmit", "PostModelSwitch")}
    return settings


def capture_model_hook(output, raw, now_ns):
    """Существующий private fixture sink, не authority file и не create API.

    Отсутствующий файл — ещё не активированный producer; ранний hook теряется.
    Ровно 4096 байт тоже отбрасываются: без чтения 4097-го размер не доказан.
    """
    row = model_hook_projection(raw, now_ns)
    if row is None:
        return False
    try:
        fd = os.open(output, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            return False
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        data = (json.dumps(row, ensure_ascii=True, allow_nan=False) + "\n").encode()
        if os.fstat(fd).st_size + len(data) > 65536:
            return False
        return os.write(fd, data) == len(data)
    finally:
        os.close(fd)


def _model_hook_stdin():
    """EOF и размер ограничены; зависший producer не держит CLI открытым."""
    chunks = bytearray()
    deadline = time.monotonic() + .2
    while len(chunks) < 4096:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
            return b""
        chunk = os.read(0, 4096 - len(chunks))
        if not chunk:
            return bytes(chunks)
        chunks.extend(chunk)
    return b""


def model_switch_observed(events, before, after, started_ns, ended_ns, active_ns, target, previous):
    """Нужен switch в своём окне, не запрос/сокет/statusline или старый hook."""
    if (not live_contract_observed(before) or not live_contract_observed(after)
            or not 0 <= active_ns <= started_ns < ended_ns):
        return False
    first, last = before[0], after[0]
    identity = lambda row: (row["pid"], row["sessionId"], row["process"]["start_ticks"],
                            row.get("config_home"), row.get("messagingSocketPath"))
    if identity(first) != identity(last):
        return False
    matches = [event for event in events if event.get("event") == "PostModelSwitch"
        and event.get("session_id") == first["sessionId"]
        and event.get("to_model") == target and event.get("from_model") == previous
        and type(event.get("at_ns")) is int and started_ns <= event["at_ns"] <= ended_ns]
    return len(matches) == 1


def probe_command(claude, settings, session_id, messaging_socket):
    """Явный inbox нужен для живого контракта, bare isolation сохраняется."""
    return [claude, "--bare", "--model", MODEL, "--session-id", session_id,
            "--messaging-socket-path", messaging_socket,
            "--permission-mode", "default", "--tools", "Bash,AskUserQuestion",
            "--settings", str(settings), "--setting-sources", "user",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']


def history_projection(body, returncode):
    """Штатный отказ адаптера сохраняет причину, records и cursor не копируются."""
    if returncode or "error" in body:
        return {"returncode": returncode, "outcome": "unknown",
                "error": body.get("error", "history adapter failed without a reason"),
                "header_fields": sorted(body)}
    return {"returncode": returncode, "outcome": "observed", "header_fields": sorted(body),
            "session_id": body.get("session_id"), "records_count": len(body.get("records", [])),
            "record_types": sorted({r.get("type", "unknown") for r in body.get("records", [])}),
            "reset": body.get("reset"), "has_more": body.get("has_more"), "partial": body.get("partial"),
            "cursor_present": bool(body.get("cursor")), "epoch_present": bool(body.get("epoch"))}


def history_snapshot(module, work, env):
    """Явный локальный stdlib адаптер; приватные records/cursor не пишутся."""
    before = Path(module).read_bytes()
    source = {"sha256": hashlib.sha256(before).hexdigest(), "size": len(before)}
    result = subprocess.run([sys.executable, "-I", module, "history", str(work)],
        env=env, cwd="/", capture_output=True, text=True, timeout=10)
    source["unchanged_during_call"] = Path(module).read_bytes() == before
    try:
        body = json.loads(result.stdout)
    except ValueError:
        return {"returncode": result.returncode, "outcome": "unknown", "error": "invalid history JSON"}
    summary = {**history_projection(body, result.returncode), "source": source}
    if not source["unchanged_during_call"]:
        summary.update(outcome="unknown", error="history source changed during call")
    return summary


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
        self.auth_checks = []

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
        authenticated = fixture_auth_ok(dict(self.headers))
        self.server.auth_checks.append(authenticated)
        if not authenticated:
            self.server.errors.append("fixture request rejected: dummy API key gate failed")
            self.send_error(403, "Dummy API key required")
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
        self.owned = {}
        self.cleanup = {"verified": False, "leftovers": [], "errors": []}
        self.server_identity = None
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
            self.server_identity = process_identity(int(self.call("display-message", "-p", "#{pid}")))
            self.call("set-option", "-g", "escape-time", "0")
            self.call("set-option", "-g", "focus-events", "on")
            self.call("set-option", "-g", "status", "off")
            def controlling_tty():
                os.setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            self.proc = subprocess.Popen([tmux, "-S", label, "-f", "/dev/null", "attach-session",
                                          "-t", "probe"], env=env, cwd=work,
                                         stdin=slave, stdout=slave, stderr=slave,
                                         preexec_fn=controlling_tty)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(slave)

    def call(self, *args):
        r = subprocess.run([self.tmux, "-S", self.label, "-f", "/dev/null", *args],
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

    def track_owned(self):
        """Сначала доказанная цепочка от pane; PID из файлов не даёт права kill."""
        pane = int(self.call("display-message", "-p", "-t", "probe", "#{pane_pid}"))
        processes = {}
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                raw = path.read_text()
                fields = raw.rsplit(")", 1)[1].split()
                processes[int(path.parent.name)] = (int(fields[1]), fields[19], raw.split("(", 1)[1].rsplit(")", 1)[0])
            except (FileNotFoundError, ProcessLookupError):
                continue
        descendants = {pane}
        while True:
            children = {pid for pid, (parent, _, _) in processes.items() if parent in descendants}
            if children <= descendants:
                break
            descendants |= children
        for pid in descendants:
            if pid in processes:
                _, start, command = processes[pid]
                # Только Claude, собственная оболочка и sleep этого сценария.
                if pid == pane or command in ("claude", "bash", "sh", "sleep"):
                    self.owned[pid] = {"pid": pid, "start_ticks": start, "command": command}

    def remaining(self):
        """Zombie тоже остаётся процессом: сообщаем, а не выдаём за cleanup."""
        leftovers = []
        for pid, owned in self.owned.items():
            identity = process_identity(pid)
            if identity and identity["start_ticks"] == owned["start_ticks"]:
                leftovers.append({**owned, "state": identity["state"]})
        return leftovers

    def close(self):
        errors = []
        try:
            self.track_owned()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
            errors.append("cleanup ownership capture failed: " + str(e))
        stopped = subprocess.run([self.tmux, "-S", self.label, "-f", "/dev/null", "kill-server"],
                                 env=self.env, capture_output=True, timeout=10)
        kill_succeeded = stopped.returncode == 0
        server_after = process_identity(self.server_identity["pid"]) if self.server_identity else None
        until = time.monotonic() + 2
        while (server_after and self.server_identity and
               server_after["start_ticks"] == self.server_identity["start_ticks"] and time.monotonic() < until):
            time.sleep(.1)
            server_after = process_identity(self.server_identity["pid"])
        server_stopped = bool(kill_succeeded and self.server_identity and not (server_after and
            server_after["start_ticks"] == self.server_identity["start_ticks"]))
        if not server_stopped:
            errors.append("own tmux kill-server failed")
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        # Проверка start непосредственно перед сигналом; чужие PID не затрагиваются.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for owned in self.remaining():
                identity = process_identity(owned["pid"])
                if identity and identity["start_ticks"] == owned["start_ticks"] and identity["state"] != "Z":
                    try:
                        os.kill(owned["pid"], sig)
                    except ProcessLookupError:
                        pass
            until = time.monotonic() + 2
            while self.remaining() and time.monotonic() < until:
                time.sleep(.1)
        leftovers = self.remaining()
        verified = cleanup_verdict(server_stopped, leftovers) and not errors
        self.cleanup = {"verified": verified, "kill_server_succeeded": kill_succeeded,
                        "server_identity": self.server_identity, "server_after": server_after,
                        "server_pid_start_gone": server_stopped, "owned_processes": list(self.owned.values()), "leftovers": leftovers, "errors": errors}
        os.close(self.master)
        self.frames.close()
        self.inputs.close()
        if not verified:
            raise RuntimeError("own process cleanup not verified; see cleanup evidence")
        return verified


def _run_model_hooks(terminal, fixture, report, output, ready, capture_live, snapshot, turn):
    """Только наблюдения native CLI; sink-активация не подменяет production ACK."""
    details = report["model_hooks"]
    if not live_contract_observed(ready):
        raise RuntimeError("model hook capture requires proven owned live registry/key/socket identity")
    active_ns = time.monotonic_ns()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    details.update(activated_at_ns=active_ns,
        activation="fixture-only sink after live proof; not production HookBridge or registration ACK")
    def events():
        with output.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise RuntimeError("bounded model hook evidence exceeded fixture capacity")
        return [json.loads(line) for line in raw.splitlines()]
    initial_start = time.monotonic_ns()
    initial_answer = turn("after-model", "probe:after-model initial hook control")
    initial_requests = [r for r in fixture.requests if r["kind"] == "after-model"]
    capture_live("hook-initial")
    details["initial_fixture_request"] = dict(fresh_answer_rendered=initial_answer,
        model=initial_requests[-1]["body"].get("model") if initial_requests else None,
        confirmation=False, reason="next fixture request is supporting evidence, not model readiness")
    cases = []
    for name, target, previous in (("same-model", MODEL, MODEL),
            ("change", SECOND_MODEL, MODEL), ("return", MODEL, SECOND_MODEL)):
        before = capture_live("hook-" + name + "-before")
        start_ns = time.monotonic_ns()
        terminal.text("/model " + target)
        terminal.pump(2)
        screen = snapshot("hook-" + name + "-command")
        confirmation = "Switch model?" in screen
        if confirmation:
            terminal.send(b"\r")
            terminal.pump(1)
        request_count = len(fixture.requests)
        answer = turn("after-model", "probe:after-model hook " + name)
        terminal.pump(.5)
        after = capture_live("hook-" + name + "-after")
        end_ns = time.monotonic_ns()
        seen = events()
        window = [event for event in seen if start_ns <= event["at_ns"] <= end_ns]
        requests = [r for r in fixture.requests[request_count:] if r["kind"] == "after-model"]
        request_model = requests[-1]["body"].get("model") if requests else None
        observed = model_switch_observed(seen, before, after, start_ns, end_ns, active_ns, target, previous)
        cases.append(dict(name=name, target=target, previous=previous, started_at_ns=start_ns,
            ended_at_ns=end_ns, events=window, hook_observed=observed, fresh_answer_rendered=answer,
            next_request_model=request_model, switch_confirmation_seen=confirmation,
            before_snapshot="hook-" + name + "-before", after_snapshot="hook-" + name + "-after"))
    seen = events()
    controls = [event for event in seen if event["event"] == "UserPromptSubmit"
        and event["session_id"] == ready[0]["sessionId"] and event["at_ns"] >= initial_start]
    details["events"] = seen
    details["cases"] = cases
    details["initial_readiness"] = "unknown; no readiness receipt or scope retirement is implemented by this probe"
    details["strategy"] = ("Require a genuine launch-scoped causal model query/handshake, or an explicitly authorized "
        "real transition away and back with correlated PostModelSwitch. Do not infer readiness from --model, "
        "statusline, registry/socket liveness, or a next fixture request. Same-model reassertion needs its own hook.")
    unsupported = details["native_bare_skips_hooks"]
    reason = ("installed --help explicitly says --bare skips settings/plugin hooks; isolation retained"
        if unsupported else "no explicit hook-suppression contract; missing hook evidence remains unknown")
    details["native_capability_reason"] = reason
    report["scenarios"]["model-hooks-control"] = dict(outcome=outcome(bool(controls), unsupported),
        evidence=dict(projected_prompt_events=len(controls), initial_snapshot="hook-initial", reason=reason))
    for case in cases:
        # Пустой sink не доказывает отсутствие native события. Отказ лишь из
        # явного --bare contract; same-model в обычном режиме не проверен.
        report["scenarios"]["model-hooks-" + case["name"]] = dict(
            outcome=outcome(case["hook_observed"], unsupported), evidence={**case, "reason": reason})
    report["scenarios"]["live-registry-contract"] = dict(outcome=outcome(all(
        live_contract_observed(rows) for rows in report["live_snapshots"].values())),
        evidence=dict(snapshots=list(report["live_snapshots"])))


def run(root, timeout=30, history_module=None, model_hooks=False):
    """Возвращает независимые наблюдения и пути доказательств; ничего не печатает."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.chmod(root, 0o700)
    # Короткий runtime вне репозитория: длина output не влияет на Unix sockets.
    runtime = Path(tempfile.mkdtemp(prefix="mp-", dir="/tmp"))
    home, work = runtime / "h", runtime / "w"
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
    hook_output = root / "model-hooks.jsonl" if model_hooks else None
    hook_prepared_ns = time.monotonic_ns()
    settings.write_text(json.dumps(probe_settings(hook_output)))
    server_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    server_thread.start()
    label = tmux_socket(runtime)
    session_id = str(uuid.uuid4())
    report = {"fixture_backed": True, "paid_provider_tested": False,
              "tmux_socket": label, "requested_session_id": session_id,
              "runtime_path": str(runtime), "home": str(home), "work": str(work),
              "live_snapshots": {}, "history_module": history_module, "history": {},
              "report_path": str(root / "report.json"), "scenarios": {},
              "terminal_path": str(root / "terminal.jsonl"), "errors": []}
    if model_hooks:
        report["model_hooks"] = dict(output_path=str(hook_output), prepared_at_ns=hook_prepared_ns,
            source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            projection_fields=["event", "session_id", "to_model", "from_model", "at_ns"],
            input_limit_bytes=4096, at_limit_input_dropped=True,
            preactivation="sink absent; startup hooks are dropped, never treated as readiness")
    terminal = None
    def record(name, observed, evidence, unsupported=False):
        report["scenarios"][name] = {"outcome": outcome(observed, unsupported), "evidence": evidence}
    def snapshot(name):
        screen = terminal.screen()
        (root / (name + ".txt")).write_text(screen)
        return screen
    def capture_live(name, with_history=False):
        rows = live_snapshot(home / ".claude", work, terminal.identity()["pid"])
        report["live_snapshots"][name] = rows
        (root / (name + "-registry.json")).write_text(json.dumps(rows, indent=2) + "\n")
        if with_history and history_module:
            report["history"][name] = history_snapshot(history_module, work, env)
        return rows
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
        managed = [str(path) for path in (Path("/etc/claude-code/managed-settings.json"),
            Path("/etc/claude-code/managed-settings.d"), Path("/etc/claude-code/managed-mcp.json"))
            if path.exists() or path.is_symlink()]
        report["managed_settings_absent"] = managed_preflight(managed)
        if not report["managed_settings_absent"]:
            raise RuntimeError("managed settings could override probe isolation; Claude not started")
        if history_module:
            if (not valid_history_argument(history_module) or Path(history_module).is_symlink()
                    or not stat.S_ISREG(Path(history_module).stat().st_mode)):
                raise ValueError("history module requires an explicit regular local session.py source")
        git = shutil.which("git")
        if not git:
            raise RuntimeError("chat probe requires git for the transport preflight")
        guard = subprocess.run([git, "ls-remote", "git@github.com:anthropics/claude-plugins-official.git"],
            env=env, cwd=work, capture_output=True, timeout=10)
        report["git_ssh_transport_blocked"] = (guard.returncode != 0 and
            b"transport 'ssh' not allowed" in guard.stderr)
        if not report["git_ssh_transport_blocked"]:
            raise RuntimeError("git SSH transport guard not confirmed; Claude not started")
        metadata = subprocess.run([git, "rev-parse", "--git-dir"], env=env, cwd=work,
            capture_output=True, timeout=10)
        report["caller_git_metadata_absent"] = metadata.returncode != 0
        if not report["caller_git_metadata_absent"]:
            raise RuntimeError("probe workdir inherited git metadata; Claude not started")
        report["claude_version"] = subprocess.run([claude, "--version"], env=env, cwd=work,
            capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        help_text = subprocess.run([claude, "--help"], env=env, cwd=work,
            capture_output=True, text=True, timeout=10, check=True).stdout
        (root / "claude-help.txt").write_text(help_text)
        if model_hooks:
            report["model_hooks"]["native_bare_skips_hooks"] = bare_skips_hooks(help_text)
        report["claude_executable"] = claude
        if "--bare" not in help_text:
            raise RuntimeError("installed claude lacks --bare isolation; probe not started")
        report["isolation_mode"] = "bare; explicit temporary settings; default permissions"
        report["limitations"] = ["Fixture-backed responses only; no paid provider evidence.",
            "Bare mode skips discovery/keychain and may expose fewer tools than normal mode.",
            "External HTTP/HTTPS proxy attempts are refused and git SSH transport is disabled; not an OS network sandbox.",
            "Managed settings presence refuses launch; no global configuration or real credentials are read.",
            "Temporary runtime is retained privately for inspection; projected key evidence excludes peerToken.",
            "Linux /proc identity and English UI observations; unknown means insufficient evidence."]
        command = probe_command(claude, settings, session_id, str(runtime / "c.sock"))
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
        terminal.track_owned()
        ready_registry = capture_live("ready")
        if model_hooks:
            _run_model_hooks(terminal, fixture, report, hook_output, ready_registry, capture_live, snapshot, turn)
            return report
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
        model_registry = capture_live("after-model", with_history=True)
        record("model-identity", bool(ready_registry) and bool(model_registry) and
               [r["sessionId"] for r in ready_registry] == [r["sessionId"] for r in model_registry] and model_answer and bool(model_requests) and
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
        clear_registry = capture_live("after-clear", with_history=True)
        report["registry_session_id_changed"] = (bool(model_registry) and bool(clear_registry) and
            {r["sessionId"] for r in model_registry} != {r["sessionId"] for r in clear_registry})
        history = json.dumps(clear_requests[-1]["body"].get("messages", [])) if clear_requests else ""
        record("live-registry-contract", all(live_contract_observed(rows) for rows in
               (ready_registry, model_registry, clear_registry)),
               {"snapshots": ["ready-registry.json", "after-model-registry.json", "after-clear-registry.json"]})
        history_calls = report["history"]
        history_observed = bool(history_module and model_registry and clear_registry
            and report["registry_session_id_changed"] and all(
                history_calls.get(name, {}).get("outcome") == "observed"
                and history_calls[name].get("session_id") == rows[0]["sessionId"] for name, rows in
                (("after-model", model_registry), ("after-clear", clear_registry))))
        record("history-adapter", history_observed,
               {"calls": history_calls, "selected": bool(history_module)})
        record("clear-identity", report["registry_session_id_changed"] and clear_answer and terminal.identity() == before and
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
        terminal.track_owned()
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
        report["fixture_auth"] = {"requests_checked": len(fixture.auth_checks),
            "all_dummy_key": bool(fixture.auth_checks) and all(fixture.auth_checks)}
        entries = list(home.rglob("*"))
        unexpected = unexpected_home_paths([str(p.relative_to(home)) for p in entries],
            directories=[str(p.relative_to(home)) for p in entries if p.is_dir() and not p.is_symlink()])
        report["unexpected_home_paths"] = unexpected
        if unexpected:
            report["errors"].append("unexpected git/marketplace/credential touches in own temporary home")
        report["cleanup"] = {"own_processes": terminal.cleanup if terminal else {"verified": False},
                             "own_tmux_server_stopped": tmux_stopped,
                             "fixture_listener_closed": fixture.socket.fileno() == -1,
                             "fixture_thread_stopped": not server_thread.is_alive()}
        names = (("model-hooks-control", "model-hooks-same-model", "model-hooks-change", "model-hooks-return",
                  "live-registry-contract") if model_hooks else
                 ("terminal-framing", "foreground-slash", "model-identity", "clear-identity",
                  "live-registry-contract", "history-adapter", "stream-interrupt",
                  "permissions-default", "bash-interrupt", "ask-user-question"))
        for name in names:
            if name not in report["scenarios"]:
                record(name, False, {"reason": "scenario not reached"})
        (root / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


if __name__ == "__main__":
    # Единственный внутренний entrypoint fixed command, не запускатель команд.
    if len(sys.argv) != 3 or sys.argv[1] != "--capture-model-hook":
        sys.exit("fixed model hook capture arguments required")
    try:
        capture_model_hook(sys.argv[2], _model_hook_stdin(), time.monotonic_ns())
    except OSError as error:
        sys.exit("private model hook capture failed: " + type(error).__name__)
