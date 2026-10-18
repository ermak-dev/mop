#!/usr/bin/env python3
"""HYPOTHESIS: #721 задаёт installed hold pin, но исполняемого artifact нет.
SOLUTION: отдельный stdlib artifact проверяет argv/свои bytes/root trust и
только ждёт сигнал; metadata builder возвращает настоящие bytes/hash без I/O записи.
Дополнительный red: -IS без -B оставлял root право создавать stdlib bytecode;
исправление — -ISB и проверка interpreter flags, не env-переключатель.
RESULT: red — отсутствовали standalone anchor и metadata builder. Green —
12 pure/import/lifecycle checks и отдельный owned subprocess gate через
mop driver hold-probe, без пула и native readiness.
STATUS: FIXED — see #643
"""
import ast
import hashlib
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

import hermetic  # noqa: F401 — до mop (#209)
from _lib import run_command

ROOT = Path(__file__).resolve().parents[1]
OPTIONS = ('--sha256', '--target', '--incarnation', '--executor-epoch',
           '--generation', '--bootstrap-revision')


def arguments(digest='a'*64):
    values = (digest, str(UUID(int=1)), str(UUID(int=2)), str(UUID(int=3)),
              '1', str(UUID(int=4)))
    return ['hold', *[item for pair in zip(OPTIONS, values) for item in pair]]


