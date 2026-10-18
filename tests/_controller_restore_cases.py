"""Аварии restore на настоящей PostgreSQL; ни старого пула, ни общей БД."""
from contextlib import contextmanager
import copy
from pathlib import Path
import tempfile
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql
from django.conf import settings
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings

from mop.server.controller import backup as archive_tools
from mop.server.controller.models import Installation
from mop.server.controller.runtime import initialize, verify_installation


# BUG: COMMIT архива возвращал старый epoch/effects_enabled до отдельного fence.
# HYPOTHESIS: отдельная ORM-строка guard, исключённая из архива, переживает
# pg_restore и аварии; initialize/startup не вправе выдавать ей clean заново.
class RestoreSafety(TransactionTestCase):
    serialized_rollback = True

    @contextmanager
    def target(self, *, migrate=False, name_prefix='restore_guard_'):
        config = copy.deepcopy(connection.settings_dict)
        config['NAME'] = name_prefix + uuid.uuid4().hex
        params = {key.lower(): config[key] for key in ('HOST', 'PORT', 'USER', 'PASSWORD')}
        params['dbname'] = 'postgres'
        allowed = type(self).databases
        with psycopg.connect(**params, autocommit=True) as provisioner:
            provisioner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(config['NAME'])))
            type(self).databases = allowed | {'restore_guard_target'}
            connections.databases['restore_guard_target'] = config
            try:
                if migrate:
                    call_command('migrate', database='restore_guard_target', verbosity=0, interactive=False)
                yield 'restore_guard_target'
            finally:
                target = connections['restore_guard_target']
                target.close()
                target.close_pool()
                del connections['restore_guard_target']
                del connections.databases['restore_guard_target']
                type(self).databases = allowed
                provisioner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(config['NAME'])))

    @contextmanager
    def archive(self):
        self.original = initialize()
        Installation.objects.filter(pk=1).update(effects_enabled=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.dump'
            archive_tools.backup(path)
            yield path

    def assert_restart_denied(self, using):
        from mop.server.controller.runtime import require_restore_ready
        connections[using].close()
        connections[using].close_pool()
        with self.assertRaisesRegex(RuntimeError, 'restore|guard|maintenance'):
            require_restore_ready(using)
        with self.assertRaisesRegex(RuntimeError, 'restore|guard|maintenance'):
            verify_installation(using)
        with self.assertRaisesRegex(RuntimeError, 'restore|guard|maintenance'):
            initialize(using)
        # Совсем новый процесс не наследует Python globals/соединения restorer.
        import json
        import subprocess
        import sys
        code = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from mop.server.controller import runtime
runtime.configure()
from django.db import connections
connections.databases['default']['NAME'] = sys.argv[2]
connections['default'].ensure_connection()
if connections['default'].connection.info.dbname != sys.argv[2]:
    raise SystemExit('restart probe connected to the wrong database')
results = {}
try:
    for name in ('verify_installation', 'initialize'):
        try:
            getattr(runtime, name)()
        except RuntimeError as exc:
            results[name] = str(exc)
        else:
            raise SystemExit(name + ' accepted interrupted restore')
    print(json.dumps(results))
finally:
    runtime.close_pools()
'''
        child = subprocess.run([sys.executable, '-I', '-c', code, str(Path(__file__).resolve().parents[1]),
                                connections[using].settings_dict['NAME']],
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 0, child.stderr or child.stdout)
        for reason in json.loads(child.stdout).values():
            self.assertRegex(reason, 'restore|guard|maintenance')

    def test_restore_arm_checks_physical_models_not_permission_metadata(self):
        # BUG: unmanaged metadata-модель имеет ContentType, но не SQL-таблицу.
        # HYPOTHESIS: проверка пустоты должна обходить только модели, для которых
        # Django действительно создаёт таблицы, не пропуская реальные данные.
        # SOLUTION: ModelOptions.can_migrate отделяет физические модели от metadata.
        # RESULT: PG17 red — UndefinedTable при полном migration ledger;
        # PG16/17 green — по 23 restore/secret/trust проверки, реальные данные отклонены.
        # STATUS: FIXED — see #623; с #685 контрольные права controller
        # регистрирует Installation, unmanaged дубль удалён.
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        from mop.server.controller.models import Installation, RestoreGuard
        from mop.server.controller.runtime import _schema_ready
        with self.target(migrate=True) as target:
            _schema_ready(target)
            # Канонический владелец controller-прав — Installation (#685);
            # единственная registration-строка без unmanaged дубля.
            self.assertTrue(Permission.objects.using(target).filter(
                content_type__app_label='controller', content_type__model='installation',
                codename='manage_proxy').exists())
            self.assertEqual(Permission.objects.using(target).filter(
                content_type__app_label='controller', codename='manage_proxy').count(), 1)
            generation = archive_tools._arm_restore(target)
            connections[target].close()
            connections[target].close_pool()
            guard = RestoreGuard.objects.using(target).get(pk=1)
            self.assertEqual(guard.state, 'restoring')
            self.assertEqual(guard.generation, generation)
            self.assertFalse(Installation.objects.using(target).exists())
        with self.target(migrate=True) as target:
            get_user_model().objects.using(target).create(username='not-an-empty-target')
            with self.assertRaisesRegex(RuntimeError, 'empty'):
                archive_tools._arm_restore(target)
            self.assertEqual(RestoreGuard.objects.using(target).get(pk=1).state, 'uninitialized')
            self.assertTrue(get_user_model().objects.using(target).filter(username='not-an-empty-target').exists())

    def test_guard_database_failure_does_not_assert_permanent_maintenance(self):
        # BUG #753: произвольный отказ SQL объявлялся доказательством сломанного
        # guard; постоянный maintenance допустим только для проверенного state.
        # HYPOTHESIS: safe boundary должна скрывать raw details, не выдумывая
        # причину отказа или необходимость восстановления.
        # SOLUTION: общий DB refusal остаётся unavailable без permanent
        # maintenance; missing/not-ready guard по-прежнему требует recovery.
        # RESULT: red — лишний maintenance suffix у синтетического DB refusal;
        # 194/194 PG17 concurrency/restore/grant/RBAC checks green.
        # STATUS: FIXED — see #753.
        from django.db import DatabaseError
        from mop.server.controller.runtime import require_restore_ready
        with patch('django.db.models.query.QuerySet.get', side_effect=DatabaseError('fixture-private-detail')):
            with self.assertRaises(RuntimeError) as failure:
                require_restore_ready()
        self.assertEqual(str(failure.exception), 'controller restore guard is unavailable: DatabaseError')

    def test_backup_cannot_export_an_uncommitted_installation(self):
        # pg_dump использует другую connection и не видит initialize до COMMIT.
        with self.target(migrate=True) as using, tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'uncommitted.dump'
            with transaction.atomic(using=using):
                initialize(using)
                with self.assertRaisesRegex(RuntimeError, 'transaction|autocommit'):
                    archive_tools.backup(path, using=using)
                self.assertFalse(path.exists())

    def test_guard_word_in_database_name_does_not_make_archive_forbidden(self):
        # TOC header содержит dbname, но это не TABLE/CONSTRAINT guard.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'guard-named-source.dump'
            with self.target(migrate=True, name_prefix='controller_restoreguard_test_') as source:
                initialize(source)
                archive_tools.backup(path, using=source)
            with self.target() as target:
                result = archive_tools.restore(path, using=target)
                self.assertFalse(result['effects_enabled'])
                self.assertEqual(verify_installation(target)['epoch'], result['epoch'])

    def test_restore_refuses_an_ambient_transaction_before_archive_io(self):
        # Arm внутри чужого atomic был бы лишь savepoint, не durable COMMIT.
        with self.archive() as archive, self.target(migrate=True) as target:
            @contextmanager
            def manual_transaction():
                connections[target].set_autocommit(False)
                try:
                    yield
                finally:
                    connections[target].rollback()
                    connections[target].set_autocommit(True)
            for mode in ('atomic', 'manual'):
                outer = transaction.atomic(using=target) if mode == 'atomic' else manual_transaction()
                with self.subTest(mode=mode), outer, patch.object(
                        archive_tools, '_private_path', side_effect=AssertionError('archive I/O under ambient SQL transaction')) as path_io:
                    with self.assertRaisesRegex(RuntimeError, 'transaction|autocommit'):
                        archive_tools.restore(archive, using=target)
                    path_io.assert_not_called()

    def test_restore_crashes_before_during_and_after_archive_commit_stay_fenced(self):
        tool = archive_tools._tool
        with self.archive() as archive:
            for phase in ('before', 'during', 'after'):
                with self.subTest(phase=phase), self.target() as target:
                    reached = []
                    generations = []
                    def interrupt(command, *args, **kwargs):
                        self.assertFalse(connections[target].in_atomic_block, 'tool I/O under SQL transaction')
                        if command != 'pg_restore' or '--single-transaction' not in kwargs.get('extra', ()):
                            return tool(command, *args, **kwargs)
                        reached.append(phase)
                        from mop.server.controller.models import RestoreGuard
                        guard = RestoreGuard.objects.using(target).get(pk=1)
                        self.assertEqual(guard.state, 'restoring')
                        generations.append(guard.generation)
                        if phase == 'before':
                            raise SystemExit('before restore')
                        if phase == 'during':
                            # HYPOTHESIS 1 (WRONG): обрыв хвоста обязан дать отказ;
                            # PG16/17 приняли архив без последних 128 байт (pipeline 5431).
                            # HYPOTHESIS 2: роль без владения даёт настоящий DDL-отказ
                            # внутри restore transaction, не ошибку чтения заголовка.
                            config = connections[target].settings_dict
                            params = {key.lower(): config[key] for key in ('HOST', 'PORT', 'USER', 'PASSWORD')}
                            params['dbname'] = 'postgres'
                            role = 'restore_denied_' + uuid.uuid4().hex
                            with psycopg.connect(**params, autocommit=True) as admin:
                                admin.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
                                try:
                                    kwargs['extra'] = (*kwargs['extra'], '--role=' + role)
                                    with self.assertRaisesRegex(RuntimeError, 'must be owner|permission denied') as refused:
                                        tool(command, *args, **kwargs)
                                    raise refused.exception
                                finally:
                                    admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
                        tool(command, *args, **kwargs)
                        raise SystemExit('after archive commit')
                    with patch.object(archive_tools, '_tool', interrupt):
                        with self.assertRaises((SystemExit, RuntimeError)):
                            archive_tools.restore(archive, using=target)
                    self.assertEqual(reached, [phase], 'failure did not reach the requested restore boundary')
                    self.assert_restart_denied(target)
                    from mop.server.controller.models import RestoreGuard
                    durable = RestoreGuard.objects.using(target).get(pk=1)
                    self.assertEqual(durable.state, 'restoring')
                    self.assertEqual([durable.generation], generations)
                    if phase == 'after':
                        row = Installation.objects.using(target).get(pk=1)
                        self.assertTrue(row.effects_enabled)
                        self.assertEqual(str(row.epoch), self.original['epoch'])
                    else:
                        self.assertFalse(Installation.objects.using(target).exists())

    def test_fence_commit_crash_keeps_guard_until_successful_completion(self):
        from mop.server.controller import runtime
        tool_verify = runtime._verify_installation_state
        with self.archive() as archive, self.target() as target:
            def crash(using):
                self.assertFalse(connections[using].in_atomic_block, 'root/schema verification under SQL lock')
                row = Installation.objects.using(using).get(pk=1)
                self.assertFalse(row.effects_enabled)
                self.assertNotEqual(str(row.epoch), self.original['epoch'])
                tool_verify(using)
                raise SystemExit('after fence before guard clear')
            with patch.object(runtime, '_verify_installation_state', crash):
                with self.assertRaisesRegex(SystemExit, 'after fence'):
                    archive_tools.restore(archive, using=target)
            self.assert_restart_denied(target)

    def test_completed_restore_preserves_guard_generation_and_disables_effects(self):
        from mop.server.controller.models import RestoreGuard
        from mop.server.controller.runtime import require_restore_ready
        tool = archive_tools._tool
        generations = []
        with self.archive() as archive, self.target(migrate=True) as target:
            def observe(command, *args, **kwargs):
                if command == 'pg_restore' and '--single-transaction' in kwargs.get('extra', ()):
                    generations.append(RestoreGuard.objects.using(target).get(pk=1).generation)
                return tool(command, *args, **kwargs)
            with patch.object(archive_tools, '_tool', observe):
                result = archive_tools.restore(archive, using=target)
            self.assertEqual(len(generations), 1)
            self.assertEqual(require_restore_ready(target), str(generations[0]))
            self.assertEqual(verify_installation(target), initialize(target))
            self.assertFalse(result['effects_enabled'])
            self.assertNotEqual(result['epoch'], self.original['epoch'])
            guard = RestoreGuard.objects.using(target).get(pk=1)
            self.assertEqual(guard.state, 'ready')
            self.assertNotEqual(guard.generation, RestoreGuard.objects.get(pk=1).generation)

    def test_wrong_identity_and_root_retain_guard(self):
        with self.archive() as archive:
            for mismatch in ('identity', 'root'):
                with self.subTest(mismatch=mismatch), self.target() as target:
                    changed = dict(settings.BOOTSTRAP)
                    if mismatch == 'identity':
                        changed['installation_id'] = 'wrong-installation'
                    else:
                        wrong = archive.parent / 'wrong-root.key'
                        wrong.write_bytes(b'different-protected-material' * 4)
                        wrong.chmod(0o600)
                        changed['root_key'] = str(wrong)
                    with override_settings(BOOTSTRAP=changed):
                        with self.assertRaisesRegex(RuntimeError, mismatch):
                            archive_tools.restore(archive, using=target)
                    self.assert_restart_denied(target)
                    self.assertFalse(Installation.objects.using(target).get(pk=1).effects_enabled)

    def test_archives_with_guard_entries_are_rejected_before_archive_execution(self):
        from mop.server.controller.models import RestoreGuard
        initialize()
        with tempfile.TemporaryDirectory() as directory, self.target() as target:
            archive = Path(directory) / 'forbidden.dump'
            # Нарочно обходим backup: произвольный pg_dump включает guard schema/data.
            with archive.open('wb') as output:
                archive_tools._tool('pg_dump', connection.settings_dict, major=connection.pg_version // 10000,
                                    output=output, extra=('--format=custom', '--no-owner', '--no-acl'))
            archive.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'guard'):
                archive_tools.restore(archive, using=target)
            self.assertFalse(Installation.objects.using(target).exists())
            self.assertEqual(RestoreGuard.objects.using(target).get(pk=1).state, 'restoring')
            self.assert_restart_denied(target)

    def test_incompatible_archive_migration_history_is_rejected_without_replacing_guard(self):
        from django.db.migrations.recorder import MigrationRecorder
        initialize()
        with tempfile.TemporaryDirectory() as directory, self.target() as target:
            archive = Path(directory) / 'wrong-schema.dump'
            # Архив с прежней/чужой схемой нельзя объявить текущим по target ledger.
            MigrationRecorder(connection).record_unapplied('controller', '0002_restore_guard')
            try:
                with archive.open('wb') as output:
                    archive_tools._tool('pg_dump', connection.settings_dict, major=connection.pg_version // 10000,
                                        output=output, extra=('--format=custom', '--no-owner', '--no-acl',
                                                              '--exclude-table=public.controller_restoreguard'))
            finally:
                MigrationRecorder(connection).record_applied('controller', '0002_restore_guard')
            archive.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'schema|migration'):
                archive_tools.restore(archive, using=target)
            self.assertFalse(Installation.objects.using(target).exists())
            self.assert_restart_denied(target)

    def test_missing_guard_is_not_recreated_by_initialize_or_runtime(self):
        from mop.server.controller.models import RestoreGuard
        initialize()
        RestoreGuard.objects.all().delete()
        self.assert_restart_denied('default')

    def test_uninitialized_guard_cannot_legitimize_an_existing_installation(self):
        from mop.server.controller.models import RestoreGuard
        original = initialize()
        RestoreGuard.objects.filter(pk=1).update(state='uninitialized')
        with self.assertRaisesRegex(RuntimeError, 'restore|guard|maintenance'):
            initialize()
        self.assertEqual(RestoreGuard.objects.get(pk=1).state, 'uninitialized')
        self.assertEqual(str(Installation.objects.get(pk=1).epoch), original['epoch'])

    def test_missing_guard_table_is_not_repaired_by_initialize(self):
        from mop.server.controller.models import RestoreGuard
        with self.target(migrate=True) as target:
            initialize(target)
            with connections[target].schema_editor() as editor:
                editor.delete_model(RestoreGuard)
            self.assert_restart_denied(target)
            self.assertNotIn(RestoreGuard._meta.db_table, connections[target].introspection.table_names())

    def test_guard_api_can_join_a_short_admission_transaction(self):
        from mop.server.controller.models import RestoreGuard
        from mop.server.controller.runtime import require_restore_ready
        initialize()
        with transaction.atomic():
            generation = require_restore_ready(lock=True)
            self.assertEqual(generation, str(RestoreGuard.objects.get(pk=1).generation))
        RestoreGuard.objects.filter(pk=1).update(state='restoring')
        with transaction.atomic():
            with self.assertRaisesRegex(RuntimeError, 'restore|guard|maintenance'):
                require_restore_ready(lock=True)

    def test_asgi_does_not_start_adapters_when_restore_guard_is_dirty(self):
        from asgiref.sync import async_to_sync
        from asgiref.testing import ApplicationCommunicator
        from mop.server.controller.models import RestoreGuard
        from mop.server.controller.asgi import ControllerApplication
        from mop.server.controller.runtime import Runtime
        initialize()
        RestoreGuard.objects.filter(pk=1).update(state='restoring')
        class Adapter:
            started = False
            async def start(self):
                self.started = True
            async def aclose(self):
                pass
        async def run():
            adapter = Adapter()
            app = ControllerApplication(runtime=Runtime(adapters=(adapter,)), startup=verify_installation)
            lifecycle = ApplicationCommunicator(app, {'type': 'lifespan'})
            await lifecycle.send_input({'type': 'lifespan.startup'})
            response = await lifecycle.receive_output(timeout=3)
            self.assertEqual(response['type'], 'lifespan.startup.failed')
            self.assertRegex(response['message'], 'restore|guard|maintenance')
            self.assertFalse(adapter.started)
            await lifecycle.wait()
        async_to_sync(run)()

    def test_guard_generation_cannot_be_cleared_by_a_stale_restorer(self):
        from mop.server.controller.models import RestoreGuard
        from mop.server.controller import runtime
        verify = runtime._verify_installation_state
        replacement = uuid.uuid4()
        with self.archive() as archive, self.target() as target:
            def replace_generation(using):
                result = verify(using)
                RestoreGuard.objects.using(using).filter(pk=1).update(generation=replacement)
                return result
            with patch.object(runtime, '_verify_installation_state', replace_generation):
                with self.assertRaisesRegex(RuntimeError, 'generation|guard'):
                    archive_tools.restore(archive, using=target)
            self.assertEqual(RestoreGuard.objects.using(target).get(pk=1).generation, replacement)
            self.assert_restart_denied(target)
