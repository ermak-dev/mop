"""PostgreSQL tooling изолирован здесь; модели и запросы остаются ORM."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

from django.db import connections


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


def _tool(tool, config, *, major, input=None, output=None, extra=(), database=True):
    if config['ENGINE'] not in {'django.db.backends.postgresql', 'mop.server.controller.postgresql'}:
        raise RuntimeError('backup/restore is supported only for PostgreSQL')
    try:
        version = subprocess.run([tool, '--version'], capture_output=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f'PostgreSQL {tool} is unavailable') from exc
    match = re.search(rb'\b(\d+)\.', version.stdout)
    if not match or int(match[1]) != major:
        raise RuntimeError(f'PostgreSQL {major} {tool} is required; configure matching client tools')
    argv = [tool, '--host', config['HOST'], '--port', config['PORT'], '--username', config['USER']]
    if database:
        argv += ['--dbname', config['NAME']]
    argv += extra
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
    return result.stdout


def _require_autocommit(connection, operation):
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError(f'{operation} requires autocommit outside application transactions')


def backup(path, using='default'):
    from .runtime import _maintenance_role, verify_installation
    from .models import RestoreGuard
    _maintenance_role()
    connection = connections[using]
    _require_autocommit(connection, 'backup')
    verify_installation(using)
    path = _private_path(path, exists=False)
    config = connection.settings_dict
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise RuntimeError('backup destination must be a new private file') from exc
    try:
        with os.fdopen(fd, 'wb') as output:
            _tool('pg_dump', config, major=connections[using].pg_version // 10000,
                  output=output, extra=('--format=custom', '--no-owner', '--no-acl',
                                        '--exclude-table=public.' + RestoreGuard._meta.db_table))
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    # Root key не входит в dump: custody/recovery хранится отдельно.
    return {'file': str(path), 'root_key_included': False}


def _arm_restore(using):
    """Пустой target получает штатную схему; arm коммитится до archive I/O."""
    import io
    from django.apps import apps
    from django.core.management import call_command
    from django.db.migrations.recorder import MigrationRecorder
    from .models import RestoreGuard
    from .runtime import _guard_row, _schema_ready, short_transaction
    connection = connections[using]
    if not connection.introspection.table_names():
        output = io.StringIO()
        call_command('migrate', database=using, interactive=False, verbosity=0,
                     stdout=output, stderr=output)
    _schema_ready(using)
    known = set(connection.introspection.django_table_names()) | {MigrationRecorder.Migration._meta.db_table}
    if set(connection.introspection.table_names()) - known:
        raise RuntimeError('restore requires an empty controller target without unrelated tables')
    with short_transaction(using):
        guard = _guard_row(using, lock=True)
        if guard.state != 'uninitialized':
            raise RuntimeError('restore requires an empty target with an uninitialized restore guard')
        # Только штатные metadata seeds допустимы в заранее migrated target.
        for model in apps.get_models(include_auto_created=True):
            if not model._meta.can_migrate(connection):
                continue
            if model in (RestoreGuard,) or model._meta.label_lower in {'auth.permission', 'contenttypes.contenttype'}:
                continue
            if model._default_manager.using(using).exists():
                raise RuntimeError('restore requires an empty target database and stopped services')
        guard.generation = uuid.uuid4()
        guard.state = 'restoring'
        guard.save(using=using, update_fields=['generation', 'state'])
    return guard.generation


def _migration_history(script):
    """Читаем только COPY штатного ledger из pg_restore, не исполняем SQL."""
    match = re.search(rb'^COPY public\.django_migrations \(id, app, name, applied\) FROM stdin;\n(.*?)^\\\.\s*$',
                      script, re.MULTILINE | re.DOTALL)
    if not match:
        raise RuntimeError('backup schema migration history is missing or unsupported')
    rows = set()
    for line in match[1].splitlines():
        columns = line.split(b'\t')
        if len(columns) != 4 or not columns[0].isdigit() or any(
                not re.fullmatch(rb'[A-Za-z0-9_]+', value) for value in columns[1:3]):
            raise RuntimeError('backup schema migration history is malformed')
        row = tuple(value.decode('ascii') for value in columns[1:3])
        if row in rows:
            raise RuntimeError('backup schema migration history contains duplicates')
        rows.add(row)
    return rows


def _archive_plan(source, connection):
    """Guard сохраняется целиком, Installation — с target schema и ACL, но source data."""
    from django.db.migrations.recorder import MigrationRecorder
    from .models import Installation, RestoreGuard
    config, major = connection.settings_dict, connection.pg_version // 10000
    def inspect(extra):
        source.seek(0)
        return _tool('pg_restore', config, major=major, input=source, extra=extra, database=False)
    toc = inspect(('--list',))
    guard = RestoreGuard._meta.db_table.encode('ascii').lower()
    installation = Installation._meta.db_table.encode('ascii').lower()
    table_entry = re.compile(
        rb'^\d+;\s+\d+\s+\d+\s+(?:TABLE DATA|FK CONSTRAINT|CONSTRAINT|DEFAULT|TRIGGER|RULE|POLICY|TABLE)'
        rb'\s+\S+\s+("[^"]*"|\S+)(?:\s|$)')
    for line in toc.splitlines():
        entry = table_entry.match(line)
        if entry and entry[1].strip(b'"').lower() == guard:
            raise RuntimeError('backup contains forbidden restore guard entries')
    schema = inspect(('--schema-only', '--file=-', '--no-owner', '--no-acl'))
    if guard in schema.lower():
        raise RuntimeError('backup schema references the forbidden restore guard')
    history = _migration_history(inspect(('--data-only', '--table=django_migrations', '--file=-')))
    expected = set(MigrationRecorder(connection).migration_qs.values_list('app', 'name'))
    if history != expected:
        raise RuntimeError('backup schema migration history does not match the controller')
    plan = []
    for line in toc.splitlines(keepends=True):
        if re.match(rb'^\d+;\s+\d+\s+\d+\s+DATABASE\b', line):
            raise RuntimeError('backup must not contain database creation entries')
        if re.match(rb'^\d+;\s+\d+\s+\d+\s+SCHEMA\b', line):
            if not re.match(rb'^\d+;\s+\d+\s+\d+\s+SCHEMA\s+-\s+public\s', line):
                raise RuntimeError('backup contains an unsupported schema')
            continue
        entry = table_entry.match(line)
        if entry and entry[1].strip(b'"').lower() == installation and not re.match(
                rb'^\d+;\s+\d+\s+\d+\s+TABLE DATA\s', line):
            # Target уже migrated и sealed; DROP/CREATE вернул бы default CRUD
            # сразу после archive COMMIT, раньше проверки root и завершения fence.
            continue
        plan.append(line)
    return b''.join(plan)


def _verify_restored_secrets(using):
    from django.apps import apps
    if not apps.is_installed('mop.server.secrets'):
        return
    from django.conf import settings
    from .bootstrap import secret_provider
    from .models import Installation
    from mop.server.secrets.crypto import SecretCryptoError
    from mop.server.secrets.services import verify_storage, StorageUnavailable
    installation = Installation.objects.using(using).get(pk=1)
    def provider(key_id):
        return secret_provider(settings.BOOTSTRAP, installation_id=installation.identity,
            expected_root_digest=installation.root_digest, key_id=key_id)
    failure = None
    try:
        verify_storage(provider, installation_id=installation.identity, using=using)
    except SecretCryptoError:
        failure = 'restored secret authentication failed; maintenance recovery required'
    except StorageUnavailable:
        failure = 'restored secret storage is unavailable; maintenance recovery required'
    if failure:
        raise RuntimeError(failure)


def restore(path, using='default'):
    from .runtime import _maintenance_role, _guard_row, _verify_installation_state, short_transaction
    from .models import Installation
    _maintenance_role()
    connection = connections[using]
    _require_autocommit(connection, 'restore')
    path = _private_path(path, exists=True)
    generation = _arm_restore(using)
    # После arm никакой finally не объявляет target готовым: даже SystemExit,
    # потеря COMMIT reply и авария процесса должны оставлять durable fence.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source, tempfile.TemporaryDirectory(prefix='mop-restore-plan-') as directory:
        plan = Path(directory) / 'archive.list'
        plan.write_bytes(_archive_plan(source, connection))
        source.seek(0)
        _tool('pg_restore', connection.settings_dict, major=connection.pg_version // 10000, input=source,
              extra=('--single-transaction', '--exit-on-error', '--no-owner', '--no-acl',
                     '--clean', '--if-exists', '--use-list=' + str(plan)))
    with short_transaction(using):
        guard = _guard_row(using, lock=True)
        if guard.state != 'restoring' or guard.generation != generation:
            raise RuntimeError('restore guard generation changed during restore')
        row = Installation.objects.using(using).select_for_update().get(pk=1)
        row.epoch = uuid.uuid4()
        row.effects_enabled = False
        row.save(using=using, update_fields=['epoch', 'effects_enabled'])
    # Root-файл и schema loader читаются без SQL locks; guard ещё dirty.
    _verify_installation_state(using)
    _verify_restored_secrets(using)
    with short_transaction(using):
        guard = _guard_row(using, lock=True)
        current = Installation.objects.using(using).select_for_update().get(pk=1)
        if guard.state != 'restoring' or guard.generation != generation:
            raise RuntimeError('restore guard generation changed before completion')
        if current.epoch != row.epoch or current.effects_enabled:
            raise RuntimeError('restored installation fence changed before completion')
        # Archive --no-acl пересоздаёт Installation с target default CRUD.
        # Возвращаем trust exception в той же completion transaction, прежде
        # чем guard снова разрешит runtime. Проверка внешней БД прав не чинит.
        from ..install_postgresql import seal_trust_tables
        seal_trust_tables(connection.connection)
        guard.state = 'ready'
        guard.save(using=using, update_fields=['state'])
    return {'epoch': str(row.epoch), 'effects_enabled': False}
