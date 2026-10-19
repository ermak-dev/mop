"""Runtime установки: системный python и приватный venv контроллера (#807).

Модуль только данных и subprocess, без импорта контроллера: его зовёт
привилегированный installer до того, как в системе появится хоть одна
библиотека Django. apt здесь не ставится -- системные пакеты ставит
`install_provision` одним вызовом вместе с postgresql-client; здесь --
проверка системного python, venv, объявленный pip extra и проверка импортов.

Интерпретатор -- системный `python3`, каким его даёт образ (на Debian 13 это
3.13.5): установка не даунгрейдит python и не берёт пакеты Debian как замену
точной декларации.
"""
from pathlib import Path
import re
import subprocess

# Ниже этого установка не идёт: pyproject.toml объявляет requires-python >=3.12.
MINIMUM = (3, 12)
# Ровно то, что нужно контроллеру, чтобы стартовать (issue #807).
IMPORTS = ('django', 'rest_framework', 'channels', 'psycopg', 'uvicorn', 'ldap', 'django_auth_ldap')
REQUIREMENT = re.compile(r'^>=(\d+)\.(\d+)$')
VERSION_PROBE = 'import sys; sys.stdout.write("%d.%d.%d\\n" % sys.version_info[:3])'


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


def _run(argv, execute, timeout, step):
    try:
        result = execute(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise _failed(step, 'command unavailable') from None
    if result.returncode:
        raise _failed(step, 'command exited ' + str(result.returncode))
    return result


def system_python(requirement, *, execute=subprocess.run, timeout=60):
    """Версия системного python3; ниже объявленного требования -- отказ.

    Проверка идёт до первого изменения системы: даунгрейд python ради
    установки -- не наш путь, а apt на Debian способен притянуть версию.
    """
    minimum = requirement_minimum(requirement)
    try:
        result = execute(['python3', '-I', '-c', VERSION_PROBE], stdin=subprocess.DEVNULL,
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


def create_venv(venv, *, pip_extra='server', wheels=None, source=None,
                execute=subprocess.run, timeout=900):
    """Создать (или переиспользовать) venv и поставить в него объявленный extra.

    Повторный шаг идемпотентен: существующий venv не пересоздаётся, pip
    сходится к уже установленным версиям. Проверка импортов -- часть шага:
    без Django/DRF/psycopg юнит не стартует, и узнать об этом лучше здесь.
    """
    venv = Path(venv)
    source = installed_source(source)
    offline = []
    if wheels is not None:
        wheels = Path(wheels)
        if not wheels.is_dir():
            raise RuntimeError('offline wheel directory does not exist: ' + str(wheels))
        offline = ['--no-index', '--find-links', str(wheels)]
    python = str(venv / 'bin/python')
    if not Path(python).exists():
        try:
            venv.mkdir(parents=True)
        except OSError:
            raise RuntimeError('venv path is not creatable: ' + str(venv)) from None
        # --system-site-packages нет намеренно: библиотеки Debian смешались бы
        # с точной декларацией, а Django 4.2 не годится контроллеру.
        _run(['python3', '-m', 'venv', str(venv)], execute, timeout, 'venv creation')
        # Контроллер читает и исполняет venv из-под service-пользователей,
        # пишет в него только root установки.
        venv.chmod(0o755)
    _run([python, '-m', 'pip', 'install', *offline, '--upgrade', 'pip'],
         execute, timeout, 'pip upgrade')
    _run([python, '-m', 'pip', 'install', *offline, str(source) + '[' + pip_extra + ']'],
         execute, timeout, pip_extra + ' extra')
    _run([python, '-I', '-c', 'import ' + ', '.join(IMPORTS)],
         execute, timeout, 'import verification')
    return str(venv)
