"""Узкие adapter compositions; Root передаёт trusted clients, не request callbacks."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .contracts import ImageAvailability, StageResult
from .commands import validate_source
from .subprocess import TrustedInvocation


def confirmed(fact, build, node):
    now = datetime.now(timezone.utc)
    return (type(fact) is ImageAvailability and fact.node_id == node and fact.available is True
        and fact.source_digest == build.source_digest and type(fact.external_reference) is str and 0 < len(fact.external_reference) <= 512
        and type(fact.observed_at) is datetime and fact.observed_at.utcoffset() is not None
        and now-timedelta(seconds=10) <= fact.observed_at <= now)


class AnsibleImageBuilder:
    capabilities = frozenset({'image_builder','inspect'})

    def __init__(self, *, invocations, runner, availability, inspector, timeout=3600):
        if not 0 < timeout <= 7200:
            raise ValueError('bounded image build timeout required')
        self.invocations, self.runner, self.reader, self.inspector = invocations, runner, availability, inspector
        self.timeout = timeout

    def availability(self, build, node_id):
        return self.reader(build, node_id)

    def apply(self, ticket):
        if ticket.stage != 'build':
            raise ValueError('image builder accepts only the pinned build stage')
        # Per-node subprocess не становится отдельной очередью; общий stage
        # остаётся unresolved, если процесс умер между двумя гипервизорами.
        for node in ticket.build.target_nodes:
            build = ticket.build if len(ticket.build.target_nodes) == 1 else replace(ticket.build, target_nodes=(node,))
            deadline = getattr(build, 'not_after', None)
            timeout = self.timeout if deadline is None else min(self.timeout, (deadline-datetime.now(timezone.utc)).total_seconds())
            if timeout <= 0:
                return StageResult('failed', True, 'build execution deadline expired before subprocess start')
            with self.invocations(build) as invocation:
                result = self.runner.run(invocation, timeout=timeout)
            if not result.quiescent:
                return StageResult('unknown', False, result.reason)
            if result.outcome != 'succeeded':
                return self._confirmation(ticket, local_failure=result.reason)
        return self._confirmation(ticket)

    def _confirmation(self, ticket, *, local_failure=''):
        # Завершённый локальный ssh/Ansible процесс не доказывает остановку
        # удалённого pct/Ansible task. Даже rc=0 + image exists этого не заменяют.
        physical = self.inspect(ticket)
        prefix = local_failure+'; ' if local_failure else ''
        if physical.outcome == 'unknown' or not physical.quiescent:
            return StageResult('unknown', physical.quiescent, (prefix+physical.reason)[:512])
        if physical.outcome == 'failed':
            return StageResult('failed', True, (prefix+physical.reason)[:512])
        return StageResult('succeeded', True, (prefix+'physical image revision and external quiescence confirmed')[:512])

    def inspect(self, ticket):
        # Нужен не только image exists, но и stop/quiescence прежнего build process.
        result = self.inspector(ticket)
        if type(result) is not StageResult:
            raise ValueError('typed physical image inspection required')
        if result.outcome == 'succeeded' and not all(confirmed(self.reader(ticket.build, node), ticket.build, node) for node in ticket.build.target_nodes):
            return StageResult('unknown', result.quiescent, 'physical availability requires readback')
        return result


class PveAnsibleInvocations:
    """Генерирует только известный playbook, private vars и immutable SHA.

    inventory/playbook/settings/workspace — доверенные Root deployment references.
    Ничего из них не приходит через HTTP. Никакого config/.env или старого builder.
    """
    def __init__(self, *, executable, inventory, playbook, workspace, settings, node_names, environment=()):
        for path in (executable, inventory, playbook, workspace):
            if not os.path.isabs(path):
                raise ValueError('root-composed absolute image paths required')
        if not Path(workspace).is_dir() or Path(workspace).stat().st_mode & 0o077:
            raise ValueError('private image workspace required')
        self.executable, self.inventory, self.playbook, self.workspace = executable, inventory, playbook, workspace
        self.settings, self.node_names, self.environment = dict(settings), dict(node_names), tuple(environment)

    @contextmanager
    def __call__(self, build):
        validate_source(build.source_ref, build.source_commit)
        if (hashlib.sha256(build.manifest).hexdigest() != build.manifest_digest
                or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', build.project_name)
                or build.mode not in ('missing','update','rebuild')):
            raise ValueError('verified immutable image inputs required')
        import yaml
        content = yaml.safe_load(build.manifest)
        if (type(content) is not list or len(content) != 1 or type(content[0]) is not dict
                or type(content[0].get('vars', {})) is not dict or type(content[0].get('tasks', [])) is not list):
            raise ValueError('image manifest must contain one sandbox play')
        if not build.target_nodes or any(pk not in self.node_names for pk in build.target_nodes):
            raise ValueError('target node is outside privileged inventory enrollment')
        with tempfile.TemporaryDirectory(prefix='mop-image-', dir=self.workspace) as directory:
            root = Path(directory)
            def private(name, data):
                path = root/name
                with path.open('x') as stream:
                    os.chmod(path, 0o600)
                    json.dump(data, stream)
                return str(path)
            variables = content[0].get('vars', {})
            resource_keys = {'MOP_CORES','MOP_MEM_MB','MOP_DISK_GB'}
            if any(type(key) is not str or key.upper().startswith('MOP_') and key not in resource_keys for key in variables):
                raise ValueError('project manifest cannot override installation settings')
            asks = {key:value for key,value in variables.items() if key in resource_keys}
            variables = {key:value for key,value in variables.items() if key not in resource_keys}
            tasks = content[0].get('tasks', [])
            extra = dict(mop_project=build.project_name, mop_origin=build.origin, mop_project_asks=asks,
                mop_image_branch=build.source_ref, mop_image_commit=build.source_commit,
                mop_project_vars=private('project-vars.json', variables),
                mop_project_tasks=private('project-tasks.json', tasks))
            if build.mode == 'rebuild':
                extra['mop_fresh'] = True
            # Один per-node запуск; --limit не меняет ветвление playbook.
            if len(build.target_nodes) != 1:
                raise ValueError('PVE strategy requires a per-node invocation composition')
            extra['mop_node'] = self.node_names[build.target_nodes[0]]
            settings = private('settings.json', self.settings)
            project = private('image.json', extra)
            yield TrustedInvocation((self.executable, '-i', self.inventory, self.playbook,
                '--extra-vars', '@'+settings, '--extra-vars', '@'+project), directory, self.environment)


class FencedScheduler:
    """Client обязан реализовать conditional external writes и typed readback.

    CAS относится к исходной spec identity, не только имени job. Nomad dynamic
    metadata объявляется лишь после physical source_digest confirmation.
    """
    capabilities = frozenset({'scheduler','inspect','fenced_restore'})

    def __init__(self, *, client):
        required = {'conditional_spec','conditional_announce','inspect','complete_plan'}
        if not required <= getattr(client, 'capabilities', frozenset()):
            raise ValueError('scheduler lacks conditional spec/readback capabilities')
        self.client = client

    def plan(self, build):
        return self.client.plan(build)

    def apply(self, ticket):
        if ticket.stage in ('clear','restore'):
            return self.client.compare_and_apply_spec(ticket)
        if ticket.stage == 'announce':
            return self.client.confirm_and_announce_image(ticket)
        raise ValueError('scheduler stage is outside enrollment')

    def inspect(self, ticket):
        return self.client.inspect(ticket)


class FencedNodeExecutor:
    capabilities = frozenset({'node_executor','inspect'})

    def __init__(self, *, client):
        if not {'conditional_body','inspect'} <= getattr(client, 'capabilities', frozenset()):
            raise ValueError('node executor lacks conditional body/readback capabilities')
        self.client = client

    def apply(self, ticket):
        if ticket.stage != 'wipe':
            raise ValueError('node executor accepts only the addressed wipe stage')
        return self.client.wipe_stopped_incarnation(ticket)

    def inspect(self, ticket):
        return self.client.inspect(ticket)
