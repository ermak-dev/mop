"""Локальная связка реальных адаптеров; не обычная CI-проверка и не production."""
import argparse
import asyncio
import base64
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import site
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# -I не читает .pth; установленные зависимости берём из site, не evidence/cwd.
sys.path.append(site.getusersitepackages())
from mop.client import chatprobe as probe

def verdict(facts, unsupported=False):
    return "unsupported" if unsupported else "observed" if facts and all(v is True for v in facts.values()) else "unknown"

def source_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def absolute(value):
    if not Path(value).is_absolute():
        raise argparse.ArgumentTypeError("trusted checkout root must be absolute")
    return Path(value).resolve()

def bounded(value):
    number = int(value)
    if not 1 <= number <= 30:
        raise argparse.ArgumentTypeError("timeout must be 1..30 seconds")
    return number

def parser():
    p = argparse.ArgumentParser(description="Local fixture-backed runtime integration; no paid provider")
    p.add_argument("--output", type=Path, default=Path("/tmp") / ("chat-smoke-" + uuid.uuid4().hex))
    p.add_argument("--timeout", type=bounded, default=20)
    for kind in ("node", "history", "web"):
        p.add_argument("--" + kind + "-root", type=absolute, default=ROOT)
    p.add_argument("--keep-serving", action="store_true")
    return p

def body_history(source, work, env, cursor=None):
    argv = [sys.executable, "-I", str(source), "history", str(work)]
    if cursor is not None:
        argv += ["--cursor", cursor]
    got = subprocess.run(argv, env=env, cwd="/", capture_output=True, text=True, timeout=10)
    result = json.loads(got.stdout)
    if got.returncode and not result.get("error"):
        raise RuntimeError("history subprocess failed without verdict")
    return result

