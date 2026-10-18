"""Регрессии #755: законный maximum-ref bootstrap в прежнем credential budget.

Исходный RED #751: 63 full gates, 25257 SQL, 14.928 s против 5 s deadline.
Повтор на frozen base: 66 gates, 26453 SQL; socket timeout воспроизведён до fix.
Named batch совмещает соседние post/pre checkpoints, а каждый checkpoint
заново доказывает scope/issuer/claims/selection. Только immutable ciphertext
не нужен metadata-only queries. Ни 32-ref cap, ни production 5/10 s не меняются.
Отдельный runner join-ит своего worker до flush/DROP после request timeout.
"""
from django.test import TransactionTestCase, override_settings

import _bootstrap_transfer_cases as transfer_cases


@override_settings(ALLOWED_HOSTS=['testserver'])
class MaximumSelectionDeadline(TransactionTestCase):
    serialized_rollback = True
    setUp = transfer_cases.TransferDelivery.setUp
    fixture = transfer_cases.TransferDelivery.fixture
    async def exercise(self, body, *, application, peers=None, timeout=2, client_timeout=3):
        import os
        from pathlib import Path
        from mop.server.controller.runtime import Runtime
        runtime = Runtime(database_limit=2, queue_limit=0)
        server = self.service.CredentialService(self.path, application=application,
            runtime=runtime, allowed_uids=(os.geteuid(),) if peers is None else peers, timeout=timeout)
        await server.start()
        try:
            client = self.Client(self.path, server_uid=os.geteuid(), timeout=client_timeout)
            await body(client, server)
        finally:
            await server.aclose()
            # Request budget остаётся 5s. Только teardown ждёт своего worker:
            # flush/DROP не вправе гоняться с незавершённым SQL после timeout.
            await runtime.aclose(timeout=180)
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(runtime.pending, 0)
    reserved_grant = transfer_cases.TransferDelivery.reserved_grant
    grant_assignment = transfer_cases.TransferDelivery.grant_assignment
    post = transfer_cases.TransferDelivery.post
    run_delivery = transfer_cases.TransferDelivery.run_delivery
    initial_phase_roundtrip = transfer_cases.TransferDelivery.initial_phase_roundtrip

    def maximum_inputs(self):
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        actor = self.access.token_context(self.token)
        refs = tuple(app.store.create(actor, project_id=self.project.pk, name='input-'+str(i),
            kind='file', purpose='project', value=b'x').active_version_id for i in range(31))
        return dict(tasks=b'x'*262144, variables=b'y'*131072,
            provenance='\U0001f600'*1024, commit='a'*64, secret_refs=refs)

    def test_bootstrap_batch_does_not_repeat_full_admission_per_consumer(self):
        # HYPOTHESIS: одиночный retained reader повторяет весь execution/issuer
        # граф дважды на consumer, включая повторный полный ref-набор.
        # Число queries и фаз измеряется на настоящем PG, не mock permissions.
        # SOLUTION: named bootstrap admission совмещает повторные SQL proofs
        # одного checkpoint; batch сохраняет fresh gate после каждого decrypt.
        # Metadata-only reference guards не перечитывают immutable ciphertext.
        # RESULT: 26453 -> 4047 SQL; 31 inputs + model_config проходят socket/TLS
        # за 2.806/2.834 s при прежних 5 s. Early/mid/late mutations отказывают.
        # STATUS: FIXED — see #755.
        import time
        from collections import Counter
        from functools import wraps
        from contextlib import ExitStack
        from unittest.mock import patch
        from django.db import connection
        from mop.common.credentials import Command
        from mop.server.controller.launchdelivery import LaunchAuthority
        from mop.server.controller.initialselection import LaunchSelections
        from mop.server.controller.machinebinding import OperationWork
        from mop.server.sessions.services import MachineExecution
        from mop.server.access.grants import Grants
        from mop.server.access.services import Access
        self.fixture('bootstrap_body', bootstrap_content=self.maximum_inputs())
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        phases, active, queries, tables = {}, [], Counter(), Counter()
        def measure(cls, name):
            original = getattr(cls, name)
            key = cls.__name__+'.'+name
            @wraps(original)
            def measured(*args, **kwargs):
                start = time.monotonic()
                item = [0]
                active.append((key, item))
                try:
                    return original(*args, **kwargs)
                finally:
                    active.pop()
                    count, sql, elapsed = phases.get(key, (0, 0, 0))
                    phases[key] = count+1, sql+item[0], elapsed+time.monotonic()-start
            return patch.object(cls, name, measured)
        def query(execute, sql, params, many, context):
            for key, item in active:
                item[0] += 1
            queries[active[-1][0] if active else 'other'] += 1
            tables[sql.split(' FROM ')[1].split()[0] if ' FROM ' in sql else sql.split()[0]] += 1
            return execute(sql, params, many, context)
        with ExitStack() as stack:
            for cls, name in ((LaunchAuthority, 'selection'), (Access, 'authorize'), (MachineExecution, 'initial_bootstrap_admission'), (MachineExecution, 'action_admission'),
                    (OperationWork, 'launch_definition'), (LaunchSelections, 'read'),
                    (Grants, 'authorize_launch'), (Access, 'authorize_owner')):
                stack.enter_context(measure(cls, name))
            stack.enter_context(connection.execute_wrapper(query))
            value = app.execute(self.machine_auth, Command.INITIAL_LAUNCH, {'work': self.metadata})
        print('bootstrap authority phases:', {key: (count, sql, round(elapsed, 3))
            for key, (count, sql, elapsed) in phases.items()}, 'query owners:', dict(queries), 'tables:', dict(tables), flush=True)
        self.assertEqual(len(value['consumers']), 31)
        self.assertLessEqual(sum(queries.values()), 4096, 'bootstrap authority SQL amplification')

    def batch_mutation(self, mutation, position):
        import asyncio
        import base64
        from django.db import connection
        from mop.server.controller import bootstrap
        from mop.server.controller.models import Installation
        self.fixture('bootstrap_body', bootstrap_content=self.maximum_inputs())
        install = Installation.objects.get(pk=1)
        real = bootstrap.secret_provider(self.bootstrap, installation_id=install.identity,
            expected_root_digest=install.root_digest, key_id='root-v1')
        if mutation == 'owner_group':
            from django.contrib.auth.models import Group, Permission
            group = Group.objects.create(name='batch-issuer')
            permission = Permission.objects.get(content_type__app_label='access', codename='issue_grant')
            group.permissions.add(permission)
            self.user.user_permissions.remove(permission)
            self.user.groups.add(group)
        calls = []
        def change():
            from django.utils import timezone
            from mop.server.access import models as a
            from mop.server.operations import models as o
            from mop.server.sessions import models as e
            from mop.server.preparation import models as b
            from mop.server.secrets import models as s
            from rest_framework.authtoken.models import Token
            from uuid import uuid4
            if mutation == 'approval':
                b.ApprovalRevocation.objects.create(approval_id=self.selected_launch.bootstrap_policy.approval_id)
            elif mutation == 'grant':
                a.Grant.objects.filter(pk=self.reserved.id).update(state='revoked')
            elif mutation == 'owner_group':
                group.permissions.clear()
            elif mutation == 'machine_token':
                Token.objects.filter(user_id=self.machine.user_id).delete()
            elif mutation == 'issuer_token':
                Token.objects.filter(user=self.user).delete()
            elif mutation == 'epoch':
                Installation.objects.filter(pk=1).update(epoch=uuid4())
            elif mutation == 'executor_epoch':
                e.ExecutionIncarnation.objects.filter(pk=self.reserved.incarnation).update(executor_epoch=uuid4())
            elif mutation == 'claim':
                o.ResourceClaim.objects.filter(operation=self.operation, released_at=None).update(released_at=timezone.now())
            elif mutation == 'claim_token':
                o.ResourceClaim.objects.filter(operation=self.operation, released_at=None).update(token=uuid4())
            elif mutation == 'step_token':
                o.Step.objects.filter(pk=self.metadata['step_id']).update(token=uuid4())
            elif mutation == 'lease':
                e.AssignmentAttempt.objects.filter(pk=self.attempt.id).update(lease_until=timezone.now())
            elif mutation == 'profile':
                type(self.user).objects.filter(pk=self.user.pk).update(email='changed@example.invalid')
            elif mutation == 'active_version':
                version = s.SecretVersion.objects.get(pk=next(version for purpose, version in self.selected_launch.consumers
                    if purpose == 'bootstrap_input'))
                # Явная аварийная порча metadata, не lifecycle API и не authority fixture.
                s.Secret._base_manager.filter(pk=version.secret_id).update(active_version_id=None)
            elif mutation == 'retained_reference':
                version = s.SecretVersion.objects.get(pk=self.selected_launch.bootstrap_policy.version_id)
                s.Secret._base_manager.filter(pk=version.secret_id).update(revoked=True)
            else:
                raise AssertionError(mutation)
        class Changing:
            def decrypt(inner, envelope, context):
                self.assertFalse(connection.in_atomic_block)
                calls.append(context)
                if mutation == 'corrupt_envelope' and len(calls) == position:
                    from dataclasses import replace
                    corrupted = replace(envelope, ciphertext=bytes([envelope.ciphertext[0] ^ 1]) + envelope.ciphertext[1:])
                    self.assertEqual(len(corrupted.to_bytes()), len(envelope.to_bytes()))
                    return real.decrypt(corrupted, context)
                value = real.decrypt(envelope, context)
                if mutation != 'corrupt_envelope' and len(calls) == position:
                    change()
                return value
        async def body(client, server):
            response = await asyncio.to_thread(self.post, path='/api/v1/machine/work/initial-launch')
            # AEAD authenticity failure — существующий refused/403, не availability.
            self.assertEqual(response.status_code, 403, response.content)
            if mutation == 'corrupt_envelope':
                self.assertEqual(response.json()['error']['message'], 'secret authentication failed')
            self.assertEqual(set(response.json()), {'schema_version', 'error'})
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertNotIn(base64.b64encode(b'x'*256), response.content)
            self.assertNotIn(b'consumers', response.content)
            self.assertNotIn(b'bootstrap_revision', response.content)
        self.run_delivery(body, provider=Changing())
        self.assertEqual(len(calls), position, 'definitive revocation must stop before another decrypt')

    def test_timed_out_bootstrap_batch_keeps_worker_and_pool_owned(self):
        import asyncio
        import threading
        from asgiref.sync import async_to_sync
        from django.db import connection
        from mop.server.controller import bootstrap
        from mop.server.controller.models import Installation
        self.fixture('bootstrap_body', bootstrap_content=self.maximum_inputs())
        install = Installation.objects.get(pk=1)
        real = bootstrap.secret_provider(self.bootstrap, installation_id=install.identity,
            expected_root_digest=install.root_digest, key_id='root-v1')
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Slow:
            def decrypt(inner, envelope, context):
                self.assertFalse(connection.in_atomic_block)
                calls.append(context)
                if len(calls) == 2:
                    # Canonical body уже проверен; ждёт именно batch consumer.
                    entered.set()
                    if not release.wait(10):
                        raise TimeoutError('bounded bootstrap provider wait')
                return real.decrypt(envelope, context)
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application(provider=Slow())
        async def body(client, server):
            caller = asyncio.create_task(asyncio.to_thread(
                client.read_initial_launch, self.machine_auth, self.metadata))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                with self.assertRaises(self.Error):
                    await caller
                self.assertGreater(server.runtime.pending, 0)
                with self.assertRaisesRegex(RuntimeError, 'shutdown incomplete'):
                    await server.runtime.aclose(timeout=.02)
                self.assertGreater(server.runtime.pending, 0)
            finally:
                release.set()
                await asyncio.gather(caller, return_exceptions=True)
        # Меньший test-only deadline воспроизводит cancellation; production 5s
        # и default shutdown 10s не меняются. exercise join-ит worker до flush.
        async_to_sync(self.exercise)(body, application=app, timeout=1, client_timeout=2)
        self.assertEqual(len(calls), 32)

    def test_maximum_feasible_31_bootstrap_refs_plus_model_config_cross_real_tls(self):
        # Codec допускает 32 refs, SQL create требует distinct model_config.
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        actor = self.access.token_context(self.token)
        refs = tuple(app.store.create(actor, project_id=self.project.pk, name='input-'+str(i),
            kind='file', purpose='project', value=b'x').active_version_id for i in range(31))
        self.initial_phase_roundtrip('bootstrap_body', bootstrap_content=dict(tasks=b'x'*262144,
            variables=b'y'*131072, provenance='\U0001f600'*1024, commit='a'*64, secret_refs=refs))
        print('maximum bootstrap transport seconds:', self.phase_timings, flush=True)
        for elapsed in self.phase_timings.values():
            self.assertLess(elapsed, 5, 'unchanged credential deadline')


def _mutation_case(mutation, position):
    def check(self):
        self.batch_mutation(mutation, position)
    return check


for _mutation in ('approval', 'grant', 'owner_group', 'machine_token', 'issuer_token', 'epoch',
                  'executor_epoch', 'claim', 'claim_token', 'step_token', 'lease', 'profile',
                  'active_version', 'retained_reference', 'corrupt_envelope'):
    for _position in (1, 16, 32):
        setattr(MaximumSelectionDeadline, f'test_batch_{_mutation}_at_decrypt_{_position}',
            _mutation_case(_mutation, _position))
