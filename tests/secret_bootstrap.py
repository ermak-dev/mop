"""Связь AEAD с защищённым root trust, без БД и файлов установки.

HYPOTHESIS: fingerprint корня не является ключом; усечение корня теряет
энтропию и связывает разные bootstrap с одинаковым префиксом.
SOLUTION: явная credential-композиция с проверкой identity/fingerprint
и HKDF-SHA256 от всех байтов защищённого корня.
RESULT: четыре behavioral red на отсутствующую композицию, затем green
для derivation, повторного чтения, отказов и защищённых файлов.
STATUS: FIXED — see #637 (credential-only root composition; runtime проверяет отдельный gate)
"""
import hashlib
import hmac
from pathlib import Path
import tempfile
import unittest

import hermetic  # noqa: F401 — до импорта mop
from _lib import offline


class BootstrapCrypto(unittest.TestCase):
    def setUp(self):
        from mop.server.controller import bootstrap
        self.assertTrue(callable(getattr(bootstrap, 'secret_provider', None)),
                        'protected bootstrap does not compose the secret AEAD provider')
        self.factory = bootstrap.secret_provider
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'root.key'
        self.root = b'synthetic-root-material-' * 5
        self.write(self.root)
        self.bootstrap = {'role': 'credential', 'installation_id': 'fixture-install',
                          'root_key': str(self.path)}

    def write(self, root):
        self.path.write_bytes(root)
        self.path.chmod(0o600)

    def provider(self, **overrides):
        params = {'installation_id': 'fixture-install',
                  'expected_root_digest': hashlib.sha256(self.root).hexdigest(),
                  'key_id': 'root-v1'}
        params.update(overrides)
        return self.factory(self.bootstrap, **params)

    def test_all_root_bytes_are_used_by_domain_separated_hkdf(self):
        provider = self.provider()
        salt = b'mop.secret.root.hkdf-sha256.v1\x00'
        info = b'mop.secret.aead\x00fixture-install\x00root-v1'
        prk = hmac.digest(salt, self.root, 'sha256')
        expected = hmac.digest(prk, info + b'\x01', 'sha256')
        self.assertEqual(provider.keyring.keys['root-v1'], expected)
        other = self.root[:32] + b'different suffix'
        self.write(other)
        self.assertNotEqual(self.provider(expected_root_digest=hashlib.sha256(other).hexdigest())
                            .keyring.keys['root-v1'], expected)
        self.assertNotEqual(self.provider(expected_root_digest=hashlib.sha256(other).hexdigest(),
                                         key_id='root-v2').keyring.keys['root-v2'], expected)

    def test_roundtrip_survives_recomposition_not_public_fingerprint(self):
        from mop.server.secrets.crypto import SecretContext, SecretCryptoError, Keyring, XChaCha20Provider
        context = SecretContext(b'fixture-install', b'record', b'purpose', 1)
        envelope = self.provider().encrypt(b'synthetic-secret', context)
        self.assertEqual(self.provider().decrypt(envelope, context), b'synthetic-secret')
        public = bytes.fromhex(hashlib.sha256(self.root).hexdigest())
        with self.assertRaises(SecretCryptoError):
            XChaCha20Provider(Keyring('root-v1', {'root-v1': public})).decrypt(envelope, context)
        self.assertNotIn(self.root.decode(), repr(self.provider()))

    def test_wrong_identity_fingerprint_role_and_weak_root_are_refused(self):
        for params in ({'installation_id': 'another'}, {'expected_root_digest': '0' * 64},
                       {'expected_root_digest': 'invalid'}, {'key_id': '../key'}):
            with self.subTest(params=params), self.assertRaises((RuntimeError, ValueError)):
                self.provider(**params)
        for role in ('web', 'worker', 'gateway', 'builder', 'maintenance'):
            self.bootstrap['role'] = role
            with self.subTest(role=role), self.assertRaisesRegex(RuntimeError, 'credential'):
                self.provider()
        self.bootstrap['role'] = 'credential'
        self.write(b'short')
        with self.assertRaisesRegex(RuntimeError, 'root key'):
            self.provider(expected_root_digest=hashlib.sha256(b'short').hexdigest())

    def test_protected_read_and_missing_root_do_not_generate_replacements(self):
        self.path.chmod(0o644)
        with self.assertRaises(RuntimeError):
            self.provider()
        self.path.unlink()
        with self.assertRaises(RuntimeError):
            self.provider()
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    with offline():
        unittest.main()
