"""Сквозные send/dispatch: DRF -> общий журнал -> machine claim/input/receipt.

HYPOTHESIS: SendCommand есть, но конечный handler, route и private input отсутствуют.
SPEC: send не присваивает work; dispatch требует receiving capability и владельца;
identity до delivery, два current gates, durable unknown не повторяется.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from unittest.mock import patch
from uuid import uuid4

from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from _restart_vertical_cases import RestartVertical
from mop.server.controller import operationviews, machineviews
from mop.server.controller.machinebinding import compose, operations
from mop.server.operations import models as m
from mop.server.sessions import models as s


@override_settings(ALLOWED_HOSTS=['testserver'])
class SendDispatch(TransactionTestCase):
    serialized_rollback = True
    setUp = RestartVertical.setUp
    fixture = RestartVertical.fixture
    body = RestartVertical.body

    def test_public_message_admission_is_not_a_missing_route(self):
        self.fixture()
        response = self.post()
        self.assertNotEqual(response.status_code, 404, 'typed messages admission must be routed')

    def prepare(self, *, receives=True, dispatch_capability=False):
        from _grant_cases import manifest
        original = manifest
        def receiving(**kw):
            return original(**{**kw, 'receives': receives, 'dispatch': dispatch_capability})
        with patch('_grant_cases.manifest', receiving):
            self.fixture()
        from mop.server.controller.messagebinding import MessageCipher
        from mop.server.secrets.crypto import XChaCha20Provider, Keyring
        provider = XChaCha20Provider(Keyring('test', {'test': b'm'*32}))
        self.cipher = MessageCipher(provider, 'isolated-ci', digest_key=b'd'*32)
        self.ops = operations(grants=self.grants, message_support=self.support, message_cipher=self.cipher)
        self.service = compose(grants=self.grants, message_support=self.support, message_cipher=self.cipher)
        patch.object(operationviews, 'message_api', lambda: self.ops).start()
        patch.object(operationviews, 'operations_api', lambda: self.ops).start()
        patch.object(machineviews, 'execution', self.service).start()

    def envelope(self, **changes):
        return self.body(**{'target_id':str(self.target.id), 'params':{'message': 'private send payload'}, **changes})

    def post(self, body=None, dispatch=False):
        return self.client.post('/api/v1/dispatches' if dispatch else '/api/v1/messages',
            body or self.envelope(), content_type='application/json', secure=True)

    def admit(self, *, dispatch=False):
        response = self.post(dispatch=dispatch)
        self.assertEqual(response.status_code, 202, response.content)
        return response.json()['operation']

    def next(self):
        work, = self.service.poll(self.machine_actor, self.node.id, executor_epoch=self.boot, limit=1)
        attempt = self.service.claim(self.machine_actor, work, request_id=uuid4())
        return work, attempt

    def finish(self, work, attempt, outcome='succeeded', evidence='identity-applied'):
        return self.service.result(self.machine_actor, attempt.id, expected_revision=attempt.revision,
            receipt_id=uuid4(), outcome=outcome, evidence=evidence)

    def test_rest_machine_input_and_result_share_journal_and_purge_private_bytes(self):
        self.prepare()
        body = self.envelope()
        response = self.post(body)
        self.assertEqual(response.status_code, 202, response.content)
        operation = response.json()['operation']
        self.assertNotIn('private send payload', json.dumps(operation))
        self.assertEqual(operation.get('message_id'), str(m.MessageBinding.objects.get().message_id))
        self.assertNotEqual(operation['message_id'], operation['request_id'])
        self.assertNotEqual(operation['message_id'], operation['id'])
        owner, generation = self.target.owner_id, self.target.generation
        work, attempt = self.next()
        self.assertEqual(work.action, 'apply_identity')
        self.finish(work, attempt)
        work, attempt = self.next()
        self.assertEqual(work.action, 'send_message')
        response = self.machine.get('/api/v1/machine/work/input', dict(schema_version='1',
            operation_id=str(work.operation_id), step_id=str(work.step_id)), secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        value = response.json()
        self.assertEqual(value['message'], body['params']['message'])
        self.assertEqual(value['operation_request_id'], body['request_id'])
        self.assertEqual(value['message_id'], str(m.MessageBinding.objects.get().message_id))
        self.finish(work, attempt, evidence='delivered')
        status = self.ops.status(self.actor, work.operation_id)
        self.assertEqual(status['state'], 'succeeded')
        self.assertFalse(status['resources_held'])
        self.assertEqual(bytes(m.MessageBinding.objects.get().ciphertext), b'')
        self.assertEqual(m.EffectReceipt.objects.count(), 2)
        self.assertEqual(m.AuditEvent.objects.count(), m.OutboxEvent.objects.count())
        self.target.refresh_from_db()
        self.assertEqual((self.target.owner_id, self.target.generation), (owner, generation))
        self.assertNotIn(body['params']['message'], repr(list(m.AuditEvent.objects.values())))
        self.assertNotIn(body['params']['message'], repr(list(m.OutboxEvent.objects.values())))
        retry = self.post(body)
        self.assertEqual(retry.json()['operation']['id'], operation['id'])
        self.assertEqual(retry.json()['operation']['state'], 'succeeded')

    def test_unknown_is_not_retryable_and_keeps_shared_claims(self):
        self.prepare()
        self.admit()
        work, attempt = self.next()
        self.finish(work, attempt)
        work, attempt = self.next()
        self.finish(work, attempt, outcome='unknown', evidence='connection-lost')
        self.assertEqual(self.service.poll(self.machine_actor, self.node.id, executor_epoch=self.boot), ())
        status = self.ops.status(self.actor, work.operation_id)
        self.assertEqual(status['state'], 'unknown')
        self.assertTrue(status['resources_held'])
        self.assertEqual(bytes(m.MessageBinding.objects.get().ciphertext), b'')

    def test_dispatch_requires_receiving_policy_without_role_flag_bypass(self):
        self.prepare(receives=False)
        self.admit()
        response = self.post(dispatch=True)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(m.Operation.objects.count(), 1)

    def test_dispatch_uses_named_command_and_does_not_reassign_human_work(self):
        self.prepare()
        self.admit(dispatch=True)
        self.assertEqual(m.Operation.objects.get().action, 'dispatch')
        self.assertEqual(list(m.Step.objects.order_by('ordinal').values_list('name', flat=True)),
            ['apply_identity', 'send_message'])
        self.target.refresh_from_db()
        self.assertEqual(self.target.owner_id, self.user.pk)

    def test_request_digest_conflicts_and_replay_survives_expired_runtime(self):
        self.prepare()
        body = self.envelope()
        first = self.post(body)
        self.assertEqual(first.status_code, 202, first.content)
        changed = {**body, 'params': {'message': 'different'}}
        self.assertEqual(self.post(changed).status_code, 409)
        s.ExecutionIncarnation.objects.filter(pk=self.target.current_incarnation_id).update(accepting=False)
        again = self.post(body)
        self.assertEqual(again.status_code, 202, again.content)
        self.assertEqual(first.json()['operation']['id'], again.json()['operation']['id'])

    def test_deadline_and_revoked_recipient_rechecked_before_input(self):
        self.prepare()
        self.admit()
        work, attempt = self.next()
        self.finish(work, attempt)
        work, attempt = self.next()
        self.grants.revoke(self.actor, self.target.grant_id,
            expected_revision=self.grants.get(self.actor, self.target.grant_id).revision)
        response = self.machine.get('/api/v1/machine/work/input', dict(schema_version='1',
            operation_id=str(work.operation_id), step_id=str(work.step_id)), secure=True)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertNotIn('private send payload', response.content.decode())

    def test_delegated_send_and_dispatch_use_current_capabilities_and_private_result(self):
        self.prepare(dispatch_capability=True)
        from rest_framework.authtoken.models import Token
        from django.test import Client
        key = Token.objects.get(user__principal__grant__id=self.target.grant_id).key
        self.client = Client(HTTP_AUTHORIZATION='Token '+key)
        for dispatch in (False, True):
            response = self.post(dispatch=dispatch)
            self.assertEqual(response.status_code, 202, response.content)
            operation = response.json()['operation']
            response = self.client.get('/api/v1/operations/'+operation['id'], secure=True)
            self.assertEqual(response.status_code, 200, response.content)

    def test_holder_is_current_endpoint_not_json_owner_or_another_conversation(self):
        self.prepare()
        from mop.server.catalog.models import ClientRegistration
        from mop.server.access.models import ClientOwnership
        from mop.server.controller.models import Installation
        from mop.server.sessions.endpoints import Endpoints
        from mop.server.controller.endpointviews import authority
        registration = ClientRegistration.objects.create(installation=Installation.objects.get(), label='holder')
        ClientOwnership.objects.create(client=registration, user=self.user)
        endpoints = Endpoints(authority=authority)
        endpoint = endpoints.register(self.actor, client_id=registration.id, project_id=self.project.id,
            external_session_id='external-conversation', display_name='holder')
        holder = endpoints.connect(self.actor, endpoint.id, gateway_id=uuid4())
        from mop.server.operations.commands import parse_dispatch
        from mop.server.access.services import Denied
        self.ops.messages.endpoints = endpoints
        command = parse_dispatch(self.envelope(params={'message':'holder task', 'notify_when_idle':True}))
        value = self.ops.submit_message(self.actor, command, holder=holder)
        self.assertEqual(m.MessageBinding.objects.get().holder_endpoint, endpoint.id)
        self.assertEqual(self.ops.status(self.actor, value['id'])['state'], 'accepted')
        self.service.work.operations.messages.endpoints = endpoints
        work, attempt = self.next()
        self.finish(work, attempt)
        work, attempt = self.next()
        response = self.machine.get('/api/v1/machine/work/input', dict(schema_version='1',
            operation_id=str(work.operation_id), step_id=str(work.step_id)), secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json().get('reply_address'), endpoint.address)
        endpoints.disconnect(holder)
        response = self.machine.get('/api/v1/machine/work/input', dict(schema_version='1',
            operation_id=str(work.operation_id), step_id=str(work.step_id)), secure=True)
        self.assertEqual(response.status_code, 403, response.content)

    def test_private_decrypt_wait_rechecks_deadline_and_machine_credentials(self):
        self.prepare()
        self.admit()
        work, attempt = self.next()
        self.finish(work, attempt)
        work, attempt = self.next()
        original = self.cipher.open
        def expired(*args):
            value = original(*args)
            m.Operation.objects.filter(pk=work.operation_id).update(not_after=timezone.now()-timedelta(seconds=1))
            return value
        with patch.object(self.cipher, 'open', expired):
            response = self.machine.get('/api/v1/machine/work/input', dict(schema_version='1',
                operation_id=str(work.operation_id), step_id=str(work.step_id)), secure=True)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertNotIn('private send payload', response.content.decode())

    def test_crypto_failure_surfaces_safe_reason_without_operation_or_plaintext(self):
        self.prepare()
        from mop.server.secrets.crypto import SecretCryptoError
        with patch.object(type(self.cipher.provider), 'encrypt', side_effect=SecretCryptoError('secret encryption key is unavailable')):
            response = self.post()
        self.assertEqual(response.status_code, 503, response.content)
        self.assertIn('secret encryption key is unavailable', response.content.decode())
        self.assertNotIn('private send payload', response.content.decode())
        self.assertFalse(m.Operation.objects.exists())

    def test_identical_replay_does_not_require_cipher_provider_but_new_request_does(self):
        self.prepare()
        body = self.envelope()
        first = self.post(body)
        self.assertEqual(first.status_code, 202, first.content)
        self.cipher.provider = None
        again = self.post(body)
        self.assertEqual(again.status_code, 202, again.content)
        self.assertEqual(again.json()['operation']['id'], first.json()['operation']['id'])
        response = self.post()
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(m.Operation.objects.count(), 1)

    def test_expired_queued_ciphertext_is_purged_without_replay(self):
        self.prepare()
        operation = self.admit()
        m.Operation.objects.filter(pk=operation['id']).update(not_after=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.service.poll(self.machine_actor, self.node.id, executor_epoch=self.boot), ())
        self.assertEqual(bytes(m.MessageBinding.objects.get().ciphertext), b'')
        self.assertEqual(self.ops.status(self.actor, operation['id'])['state'], 'refused')
        self.assertFalse(m.ResourceClaim.objects.exists())

    def test_machine_revoked_during_decrypt_receives_no_private_bytes(self):
        self.prepare()
        self.admit()
        work, attempt = self.next()
        self.finish(work, attempt)
        work, attempt = self.next()
        original = self.cipher.open
        def revoked(*args):
            from rest_framework.authtoken.models import Token
            value = original(*args)
            Token.objects.filter(key=self.machine_key).delete()
            return value
        with patch.object(self.cipher, 'open', revoked):
            response = self.machine.get('/api/v1/machine/work/input', dict(schema_version='1',
                operation_id=str(work.operation_id), step_id=str(work.step_id)), secure=True)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertNotIn('private send payload', response.content.decode())

    def test_payload_encryption_wait_does_not_commit_stale_recipient(self):
        self.prepare()
        original = self.cipher.seal
        def changed(*args):
            value = original(*args)
            s.Assignment.objects.filter(pk=self.target.id).update(revision=self.target.revision+1)
            return value
        with patch.object(self.cipher, 'seal', changed):
            response = self.post()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(m.Operation.objects.exists())
        self.assertFalse(m.OutboxEvent.objects.exists())

    def test_encrypted_payload_cannot_be_substituted_between_operations(self):
        self.prepare()
        first, second = self.admit(), self.admit()
        one, two = (m.MessageBinding.objects.get(operation_id=value['id']) for value in (first, second))
        from mop.server.secrets.crypto import SecretCryptoError
        with self.assertRaises(SecretCryptoError):
            self.cipher.open(two.operation_id, two.message_id, self.target.id, bytes(one.ciphertext))
        self.assertNotIn('private send payload', bytes(one.ciphertext).decode('utf-8',errors='ignore'))

    def test_notify_without_holder_and_json_authority_fields_are_refused(self):
        self.prepare()
        response = self.post(self.envelope(params={'message':'private send payload', 'notify_when_idle':True}))
        self.assertEqual(response.status_code, 403, response.content)
        for field in ('owner', 'holder', 'dispatch', 'force', 'subject', 'shell'):
            response = self.post(self.envelope(**{field:'untrusted'}))
            self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(m.Operation.objects.exists())

    def test_unknown_identity_stage_never_delivers_or_releases_resource(self):
        self.prepare()
        self.admit(dispatch=True)
        work, attempt = self.next()
        self.finish(work, attempt, outcome='unknown', evidence='connection-lost')
        self.assertEqual(self.service.poll(self.machine_actor, self.node.id, executor_epoch=self.boot), ())
        self.assertTrue(self.ops.status(self.actor, work.operation_id)['resources_held'])
        self.assertFalse(s.IdentityApplication.objects.exists())
        self.assertFalse(s.AssignmentAttempt.objects.filter(action='send_message').exists())

    def test_manual_outer_transaction_cannot_return_accepted(self):
        self.prepare()
        from django.db import transaction
        from mop.server.operations.commands import parse_send
        from mop.server.operations.service import Refused
        with transaction.atomic():
            with self.assertRaisesRegex(Refused, 'independent commit'):
                self.ops.submit(self.actor, parse_send(self.envelope()))
        self.assertFalse(m.Operation.objects.exists())

    def test_delegated_send_without_dispatch_capability_cannot_dispatch(self):
        self.prepare(dispatch_capability=False)
        from rest_framework.authtoken.models import Token
        from django.test import Client
        key = Token.objects.get(user__principal__grant__id=self.target.grant_id).key
        self.client = Client(HTTP_AUTHORIZATION='Token '+key)
        self.admit()
        response = self.post(dispatch=True)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(m.Operation.objects.count(), 1)

    def test_default_production_binding_fails_closed_without_private_cipher(self):
        self.prepare()
        from mop.server.operations.commands import parse_send
        from mop.server.operations.service import Refused
        with self.assertRaisesRegex(Refused, 'protected message cipher'):
            operations(grants=self.grants).submit(self.actor, parse_send(self.envelope()))
        self.assertFalse(m.Operation.objects.exists())

    def test_processes_resolve_duplicate_conflict_and_single_machine_claim(self):
        self.prepare()
        from multiprocessing import get_context
        from dataclasses import replace
        from _send_http_fixture import submit_process, claim_process
        from mop.server.operations.commands import parse_send
        import os
        binding = (connections['default'].settings_dict['NAME'], os.environ['MOP_CONTROLLER_BOOTSTRAP'],
            self.target.id, self.target.current_incarnation_id, self.grants.approvals.proof)
        ctx = get_context('spawn')
        def race(target, arguments):
            connections.close_all()
            ready, results = ctx.Barrier(2), ctx.Queue()
            children = [ctx.Process(target=target, args=(*argument, ready, results)) for argument in arguments]
            for child in children:
                child.start()
            try:
                values = [results.get(timeout=20) for _ in children]
                for child in children:
                    child.join(20)
                    self.assertEqual(child.exitcode, 0)
                return values
            finally:
                for child in children:
                    if child.is_alive():
                        child.kill(); child.join(5)
                results.close(); results.join_thread()
        command = parse_send(self.envelope())
        values = race(submit_process, [(binding,self.actor,command)]*2)
        self.assertEqual(values[0], values[1])
        self.assertEqual(values[0][0], 'accepted')
        self.assertEqual(m.Operation.objects.count(), 1)
        command = replace(command, intent=replace(command.intent, request_id=uuid4()))
        values = race(submit_process, [(binding,self.actor,command),
            (binding,self.actor,replace(command,message='conflicting intent'))])
        self.assertEqual(sorted(value[0] for value in values), ['accepted','refused'])
        self.assertIn(('refused','request_conflict'), values)
        work, = self.service.poll(self.machine_actor,self.node.id,executor_epoch=self.boot,limit=1)
        values = race(claim_process, [(binding,self.machine_actor,work)]*2)
        self.assertEqual(sorted(values), ['claimed','conflict'])
        self.assertEqual(s.AssignmentAttempt.objects.count(), 1)

    def test_production_node_http_client_drives_real_tls_machine_receipts(self):
        self.prepare()
        from _send_http_fixture import run
        run(self)

    def test_concurrent_identical_request_and_conflicting_claims_use_database(self):
        self.prepare()
        from mop.server.operations.commands import parse_send
        command = parse_send(self.envelope())
        def submit(_):
            try:
                return self.ops.submit(self.actor, command)['id']
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(m.Operation.objects.count(), 1)
        work, = self.service.poll(self.machine_actor, self.node.id, executor_epoch=self.boot, limit=1)
        def claim(_):
            from mop.server.sessions.services import Conflict
            try:
                self.service.claim(self.machine_actor, work, request_id=uuid4())
                return 'claimed'
            except Conflict:
                return 'conflict'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(sorted(results), ['claimed', 'conflict'])
        self.assertEqual(s.AssignmentAttempt.objects.count(), 1)
