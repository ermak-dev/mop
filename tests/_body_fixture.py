#!/usr/bin/env python3
"""Общие fixture'ы запуска тела (#675): work/bundle/wire/binding.

Не проверка: `mop dev test` и hermetic пропускают `_*.py`. До #675
work/bundle жили в body_launch, wire -- в http_execution, binding -- в
review_http_execution, и проверка импортировала проверку (запрещено
tests_lib, #269). Хелпер не импортирует ни одну проверку.
"""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import _lib  # noqa: F401 -- общий временный корень процесса (#338)
import hermetic  # noqa: F401
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mop.common import soul
from mop.node.http_execution import Work
from mop.node.execution_adapters import BoundTarget


def api():
    spec = importlib.util.find_spec('mop.driver.launch')
    assert spec is not None, 'addressed body launch boundary is missing'
    from mop.driver import launch
    return launch


WORK_KEYS = ('epoch', 'operation_id', 'step_id', 'target_id', 'incarnation_id', 'node_id',
             'executor_epoch', 'grant_id')


def work():
    return Work.parse(dict(schema_version=1, **{key: str(uuid4()) for key in
        ('epoch', 'operation_id', 'step_id', 'target_id', 'incarnation_id', 'node_id',
         'executor_epoch', 'grant_id')}, owner_id='1', generation='2', revision='3',
        role='worker', bundle_digest='a'*64, action='apply_identity',
        not_after=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()))


def bundle():
    return soul.from_manifest(dict(version=1, soul='worker',
        prompt='prompt.md', runtime=dict(driver='any', receives_dispatch=True),
        capabilities=dict(node_verbs=[], cluster_verbs=[], dispatch=False, spawn=[]),
        secrets=dict(files=[], vars=[])), b'approved prompt\n')

def wire(**changes):
    value = dict(schema_version=1, **{k: str(uuid4()) for k in WORK_KEYS},
        owner_id='1', generation='1', revision='1',
        role='worker', bundle_digest='a'*64, action='send_message',
        not_after=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat())
    value.update(changes)
    return value


def binding(work, root):
    return BoundTarget(work.target_id, work.incarnation_id, work.node_id,
        work.executor_epoch, work.generation, work.role, work.bundle_digest,
        work.grant_id, root/'repo', root/'tmux', '%0', root/'launcher')
