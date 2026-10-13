#!/usr/bin/env python3
"""PTY-реестр без пула (#598).

HYPOTHESIS: attach-процесс не доказывает живую Claude-сессию; повтор RPC
может повторно ввести клавиши, а capture-pane не является потоком байтов.
SOLUTION: приватный ограниченный реестр, строгая identity и одноразовый seq.
Коррекция: foreground -- из /proc, не ioctl чужого tty; сравнение registry
только по identity; staged open не разрешает ввод до commit. Забытый ack
даёт unknown, отсутствующий close -- idempotent, новый writer открывается.
RESULT: red реальной пробы показал ENOTTY без патчей ядра; локальный
stdlib-PTY подтвердил probe независимого non-CTTY caller и статусные гонки
без Claude/пула и без оплачиваемых вызовов.
STATUS: FIXED — see #598
"""
import asyncio
import base64
import importlib.util
import sys
import os
import unittest

import hermetic  # noqa: F401
from _lib import Checks
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

IDENT = dict(pid=123, start="456", sessionId="session-one", pane="%0", tty="/dev/pts/7")


class FakePTY:
    def __init__(self):
        self.input = []
        self.sizes = []
        self.closed = False
        self.fail = False
        self.on_data = None
        self.on_exit = None

    def write(self, data):
        if self.fail:
            raise OSError("secret input must not leak")
        self.input.append(data)
        return len(data)

    def resize(self, cols, rows):
        self.sizes.append((cols, rows))

    async def close(self):
        self.closed = True


