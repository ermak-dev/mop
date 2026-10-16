"""#649: настоящий grant authority и standard token, не fake grant authorizer."""
from datetime import timedelta
from uuid import uuid4
from django.test import TransactionTestCase
from django.utils import timezone
import _grant_cases as grants_fixture


class EndpointGrantReports(TransactionTestCase):
    serialized_rollback = True
    # Повторно используем accepted fixture; production Access/Grants остаются настоящими.
    setUp = grants_fixture.GrantCases.setUp
    permit = grants_fixture.GrantCases.permit
    publish = grants_fixture.GrantCases.publish
    reserve = grants_fixture.GrantCases.reserve
    active = grants_fixture.GrantCases.active

    def test_current_grant_can_report_but_cannot_register_human_or_enumerate_local_sessions(self):
        from mop.server.sessions.endpoints import Endpoints
        from mop.server.catalog.models import ClientRegistration
        from mop.server.controller.models import Installation
        from mop.server.controller.endpointviews import authority
        from mop.server.controller.endpointcrypto import EndpointCipher
        from mop.server.access import models as a
        from django.contrib.auth.models import Permission
        self.permit('catalog.view_project')
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label='execution',
            codename__in=('register_endpoint', 'view_delivery', 'deliver_report')))
        api = Endpoints(authority=authority, cipher=EndpointCipher(self.provider, 'isolated-ci'),
            grants=self.api.local_authority())
        Installation.objects.update(effects_enabled=True)
        epoch = Installation.objects.get().epoch
        client = ClientRegistration.objects.create(installation=Installation.objects.get(), label='own')
        a.ClientOwnership.objects.create(client=client, user=self.user)
        args = dict(client_id=client.pk, project_id=self.project.pk,
                    external_session_id='human-cli', display_name='same')
        endpoint = api.register(self.actor, **args)
        context = api.connect(self.actor, endpoint.id, gateway_id=uuid4())
        grant, actor = self.active()
        with self.assertRaises(PermissionError):
            api.register(actor, **args)
        with self.assertRaises(PermissionError):
            api.addresses(actor, self.project.pk)
        with self.assertRaises(PermissionError):
            api.trusted_context(actor, endpoint.id, context.nonce)
        report = api.send(actor, expected_epoch=epoch, address=endpoint.address, message_id=uuid4(), text='private report',
            not_after=timezone.now()+timedelta(minutes=1))
        self.assertEqual(report.state, 'queued')
        frame = api.take(context)
        self.assertEqual(frame.text, 'private report')
        self.assertEqual(api.acknowledge(context, report.id, outcome='accepted').state, 'accepted')
        pending = api.send(actor, expected_epoch=epoch, address=endpoint.address, message_id=uuid4(), text='revoked report',
            not_after=timezone.now()+timedelta(minutes=1))
        self.api.revoke(self.actor, grant.id, expected_revision=grant.revision)
        self.assertIsNone(api.take(context))
        self.assertEqual(api.delivery(self.actor, pending.id).state, 'unavailable')
