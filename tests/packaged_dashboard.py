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
from unittest import mock
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
# #695: собранная статика -- отдельный каталог, общий ролям (пишет
# maintenance штатным collectstatic, раздаёт web), путь -- в bootstrap.
base['static_root'] = str(root / 'static')
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
maintenance('collectstatic')
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


def site_packages(home):
    return home / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'


def venv_env():
    """Окружение каждого процесса внутри venv проверки (#700): без PYTHONPATH
    и PYTHONHOME. Клон на PYTHONPATH (его экспортирует bin/mop) подменял
    установку: pip в venv находил mop уже установленным в клоне и не ставил
    ничего. venv сам чистит эти две переменные для своего ensurepip
    (CPython gh-98251, EnvBuilder._call_new_python)."""
    return {key: value for key, value in os.environ.items()
            if key not in ('PYTHONPATH', 'PYTHONHOME')}


def install(python, wheelhouse, requirement):
    """pip install из wheelhouse проверки в её venv; вывод pip -- в отказ."""
    result = subprocess.run([str(python), '-m', 'pip', 'install',
                             '--disable-pip-version-check', '--no-index',
                             '--find-links', str(wheelhouse), requirement],
                            env=venv_env(), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    if result.returncode:
        raise AssertionError(f'pip install {requirement} failed:\n{result.stdout}')
    return result.stdout


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
        cls.site = site_packages(cls.venv)
        install(cls.python, cls.wheelhouse, 'mop[server]')
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
                                 cwd=cls.root, env=venv_env(), stdout=subprocess.PIPE,
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
        # Имена берутся из собранного dist: хеш в имени меняется с каждой
        # сборкой, захардкоженное имя молча ломает проверку на resync.
        mime = {'.js': 'text/javascript', '.css': 'text/css'}
        names = {name.suffix: name.name for name in self.committed('assets').iterdir()}
        with self.running() as port:
            for suffix, expected in mime.items():
                path = names[suffix]
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

    # #695: стандартная отдача статики вместо самописного файлового сервера.
    # RED: bootstrap отвергал static_root, collectstatic не было, Admin CSS
    # не раздавался; самописная view ассетов с таблицей MIME на месте.
    # SOLUTION: WhiteNoise (WHITENOISE_ROOT = пакетный dist, явный immutable на
    # /assets/), штатный collectstatic в общий каталог из bootstrap.
    # RESULT: 10/10 на реальном wheel под uvicorn (настоящий ASGI-стек).
    # STATUS: FIXED — see #695
    def test_dashboard_responses_carry_nosniff(self):
        _, assets = self.committed_files()
        with self.running() as port:
            for path in ('/', '/assets/' + assets[0]):
                status, headers, _ = request(port, 'GET', path)
                self.assertEqual(status, 200, path)
                self.assertEqual(headers.get('x-content-type-options'), 'nosniff', path)

    def test_admin_static_is_collected_and_served_immutable(self):
        import re
        with self.running() as port:
            status, _, body = request(port, 'GET', '/admin/login/')
            self.assertEqual(status, 200)
            link = re.search(rb'/static/admin/css/base\.[0-9a-f]{12}\.css', body)
            self.assertIsNotNone(link, 'admin pages must link manifest-hashed collected static')
            status, headers, css = request(port, 'GET', link.group(0).decode())
            self.assertEqual(status, 200)
            self.assertTrue(css, 'collected admin stylesheet must not be empty')
            self.assertIn('immutable', headers.get('cache-control', ''))

    def test_custom_asset_file_server_is_gone(self):
        source = (ROOT / 'mop' / 'server' / 'controller' / 'dashboard.py').read_text()
        self.assertNotIn('CONTENT_TYPES', source, 'MIME table belongs to the static server, not the app')
        self.assertNotIn('def asset(', source, 'hashed assets are served by the standard static server')

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
        install(python, self.wheelhouse, 'mop')
        probe = ("import importlib.util; import mop.cli; "
                 "assert importlib.util.find_spec('django') is None; "
                 "assert importlib.util.find_spec('channels') is None; print('client-clean')")
        result = subprocess.run([str(python), '-I', '-c', probe], env=venv_env(),
                                capture_output=True, text=True, cwd=tempfile.gettempdir())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'client-clean')

    # #700: полный CI (bin/mop dev test -v) красил 8 из 10 проверок класса --
    # «controller must come from the installed package» и в клиентском venv
    # «No module named 'mop'», а `mop dev controller --check dashboard` на том
    # же HEAD был зелёным.
    # HYPOTHESIS: bin/mop экспортирует PYTHONPATH=<клон>, mop dev test отдаёт
    # окружение файлу проверки, и pip внутри чистого venv его наследует; SETUP
    # CI (pip wheel '.[server]') и pip wheel этого класса оставляют в клоне
    # mop.egg-info. pip видит mop уже установленным в клоне («Requirement
    # already satisfied: mop in /io») и в venv не ставит ничего. Изолированный
    # --check dashboard звал файл без PYTHONPATH -- поэтому зелёный.
    # Клон здесь подложен явно: проверка ловит дефект при любом способе
    # запуска, не только из bin/mop.
    # SOLUTION: каждый процесс внутри venv проверки (pip install, ребёнок-
    # контроллер, клиентская проба) получает venv_env() -- окружение без
    # PYTHONPATH/PYTHONHOME, как ensurepip у самого venv.
    # RESULT: до правки прогон как в CI (bin/mop dev test -v packaged_dashboard
    # в fixture dev controller) -- FAILED (failures=8) за 435 с, те же две
    # причины; изолированный --check dashboard с этой проверкой -- 1 провал,
    # «Requirement already satisfied: mop in <подложенный клон>». После
    # правки оба пути -- 11 из 11; controller_install и control_tls под тем
    # же PYTHONPATH зелёные и до правки (установка по пути и --target pip
    # всегда ставят заново).
    # STATUS: FIXED — see #700
    def test_venv_install_ignores_a_checkout_on_pythonpath(self):
        checkout = Path(tempfile.mkdtemp(prefix='mop-700-checkout-'))
        (checkout / 'mop.egg-info').mkdir()
        (checkout / 'mop.egg-info' / 'PKG-INFO').write_text(
            'Metadata-Version: 2.1\nName: mop\nVersion: 0.1.0\n')
        home = fresh_venv(tempfile.mkdtemp(prefix='mop-700-venv-'))
        python, site = home / 'bin' / 'python', site_packages(home)
        with mock.patch.dict(os.environ, {'PYTHONPATH': str(checkout)}):
            output = install(python, self.wheelhouse, 'mop')
        self.assertTrue((site / 'mop' / '__init__.py').is_file(),
                        'a checkout on PYTHONPATH must not stand in for the venv install:\n'
                        + output)
        located = subprocess.run([str(python), '-I', '-c', 'import mop; print(mop.__file__)'],
                                 env=venv_env(), capture_output=True, text=True,
                                 cwd=tempfile.gettempdir())
        self.assertEqual(located.returncode, 0, located.stderr)
        self.assertEqual(Path(located.stdout.strip()), site / 'mop' / '__init__.py')


if __name__ == '__main__':
    unittest.main()
