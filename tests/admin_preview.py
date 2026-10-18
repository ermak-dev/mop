"""Изолированный local Admin preview (#712), без установки/пула.

HYPOTHESIS: для долгоживущей админки нет управляемого preview-входа.
SPEC: loopback HTTPS, новый private каталог, пароли не в argv/выводе,
maintenance и web разные bootstrap, web не получает root key.
SOLUTION: новый mop server preview, отдельные DockerPG17 и runtime principal.
RESULT: RED отсутствующей композиции; GREEN pure paths/private/boundaries и
живой HTTPS login, Actor/Project lists, Admin CSS200 в owned preview.
STATUS: FIXED — see #712
"""
import importlib.util
from pathlib import Path
import tempfile
import hermetic  # noqa: F401
from _lib import Checks


def main():
    c = Checks()
    if not c.check('preview composition exists', importlib.util.find_spec('mop.server.controller.preview') is not None):
        return c.report('admin_preview')
    from mop.server.controller.preview import PreviewPlan, prepare_files
    for directory, port in ((Path('/'), 18080), (Path('relative'), 18080), (Path('/tmp/../bad'), 18080),
                            (Path('/tmp/preview'), True), (Path('/tmp/preview'), 0),
                            (Path('/tmp/preview'), 65536)):
        try:
            PreviewPlan(directory, port)
            c.fail('invalid preview path/port refuses', (str(directory), port))
        except ValueError:
            c.check('invalid preview path/port refuses', True)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)/'admin'
        plan = PreviewPlan(root, 18080)
        c.expect('only loopback HTTPS is advertised', plan.url, 'https://localhost:18080/admin/')
        values = prepare_files(plan, database_port=55432)
        import json
        maintenance = json.loads((root/'maintenance.json').read_text())
        web = json.loads((root/'web.json').read_text())
        c.expect('web is a separate database principal', web['database']['USER'], 'preview_web')
        c.check('maintenance alone receives root', 'root_key' in maintenance and 'root_key' not in web)
        c.check('private directory', root.stat().st_mode & 0o777 == 0o700)
        for name in ('maintenance.json', 'web.json', 'root.key', 'django.key', 'admin-password'):
            c.check('private file '+name, (root/name).stat().st_mode & 0o777 == 0o600)
        password = (root/'admin-password').read_text().strip()
        c.check('password never appears in diagnostics', password not in repr(values))
        try:
            prepare_files(plan, database_port=55432)
            c.fail('existing preview is not overwritten')
        except FileExistsError:
            c.check('existing preview is not overwritten', True)
    return c.report('admin_preview')


if __name__ == '__main__':
    raise SystemExit(main())
