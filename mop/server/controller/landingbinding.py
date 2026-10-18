"""Landing composition #638: SQL-only, без Operation/node/внешнего эффекта."""
from uuid import UUID

from django.db import transaction

from mop.server.access.services import Access, current_actor
from mop.server.sessions.services import Landing, Conflict, Unavailable
from .runtime import operation_installation_state, short_transaction


class ProjectLanding:
    """Публичная граница завершает собственный COMMIT до возврата владения.

    Domain Landing остаётся единственным путём изменения токена. Restore guard
    и installation lock идут раньше project/token, сеть под ними запрещена.
    effects_enabled не нужен: это человеческая SQL-конфигурация, не pool effect.
    """
    def _call(self, actor, project_id, action, *, epoch=None, expected_revision=None):
        if transaction.get_connection().in_atomic_block or not transaction.get_connection().get_autocommit():
            raise Unavailable('landing requires no surrounding transaction')
        current_actor(actor)
        if type(project_id) is not UUID or epoch is not None and type(epoch) is not UUID:
            raise ValueError('typed project and epoch UUIDs required')
        if action != 'get' and (type(epoch) is not UUID or type(expected_revision) is not int
                or not 0 <= expected_revision <= 9223372036854775807):
            raise ValueError('installation epoch and nonnegative expected revision required')
        permission = {'get': 'view_landing', 'acquire': 'acquire_landing', 'release': 'release_landing'}[action]
        with short_transaction():
            try:
                installation = operation_installation_state()
            except RuntimeError as error:
                raise Unavailable(str(error)) from None
            Access().authorize(actor, 'execution.' + permission, project_ids=(project_id,))
            if epoch is not None and epoch != installation.epoch:
                raise Conflict('installation epoch changed; read current landing before a new intent')
            domain = Landing()
            row = (domain.get(actor, project_id) if action == 'get' else
                   getattr(domain, action)(actor, project_id, expected_revision=expected_revision))
            landing = {'project_id': str(project_id),
                'state': 'unmaterialized' if row is None else 'free' if row.owner_id is None else 'held',
                'revision': '0' if row is None else str(row.revision),
                'generation': None if row is None else str(row.generation),
                'owner_id': None if row is None else row.owner_id}
            result = {'schema_version': 1, 'epoch': str(installation.epoch), 'landing': landing}
        return result

    def get(self, actor, project_id, *, epoch=None):
        return self._call(actor, project_id, 'get', epoch=epoch)

    def acquire(self, actor, project_id, *, epoch, expected_revision):
        return self._call(actor, project_id, 'acquire', epoch=epoch, expected_revision=expected_revision)

    def release(self, actor, project_id, *, epoch, expected_revision):
        return self._call(actor, project_id, 'release', epoch=epoch, expected_revision=expected_revision)


def landing_service():
    return ProjectLanding()