class Terminal(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        spec = importlib.util.find_spec("mop.node.terminal")
        self.assertIsNotNone(spec, "#598 requires a node terminal registry, not pane snapshots")
        from mop.node import terminal
        self.t = terminal
        self.identity = dict(IDENT)
        self.denied = False
        self.now = 0
        self.ptys = []
        self.auth = []

        async def probe(name):
            return self.identity

        async def authorize(name, owner, claim=False, require_owner=False):
            self.auth.append((name, owner, claim))
            if self.denied:
                raise ValueError("foreign work")

        async def spawn(name, mode, cols, rows, on_data, on_exit):
            p = FakePTY()
            p.on_data, p.on_exit = on_data, on_exit
            self.ptys.append(p)
            return p

        self.r = terminal.Registry(probe, authorize, spawn, clock=lambda: self.now)

    async def asyncTearDown(self):
        if hasattr(self, "r"):
            await self.r.shutdown()

    async def open(self, mode="rw", name="pu-mop-1", owner="alice"):
        return await self.r.open(name, owner, mode, 80, 24)

    async def test_exact_contract_raw_ring_and_loss(self):
        got = await self.open()
        self.assertEqual(set(got), {"tid", "epoch", "cursor", "mode", "cols", "rows"})
        tid = got["tid"]
        raw = b"\x1b[31m\xff\x00\r\n"
        self.ptys[0].on_data(raw)
        read = await self.r.read("pu-mop-1", "alice", tid, 0, 0)
        self.assertEqual(set(read), {"data", "cursor", "reset", "closed", "exit", "epoch"})
        self.assertEqual(base64.b64decode(read["data"]), raw)
        self.assertFalse(read["reset"])
        self.ptys[0].on_data(b"x" * (512 * 1024 + 9))
        read = await self.r.read("pu-mop-1", "alice", tid, 0, 0)
        self.assertTrue(read["reset"])
        self.assertEqual(len(base64.b64decode(read["data"])), 64 * 1024)
        self.assertLessEqual(len(self.r.sessions[tid].ring.data), 512 * 1024)
        self.assertEqual(read["cursor"], len(raw) + 9 + 64 * 1024)
        with self.assertRaises(ValueError):
            await self.r.read("pu-mop-1", "alice", tid, 10**9, 0)

    async def test_duplicate_and_unknown_are_never_replayed(self):
        tid = (await self.open())["tid"]
        b64 = base64.b64encode(b"\x03").decode()
        got = await self.r.write("pu-mop-1", "alice", tid, 1, b64)
        self.assertEqual(got, {"written": 1, "seq": 1, "outcome": "written"})
        self.assertEqual(await self.r.write("pu-mop-1", "alice", tid, 1, b64), got)
        self.assertEqual(self.ptys[0].input, [b"\x03"])
        with self.assertRaises(ValueError):
            await self.r.write("pu-mop-1", "alice", tid, 1, "eA==")
        with self.assertRaises(ValueError):
            await self.r.write("pu-mop-1", "alice", tid, 3, b64)
        self.ptys[0].fail = True
        got = await self.r.write("pu-mop-1", "alice", tid, 2, b64)
        self.assertEqual(got, {"written": 0, "seq": 2, "outcome": "unknown"})
        self.ptys[0].fail = False
        self.assertEqual(await self.r.write("pu-mop-1", "alice", tid, 2, b64), got)
        self.assertEqual(self.ptys[0].input, [b"\x03"])
        with self.assertRaises(ValueError):
            await self.r.write("pu-mop-1", "alice", tid, 3, b64)

    async def test_identity_clear_exit_shell_and_unknown_close(self):
        for change in ({"sessionId": "after-clear"}, {"pid": 999}, {"start": "457"},
                       {"tty": "/dev/pts/8"}, {"pane": "%1"}, None, {}):
            self.identity = dict(IDENT)
            tid = (await self.open())["tid"]
            self.identity = dict(IDENT, **change) if change else change
            with self.assertRaises(ValueError):
                await self.r.write("pu-mop-1", "alice", tid, 1, "eA==")
            self.assertTrue(self.ptys[-1].closed)
            self.assertEqual(self.ptys[-1].input, [])
            await self.r.close("pu-mop-1", "alice", tid)

    async def test_unknown_exception_and_refusal_reason_survive(self):
        tid = (await self.open())["tid"]
        def broken(data):
            raise RuntimeError("secret raw keys")
        self.ptys[0].write = broken
        got = await self.r.write("pu-mop-1", "alice", tid, 1, "eA==")
        self.assertEqual(got, {"written": 0, "seq": 1, "outcome": "unknown"})
        self.assertTrue(self.ptys[0].closed)
        await self.r.close("pu-mop-1", "alice", tid)
        tid = (await self.open())["tid"]
        self.denied = True
        with self.assertRaisesRegex(ValueError, "^foreign work$"):
            await self.r.write("pu-mop-1", "alice", tid, 1, "eA==")

    async def test_reader_ignores_size_and_cannot_write(self):
        tid = (await self.open("ro"))["tid"]
        with self.assertRaises(ValueError):
            await self.r.write("pu-mop-1", "alice", tid, 1, "eA==")
        self.assertEqual(await self.r.resize("pu-mop-1", "alice", tid, 100, 30), {"ok": True})
        self.assertEqual(self.ptys[0].sizes, [])

    async def test_actor_target_match_on_every_call(self):
        tid = (await self.open())["tid"]
        for name, owner in (("pu-mop-2", "alice"), ("pu-mop-1", "bob")):
            for call in (lambda: self.r.read(name, owner, tid, 0, 0),
                         lambda: self.r.write(name, owner, tid, 1, "eA=="),
                         lambda: self.r.resize(name, owner, tid, 80, 24),
                         lambda: self.r.close(name, owner, tid)):
                with self.assertRaises(ValueError):
                    await call()
        self.assertFalse(self.ptys[0].closed)

    async def test_lost_write_ack_after_expiry_is_unknown_never_redelivered(self):
        # BUG: реестр забывает подтверждение после TTL, отказ не доказывает
        # недоставку старого seq. HYPOTHESIS: unknown для отсутствующего tid.
        tid = (await self.open())["tid"]
        await self.r.write("pu-mop-1", "alice", tid, 1, "eA==")  # ack потерян
        self.now = 31
        await self.r.maintain()
        got = await self.r.write("pu-mop-1", "alice", tid, 1, "eA==")
        self.assertEqual(got, {"written": 0, "seq": 1, "outcome": "unknown"})
        self.assertEqual(self.ptys[0].input, [b"x"])
        fresh = (await self.open())["tid"]
        with self.assertRaises(ValueError):
            await self.r.write("pu-mop-1", "alice", fresh, 2, "eA==")
        self.assertEqual(self.ptys[1].input, [])
        self.assertEqual(await self.r.write("pu-mop-1", "alice", fresh, 1, "eA=="),
                         {"written": 1, "seq": 1, "outcome": "written"})
        self.assertEqual(self.ptys[1].input, [b"x"])
        self.assertEqual(await self.r.write("pu-mop-1", "alice", tid, 1, "eA=="), got)
        self.assertEqual(self.ptys[1].input, [b"x"])

    async def test_old_consumed_sequence_without_receipt_is_unknown(self):
        # Старый ack потерян, новая запись уже сменила единственную квитанцию.
        tid = (await self.open())["tid"]
        await self.r.write("pu-mop-1", "alice", tid, 1, "eA==")
        await self.r.write("pu-mop-1", "alice", tid, 2, "eQ==")
        self.assertEqual(await self.r.write("pu-mop-1", "alice", tid, 1, "eA=="),
                         {"written": 0, "seq": 1, "outcome": "unknown"})
        self.assertEqual(self.ptys[0].input, [b"x", b"y"])
        self.now = 31
        self.assertEqual(await self.r.write("pu-mop-1", "alice", tid, 2, "eQ=="),
                         {"written": 1, "seq": 2, "outcome": "written"})
        self.assertEqual(self.ptys[0].input, [b"x", b"y"])

    async def test_partial_and_eagain_unknown_close_without_reinput(self):
        for failure in (lambda raw: 1, lambda raw: (_ for _ in ()).throw(BlockingIOError())):
            tid = (await self.open())["tid"]
            self.ptys[-1].write = failure
            got = await self.r.write("pu-mop-1", "alice", tid, 1, "eHg=")
            self.assertEqual(got["outcome"], "unknown")
            self.assertTrue(self.ptys[-1].closed)
            self.assertEqual(await self.r.write("pu-mop-1", "alice", tid, 1, "eHg="), got)
            await self.r.close("pu-mop-1", "alice", tid)

    async def test_close_missing_closed_and_expired_is_idempotent(self):
        tid = (await self.open())["tid"]
        self.assertEqual(await self.r.close("pu-mop-1", "alice", tid), {"ok": True})
        self.assertEqual(await self.r.close("pu-mop-1", "alice", tid), {"ok": True})
        self.assertEqual(await self.r.close("pu-mop-1", "alice", "missing"), {"ok": True})
        tid = (await self.open())["tid"]
        self.now = 31
        await self.r.maintain()
        self.assertEqual(await self.r.close("pu-mop-1", "alice", tid), {"ok": True})

    async def test_attach_exit_during_commit_compensates_before_return(self):
        calls = []
        class Receipt:
            async def commit(inner):
                calls.append("commit")
                self.ptys[-1].on_exit(1)
            async def rollback(inner):
                calls.append("rollback")
        async def prepare(*args, **kwargs):
            return Receipt()
        with self.assertRaisesRegex(ValueError, "attach exited"):
            await self.r.open("pu-mop-1", "alice", "rw", 80, 24, prepare)
        self.assertEqual(calls, ["commit", "rollback"])
        self.assertEqual(self.r.sessions, {})
        self.assertTrue(self.ptys[0].closed)
        self.assertEqual(self.ptys[0].input, [])

    async def test_authorization_snapshot_is_not_a_preparation_receipt(self):
        authorize = self.r.authorize
        async def snapshot(*args, **kwargs):
            await authorize(*args, **kwargs)
            return {"known": True}
        self.r.authorize = snapshot
        self.assertIn("tid", await self.open())

    async def test_provisional_handle_cannot_type_before_commit(self):
        async def spawn(*args):
            tid = next(iter(self.r.sessions))
            with self.assertRaises(ValueError):
                await self.r.write("pu-mop-1", "alice", tid, 1, "eA==")
            p = FakePTY()
            self.ptys.append(p)
            return p
        self.r.spawn = spawn
        await self.open()
        self.assertEqual(self.ptys[0].input, [])

    async def test_writer_concurrency_caps_and_expiry(self):
        got = await asyncio.gather(self.open(), self.open(), return_exceptions=True)
        self.assertEqual(sum(isinstance(x, dict) for x in got), 1)
        self.assertTrue(self.r.writer_open("pu-mop-1"))
        for _ in range(3):
            await self.open("ro")
        with self.assertRaises(ValueError):
            await self.open("ro")
        for n in range(2, 5):
            for _ in range(4):
                await self.open("ro", f"pu-mop-{n}")
        with self.assertRaises(ValueError):
            await self.open("ro", "pu-mop-5")
        self.now = 31
        await self.r.maintain()
        self.assertFalse(self.r.writer_open("pu-mop-1"))
        self.assertTrue(self.ptys[0].closed)
        self.now = 61
        await self.r.maintain()
        self.assertEqual(self.r.sessions, {})
        self.assertTrue(all(p.closed for p in self.ptys))

    async def test_slow_open_does_not_queue_other_targets(self):
        entered, release = asyncio.Event(), asyncio.Event()
        authorize = self.r.authorize
        async def slow(name, owner, **kwargs):
            if name == "pu-mop-1" and kwargs.get("claim"):
                entered.set()
                await release.wait()
            return await authorize(name, owner, **kwargs)
        self.r.authorize = slow
        first = asyncio.create_task(self.open())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(self.r.writer_open("pu-mop-1"), "pending writer reserves input before identity preparation")
            second = await asyncio.wait_for(self.open("rw", "pu-mop-2"), 1)
            self.assertIn("tid", second)
        finally:
            release.set()
            await first

    async def test_periodic_identity_and_gate_recheck(self):
        tid = (await self.open())["tid"]
        self.denied = True
        await self.r.maintain()
        self.assertTrue(self.ptys[0].closed)
        self.assertTrue((await self.r.read("pu-mop-1", "alice", tid, 0, 0))["closed"])

    async def test_gate_before_open_write_resize_and_interrupt_no_send_lock(self):
        self.denied = True
        with self.assertRaises(ValueError):
            await self.open()
        self.assertEqual(self.ptys, [])
        self.denied = False
        tid = (await self.open())["tid"]
        self.assertIn(("pu-mop-1", "alice", True), self.auth)
        self.denied = True
        with self.assertRaises(ValueError):
            await self.r.resize("pu-mop-1", "alice", tid, 100, 30)
        self.assertEqual(self.ptys[0].sizes, [])

    async def test_validation_strict_bounds(self):
        base = dict(verb="term_open", name="pu-mop-1", owner="alice", mode="rw", cols=80, rows=24)
        self.t.validate("term_open", base)
        for update in (dict(owner=""), dict(owner="  "), dict(name=";sh"), dict(mode="x"),
                       dict(cols=True), dict(cols=0), dict(rows=1001), dict(pid=1),
                       dict(force=True), dict(owner="alice\nsecret"), dict(owner="alice\x7f")):
            with self.assertRaises(ValueError):
                self.t.validate("term_open", dict(base, **update))
        for raw in ("?", "eA==\n", base64.b64encode(b"x" * 4097).decode()):
            with self.assertRaises(ValueError):
                self.t.decode_input(raw)
        for wait in (True, -1, 16, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.t.validate("term_read", dict(verb="term_read", name="pu-mop-1", owner="alice",
                                                tid="opaque", cursor=0, wait=wait))
        with self.assertRaises(ValueError):
            self.t.Identity.from_dict(dict(IDENT, pid=True))

    async def test_probe_source_isolated_and_body_identity_fail_closed(self):
        script = self.t.probe_script("/explicit/session.py", "/clone", "pu-mop-1")
        self.assertTrue(script.startswith("python3 -I -c "))
        import tempfile
        from pathlib import Path
        import textwrap
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "session_source.py")
            one = dict(pid=1, sessionId="x", cwd="/clone", messagingSocketPath="one")
            two = dict(one, pid=2, messagingSocketPath="two")
            for live, matches in ((None, []), (dict(one, sessionId=None), [dict(one, sessionId=None)]),
                                  (one, [one, two])):
                path.write_text(textwrap.dedent(f'''\
                    def live_session_for_cwd(cwd): return {live!r}
                    def sessions(): return {matches!r}
                    def socket_alive(path): return True
                '''))
                # Два живых кандидата обязаны отказать до чтения /proc и tmux.
                with self.assertRaises(ValueError):
                    self.t.resolve_body_identity(str(path), "/clone", self.t.pane_argv("pu-mop-1"))

    async def test_body_proc_pane_foreground_and_clear_binding(self):
        import io
        import tempfile
        import stat
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "source.py")
            metadata = dict(pid=123, sessionId="session-one", cwd="/clone", messagingSocketPath="socket")
            path.write_text(f"def live_session_for_cwd(cwd): return {metadata!r}\n"
                            f"def sessions(): return [{metadata!r}]\n"
                            "def socket_alive(path): return True\n")
            fields = ["0"] * 20
            fields[0], fields[2], fields[4], fields[5], fields[19] = "S", "123", "34823", "123", "456"
            proc = "123 (claude (agent)) " + " ".join(fields)
            pane = SimpleNamespace(stdout="%0\t/dev/pts/7\n")
            device = SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=os.makedev(136, 7))
            def opened(path, *a, **kw):
                return io.StringIO(proc)
            with patch("subprocess.run", return_value=pane), patch("builtins.open", side_effect=opened), \
                    patch("os.stat", return_value=device):
                got = self.t.resolve_body_identity(str(path), "/clone", self.t.pane_argv("pu-mop-1"))
                self.assertEqual(got, IDENT)
                after = fields.copy()
                after[5] = "124"
                proc_after = "123 (claude (agent)) " + " ".join(after)
                with patch("builtins.open", side_effect=[io.StringIO(proc), io.StringIO(proc_after)]):
                    with self.assertRaises(ValueError):
                        self.t.resolve_body_identity(str(path), "/clone", self.t.pane_argv("pu-mop-1"))
                with patch("builtins.open", return_value=io.StringIO(proc_after)):
                    with self.assertRaises(ValueError):
                        self.t.resolve_body_identity(str(path), "/clone", self.t.pane_argv("pu-mop-1"))
                pane.stdout = "%0\t/dev/pts/7\n%1\t/dev/pts/8\n"
                with self.assertRaises(ValueError):
                    self.t.resolve_body_identity(str(path), "/clone", self.t.pane_argv("pu-mop-1"))

    async def real_body_probe(self, changing=False, identity_change=None):
        # BUG: O_NOCTTY + tcgetpgrp из чужой session всегда даёт ENOTTY.
        # HYPOTHESIS: /proc уже доказывает foreground; ioctl ошибочно требует
        # controlling tty вызывающего, а полный registry сравнивает status.
        import json
        import shlex
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        output = bytearray()
        child = ("import os,json; assert os.tcgetpgrp(0)==os.getpgrp(); "
                 "os.write(1,(json.dumps(dict(pid=os.getpid(),tty=os.ttyname(0)))+'\\n').encode()); "
                 "os.read(0,1)")
        process = await self.t.PTY.spawn([sys.executable, "-I", "-c", child], 80, 24,
                                        output.extend, lambda code: None)
        try:
            async def ready():
                while b"\n" not in output:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(ready(), 3)
            info = json.loads(output)
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory, "session_source.py")
                metadata = dict(pid=info["pid"], sessionId="real-session", cwd=directory,
                                messagingSocketPath="fixture-socket")
                source.write_text(
                    f"record={metadata!r}\ncount=0\n"
                    "def live_session_for_cwd(cwd):\n"
                    " global count\n count+=1\n"
                    f" return dict(record, **({(identity_change or {})!r} if count>1 else {{}}))\n"
                    "def sessions():\n"
                    " return [dict(record, **" + repr(dict(status='working', updatedAt=2, waitingFor='tool') if changing else {}) + ")]\n"
                    "def socket_alive(path): return True\n")
                argv = [sys.executable, "-I", "-c", f"print('%0\\t{info['tty']}')"]
                with patch.object(self.t, "pane_argv", return_value=argv):
                    script = self.t.probe_script(str(source), directory, "pu-mop-1")
                caller = await asyncio.create_subprocess_exec(
                    *shlex.split(script), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=True)
                stdout, stderr = await asyncio.wait_for(caller.communicate(), 3)
                return caller.returncode, stdout, stderr, info
        finally:
            await process.close()

    async def test_real_body_probe_from_independent_noncontrolling_caller(self):
        code, out, err, info = await self.real_body_probe()
        self.assertEqual(code, 0, err.decode())
        import json
        got = json.loads(out)
        self.assertEqual(got["pid"], info["pid"])
        self.assertEqual(got["tty"], info["tty"])
        self.assertEqual(got["sessionId"], "real-session")
        self.assertEqual(got["pane"], "%0")
        self.assertTrue(got["start"].isdigit())

    async def test_real_body_probe_ignores_mutable_registry_status(self):
        code, out, err, info = await self.real_body_probe(changing=True)
        self.assertEqual(code, 0, err.decode())

    async def test_real_body_probe_rejects_registry_identity_changes(self):
        for change in ({"sessionId": "new"}, {"pid": 1},
                       {"messagingSocketPath": "new-socket"}, {"cwd": "/other"}):
            with self.subTest(change=change):
                code, out, err, info = await self.real_body_probe(identity_change=change)
                self.assertNotEqual(code, 0)
                self.assertEqual(out, b"")

    async def test_controlling_pty_fixture_without_claude(self):
        # Никакой модели/пула: проверяем setsid -c маленьким stdlib-процессом.
        output = bytearray()
        exited = asyncio.Event()
        codes = []
        code = ("import os; "
                "assert os.isatty(0); "
                "assert os.tcgetpgrp(0) == os.getpgrp(); "
                "assert os.getsid(0) == os.getpid(); "
                "os.write(1,b'\\x1b[31mREADY\\xff'); "
                "assert os.read(0,1) == b'x'; "
                "os.write(1,b'DONE')")
        def on_exit(code):
            codes.append(code)
            exited.set()
        process = await self.t.PTY.spawn([sys.executable, "-I", "-c", code], 80, 24,
                                        output.extend, on_exit)
        try:
            async def ready():
                while b"READY" not in output and not exited.is_set():
                    await asyncio.sleep(.01)
            await asyncio.wait_for(ready(), 3)
            self.assertIn(b"\x1b[31mREADY\xff", output)
            self.assertFalse(os.get_blocking(process.fd), "node PTY writes must never block")
            process.resize(100, 30)
            self.assertEqual(process.write(b"x"), 1)
            await asyncio.wait_for(exited.wait(), 3)
            self.assertEqual(codes, [0])
            self.assertIn(b"DONE", output)
        finally:
            await process.close()

    async def test_longpoll_wakeup_and_exit(self):
        tid = (await self.open("ro"))["tid"]
        task = asyncio.create_task(self.r.read("pu-mop-1", "alice", tid, 0, 15))
        await asyncio.sleep(0)
        self.ptys[0].on_data(b"actual stream")
        got = await asyncio.wait_for(task, 1)
        self.assertEqual(base64.b64decode(got["data"]), b"actual stream")
        self.ptys[0].on_exit(17)
        got = await self.r.read("pu-mop-1", "alice", tid, got["cursor"], 0)
        self.assertTrue(got["closed"])
        self.assertEqual(got["exit"], 17)


