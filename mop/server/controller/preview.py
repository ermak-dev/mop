"""Owned local preview Admin (#712), не установка и не источник authority пула.

Только новая Docker PostgreSQL17 и loopback HTTPS. Секреты -- новые private
файлы, не argv/вывод. Процесс владеет лишь собственным контейнером и child.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import time
from urllib.request import HTTPSHandler, ProxyHandler, build_opener
from uuid import uuid4


@dataclass(frozen=True)
class PreviewPlan:
    directory: Path
    port: int

    def __post_init__(self):
        if (not isinstance(self.directory, Path) or not self.directory.is_absolute()
                or '..' in self.directory.parts or self.directory == Path('/')
                or type(self.port) is not int or not 1024 <= self.port <= 65535):
            raise ValueError('new absolute preview directory and unprivileged port required')

    @property
    def url(self):
        return f'https://localhost:{self.port}/admin/'


@dataclass(frozen=True)
class PreviewSecrets:
    maintenance: str = field(repr=False)
    web: str = field(repr=False)


def _write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)


def prepare_files(plan, *, database_port):
    plan.directory.mkdir(mode=0o700)
    root = plan.directory
    values = PreviewSecrets(secrets.token_urlsafe(32), secrets.token_urlsafe(32))
    for name in ('root.key', 'django.key'):
        _write(root/name, secrets.token_hex(64))
    _write(root/'admin-password', secrets.token_urlsafe(32)+'\n')
    _write(root/'postgres.env', 'POSTGRES_USER=preview_maintenance\nPOSTGRES_DB=preview\n'
           + 'POSTGRES_PASSWORD='+values.maintenance+'\n')
    database = dict(ENGINE='django.db.backends.postgresql', NAME='preview', HOST='127.0.0.1',
                    PORT=str(database_port), USER='preview_maintenance', PASSWORD=values.maintenance,
                    OPTIONS={'sslmode': 'disable'})
    base = dict(installation_id='local-admin-preview', django_key=str(root/'django.key'),
                static_root=str(root/'static'))
    _write(root/'maintenance.json', json.dumps(dict(base, role='maintenance', database=database,
                                                   root_key=str(root/'root.key'))))
    _write(root/'web.json', json.dumps(dict(base, role='web', database={**database,
                                            'USER': 'preview_web', 'PASSWORD': values.web})))
    return values


def _environment(bootstrap):
    # Только код текущего checkout и выбранный private bootstrap; старые MOP_*
    # не должны перенаправить preview на установку или шину.
    env = {key: value for key, value in os.environ.items() if not key.startswith('MOP_')}
    return {**env, 'MOP_ENV_FILE': os.devnull, 'MOP_CONTROLLER_BOOTSTRAP': str(bootstrap)}


def initialize_preview(directory):
    """Maintenance child: настоящие migrations/Admin, effects выключены."""
    from .runtime import configure, maintenance, create_admin, close_pools
    configure()
    try:
        for action in ('migrate', 'initialize', 'collectstatic'):
            maintenance(action)
        from .bootstrap import private_bytes
        create_admin('admin', private_bytes(Path(directory)/'admin-password').decode().strip())
        from .models import Installation
        Installation.objects.update(effects_enabled=False)
    finally:
        close_pools()


def serve_preview(directory, port):
    """Новый web child, без переключения maintenance settings в живом процессе."""
    from .runtime import configure, serve
    configure()
    return serve('127.0.0.1', port, hostnames=('localhost', '127.0.0.1'),
                 tls_cert=str(Path(directory)/'server.crt'), tls_key=str(Path(directory)/'server.key'))


@contextmanager
def running(plan):
    import psycopg
    from psycopg import sql
    # При гонке за свободный порт docker откажет, а не перезапустит чужой сервис.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        database_port = probe.getsockname()[1]
    values = prepare_files(plan, database_port=database_port)
    root = plan.directory
    name = 'mop-admin-preview-'+uuid4().hex
    child = None
    started = False
    log = (root/'server.log').open('x')
    os.chmod(root/'server.log', 0o600)
    try:
        endpoint = subprocess.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
                                  text=True, capture_output=True, check=True).stdout.strip()
        if os.environ.get('DOCKER_HOST') or not endpoint.startswith('unix://'):
            raise RuntimeError('local Unix Docker context required for admin preview')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
            '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
            '-keyout', str(root/'server.key'), '-out', str(root/'server.crt')],
            check=True, stdout=log, stderr=log)
        os.chmod(root/'server.key', 0o600)
        subprocess.run(['docker', 'run', '--detach', '--rm', '--name', name,
            '--label', 'mop.admin-preview=true', '--env-file', str(root/'postgres.env'),
            '--publish', f'127.0.0.1:{database_port}:5432', 'postgres:17-bookworm'],
            check=True, stdout=log, stderr=log)
        started = True
        limit = time.monotonic()+45
        while True:
            try:
                connection = psycopg.connect(host='127.0.0.1', port=database_port, dbname='preview',
                    user='preview_maintenance', password=values.maintenance, connect_timeout=1, autocommit=True)
                break
            except psycopg.OperationalError:
                if time.monotonic() >= limit:
                    raise RuntimeError('owned preview PostgreSQL did not become ready') from None
                time.sleep(.2)
        with connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE ROLE preview_web LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE').format(
                sql.Literal(values.web)))
            cursor.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
            cursor.execute('GRANT USAGE ON SCHEMA public TO preview_web')
            cursor.execute('ALTER DEFAULT PRIVILEGES FOR ROLE preview_maintenance IN SCHEMA public '
                           'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO preview_web')
            cursor.execute('ALTER DEFAULT PRIVILEGES FOR ROLE preview_maintenance IN SCHEMA public '
                           'GRANT USAGE, SELECT ON SEQUENCES TO preview_web')
        subprocess.run([sys.executable, '-c', 'from mop.server.controller.preview import initialize_preview; '
            'import sys; initialize_preview(sys.argv[1])', str(root)], check=True,
            env=_environment(root/'maintenance.json'), stdout=log, stderr=log)
        child = subprocess.Popen([sys.executable, '-c', 'from mop.server.controller.preview import serve_preview; '
            'import sys; serve_preview(sys.argv[1], int(sys.argv[2]))', str(root), str(plan.port)],
            env=_environment(root/'web.json'), stdout=log, stderr=log)
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context(cafile=str(root/'server.crt'))))
        limit = time.monotonic()+30
        while True:
            try:
                with opener.open(plan.url, timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                if child.poll() is not None or time.monotonic() >= limit:
                    raise RuntimeError('preview Admin did not become ready; inspect its private server.log') from None
                time.sleep(.2)
        yield child
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if started:
            subprocess.run(['docker', 'rm', '--force', name], stdout=log, stderr=log, check=True)
        log.close()
