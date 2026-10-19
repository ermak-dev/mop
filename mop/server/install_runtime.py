"""Runtime установки: системный python и приватный venv контроллера (#807).

Модуль только данных и subprocess, без импорта контроллера: его зовёт
привилегированный installer до того, как в системе появится хоть одна
библиотека Django. apt здесь не ставится -- системные пакеты ставит
`install_provision` одним вызовом вместе с postgresql-client; здесь --
проверка системного python, venv, объявленный pip extra и проверка импортов.

Интерпретатор -- установленный, взятый абсолютным путём: каждый шаг читает
`sys.executable` и не перечитывает PATH после гейта (#811). Venv создаётся
только в свежезарезервированном каталоге, а существующий принимается лишь
как принадлежащий установке; чужой `bin/python` не исполняется (#811).

Проверка и исполнение идут через один удержанный descriptor каталога venv:
интерпретатор запускается как `bin/python` с cwd, привязанным к fd, а не
строкой пути, которую между проверкой и exec можно подменить symlink'ом
(#811). Тот же fd доказывает, что удаляется именно зарезервированное дерево.
"""
from pathlib import Path
import os
import re
import stat
import subprocess
import sys
import urllib.parse

# Ниже этого установка не идёт: pyproject.toml объявляет requires-python >=3.12.
MINIMUM = (3, 12)
# Ровно то, что нужно контроллеру, чтобы стартовать (issue #807).
IMPORTS = ('django', 'rest_framework', 'channels', 'psycopg', 'uvicorn', 'ldap', 'django_auth_ldap')
REQUIREMENT = re.compile(r'^>=(\d+)\.(\d+)$')
VERSION_PROBE = 'import sys; sys.stdout.write("%d.%d.%d\\n" % sys.version_info[:3])'
# Бюджет всей venv-цепочки: venv, pip upgrade, extra, import verification
# (#807). Каждый шаг получает долю, поэтому сумма не превышает бюджет и
# внешний kill установщика (VENV_TIMEOUT + запас) достижим (#811).
VENV_TIMEOUT = 1800
VENV_SHARES = {'venv creation': 3, 'pip upgrade': 1, 'extra': 2, 'import verification': 1}
# Ключи URL, значение которых -- учётные данные: query-параметр и сегмент
# пути после такого ключа не попадают в отчёт оператору (#811).
CREDENTIAL_KEY = re.compile(r'(?i)^(token|secret|password|passwd|pwd|key|apikey|api[-_]key|'
                            r'access[-_]token|refresh[-_]token|auth|authorization|'
                            r'credential|credentials|sig|signature)$')
# Ключ сегмента может быть также парой `token=...` внутри пути (#811).
SEGMENT_KEY = re.compile(r'(?i)^([A-Za-z][A-Za-z0-9_.-]*)=(.*)$')
URL = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://[^\s]+')


def requirement_minimum(requirement):
    """Минимум из объявленного `>=X.Y`; иной формы требование не бывает."""
    match = REQUIREMENT.fullmatch(requirement) if type(requirement) is str else None
    if match is None:
        raise ValueError('runtime python requires an explicit >=X.Y requirement')
    minimum = (int(match.group(1)), int(match.group(2)))
    if minimum < MINIMUM:
        raise ValueError('runtime python requirement is below 3.12')
    return minimum


def _failed(step, detail=''):
    return RuntimeError('runtime installation failed at ' + step + (': ' + detail if detail else ''))


def interpreter():
    """Абсолютный установленный интерпретатор; PATH после гейта не перечитывается.

    `python3` из PATH -- то же имя, которое apt способен подменить, а
    установщик уже проверил версию конкретного файла (#811).
    """
    executable = getattr(sys, 'executable', '')
    if not executable:
        raise RuntimeError('system python3 is required for the declared runtime')
    return os.path.realpath(executable)


