"""Независимый процесс: ORM claim не зависит от памяти первого worker."""
def claim(database, bootstrap, operation_id, ready, output):
    import os
    os.environ['MOP_CONTROLLER_BOOTSTRAP'] = bootstrap
    from mop.server.controller.runtime import configure
    configure()
    from django.conf import settings
    settings.DATABASES['default']['NAME'] = database
    settings.DATABASES['default']['CONN_MAX_AGE'] = 0
    settings.DATABASES['default']['OPTIONS'].pop('pool', None)
    from django.db import connections
    from mop.server.controller.runtime import builds_service
    try:
        ready.wait(timeout=15)
        output.put(builds_service().claim(operation_id))
    finally:
        connections.close_all()
