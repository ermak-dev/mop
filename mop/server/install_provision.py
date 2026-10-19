"""Явная OS-подготовка нового контроллера; самостоятельный stdlib executor."""
import grp
import json
import os
from pathlib import Path
import pwd
import secrets
import stat
import subprocess


def document(spec):
    return {'installation_id': spec.identity, 'database': dict(spec.database),
            'paths': dict(spec.paths), 'runtime': dict(spec.runtime),
            'identities': dict(spec.identities)}


def _validate(data):
    # Тот же stdlib validator, что у непривилегированного plan.
    from mop.server.installation import specification
    return document(specification(data))


def _absent(path):
    current = Path('/')
    for part in Path(path).parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError('fresh target refuses symlink ancestors')
        if current == Path(path):
            raise RuntimeError('OS preparation requires a new target tree; existing paths are never adopted')
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()) or (
                info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            raise RuntimeError('fresh target has an unsafe ancestor')
    raise RuntimeError('OS preparation requires a new target tree; existing paths are never adopted')


def _reserve(path):
    """Создать новое дерево через dirfd; preflight не даёт права следовать link.

    Возвращает открытый descriptor последнего созданного каталога: venv
    создаётся только по проверенному inode, а не по пути, чей symlink-предок
    мог быть подменён между preflight и привилегированным шагом (#811).
    Закрывает descriptor вызывающий.
    """
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = Path(path).parts[1:]
        for index, part in enumerate(parts):
            info = os.fstat(descriptor)
            if info.st_uid not in (0, os.getuid()) or (
                    info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
                raise RuntimeError('fresh target has an unsafe ancestor')
            if index == len(parts) - 1:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    raise RuntimeError('OS preparation requires a new target tree; '
                                       'existing paths are never adopted') from None
                return os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=descriptor)
            created = False
            try:
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
                created = True
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            if created:
                os.fchmod(child, 0o755)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)


