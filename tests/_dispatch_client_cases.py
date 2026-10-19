"""Настоящий TLS/DRF dispatch admission, без выполнения native эффекта (#757)."""
from datetime import timedelta
import json
from uuid import uuid4

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from _lib import run_command
from _control_client_cases import NativeControlClient
from _restart_vertical_cases import RestartVertical
from _send_dispatch_cases import SendDispatch
from mop import cli
from mop.client import control as api
from mop.server.operations.models import Operation


@override_settings(ALLOWED_HOSTS=['localhost', 'testserver'])
class NativeDispatchClient(TransactionTestCase):
    serialized_rollback = True
    fixture = RestartVertical.fixture
    body = RestartVertical.body
    https = NativeControlClient.https

    def setUp(self):
        RestartVertical.setUp(self)
        self.token = self.key

    def test_real_tls_lost_reply_keeps_one_distinct_dispatch_admission(self):
        SendDispatch.prepare(self)
        key = str(uuid4())
        deadline = (timezone.now()+timedelta(minutes=2)).isoformat()
        with self.https() as (server, client):
            server.drop_post = True
            out, err, code = run_command(cli.main, ['dispatch', str(self.target.pk),
                'private-dispatch-757', '--epoch', str(self.epoch),
                '--incarnation', str(self.target.current_incarnation_id),
                '--expected-revision', str(self.target.revision), '--request-id', key,
                '--not-after', deadline])
            self.assertEqual(code, 3, err)
            value = json.loads(out)['operation']
            self.assertEqual(value['state'], 'accepted')
            self.assertEqual([method for method, _ in server.calls], ['POST', 'GET'])
            self.assertEqual(server.calls[0][1], '/api/v1/dispatches')
            row = Operation.objects.get(request_id=key)
            self.assertEqual((row.action, str(row.pk)), ('dispatch', value['id']))
            self.assertEqual(Operation.objects.count(), 1)
            self.assertEqual(client.lookup_operation(key, epoch=str(self.epoch)).id, value['id'])
        for private in (self.key, 'private-dispatch-757'):
            self.assertNotIn(private, out+err)

    def test_send_permission_does_not_authorize_real_dispatch(self):
        SendDispatch.prepare(self)
        from _grant_cases import permit_actions
        from mop.server.access.models import IdentitySource, ProjectMembership
        self.user.is_superuser = False
        self.user.save(update_fields=['is_superuser'])
        ProjectMembership.objects.create(user=self.user, project=self.project,
            source=IdentitySource.objects.get(name='local'))
        permit_actions(self.user, 'execution.send_message', 'operations.view_operation')
        with self.https() as (server, client):
            command = client.prepare_dispatch(api.Target(str(self.target.pk),
                str(self.target.current_incarnation_id), str(self.target.revision)),
                epoch=str(self.epoch), not_after=(timezone.now()+timedelta(minutes=2)).isoformat(),
                message='private-dispatch-757')
            with self.assertRaises(api.APIError) as caught:
                client.submit(command)
            self.assertEqual(caught.exception.error.http_status, 403)
            self.assertEqual(Operation.objects.count(), 0)
            self.assertEqual([method for method, _ in server.calls], ['POST'])
