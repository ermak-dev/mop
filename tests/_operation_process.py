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
    from mop.server.operations.service import Operations
    try:
        result.put(Operations().status(actor, operation_id))
    finally:
        connections.close_all()


def claim(database, bootstrap, operation_id, ready, result):
    _configure(database, bootstrap)
    from django.db import connections
    from mop.server.operations.service import Operations
    try:
        ready.wait(timeout=10)
        result.put(Operations().claim(operation_id))
    finally:
        connections.close_all()
