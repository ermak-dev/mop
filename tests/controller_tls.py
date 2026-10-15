"""Защищённые ссылки TLS проверяются до Django/libpq и native backup."""
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _lib  # noqa: E402,F401
import hermetic  # noqa: E402,F401
from mop.server.controller.bootstrap import load


# BUG: bootstrap доверяет любым непустым sslrootcert/sslcert/sslkey (#632).
# HYPOTHESIS: проверка типа строки не защищает от подмены trust/key через путь.
# SOLUTION: единая проверка dirfd/O_NOFOLLOW, владельцев и прав до передачи libpq.
# RESULT: небезопасные ссылки отвергнуты; публичный доверенный CA разрешён.
# STATUS: FIXED — see #632.
class ProtectedTLS(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / 'bootstrap.json'
        self.ca = self.file('fixture-ca.pem', 0o644)
        self.cert = self.file('fixture-cert.pem', 0o644)
        self.key = self.file('fixture-key.pem', 0o600)
        self.options = {'sslmode': 'verify-full', 'sslrootcert': str(self.ca),
                        'sslcert': str(self.cert), 'sslkey': str(self.key)}

    def file(self, name, mode):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture-only-not-a-key-or-certificate')
        path.chmod(mode)
        return path

    def load(self):
        self.path.write_text(json.dumps({
            'installation_id': 'tls-fixture', 'role': 'web',
            'database': {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'fixture',
                         'HOST': 'db.example.invalid', 'PORT': '5432', 'USER': 'fixture',
                         'PASSWORD': 'fixture-password', 'OPTIONS': self.options}}))
        self.path.chmod(0o600)
        return load(self.path)

    def refused(self):
        with self.assertRaises(RuntimeError) as error:
            self.load()
        message = ''.join(traceback.format_exception(error.exception))
        self.assertNotIn(str(self.directory), message)
        self.assertNotIn('fixture-only-not-a-key-or-certificate', message)
        self.assertIsNone(error.exception.__context__)

    def test_owned_public_trust_and_private_key_accepted(self):
        self.assertEqual(self.load()['database']['OPTIONS'], self.options)
        self.key.chmod(0o400)
        self.load()

    def test_relative_missing_directory_and_symlink_tls_references_refused(self):
        link = self.directory / 'link.pem'
        link.symlink_to(self.ca)
        for option in ('sslrootcert', 'sslcert', 'sslkey'):
            original = self.options[option]
            for value in ('relative.pem', str(self.directory / 'absent.pem'),
                          str(self.directory), str(link), 'system'):
                with self.subTest(option=option, value_kind=value.rsplit('/', 1)[-1]):
                    self.options[option] = value
                    self.refused()
            self.options[option] = original

    def test_writable_tls_files_refused(self):
        for option, path in (('sslrootcert', self.ca), ('sslcert', self.cert),
                             ('sslkey', self.key)):
            original = path.stat().st_mode & 0o777
            for mode in ((0o664, 0o666) if option != 'sslkey' else (0o644, 0o640, 0o660)):
                with self.subTest(option=option, mode=oct(mode)):
                    path.chmod(mode)
                    self.refused()
            path.chmod(original)

    def test_symlink_and_writable_replacement_ancestors_refused(self):
        ca = self.file('chain/trust/ca.pem', 0o644)
        self.options['sslrootcert'] = str(ca)
        alias = self.directory / 'alias'
        alias.symlink_to(ca.parent, target_is_directory=True)
        self.options['sslrootcert'] = str(alias / ca.name)
        self.refused()
        self.options['sslrootcert'] = str(ca)
        for parent in (ca.parent, ca.parent.parent):
            for mode in (0o777, 0o775):
                with self.subTest(parent=parent.name, mode=oct(mode)):
                    parent.chmod(mode)
                    self.refused()
            parent.chmod(0o755)
        self.load()

    def test_untrusted_tls_owner_refused_but_root_owned_public_ca_accepted(self):
        real_fstat = os.fstat
        inode = self.ca.stat().st_ino

        def owner(uid):
            def fstat(fd):
                info = real_fstat(fd)
                if info.st_ino == inode:
                    values = list(info)
                    values[4] = uid
                    return os.stat_result(values)
                return info
            return fstat

        with patch('mop.server.controller.bootstrap.os.fstat', owner(os.geteuid() + 10001)):
            self.refused()
        with patch('mop.server.controller.bootstrap.os.fstat', owner(0)):
            self.load()

    def test_sticky_trusted_directory_safe_but_untrusted_directory_owner_refused(self):
        ca = self.file('sticky/ca.pem', 0o644)
        ca.parent.chmod(0o1777)
        self.options['sslrootcert'] = str(ca)
        self.load()
        real_fstat = os.fstat
        inode = ca.parent.stat().st_ino

        def fstat(fd):
            info = real_fstat(fd)
            if info.st_ino == inode:
                values = list(info)
                values[4] = os.geteuid() + 10001
                return os.stat_result(values)
            return info

        with patch('mop.server.controller.bootstrap.os.fstat', fstat):
            self.refused()

    def test_fifo_tls_reference_refused_without_blocking(self):
        fifo = self.directory / 'fifo.pem'
        os.mkfifo(fifo, 0o600)
        self.options['sslrootcert'] = str(fifo)
        self.refused()

    def test_verify_full_requires_explicit_trust_not_ambient_libpq_default(self):
        del self.options['sslrootcert']
        self.refused()

    def test_client_certificate_and_key_must_be_explicit_pair(self):
        for missing in ('sslcert', 'sslkey'):
            original = self.options.pop(missing)
            self.refused()
            self.options[missing] = original


if __name__ == '__main__':
    unittest.main()
