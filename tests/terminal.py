#!/usr/bin/env python3
"""PTY-реестр без пула (#598).

HYPOTHESIS: attach-процесс не доказывает живую Claude-сессию; повтор RPC
может повторно ввести клавиши, а capture-pane не является потоком байтов.
SOLUTION: приватный ограниченный реестр, строгая identity и одноразовый seq.
RESULT: реестр и агент прошли узкие строгие проверки; локальный stdlib-PTY
подтвердил controlling terminal без Claude/пула и без оплачиваемых вызовов.
STATUS: FIXED — see #598
"""
import asyncio
import base64
import importlib.util
import sys
import os
import unittest

import hermetic  # noqa: F401
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
                    patch("os.stat", return_value=device), patch("os.open", return_value=999), \
                    patch("os.close"), patch("os.tcgetpgrp", return_value=123) as foreground:
                got = self.t.resolve_body_identity(str(path), "/clone", self.t.pane_argv("pu-mop-1"))
                self.assertEqual(got, IDENT)
                after = fields.copy()
                after[5] = "124"
                proc_after = "123 (claude (agent)) " + " ".join(after)
                with patch("builtins.open", side_effect=[io.StringIO(proc), io.StringIO(proc_after)]):
                    with self.assertRaises(ValueError):
                        self.t.resolve_body_identity(str(path), "/clone", self.t.pane_argv("pu-mop-1"))
                foreground.return_value = 124
                with self.assertRaises(ValueError):
                    self.t.resolve_body_identity(str(path), "/clone", self.t.pane_argv("pu-mop-1"))
                foreground.return_value = 123
                pane.stdout = "%0\t/dev/pts/7\n%1\t/dev/pts/8\n"
                with self.assertRaises(ValueError):
                    self.t.resolve_body_identity(str(path), "/clone", self.t.pane_argv("pu-mop-1"))

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


if __name__ == "__main__":
    unittest.main()
