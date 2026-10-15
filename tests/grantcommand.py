"""Адресный операторский отзыв без поиска по имени папета (#655).

HYPOTHESIS: библиотечный revoke недоступен оператору через штатный CLI.
SOLUTION: controller-only commandlet отзывает ровно указанный principal.
STATUS: FIXED — see #655.
"""
import importlib
import json
import tempfile
from pathlib import Path

import hermetic
from _lib import Checks, patched, run_command
from mop.server import grants


def main():
    c = Checks()
    try:
        command = importlib.import_module('mop.cli.server.grant')
    except ModuleNotFoundError:
        c.check('#655 exact-principal revocation has an operator command', False)
        return c.report('grantcommand')
    principal = 'grant-' + 'a' * 32
    sibling = 'grant-' + 'b' * 32
    with tempfile.TemporaryDirectory(prefix='mop-grant-command-655-') as root:
        path = Path(root, 'registry.json')
        records = {p: {'principal': p, 'job': 'pu-example-1',
                      'project': project, 'active': True}
                   for p, project in ((principal, 'admin'), (sibling, 'example'))}
        path.write_text(json.dumps(records))
        password = Path(root, 'nats-' + principal + '.pass')
        password.write_text('fixture-private')
        guards = []
        def require():
            guards.append(True)
        with patched(grants, _root_dir=lambda value: root), \
             patched(command, require_controller=require):
            out, err, code = run_command(command.main, ['revoke', principal], via_cli=True)
            got = json.loads(path.read_text())
            c.check('#655 revoke succeeds quietly', code == 0 and not out and not err,
                    (out, err, code))
            c.check('#655 exact principal is revoked even in an obsolete project',
                    not got[principal]['active'] and bool(got[principal]['revoked_at']))
            c.expect('#655 same job name never selects another incarnation',
                     got[sibling], records[sibling])
            c.check('#655 verb revocation does not cut transport implicitly', password.exists())
            first = path.read_bytes()
            run_command(command.main, ['revoke', principal], via_cli=True)
            c.expect('#655 repeated revoke is idempotent', path.read_bytes(), first)
            out, err, code = run_command(command.main, ['revoke', principal, '--cut'], via_cli=True)
            c.check('#655 explicit cut removes only the selected password',
                    code == 0 and not password.exists() and not out and not err)
            before = path.read_bytes()
            for args in (['revoke', 'pu-example-1'], ['revoke', 'grant-../private'],
                         ['revoke', principal, '--unknown'], ['cut', principal],
                         ['revoke', principal, sibling], []):
                out, err, code = run_command(command.main, args, via_cli=True)
                c.check('#655 malformed target or action refuses without mutation',
                        code != 0 and path.read_bytes() == before, args)
            out, err, code = run_command(command.main, ['revoke', 'grant-' + 'c' * 32], via_cli=True)
            c.check('#655 unknown principal refuses with a reason',
                    code != 0 and 'unknown principal' in str(err) and 'fixture-private' not in out + err)
        def denied():
            raise RuntimeError('controller fixture refused')
        with patched(grants, _root_dir=lambda value: root), \
             patched(command, require_controller=denied):
            before = path.read_bytes()
            out, err, code = run_command(command.main, ['revoke', sibling, '--cut'], via_cli=True)
            c.check('#655 controller guard runs before writes',
                    code != 0 and 'controller fixture refused' in err and path.read_bytes() == before)
        c.check('#655 successful requests require controller authorization', len(guards) >= 3)
    return c.report('grantcommand')


if __name__ == '__main__':
    raise SystemExit(main())
