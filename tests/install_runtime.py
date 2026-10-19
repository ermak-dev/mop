"""Install runtime contract: declared system packages and a private venv (#807).

Спецификация установки объявляет runtime: требование к python, путь venv,
системные apt-пакеты и pip extra. Проверки здесь -- чистые: ОС-команды идут
в подставной executor, настоящие apt/venv не запускаются. Живой прогон на
Debian 13 -- отдельно, не этот файл.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _fresh_install_fixture import ROOT


RUNTIME = {
    'python': '>=3.12',
    'venv': '/opt/mop-fresh/venv',
    'system_packages': ['build-essential', 'curl', 'git', 'libldap2-dev', 'libpq-dev',
                        'libsasl2-dev', 'python3-dev', 'python3-venv', 'rsync'],
    'pip_extra': 'server',
}
# #811: интерпретатор берётся абсолютным путём, не повторным поиском в PATH.
PYTHON = os.path.realpath(sys.executable)


def installation(**overrides):
    data = {
        'installation_id': 'fresh-runtime',
        'database': {'topology': 'remote', 'major': 17, 'host': 'db.example.invalid',
                     'port': 5432, 'name': 'fresh_db', 'tls_ca': '/etc/mop-db/ca.pem'},
        'paths': {'configuration': '/etc/mop-fresh', 'state': '/var/lib/mop-fresh',
                  'backup': '/var/backups/mop-fresh', 'root_trust': '/var/lib/mop-trust'},
        'identities': {'maintenance': 'mop_maintenance', 'web': 'mop_web',
                       'worker': 'mop_worker', 'gateway': 'mop_gateway',
                       'credentials': 'mop_credentials', 'builder': 'mop_builder'},
        'runtime': dict(RUNTIME),
    }
    data.update(overrides)
    return data


class Executor:
    """Подставной OS executor: версия python, venv-skeleton и успешные команды.

    #811: venv python больше не зовётся абсолютным путём -- argv[0] это
    `bin/python`, а каталог приходит cwd-привязкой к fd резерва. Скелет
    создаётся по этому cwd, поэтому фикстура видит тот же каталог, что и
    настоящий venv. `run_real` исполняет только абсолютный `.../bin/python`:
    ровно то, чем старая ветка запускала подменённый интерпретатор.
    Права явные: под umask 002 свежие каталоги вышли бы group-writable и не
    прошли бы проверку владельца, хотя дерево настоящее (#811).
    """
    def __init__(self, version=b'3.13.5\n', run_real=False):
        self.calls = []
        self.options = []
        self.version = version
        self.run_real = run_real

    @staticmethod
    def _mode(path, mode):
        os.chmod(path, mode)

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.options.append(dict(kwargs))
        # Каталог шага: cwd-привязка к fd (#811) или абсолютный путь из
        # старой ветки -- фикстура обязана видеть оба.
        target = Path(kwargs['cwd']) if kwargs.get('cwd') else Path(argv[3] if len(argv) > 3
                                                                   and argv[1:3] == ['-m', 'venv']
                                                                   else os.path.dirname(os.path.dirname(argv[0])))
        stdout = b''
        if self.run_real and os.path.isabs(argv[0]) and argv[0].endswith('/bin/python') \
                and os.path.exists(argv[0]):
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True)
            return subprocess.CompletedProcess(argv, result.returncode, b'', result.stderr)
        if len(argv) >= 3 and os.path.isabs(argv[0]) \
                and argv[1] == '-I' and 'sys.version_info' in argv[-1]:
            stdout = self.version
        elif len(argv) == 4 and argv[1] == '-m' and argv[2] == 'venv':
            # #811: umask установщика 077 виден на реальных правах; фикстура
            # повторяет его, чтобы нормализацию прав нельзя было не заметить.
            # bin/python -- symlink на проверенный интерпретатор: ровно то,
            # что делает `python -m venv` без `--copies` (#811).
            venv = target
            (venv / 'bin').mkdir(parents=True, mode=0o700)
            from mop.server.install_runtime import interpreter
            (venv / 'bin' / 'python').symlink_to(interpreter())
            (venv / 'bin' / 'pip').write_text('')
            (venv / 'bin' / 'pip').chmod(0o700)
            (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
            (venv / 'pyvenv.cfg').chmod(0o600)
        elif 'pip' in argv and any(str(item).endswith('[server]') for item in argv):
            # pip тоже наследует umask: установленные файлы выходят 0600/0700.
            site = target / 'lib' / 'python3.13' / 'site-packages' / 'pkg'
            site.mkdir(parents=True, mode=0o700)
            (site / '__init__.py').write_text('')
            (site / '__init__.py').chmod(0o600)
            (site.parent / 'pkg-1.0.dist-info').mkdir(mode=0o700)
        return subprocess.CompletedProcess(argv, 0, stdout, b'')

    def commands(self, *prefix):
        return [argv for argv in self.calls if argv[:len(prefix)] == list(prefix)]

    def pip_steps(self):
        """Шаги venv python: (argv, options), а не абсолютный путь (#811)."""
        return [(argv, options) for argv, options in zip(self.calls, self.options)
                if argv[:1] == ['bin/python']]


class OsProxy:
    """os только для одного модуля: подменить lstat/chmod, не трогая harness."""
    def __init__(self, **overrides):
        self.overrides = overrides

    def __getattr__(self, name):
        return self.overrides.get(name, getattr(os, name))


def stat_like(template, *, dev=None, ino=None):
    """stat_result с той же формой, но заданными dev/ino."""
    fields = list(template)
    if dev is not None:
        fields[2] = dev
    if ino is not None:
        fields[1] = ino
    return os.stat_result(fields)


# HYPOTHESIS: spec не объявляет runtime, поэтому установка не знает, какие
# системные пакеты ставить и каким интерпретатором запускать контроллер.
# SOLUTION: явный runtime-блок с валидацией (python/venv/system_packages/pip_extra).
# RESULT: RED -- specification отвергает unknown installation keys.
# STATUS: FIXED — see #807.
class RuntimeSpecification(unittest.TestCase):
    def spec(self, **overrides):
        from mop.server.installation import specification
        return specification(installation(**overrides))

    def test_runtime_block_is_required_with_declared_python_and_venv(self):
        from mop.server.installation import specification
        with self.assertRaisesRegex(ValueError, 'runtime'):
            specification(installation(runtime=None))
        spec = self.spec()
        runtime = dict(spec.runtime)
        self.assertEqual(runtime['python'], '>=3.12')
        self.assertEqual(runtime['venv'], '/opt/mop-fresh/venv')
        self.assertEqual(runtime['pip_extra'], 'server')
        self.assertIn('build-essential', runtime['system_packages'])
        self.assertIn('python3-venv', runtime['system_packages'])

    def test_system_packages_are_safe_apt_names_and_cannot_pin_a_downgrade(self):
        for packages in (['python3=3.12'], ['python3.13=3.13.4'], ['Bad Name'], [], 'python3'):
            with self.subTest(packages=packages):
                with self.assertRaisesRegex(ValueError, 'system package'):
                    self.spec(runtime={**RUNTIME, 'python': '>=3.12', 'system_packages': packages})
        duplicate = self.spec(runtime={**RUNTIME, 'system_packages': ['git', 'git']})
        self.assertEqual(dict(duplicate.runtime)['system_packages'], ['git'])

    def test_python_requirement_must_accept_the_declared_minimum(self):
        for requirement in ('3.12', '>=3.11', '>3.12', '>=3', '>=x.y', 312):
            with self.subTest(requirement=requirement):
                with self.assertRaisesRegex(ValueError, 'python'):
                    self.spec(runtime={**RUNTIME, 'python': requirement})
        self.spec(runtime={**RUNTIME, 'python': '>=3.13'})

    def test_venv_must_be_a_private_path_outside_every_other_tree(self):
        for venv in ('relative/venv', '/opt/../venv', '/etc/mop-fresh/venv', '/',
                     '/etc/mop-fresh'):
            with self.subTest(venv=venv):
                with self.assertRaises(ValueError):
                    self.spec(runtime={**RUNTIME, 'venv': venv})
        self.spec(runtime={**RUNTIME, 'venv': '/var/lib/mop-venv'})

    def test_plan_and_provisioning_artifact_expose_the_declared_runtime(self):
        from mop.server.installation import plan, provisioning_artifact
        spec = self.spec()
        planned = plan(spec)
        self.assertEqual(planned['runtime']['venv'], '/opt/mop-fresh/venv')
        self.assertEqual(planned['runtime']['pip_extra'], 'server')
        self.assertIn('python3-venv', planned['runtime']['system_packages'])
        # #811: шаг setup -- это venv-цепочка установки, а не голый
        # `python3 -m venv`, после которого контроллер всё ещё без Django.
        self.assertNotIn('python3 -m venv', planned['setup'])
        self.assertIn('/opt/mop-fresh/venv', planned['setup'])
        artifact = provisioning_artifact(spec)
        self.assertEqual(artifact['runtime']['python'], '>=3.12')
        apt = [task for task in artifact['playbook'][0]['tasks']
               if 'ansible.builtin.apt' in task and task['ansible.builtin.apt'].get('name') != 'postgresql-client-17']
        self.assertTrue(apt, 'review playbook does not install declared system packages')
        self.assertEqual(apt[0]['ansible.builtin.apt']['name'], dict(spec.runtime)['system_packages'])
        self.assertFalse(apt[0]['ansible.builtin.apt']['install_recommends'])


# HYPOTHESIS: provisioning ставит только postgresql-client и не создаёт venv;
# bin/mop затем не находит Django/DRF/psycopg на чистой Debian 13.
# SOLUTION: те же guards, затем apt из runtime, проверка python, venv и pip extra.
# RESULT: RED -- нет install_runtime и runtime в document.
# STATUS: FIXED — see #807.
class ProvisioningRuntime(unittest.TestCase):
    def setUp(self):
        from mop.server import install_provision
        from mop.server.installation import specification
        self.module = install_provision
        self.tmp = tempfile.TemporaryDirectory(prefix='mfi-runtime-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        data = installation()
        data['paths'] = {key: str(base / key) for key in data['paths']}
        data['runtime'] = {**RUNTIME, 'venv': str(base / 'venv')}
        self.spec = specification(data)
        self.data = install_provision.document(self.spec)

    def prepare(self, executor, **kwargs):
        with patch.object(self.module.os, 'geteuid', return_value=0), \
             patch.object(self.module.pwd, 'getpwnam', side_effect=KeyError), \
             patch.object(self.module.grp, 'getgrnam', side_effect=KeyError):
            return self.module.prepare(self.data, execute=executor, **kwargs)

    def test_system_packages_are_installed_without_recommends_or_downgrade(self):
        executor = Executor()
        self.prepare(executor)
        apt = executor.commands('apt-get', 'install')
        self.assertEqual(len(apt), 1)
        argv = apt[0]
        self.assertEqual(argv[:4], ['apt-get', 'install', '-y', '--no-install-recommends'])
        self.assertIn('postgresql-client-17', argv)
        for package in RUNTIME['system_packages']:
            self.assertIn(package, argv)
        self.assertNotIn('--allow-downgrades', argv)
        self.assertNotIn('--allow-change-held-packages', argv)

    def test_system_python_below_the_declared_requirement_is_refused_before_changes(self):
        executor = Executor(version=b'3.11.9\n')
        with self.assertRaisesRegex(RuntimeError, r'3\.12'):
            self.prepare(executor)
        self.assertEqual(executor.commands('apt-get'), [])
        self.assertEqual(executor.commands('python3', '-m', 'venv'), [])

    def test_venv_step_creates_the_interpreter_and_verifies_the_server_imports(self):
        executor = Executor()
        self.prepare(executor)
        venv = str(Path(self.tmp.name) / 'venv')
        created = executor.commands(PYTHON, '-m', 'venv')
        self.assertEqual(created, [[PYTHON, '-m', 'venv', '.']])
        pip = [argv for argv, _ in executor.pip_steps() if argv[1:4] == ['-m', 'pip', 'install']]
        self.assertTrue(pip, 'declared server extra was not installed into the venv')
        self.assertIn('--upgrade', pip[0])
        self.assertIn('pip', pip[0])
        self.assertTrue(any(argv[-1].endswith('[server]') for argv in pip),
                        'the declared server extra is missing from the install command')
        verified = [argv for argv, _ in executor.pip_steps()
                    if argv[1:3] == ['-I', '-c'] and 'import' in argv[-1]]
        self.assertTrue(verified, 'imports are not verified in the created venv')
        for name in ('django', 'rest_framework', 'channels', 'psycopg', 'uvicorn',
                     'ldap', 'django_auth_ldap'):
            self.assertIn(name, verified[0][-1])
        self.assertTrue(Path(venv).is_dir())
        self.assertEqual(Path(venv).stat().st_mode & 0o022, 0,
                         'venv must not be writable by group or other')
        # #811: каталоги 0755 и bin/* 0755 -- службы читают venv при любом umask.
        # Права нормализуются после pip: его файлы тоже наследуют umask.
        for directory in (Path(venv), Path(venv) / 'bin',
                          Path(venv) / 'lib' / 'python3.13' / 'site-packages' / 'pkg',
                          Path(venv) / 'lib' / 'python3.13' / 'site-packages' / 'pkg-1.0.dist-info'):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755, str(directory))
        self.assertEqual((Path(venv) / 'bin' / 'python').stat().st_mode & 0o777, 0o755)
        self.assertEqual((Path(venv) / 'pyvenv.cfg').stat().st_mode & 0o777, 0o644)
        self.assertEqual((Path(venv) / 'lib' / 'python3.13' / 'site-packages' / 'pkg' /
                          '__init__.py').stat().st_mode & 0o777, 0o644)

    def test_offline_install_uses_the_declared_wheel_directory(self):
        executor = Executor()
        wheels = Path(self.tmp.name) / 'wheels'
        wheels.mkdir()
        data = dict(self.data)
        data['runtime'] = {**self.data['runtime'], 'wheels': str(wheels)}
        from mop.server.installation import specification
        self.spec = specification(data)
        self.data = self.module.document(self.spec)
        self.prepare(executor)
        installed = [argv for argv in executor.calls if 'pip' in argv and 'install' in argv
                     and any('[server]' in item for item in argv)]
        self.assertEqual(len(installed), 1)
        self.assertIn('--no-index', installed[0])
        self.assertIn('--find-links', installed[0])
        self.assertIn(str(wheels), installed[0])

    def test_the_controller_path_never_uses_user_or_break_system_packages(self):
        sources = [ROOT / 'mop/server/install_runtime.py',
                   ROOT / 'mop/server/install_provision.py',
                   ROOT / 'mop/cli/server/controller.py',
                   ROOT / 'mop/server/installation.py']
        for path in sources:
            with self.subTest(path=str(path)):
                text = path.read_text()
                self.assertNotIn('--break-system-packages', text)
                self.assertNotIn('"pip", "install", "--user"', text)
                self.assertNotIn("'pip', 'install', '--user'", text)


class VenvReuse(unittest.TestCase):
    def made(self, venv):
        """Принадлежащий установке venv: каталог, pyvenv.cfg, bin/python.

        Права явные: под umask 002 свежие каталоги вышли бы group-writable и
        не прошли бы проверку владельца (#811), хотя venv настоящий.
        `bin/python` -- symlink на проверенный интерпретатор, как у настоящего
        `python -m venv` без `--copies` (#811).
        """
        (venv / 'bin').mkdir(parents=True)
        (venv / 'bin/python').symlink_to(PYTHON)
        (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
        for path in (venv, venv / 'bin'):
            os.chmod(path, 0o755)
        os.chmod(venv / 'pyvenv.cfg', 0o644)

    def test_existing_installation_owned_venv_is_reused_instead_of_recreated(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-venv-', dir='/tmp') as directory:
            venv = Path(directory) / 'venv'
            self.made(venv)
            executor = Executor()
            install_runtime.create_venv(venv, pip_extra='server',
                                        wheels=None, execute=executor, timeout=60)
            self.assertEqual(executor.commands(PYTHON, '-m', 'venv'), [],
                             'an existing venv must not be recreated')
            self.assertTrue([argv for argv, _ in executor.pip_steps()
                             if argv[:4] == ['bin/python', '-m', 'pip', 'install']])

    # HYPOTHESIS: preflight от `_absent` не резервирует venv, поэтому чужой
    # `venv/bin/python` исполняется от root (#811).
    # SOLUTION: reuse только для venv установки; непроверенный путь отвергается.
    # RESULT: RED -- старый create_venv зовёт planted-интерпретатор.
    # STATUS: FIXED — see #811.
    def test_existing_unverified_interpreter_is_never_executed(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-venv-', dir='/tmp') as directory:
            venv = Path(directory) / 'venv'
            (venv / 'bin').mkdir(parents=True)
            planted = venv / 'bin/python'
            planted.write_text('#!/bin/sh\ntouch planted\n')
            planted.chmod(0o755)
            executor = Executor()
            with self.assertRaisesRegex(RuntimeError, 'not installation-owned'):
                install_runtime.create_venv(venv, pip_extra='server',
                                            wheels=None, execute=executor, timeout=60)
            self.assertEqual(executor.calls, [], 'a planted interpreter must never run')

    # HYPOTHESIS: reuse path исполняет `venv/bin/python` по пути, и подменённый
    # между проверкой и первым pip каталог исполняется от root (#811).
    # SOLUTION: interpreter открывается через fd проверенного каталога, а не
    # разрешается заново по пути; каждый шаг идёт тем же fd/inode.
    # RESULT: RED -- swap между _verify_venv и pip даёт attacker shell от root.
    # STATUS: FIXED — see #811.
    def test_reuse_never_executes_an_interpreter_swapped_after_verification(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-swap-', dir='/tmp') as directory:
            base = Path(directory)
            venv = base / 'venv'
            self.made(venv)
            outside = base / 'outside'
            (outside / 'bin').mkdir(parents=True)
            marker = base / 'attacker-ran'
            (outside / 'bin' / 'python').write_text('#!/bin/sh\ntouch %s\nexit 0\n' % marker)
            (outside / 'bin' / 'python').chmod(0o755)
            (outside / 'pyvenv.cfg').write_text('home = /usr/bin\n')
            executor = Executor()
            swapped = []
            def execute(argv, **options):
                if not swapped:
                    swapped.append(True)
                    # ровно окно между проверкой и первым pip
                    os.rename(venv, base / 'venv.real')
                    os.rename(outside, venv)
                return executor(argv, **options)
            with self.assertRaisesRegex(RuntimeError, 'reservation|inode|prepared tree|escaped'):
                install_runtime.create_venv(venv, pip_extra='server', source=ROOT,
                                            execute=execute, timeout=60)
            self.assertFalse(marker.exists(), 'a swapped interpreter was executed')
            # Подмена видна в том же окне, что и первый шаг: дальше не идёт
            # ни одного шага, а сам первый исполнен через fd (реальное дерево).
            self.assertFalse([argv for argv in executor.calls
                              if any('[server]' in str(item) for item in argv)],
                             'an escalation step ran after the swap was detected')

    def test_every_venv_python_step_uses_the_verified_descriptor(self):
        """pip и import check идут через тот же проверенный каталог, не по пути.

        Абсолютный `str(venv / 'bin/python')` -- ровно та точка, где между
        проверкой и exec можно подменить имя; проверка требует cwd-привязку
        к fd и относительное имя интерпретатора (#811).
        """
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-fd-', dir='/tmp') as directory:
            venv = Path(directory) / 'venv'
            self.made(venv)
            executor = Executor()
            install_runtime.create_venv(venv, pip_extra='server', source=ROOT,
                                        execute=executor, timeout=60)
            steps = executor.pip_steps()
            self.assertTrue(steps, 'the venv python steps are missing')
            for argv, options in steps:
                self.assertEqual(argv[0], 'bin/python',
                                 'the venv python must not be re-resolved by path')
                self.assertNotIn(str(venv), argv[0])
                self.assertEqual(options.get('cwd'), '/proc/self/fd/' + str(options.get('pass_fds', (0,))[0]),
                                 'the venv step must be pinned by directory fd')

    # HYPOTHESIS: cwd-привязки достаточно, и интерпретатор можно не привязывать
    # к fd: тогда исполняется файл, заново разрешённый по имени (#811).
    # SOLUTION: executable шага -- fd проверенного интерпретатора, а не путь;
    # проверка читает его из /proc/self/fd во время вызова executor.
    # RESULT: снятие `executable=` из options оставляет прежние проверки
    # зелёными; эта -- красная (mutation proof, см. STATUS).
    # STATUS: FIXED — see #811.
    def test_the_interpreter_is_executed_through_its_verified_descriptor(self):
        from mop.server import install_runtime
        seen = []

        problems = []

        class Probe(Executor):
            def __call__(probe, argv, **options):
                if argv[:1] == ['bin/python']:
                    executable = options.get('executable')
                    if not executable or not executable.startswith('/proc/self/fd/'):
                        problems.append('the interpreter was not executed through a descriptor')
                    else:
                        number = int(executable.rsplit('/', 1)[1])
                        directory = int(options['cwd'].rsplit('/', 1)[1])
                        if number not in options.get('pass_fds', ()):
                            problems.append('the interpreter fd is not inherited by the child')
                        if number == directory:
                            problems.append('the interpreter fd is the venv directory fd')
                        if os.path.realpath('/proc/self/fd/%d' % number) != PYTHON:
                            problems.append('the executed descriptor is not the verified interpreter')
                        seen.append(number)
                return super().__call__(argv, **options)

        with tempfile.TemporaryDirectory(prefix='mfi-exec-', dir='/tmp') as directory:
            venv = Path(directory) / 'venv'
            self.made(venv)
            executor = Probe()
            install_runtime.create_venv(venv, pip_extra='server', source=ROOT,
                                        execute=executor, timeout=60)
            self.assertEqual(problems, [])
            self.assertEqual(len(seen), len(executor.pip_steps()),
                             'a venv python step ran without the verified interpreter fd')

    # HYPOTHESIS: единственная проверка после `_verify_venv` -- pre-exec
    # `_same_tree`; без неё первый шаг исполняется в дереве, чей путь уже
    # подменён (review round 2: удаление только этой проверки оставляло всё
    # зелёным) (#811).
    # SOLUTION: тест на ручном setup (reserved=None) подменяет дерево сразу
    # после `_verify_venv` и требует, чтобы ни один шаг не исполнился.
    # RESULT: с проверкой -- отказ до шага; без неё первый pip исполняется и
    # отказ приходит только из post-step проверки (mutation proof).
    # STATUS: FIXED — see #811.
    def test_a_swap_between_verification_and_exec_never_reaches_a_step(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-pre-', dir='/tmp') as directory:
            base = Path(directory)
            venv = base / 'venv'
            self.made(venv)
            replacement = base / 'replacement'
            replacement.mkdir(mode=0o755)
            executor = Executor()
            verify = install_runtime._verify_venv
            calls = []
            def swap(descriptor, path):
                verify(descriptor, path)
                # Ветка reuse проверяет дерево дважды; подмена -- после
                # последней проверки и до pre-exec `_same_tree`.
                calls.append(True)
                if len(calls) > 1:
                    os.rename(venv, base / 'verified')
                    os.rename(replacement, venv)
            with patch.object(install_runtime, '_verify_venv', side_effect=swap):
                with self.assertRaisesRegex(RuntimeError, 'prepared tree'):
                    install_runtime.create_venv(venv, pip_extra='server', source=ROOT,
                                                execute=executor, timeout=60)
            self.assertEqual(executor.calls, [],
                             'a step ran after the verified tree was swapped')
            self.assertTrue((base / 'verified' / 'pyvenv.cfg').is_file(),
                            'the verified tree must be retained, not executed by path')

    # HYPOTHESIS: reuse принимает regular file с любым exec-битом за
    # интерпретатор venv и исполняет его от root (#811).
    # SOLUTION: только стандартная форма venv -- symlink на проверенный
    # интерпретатор; regular file отвергается до первого шага.
    # RESULT: RED -- `#!/bin/sh` в bin/python исполнился, установка вернула успех.
    # STATUS: FIXED — see #811.
    def test_a_regular_file_planted_as_the_interpreter_is_never_executed(self):
        """Реальный executor: шаг запускается ровно с fd-привязкой `_python`.

        `bin/python` -- не symlink, а обычный файл `#!/bin/sh`, который
        коснётся маркера. Настоящий `python -m venv` без `--copies` (именно
        его запускает установка) делает symlink (`bin/python -> python3 ->
        <interpreter>`), поэтому regular file -- не форма venv установки, и
        исполнять его нельзя (#811).
        """
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-plant-', dir='/tmp') as directory:
            base = Path(directory)
            venv = base / 'venv'
            (venv / 'bin').mkdir(parents=True)
            marker = base / 'planted-executed'
            (venv / 'bin' / 'python').write_text('#!/bin/sh\ntouch %s\n' % marker)
            (venv / 'bin' / 'python').chmod(0o755)
            (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
            for path in (venv, venv / 'bin'):
                os.chmod(path, 0o755)
            os.chmod(venv / 'pyvenv.cfg', 0o644)
            calls = []
            def execute(argv, **options):
                calls.append(list(argv))
                if options.get('executable') and argv[:1] == ['bin/python']:
                    # Настоящее исполнение того, что выбрал _python: только
                    # прямой запуск доказывает, что planted файл не поедет.
                    run = {key: options[key] for key in ('cwd', 'pass_fds', 'executable')
                           if key in options}
                    result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            timeout=30, check=False, **run)
                    return subprocess.CompletedProcess(argv, result.returncode, b'', result.stderr)
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            with self.assertRaisesRegex(RuntimeError, 'not installation-owned'):
                install_runtime.create_venv(venv, pip_extra='server', wheels=None,
                                            source=ROOT, execute=execute, timeout=60)
            self.assertFalse(marker.exists(), 'a planted regular bin/python ran as the installer')
            self.assertEqual(calls, [], 'a planted interpreter must never reach an execution step')

    def test_venv_python_symlink_outside_installation_is_refused(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-venv-', dir='/tmp') as directory:
            venv = Path(directory) / 'venv'
            self.made(venv)
            (venv / 'bin/python').unlink()
            (venv / 'bin/python').symlink_to('/bin/true')
            executor = Executor()
            with self.assertRaisesRegex(RuntimeError, 'not installation-owned'):
                install_runtime.create_venv(venv, pip_extra='server',
                                            wheels=None, execute=executor, timeout=60)
            self.assertEqual(executor.calls, [])

    def test_manual_setup_does_not_follow_a_symlink_ancestor(self):
        """Ручной setup идёт тем же dirfd-путём, что установка (#811)."""
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-venv-', dir='/tmp') as directory:
            outside = Path(directory) / 'outside'
            outside.mkdir()
            parent = Path(directory) / 'alias'
            parent.symlink_to(outside, target_is_directory=True)
            executor = Executor()
            with self.assertRaisesRegex(RuntimeError, 'not creatable'):
                install_runtime.create_venv(parent / 'venv', pip_extra='server',
                                            wheels=None, execute=executor, timeout=60)
            self.assertEqual(list(outside.iterdir()), [])


# HYPOTHESIS: `_run` отбрасывает stderr, и настоящая причина отказа pip/ensurepip
# до оператора не доходит (#811).
# SOLUTION: безопасная последняя строка отказа в сообщении; частичный venv не
# остаётся молча -- удаляется либо назван retained.
# RESULT: RED -- сообщение было только `command exited 1`.
# STATUS: FIXED — see #811.
class FailureDiagnosis(unittest.TestCase):
    def failing(self, step, stderr):
        """Executor: venv создаётся, шаг `step` падает с заданным stderr."""
        root = Path(tempfile.mkdtemp(prefix='mfi-fail-', dir='/tmp'))
        self.addCleanup(lambda: __import__('shutil').rmtree(root, ignore_errors=True))
        venv = root / 'venv'
        def execute(argv, **kwargs):
            if argv[1:3] == ['-m', 'venv']:
                (venv / 'bin').mkdir(parents=True)
                (venv / 'bin/python').symlink_to(PYTHON)
                (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            if step == argv[-1] or (step == '[server]' and str(argv[-1]).endswith('[server]')):
                return subprocess.CompletedProcess(argv, 1, b'', stderr)
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        return venv, execute

    def test_missing_build_backend_reason_reaches_the_operator(self):
        from mop.server import install_runtime
        venv, execute = self.failing('[server]', b'Collecting mop\n'
            b'ERROR: Could not find a version that satisfies the requirement setuptools>=40.8.0\n')
        with self.assertRaises(RuntimeError) as caught:
            install_runtime.create_venv(venv, pip_extra='server', source=ROOT,
                                        execute=execute, timeout=60)
        message = str(caught.exception)
        self.assertIn('Could not find a version that satisfies the requirement setuptools>=40.8.0', message)
        self.assertIn('server extra', message)
        self.assertFalse(venv.exists(), 'an incomplete venv must not be left silently')

    def test_ensurepip_reason_survives_venv_creation(self):
        from mop.server import install_runtime
        root = Path(tempfile.mkdtemp(prefix='mfi-fail-', dir='/tmp'))
        self.addCleanup(lambda: __import__('shutil').rmtree(root, ignore_errors=True))
        venue = root / 'venv'
        def execute(argv, **kwargs):
            if argv[1:3] == ['-m', 'venv']:
                return subprocess.CompletedProcess(argv, 1, b'',
                    b'Error: ensurepip is not available: python3-venv is missing\n')
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        with self.assertRaises(RuntimeError) as caught:
            install_runtime.create_venv(venue, pip_extra='server', source=ROOT,
                                        execute=execute, timeout=60)
        self.assertIn('python3-venv is missing', str(caught.exception))
        self.assertFalse(venue.exists())

    def test_generic_pip_wrapper_text_does_not_hide_the_real_error(self):
        """pip заканчивает общим subprocess-exited-with-error; причина -- глубже."""
        from mop.server import install_runtime
        venv, execute = self.failing('[server]',
            b'  error: subprocess-exited-with-error\n'
            b'  \xe2\x94\x82 ERROR: Could not find a version that satisfies '
            b'the requirement setuptools>=40.8.0 (from versions: none)\n'
            b'  \xe2\x94\x82 ERROR: No matching distribution found for setuptools>=40.8.0\n'
            b'  note: This error originates from a subprocess\n'
            b'error: subprocess-exited-with-error\n')
        with self.assertRaises(RuntimeError) as caught:
            install_runtime.create_venv(venv, pip_extra='server', source=ROOT,
                                        execute=execute, timeout=60)
        message = str(caught.exception)
        self.assertIn('No matching distribution found for setuptools>=40.8.0', message)
        self.assertNotIn('subprocess-exited-with-error', message)

    def test_index_credentials_are_not_copied_into_the_reason(self):
        from mop.server import install_runtime
        cases = {
            'userinfo': b'ERROR: failed https://user:s3cret@index.example.invalid/simple\n',
            'query token': b'ERROR: failed https://index.example.invalid/simple?token=s3cret\n',
            'query token pair': b'ERROR: failed https://index.example.invalid/simple?a=1&token=s3cret&b=2\n',
            'path segment': b'ERROR: failed https://index.example.invalid/token/s3cret/simple\n',
            'percent-encoded': b'ERROR: https://index.example.invalid/token/s3cret%2Fx/simple\n',
            'key parameter': b'ERROR: failed https://index.example.invalid/simple?api_key=s3cret\n',
        }
        for label, stderr in cases.items():
            with self.subTest(label=label):
                venv, execute = self.failing('[server]', stderr)
                with self.assertRaises(RuntimeError) as caught:
                    install_runtime.create_venv(venv, pip_extra='server', source=ROOT,
                                                execute=execute, timeout=60)
                message = str(caught.exception)
                self.assertNotIn('s3cret', message)
                self.assertNotIn('s3cret%2Fx', message)
                # Соседние query-параметры и хост не исчезают: отчёт остаётся
                # оператору полезным.
                self.assertIn('index.example.invalid', message)

    def test_redaction_keeps_ordinary_urls_and_names(self):
        from mop.server import install_runtime
        plain = 'ERROR: no matching distribution for https://index.example.invalid/simple'
        self.assertEqual(install_runtime._redacted(plain), plain)
        # Слово «token» вне credential-позиции не съедает соседний текст.
        self.assertIn('tokenizer', install_runtime._redacted('ERROR: tokenizer package missing'))

    def test_existing_venv_is_named_retained_on_failure(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-venv-', dir='/tmp') as directory:
            venue = Path(directory) / 'venv'
            VenvReuse().made(venue)
            def execute(argv, **kwargs):
                if '[server]' in str(argv[-1]) and 'pip' in argv:
                    return subprocess.CompletedProcess(argv, 1, b'', b'ERROR: offline wheel missing\n')
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            with self.assertRaises(RuntimeError) as caught:
                install_runtime.create_venv(venue, pip_extra='server', source=ROOT,
                                            execute=execute, timeout=60)
            self.assertIn('offline wheel missing', str(caught.exception))
            self.assertIn('retained', str(caught.exception))
            self.assertTrue(venue.is_dir())


class RenderedUnits(unittest.TestCase):
    def test_bound_credential_unit_declares_the_venv_interpreter(self):
        from mop.server.installation import service_artifact, specification
        spec = specification(installation())
        values = {'public_origin': 'https://public.example.invalid', 'http_bind': '127.0.0.1',
                  'http_port': 9443, 'db_pool_max': 8, 'instance_db_connections': 24,
                  'admin_db_headroom': 8}
        result = service_artifact(spec, values, credential_uid=1944, credential_gid=1944,
                                  frontend_uids={'web': 1945, 'worker': 1946})
        venv = dict(spec.runtime)['venv']
        for role in ('web', 'worker', 'credentials'):
            with self.subTest(role=role):
                unit = result['units'][role]
                self.assertIn('Environment=MOP_HOST_PYTHON=' + venv + '/bin/python', unit)

    def test_units_exec_the_venv_interpreter_and_declare_it_as_host_python(self):
        from mop.server.installation import service_artifact, specification
        spec = specification(installation())
        values = {'public_origin': 'https://public.example.invalid', 'http_bind': '127.0.0.1',
                  'http_port': 9443, 'db_pool_max': 8, 'instance_db_connections': 24,
                  'admin_db_headroom': 8}
        result = service_artifact(spec, values)
        venv = dict(spec.runtime)['venv']
        for role in ('web', 'worker'):
            with self.subTest(role=role):
                unit = result['units'][role]
                # -P: WorkingDirectory=<state> не попадает в sys.path, как в bin/mop.
                self.assertIn('ExecStart=' + venv + '/bin/python -P -m mop.server.controller ', unit)
                self.assertNotIn('ExecStart=' + venv + '/bin/mop server controller', unit)
                self.assertIn('Environment=MOP_HOST_PYTHON=' + venv + '/bin/python', unit)

    def test_controller_package_entrypoint_dispatches_the_controller_cli(self):
        command = [sys.executable, '-m', 'mop.server.controller', '--help']
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30,
                                env={key: value for key, value in os.environ.items()
                                     if not key.startswith(('MOP_', 'DJANGO_'))})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('setup', result.stdout)


# HYPOTHESIS: `_remove_incomplete` получает owned=None на ветке существующего
# пустого каталога и rmtree идёт по пути: подменённый каталог удаляется (#811).
# SOLUTION: без удержанного fd удаление запрещено; удаление -- по резерву.
# RESULT: RED -- подставленное дерево исчезало после провала pip.
# STATUS: FIXED — see #811.
class IncompleteRemoval(unittest.TestCase):
    def test_a_substituted_directory_is_never_removed(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-remove-', dir='/tmp') as directory:
            base = Path(directory)
            venv = base / 'venv'
            venv.mkdir()
            venv.chmod(0o755)
            replacement = base / 'replacement'
            replacement.mkdir()
            (replacement / 'keep.txt').write_text('not installation data')
            def execute(argv, **kwargs):
                if argv[1:3] == ['-m', 'venv']:
                    # скелет: и абсолютный путь (старая ветка), и cwd-fd (новая)
                    target = Path(kwargs['cwd']) if argv[3] == '.' else Path(argv[3])
                    (target / 'bin').mkdir(parents=True, exist_ok=True)
                    (target / 'bin' / 'python').symlink_to(PYTHON)
                    (target / 'pyvenv.cfg').write_text('home = /usr/bin\n')
                    return subprocess.CompletedProcess(argv, 0, b'', b'')
                if 'pip' in argv and any('[server]' in str(item) for item in argv):
                    # окно между веткой пустого каталога и провалом pip
                    os.rename(venv, base / 'original')
                    os.rename(replacement, venv)
                    return subprocess.CompletedProcess(argv, 1, b'', b'ERROR: boom\n')
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            with self.assertRaises(RuntimeError) as caught:
                install_runtime.create_venv(venv, pip_extra='server', source=ROOT,
                                            execute=execute, timeout=60)
            self.assertIn('boom', str(caught.exception))
            self.assertTrue((venv / 'keep.txt').is_file(),
                            'removal followed the path into a substituted directory')

    def test_remove_incomplete_refuses_without_a_reservation(self):
        """Без резерва удалять нечего доказывать: путь -- не доказательство."""
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-remove-', dir='/tmp') as directory:
            venv = Path(directory) / 'venv'
            venv.mkdir()
            (venv / 'keep.txt').write_text('foreign')
            self.assertFalse(install_runtime._remove_incomplete(venv, None))
            self.assertTrue((venv / 'keep.txt').is_file())

    def test_remove_incomplete_removes_only_the_reserved_inode(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-remove-', dir='/tmp') as directory:
            base = Path(directory)
            venv = base / 'venv'
            descriptor = os.open(str(base), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.mkdir('venv', 0o700, dir_fd=descriptor)
                reserved = os.open('venv', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=descriptor)
                try:
                    (venv / 'partial.txt').write_text('partial')
                    self.assertTrue(install_runtime._remove_incomplete(venv, reserved))
                    self.assertFalse(venv.exists())
                    # Тот же вызов с чужим путём: подменённый каталог остаётся.
                    other = base / 'other'
                    other.mkdir()
                    (other / 'keep.txt').write_text('foreign')
                    self.assertFalse(install_runtime._remove_incomplete(other, reserved))
                    self.assertTrue((other / 'keep.txt').is_file())
                finally:
                    os.close(reserved)
            finally:
                os.close(descriptor)


# HYPOTHESIS: `_owned` смотрел только на 0o002 и собственника: root-owned файл
# с группой атакующего и mode 0775, group-writable pyvenv.cfg и
# world-writable bin/ принимались, и групповой писатель исполнялся от root.
# SOLUTION: запрет записи группы/остальных на объекте и чужая группа -- отказ;
# проверяются venv root, bin/, pyvenv.cfg и интерпретатор.
# RESULT: RED -- все четыре случая принимались.
# STATUS: FIXED — see #811.
class Ownership(unittest.TestCase):
    def test_group_and_world_writable_objects_are_refused(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-mode-', dir='/tmp') as directory:
            venv = Path(directory) / 'venv'
            VenvReuse().made(venv)
            cases = (
                ('pyvenv.cfg group-writable', venv / 'pyvenv.cfg', 0o664),
                ('bin world-writable', venv / 'bin', 0o777),
                ('venv root group-writable', venv, 0o775),
            )
            for label, path, mode in cases:
                with self.subTest(label=label):
                    original = os.lstat(path).st_mode & 0o777
                    os.chmod(path, mode)
                    try:
                        with self.assertRaisesRegex(RuntimeError, 'not installation-owned'):
                            install_runtime.create_venv(venv, pip_extra='server',
                                                        wheels=None, source=ROOT,
                                                        execute=Executor(), timeout=60)
                    finally:
                        os.chmod(path, original)
            # Групповой писатель, подсунутый вместо интерпретатора: regular
            # file с exec-битом отвергается по форме, а не по mode (#811).
            (venv / 'bin' / 'python').unlink()
            (venv / 'bin' / 'python').write_text('')
            os.chmod(venv / 'bin' / 'python', 0o775)
            with self.assertRaisesRegex(RuntimeError, 'not installation-owned'):
                install_runtime.create_venv(venv, pip_extra='server', wheels=None,
                                            source=ROOT, execute=Executor(), timeout=60)

    def test_a_file_of_an_arbitrary_foreign_group_is_refused(self):
        from mop.server import install_runtime
        info = types.SimpleNamespace(st_uid=0, st_gid=424242, st_mode=stat.S_IFREG | 0o755)
        self.assertFalse(install_runtime._owned(info),
                         'an arbitrary gid outside the invoking groups is foreign')
        trusted = types.SimpleNamespace(st_uid=0, st_gid=os.getegid(),
                                        st_mode=stat.S_IFREG | 0o755)
        self.assertTrue(install_runtime._owned(trusted))

    def test_group_writable_pyvenv_cfg_and_bin_redden_the_reuse_path(self):
        """Тот же отказ доходит и до create_venv, не только до _verify_venv."""
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-mode-', dir='/tmp') as directory:
            venv = Path(directory) / 'venv'
            VenvReuse().made(venv)
            os.chmod(venv / 'pyvenv.cfg', 0o664)
            executor = Executor()
            with self.assertRaisesRegex(RuntimeError, 'not installation-owned'):
                install_runtime.create_venv(venv, pip_extra='server', wheels=None,
                                            source=ROOT, execute=executor, timeout=60)
            self.assertEqual(executor.calls, [])

    # HYPOTHESIS: `_owned` смотрит только на group-write (0o022) и не видит
    # S_ISGID: каталог 02755 с gid из доверенных групп принимался, хотя
    # setgid -- тот же чужой контроль над записью, что и group-write (#811).
    # SOLUTION: S_ISGID на любом объекте установки -- отказ; `_reserve` создаёт
    # 0755/0700, поэтому легитимному пути setgid не нужен.
    # RESULT: RED -- venv root и bin/ с mode 02755 принимались (executed: YES).
    # STATUS: FIXED — see #811.
    def test_setgid_directories_are_refused_wherever_ownership_is_checked(self):
        from mop.server import install_runtime
        directory = types.SimpleNamespace(st_uid=0, st_gid=os.getegid(),
                                          st_mode=stat.S_IFDIR | stat.S_ISGID | 0o755)
        self.assertFalse(install_runtime._owned(directory),
                         'a setgid directory is not installation-owned')
        with tempfile.TemporaryDirectory(prefix='mfi-sgid-', dir='/tmp') as base:
            for label, mode in (('venv root setgid', (Path(base) / 'venv', 0o2755)),
                                ('bin setgid', (Path(base) / 'venv' / 'bin', 0o2755))):
                with self.subTest(label=label):
                    venv = Path(base) / 'venv'
                    if venv.exists():
                        import shutil
                        shutil.rmtree(venv)
                    VenvReuse().made(venv)
                    path, value = mode
                    os.chmod(path, value)
                    self.assertTrue(os.lstat(path).st_mode & stat.S_ISGID,
                                    'the fixture did not set the setgid bit')
                    executor = Executor()
                    with self.assertRaisesRegex(RuntimeError, 'not installation-owned'):
                        install_runtime.create_venv(venv, pip_extra='server', wheels=None,
                                                    source=ROOT, execute=executor, timeout=60)
                    self.assertEqual(executor.calls, [],
                                     'a setgid tree reached an execution step')


# HYPOTHESIS: сравнение деревьев шло по st_ino без st_dev: каталог на другом
# устройстве с тем же номером inode считался тем же самым (#811).
# SOLUTION: везде пара (st_dev, st_ino).
# RESULT: RED -- подменённый st_dev проходил все три проверки.
# STATUS: FIXED — see #811.
class TreeIdentity(unittest.TestCase):
    def test_a_different_device_with_the_same_inode_is_not_the_same_tree(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-dev-', dir='/tmp') as directory:
            base = Path(directory)
            venv = base / 'venv'
            venv.mkdir()
            held = os.stat(venv)
            other_device = stat_like(held, dev=held.st_dev + 1, ino=held.st_ino)
            self.assertNotEqual(install_runtime._identity(other_device),
                                install_runtime._identity(held))
            descriptor = os.open(str(venv), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                swap = {os.lstat: lambda path: other_device}
                with patch.object(install_runtime, 'os', OsProxy(lstat=swap[os.lstat])):
                    with self.assertRaisesRegex(RuntimeError, 'prepared tree'):
                        install_runtime._check_reservation(descriptor, venv)
                    self.assertFalse(install_runtime._remove_incomplete(venv, descriptor))
                self.assertTrue(venv.is_dir(), 'a same-inode other-device tree was deleted')
            finally:
                os.close(descriptor)

    def test_same_device_and_inode_still_matches(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-dev-', dir='/tmp') as directory:
            venv = Path(directory) / 'venv'
            venv.mkdir()
            descriptor = os.open(str(venv), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                self.assertTrue(install_runtime._same_tree(descriptor, venv))
                install_runtime._check_reservation(descriptor, venv)
            finally:
                os.close(descriptor)


# HYPOTHESIS: `_normalize_modes` мог бросить PermissionError, а ловился только
# RuntimeError: частичный venv оставался без имени и причины; и он переписывал
# права всех bin/* и прочих файлов, включая чужие исполняемые (#811).
# SOLUTION: реальный класс ошибки доходит до причины и обрабатывается как
# провал шага; нормализуются только файлы свежесозданного дерева.
# RESULT: RED -- PermissionError выходил мимо; чужой mode 0700 становился 0755.
# STATUS: FIXED — see #811.
class ModeNormalization(unittest.TestCase):
    def test_permission_failure_is_named_and_the_partial_venv_handled(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-perm-', dir='/tmp') as directory:
            base = Path(directory)
            venv = base / 'venv'
            def execute(argv, **kwargs):
                if argv[1:3] == ['-m', 'venv']:
                    (venv / 'bin').mkdir(parents=True)
                    (venv / 'bin/python').symlink_to(PYTHON)
                    (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
                    return subprocess.CompletedProcess(argv, 0, b'', b'')
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            # Отказ правки прав -- обе формы: path-chmod и fd-fchmod, чтобы
            # проверка была красной именно из-за необработанного класса.
            denied = self._denied()
            with patch.object(install_runtime, 'os', OsProxy(chmod=denied, fchmod=denied)):
                with self.assertRaises(RuntimeError) as caught:
                    install_runtime.create_venv(venv, pip_extra='server', source=ROOT,
                                                execute=execute, timeout=60)
            message = str(caught.exception)
            self.assertIn('PermissionError', message)
            self.assertIn('permission denied', message.lower())
            self.assertIn('removed incomplete venv', message)
            self.assertFalse(venv.exists())

    def test_existing_reused_venv_is_never_renormalized(self):
        """Чужой исполняемый файл в переиспользованном venv не переписывается."""
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-keep-', dir='/tmp') as directory:
            venv = Path(directory) / 'venv'
            VenvReuse().made(venv)
            foreign = venv / 'bin' / 'foreign-tool'
            foreign.write_text('#!/bin/sh\n')
            foreign.chmod(0o700)
            install_runtime.create_venv(venv, pip_extra='server', source=ROOT,
                                        execute=Executor(), timeout=60)
            self.assertEqual(foreign.stat().st_mode & 0o777, 0o700,
                             'a foreign executable in a reused venv was rewritten')

    @staticmethod
    def _denied():
        def deny(*args, **kwargs):
            raise PermissionError(13, 'Permission denied')
        return deny


if __name__ == '__main__':
    unittest.main()
