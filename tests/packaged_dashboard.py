"""Упакованный дашборд отдаётся контроллером из пакета: #627/#644.

HYPOTHESIS: установленный пакет mop (PEP 517 wheel + extra server) не везёт
web/dist и не отдаёт дашборд: контроллер держит только /api/v1 и standard
Admin, страница React-приложения живёт в web/dist рабочей копии, которой на
сервере нет. STATUS ниже фиксирует красное до fix.
"""
import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

import _lib  # noqa: F401 -- lifetime временных файлов
import hermetic  # noqa: F401 -- до mop и чтения настроек

ROOT = Path(__file__).resolve().parents[1]

if importlib.util.find_spec('django') is None:
    hermetic.skip('packaged dashboard', 'declared server extra is not installed')
    raise SystemExit(0)

CHILD = r'''
import json, os, subprocess, sys
from pathlib import Path

site = Path(sys.argv[1])          # site-packages установленного пакета
root = Path(sys.argv[2])          # домашний каталог ребёнка
port = int(sys.argv[3])

# Контроллер обязан быть пакетом: репозитория рядом нет.
assert (site / 'mop' / 'server' / 'controller' / 'asgi.py').is_file(), \
    'controller must come from the installed package'
assert not (root / 'web').exists() and not (root / 'mop').exists(), \
    'no repository checkout may sit next to the packaged controller'

import secrets
(root / 'django.key').write_text('test-only-signing-key-' + secrets.token_hex(16))
(root / 'django.key').chmod(0o600)
generated = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048',
    '-nodes', '-days', '1', '-subj', '/CN=localhost',
    '-addext', 'subjectAltName=DNS:localhost',
    '-keyout', str(root / 'server.key'), '-out', str(root / 'server.crt')],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
assert generated.returncode == 0, 'isolated TLS certificate generation failed'
(root / 'server.key').chmod(0o600)
db = json.loads(os.environ['MOP_627_DASHBOARD_DB'])
web = json.loads(os.environ['MOP_627_DASHBOARD_WEB_DB'])
base = {'installation_id': 'packaged-627', 'django_key': str(root / 'django.key')}
# Один root.key на весь класс (#правка фикстуры): initialize записывает его
# digest в доверие установки, случайный на каждого ребёнка ломал вторую
# попытку. maintenance оставляет доверенный ему доступ, web получает
# отдельного непривилегированного владельца-не-владельца схемы (#644).
(root / 'maintenance.json').write_text(json.dumps({**base, 'role': 'maintenance',
    'root_key': str(root / 'root.key'), 'database': db}))
(root / 'maintenance.json').chmod(0o600)
(root / 'web.json').write_text(json.dumps({**base, 'role': 'web', 'database': web}))
(root / 'web.json').chmod(0o600)

os.environ['MOP_CONTROLLER_BOOTSTRAP'] = str(root / 'maintenance.json')
from mop.server.controller.runtime import configure, maintenance, close_pools
configure()
maintenance('migrate')
maintenance('initialize')
close_pools()

os.environ['MOP_CONTROLLER_BOOTSTRAP'] = str(root / 'web.json')
from django.conf import settings
# Второй bootstrap меняет только файл; настройки читаются один раз, роль
# процесса переопределяет тест -- как это делает развёрнутый сервис.
settings.BOOTSTRAP['database'] = web
settings.BOOTSTRAP['role'] = 'web'
settings.DATABASES['default'].update(web)
settings.ALLOWED_HOSTS = ['localhost', '127.0.0.1']
import uvicorn
uvicorn.Server(uvicorn.Config('mop.server.controller.asgi:application',
    host='127.0.0.1', port=port, lifespan='on', log_level='error',
    ssl_certfile=str(root / 'server.crt'), ssl_keyfile=str(root / 'server.key'),
    timeout_graceful_shutdown=10)).run()
'''


def fresh_venv(directory):
    """Чистый venv на системном Python; проверка -- про пакет, не про клоны."""
    home = Path(directory) / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', str(home)], check=True)
    return home


