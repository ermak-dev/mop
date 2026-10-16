"""AEAD-граница нового хранилища: только переданные bootstrap-ключи и точные байты.

Никаких файлов, env, ORM, авторизации или доставки. Формат v1 использует
libsodium XChaCha20-Poly1305-IETF; tag включён в ciphertext (последние 16 байт).
Корневые ключи — 32 случайных байта, не пароль. Bootstrap обязан сохранять
соответствие key_id -> материал и не переименовывать тот же ключ при ротации.

192-битный nonce берётся из системного CSPRNG PyNaCl при каждом encrypt.
Случайность не является доказательством уникальности: будущий ORM обязан
иметь unique(key_id, nonce), при конфликте заново зашифровать с новым nonce
до сохранения и не публиковать отвергнутый envelope. Счётчиков, глобального
реестра nonce и гарантий ровно-однократного сохранения здесь нет.

Python не гарантирует стирание immutable bytes из памяти. Доступ к keys и
результату decrypt разрешён только доверенной композиции; redacted repr
не превращает эти объекты в безопасный общий serializer или audit payload.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import re
import struct
from types import MappingProxyType
from typing import Protocol

from nacl import bindings, utils

MAX_PLAINTEXT_BYTES = 1024 * 1024
MAX_CONTEXT_BYTES = 1024
MAX_KEY_ID_BYTES = 64
_NONCE_BYTES = 24
_TAG_BYTES = 16
_MAGIC = b"MOPS"
_VERSION = 1
_MAX_ENVELOPE_BYTES = 7 + MAX_KEY_ID_BYTES + _NONCE_BYTES + MAX_PLAINTEXT_BYTES + _TAG_BYTES


class SecretCryptoError(ValueError):
    """Безопасная категория отказа без входных значений и исключения провайдера."""


def _key_id(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise SecretCryptoError("invalid encryption key identifier")


def _bytes(value, minimum, maximum, message):
    if type(value) is not bytes or not minimum <= len(value) <= maximum:
        raise SecretCryptoError(message)


@dataclass(frozen=True, slots=True, repr=False)
class SecretContext:
    """AAD: непрозрачные идентичности, назначение поля и версия секрета, не revision."""

    installation: bytes
    record: bytes
    purpose: bytes
    secret_version: int

    def __post_init__(self):
        for value in (self.installation, self.record, self.purpose):
            _bytes(value, 1, MAX_CONTEXT_BYTES, "invalid secret context bytes")
        if type(self.secret_version) is not int or not 1 <= self.secret_version < 2 ** 64:
            raise SecretCryptoError("invalid secret version")

    def __repr__(self):
        return "SecretContext(<redacted>)"


def _aad(context, version, key_id):
    if type(context) is not SecretContext:
        raise SecretCryptoError("invalid secret context")
    encoded = key_id.encode("ascii")
    parts = [b"mop.secret.aad\x00", bytes([version]), struct.pack(">H", len(encoded)), encoded]
    for value in (context.installation, context.record, context.purpose):
        parts.extend((struct.pack(">I", len(value)), value))
    parts.append(struct.pack(">Q", context.secret_version))
    return b"".join(parts)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class Keyring:
    """Снимок bootstrap: один ключ записи, старые ключи только для чтения.

    Входная map копируется, mutable buffers отвергаются. Ключи не генерируются
    и не восстанавливаются автоматически при отсутствии/ошибке.
    """

    active_key_id: str
    keys: Mapping[str, bytes]

    def __post_init__(self):
        _key_id(self.active_key_id)
        if not isinstance(self.keys, Mapping):
            raise SecretCryptoError("invalid encryption keyring")
        copied = dict(self.keys)
        for identifier, key in copied.items():
            _key_id(identifier)
            _bytes(key, 32, 32, "invalid encryption key material")
        if self.active_key_id not in copied:
            raise SecretCryptoError("active encryption key is missing")
        if len(set(copied.values())) != len(copied):
            raise SecretCryptoError("duplicate encryption key material")
        object.__setattr__(self, "keys", MappingProxyType(copied))

    def __repr__(self):
        return "Keyring(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class Envelope:
    """Фиксированный бинарный v1: magic, version, длина/id ключа, nonce, ciphertext/tag."""

    version: int
    key_id: str
    nonce: bytes
    ciphertext: bytes

    def __post_init__(self):
        if type(self.version) is not int or self.version != _VERSION:
            raise SecretCryptoError("unsupported secret envelope version")
        _key_id(self.key_id)
        _bytes(self.nonce, _NONCE_BYTES, _NONCE_BYTES, "invalid secret nonce")
        _bytes(self.ciphertext, _TAG_BYTES, MAX_PLAINTEXT_BYTES + _TAG_BYTES,
               "invalid secret ciphertext length")

    def __repr__(self):
        return "Envelope(<redacted>)"

    def to_bytes(self) -> bytes:
        """Точные байты для BinaryField; не текстовая перекодировка секрета."""
        encoded = self.key_id.encode("ascii")
        return (_MAGIC + bytes([self.version]) + struct.pack(">H", len(encoded))
                + encoded + self.nonce + self.ciphertext)

    @classmethod
    def from_bytes(cls, value: bytes) -> "Envelope":
        """Структурная проверка не доказывает подлинность: после неё нужен decrypt."""
        _bytes(value, 7 + 1 + _NONCE_BYTES + _TAG_BYTES, _MAX_ENVELOPE_BYTES,
               "invalid secret envelope length")
        if value[:4] != _MAGIC:
            raise SecretCryptoError("invalid secret envelope header")
        size = struct.unpack(">H", value[5:7])[0]
        if not 1 <= size <= MAX_KEY_ID_BYTES or len(value) < 7 + size + _NONCE_BYTES + _TAG_BYTES:
            raise SecretCryptoError("invalid secret envelope key identifier")
        try:
            key_id = value[7:7 + size].decode("ascii")
        except UnicodeDecodeError:
            key_id = None
        if key_id is None:
            raise SecretCryptoError("invalid secret envelope key identifier")
        offset = 7 + size
        return cls(value[4], key_id, value[offset:offset + _NONCE_BYTES], value[offset + _NONCE_BYTES:])


class CryptoProvider(Protocol):
    """Узкий заменяемый порт; только явные входы, не поиск ambient authority."""

    def encrypt(self, plaintext: bytes, context: SecretContext) -> Envelope: ...

    def decrypt(self, envelope: Envelope, context: SecretContext) -> bytes: ...


@dataclass(frozen=True, slots=True, repr=False)
class XChaCha20Provider:
    """Реализация порта проверенным AEAD из PyNaCl/libsodium."""

    keyring: Keyring

    def __post_init__(self):
        if type(self.keyring) is not Keyring:
            raise SecretCryptoError("invalid encryption keyring")

    def __repr__(self):
        return "XChaCha20Provider(<redacted>)"

    def encrypt(self, plaintext: bytes, context: SecretContext) -> Envelope:
        """Новый nonce на вызов; сохранение/уникальность — ответственность ORM."""
        _bytes(plaintext, 0, MAX_PLAINTEXT_BYTES, "invalid secret plaintext bytes or size")
        identifier = self.keyring.active_key_id
        aad = _aad(context, _VERSION, identifier)
        try:
            nonce = utils.random(_NONCE_BYTES)
            ciphertext = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
                plaintext, aad, nonce, self.keyring.keys[identifier])
        except Exception:
            # Ошибка CSPRNG/библиотеки может содержать входы; наружу только категория.
            pass
        else:
            return Envelope(_VERSION, identifier, nonce, ciphertext)
        # Вне except: даже __context__ не сохраняет сырое исключение провайдера.
        raise SecretCryptoError("secret encryption failed")

    def decrypt(self, envelope: Envelope, context: SecretContext) -> bytes:
        """Нет ключа/подлинности — отказ, не пустой секрет и не fallback."""
        if type(envelope) is not Envelope:
            raise SecretCryptoError("invalid secret envelope")
        aad = _aad(context, envelope.version, envelope.key_id)
        key = self.keyring.keys.get(envelope.key_id)
        if key is None:
            raise SecretCryptoError("secret encryption key is unavailable")
        try:
            return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
                envelope.ciphertext, aad, envelope.nonce, key)
        except Exception:
            pass
        raise SecretCryptoError("secret authentication failed")
