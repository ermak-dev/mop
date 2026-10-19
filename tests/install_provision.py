"""Explicit OS preparation: owned tempdirs/fake accounts, never host provisioning."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _fresh_install_fixture import FreshPlanFixture, ROOT

# #811: интерпретатор -- абсолютный установленный путь, не `python3` из PATH.
PYTHON = os.path.realpath(sys.executable)


def fake_execute(calls, version=b'3.13.5\n'):
    """Подставной executor установки: версия python, скелет venv, успех.

    Venv создаётся через cwd-привязку к fd зарезервированного дерева
    (#811), поэтому скелет пишется по `cwd`, а не по абсолютному пути.
    """
    def execute(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ['-m', 'venv']:
            # Каталог шага: cwd-привязка к fd (#811) или абсолютный путь из
            # ветки без резерва -- фикстура видит оба.
            venv = Path(kwargs['cwd']) if kwargs.get('cwd') else Path(argv[3])
            (venv / 'bin').mkdir(parents=True, exist_ok=True)
            # bin/python -- symlink на проверенный интерпретатор, как у
            # настоящего `python -m venv` без `--copies` (#811).
            (venv / 'bin' / 'python').symlink_to(PYTHON)
            (venv / 'bin' / 'pip').write_text('')
            (venv / 'bin' / 'pip').chmod(0o755)
            (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
            (venv / 'pyvenv.cfg').chmod(0o644)
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        return subprocess.CompletedProcess(
            argv, 0, version if 'version_info' in str(argv[-1]) else b'', b'')
    return execute


# HYPOTHESIS: provisioning описывает OS resources, но не имеет конечного apply.
# SOLUTION: отдельный stdlib executor; весь preflight до первого OS изменения.
# RESULT: owned tempdirs и fake OS executor, не установка host/PVE и не rollout.
# STATUS: FIXED — see #644.
class ProvisionTests(FreshPlanFixture, unittest.TestCase):
    def module(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('mop.server.install_provision'),
                             'finite stdlib OS provisioning executor is missing')
        from mop.server import install_provision
        return install_provision

    def document(self):
        from mop.server.installation import specification
        self.spec['paths'] = {key: str(Path(self.tmp.name) / key) for key in self.spec['paths']}
        self.spec['runtime'] = {**self.spec['runtime'],
                                'venv': str(Path(self.tmp.name) / 'venv')}
        return specification(self.spec)

    def test_apply_flag_is_required_and_preview_never_executes(self):
        module = self.module()
        spec = self.document()
        calls = []
        with self.assertRaisesRegex(ValueError, 'explicit apply'):
            module.apply(spec, apply=False, execute=lambda *a, **k: calls.append((a, k)))
        self.assertEqual(calls, [])
        self.assertFalse(Path(dict(spec.paths)['configuration']).exists())

    def test_finite_executor_uses_stdin_no_shell_or_secret_argv(self):
        module = self.module()
        spec = self.document()
        calls = []
        def execute(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        result = module.apply(spec, apply=True, execute=execute)
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv[:4], ['sudo', '-n', 'python3', '-I'])
        self.assertNotIn('shell', kwargs)
        document = json.loads(kwargs['input'])
        self.assertEqual(document['database']['topology'], 'remote')
        # #811: внешний kill должен быть достижим -- он больше внутреннего
        # venv-бюджета (1800 s), а не 900 s при 1800 s внутри.
        self.assertGreater(kwargs['timeout'], 1800)
        self.assertTrue(result['os_prepared'])
        self.assertFalse(result['services_started'])
        self.assertFalse(result['installation_accepted'])

    def test_real_isolated_executor_refuses_unprivileged_os_changes_without_legacy_reads(self):
        import sys
        module = self.module()
        from mop.cli.server import install
        self.assertTrue(callable(getattr(install, 'provision_target', None)),
                        'privileged preparation diagnostics need a CLI-owned frontend')
        self.assertNotEqual(os.geteuid(), 0, 'OS refusal proof requires an ordinary test user')
        data = module.document(self.document())
        (Path(self.tmp.name) / '.env').write_text('not a source of configuration')
        result = subprocess.run([sys.executable, '-I', '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); '
            'from mop.cli.server.install import provision_target; sys.exit(provision_target())', str(ROOT)],
            input=json.dumps(data).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.tmp.name, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {'error': 'OS preparation requires explicit root execution'})
        self.assertEqual(result.stderr, b'')
        for path in data['paths'].values():
            self.assertFalse(Path(path).exists())

    def test_fresh_root_environment_does_not_require_sudo_to_install_sudo(self):
        module = self.module()
        calls = []
        def execute(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        with patch.object(module.os, 'geteuid', return_value=0):
            module.apply(self.document(), apply=True, execute=execute)
        self.assertEqual(calls[0][:2], ['python3', '-I'])

    def test_provisioned_postgresql_cannot_pretend_client_package_is_server(self):
        module = self.module()
        self.spec['database']['topology'] = 'provisioned'
        calls = []
        with self.assertRaisesRegex(RuntimeError, 'server TLS.*network'):
            module.apply(self.document(), apply=True, execute=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_cli_effect_flag_reaches_real_finite_path_not_full_runtime_refusal(self):
        from mop.cli.server import install
        module = self.module()
        self.document()
        self.write()
        with patch.object(module, 'apply', return_value={'os_prepared': True}) as run:
            self.assertEqual(install.main(['provisioning', '--spec', str(self.path), '--apply']), 0)
        self.assertTrue(run.call_args.kwargs['apply'])

    def test_target_preflight_refuses_every_existing_tree_before_accounts_or_packages(self):
        module = self.module()
        spec = self.document()
        document = module.document(spec)
        for key in self.spec['paths']:
            with self.subTest(key=key):
                path = Path(self.spec['paths'][key])
                path.mkdir()
                calls = []
                with patch.object(module.os, 'geteuid', return_value=0), \
                     patch.object(module.pwd, 'getpwnam', side_effect=KeyError), \
                     patch.object(module.grp, 'getgrnam', side_effect=KeyError):
                    with self.assertRaisesRegex(RuntimeError, 'new target'):
                        module.prepare(document, execute=lambda *a, **k: calls.append(a))
                self.assertEqual(calls, [])
                path.rmdir()

    def test_symlink_ancestor_and_existing_account_refused_without_effects(self):
        module = self.module()
        spec = self.document()
        document = module.document(spec)
        linked = Path(self.tmp.name) / 'alias'
        linked.symlink_to(self.tmp.name, target_is_directory=True)
        document['paths']['state'] = str(linked / 'state')
        calls = []
        with patch.object(module.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                module.prepare(document, execute=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])
        document = module.document(spec)
        with patch.object(module.os, 'geteuid', return_value=0), \
             patch.object(module.pwd, 'getpwnam', return_value=object()):
            with self.assertRaisesRegex(RuntimeError, 'identity already exists'):
                module.prepare(document, execute=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_owned_tempdir_apply_creates_separated_roots_without_starting_service(self):
        module = self.module()
        document = module.document(self.document())
        calls = []
        execute = fake_execute(calls)
        with patch.object(module.os, 'geteuid', return_value=0), \
             patch.object(module.pwd, 'getpwnam', side_effect=KeyError), \
             patch.object(module.grp, 'getgrnam', side_effect=KeyError):
            module.prepare(document, execute=execute)
        paths = document['paths']
        # #807: venv создаёт сама установка, а не отдельный ручной setup.
        self.assertTrue(Path(document['runtime']['venv']).is_dir(), 'installation owns the venv')
        # #811: venv создаётся в удержанном дереве: cwd привязан к fd резерва,
        # а не к пути, который между preflight и этим шагом мог быть подменён.
        created = [argv for argv in calls if argv[1:3] == ['-m', 'venv']]
        self.assertEqual(created, [[PYTHON, '-m', 'venv', '.']])
        for role in document['identities']:
            for base in ('configuration', 'state'):
                self.assertEqual((Path(paths[base]) / role).stat().st_mode & 0o777, 0o700)
        # #644/#695: общий каталог собранной статики -- maintenance пишет, web читает.
        shared = Path(paths['state']) / 'static'
        self.assertEqual(shared.stat().st_mode & 0o777, 0o750)
        identities = document['identities']
        self.assertIn(['chown', identities['maintenance'] + ':' + identities['web'], str(shared)], calls)
        root = Path(paths['root_trust']) / 'root.key'
        self.assertEqual(len(root.read_bytes()), 32)
        self.assertEqual(root.stat().st_mode & 0o777, 0o600)
        keys = [(Path(paths['configuration']) / role / 'django.key').read_bytes()
                for role in ('maintenance', 'web', 'worker', 'gateway', 'credentials')]
        self.assertEqual(len(set(keys)), 1, 'auth signatures require shared signing material')
        for role in ('web', 'worker', 'gateway', 'builder'):
            self.assertFalse((Path(paths['configuration']) / role / 'root.key').exists())
        # #807: apt -- один вызов, клиент PostgreSQL и системные пакеты из
        # объявленного runtime, затем системный python и venv установки.
        self.assertIn(['apt-get', 'install', '-y', '--no-install-recommends', 'postgresql-client-16',
                       *document['runtime']['system_packages']], calls)
        self.assertFalse(any('systemctl' in argv or 'ansible-playbook' in argv for argv in calls))

    # HYPOTHESIS: реальный OS executor расходится с review-artifact и не
    # даёт разным web/worker uid прочесть service-owned socket directory.
    # SOLUTION: shared_directories у review/executor, точная supplementary group.
    # RESULT: RED -- каталог отсутствовал; GREEN -- 0750 без чтения private 0700.
    # STATUS: FIXED — see #644.
    def test_socket_directory_and_peer_groups_follow_the_same_installation_definition(self):
        module = self.module()
        from mop.server.installation import provisioning_artifact
        spec = self.document()
        calls = []
        execute = fake_execute(calls)
        with patch.object(module.os, 'geteuid', return_value=0), \
             patch.object(module.pwd, 'getpwnam', side_effect=KeyError), \
             patch.object(module.grp, 'getgrnam', side_effect=KeyError):
            module.prepare(module.document(spec), execute=execute)
        path = Path(dict(spec.paths)['state']) / 'credential-socket'
        self.assertTrue(path.is_dir(), 'protected service socket directory was not provisioned')
        self.assertEqual(path.stat().st_mode & 0o777, 0o750)
        users = dict(spec.identities)
        self.assertIn(['chown', users['credentials'] + ':' + users['credentials'], str(path)], calls)
        for role in ('web', 'worker'):
            self.assertIn(['usermod', '--append', '--groups', users['credentials'], users[role]], calls)
        for role in ('gateway', 'builder', 'maintenance'):
            self.assertNotIn(['usermod', '--append', '--groups', users['credentials'], users[role]], calls)
        self.assertIn(str(path), [item['path'] for item in provisioning_artifact(spec)['shared_directories']])
        for base in ('configuration', 'state'):
            self.assertEqual((Path(dict(spec.paths)[base]) / 'credentials').stat().st_mode & 0o777, 0o700)
        self.assertFalse(any('systemctl' in argv for argv in calls))

    def test_private_caller_umask_does_not_make_shared_ancestors_untraversable(self):
        module = self.module()
        self.document()
        parent = Path(self.tmp.name) / 'new-parent'
        self.spec['paths'] = {key: str(parent / key) for key in self.spec['paths']}
        from mop.server.installation import specification
        data = module.document(specification(self.spec))
        mask = os.umask(0o077)
        try:
            with patch.object(module.os, 'geteuid', return_value=0), \
                 patch.object(module.pwd, 'getpwnam', side_effect=KeyError), \
                 patch.object(module.grp, 'getgrnam', side_effect=KeyError):
                module.prepare(data, execute=fake_execute([]))
        finally:
            os.umask(mask)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual((parent / 'configuration' / 'web').stat().st_mode & 0o777, 0o700)

    def test_parent_replaced_after_preflight_never_receives_artifacts(self):
        module = self.module()
        self.document()
        parent = Path(self.tmp.name) / 'new-parent'
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir(mode=0o700)
        self.spec['paths']['configuration'] = str(parent / 'configuration')
        from mop.server.installation import specification
        document = module.document(specification(self.spec))
        absent = module._absent
        count = 0
        def race(path):
            nonlocal count
            absent(path)
            count += 1
            if count == len(document['paths']):
                parent.symlink_to(outside, target_is_directory=True)
        with patch.object(module.os, 'geteuid', return_value=0), \
             patch.object(module.pwd, 'getpwnam', side_effect=KeyError), \
             patch.object(module.grp, 'getgrnam', side_effect=KeyError), \
             patch.object(module, '_absent', side_effect=race):
            with self.assertRaises((OSError, RuntimeError)):
                module.prepare(document, execute=fake_execute([]))
        self.assertEqual(list(outside.iterdir()), [])

    # HYPOTHESIS: preflight `_absent` только проверяет, а venv резервируется
    # поздно (или не резервируется вовсе): между preflight и резервом symlink
    # предка уводит создание venv в чужое дерево (#811).
    # SOLUTION: venv резервируется тем же dirfd-механизмом ДО apt; подменённый
    # предок отвергается на резерве, до единого привилегированного шага.
    # RESULT: RED на ddcc29f0 -- `_reserve` для venv не зовётся вовсе, apt уже
    # выполнился, и дерево создано за symlink'ом.
    # STATUS: FIXED — see #811.
    def test_parent_replacement_reaches_the_reserve_gate(self):
        module = self.module()
        self.document()
        parent = Path(self.tmp.name) / 'venv-parent'
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir(mode=0o700)
        # Только venv: прочие деревья остаются в чистом каталоге, чтобы отказ
        # пришёл именно от резерва venv, а не от соседнего дерева.
        self.spec['runtime'] = {**self.spec['runtime'], 'venv': str(parent / 'venv')}
        from mop.server.installation import specification
        document = module.document(specification(self.spec))
        reserved = []
        steps = []
        reserve = module._reserve
        def gate(argv, **kwargs):
            steps.append(list(argv))
            # Версия системного python доходит до гейта: иначе тест умирает
            # на `found unusable` и никогда не входит в _reserve.
            return subprocess.CompletedProcess(
                argv, 0, b'3.13.5\n' if 'sys.version_info' in str(argv[-1]) else b'')
        def observe(path):
            reserved.append(path)
            if path == document['runtime']['venv']:
                # Окно после preflight, но до первого привилегированного шага:
                # предок venv подменяется symlink'ом.
                os.rename(parent, Path(self.tmp.name) / 'reserved-parent')
                parent.symlink_to(outside, target_is_directory=True)
            return reserve(path)
        with patch.object(module.os, 'geteuid', return_value=0), \
             patch.object(module.pwd, 'getpwnam', side_effect=KeyError), \
             patch.object(module.grp, 'getgrnam', side_effect=KeyError), \
             patch.object(module, '_reserve', side_effect=observe):
            with self.assertRaises((OSError, RuntimeError)):
                module.prepare(document, execute=gate)
        self.assertIn(document['runtime']['venv'], reserved,
                      'the venv must be reserved through the dirfd mechanism')
        self.assertEqual(list(outside.iterdir()), [],
                         'the substituted ancestor received the created tree')
        # После preflight venv до отказа не идёт ни один OS-шаг: подмена видна
        # до привилегированной работы, а не после неё.
        self.assertFalse([argv for argv in steps
                          if argv[:1] in (['apt-get'], ['groupadd'], ['useradd'], ['chown'], ['usermod'])],
                         'a privileged step ran before the venv reservation was gated')

    # HYPOTHESIS: после резерва путь к venv переразрешается, и подменённый во
    # время apt каталог исполняется от root (#811).
    # SOLUTION: исполнение привязано к fd резерва, а смена (dev, ino) --
    # типизированный отказ без исполнения.
    # RESULT: RED на ddcc29f0 -- подставленный bin/python исполнился от root.
    # STATUS: FIXED — see #811.
    def test_swap_after_reservation_is_caught_by_the_reserved_inode(self):
        module = self.module()
        self.document()
        base = Path(self.tmp.name) / 'base'
        base.mkdir(mode=0o755)
        outside = Path(self.tmp.name) / 'outside'
        (outside / 'venv' / 'bin').mkdir(parents=True)
        marker = outside / 'executed-as-root'
        (outside / 'venv' / 'bin' / 'python').write_text(
            '#!/bin/sh\ntouch %s\nexit 0\n' % marker)
        (outside / 'venv' / 'bin' / 'python').chmod(0o755)
        (outside / 'venv' / 'pyvenv.cfg').write_text('home = /usr/bin\n')
        self.spec['runtime'] = {**self.spec['runtime'], 'venv': str(base / 'outer' / 'venv')}
        from mop.server.installation import specification
        document = module.document(specification(self.spec))
        swapped = []
        steps = []
        def execute(argv, **kwargs):
            steps.append(list(argv))
            if argv[:1] == ['apt-get'] and not swapped:
                # Окно apt: предок venv подменяется symlink'ом на дерево с
                # подставленным интерпретатором. Установка, которая уже
                # зарезервировала предок, оставит реальный каталог; ветка без
                # резерва увидит только переименование/создание symlink'а.
                swapped.append(True)
                if (base / 'outer').is_symlink() or not (base / 'outer').exists():
                    os.symlink(outside, base / 'outer')
                else:
                    os.rename(base / 'outer', base / 'outer.reserved')
                    os.symlink(outside, base / 'outer')
            if os.path.isabs(argv[0]) and argv[0].endswith('/bin/python') \
                    and os.path.exists(argv[0]):
                # Старая ветка зовёт абсолютный путь: исполняем ровно его,
                # чтобы отказ был доказан фактом, а не рассуждением.
                result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                        capture_output=True)
                return subprocess.CompletedProcess(argv, result.returncode, b'', b'')
            return subprocess.CompletedProcess(
                argv, 0, b'3.13.5\n' if 'sys.version_info' in str(argv[-1]) else b'')
        with patch.object(module.os, 'geteuid', return_value=0), \
             patch.object(module.pwd, 'getpwnam', side_effect=KeyError), \
             patch.object(module.grp, 'getgrnam', side_effect=KeyError):
            with self.assertRaisesRegex(RuntimeError, 'prepared tree|reserved tree|escaped'):
                module.prepare(document, execute=execute)
        self.assertTrue(swapped, 'the apt window never opened')
        self.assertFalse(marker.exists(), 'the planted interpreter ran as root')
        # Отказ приходит на входе в create_venv: `_check_reservation` сверяет
        # путь с удержанным inode до первого шага venv. Pre-exec проверка в
        # create_venv -- защита того же рода для ручного setup, где резерва
        # нет; её одну краснит tests/install_runtime.py
        # (test_a_swap_between_verification_and_exec_never_reaches_a_step).
        self.assertFalse([argv for argv in steps
                          if argv[1:3] == ['-m', 'venv'] or argv[:1] == ['bin/python']],
                         'venv steps ran even though the reserved path had been swapped')

    def test_venv_is_reserved_by_dirfd_before_any_privileged_step(self):
        module = self.module()
        document = module.document(self.document())
        events = []
        reserve = module._reserve
        def observe(path):
            events.append(('reserve', path))
            return reserve(path)
        def execute(argv, **kwargs):
            events.append(('execute', argv))
            if argv[1:3] == ['-m', 'venv']:
                venv = Path(kwargs['cwd']) if kwargs.get('cwd') else Path(argv[3])
                (venv / 'bin').mkdir(parents=True, exist_ok=True)
                # Форма настоящего venv: symlink на проверенный интерпретатор.
                (venv / 'bin' / 'python').symlink_to(PYTHON)
                (venv / 'bin' / 'pip').write_text('')
                (venv / 'bin' / 'pip').chmod(0o755)
                (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
                (venv / 'pyvenv.cfg').chmod(0o644)
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            return subprocess.CompletedProcess(
                argv, 0, b'3.13.5\n' if 'sys.version_info' in str(argv[-1]) else b'')
        with patch.object(module.os, 'geteuid', return_value=0), \
             patch.object(module.pwd, 'getpwnam', side_effect=KeyError), \
             patch.object(module.grp, 'getgrnam', side_effect=KeyError), \
             patch.object(module, '_reserve', side_effect=observe):
            module.prepare(document, execute=execute)
        reserved = [path for kind, path in events if kind == 'reserve']
        self.assertIn(document['runtime']['venv'], reserved,
                      'the venv tree must be reserved with the dirfd mechanism (#811)')
        venv_at = events.index(('reserve', document['runtime']['venv']))
        apt_at = next(index for index, (kind, argv) in enumerate(events)
                      if kind == 'execute' and argv[:1] == ['apt-get'])
        self.assertLess(venv_at, apt_at,
                        'the venv reservation must precede apt')

    def test_existing_venv_path_is_refused_before_changes(self):
        module = self.module()
        document = module.document(self.document())
        venv = Path(document['runtime']['venv'])
        venv.mkdir()
        calls = []
        with patch.object(module.os, 'geteuid', return_value=0), \
             patch.object(module.pwd, 'getpwnam', side_effect=KeyError), \
             patch.object(module.grp, 'getgrnam', side_effect=KeyError):
            with self.assertRaisesRegex(RuntimeError, 'new target'):
                module.prepare(document, execute=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_reserve_refuses_a_symlink_ancestor_and_holds_the_created_inode(self):
        """Защита, без которой гонка выше снова получает чужие артефакты.

        Тест краснеет, если `_reserve` заменить на mkdir по пути: symlink
        принимается, а возвращённый descriptor не совпадает с деревом по
        паре (dev, ino) -- не только по номеру inode (#811).
        """
        module = self.module()
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir(mode=0o700)
        parent = Path(self.tmp.name) / 'new-parent'
        parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, RuntimeError)):
            descriptor = module._reserve(str(parent / 'configuration'))
            if descriptor is not None:
                os.close(descriptor)
        self.assertEqual(list(outside.iterdir()), [])
        clean = Path(self.tmp.name) / 'clean-parent' / 'configuration'
        descriptor = module._reserve(str(clean))
        self.assertIsInstance(descriptor, int,
                              'reserve must hand back the created directory descriptor')
        try:
            held = os.fstat(descriptor)
            named = os.lstat(clean)
            self.assertEqual((held.st_dev, held.st_ino), (named.st_dev, named.st_ino))
        finally:
            os.close(descriptor)

    def test_service_composition_uses_purpose_bootstrap_and_native_tls_without_claiming_runtime(self):
        from mop.server import installation
        renderer = getattr(installation, 'service_artifact', None)
        self.assertTrue(callable(renderer), 'derived service composition is missing')
        spec = self.document()
        values = {'public_origin': 'https://public.example.invalid', 'http_bind': '10.4.2.3',
                  'http_port': 9443, 'db_pool_max': 8, 'instance_db_connections': 24,
                  'admin_db_headroom': 8}
        result = renderer(spec, values)
        self.assertFalse(result['applied'])
        self.assertFalse(result['runtime_bound'])
        self.assertEqual(set(result['units']), {'web', 'worker'})
        self.assertIn(669, result['blocked_services']['credentials'])
        for role, unit in result['units'].items():
            self.assertIn('User=' + self.spec['identities'][role], unit)
            self.assertIn('SupplementaryGroups=' + self.spec['identities']['credentials'], unit)
            self.assertIn(self.spec['paths']['configuration'] + '/credentials', unit)
            self.assertIn(self.spec['paths']['state'] + '/credentials', unit)
            self.assertIn('MOP_CONTROLLER_BOOTSTRAP=' + self.spec['paths']['configuration'] + '/' + role + '/bootstrap.json', unit)
            for directive in ('NoNewPrivileges=true', 'ProtectSystem=strict', 'TimeoutStopSec=30', 'TasksMax=64'):
                self.assertIn(directive, unit)
            self.assertNotIn('EnvironmentFile', unit)
            self.assertNotIn('root.key', unit)
            self.assertNotIn('Wants=mop-web', unit)
        web = result['units']['web']
        self.assertIn('--host 10.4.2.3 --port 9443 --hostname public.example.invalid', web)
        self.assertIn('--tls-cert ', web)
        self.assertIn('--tls-key ', web)
        self.assertNotIn('10.4.2.3 --hostname 10.4.2.3', web)
        privileged_port = renderer(spec, {**values, 'http_port': 443})
        self.assertIn('AmbientCapabilities=CAP_NET_BIND_SERVICE', privileged_port['units']['web'])
        self.assertNotIn('AmbientCapabilities=CAP_NET_BIND_SERVICE', privileged_port['units']['worker'])
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            renderer(spec, {**values, 'public_origin': 'http://public.example.invalid'})

    # HYPOTHESIS: socket ACL есть, но private material и daemon unit отсутствуют.
    # SPEC: два purpose-owned root экземпляра, одна signing trust; daemon использует
    # существующий credentials entrypoint, точный path/group/peer UID, без readiness receipt.
    # SOLUTION: installer provisions private material и finite service composition.
    # RESULT: RED — нет private root/unit/resolver; pure GREEN, OS actions fake, не host apply.
    # STATUS: FIXED — see #644.
    def test_credential_private_keys_are_provisioned_without_granting_frontend_root_access(self):
        module = self.module()
        spec = self.document()
        data = module.document(spec)
        calls = []
        with patch.object(module.os, 'geteuid', return_value=0), \
             patch.object(module.pwd, 'getpwnam', side_effect=KeyError), \
             patch.object(module.grp, 'getgrnam', side_effect=KeyError):
            module.prepare(data, execute=fake_execute(calls))
        paths, users = data['paths'], data['identities']
        private = Path(paths['configuration']) / 'credentials'
        self.assertTrue((private / 'root.key').is_file(), 'credential root custody is missing')
        self.assertEqual((private / 'root.key').read_bytes(), (Path(paths['root_trust']) / 'root.key').read_bytes())
        self.assertEqual((private / 'django.key').read_bytes(),
                         (Path(paths['configuration']) / 'web' / 'django.key').read_bytes())
        for name in ('root.key', 'django.key'):
            path = private / name
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn(['chown', users['credentials'] + ':' + users['credentials'], str(path)], calls)
        for role in ('web', 'worker', 'gateway', 'builder'):
            self.assertFalse((Path(paths['configuration']) / role / 'root.key').exists())
        self.assertFalse(any('systemctl' in argv for argv in calls))

    def test_bound_credential_unit_uses_exact_os_peers_and_existing_daemon_lifecycle(self):
        from mop.server import installation
        import inspect
        self.assertIn('frontend_uids', inspect.signature(installation.service_artifact).parameters,
                      'service artifact cannot bind exact frontend OS peers')
        spec = self.document()
        values = {'public_origin': 'https://public.example.invalid', 'http_bind': '127.0.0.1',
                  'http_port': 9443, 'db_pool_max': 8, 'instance_db_connections': 24, 'admin_db_headroom': 8}
        peers = {'web': 1945, 'worker': 1946}
        result = installation.service_artifact(spec, values, credential_uid=1944,
                                               credential_gid=1944, frontend_uids=peers)
        self.assertEqual(set(result['units']), {'web', 'worker', 'credentials'})
        self.assertNotIn('credentials', result['blocked_services'])
        self.assertEqual(result['blocked_services'], {'worker': [625], 'builder': [648], 'gateway': [628]})
        self.assertFalse(result['runtime_bound'])
        self.assertFalse(result['services_started'])
        unit = result['units']['credentials']
        config = self.spec['paths']['configuration'] + '/credentials'
        socket_dir = self.spec['paths']['state'] + '/credential-socket'
        self.assertIn('User=' + self.spec['identities']['credentials'], unit)
        self.assertIn('Group=' + self.spec['identities']['credentials'], unit)
        self.assertIn('-m mop.cli server credentials --bootstrap ' + config + '/bootstrap.json', unit)
        self.assertIn(self.spec['runtime']['venv'] + '/bin/python -P -m mop.cli server credentials', unit)
        self.assertIn('--socket ' + socket_dir + '/service.sock --key-id root-v1', unit)
        self.assertIn('--socket-group 1944 --peer-uid 1945 --peer-uid 1946', unit)
        self.assertIn('ReadWritePaths=' + self.spec['paths']['state'] + '/credentials ' + socket_dir, unit)
        self.assertIn('TimeoutStopSec=30', unit)
        self.assertIn('KillSignal=SIGTERM', unit)
        self.assertIn('NoNewPrivileges=true', unit)
        self.assertNotIn('EnvironmentFile', unit)
        self.assertNotIn('ExecStartPre', unit, 'startup must not delete stale sockets')
        self.assertEqual(result['required_artifacts']['credentials'], ['bootstrap.json', 'django.key', 'root.key'])
        for role in ('web', 'worker'):
            self.assertIn(config, result['units'][role])
            self.assertNotIn('--key-id', result['units'][role])
        for bad in ({'web': 1944, 'worker': 1946}, {'web': 0, 'worker': 1946},
                    {'web': 1945}, {'web': 1945, 'worker': 1945},
                    {'web': True, 'worker': 1946}, {**peers, 'gateway': 1947}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'credential.*identit|frontend'):
                installation.service_artifact(spec, values, credential_uid=1944,
                                              credential_gid=1944, frontend_uids=bad)
        with self.assertRaisesRegex(ValueError, 'credential.*identit|frontend'):
            installation.service_artifact(spec, values, credential_uid=1944, frontend_uids=peers)

    def test_daemon_composition_resolves_only_declared_os_accounts_without_fallback(self):
        from mop.server import installation
        from types import SimpleNamespace
        import pwd
        import grp
        resolver = getattr(installation, 'credential_os_coordinates', None)
        self.assertTrue(callable(resolver), 'installer render has no exact OS peer resolver')
        spec = self.document()
        users = dict(spec.identities)
        accounts = {users[role]: SimpleNamespace(pw_uid=uid, pw_gid=uid)
                    for role, uid in (('credentials', 1944), ('web', 1945), ('worker', 1946))}
        with patch.object(pwd, 'getpwnam', side_effect=accounts.__getitem__) as lookup, \
             patch.object(grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=1944)):
            coordinates = resolver(spec)
        self.assertEqual(coordinates, {'credential_uid': 1944, 'credential_gid': 1944,
                                      'frontend_uids': {'web': 1945, 'worker': 1946}})
        self.assertEqual({call.args[0] for call in lookup.call_args_list}, set(accounts))
        with patch.object(pwd, 'getpwnam', side_effect=KeyError):
            with self.assertRaisesRegex(RuntimeError, 'OS identit'):
                resolver(spec)
        with patch.object(pwd, 'getpwnam', side_effect=accounts.__getitem__), \
             patch.object(grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=2000)):
            with self.assertRaisesRegex(RuntimeError, 'group'):
                resolver(spec)

    def test_safe_preflight_reason_survives_the_privileged_boundary(self):
        module = self.module()
        def execute(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1,
                json.dumps({'error': 'fresh service identity already exists; explicit maintenance required'}).encode(),
                b'')
        with self.assertRaisesRegex(RuntimeError, 'identity already exists'):
            module.apply(self.document(), apply=True, execute=execute)

    def test_executor_failure_never_leaks_raw_stderr_or_claims_success(self):
        module = self.module()
        def execute(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, b'secret-output', b'secret-stderr')
        with self.assertRaisesRegex(RuntimeError, 'partial.*retained') as caught:
            module.apply(self.document(), apply=True, execute=execute)
        self.assertNotIn('secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