def wait_port(port, host='127.0.0.1', timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with socket.socket() as probe:
            probe.settimeout(.5)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(.25)
    return False


def request(port, method, path, *, headers=None, timeout=15):
    """Настоящий HTTPS запрос к упакованному контроллеру."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection('127.0.0.1', port, timeout=timeout, context=context)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()


class PackagedDashboard(unittest.TestCase):
    """Один настоящий процесс ASGI из пакета на настоящем PostgreSQL."""

    @classmethod
    def setUpClass(cls):
        host = hermetic.FIXTURE_ENV.get('MOP_TEST_POSTGRES_HOST')
        if not host:
            hermetic.skip('packaged dashboard', 'isolated CI fixture is required')
            raise unittest.SkipTest('isolated fixture is not configured')
        import psycopg
        cls.database = 'mop_packaged_' + os.urandom(4).hex()
        # Runtime principal (#644): web не владеет схемой и не имеет DDL.
        # Провижинит родитель, один на класс: свежий профиль теста.
        cls.web_role = 'mop_packaged_web_' + os.urandom(4).hex()
        cls.web_password = 'runtime-only-' + secrets.token_hex(12)
        admin_kwargs = dict(host=host, port='5432', user='mop_ci',
                            password='isolated-test-only', dbname='mop_ci', autocommit=True)
        with psycopg.connect(**admin_kwargs) as admin, admin.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE "{cls.database}"')
            cursor.execute(f'''CREATE ROLE "{cls.web_role}" LOGIN
                PASSWORD '{cls.web_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE''')
        with psycopg.connect(dbname=cls.database, **{k: v for k, v in admin_kwargs.items()
                                                    if k != 'dbname'}) as admin, admin.cursor() as cursor:
            # Только DML, без CREATE на схему и без DDL (#644): иначе
            # controller.E004 справедливо отказывает runtime-принципалу.
            # Права по умолчанию цепляют таблицы, созданные mop_ci позже.
            cursor.execute(f'GRANT CONNECT ON DATABASE "{cls.database}" TO "{cls.web_role}"')
            cursor.execute(f'GRANT USAGE ON SCHEMA public TO "{cls.web_role}"')
            cursor.execute(f'''ALTER DEFAULT PRIVILEGES FOR ROLE mop_ci IN SCHEMA public
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{cls.web_role}"''')
            cursor.execute(f'''ALTER DEFAULT PRIVILEGES FOR ROLE mop_ci IN SCHEMA public
                GRANT USAGE, SELECT ON SEQUENCES TO "{cls.web_role}"''')
        cls.wheelhouse = Path(tempfile.mkdtemp(prefix='mop-627-wheels-'))
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--quiet',
                        '--disable-pip-version-check', '--wheel-dir', str(cls.wheelhouse),
                        str(ROOT) + '[server]'], check=True)
        cls.venv = fresh_venv(tempfile.mkdtemp(prefix='mop-627-venv-'))
        cls.python = cls.venv / 'bin' / 'python'
        cls.site = (cls.venv / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}'
                    / 'site-packages')
        subprocess.run([str(cls.python), '-m', 'pip', 'install', '--quiet',
                        '--disable-pip-version-check', '--no-index',
                        '--find-links', str(cls.wheelhouse), 'mop[server]'],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        cls.root = Path(tempfile.mkdtemp(prefix='mop-627-child-'))
        # Один доверенный root.key на класс: initialize пишет его digest.
        (cls.root / 'root.key').write_bytes(os.urandom(64))
        (cls.root / 'root.key').chmod(0o600)
        (cls.root / 'server.py').write_text(CHILD)
        cls._child = None

    @classmethod
    def tearDownClass(cls):
        if cls._child is not None:
            cls._child.terminate()
            cls._child.wait(15)
        import psycopg
        host = hermetic.FIXTURE_ENV.get('MOP_TEST_POSTGRES_HOST')
        if host:
            with contextlib.suppress(Exception):
                with psycopg.connect(host=host, port='5432', user='mop_ci',
                                     password='isolated-test-only', dbname='mop_ci',
                                     autocommit=True) as admin, admin.cursor() as cursor:
                    cursor.execute(f'DROP DATABASE IF EXISTS "{cls.database}"')
                    cursor.execute(f'DROP ROLE IF EXISTS "{cls.web_role}"')

    @contextlib.contextmanager
    def running(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        host = hermetic.FIXTURE_ENV['MOP_TEST_POSTGRES_HOST']
        cls = type(self)

        def database(user, password):
            return {'ENGINE': 'django.db.backends.postgresql', 'NAME': cls.database,
                    'HOST': host, 'PORT': '5432', 'USER': user, 'PASSWORD': password,
                    'OPTIONS': {'sslmode': 'disable'}}

        os.environ['MOP_627_DASHBOARD_DB'] = json.dumps(database('mop_ci', 'isolated-test-only'))
        os.environ['MOP_627_DASHBOARD_WEB_DB'] = json.dumps(database(cls.web_role, cls.web_password))
        child = subprocess.Popen([str(cls.python), '-I', str(cls.root / 'server.py'),
                                  str(cls.site), str(cls.root), str(port)],
                                 cwd=cls.root, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        type(self)._child = child
        try:
            self.assertTrue(wait_port(port), 'packaged controller did not start:\n'
                           + (child.stdout.read() if child.poll() is not None else ''))
            yield port
        finally:
            child.terminate()
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            type(self)._child = None

    def committed(self, *parts):
        return Path(ROOT, 'web', 'dist', *parts)

    def committed_files(self):
        dist = self.committed()
        index = (dist / 'index.html').read_bytes()
        assets = sorted(p.name for p in (dist / 'assets').iterdir() if p.is_file())
        return index, assets

    # ── HYPOTHESIS: wheel не везёт web/dist; контроллер его не отдаёт ──
    def test_committed_dashboard_ships_in_the_wheel(self):
        import tomllib
        config = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        patterns = config['tool']['setuptools']['package-data'].get('mop.server.controller', [])
        self.assertTrue(any('dist' in pattern for pattern in patterns),
                        'the controller package must declare the dashboard dist')
        wheel = next(zipfile.ZipFile(w) for w in self.wheelhouse.glob('mop-*.whl'))
        names = wheel.namelist()
        index = next(n for n in names if n.endswith('dist/index.html'))
        self.assertEqual(wheel.read(index),
                         self.committed('index.html').read_bytes())
        for name in self.committed('assets').iterdir():
            self.assertIn(f'mop/server/controller/dist/assets/{name.name}', names, name.name)

    def test_index_and_hashed_assets_served_same_origin(self):
        index, assets = self.committed_files()
        with self.running() as port:
            status, headers, body = request(port, 'GET', '/')
            self.assertEqual(status, 200)
            self.assertEqual(headers.get('content-type'), 'text/html; charset=utf-8')
            self.assertEqual(body, index)
            self.assertEqual(headers.get('cache-control'), 'no-store')
            for name in assets:
                status, headers, body = request(port, 'GET', '/assets/' + name)
                self.assertEqual(status, 200, name)
                self.assertEqual(body, self.committed('assets', name).read_bytes(), name)
                control = headers.get('cache-control', '')
                self.assertIn('max-age=31536000', control, name)
                self.assertIn('immutable', control, name)

    def test_asset_mime_types_match_the_served_suffix(self):
        with self.running() as port:
            for path, expected in (('index-BNo5kAFU.js', 'text/javascript'),
                                   ('index-N7aBNmCs.css', 'text/css')):
                status, headers, _ = request(port, 'GET', '/assets/' + path)
                self.assertEqual(status, 200, path)
                self.assertTrue((headers.get('content-type') or '').startswith(expected), path)

    def test_traversal_and_unknown_api_stay_refused(self):
        index, _ = self.committed_files()
        with self.running() as port:
            for path in ('/assets/../bootstrap.json', '/assets/%2e%2e/bootstrap.json',
                         '/assets/no-such.js', '/assets/../../etc/passwd'):
                status, _, _ = request(port, 'GET', path)
                self.assertIn(status, (400, 404), path)
            # Неизвестный путь API -- 404 от маршрутизатора, а не страница
            # приложения: дашборд не проглатывает чужое пространство имён.
            status, _, body = request(port, 'GET', '/api/v1/nope')
            self.assertEqual(status, 404)
            self.assertNotEqual(body, index)
            status, _, _ = request(port, 'GET', '/nonexistent-page')
            self.assertEqual(status, 404)

    def test_get_head_and_cleanup_contract(self):
        with self.running() as port:
            status, headers, body = request(port, 'HEAD', '/')
            self.assertEqual(status, 200)
            self.assertEqual(headers.get('cache-control'), 'no-store')
            self.assertEqual(len(body), 0)
            status, _, _ = request(port, 'GET', '/nonexistent-page')
            self.assertEqual(status, 404)

    def test_no_embedded_secrets_and_auth_endpoints_unchanged(self):
        wheel = next(zipfile.ZipFile(w) for w in self.wheelhouse.glob('mop-*.whl'))
        blob = b''.join(wheel.read(n) for n in wheel.namelist()
                        if n.endswith(('dist/index.html', '.js', '.css'))
                        and '/dist/assets/' in n)
        for secret in (b'isolated-test-only', b'django.key', b'bootstrap.json'):
            self.assertNotIn(secret, blob)
        with self.running() as port:
            status, headers, body = request(port, 'GET', '/api/v1/auth/csrf')
            self.assertEqual(status, 200)
            self.assertTrue((headers.get('set-cookie') or '').startswith('csrftoken='), headers)
            # Health остаётся защищённым DRF-запросом (#контракт контроллера):
            # анониму -- 401 с вызовом Token, не анонимный успех.
            status, headers, body = request(port, 'GET', '/api/v1/health')
            self.assertEqual(status, 401)
            self.assertEqual(headers.get('www-authenticate'), 'Token')
            self.assertEqual(json.loads(body)['error']['code'], 'authentication_required')

    def test_client_wheel_stays_free_of_server_imports(self):
        home = fresh_venv(tempfile.mkdtemp(prefix='mop-627-client-'))
        python = home / 'bin' / 'python'
        subprocess.run([str(python), '-m', 'pip', 'install', '--quiet',
                        '--disable-pip-version-check', '--no-index',
                        '--find-links', str(self.wheelhouse), 'mop'],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        probe = ("import importlib.util; import mop.cli; "
                 "assert importlib.util.find_spec('django') is None; "
                 "assert importlib.util.find_spec('channels') is None; print('client-clean')")
        result = subprocess.run([str(python), '-I', '-c', probe],
                                capture_output=True, text=True, cwd=tempfile.gettempdir())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'client-clean')


if __name__ == '__main__':
    unittest.main()