class HoldAnchorTests(unittest.TestCase):
    def module(self, name='hold_anchor'):
        try:
            return importlib.import_module('mop.node.' + name)
        except ModuleNotFoundError as error:
            if error.name != 'mop.node.' + name:
                raise
            self.fail('production passive anchor module is missing: ' + name)

    def test_parser_accepts_only_public_metadata_without_authority(self):
        anchor = self.module()
        parsed = anchor.parse(arguments())
        self.assertEqual(parsed._asdict(), dict(sha256='a'*64,
            target=str(UUID(int=1)), incarnation=str(UUID(int=2)),
            executor_epoch=str(UUID(int=3)), generation=1,
            bootstrap_revision=str(UUID(int=4))))
        # Порядок ключей не authority; набор и единственность обязательны.
        pairs = list(zip(arguments()[1::2], arguments()[2::2]))
        self.assertEqual(anchor.parse(['hold', *[x for pair in reversed(pairs) for x in pair]]), parsed)
        last = arguments()
        last[last.index('--generation')+1] = str(2**63-1)
        self.assertEqual(anchor.parse(last).generation, 2**63-1)

    def test_parser_rejects_unknown_duplicate_missing_and_noncanonical_values(self):
        parse = self.module().parse
        cases = [[], ['hold'], ['start', *arguments()[1:]], arguments()+['--native'],
                 arguments()+['--target', str(UUID(int=5))], arguments()[:-2],
                 arguments()+['--help'], None, 'hold', ['hold', *arguments()[1:-1], None]]
        for option, invalid in (
                ('--sha256', ('A'*64, 'g'*64, 'a'*63, 'a'*65, 'sha256-'+'a'*64, 'a'*64+'\n')),
                ('--target', ('', 'not-a-uuid', UUID(int=1).hex, '{'+str(UUID(int=1))+'}',
                              'AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA')),
                ('--incarnation', ('../x', '1', str(UUID(int=2))+'\n')),
                ('--executor-epoch', ('epoch', 'urn:uuid:'+str(UUID(int=3)))),
                ('--bootstrap-revision', ('old', ' '+str(UUID(int=4)))),
                ('--generation', ('0', '-1', '+1', '01', '1.0', ' 1', '1\n',
                                  '١', str(2**63), '9'*5000))):
            for value in invalid:
                argv = arguments()
                argv[argv.index(option)+1] = value
                cases.append(argv)
        duplicate = arguments()
        duplicate[duplicate.index('--target')] = '--incarnation'
        cases.append(duplicate)
        for argv in cases:
            with self.subTest(argv=repr(argv)[:180]), self.assertRaises(ValueError):
                parse(argv)

    def test_installed_path_is_absolute_canonical_and_content_addressed(self):
        anchor = self.module()
        digest = 'a'*64
        path = '/opt/mop/anchors/sha256-'+digest+'/hold-anchor'
        self.assertEqual(anchor.installed_path(path, digest).as_posix(), path)
        for bad in ('hold-anchor', '//opt/mop/sha256-'+digest+'/hold-anchor',
                    path.replace('/anchors/', '/anchors/../'), path+'/../hold-anchor',
                    path.replace('/anchors/', '/anchors/./'), path.replace(digest, 'b'*64),
                    path.replace('/hold-anchor', '/bad name'), '/sha256-'+digest,
                    path+'\n', path.replace('/anchors/', '/anchors//')):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                anchor.installed_path(bad, digest)

    def test_artifact_metadata_has_exact_source_bytes_not_placeholder_or_authority(self):
        builder = self.module('hold_artifact')
        # HYPOTHESIS: ручной capture нарушает retained tests_lib gate (#675).
        # SOLUTION: общий runner сохраняет artifact и проверку тишины библиотеки.
        # RESULT: retained tests_lib RED -> GREEN, artifact checks сохранены.
        # STATUS: FIXED — see #675
        artifacts = []
        out, err, code = run_command(lambda _: artifacts.append(builder.artifact()))
        self.assertEqual(code, 0)
        artifact, = artifacts
        content = (ROOT/'mop/node/hold_anchor.py').read_bytes()
        self.assertEqual(artifact.content, content)
        self.assertEqual(artifact.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(artifact.executable('/opt/mop/anchors'),
            '/opt/mop/anchors/sha256-'+artifact.sha256+'/hold-anchor')
        # Root imports тоже не должны создавать stdlib __pycache__ на узле.
        self.assertTrue(content.startswith(b'#!/usr/bin/python3 -ISB\n'))
        self.assertEqual((out, err), ('', ''))
        for bad in ('relative', '/opt/../anchors', '/opt//anchors', '/opt/anchors/', '/tmp/bad name'):
            with self.subTest(prefix=bad), self.assertRaises(ValueError):
                artifact.executable(bad)

    def test_real_artifact_pin_matches_the_unchanged_public_renderer_contract(self):
        builder = self.module('hold_artifact')
        from mop.server.launch_spec import anchor_pin
        artifact = builder.artifact()
        path = artifact.executable('/opt/mop/anchors')
        self.assertEqual(anchor_pin(path, artifact.sha256), (path, artifact.sha256))
        self.assertEqual(self.module().installed_path(path, artifact.sha256).as_posix(), path)

    def test_runtime_imports_only_stdlib_and_never_acquires_startup_authority(self):
        module = self.module()
        tree = ast.parse(Path(module.__file__).read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imports.append(node.module)
        self.assertTrue(set(name.split('.')[0] for name in imports) <= sys.stdlib_module_names)
        self.assertFalse(set(imports) & {'subprocess', 'socket', 'ctypes', 'importlib', 'runpy'})
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, 'attr', getattr(node.func, 'id', ''))
                self.assertNotIn(name, ('system', 'popen', 'fork', 'execv', 'execve', 'spawnv',
                    'eval', 'exec', '__import__', 'getenv', 'mkdir', 'makedirs', 'unlink',
                    'write_bytes', 'write_text', 'chown', 'chmod'))
            if isinstance(node, ast.Attribute):
                self.assertNotEqual(node.attr, 'environ')

    def test_untrusted_temporary_ancestor_cannot_become_an_installed_anchor(self):
        anchor = self.module()
        content = Path(anchor.__file__).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/('sha256-'+digest)/'hold-anchor'
            path.parent.mkdir()
            path.write_bytes(content)
            path.chmod(0o555)
            # Ни UID текущего пользователя, ни sticky /tmp не доверенная установка.
            with self.assertRaisesRegex(ValueError, 'root|trust|writ'):
                anchor.verify_installed(str(path), digest)

    def test_refusal_never_waits_and_signal_mask_is_restored(self):
        anchor = self.module()
        import signal
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        with patch.object(anchor, 'verify_installed', side_effect=ValueError('untrusted anchor')), \
             patch.object(anchor.signal, 'sigwait', side_effect=AssertionError('refusal waited')):
            with self.assertRaisesRegex(ValueError, 'untrusted anchor'):
                anchor.hold(arguments(), '/untrusted')
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), previous)

    def test_fd_hash_and_identity_are_rechecked_before_wait(self):
        anchor = self.module()
        # Точка fstat после чтения: подмена identity/content не возвращает bytes.
        with tempfile.TemporaryFile() as stream:
            stream.write(b'owned anchor bytes')
            stream.flush()
            stream.seek(0)
            info = os.fstat(stream.fileno())
            changed = os.stat_result((info.st_mode, info.st_ino+1, info.st_dev,
                info.st_nlink, info.st_uid, info.st_gid, info.st_size,
                info.st_atime, info.st_mtime, info.st_ctime))
            self.assertFalse(anchor.same_file(info, changed))
            self.assertTrue(anchor.same_file(info, info))
            with patch.object(anchor.os, 'fstat', side_effect=[info, changed]), \
                 self.assertRaisesRegex(ValueError, 'identity|bytes'):
                anchor.read_bytes(stream.fileno())

    def test_verified_fd_is_closed_and_mask_restored_on_signal_or_wait_failure(self):
        anchor = self.module()
        import signal
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        for failure in (False, True):
            with tempfile.TemporaryFile() as stream:
                descriptor = os.dup(stream.fileno())
                options = {'side_effect': RuntimeError('wait failed')} if failure else {'return_value': signal.SIGTERM}
                with patch.object(anchor, 'verify_installed', return_value=descriptor), \
                     patch.object(anchor.signal, 'sigwait', **options):
                    if failure:
                        with self.assertRaisesRegex(RuntimeError, 'wait failed'):
                            anchor.hold(arguments(), '/owned-fixture')
                    else:
                        self.assertEqual(anchor.hold(arguments(), '/owned-fixture'), 0)
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
                self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), previous)

    def test_standalone_cold_import_does_not_import_the_package_or_print(self):
        anchor = self.module()
        script = '''import runpy, sys
runpy.run_path(sys.argv[1], run_name='owned_import_probe')
assert not any(name == 'mop' or name.startswith('mop.') for name in sys.modules)
'''
        result = subprocess.run([sys.executable, '-ISB', '-c', script, anchor.__file__],
            capture_output=True, text=True, timeout=5, cwd=hermetic.HOME,
            env=hermetic.child_env({'PYTHONPATH': '/does-not-exist', 'MOP_ENV_FILE': '/must-not-read'}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), ('', ''))

    def test_metadata_builder_refuses_symlink_or_changing_source_without_writing(self):
        builder = self.module('hold_artifact')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'hold_anchor.py'
            content = b'owned metadata fixture, not a production anchor'
            source.write_bytes(content)
            with patch.object(builder, '__file__', str(Path(directory)/'hold_artifact.py')):
                self.assertEqual(builder.artifact().sha256, hashlib.sha256(content).hexdigest())
                with patch.object(builder, 'same_file', return_value=False), \
                     self.assertRaisesRegex(ValueError, 'identity'):
                    builder.artifact()
                source.rename(source.with_name('real-source'))
                source.symlink_to('real-source')
                with self.assertRaises(OSError):
                    builder.artifact()


if __name__ == '__main__':
    unittest.main()