def prepare(data, *, execute=subprocess.run):
    """Все guards до изменений; partial failure никогда не откатывает чужой мир."""
    data = _validate(data)
    if os.geteuid() != 0:
        raise RuntimeError('OS preparation requires explicit root execution')
    if data['database']['topology'] != 'remote':
        raise RuntimeError('provisioned PostgreSQL requires explicit server TLS and network provisioning')
    paths, users = data['paths'], data['identities']
    for path in paths.values():
        _absent(path)
    # Venv объявлена установкой (#807), но свежесть дерева -- то же правило.
    # Preflight _absent -- только проверка; каталог резервируется ниже через
    # dirfd вместе с остальными деревьями (#811).
    _absent(data['runtime']['venv'])
    for user in users.values():
        for lookup in (pwd.getpwnam, grp.getgrnam):
            try:
                lookup(user)
            except KeyError:
                continue
            raise RuntimeError('fresh service identity already exists; explicit maintenance required')

    # Версия python проверяется до первого изменения: установка не
    # даунгрейдит интерпретатор ради старых пакетов (#807).
    from .install_runtime import VENV_TIMEOUT, create_venv, system_python
    system_python(data['runtime']['python'], execute=execute)

    def command(argv):
        result = execute(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, timeout=600, check=False)
        if result.returncode:
            raise RuntimeError('OS preparation failed; partial resources retained; explicit maintenance required')

    # mkdir без exist_ok резервирует новые деревья до packages/accounts. Venv
    # резервируется тем же dirfd-механизмом, что и остальные деревья: её
    # создание не следует symlink-предку, который мог появиться за время apt
    # (#811). Сам venv заполняется ниже, после пакетов сборки:
    # python-ldap/ldap3/nkeys/pynacl -- sdist и собираются на месте.
    for key in ('configuration', 'state', 'backup', 'root_trust'):
        os.close(_reserve(paths[key]))
    venv_descriptor = _reserve(data['runtime']['venv'])
    try:
        # Один apt за прогон: matching postgresql-client и явный список из образа
        # (#807). Без recommends и без пина версии, чтобы apt не притянул другой
        # python вместо системного.
        command(['apt-get', 'install', '-y', '--no-install-recommends',
                 'postgresql-client-' + str(data['database']['major']),
                 *data['runtime']['system_packages']])
        for user in users.values():
            command(['groupadd', '--system', user])
            command(['useradd', '--system', '--gid', user, '--no-create-home',
                     '--shell', '/usr/sbin/nologin', '--password', '!', user])
        for base in ('configuration', 'state'):
            for role, user in users.items():
                path = Path(paths[base]) / role
                path.mkdir(mode=0o700)
                command(['chown', user + ':' + user, str(path)])
            Path(paths[base]).chmod(0o755)
        from .installation import CREDENTIAL_CLIENT_ROLES, shared_directories, specification
        # Frontend-группа не читает private config/state credential-службы (0700).
        for role in CREDENTIAL_CLIENT_ROLES:
            command(['usermod', '--append', '--groups', users['credentials'], users[role]])
        for item in shared_directories(specification(data)):
            shared = Path(item['path'])
            shared.mkdir(mode=int(item['mode'], 8))
            shared.chmod(int(item['mode'], 8))
            command(['chown', item['owner'] + ':' + item['group'], str(shared)])
        signing = secrets.token_hex(32).encode('ascii')
        for role in ('maintenance', 'web', 'worker', 'gateway', 'credentials'):
            path = Path(paths['configuration']) / role / 'django.key'
            _key(path, signing)
            command(['chown', users[role] + ':' + users[role], str(path)])
        # Maintenance recovery и runtime credential имеют разные private custody,
        # но одинаковый root fingerprint; frontends не получают ни bytes, ни traversal.
        root = secrets.token_bytes(32)
        _key(Path(paths['root_trust']) / 'root.key', root)
        credential_root = Path(paths['configuration']) / 'credentials' / 'root.key'
        _key(credential_root, root)
        command(['chown', users['credentials'] + ':' + users['credentials'], str(credential_root)])
        for key in ('backup', 'root_trust'):
            command(['chown', '-R', users['maintenance'] + ':' + users['maintenance'], paths[key]])
        # Venv -- последним: пакеты сборки и заголовки уже стоят, а провал pip не
        # откатывает OS, которую установка уже объявила частичной. Каталог --
        # именно зарезервированный: create_venv сверяет inode (#811).
        create_venv(Path(data['runtime']['venv']), pip_extra=data['runtime']['pip_extra'],
                    wheels=data['runtime'].get('wheels'), execute=execute,
                    timeout=VENV_TIMEOUT, reserved=venv_descriptor)
    finally:
        os.close(venv_descriptor)


def _key(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def apply(spec, *, apply=False, execute=subprocess.run):
    """Только local target: и host, и PVE guest запускают один и тот же путь."""
    if apply is not True:
        raise ValueError('OS provisioning requires explicit apply')
    data = document(spec)
    from .install_runtime import VENV_TIMEOUT
    if data['database']['topology'] != 'remote':
        raise RuntimeError('provisioned PostgreSQL requires explicit server TLS and network provisioning')
    # Explicit apply запускает именно выбранный installer source от root;
    # -I исключает ambient PYTHONPATH и user site. Секретов в argv нет.
    try:
        prefix = [] if os.geteuid() == 0 else ['sudo', '-n']
        launcher = ('import sys; sys.path.insert(0, sys.argv[1]); '
                    'from mop.cli.server.install import provision_target; sys.exit(provision_target())')
        result = execute([*prefix, 'python3', '-I', '-c', launcher,
                          str(Path(__file__).resolve().parents[2])],
                         input=json.dumps(data).encode(), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, timeout=VENV_TIMEOUT + 300, check=False)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('OS preparation unavailable; partial resources retained; explicit maintenance required') from None
    if result.returncode:
        try:
            verdict = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            verdict = None
        if type(verdict) is dict and set(verdict) == {'error'} and type(verdict['error']) is str:
            raise RuntimeError(verdict['error'])
        raise RuntimeError('OS preparation failed; partial resources retained; explicit maintenance required')
    return {'os_prepared': True, 'services_started': False, 'installation_accepted': False}
