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
"""
from pathlib import Path
import os
import re
import shutil
import stat
import subprocess
import sys

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
    # Учётные данные URL индекса не должны попасть в отчёт оператору.
    chosen = re.sub(r'([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@', r'\1<redacted>@', chosen)
    return ''.join(char for char in chosen if char.isprintable())[:400]


def _run(argv, execute, timeout, step):
    try:
        result = execute(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, timeout=timeout, check=False)
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
    """Владелец -- установка; чужой может писать только в свою группу."""
    return info.st_uid in (0, os.geteuid()) and not info.st_mode & 0o002


def _verified_regular(path, label, *, system_target=False):
    """Файл принадлежит установке; symlink -- только на установленный python.

    `venv` штатно делает `bin/python` symlink'ом на базовый интерпретатор;
    любой другой symlink (в том числе на чужой файл) -- не venv установки
    (#811). Разрешение symlink ничего не исполняет: сверяется realpath.
    """
    try:
        info = os.lstat(path)
    except OSError:
        raise RuntimeError('existing venv is not installation-owned: ' + label) from None
    if stat.S_ISLNK(info.st_mode):
        if not system_target or os.path.realpath(path) != interpreter():
            raise RuntimeError('existing venv is not installation-owned: ' + label)
        return
    if not stat.S_ISREG(info.st_mode) or not _owned(info):
        raise RuntimeError('existing venv is not installation-owned: ' + label)


def _verify_venv(venv):
    """Повтор шага допустим только для venv, созданного установкой (#811)."""
    root = os.lstat(venv)
    if stat.S_ISLNK(root.st_mode) or not stat.S_ISDIR(root.st_mode) or not _owned(root):
        raise RuntimeError('existing venv is not installation-owned: ' + str(venv))
    _verified_regular(Path(venv) / 'pyvenv.cfg', str(venv))
    _verified_regular(Path(venv) / 'bin' / 'python', str(venv), system_target=True)


def _normalize_modes(venv):
    """Каталоги 0755 и bin/* 0755: службы читают venv при любом umask установщика.

    `python -m venv` наследует umask: под 077 `pyvenv.cfg` и `bin` выходили
    0600/0700, и service-пользователь не мог ни прочесть конфиг, ни пройти в
    каталог (#811).
    """
    root = Path(venv)
    for current, directories, files in os.walk(root, followlinks=False):
        current = Path(current)
        for name in directories:
            path = current / name
            if not path.is_symlink():
                os.chmod(path, 0o755)
        for name in files:
            path = current / name
            if path.is_symlink():
                continue
            os.chmod(path, 0o755 if current.name == 'bin' else 0o644)
    os.chmod(root, 0o755)


def create_venv(venv, *, pip_extra='server', wheels=None, source=None,
                execute=subprocess.run, timeout=VENV_TIMEOUT, reserved=None):
    """Создать (или переиспользовать) venv и поставить в него объявленный extra.

    Повторный шаг идемпотентен: принадлежащий установке venv не пересоздаётся,
    pip сходится к уже установленным версиям. `reserved` -- descriptor
    свежезарезервированного каталога (install_provision._reserve): venv
    создаётся только в нём, по проверенному inode, а не по пути, который
    между preflight и этим шагом мог подменить symlink-предка (#811).
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
    if reserved is not None:
        held = os.fstat(reserved)
        info = os.lstat(venv)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_ino != held.st_ino:
            raise RuntimeError('venv reservation no longer names the prepared tree: ' + str(venv))
        if os.listdir(venv):
            raise RuntimeError('reserved venv directory is not empty: ' + str(venv))
        created = True
    elif (venv / 'bin' / 'python').exists():
        _verify_venv(venv)
        created = False
    else:
        if venv.exists():
            info = os.lstat(venv)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or not _owned(info):
                raise RuntimeError('venv path is not creatable: ' + str(venv))
            if os.listdir(venv):
                raise RuntimeError('existing venv is not installation-owned: ' + str(venv))
        else:
            # Ручной `controller setup` идёт тем же dirfd-путём, что установка:
            # mkdir по пути следовал бы подменённому symlink-предку (#811).
            from .install_provision import _reserve
            try:
                owned = _reserve(str(venv))
            except (OSError, RuntimeError):
                raise RuntimeError('venv path is not creatable: ' + str(venv)) from None
        created = True
    python = str(venv / 'bin/python')
    total = sum(VENV_SHARES.values())
    def budget(step):
        return max(1, timeout * VENV_SHARES[step] // total)
    try:
        if created:
            # --system-site-packages нет намеренно: библиотеки Debian смешались бы
            # с точной декларацией, а Django 4.2 не годится контроллеру.
            _run([interpreter(), '-m', 'venv', str(venv)], execute,
                 budget('venv creation'), 'venv creation')
            if owned is not None and os.lstat(venv).st_ino != os.fstat(owned).st_ino:
                raise RuntimeError('venv creation escaped the reserved tree: ' + str(venv))
        _run([python, '-m', 'pip', 'install', *offline, '--upgrade', 'pip'],
             execute, budget('pip upgrade'), 'pip upgrade')
        _run([python, '-m', 'pip', 'install', *offline, str(source) + '[' + pip_extra + ']'],
             execute, budget('extra'), pip_extra + ' extra')
        # Права нормализуются ПОСЛЕ pip: он тоже наследует umask установщика, и
        # тогда site-packages под 077 был бы нечитаем службам (#811).
        _normalize_modes(venv)
        _run([python, '-I', '-c', 'import ' + ', '.join(IMPORTS)],
             execute, budget('import verification'), 'import verification')
    except RuntimeError as error:
        if created:
            removed = _remove_incomplete(venv, owned)
            raise RuntimeError(str(error) + ('; removed incomplete venv' if removed
                                             else '; incomplete venv retained at ' + str(venv))) from None
        raise RuntimeError(str(error) + '; existing venv retained') from None
    finally:
        if reserved is None and owned is not None:
            os.close(owned)
    return str(venv)


def _remove_incomplete(venv, reserved=None):
    """Свежесозданный venv не оставляется молча: причина уже в ошибке (#811).

    Возвращает False, если удалять нельзя: путь больше не тот каталог, который
    резервировали (его подменили), -- чужие файлы установка не трогает.
    """
    try:
        info = os.lstat(venv)
        if reserved is not None and info.st_ino != os.fstat(reserved).st_ino:
            return False
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(venv)
            return True
    except OSError:
        pass
    return False
