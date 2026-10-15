"""PostgreSQL tooling изолирован здесь; модели и запросы остаются ORM."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

from django.conf import settings
from django.db import connections, transaction


def _private_path(path, *, exists):
    path = Path(path)
    if not path.is_absolute():
        raise RuntimeError('backup path must be absolute')
    try:
        if any(parent.is_symlink() for parent in path.parents):
            raise RuntimeError('backup path must not contain symlinks')
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
            raise RuntimeError('backup directory must be private and owned by this user')
        if exists:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise RuntimeError('backup must be a private regular file owned by this user')
    except OSError as exc:
        raise RuntimeError('backup path is unavailable') from exc
    return path


@contextmanager
def _tool_environment(config):
    """Password не находится ни в argv, ни в постоянном environment-файле."""
    def escape(value):
        return str(value).replace('\\', '\\\\').replace(':', '\\:')
    with tempfile.TemporaryDirectory(prefix='mop-pg-tool-') as directory:
        password = Path(directory) / 'pgpass'
        password.write_text(':'.join(escape(config[key]) for key in
                                     ('HOST', 'PORT', 'NAME', 'USER', 'PASSWORD')) + '\n')
        password.chmod(0o600)
        env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
        env['PGPASSFILE'] = str(password)
        env['PGCONNECT_TIMEOUT'] = str(config['OPTIONS'].get('connect_timeout', 5))
        for key, name in {'sslmode': 'PGSSLMODE', 'sslrootcert': 'PGSSLROOTCERT',
                          'sslcert': 'PGSSLCERT', 'sslkey': 'PGSSLKEY'}.items():
            if key in config['OPTIONS']:
                env[name] = config['OPTIONS'][key]
        yield env


def _tool(tool, config, *, major, input=None, output=None, extra=()):
    if config['ENGINE'] != 'django.db.backends.postgresql':
        raise RuntimeError('backup/restore is supported only for PostgreSQL')
    try:
        version = subprocess.run([tool, '--version'], capture_output=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f'PostgreSQL {tool} is unavailable') from exc
    match = re.search(rb'\b(\d+)\.', version.stdout)
    if not match or int(match[1]) != major:
        raise RuntimeError(f'PostgreSQL {major} {tool} is required; configure matching client tools')
    argv = [tool, '--host', config['HOST'], '--port', config['PORT'],
            '--username', config['USER'], '--dbname', config['NAME'], *extra]
    try:
        with _tool_environment(config) as env:
            result = subprocess.run(argv, stdin=input, stdout=output or subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'PostgreSQL {tool} unavailable or timed out') from exc
    if result.returncode:
        # Tool — недоверенный внешний adapter. Убираем credential один раз.
        reason = result.stderr.decode('utf-8', errors='replace')
        secret = config.get('PASSWORD')
        if secret:
            reason = reason.replace(secret, '[redacted]')
        reason = ' '.join(reason.split())[:600]
        raise RuntimeError(f'PostgreSQL {tool} failed: {reason or result.returncode}')


def backup(path, using='default'):
    from .runtime import _maintenance_role, verify_installation
    _maintenance_role()
    verify_installation(using)
    path = _private_path(path, exists=False)
    config = connections[using].settings_dict
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise RuntimeError('backup destination must be a new private file') from exc
    try:
        with os.fdopen(fd, 'wb') as output:
            _tool('pg_dump', config, major=connections[using].pg_version // 10000,
                  output=output, extra=('--format=custom', '--no-owner', '--no-acl'))
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    # Root key не входит в dump: custody/recovery хранится отдельно.
    return {'file': str(path), 'root_key_included': False}


def restore(path, using='default'):
    from .runtime import _maintenance_role, verify_installation
    from .models import Installation
    _maintenance_role()
    connection = connections[using]
    if connection.introspection.table_names():
        raise RuntimeError('restore requires an empty target database and stopped services')
    path = _private_path(path, exists=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        _tool('pg_restore', connection.settings_dict, major=connection.pg_version // 10000, input=source,
              extra=('--single-transaction', '--exit-on-error', '--no-owner', '--no-acl'))
    try:
        verify_installation(using)
    except Exception:
        # Не включаем dispatcher при несовпадении schema/identity/root.
        # Частичную/непроверенную БД оператор не принимает за чистую установку.
        raise
    with transaction.atomic(using=using):
        row = Installation.objects.using(using).select_for_update().get(pk=1)
        row.epoch = uuid.uuid4()
        row.effects_enabled = False
        row.save(using=using, update_fields=['epoch', 'effects_enabled'])
    return {'epoch': str(row.epoch), 'effects_enabled': False}
