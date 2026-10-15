"""Отдельное право dispatch не является node_verbs (#657).

HYPOTHESIS: resolve проверяет dispatch только по списку обычных глаголов.
SOLUTION: отдельный node dispatch использует capabilities.dispatch.
STATUS: FIXED — see #657.
"""
import os
import tempfile

import hermetic
from _lib import Checks
from mop.common import config, soul
from mop.server import grants


def main():
    c = Checks()
    with tempfile.TemporaryDirectory(prefix='mop-dispatch-resolve-657-') as root:
        records = {}
        for number, role in enumerate(('master', 'puppet', 'builder'), 1):
            bundle = soul.load(os.path.join(config.PROJECT, 'mop', 'skills', role))
            row = grants.prepare('example', role, bundle.digest,
                                 bundle.manifest.to_dict(), bundle.prompt,
                                 'anton', f'request-{number}', f'pu-example-{number}',
                                 soul.incarnation(), f'intent-{number}', root=root)
            records[role] = grants.activate(row['principal'], root=root)
        master = records['master']['principal']
        got = grants.resolve(master, 'node', 'dispatch', target='pu-example-2', root=root)
        c.check('#657 explicit dispatch permission authorizes its dedicated node operation',
                got.get('allow') is True, got)
        c.check('#657 dispatch is not promoted to an ordinary node verb',
                'dispatch' not in got['capabilities']['node_verbs'])
        c.check('#657 dispatch reply retains the independently resolved target',
                got.get('source_dispatch') is True and got['target']['found'] is True
                and got['target']['receives_dispatch'] is True)
        target_master = grants.resolve(master, 'node', 'dispatch', target='pu-example-1', root=root)
        c.check('#657 receiving policy remains explicit rather than granting every target',
                target_master['target']['receives_dispatch'] is False)
        c.check('#657 cluster dispatch has no implied permission',
                grants.resolve(master, 'cluster', 'dispatch', root=root)['allow'] is False)
        for role in ('puppet', 'builder'):
            denied = grants.resolve(records[role]['principal'], 'node', 'dispatch',
                                    target='pu-example-2', root=root)
            c.check('#657 non-dispatching roles remain denied',
                    denied['allow'] is False and bool(denied['reason']), denied)
        c.check('#657 ordinary node privileges are unchanged',
                grants.resolve(master, 'node', 'tail', root=root)['allow'] is True
                and grants.resolve(master, 'node', 'disk', root=root)['allow'] is False)
        grants.revoke(master, root=root)
        denied = grants.resolve(master, 'node', 'dispatch', target='pu-example-2', root=root)
        c.check('#657 revoked dispatch source stays denied',
                denied['allow'] is False and 'revoked' in denied['reason'])
    return c.report('dispatchresolve')


if __name__ == '__main__':
    raise SystemExit(main())
