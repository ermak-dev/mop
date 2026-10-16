"""Защищённый ввод до БД; не является источником настроек установки."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat


class Bootstrap(dict):
    """Диагностика никогда не показывает содержимое credentials."""
    def __repr__(self):
        return '<controller bootstrap: redacted>'

    __str__ = __repr__


def _protected_open(path, *, private=False, key=False):
    """Проверить всю цепочку через dirfd, не раскрывая путь в ошибке."""
    if not isinstance(path, (str, os.PathLike)):
        raise RuntimeError('bootstrap path must be an absolute filesystem path')
    raw = os.fspath(path)
    if not isinstance(raw, str) or '\x00' in raw:
        raise RuntimeError('bootstrap path must be an absolute filesystem path')
    path = Path(raw)
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError('bootstrap paths must be absolute without parent traversal')
    uid = os.geteuid()
    trusted = {0, uid}
    directory = fd = None
    try:
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in (*path.parts[1:-1], None):
            info = os.fstat(directory)
            if info.st_uid not in trusted or (info.st_mode & 0o022 and
                                              not info.st_mode & stat.S_ISVTX):
                raise RuntimeError('bootstrap path directory permits untrusted replacement')
            # Sticky-каталог не даёт чужому uid заменить доверенного потомка.
            # Владельца каждого потомка проверяем до перехода дальше или чтения.
            if part is None:
                if private and (info.st_uid != uid or info.st_mode & 0o077):
                    raise RuntimeError('bootstrap directory must be private and owned by this user')
                break
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError('bootstrap requires a regular file without symlinks')
        if private and (info.st_uid != uid or info.st_mode & 0o077):
            raise RuntimeError('bootstrap file must be private and owned by this user')
        if info.st_uid not in trusted or info.st_mode & 0o022:
            raise RuntimeError('bootstrap TLS file must have trusted ownership and no untrusted writes')
        if key and stat.S_IMODE(info.st_mode) not in {0o400, 0o600}:
            raise RuntimeError('bootstrap TLS private key requires mode 0600 or 0400')
        stream = os.fdopen(fd, 'rb')
        fd = None
        return stream
    except OSError:
        # Исключение ОС содержит имя файла: не сохраняем его даже в context.
        pass
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)
    raise RuntimeError('bootstrap file unavailable or not a regular file (symlink refused)')


def private_bytes(path):
    """Открыть принадлежащий процессу файл без symlink и публичного доступа."""
    with _protected_open(path, private=True) as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise RuntimeError('bootstrap file exceeds size limit')
    return data


def load(path):
    """Разрешены только координаты БД, identity и ссылки на ключи роли."""
    try:
        data = json.loads(private_bytes(path))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError('bootstrap is not valid JSON') from exc
    required = {'installation_id', 'role', 'database'}
    if not isinstance(data, dict) or not required <= data.keys() or \
            data.keys() - (required | {'root_key', 'django_key'}):
        raise RuntimeError('bootstrap keys are invalid')
    identity = data['installation_id']
    if not isinstance(identity, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', identity):
        raise RuntimeError('bootstrap installation identity is invalid')
    if not isinstance(data['role'], str) or data['role'] not in {'maintenance', 'credential', 'web', 'worker', 'gateway'}:
        raise RuntimeError('bootstrap process role is invalid')
    db = data['database']
    required_db = {'ENGINE', 'NAME', 'HOST', 'PORT', 'USER', 'PASSWORD', 'OPTIONS'}
    if not isinstance(db, dict) or set(db) != required_db:
        raise RuntimeError('bootstrap database keys are invalid')
    if db['ENGINE'] != 'django.db.backends.postgresql':
        raise RuntimeError('controller runtime requires PostgreSQL 16 or 17')
    if any(not isinstance(db[k], str) or not db[k] for k in required_db - {'OPTIONS'}):
        raise RuntimeError('bootstrap database coordinates are invalid')
    if not db['PORT'].isdigit() or not 1 <= int(db['PORT']) <= 65535:
        raise RuntimeError('bootstrap database port is invalid')
    opts = db['OPTIONS']
    if not isinstance(opts, dict) or set(opts) - {'sslmode', 'sslrootcert', 'sslcert', 'sslkey'}:
        raise RuntimeError('bootstrap database TLS keys are invalid')
    if any(not isinstance(value, str) or not value or '\x00' in value for value in opts.values()):
        raise RuntimeError('bootstrap database TLS values are invalid')
    if opts.get('sslmode') not in {'verify-full', 'disable'}:
        raise RuntimeError('bootstrap database requires verify-full TLS or explicit isolated-test disable')
    if opts.get('sslmode') == 'verify-full' and not opts.get('sslrootcert'):
        raise RuntimeError('bootstrap database TLS trust is required')
    if ('sslcert' in opts) != ('sslkey' in opts):
        raise RuntimeError('bootstrap database TLS client certificate and key must be supplied together')
    for name in ('sslrootcert', 'sslcert', 'sslkey'):
        if name in opts:
            with _protected_open(opts[name], key=name == 'sslkey'):
                pass
    if data['role'] not in {'maintenance', 'credential'} and 'root_key' in data:
        raise RuntimeError('root key must not be supplied to web, worker or gateway')
    if data['role'] == 'credential' and 'django_key' not in data:
        raise RuntimeError('credential bootstrap Django signing key is required')
    if data['role'] in {'maintenance', 'credential'}:
        root = private_bytes(data.get('root_key', ''))
        if len(root) < 32:
            raise RuntimeError('bootstrap root key must contain at least 32 bytes')
    if 'django_key' in data:
        signing_key = private_bytes(data['django_key'])
        if len(signing_key) < 50 or not signing_key.isascii():
            raise RuntimeError('bootstrap Django signing key must contain at least 50 ASCII bytes')
    return Bootstrap(data)


def root_digest(bootstrap):
    """Fingerprint проверяет custody, не используется как ключ шифрования."""
    return hashlib.sha256(private_bytes(bootstrap['root_key'])).hexdigest()


def secret_provider(bootstrap, *, installation_id, expected_root_digest, key_id):
    """Credential-композиция; identity/fingerprint берутся из проверенной Installation."""
    from mop.server.secrets.crypto import Keyring, XChaCha20Provider
    if bootstrap.get('role') != 'credential':
        raise RuntimeError('secret root access requires the credential process role')
    if type(installation_id) is not str or not re.fullmatch(
            r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', installation_id) or \
            bootstrap.get('installation_id') != installation_id:
        raise RuntimeError('secret root installation identity mismatch')
    if type(expected_root_digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_root_digest):
        raise RuntimeError('secret root fingerprint is invalid')
    if type(key_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', key_id):
        raise ValueError('invalid encryption key identifier')
    root = private_bytes(bootstrap.get('root_key', ''))
    if len(root) < 32:
        raise RuntimeError('bootstrap root key must contain at least 32 bytes')
    if not hmac.compare_digest(hashlib.sha256(root).hexdigest(), expected_root_digest):
        raise RuntimeError('secret root fingerprint mismatch')
    # RFC 5869: один 32-byte expand block; корень — случайные bytes, не пароль.
    prk = hmac.digest(b'mop.secret.root.hkdf-sha256.v1\x00', root, 'sha256')
    info = b'mop.secret.aead\x00' + installation_id.encode('ascii') + b'\x00' + key_id.encode('ascii')
    key = hmac.digest(prk, info + b'\x01', 'sha256')
    return XChaCha20Provider(Keyring(key_id, {key_id: key}))