def _redact_url(match):
    """URL без userinfo и без значений credential-ключей (#811).

    Прежняя редакция знала только `user:password@`: `?token=...` и
    `/token/<secret>/` уезжали в отчёт оператору как есть. Здесь скрывается
    userinfo, значение query-параметра с credential-именем и сегмент пути,
    идущий сразу после такого имени.
    """
    try:
        parts = urllib.parse.urlsplit(match.group(0))
    except ValueError:
        return '<redacted>'
    netloc = parts.netloc
    if '@' in netloc:
        netloc = '<redacted>@' + netloc.rsplit('@', 1)[1]
    segments = parts.path.split('/')
    for index, segment in enumerate(segments[:-1]):
        plain = urllib.parse.unquote(segment)
        if CREDENTIAL_KEY.fullmatch(plain):
            segments[index + 1] = '<redacted>'
        else:
            # Форма `/token=<value>/`: ключ и значение -- один сегмент.
            paired = SEGMENT_KEY.fullmatch(plain)
            if paired and CREDENTIAL_KEY.fullmatch(paired.group(1)):
                segments[index] = paired.group(1) + '=<redacted>'
    query = []
    for pair in parts.query.split('&'):
        name, separator, _ = pair.partition('=')
        if separator and (CREDENTIAL_KEY.fullmatch(name) or
                          CREDENTIAL_KEY.fullmatch(urllib.parse.unquote(name))):
            query.append(name + '=<redacted>')
        else:
            query.append(pair)
    redacted = urllib.parse.urlunsplit(
        (parts.scheme, netloc, '/'.join(segments), '&'.join(query), parts.fragment))
    return redacted


def _redacted(text):
    """Строка отказа без учётных данных URL; прочий текст не трогается."""
    return URL.sub(_redact_url, text)


def _reason(result):
    """Последняя содержательная строка отказа pip/ensurepip, без секретов (#811).

    Раньше отказ доносил только `command exited N`, и настоящая причина
    (например, отсутствующий build backend) умирала в захваченном stderr.
    pip печатает причину вложенно (`ERROR: Could not find ...`) и завершает
    общим `error: subprocess-exited-with-error`: общая строка не говорит
    оператору ничего, поэтому предпочитаются настоящие `ERROR:`.
    """
    raw = getattr(result, 'stderr', None) or b''
    text = raw if isinstance(raw, str) else raw.decode('utf-8', 'replace')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Строка pip может быть декорирована рамкой (`│`, `╰─>`); сравнение идёт
    # по тексту без декорации, чтобы `note:` не перебивал `ERROR:`.
    def core(line):
        return line.lstrip('│|╰╭─>| \t')
    generic = ('subprocess-exited-with-error', 'see above for output',
               'did not run successfully', 'exit code:')
    meaningful = [line for line in lines
                  if core(line).lower().startswith('error') and
                  not any(skip in line.lower() for skip in generic)]
    chosen = core((meaningful or lines or [''])[-1])
    # Учётные данные индекса не должны попасть в отчёт оператору: userinfo,
    # query-параметр и сегмент пути (#811).
    chosen = _redacted(chosen)
    return ''.join(char for char in chosen if char.isprintable())[:400]


def _run(argv, execute, timeout, step, **options):
    """Один внешний шаг; `options` -- cwd/pass_fds привязки к fd (#811)."""
    try:
        result = execute(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, timeout=timeout, check=False, **options)
    except (OSError, subprocess.SubprocessError):
        raise _failed(step, 'command unavailable') from None
    if result.returncode:
        reason = _reason(result)
        raise _failed(step, 'command exited ' + str(result.returncode) +
                      (': ' + reason if reason else ''))
    return result


def system_python(requirement, *, execute=subprocess.run, timeout=60):
    """Версия установленного python; ниже объявленного требования -- отказ.

    Проверка идёт до первого изменения системы: даунгрейд python ради
    установки -- не наш путь, а apt на Debian способен притянуть версию.
    """
    minimum = requirement_minimum(requirement)
    try:
        result = execute([interpreter(), '-I', '-c', VERSION_PROBE], stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                         check=False)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('system python3 is required for the declared runtime') from None
    raw = result.stdout if getattr(result, 'stdout', None) else b''
    try:
        version = tuple(int(part) for part in raw.decode('ascii').strip().split('.')[:2])
    except (ValueError, UnicodeError):
        version = ()
    found = '.'.join(str(part) for part in version) if len(version) == 2 else 'unusable'
    if getattr(result, 'returncode', 0) != 0 or len(version) != 2 or version < minimum:
        raise RuntimeError('system python ' + requirement[2:] + ' or newer is required; found ' + found)
    return version


