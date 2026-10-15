"""Защищённый ввод до БД; не является источником настроек установки."""
import hashlib
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


def private_bytes(path):
    """Открыть принадлежащий процессу файл без symlink и публичного доступа."""
    path = Path(path)
    if not path.is_absolute():
        raise RuntimeError('bootstrap paths must be absolute')
    try:
        for parent in path.parents:
            if parent.is_symlink():
                raise RuntimeError('bootstrap requires a regular path without symlinks')
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
            raise RuntimeError('bootstrap directory must be private and owned by this user')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('bootstrap requires a regular file without symlinks')
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise RuntimeError('bootstrap file must be private and owned by this user')
            data = stream.read(65537)
            if len(data) > 65536:
                raise RuntimeError('bootstrap file exceeds size limit')
            return data
    except OSError as exc:
        raise RuntimeError('bootstrap file unavailable or not a regular file (symlink refused)') from exc


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
    if data['role'] not in {'maintenance', 'web', 'worker', 'gateway'}:
        raise RuntimeError('bootstrap process role is invalid')
    db = data['database']
    required_db = {'ENGINE', 'NAME', 'HOST', 'PORT', 'USER', 'PASSWORD', 'OPTIONS'}
    if not isinstance(db, dict) or set(db) != required_db:
        raise RuntimeError('bootstrap database keys are invalid')
    if db['ENGINE'] != 'django.db.backends.postgresql':
        raise RuntimeError('controller runtime requires PostgreSQL 16 or 17')
    if any(not isinstance(db[k], str) or not db[k] for k in required_db - {'OPTIONS'}):
        raise RuntimeError('bootstrap database coordinates are invalid')
    opts = db['OPTIONS']
    if not isinstance(opts, dict) or set(opts) - {'sslmode', 'sslrootcert', 'sslcert', 'sslkey'}:
        raise RuntimeError('bootstrap database TLS keys are invalid')
    if opts.get('sslmode') not in {'verify-full', 'disable'}:
        raise RuntimeError('bootstrap database requires verify-full TLS or explicit isolated-test disable')
    if opts.get('sslmode') == 'verify-full' and not opts.get('sslrootcert'):
        raise RuntimeError('bootstrap database TLS trust is required')
    if data['role'] != 'maintenance' and 'root_key' in data:
        raise RuntimeError('root key must not be supplied to web, worker or gateway')
    if data['role'] == 'maintenance':
        root = private_bytes(data.get('root_key', ''))
        if len(root) < 32:
            raise RuntimeError('bootstrap root key must contain at least 32 bytes')
    if 'django_key' in data and len(private_bytes(data['django_key'])) < 50:
        raise RuntimeError('bootstrap Django signing key must contain at least 50 bytes')
    return Bootstrap(data)


def root_digest(bootstrap):
    """Fingerprint проверяет custody, не используется как ключ шифрования."""
    return hashlib.sha256(private_bytes(bootstrap['root_key'])).hexdigest()
