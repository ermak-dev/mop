"""Границы archive tools без PostgreSQL: SQL mode и разбор настоящих TOC entries.

HYPOTHESIS: pg_dump не видит незакоммиченный ORM snapshot, а guard-поиск
по всему TOC принимает имя базы/owner из metadata за защищённую таблицу.
SOLUTION: одинаковый autocommit precondition и проверка object entries.
RESULT: offline red 2 failures/2 errors, затем 6/6 narrow green;
37 Django cases на изолированных PG16 и PG17 подтвердили обе регрессии.
STATUS: FIXED — see #632 (archive boundary, не вся foundation).
"""
import io
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import hermetic  # noqa: F401
from _lib import offline

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    try:
        import django
    except ImportError:
        hermetic.skip('archive boundary', 'declared server extra is not installed')
        return
    from django.conf import settings
    settings.configure(INSTALLED_APPS=['mop.server.controller'], DATABASES={},
                       DEFAULT_AUTO_FIELD='django.db.models.BigAutoField')
    django.setup()
    unittest.main()


class ArchiveBoundary(unittest.TestCase):
    def test_backup_rejects_uncommitted_modes_before_verification_or_file_io(self):
        from mop.server.controller import backup, runtime
        for atomic, autocommit in ((True, True), (False, False)):
            connection = SimpleNamespace(in_atomic_block=atomic, get_autocommit=lambda: autocommit)
            with self.subTest(atomic=atomic), \
                    patch.object(backup, 'connections', {'default': connection}), \
                    patch.object(runtime, '_maintenance_role'), \
                    patch.object(runtime, 'verify_installation',
                                 side_effect=AssertionError('uncommitted verification reached')) as verify, \
                    patch.object(backup, '_private_path',
                                 side_effect=AssertionError('archive file I/O reached')) as file_io:
                with self.assertRaisesRegex(RuntimeError, 'autocommit|transaction'):
                    backup.backup('/private/backup.dump')
                verify.assert_not_called()
                file_io.assert_not_called()

    def plan(self, toc):
        from mop.server.controller import backup
        from django.db.migrations import recorder
        expected = [('controller', '0001_initial'), ('controller', '0002_restore_guard')]
        history = b'COPY public.django_migrations (id, app, name, applied) FROM stdin;\n'
        history += b'1\tcontroller\t0001_initial\t2026-10-16\n'
        history += b'2\tcontroller\t0002_restore_guard\t2026-10-16\n\\.\n'
        connection = SimpleNamespace(settings_dict={}, pg_version=170000)
        records = SimpleNamespace(migration_qs=SimpleNamespace(values_list=lambda *args: expected))

        def tool(*args, **kwargs):
            extra = kwargs['extra']
            if '--list' in extra:
                return toc
            if '--schema-only' in extra:
                return b'CREATE TABLE public.controller_installation (id integer);\n'
            return history

        with patch.object(backup, '_tool', side_effect=tool), \
                patch.object(recorder, 'MigrationRecorder', return_value=records):
            return backup._archive_plan(io.BytesIO(b'owned fixture archive'), connection)

    def test_toc_database_metadata_is_not_a_guard_object(self):
        toc = (b'; Archive metadata\n; dbname: controller_restoreguard_test\n'
               b'1; 2615 2200 SCHEMA - public mop_ci\n'
               b'2; 1259 16401 TABLE public catalog_project mop_ci\n')
        plan = self.plan(toc)
        self.assertIn(b'TABLE public catalog_project', plan)
        self.assertNotIn(b'SCHEMA - public', plan)

    # HYPOTHESIS: общий archive gate требует прежний Installation DROP/CREATE,
    # хотя #644 сохраняет sealed target schema, чтобы runtime не получил CRUD окно.
    # SOLUTION: прежняя metadata-проверка использует обычную таблицу; trust отдельно
    # проверяется строже: исходные data остаются, DDL и constraints не возвращаются.
    # RESULT: после merge #644 старый expectation red; новый контракт требует обе гарантии.
    # STATUS: FIXED — see #644 / #623.
    def test_installation_schema_is_target_owned_but_source_data_is_restored(self):
        data = b'3; 0 16401 TABLE DATA public controller_installation mop_ci\n'
        ordinary = b'4; 1259 16402 TABLE public catalog_project mop_ci\n'
        toc = (b'1; 1259 16401 TABLE public controller_installation mop_ci\n'
               b'2; 2606 16403 CONSTRAINT public controller_installation installation_pk mop_ci\n'
               + data + ordinary)
        self.assertEqual(self.plan(toc), data + ordinary)

    def test_toc_owner_is_not_a_guard_table(self):
        entry = b'2; 1259 16401 TABLE public catalog_project controller_restoreguard\n'
        self.assertEqual(self.plan(entry), entry)

    def test_guard_table_and_data_entries_are_still_forbidden(self):
        for kind in (b'TABLE', b'TABLE DATA', b'CONSTRAINT'):
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, 'forbidden.*guard'):
                self.plan(b'2; 1259 16401 ' + kind + b' public controller_restoreguard guard_pk mop_ci\n')


if __name__ == '__main__':
    with offline():
        main()