def installed_source(source=None):
    """Дерево исходников mop: без pyproject.toml ставить `[server]` нечего."""
    source = Path(source) if source is not None else Path(__file__).resolve().parents[2]
    if not (source / 'pyproject.toml').is_file():
        raise RuntimeError('dependency setup requires a mop source tree')
    return source


def _owned(info):
    """Объект принадлежит установке и писать в него чужой не может (#811).

    Владелец -- root или текущий euid, и ни группа, ни остальные не пишут.
    Группа не должна быть чужой: root-owned файл с mode 0775 и группой
    атакующего -- это файл, который атакующий перепишет, и проверка,
    смотревшая только `st_uid`, исполняла его от root.
    """
    if info.st_uid not in (0, os.geteuid()):
        return False
    if info.st_mode & 0o022:
        return False
    return info.st_gid in _trusted_groups()


def _trusted_groups():
    """Группы, которые установка считает своими: root и группы процесса.

    Основная и дополнительные группы вызывающего -- те, что дали ему его
    собственные креды; произвольный gid чужой группы (например, группы
    атакующего uid) в это множество не входит.
    """
    groups = {0, os.getegid()}
    try:
        groups.update(os.getgroups())
    except OSError:
        pass
    return groups


def _open_dir(name, label, *, dir_fd=None, owned=True):
    """Каталог по имени без следования symlink; fd закрывает вызывающий.

    `owned=False` -- обход внутри дерева установки: каталог мог быть создан
    pip или umask-ом установщика, но подменять его symlink'ом всё равно
    нельзя, а вызывающий проверяет владельца там, где это важно.
    """
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        raise RuntimeError('existing venv is not installation-owned: ' + label) from None
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or (owned and not _owned(info)):
        os.close(descriptor)
        raise RuntimeError('existing venv is not installation-owned: ' + label)
    return descriptor


def _verified_regular(directory, name, label, *, system_target=False):
    """Файл принадлежит установке; symlink -- только на установленный python.

    `venv` штатно делает `bin/python` symlink'ом на базовый интерпретатор;
    любой другой symlink (в том числе на чужой файл) -- не venv установки
    (#811). Разрешение symlink ничего не исполняет: сверяется realpath.

    Проверяются dev/ino и владелец: файл под чужим именем между lstat и
    чтением уже не тот, что проверен (#811).
    """
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        raise RuntimeError('existing venv is not installation-owned: ' + label) from None
    if stat.S_ISLNK(info.st_mode):
        if not system_target or not _system_target(directory, name):
            raise RuntimeError('existing venv is not installation-owned: ' + label)
        return
    if not stat.S_ISREG(info.st_mode) or not _owned(info):
        raise RuntimeError('existing venv is not installation-owned: ' + label)

def _system_target(directory, name):
    """Symlink ведёт на уже проверенный системный интерпретатор (#811)."""
    try:
        return os.path.realpath('/proc/self/fd/%d/%s' % (directory, name)) == interpreter()
    except OSError:
        return False


def _verify_venv(descriptor, venv):
    """Повтор шага допустим только для venv, созданного установкой (#811).

    `descriptor` -- удержанный O_DIRECTORY корня venv; проверяется ровно то
    дерево, что и дальше будет исполняться, и файлы ищутся относительно
    него, а не по пути.
    """
    label = str(venv)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode) or not _owned(os.fstat(descriptor)):
        raise RuntimeError('existing venv is not installation-owned: ' + label)
    _verified_regular(descriptor, 'pyvenv.cfg', label)
    bin_descriptor = _open_dir('bin', label, dir_fd=descriptor)
    try:
        _verified_regular(bin_descriptor, 'python', label, system_target=True)
    finally:
        os.close(bin_descriptor)


