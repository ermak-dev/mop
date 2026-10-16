"""Spawn targets: framework запускается после выбора isolated test database."""

def _configure(database, bootstrap):
    import os
    # hermetic в spawn main очищает MOP_*; путь fixture передаётся явно.
    os.environ['MOP_CONTROLLER_BOOTSTRAP'] = bootstrap
    from mop.server.controller.runtime import configure
    configure()
    from django.db import connections
    connections.databases['default']['NAME'] = database


def read(database, bootstrap, actor, operation_id, result):
    _configure(database, bootstrap)
    from django.db import connections
    from mop.server.controller.runtime import operations_service
    try:
        result.put(operations_service().status(actor, operation_id))
    finally:
        connections.close_all()


def claim(database, bootstrap, operation_id, ready, result):
    _configure(database, bootstrap)
    from django.db import connections
    from mop.server.controller.runtime import operations_service
    try:
        ready.wait(timeout=10)
        result.put(operations_service().claim(operation_id))
    finally:
        connections.close_all()
