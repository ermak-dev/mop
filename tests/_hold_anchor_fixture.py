"""Только owned Linux container: production bytes, root/nobody, без native.

HYPOTHESIS: pin renderer сам по себе не даёт установленного пассивного процесса.
SOLUTION: настоящий executable с root trust и hostile окружением, два запуска
на UID, реальные сигналы и отказы до ожидания. Никакого Nomad/SessionApi/readiness.
RESULT: red — artifact отсутствовал; green — восемь owned случаев, root/nobody,
TERM/INT/HUP, pending stop, trust/hash/identity отказы, read-only audit и отсутствие startup effects.
STATUS: FIXED — see #643
"""
import hashlib
import os
from pathlib import Path
import runpy
import shutil
import signal
import stat
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from uuid import UUID

# Fixture не импортирует пакет: в container смонтированы лишь artifact и этот файл.
SOURCE = Path('/input/hold_anchor.py')
BASE = Path('/owned-hold-fixture')


def argv(digest):
    return ['hold', '--sha256', digest, '--target', str(UUID(int=1)),
            '--incarnation', str(UUID(int=2)), '--executor-epoch', str(UUID(int=3)),
            '--generation', '1', '--bootstrap-revision', str(UUID(int=4))]


def tree_snapshot(root):
    result = {}
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        result[str(path.relative_to(root))] = (info.st_mode, info.st_uid, info.st_gid,
            path.read_bytes() if stat.S_ISREG(info.st_mode) else None)
    return result


class InstalledHoldFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0 or BASE.exists():
            raise RuntimeError('hold probe requires root in a fresh owned container')
        if not SOURCE.is_file():
            raise AssertionError('production passive anchor artifact is missing')
        # Python Docker image кладёт interpreter в /usr/local; production Linux
        # nodes дают /usr/bin/python3. Только private container, не host /usr.
        if not os.path.lexists('/usr/bin/python3'):
            os.symlink(sys.executable, '/usr/bin/python3')
        BASE.mkdir(mode=0o755)
        cls.content = SOURCE.read_bytes()
        cls.digest = hashlib.sha256(cls.content).hexdigest()
        cls.prefix = BASE/'installed'
        cls.prefix.mkdir(mode=0o755)
        cls.path = cls.prefix/('sha256-'+cls.digest)/'hold-anchor'
        cls.path.parent.mkdir(mode=0o755)
        cls.path.write_bytes(cls.content)
        cls.path.chmod(0o555)
        cls.work = BASE/'work'
        cls.work.mkdir(mode=0o755)
        # Если artifact попытается запустить хоть что-то или прочесть Python
        # рядом/cwd/PYTHONPATH, ловушки оставят след. -IS обязан их игнорировать.
        cls.traps = cls.work/'traps'
        cls.traps.mkdir(mode=0o755)
        payload = 'from pathlib import Path\nPath("'+str(cls.work/'unexpected-start')+'").touch()\nraise RuntimeError("startup trap")\n'
        for name in ('sitecustomize.py', 'hashlib.py', 'uuid.py', 'signal.py', 'mop.py'):
            (cls.traps/name).write_text(payload)
        for name in ('claude', 'git', 'bootstrap', 'native', 'mop', 'python3', 'env'):
            trap = cls.traps/name
            trap.write_text('#!/bin/sh\ntouch "'+str(cls.work/'unexpected-start')+'"\nexit 91\n')
            trap.chmod(0o755)
        for name in ('.env', 'node.env', 'secrets.env', 'connection.json', 'ready.json'):
            (cls.work/name).write_text('fixture-only-not-authority\n')
        os.chown(cls.work, 65534, 65534)
        cls.env = dict(HOME=str(cls.work), PATH=str(cls.traps), PYTHONPATH=str(cls.traps),
            PYTHONHOME=str(cls.traps), MOP_ENV_FILE=str(cls.work/'.env'),
            MOP_BUS_CONFIG=str(cls.work/'connection.json'), MOP_DRIVER='must-not-run',
            MOP_REGISTERED_LAUNCHER=str(cls.traps/'native'),
            ANTHROPIC_AUTH_TOKEN='fixture-only-not-a-credential',
            MOP_TARGET_ID='not-the-argv-target', MOP_GENERATION='not-the-argv-generation')

    def start(self, path=None, args=None, uid=65534):
        return subprocess.Popen([str(path or self.path), *(argv(self.digest) if args is None else args)],
            cwd=self.traps, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            user=uid, group=uid, extra_groups=[])

    def reject(self, *, path=None, args=None, uid=65534):
        process = self.start(path, args, uid)
        try:
            out, err = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)
            self.fail('invalid anchor reached the passive wait')
        self.assertEqual(process.returncode, 2, err)
        self.assertEqual(out, b'')
        self.assertTrue(err.startswith(b'hold anchor refused: '), err)
        self.assertNotIn(b'Traceback', err)
        self.assertNotIn(b'fixture-only', err)

    def test_exact_argv_holds_and_signals_exit_without_any_startup_effect(self):
        before = tree_snapshot(BASE)
        def caches():
            return {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in Path(os.__file__).parent.rglob('*.pyc')}
        bytecode = caches()
        # Дважды для каждого UID: allocation restart не превращается в native start.
        for uid in (0, 65534):
            for stop in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                with self.subTest(uid=uid, signal=stop):
                    process = self.start(uid=uid)
                    try:
                        time.sleep(.2)
                        self.assertIsNone(process.poll(), 'valid installed anchor did not hold')
                        children = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text()
                        self.assertEqual(children.strip(), '', 'anchor launched a child process')
                        self.assertEqual(Path(f'/proc/{process.pid}/cmdline').read_bytes().split(b'\0')[2:-1],
                                         [str(self.path).encode(), *[x.encode() for x in argv(self.digest)]])
                        process.send_signal(stop)
                        out, err = process.communicate(timeout=3)
                        self.assertEqual(process.returncode, 0, err)
                        self.assertEqual((out, err), (b'', b''))
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.communicate(timeout=3)
                    self.assertEqual(tree_snapshot(BASE), before, 'passive hold changed the fixture')
                    self.assertEqual(caches(), bytecode, 'passive hold wrote interpreter bytecode')

    def test_pending_stop_survives_exec_and_verification_without_startup_effects(self):
        before = tree_snapshot(BASE)
        def pending_stop():
            signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGTERM,))
            os.kill(os.getpid(), signal.SIGTERM)
        for uid in (0, 65534):
            process = subprocess.Popen([str(self.path), *argv(self.digest)], cwd=self.traps,
                env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, user=uid, group=uid, extra_groups=[], preexec_fn=pending_stop)
            try:
                out, err = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0, err)
                self.assertEqual((out, err), (b'', b''))
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)
        self.assertEqual(tree_snapshot(BASE), before)

    def test_regular_fd_identity_is_verified_and_replacement_refuses_before_wait(self):
        namespace = runpy.run_path(str(self.path), run_name='owned_verifier_probe')
        verify = namespace['verify_installed']
        descriptor = verify(str(self.path), self.digest)
        try:
            self.assertTrue(namespace['same_file'](os.fstat(descriptor), self.path.stat()))
        finally:
            os.close(descriptor)
        # Подменяем pathname во время чтения, оставляя открытый fd прежним.
        # Одинаковые bytes/hash не должны скрыть другую inode.
        original_read = namespace['read_bytes']
        saved = self.path.with_name('saved-identity')
        def replace_after_read(fd):
            content = original_read(fd)
            self.path.rename(saved)
            self.path.write_bytes(self.content)
            self.path.chmod(0o555)
            return content
        before_fds = len(list(Path('/proc/self/fd').iterdir()))
        # runpy возвращает копию globals; binding меняем в globals самого verifier.
        verify.__globals__['read_bytes'] = replace_after_read
        try:
            with self.assertRaisesRegex(ValueError, 'identity'):
                verify(str(self.path), self.digest)
        finally:
            verify.__globals__['read_bytes'] = original_read
            if saved.exists():
                self.path.unlink()
                saved.rename(self.path)
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before_fds)
        # FIFO проверяется тем же production verifier, не исполняется как script.
        self.path.rename(saved)
        os.mkfifo(self.path, 0o555)
        try:
            with self.assertRaisesRegex(ValueError, 'regular'):
                verify(str(self.path), self.digest)
        finally:
            self.path.unlink()
            saved.rename(self.path)
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before_fds)

    def test_hold_audit_opens_only_installed_root_chain_and_readonly_artifact(self):
        namespace = runpy.run_path(str(self.path), run_name='owned_audit_probe')
        events, active = [], False
        def observe(event, args):
            if active and (event == 'open' or event.startswith(('socket.', 'subprocess.', 'os.exec', 'os.fork', 'os.system'))):
                events.append((event, args))
        sys.addaudithook(observe)
        with patch.object(namespace['signal'], 'sigwait', return_value=signal.SIGTERM):
            active = True
            try:
                self.assertEqual(namespace['hold'](argv(self.digest), str(self.path)), 0)
            finally:
                active = False
        self.assertEqual([event for event, _ in events], ['open'] * len(self.path.parts))
        self.assertEqual([args[0] for _, args in events], ['/', *self.path.parts[1:]])
        self.assertTrue(all(args[2] & os.O_ACCMODE == os.O_RDONLY for _, args in events))

    def test_unknown_or_invalid_metadata_refuses_before_wait(self):
        for args in ([], ['--help'], ['start', *argv(self.digest)[1:]],
                     argv(self.digest)+['--native'], argv(self.digest)+['--target', str(UUID(int=9))]):
            self.reject(args=args)
        for option, value in (('--sha256', 'a'*64), ('--target', '../target'),
                              ('--incarnation', 'not-a-uuid'), ('--executor-epoch', 'epoch'),
                              ('--bootstrap-revision', 'old'), ('--generation', '0')):
            args = argv(self.digest)
            args[args.index(option)+1] = value
            self.reject(args=args)

    def test_root_trust_modes_owner_and_ancestors_fail_closed(self):
        for uid in (0, 65534):
            for path, mode in ((self.path, 0o755), (self.path, 0o575),
                               (self.path.parent, 0o777), (self.prefix, 0o775),
                               (self.prefix, 0o1777)):
                with self.subTest(uid=uid, path=path, mode=oct(mode)):
                    original = stat.S_IMODE(path.stat().st_mode)
                    try:
                        path.chmod(mode)
                        self.reject(uid=uid)
                    finally:
                        path.chmod(original)
            for path in (self.path, self.path.parent, self.prefix):
                try:
                    os.chown(path, 65534, 65534)
                    self.reject(uid=uid)
                finally:
                    os.chown(path, 0, 0)

    def test_hash_bytes_and_content_addressed_path_are_not_interchangeable(self):
        # Старый pin с изменёнными валидными Python bytes не должен удержать job.
        self.path.chmod(0o755)
        self.path.write_bytes(self.content+b'\n# changed fixture bytes\n')
        self.path.chmod(0o555)
        try:
            self.reject()
        finally:
            self.path.chmod(0o755)
            self.path.write_bytes(self.content)
            self.path.chmod(0o555)
        alias = self.prefix/'mutable-anchor'
        shutil.copyfile(self.path, alias)
        alias.chmod(0o555)
        self.reject(path=alias)
        args = argv(self.digest)
        args[args.index('--sha256')+1] = 'b'*64
        self.reject(args=args)
        # Даже python -ISB не должен сделать relative argv[0] установленным путём.
        result = subprocess.run(['/usr/bin/python3', '-ISB', os.path.relpath(self.path, self.traps),
                                 *argv(self.digest)], cwd=self.traps, env=self.env,
                                capture_output=True, timeout=3, user=65534, group=65534, extra_groups=[])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b'')

    def test_file_and_ancestor_symlinks_are_rejected_without_following(self):
        saved = self.path.with_name('saved-anchor')
        self.path.rename(saved)
        self.path.symlink_to(saved.name)
        try:
            self.reject()
        finally:
            self.path.unlink()
            saved.rename(self.path)
        old = self.prefix.with_name('saved-installed')
        self.prefix.rename(old)
        self.prefix.symlink_to(old.name)
        try:
            self.reject()
        finally:
            self.prefix.unlink()
            old.rename(self.prefix)


if __name__ == '__main__':
    unittest.main()
