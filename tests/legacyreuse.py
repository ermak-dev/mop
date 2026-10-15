"""Новая legacy Meta не наследует отозванную роль старого имени (#658).

HYPOTHESIS: bootstrap считает архив revoked bindings текущей ролью.
SOLUTION: подтверждённая legacy Meta смотрит только неотозванные привязки;
пустая/частичная ролевая Meta остаётся fail-closed.
STATUS: FIXED — see #658.
"""
import os
import tempfile

import hermetic
from _lib import Checks, patched
from mop.common import config, soul
from mop.common.domain import JobMeta
from mop.server import bootstrap, grants


def main():
    c = Checks()
    origin = 'git@example:team/example.git'
    name = 'pu-example-1'
    bundle = soul.load(os.path.join(config.PROJECT, 'mop', 'skills', 'puppet'))
    with tempfile.TemporaryDirectory(prefix='mop-legacy-reuse-658-') as root:
        rec = grants.prepare('example', bundle.role, bundle.digest,
                             bundle.manifest.to_dict(), bundle.prompt, 'anton',
                             'req-legacy-reuse', name, soul.incarnation(), 'fixture', root=root)
        calls = []
        def play(req, project, **kwargs):
            calls.append((req, kwargs))
            return {'ok': True, 'rc': 0}
        common = dict(ROOT=root, _soul_root=lambda: root, play=play,
                      puppet_creds=lambda project: {'user': 'puppet-example', 'password': 'fixture-private'})
        request = {'verb': 'bootstrap', 'name': name, 'address': '192.0.2.1'}
        legacy = JobMeta(origin, None)
        with patched(bootstrap, _job_meta=lambda name: legacy, **common):
            pending = bootstrap.answer('example', request)
            grants.activate(rec['principal'], root=root)
            active = bootstrap.answer('example', request)
        c.check('#658 pending role cannot downgrade through legacy Meta',
                bool(pending.get('error')) and not calls, pending)
        c.check('#658 active role cannot downgrade through legacy Meta',
                bool(active.get('error')) and not calls, active)
        grants.revoke(rec['principal'], root=root)
        with patched(bootstrap, _job_meta=lambda name: legacy, **common):
            reused = bootstrap.answer('example', request)
        c.check('#658 a new registered legacy job can reuse a revoked role name',
                reused.get('ok') is True and len(calls) == 1
                and reused.get('bus', {}).get('user') == 'puppet-example'
                and 'soul' not in reused, reused)
        c.check('#658 historical binding is retained for fail-closed readers',
                grants.binding_named('example', name, root=root) is True
                and grants.binding(rec['principal'], root=root)['revoked_at'])
        before = len(calls)
        for fields in ({'origin': None}, {'soul': 'puppet'},
                       {'soul_digest': bundle.digest}, {'soul_inc': rec['soul_inc']}):
            def job_meta(name):
                return JobMeta(**{'origin': origin, 'branch': None, **fields})
            with patched(bootstrap, _job_meta=job_meta, **common):
                denied = bootstrap.answer('example', request)
            c.check('#658 missing or partially role Meta never gets legacy credentials',
                    bool(denied.get('error')) and 'bus' not in denied
                    and len(calls) == before, denied)
    return c.report('legacyreuse')


if __name__ == '__main__':
    raise SystemExit(main())