# HYPOTHESIS: unittest.main обходит общий счётчик проверок (#598), поэтому
# tests_lib и вслед за ним hermetic красные. SOLUTION: оставляем утверждения,
# async-фикстуры и subTest stdlib, но каждый результат идёт в Checks вместе
# с полным traceback; пропуски не считаются успехом.
# STATUS: FIXED — see #598
class CheckResults(unittest.TestResult):
    def __init__(self, checks):
        super().__init__()
        self.checks = checks

    def addSuccess(self, test):
        super().addSuccess(test)
        self.checks.check(test.id(), True)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.checks.fail(test.id(), self.failures[-1][1])

    def addError(self, test, err):
        super().addError(test, err)
        self.checks.fail(test.id(), self.errors[-1][1])

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            records = self.failures if issubclass(err[0], test.failureException) else self.errors
            self.checks.fail(subtest.id(), records[-1][1])

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.checks.fail(test.id(), f"skipped: {reason}")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.checks.fail(test.id(), self.expectedFailures[-1][1])

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.checks.fail(test.id(), "unexpected success")


def main():
    c = Checks()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Terminal)
    count = suite.countTestCases()
    result = CheckResults(c)
    suite.run(result)
    c.expect("all terminal cases ran", result.testsRun, count)
    return c.report("terminal")


if __name__ == "__main__":
    sys.exit(main())
