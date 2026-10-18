#!/usr/bin/env python3
"""Проверки изолированной криптографической границы #637, без секретов установки.

HYPOTHESIS: AEAD с явным контекстом исключает перестановку защищённых значений.
Сначала проверяется наличие публичного поведения, а не ошибка импорта.
SOLUTION: PyNaCl XChaCha20-Poly1305, immutable bootstrap keyring и бинарный v1.
RESULT: локальные проверки проходят; ORM и реальные credentials не затрагиваются.
STATUS: FIXED — see #637 (только независимая криптографическая часть).
"""
import importlib
import pathlib
import struct
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import hermetic  # noqa: F401 -- изоляция установки до импорта mop
from _lib import offline


class DeclaredDependencyTests(unittest.TestCase):
    def test_declared_pynacl_aead_is_available(self):
        from nacl.bindings import (crypto_aead_xchacha20poly1305_ietf_encrypt,
                                   crypto_aead_xchacha20poly1305_ietf_decrypt)
        key, nonce = b"k" * 32, b"n" * 24
        encrypted = crypto_aead_xchacha20poly1305_ietf_encrypt(b"fixture", b"aad", nonce, key)
        self.assertEqual(crypto_aead_xchacha20poly1305_ietf_decrypt(encrypted, b"aad", nonce, key),
                         b"fixture")


