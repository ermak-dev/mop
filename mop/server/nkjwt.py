"""Ключи nkey, ящики xkey и JWT NATS (#206). Данные, без печати.

JWT -- как у github.com/nats-io/jwt v2: заголовок {typ: JWT, alg:
ed25519-nkey}, тело JSON, подпись ed25519 ключом издателя, всё base64url без
выравнивания; jti -- base32 от sha256 тела с пустым jti. Формат сверен с
настоящим nats-server 2.14.6: ответ, подписанный здесь, сервер принял, а
tests/callout.py повторяет его байт в байт (стенд #203/#206).

Ящик xkey -- curve25519 box Go-библиотеки nkeys: "xkv1" + nonce(24) +
шифротекст.

Библиотеки -- nkeys (подпись) и PyNaCl (всё остальное). python-nkeys 0.2.1
годится не целиком: KeyPair(public_key=...).verify без seed падает
AttributeError, а encode_seed не знает префикса curve (ErrInvalidPrefixByte),
-- поэтому проверка подписи и ключи xkey здесь прямо на PyNaCl.
"""
import base64
import binascii
import hashlib
import json
import time

import nkeys
from nacl import public, signing
from nacl.exceptions import BadSignatureError, CryptoError

# Префикс ключа curve (X...): в python-nkeys его нет.
PREFIX_BYTE_CURVE = 23 << 3
XKEY_VERSION = b"xkv1"


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b32(raw):
    return base64.b32encode(bytes(raw)).decode().rstrip("=")


def _unb32(text):
    return base64.b32decode(text + "=" * (-len(text) % 8))


def _checked(data):
    return bytearray(data) + nkeys.crc16_checksum(bytearray(data))


def _encode_public(prefix, raw):
    return _b32(_checked(bytearray([prefix]) + bytearray(raw)))


def _decode_public(key):
    """Публичный ключ -> (префикс, 32 байта). Битый -- ValueError."""
    try:
        raw = _unb32(key)
    except (binascii.Error, TypeError) as e:
        raise ValueError(f"{key!r} is not an nkey: {e}")
    if len(raw) != 35 or nkeys.crc16_checksum(bytearray(raw[:-2])) != raw[-2:]:
        raise ValueError(f"{key!r} is not an nkey")
    return raw[0], raw[1:-2]


def _encode_seed(raw, prefix):
    return _b32(_checked(bytearray([nkeys.PREFIX_BYTE_SEED | prefix >> 5,
                                    (prefix & 31) << 3]) + bytearray(raw)))


def _decode_seed(seed):
    """Seed -> (префикс ключа, 32 байта)."""
    raw = _unb32(seed)
    if len(raw) != 36 or nkeys.crc16_checksum(bytearray(raw[:-2])) != raw[-2:]:
        raise ValueError("not an nkey seed")
    return ((raw[0] & 7) << 5) | (raw[1] >> 3), raw[2:-2]


def new_account_key():
    """Новый ключ аккаунта-издателя: seed (SA...)."""
    return _encode_seed(bytes(signing.SigningKey.generate()), nkeys.PREFIX_BYTE_ACCOUNT)


def new_xkey():
    """Новая пара curve25519: (seed SX..., публичный X...)."""
    sk = public.PrivateKey.generate()
    return (_encode_seed(bytes(sk), PREFIX_BYTE_CURVE),
            _encode_public(PREFIX_BYTE_CURVE, bytes(sk.public_key)))


def public_of(seed):
    """Seed -> публичный ключ того же рода (A... у аккаунта, X... у xkey)."""
    prefix, raw = _decode_seed(seed)
    if prefix == PREFIX_BYTE_CURVE:
        return _encode_public(prefix, bytes(public.PrivateKey(bytes(raw)).public_key))
    return _encode_public(prefix, bytes(signing.SigningKey(bytes(raw)).verify_key))


def xkey_open(seed, sender, data):
    """Открыть ящик от sender (X...) своим seed. Не ящик -- ValueError."""
    prefix, raw = _decode_public(sender)
    if prefix != PREFIX_BYTE_CURVE:
        raise ValueError(f"{sender} is not a curve key")
    if not data.startswith(XKEY_VERSION) or len(data) < 28:
        raise ValueError("not an xkv1 box")
    _, mine = _decode_seed(seed)
    try:
        return public.Box(public.PrivateKey(bytes(mine)), public.PublicKey(bytes(raw))) \
            .decrypt(data[28:], data[4:28])
    except CryptoError as e:
        raise ValueError(f"the box does not open: {e}")


def xkey_seal(seed, recipient, data):
    """Запечатать data для recipient (X...) своим seed."""
    _, raw = _decode_public(recipient)
    _, mine = _decode_seed(seed)
    sealed = public.Box(public.PrivateKey(bytes(mine)), public.PublicKey(bytes(raw))).encrypt(data)
    return XKEY_VERSION + sealed.nonce + sealed.ciphertext


def _dump(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


def encode(claims, seed, now=None):
    """Подписанный JWT. iss, iat и jti дописываются здесь; now -- для
    проверок, чтобы подпись была воспроизводимой."""
    kp = nkeys.from_seed(bytearray(seed.encode()))
    body = dict(claims, iss=kp.public_key.decode(),
                iat=int(time.time() if now is None else now), jti="")
    body["jti"] = _b32(hashlib.sha256(_dump(body)).digest())
    signed = f"{_b64(_dump({'typ': 'JWT', 'alg': 'ed25519-nkey'}))}.{_b64(_dump(body))}"
    return f"{signed}.{_b64(kp.sign(signed.encode()))}"


def decode(token):
    """Тело JWT с проверкой подписи ключом из iss. Подделка -- ValueError.
    Срок (exp) не проверяется: подпись -- единственное, что здесь решает."""
    try:
        head, payload, sig = token.split(".")
        body = json.loads(_unb64(payload))
        _, raw = _decode_public(body["iss"])
        signing.VerifyKey(bytes(raw)).verify(f"{head}.{payload}".encode(), _unb64(sig))
    except (BadSignatureError, binascii.Error, KeyError, TypeError, ValueError) as e:
        raise ValueError(f"not a valid JWT: {type(e).__name__}: {e}")
    return body