class Local:
    """Фикстуры транспорта/хранилищ; identity -- реальный body subprocess."""
    name = "pu-smoke-1"

    def __init__(self, node, terminal, env, work, history, loop):
        self.node, self.terminal, self.env = node, terminal, env
        self.work, self.history, self.loop = work, history, loop
        self.rpc_log, self.audits, self.write_calls = [], [], []
        self.registry = node.Registry(self.identity, self.owner, self.spawn)

    async def identity(self, name):
        if name != self.name:
            raise ValueError("fixture target refused")
        script = self.node.probe_script(str(self.history), str(self.work), name)
        proc = await asyncio.create_subprocess_exec("/bin/sh", "-c", "exec " + script, env=self.env,
                    cwd=self.work, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 10)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode:
            # Локальное свидетельство приватно, не public API.
            raise ValueError("actual body identity failed: " + err.decode(errors="replace")[-1500:])
        return json.loads(out)

    async def owner(self, name, owner, **flags):
        # Клон фикстуры уже принадлежит probe. claim/install Git покрыты другими checks.
        if name != self.name or owner != "probe":
            raise ValueError("fixture clone belongs to probe")

    async def spawn(self, name, mode, cols, rows, on_data, on_exit):
        argv = self.node.driver.Tmux(name).attach_argv(read_only=mode == "ro")
        process = await self.node.PTY.spawn(argv, cols, rows, on_data, on_exit)
        actual_write = process.write
        def observed_write(data):
            self.write_calls.append({"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            return actual_write(data)
        process.write = observed_write
        return process

    def transport(self, node, verb, **fields):
        self.rpc_log.append({"verb": verb, "project": fields.get("project")})
        if node != "fixture" or fields.get("name") != self.name:
            raise ValueError("fixture transport target refused")
        if verb == "state":
            return {"clone": {"cur": "master", "def": "master", "dirty": 0, "ahead": 0, "owner": {"user": "probe", "at": time.time()}}}
        if verb == "history":
            return body_history(self.history, self.work, self.env, fields.get("cursor"))
        if fields.pop("project", None) != "admin":
            raise ValueError("fixture terminal private subject required")
        method = getattr(self.registry, verb.removeprefix("term_"))
        try:
            self.node.validate(verb, dict(fields, verb=verb))
            result = asyncio.run_coroutine_threadsafe(method(**fields), self.loop).result(20)
            if verb == "term_write" and getattr(self, "drop_receipt", False):
                self.drop_receipt = False
                raise TimeoutError("fixture lost the receipt after real PTY write")
            return result
        except ValueError as e:
            return {"error": str(e)}

def run(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o700)
    runtime = Path(tempfile.mkdtemp(prefix="ms-", dir="/tmp"))
    runtime.chmod(0o700)
    home, work = runtime / "h", runtime / "w"
    work.mkdir()
    (home / "tmp").mkdir(parents=True)
    config = home / ".claude"
    config.mkdir()
    fixture = probe.Fixture(work)
    env = probe.isolated_env(os.environ, home, f"http://127.0.0.1:{fixture.server_port}")
    os.environ.clear()
    os.environ.update(env)
    report = {"fixture_backed": True, "paid_provider_tested": False, "runtime": str(runtime),
              "report_path": str(root / "report.json"), "scenarios": {}, "errors": [],
              "limitations": ["No container, pool, NATS, or paid provider evidence.",
                  "Fixture clone already owned by probe; no Git claim/identity installation tested.",
                  "Node RPC private-subject dispatch covered by separate pure checks, not this local transport.",
                  "Bare CLI, default permissions; raw PTY is trusted admin control, native tmux prefix is not filtered.",
                  "Proxy and SSH guards are not an OS network sandbox."]}
    terminal = local = http = None
    thread = webthread = loopthread = None
    loop = asyncio.new_event_loop()
    sources, manifest = {}, {}
    def record(name, **facts):
        unsupported = facts.pop("unsupported", False)
        report["scenarios"][name] = {"outcome": verdict(facts, unsupported), "evidence": facts}
        print(json.dumps({"scenario": name, **report["scenarios"][name]}), flush=True)
    def save():
        (root / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    try:
        for kind, checkout, relative in (("node", args.node_root, "mop/node/terminal.py"),
                ("history", args.history_root, "mop/session.py"), ("wire", ROOT, "mop/cli/server/web.py"),
                ("runner", ROOT, "tests/_chat_smoke.py")):
            path = checkout / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError("trusted regular prerequisite source required: " + str(path))
            revision = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True).stdout.strip()
            sources[kind] = {"path": str(path), "sha256": source_hash(path), "revision": revision}
        sources["web"] = {"path": str(args.web_root / "web/dist/index.html"),
            "sha256": source_hash(args.web_root / "web/dist/index.html"), "revision": subprocess.run(
            ["git", "-C", str(args.web_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()}
        report["sources"] = sources
        managed = [p for p in (Path("/etc/claude-code/managed-settings.json"),
            Path("/etc/claude-code/managed-settings.d"), Path("/etc/claude-code/managed-mcp.json")) if p.exists() or p.is_symlink()]
        if managed:
            raise RuntimeError("managed settings present; CLI not started")
        guard = subprocess.run(["git", "ls-remote", "git@github.com:anthropics/claude-plugins-official.git"],
                env=env, cwd=work, capture_output=True, timeout=10)
        if not (guard.returncode and b"transport 'ssh' not allowed" in guard.stderr):
            raise RuntimeError("Git SSH guard not confirmed")
        metadata = subprocess.run(["git", "rev-parse", "--git-dir"], env=env, cwd=work, capture_output=True, timeout=10)
        if not metadata.returncode:
            raise RuntimeError("fixture workdir inherited Git metadata")
        claude, tmux = shutil.which("claude"), shutil.which("tmux")
        version = subprocess.run([claude, "--version"], env=env, capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        if not version.startswith("2.1.296 "):
            raise RuntimeError("requires installed Claude 2.1.296; found " + version)
        report["claude_version"] = version
        report["preflight"] = {"managed_settings_absent": True, "git_ssh_blocked": True, "caller_git_metadata_absent": True}
        (config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True,
            "theme": "dark", "customApiKeyResponses": {"approved": ["chat-probe-dummy-key"], "rejected": []}}))
        settings = config / "settings.json"
        settings.write_text(json.dumps({"permissions": {"defaultMode": "default"}}))
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        class OwnTerminal(probe.Terminal):
            renamed = False
            def call(self, *args):
                return super().call(*(Local.name if self.renamed and arg == "probe" else arg for arg in args))
        terminal = OwnTerminal(tmux, probe.tmux_socket(runtime), env, work,
            probe.probe_command(claude, settings, str(uuid.uuid4()), str(runtime / "c.sock")), root)
        terminal.call("set-option", "-g", "remain-on-exit", "on")
        terminal.pump(3)
        if "trust this" in terminal.screen().lower():
            terminal.send(b"\x1b[B\r")
            terminal.pump(2)
        terminal.call("rename-session", "-t", "probe", Local.name)
        terminal.renamed = True
        (root / "ready.txt").write_text(terminal.screen())
        report["ready_registry"] = probe.live_snapshot(config, work, terminal.identity()["pid"])
        terminal.track_owned()
        spec = importlib.util.spec_from_file_location("mop.node.terminal", sources["node"]["path"])
        node = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = node
        spec.loader.exec_module(node)
        # Настоящий driver argv, но адрес собственного -S сокета и собственного probe pane.
        original_tmux = node.driver.Tmux
        class OwnTmux(original_tmux):
            def __init__(self, name):
                super().__init__(name)
                self.base = "tmux -S " + shlex.quote(terminal.label)
            def attach_argv(self, read_only=False):
                argv = super().attach_argv(read_only)
                i = argv.index("-L")
                argv[i:i+2] = ["-S", terminal.label]
                return argv
        node.driver.Tmux = OwnTmux
        loopthread = threading.Thread(target=loop.run_forever, daemon=True)
        loopthread.start()
        local = Local(node, terminal, env, work, Path(sources["history"]["path"]), loop)
        identity = asyncio.run_coroutine_threadsafe(local.identity(local.name), loop).result(15)
        report["body_identity"] = identity
        from mop.server import websession
        from mop.server.terminal import TerminalBridge
        handler = importlib.import_module("mop.cli.server.web")
        who = SimpleNamespace(login="probe", role="admin")
        websession.provider = lambda: SimpleNamespace(lookup=lambda login: who if login == "probe" else None)
        handler.SESSIONS = websession.SessionStore(path=str(runtime / "sessions.json"))
        token = handler.SESSIONS.open(who)
        handler.COLLECTOR.rows = [handler.state.PuppetRow(local.name, "fixture", "running",
            "waiting for input", "waiting", "probe", llm=probe.MODEL, origin="/fixture/smoke.git")]
        handler.COLLECTOR.at = time.time()
        handler.TERMINALS = TerminalBridge(local.transport, lambda name: {"node": "fixture", "project": "smoke"}, audit=local.audits.append)
        handler.bus.request = local.transport
        handler.DIST = str(args.web_root / "web/dist")
        handler.PAGE = str(Path(handler.DIST) / "index.html")
        files = [Path(m.__file__) for name, m in list(sys.modules.items())
                 if name.startswith("mop.") and getattr(m, "__file__", "").endswith(".py")]
        files += list((args.web_root / "web/dist").rglob("*"))
        manifest = {str(p): source_hash(p) for p in files if p.is_file()}
        report["code_manifest"] = manifest
        report["runner_identity"] = probe.process_identity(os.getpid())
        http = handler.ThreadingHTTPServer(("127.0.0.1", 0), handler.Handler)
        http.daemon_threads = False
        origin = f"http://127.0.0.1:{http.server_port}"
        # Только локальная фикстура, сам origin gate не отключён.
        websession.public_origin = lambda get: origin
        webthread = threading.Thread(target=http.serve_forever, daemon=True)
        webthread.start()
        report["private_browser"] = {"url": origin, "cookie_name": websession.COOKIE, "token": token,
                                     "target": local.name, "web_root": str(args.web_root)}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(route, data=None, authenticated=True, supplied_origin=origin):
            headers = {"Origin": supplied_origin, "Content-Type": "application/json"}
            if authenticated:
                headers["Cookie"] = websession.COOKIE + "=" + token
            req = urllib.request.Request(origin + route, headers=headers, data=json.dumps(data).encode() if data is not None else None)
            try:
                response = opener.open(req, timeout=22)
            except urllib.error.HTTPError as e:
                response = e
            with response:
                return response.status, json.loads(response.read())
        base = "/api/puppet/" + local.name
        def call(route, data=None):
            code, body = request(base + route, data)
            if code != 200:
                raise RuntimeError(f"actual HTTP {route}: {code} {body}")
            return body
        def opened(mode):
            return call("/term", {"mode": mode, "cols": 140, "rows": 40})["tid"]
        seq = 0
        def send(data):
            nonlocal seq
            seq += 1
            return call(f"/term/{rw}/input", {"seq": seq, "data": base64.b64encode(data).decode()})
        def text(value):
            send(probe.paste(value))
            terminal.pump(.2)
            send(b"\r")
        def turn(kind, value=None):
            since = sum(r["kind"] == kind for r in fixture.requests)
            text(value or "probe:" + kind)
            return terminal.wait(lambda: probe.completion_seen(fixture.requests, kind, since, terminal.screen()), args.timeout)
        pool_code, pool = request("/api/pool")
        record("browser-roster", actual_snapshot=pool_code == 200 and bool(pool.get("projects")),
               actual_card=request(base)[0] == 200)
        record("http-auth-gates", unauthenticated=request(base + "/history", authenticated=False)[0] == 401,
               foreign_origin=request(base + "/term", {"mode": "ro", "cols": 140, "rows": 40}, supplied_origin="http://foreign.invalid")[0] == 403)
        ro = opened("ro")
        frame = call(f"/term/{ro}/read?cursor=0&wait=1")
        (root / "ro-frame.json").write_text(json.dumps(frame))
        record("ro-raw-frame", ansi=b"\x1b[" in base64.b64decode(frame["data"]),
               write_403=request(base + f"/term/{ro}/input", {"seq": 1, "data": "YQ=="})[0] == 403)
        call(f"/term/{ro}/close", {})
        record("close-preserves-cli", pid_start_same=terminal.identity()["pid"] == identity["pid"],
               messaging_socket_live=probe.live_contract_observed(probe.live_snapshot(config, work, identity["pid"])))
        rw = opened("rw")
        record("one-writer", conflict_409=request(base + "/term", {"mode": "rw", "cols": 140, "rows": 40})[0] == 409)
        unicode = "probe:framing\nsecond line Проба"
        completed = turn("framing", unicode)
        records = [r for r in fixture.requests if r["kind"] == "framing"]
        record("utf8-input", completed=completed, exact=bool(records) and unicode in
               json.dumps(records[-1]["body"]["messages"], ensure_ascii=False).replace("\\n", "\n"))
        history = call("/history")
        (root / "history-before.json").write_text(json.dumps(history, ensure_ascii=False))
        record("http-history", same_sid=history["session_id"] == identity["sessionId"], records=bool(history["records"]))
        count = len(fixture.requests)
        text("/help")
        terminal.pump(2)
        screen = terminal.screen()
        (root / "help.txt").write_text(screen)
        record("native-slash", help_ui="Help  General" in screen, no_model_call=len(fixture.requests) == count)
        send(b"\x1b")
        terminal.pump(.5)
        text("/clear")
        terminal.pump(2)
        (root / "clear.txt").write_text(terminal.screen())
        # clear меняет identity: прежний attach более не вправе вводить.
        old = request(base + f"/term/{rw}/input", {"seq": seq+1, "data": "YQ=="})
        record("clear-revokes-old-writer", refused=old[1].get("outcome") == "refused")
        call(f"/term/{rw}/close", {})
        rw, seq = opened("rw"), 0
        completed = turn("after-clear")
        after = call("/history?" + urllib.parse.urlencode({"cursor": history["cursor"]}))
        (root / "history-after.json").write_text(json.dumps(after, ensure_ascii=False))
        record("history-clear-reset", completed=completed, reset=after["reset"] is True,
               new_sid=after["session_id"] != history["session_id"], new_epoch=after["epoch"] != history["epoch"])
        text("probe:stream")
        started = terminal.wait(fixture.stream_started.is_set, args.timeout)
        send(b"\x1b")
        stopped = terminal.wait(fixture.stream_closed.is_set, 10)
        fixture.release.set()
        record("escape-stop-stream", started=started, connection_closed=stopped, next_turn=turn("recover", "probe:recover stream"))
        text("probe:bash")
        terminal.wait(lambda: any(r["kind"] == "bash" for r in fixture.requests), args.timeout)
        shown = terminal.wait(lambda: "Do you want to proceed" in terminal.screen() or (work / "bash.started").exists(), args.timeout)
        screen = terminal.screen()
        (root / "permission.txt").write_text(screen)
        prompted = shown and "Do you want to proceed" in screen
        record("native-permission-dialog", prompted=prompted, not_started=not (work / "bash.started").exists(), unsupported="bash" in fixture.unsupported)
        if prompted:
            send(b"\r")
        started = terminal.wait(lambda: (work / "bash.started").exists(), 10)
        terminal.track_owned()
        send(b"\x1b")
        terminal.pump(2)
        pids = [int((work / f).read_text()) for f in ("bash.pid", "sleep.pid") if (work / f).exists()]
        stopped = terminal.wait(lambda: len(pids) == 2 and all(not Path(f"/proc/{pid}").exists() for pid in pids), 10)
        record("bash-interrupt", started=started, children_gone=stopped, no_finish=not (work / "bash.finished").exists(),
               next_turn=turn("recover", "probe:recover bash"), unsupported="bash" in fixture.unsupported)
        text("probe:ask")
        terminal.wait(lambda: any(r["kind"] == "ask" for r in fixture.requests), args.timeout)
        terminal.pump(1)
        asks = [r for r in fixture.requests if r["kind"] == "ask"]
        record("ask-user-question", advertised=bool(asks) and any(t.get("name") == "AskUserQuestion"
               for t in asks[-1]["body"].get("tools", [])), answer_in_tool_result=False, unsupported="ask" in fixture.unsupported)
        resized = call(f"/term/{rw}/resize", {"cols": 120, "rows": 35})
        terminal.pump(.5)
        record("resize", confirmed=resized.get("ok") is True,
               actual=terminal.call("display-message", "-p", "-t", "probe", "#{pane_width}x#{pane_height}") == "120x35")
        local.drop_receipt = True
        receipt = send(b"z")
        writes = len(local.write_calls)
        same = call(f"/term/{rw}/input", {"seq": seq, "data": "eg=="})
        duplicate = call(f"/term/{rw}/input", {"seq": seq, "data": "eg=="})
        older = call(f"/term/{rw}/input", {"seq": seq-1, "data": "eg=="})
        record("no-replayed-input", lost_ack_unknown=receipt.get("outcome") == "unknown",
               duplicate_receipt=same == duplicate and same.get("outcome") == "written",
               old_unknown=older.get("outcome") == "unknown", no_extra_syscall=len(local.write_calls) == writes)
        send(b"\x7f")
        code, logged_out = request("/api/logout", {})
        code_old, _ = request(base + f"/term/{rw}/input", {"seq": seq+1, "data": "eg=="})
        record("logout-revokes", logout=code == 200 and not logged_out.get("terminal_errors"), old_cookie_401=code_old == 401,
               handles_destroyed=not local.registry.sessions, cli_alive=terminal.identity()["pid"] == identity["pid"])
        token = handler.SESSIONS.open(who)
        report["private_browser"]["token"] = token
        rw, seq = opened("rw"), 0
        if args.keep_serving:
            call(f"/term/{rw}/close", {})
            save()
            print(json.dumps({"status": "private fixture serving", "report_path": report["report_path"]}), flush=True)
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
            rw, seq = opened("rw"), 0
        text("/exit")
        exited = terminal.wait(lambda: probe.process_identity(identity["pid"]) is None, 10)
        post_exit = send(b"z")
        record("no-input-after-exit", exited=exited, refused=post_exit.get("outcome") == "refused")
    except (Exception, KeyboardInterrupt) as e:
        report["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        fixture.release.set()
        if http:
            http.shutdown()
            http.server_close()
            webthread.join(5)
        if local:
            try:
                asyncio.run_coroutine_threadsafe(local.registry.shutdown(), loop).result(10)
            except Exception as e:
                report["errors"].append("registry cleanup: " + str(e))
        if loopthread:
            loop.call_soon_threadsafe(loop.stop)
            loopthread.join(5)
        loop.close()
        if terminal:
            try:
                terminal.close()
            except Exception as e:
                report["errors"].append("owned cleanup: " + str(e))
            report["owned_cleanup"] = terminal.cleanup
        if thread:
            fixture.shutdown()
            thread.join(5)
        fixture.server_close()
        (root / "requests.json").write_text(json.dumps(fixture.requests, ensure_ascii=False, indent=2))
        report["errors"].extend(fixture.errors)
        report["fixture_auth"] = {"count": len(fixture.auth_checks), "all_dummy": bool(fixture.auth_checks) and all(fixture.auth_checks)}
        entries = list(home.rglob("*"))
        unexpected = probe.unexpected_home_paths([str(p.relative_to(home)) for p in entries],
            [str(p.relative_to(home)) for p in entries if p.is_dir() and not p.is_symlink()])
        if unexpected:
            report["errors"].append("unexpected home paths: " + str(unexpected))
        for value in sources.values():
            value["unchanged"] = source_hash(value["path"]) == value["sha256"]
            if not value["unchanged"]:
                report["errors"].append("prerequisite source changed during run")
        report["code_unchanged"] = all(Path(p).is_file() and source_hash(p) == digest for p, digest in manifest.items())
        if not report["code_unchanged"]:
            report["errors"].append("imported source or committed dist changed during run")
        report["listener_cleanup"] = {"fixture_closed": fixture.socket.fileno() == -1,
            "http_closed": http is None or http.socket.fileno() == -1, "fixture_thread_stopped": thread is None or not thread.is_alive(),
            "http_thread_stopped": webthread is None or not webthread.is_alive(),
            "loop_thread_stopped": loopthread is None or not loopthread.is_alive()}
        if not all(report["listener_cleanup"].values()):
            report["errors"].append("fixture listener/thread cleanup not confirmed")
        if local:
            report["rpc_subjects"], report["audit"] = local.rpc_log, local.audits
        save()
    return report

def interrupt(signum, frame):
    raise KeyboardInterrupt

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    report = run(parser().parse_args())
    print(json.dumps({"report_path": report["report_path"], "errors": report["errors"]}), flush=True)
    raise SystemExit(1 if report["errors"] or any(s["outcome"] == "unknown" for s in report["scenarios"].values()) else 0)