class CryptoTests(unittest.TestCase):
    def setUp(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "mop/server/secrets/crypto.py"
        self.assertTrue(path.exists(), "purpose-bound AEAD provider is not implemented")
        self.c = importlib.import_module("mop.server.secrets.crypto")
        c = self.c
        self.keys = {"root-v1": b"a" * 32}
        self.ring = c.Keyring("root-v1", self.keys)
        self.provider = c.XChaCha20Provider(self.ring)
        self.context = c.SecretContext(b"installation", b"record", b"private-key", 1)
        self.plaintext = "synthetic private material: ключ\x00".encode()

    def rejected(self, call):
        with self.assertRaises(self.c.SecretCryptoError) as caught:
            call()
        text = str(caught.exception) + repr(caught.exception)
        self.assertNotIn(self.plaintext.decode(), text)
        self.assertNotIn("a" * 32, text)
        self.assertIsNone(caught.exception.__cause__)
        return caught.exception

    def test_roundtrip_exact_bytes_and_random_ciphertexts(self):
        c = self.c
        for value in (b"", b"\x00\xff", self.plaintext, b"x" * c.MAX_PLAINTEXT_BYTES):
            first = self.provider.encrypt(value, self.context)
            second = self.provider.encrypt(value, self.context)
            self.assertEqual(self.provider.decrypt(first, self.context), value)
            self.assertIs(type(self.provider.decrypt(first, self.context)), bytes)
            self.assertNotEqual(first.nonce, second.nonce)
            self.assertNotEqual(first.ciphertext, second.ciphertext)
            self.assertEqual(first.key_id, "root-v1")
            self.assertEqual(first.version, 1)
            self.assertEqual(len(first.nonce), 24)
            self.assertEqual(len(first.ciphertext), len(value) + 16)
            self.assertEqual(c.Envelope.from_bytes(first.to_bytes()), first)
        self.rejected(lambda: self.provider.encrypt(b"x" * (c.MAX_PLAINTEXT_BYTES + 1), self.context))

    def test_each_context_binding_authenticates_and_has_unambiguous_encoding(self):
        envelope = self.provider.encrypt(self.plaintext, self.context)
        for field, value in (("installation", b"elsewhere"), ("record", b"other"),
                             ("purpose", b"password"), ("secret_version", 2)):
            self.rejected(lambda: self.provider.decrypt(envelope, replace(self.context, **{field: value})))
        left = self.c.SecretContext(b"ab", b"c", b"d", 1)
        right = self.c.SecretContext(b"a", b"bc", b"d", 1)
        self.rejected(lambda: self.provider.decrypt(self.provider.encrypt(b"x", left), right))

    def test_old_key_decryption_and_unknown_wrong_missing_keys_fail_closed(self):
        c = self.c
        old = self.provider.encrypt(self.plaintext, self.context)
        new = c.XChaCha20Provider(c.Keyring("root-v2", {**self.keys, "root-v2": b"b" * 32}))
        self.assertEqual(new.decrypt(old, self.context), self.plaintext)
        self.assertEqual(new.encrypt(b"new", self.context).key_id, "root-v2")
        missing = c.XChaCha20Provider(c.Keyring("root-v2", {"root-v2": b"b" * 32}))
        self.rejected(lambda: missing.decrypt(old, self.context))
        wrong = c.XChaCha20Provider(c.Keyring("root-v1", {"root-v1": b"b" * 32}))
        self.rejected(lambda: wrong.decrypt(old, self.context))
        self.rejected(lambda: c.Keyring("root-v1", {}))
        self.rejected(lambda: c.Keyring("root-v1", {"root-v1": b"short"}))
        self.rejected(lambda: c.Keyring("root-v1", {"root-v1": b"a" * 32, "alias": b"a" * 32}))

    def test_tamper_every_envelope_component_and_no_plaintext_fallback(self):
        c = self.c
        envelope = self.provider.encrypt(self.plaintext, self.context)
        for field in ("nonce", "ciphertext"):
            value = getattr(envelope, field)
            damaged = replace(envelope, **{field: bytes([value[0] ^ 1]) + value[1:]})
            self.rejected(lambda: self.provider.decrypt(damaged, self.context))
        alias = c.XChaCha20Provider(c.Keyring("different", {"different": b"a" * 32}))
        self.rejected(lambda: alias.decrypt(replace(envelope, key_id="different"), self.context))
        self.rejected(lambda: replace(envelope, version=2))
        raw = envelope.to_bytes()
        for malformed in (b"", self.plaintext, raw[:-1], raw + b"extra", raw[:4],
                          b"xxxx" + raw[4:], raw[:4] + b"\x02" + raw[5:]):
            # Обрезанный ciphertext структурно допустим, но не аутентичен.
            self.rejected(lambda: self.provider.decrypt(c.Envelope.from_bytes(malformed), self.context))
        for malformed in (b"", b"x" * 23, b"x" * 25):
            self.rejected(lambda: replace(envelope, nonce=malformed))
        self.rejected(lambda: replace(envelope, ciphertext=b"x" * 15))
        self.rejected(lambda: c.Envelope.from_bytes(b"x" * (c.MAX_PLAINTEXT_BYTES + 4096)))

    def test_immutable_inputs_and_safe_representations(self):
        c = self.c
        envelope = self.provider.encrypt(self.plaintext, self.context)
        self.keys["root-v1"] = b"b" * 32
        self.keys.clear()
        self.assertEqual(self.provider.decrypt(envelope, self.context), self.plaintext)
        for obj in (self.ring, envelope, self.context, self.provider):
            self.assertNotIn("a" * 32, repr(obj))
            self.assertNotIn(self.plaintext.decode(), repr(obj))
            self.assertNotIn("installation", repr(obj))
        for obj, field, value in ((self.ring, "active_key_id", "other"),
                                  (envelope, "nonce", b"x" * 24),
                                  (self.context, "record", b"other"),
                                  (self.provider, "keyring", None)):
            with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
                setattr(obj, field, value)
        with self.assertRaises(TypeError):
            self.ring.keys["root-v1"] = b"b" * 32
        for mutable in (bytearray(b"a" * 32), memoryview(b"a" * 32)):
            self.rejected(lambda: c.Keyring("root-v1", {"root-v1": mutable}))
        self.rejected(lambda: self.provider.encrypt(bytearray(b"mutable"), self.context))
        self.rejected(lambda: replace(self.context, record=bytearray(b"mutable")))
        self.rejected(lambda: replace(envelope, ciphertext=bytearray(envelope.ciphertext)))
        self.rejected(lambda: c.Envelope.from_bytes(bytearray(envelope.to_bytes())))

    def test_strict_types_limits_and_rng_failure_are_safe(self):
        c = self.c
        for value in (True, 0, -1, 2 ** 64, "1"):
            self.rejected(lambda: replace(self.context, secret_version=value))
        for field in ("installation", "record", "purpose"):
            for value in (b"", b"x" * (c.MAX_CONTEXT_BYTES + 1), "text", None):
                self.rejected(lambda: replace(self.context, **{field: value}))
            c.SecretContext(**{**dict(installation=b"i", record=b"r", purpose=b"p", secret_version=1),
                               field: b"x" * c.MAX_CONTEXT_BYTES})
        for key_id in ("", "x" * (c.MAX_KEY_ID_BYTES + 1), "ключ", "bad\nkey", b"bytes"):
            self.rejected(lambda: c.Keyring(key_id, {key_id: b"a" * 32}))
        self.rejected(lambda: self.provider.encrypt(b"x", None))
        self.rejected(lambda: self.provider.decrypt(b"plaintext", self.context))
        with patch("nacl.utils.random", side_effect=RuntimeError(self.plaintext.decode())):
            self.rejected(lambda: self.provider.encrypt(self.plaintext, self.context))

    def test_vetted_aead_interoperability_and_wire_version(self):
        from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_decrypt
        envelope = self.provider.encrypt(self.plaintext, self.context)
        # Формат v1 фиксирован; длины не зависят от разделителей в контексте.
        key_id = b"root-v1"
        aad = b"mop.secret.aad\x00" + bytes([1]) + struct.pack(">H", len(key_id)) + key_id
        for value in (self.context.installation, self.context.record, self.context.purpose):
            aad += struct.pack(">I", len(value)) + value
        aad += struct.pack(">Q", self.context.secret_version)
        self.assertEqual(crypto_aead_xchacha20poly1305_ietf_decrypt(
            envelope.ciphertext, aad, envelope.nonce, b"a" * 32), self.plaintext)
        self.assertEqual(envelope.to_bytes(), b"MOPS\x01" + struct.pack(">H", len(key_id))
                         + key_id + envelope.nonce + envelope.ciphertext)

    def test_provider_failures_do_not_keep_raw_exception_context(self):
        envelope = self.provider.encrypt(self.plaintext, self.context)
        for operation, symbol in (
            (lambda: self.provider.encrypt(self.plaintext, self.context),
             "nacl.bindings.crypto_aead_xchacha20poly1305_ietf_encrypt"),
            (lambda: self.provider.decrypt(envelope, self.context),
             "nacl.bindings.crypto_aead_xchacha20poly1305_ietf_decrypt"),
        ):
            with patch(symbol, side_effect=RuntimeError(self.plaintext.decode())):
                error = self.rejected(operation)
                self.assertIsNone(error.__context__)

    # BUG/HYPOTHESIS (#742): один SecretCryptoError смешивает bad tag и
    # недоступность provider/key; Access превращает оба в policy Denied.
    # Отдельный тип availability обязан по-прежнему наследовать безопасный
    # SecretCryptoError, но не включать raw exception/cause/context.
    # SOLUTION: unavailable -- отдельный subtype; PyNaCl authentication остаётся
    # базовым отказом, raw provider errors санитизируются вне except.
    # RESULT: RED для key/runtime/UnavailableError, GREEN с прежними tamper checks.
    # STATUS: FIXED — see #742.
    def test_missing_key_and_provider_outage_have_typed_safe_availability_not_authentication_failure(self):
        c = self.c
        envelope = self.provider.encrypt(self.plaintext, self.context)
        missing = c.XChaCha20Provider(c.Keyring('root-v2', {'root-v2': b'b'*32}))
        with self.subTest(provider_error='missing_key'):
            error = self.rejected(lambda: missing.decrypt(envelope, self.context))
            self.assertEqual(type(error).__name__, 'SecretCryptoUnavailable')
            self.assertEqual(str(error), 'secret encryption key is unavailable')
        from nacl import exceptions
        for failure in (RuntimeError, exceptions.RuntimeError, exceptions.UnavailableError):
            with self.subTest(provider_error=failure), patch(
                    'nacl.bindings.crypto_aead_xchacha20poly1305_ietf_decrypt',
                    side_effect=failure(self.plaintext.decode())):
                error = self.rejected(lambda: self.provider.decrypt(envelope, self.context))
                self.assertEqual(type(error).__name__, 'SecretCryptoUnavailable')
                self.assertEqual(str(error), 'secret decryption failed')
                self.assertIsNone(error.__context__)

    def test_bad_tag_wrong_key_and_invalid_context_are_not_crypto_availability(self):
        c = self.c
        envelope = self.provider.encrypt(self.plaintext, self.context)
        wrong = c.XChaCha20Provider(c.Keyring('root-v1', {'root-v1': b'b'*32}))
        for decrypt in (
                lambda: wrong.decrypt(envelope, self.context),
                lambda: self.provider.decrypt(envelope, replace(self.context, record=b'wrong')),
                lambda: self.provider.decrypt(replace(envelope,
                    ciphertext=envelope.ciphertext[:-1]+bytes([envelope.ciphertext[-1]^1])), self.context),
                lambda: self.provider.decrypt(envelope, None)):
            self.assertIs(type(self.rejected(decrypt)), c.SecretCryptoError)

    def test_root_keyrings_with_different_material_are_not_equal(self):
        # Сравнение только active_key_id скрывает ошибочно подменённый bootstrap.
        other = self.c.Keyring("root-v1", {"root-v1": b"b" * 32})
        self.assertNotEqual(self.ring, other)


if __name__ == "__main__":
    with offline():
        unittest.main()
