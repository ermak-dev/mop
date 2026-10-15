"""Backend/привилегии — инфраструктурная граница, не бизнес-правила."""
from django.conf import settings
from django.core.checks import Error, Tags, register
from django.db import connections, DatabaseError


@register(Tags.database)
def database_guarantees(app_configs=None, databases=None, **kwargs):
    errors = []
    for alias in databases or ['default']:
        connection = connections[alias]
        try:
            connection.ensure_connection()
            if connection.vendor != 'postgresql' or connection.pg_version // 10000 not in {16, 17}:
                errors.append(Error('Controller requires PostgreSQL 16 or 17.', id='controller.E001'))
                continue
            features = connection.features
            required = ('supports_transactions', 'has_select_for_update',
                        'supports_foreign_keys', 'supports_table_check_constraints')
            missing = [name for name in required if not getattr(features, name, False)]
            if missing:
                errors.append(Error('Database lacks required guarantees: ' + ', '.join(missing),
                                    id='controller.E002'))
            if settings.BOOTSTRAP['role'] != 'maintenance':
                # Только проверка PostgreSQL ACL на инфраструктурной границе.
                # Ни схема, ни прикладные запросы от этого SQL не зависят.
                with connection.cursor() as cursor:
                    cursor.execute('''
                        SELECT EXISTS (
                            SELECT 1 FROM pg_roles
                            WHERE pg_has_role(current_user, oid, 'MEMBER')
                              AND (rolsuper OR rolcreatedb OR rolcreaterole)
                        ), has_schema_privilege(current_user, current_schema(), 'CREATE'),
                        EXISTS (
                            SELECT 1 FROM pg_tables
                            WHERE schemaname = current_schema() AND tableowner = current_user
                        )
                    ''')
                    unsafe = any(cursor.fetchone())
                if unsafe:
                    errors.append(Error('Runtime DB principal must not own schema/tables or have DDL, '
                                        'database or role administration rights.', id='controller.E004'))
        except DatabaseError as exc:
            errors.append(Error(f'Controller database unavailable ({type(exc).__name__}).',
                                id='controller.E003'))
    return errors