def _normalize_modes(descriptor, *, scripts=True):
    """Каталоги 0755 и bin/* 0755: службы читают venv при любом umask установщика.

    `python -m venv` наследует umask: под 077 `pyvenv.cfg` и `bin` выходили
    0600/0700, и service-пользователь не мог ни прочесть конфиг, ни пройти в
    каталог (#811). Обход идёт по fd зарезервированного дерева: путь не
    переразрешается, а chmod не может уйти в подменённый каталог. Ошибка
    правки прав не глотается -- она уходит в причину шага (#811).

    `scripts` -- это корень свежесозданного дерева: только там правится
    `bin/*`, и только у свежесозданного дерева (см. create_venv), чтобы
    чужой исполняемый файл в переиспользованном venv не переписывался.
    """
    os.fchmod(descriptor, 0o755)
    for name in os.listdir(descriptor):
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            child = _open_dir(name, str(name), dir_fd=descriptor, owned=False)
            try:
                _normalize_modes(child, scripts=False)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            try:
                handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=descriptor)
            except OSError:
                continue
            try:
                os.fchmod(handle, 0o644)
            finally:
                os.close(handle)
    if scripts:
        # Дети корневого bin -- исполняемые скрипты (pip, pip3, activate...).
        bin_descriptor = _open_dir('bin', 'bin', dir_fd=descriptor, owned=False)
        try:
            for name in os.listdir(bin_descriptor):
                info = os.stat(name, dir_fd=bin_descriptor, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=bin_descriptor)
                    try:
                        os.fchmod(handle, 0o755)
                    finally:
                        os.close(handle)
        finally:
            os.close(bin_descriptor)


def _identity(info):
    """Сам каталог: пара (dev, ino), а не только номер inode (#811).

    Номер inode уникален лишь внутри файловой системы: два дерева на разных
    устройствах с одинаковым st_ino считались одним и тем же.
    """
    return info.st_dev, info.st_ino


def _same_tree(descriptor, venv):
    """Путь всё ещё называет дерево удержанного fd; пара (dev, ino)."""
    try:
        info = os.lstat(venv)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    return _identity(info) == _identity(os.fstat(descriptor))


def _check_reservation(descriptor, venv):
    """Резерв -- ещё тот каталог, и он пуст; иначе типизированный отказ."""
    if not _same_tree(descriptor, venv):
        raise RuntimeError('venv reservation no longer names the prepared tree: ' + str(venv))
    try:
        if os.listdir(descriptor):
            raise RuntimeError('reserved venv directory is not empty: ' + str(venv))
    except OSError:
        raise RuntimeError('venv reservation no longer names the prepared tree: ' + str(venv)) from None


def _pinned(descriptor):
    """Каталог для exec: cwd -- /proc/self/fd/N удержанного дерева (#811).

    Интерпретатор зовётся относительным `bin/python` из этого cwd; путь
    `str(venv / 'bin/python')` между проверкой и exec можно было подменить
    symlink'ом предка, и root исполнял чужой скрипт (#811). fd наследуется
    в ребёнка: `pass_fds` держит его открытым по тому же номеру.
    """
    return {'cwd': '/proc/self/fd/%d' % descriptor,
            'pass_fds': (descriptor,)}


