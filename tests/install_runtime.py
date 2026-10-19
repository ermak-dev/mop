"""Install runtime contract: declared system packages and a private venv (#807).

Спецификация установки объявляет runtime: требование к python, путь venv,
системные apt-пакеты и pip extra. Проверки здесь -- чистые: ОС-команды идут
в подставной executor, настоящие apt/venv не запускаются. Живой прогон на
Debian 13 -- отдельно, не этот файл.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
    """Подставной OS executor: версия python и успешные команды, без эффектов."""
    def __init__(self, version=b'3.13.5\n'):
        self.calls = []
        self.version = version

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        stdout = b''
        if len(argv) >= 3 and argv[0] == 'python3' and argv[1] == '-I' and 'sys.version_info' in argv[-1]:
            stdout = self.version
        return subprocess.CompletedProcess(argv, 0, stdout, b'')

    def commands(self, *prefix):
        return [argv for argv in self.calls if argv[:len(prefix)] == list(prefix)]


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
        created = executor.commands('python3', '-m', 'venv')
        self.assertEqual(created, [['python3', '-m', 'venv', venv]])
        pip = [argv for argv in executor.calls if argv[:4] == [venv + '/bin/python', '-m', 'pip', 'install']]
        self.assertTrue(pip, 'declared server extra was not installed into the venv')
        self.assertIn('--upgrade', pip[0])
        self.assertIn('pip', pip[0])
        self.assertTrue(any(argv[-1].endswith('[server]') for argv in pip),
                        'the declared server extra is missing from the install command')
        verified = [argv for argv in executor.calls
                    if argv[:2] == [venv + '/bin/python', '-I'] and 'import' in argv[-1]]
        self.assertTrue(verified, 'imports are not verified in the created venv')
        for name in ('django', 'rest_framework', 'channels', 'psycopg', 'uvicorn',
                     'ldap', 'django_auth_ldap'):
            self.assertIn(name, verified[0][-1])
        self.assertTrue(Path(venv).is_dir())
        self.assertEqual(Path(venv).stat().st_mode & 0o022, 0,
                         'venv must not be writable by group or other')

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
    def test_existing_venv_is_reused_instead_of_recreated(self):
        from mop.server import install_runtime
        with tempfile.TemporaryDirectory(prefix='mfi-venv-', dir='/tmp') as directory:
            venv = Path(directory) / 'venv'
            (venv / 'bin').mkdir(parents=True)
            (venv / 'bin/python').write_text('')
            executor = Executor()
            install_runtime.create_venv(venv, pip_extra='server',
                                        wheels=None, execute=executor, timeout=60)
            self.assertEqual(executor.commands('python3', '-m', 'venv'), [],
                             'an existing venv must not be recreated')
            self.assertTrue(executor.commands(str(venv / 'bin/python'), '-m', 'pip', 'install'))


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
                self.assertIn('ExecStart=' + venv + '/bin/python -m mop.server.controller ', unit)
                self.assertNotIn('ExecStart=' + venv + '/bin/mop server controller', unit)
                self.assertIn('Environment=MOP_HOST_PYTHON=' + venv + '/bin/python', unit)

    def test_controller_package_entrypoint_dispatches_the_controller_cli(self):
        command = [sys.executable, '-m', 'mop.server.controller', '--help']
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30,
                                env={key: value for key, value in os.environ.items()
                                     if not key.startswith(('MOP_', 'DJANGO_'))})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('setup', result.stdout)


if __name__ == '__main__':
    unittest.main()