def _python(descriptor):
    """Один проверенный интерпретатор на все шаги: options для _run.

    argv0 относительный, а executable -- fd интерпретатора: оба держат
    дерево, а не имя. Корневой fd остаётся открытым на всё время шагов,
    поэтому вложенные процессы pip видят `bin/python` так же (#811).
    Стандартный `bin/python` -- symlink на проверенный системный
    интерпретатор; иной symlink и неисполняемый файл отвергаются.
    Владение деревом уже доказано резервом или `_verify_venv`; здесь
    проверяется форма интерпретатора. Открытый fd закрывает вызывающий.
    """
    bin_descriptor = _open_dir('bin', 'bin', dir_fd=descriptor, owned=False)
    try:
        info = os.stat('python', dir_fd=bin_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            target = os.path.realpath('/proc/self/fd/%d/python' % bin_descriptor)
            if target != interpreter():
                raise RuntimeError('existing venv is not installation-owned: bin/python')
            handle = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        elif stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
            handle = os.open('python', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=bin_descriptor)
        else:
            raise RuntimeError('existing venv is not installation-owned: bin/python')
    finally:
        os.close(bin_descriptor)
    # Оба fd наследуются: cwd ребёнка и его executable -- это /proc/self/fd
    # ребёнка, поэтому числа обязаны быть открыты и в нём (#811).
    return handle, {'executable': '/proc/self/fd/%d' % handle,
                    'cwd': '/proc/self/fd/%d' % descriptor,
                    'pass_fds': (descriptor, handle)}


def _step(argv, execute, timeout, step, *, owned, venv, **options):
    """Шаг venv python; после него путь обязан всё ещё называть то же дерево.

    Проверка после (а не только до) шага ловит swap, случившийся во время
    pip: исполняемое уже привязано к fd, но подменённый каталог по этому
    пути не должен получить ни одного следующего шага (#811).
    """
    result = _run(argv, execute, timeout, step, **options)
    if not _same_tree(owned, venv):
        raise RuntimeError('venv reservation no longer names the prepared tree: ' + str(venv))
    return result


def create_venv(venv, *, pip_extra='server', wheels=None, source=None,
                execute=subprocess.run, timeout=VENV_TIMEOUT, reserved=None):
    """Создать (или переиспользовать) venv и поставить в него объявленный extra.

    Повторный шаг идемпотентен: принадлежащий установке venv не пересоздаётся,
    pip сходится к уже установленным версиям. `reserved` -- descriptor
    свежезарезервированного каталога (install_provision._reserve): venv
    создаётся только в нём, по проверенной паре (dev, ino), а не по пути,
    который между preflight и этим шагом мог подменить symlink-предок (#811).

    Каждый шаг venv python идёт через fd удержанного дерева, а не строкой
    пути; подмена каталога между проверкой и первым pip детектируется
    (RuntimeError), и подменённый интерпретатор не исполняется (#811).
    """
    venv = Path(venv)
    source = installed_source(source)
    offline = []
    if wheels is not None:
        wheels = Path(wheels)
        if not wheels.is_dir():
            raise RuntimeError('offline wheel directory does not exist: ' + str(wheels))
        offline = ['--no-index', '--find-links', str(wheels)]
    owned = reserved
    created = reserved is not None
    try:
        if reserved is not None:
            _check_reservation(reserved, venv)
            created = True
        else:
            try:
                owned = os.open(str(venv), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except FileNotFoundError:
                # Ручной `controller setup` идёт тем же dirfd-путём, что установка:
                # mkdir по пути следовал бы подменённому symlink-предку (#811).
                from .install_provision import _reserve
                try:
                    owned = _reserve(str(venv))
                except (OSError, RuntimeError):
                    raise RuntimeError('venv path is not creatable: ' + str(venv)) from None
                created = True
            except OSError:
                raise RuntimeError('venv path is not creatable: ' + str(venv)) from None
            else:
                held = os.fstat(owned)
                if not stat.S_ISDIR(held.st_mode):
                    raise RuntimeError('venv path is not creatable: ' + str(venv))
                has_bin = _has(owned, 'bin') and _has(owned, 'bin/python')
                if has_bin:
                    # Проверка владельца -- внутри _verify_venv: непустое дерево
                    # отвергается как чужое, а не как «не создать» (#811).
                    _verify_venv(owned, venv)
                    created = False
                elif os.listdir(owned):
                    raise RuntimeError('existing venv is not installation-owned: ' + str(venv))
                elif _owned(held):
                    created = True
                else:
                    raise RuntimeError('venv path is not creatable: ' + str(venv))
        total = sum(VENV_SHARES.values())
        def budget(step):
            return max(1, timeout * VENV_SHARES[step] // total)
        if created:
            # --system-site-packages нет намеренно: библиотеки Debian смешались бы
            # с точной декларацией, а Django 4.2 не годится контроллеру.
            # `python -m venv .` с cwd на fd: venv пишет в удержанное дерево,
            # а не в путь под ним (#811).
            _run([interpreter(), '-m', 'venv', '.'], execute,
                 budget('venv creation'), 'venv creation', **_pinned(owned))
            if not _same_tree(owned, venv):
                raise RuntimeError('venv creation escaped the reserved tree: ' + str(venv))
            if not _has(owned, 'bin') or not _has(owned, 'bin/python'):
                raise RuntimeError('venv creation left no interpreter: ' + str(venv))
        else:
            _verify_venv(owned, venv)
        # Подмена между проверкой и первым exec: путь обязан всё ещё
        # называть удержанное дерево, иначе типизированный отказ без
        # исполнения чего-либо (#811).
        if not _same_tree(owned, venv):
            raise RuntimeError('venv reservation no longer names the prepared tree: ' + str(venv))
        handle, options = _python(owned)
        try:
            _step(['bin/python', '-m', 'pip', 'install', *offline, '--upgrade', 'pip'],
                  execute, budget('pip upgrade'), 'pip upgrade',
                  owned=owned, venv=venv, **options)
            _step(['bin/python', '-m', 'pip', 'install', *offline,
                   str(source) + '[' + pip_extra + ']'],
                  execute, budget('extra'), pip_extra + ' extra',
                  owned=owned, venv=venv, **options)
            # Права нормализуются ПОСЛЕ pip: он тоже наследует umask установщика,
            # и тогда site-packages под 077 был бы нечитаем службам (#811).
            # Только у свежесозданного дерева: чужие исполняемые файлы в
            # переиспользованном venv установка не переписывает (#811).
            if created:
                _normalize_modes(owned)
        finally:
            os.close(handle)
        # Import check -- последним: он и есть приёмка venv; чужой
        # `bin/python`, подменённый во время шагов, снова не исполняется.
        handle, options = _python(owned)
        try:
            _step(['bin/python', '-I', '-c', 'import ' + ', '.join(IMPORTS)],
                  execute, budget('import verification'), 'import verification',
                  owned=owned, venv=venv, **options)
        finally:
            os.close(handle)
    except (RuntimeError, OSError, PermissionError) as error:
        reason = str(error) if isinstance(error, RuntimeError) else (
            'venv step failed: ' + type(error).__name__ + ': ' + str(error))
        if created:
            removed = _remove_incomplete(venv, owned)
            raise RuntimeError(reason + ('; removed incomplete venv' if removed
                                         else '; incomplete venv retained at ' + str(venv))) from None
        raise RuntimeError(reason + '; existing venv retained') from None
    finally:
        if reserved is None and owned is not None:
            os.close(owned)
    return str(venv)


def _has(descriptor, name):
    try:
        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        return True
    except OSError:
        return False


def _purge(descriptor):
    """Очистить дерево через удержанный fd; имена не переразрешаются.

    Каждый потомок открывается от fd родителя без следования symlink, а
    удаляется через `dir_fd` этого же родителя: подмена имени между
    открытием и удалением не уводит на чужой файл (#811).
    """
    for name in os.listdir(descriptor):
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            try:
                _purge(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _remove_incomplete(venv, reserved):
    """Свежесозданный venv не оставляется молча: причина уже в ошибке (#811).

    Удаляется только зарезервированное дерево. Без удержанного descriptor
    удалять нечего доказывать -- rmtree по пути снёс бы то, что сейчас на
    этом месте (подменённый каталог), поэтому `reserved` обязателен, а
    путь сверяется с ним парой (dev, ino). Содержимое удаляется через сам
    fd, а пустой каталог снимается по имени родителя с повторной сверкой:
    подмена после проверки не уводит удаление на чужое дерево.
    """
    if reserved is None:
        return False
    try:
        if not _same_tree(reserved, venv):
            return False
        _purge(reserved)
        parent = os.open('/proc/self/fd/%d/..' % reserved,
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        leaf = os.path.basename(os.path.realpath('/proc/self/fd/%d' % reserved))
        current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if _identity(current) != _identity(os.fstat(reserved)):
            return False
        os.rmdir(leaf, dir_fd=parent)
        return True
    except OSError:
        return False
    finally:
        os.close(parent)
