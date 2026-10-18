"""BUG/HYPOTHESIS #751: canonical base64-of-base64 пересекает старые 256 KiB.

150000 task bytes были wire-launchable, но после admission неизбежно падают
в HTTPS/private writer. Aggregate consumers и role payload вообще не проверены
до register_agent/reserve/provision. RED должен доказать реальные границы,
а не только сравнить константы. SQL sizing не получает provider/decrypt.
SOLUTION: общий wire profile применяется к exact selected immutable sizes до
reservation/register_agent/provision; только bootstrap получает отдельный default.
RESULT: девять admission/TLS cases проходят, включая base64 quantum до cap и
следующий raw byte за cap с нулём provisioning/effects и без latest fallback.
STATUS: FIXED — see #751 (byte overflow; maximum-ref deadline отдельно остаётся RED).
"""
import hashlib
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

import _launch_delivery_cases as delivery_cases
import _create_cases as create_cases


@override_settings(ALLOWED_HOSTS=['testserver'])
class TransferDelivery(TransactionTestCase):
    serialized_rollback = True
    setUp = delivery_cases.LaunchDelivery.setUp
    fixture = delivery_cases.LaunchDelivery.fixture
    exercise = delivery_cases.LaunchDelivery.exercise
    reserved_grant = delivery_cases.LaunchDelivery.reserved_grant
    grant_assignment = delivery_cases.LaunchDelivery.grant_assignment
    post = delivery_cases.LaunchDelivery.post
    run_delivery = delivery_cases.LaunchDelivery.run_delivery
    initial_phase_roundtrip = delivery_cases.LaunchDelivery.initial_phase_roundtrip

    def test_previously_launchable_150k_tasks_cross_real_tls_with_default_budget(self):
        self.initial_phase_roundtrip('bootstrap_body', bootstrap_content=dict(tasks=b'x'*150000,
            variables=b'', provenance='fixture', commit='a'*40))

    def test_maximum_task_vars_unicode_provenance_and_commit_cross_real_tls(self):
        self.initial_phase_roundtrip('bootstrap_body', bootstrap_content=dict(tasks=b'x'*262144,
            variables=b'y'*131072, provenance='\U0001f600'*1024, commit='a'*64))


@override_settings(ALLOWED_HOSTS=['testserver'])
class TransferAdmission(TransactionTestCase):
    serialized_rollback = True
    setUp = create_cases.CreateVertical.setUp
    ready, body, submit = create_cases.CreateVertical.ready, create_cases.CreateVertical.body, create_cases.CreateVertical.submit

    def select(self, *, consumers=None, definition=None, revision=None, model_config_digest=None):
        from mop.server.controller.initialselection import configure
        return configure(self.actor, project_id=self.project.pk,
            definition_id=(definition or self.definition).id,
            bootstrap_revision=(revision or self.revision).id, image_id=self.image.id,
            source_origin=self.project.origin, source_commit='d'*40, source_ref='refs/heads/exact-source',
            desired_model='fixture-model', model_config_digest=model_config_digest or hashlib.sha256(b'{"model":"fixture-model"}').hexdigest(),
            consumers=consumers or (('model_config', self.model_config.active_version_id),))

    def assert_pre_effect_refusal(self, body):
        from mop.server.access import models as a
        from mop.server.operations import models as o
        from mop.server.sessions.models import Assignment
        from mop.server.secrets.models import SecretVersion
        before = (a.AgentProfile.objects.count(), a.Grant.objects.count(), SecretVersion.objects.count())
        provider = self.grants.secrets.provider
        decrypt = provider.decrypt
        with patch.object(self.grants, 'register_agent', wraps=self.grants.register_agent) as register, \
                patch.object(type(provider), 'decrypt', autospec=True,
                    side_effect=lambda _, *args: decrypt(*args)) as private_read:
            response = self.submit(body)
        private_read.assert_not_called()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn('transfer', response.json()['error']['message'])
        register.assert_not_called()
        self.assertEqual(self.issuer.calls, [])
        self.assertEqual(before, (a.AgentProfile.objects.count(), a.Grant.objects.count(), SecretVersion.objects.count()))
        for model in (o.CreateReservation, o.CreateProvisioningAttempt, o.Operation, o.Step, Assignment):
            self.assertFalse(model.objects.exists(), model.__name__)

    def bootstrap_boundary(self, *, over):
        from mop.common.initial_transfer import phase_limit, base64_size
        from mop.common.credentials import MAX_FRAME
        from mop.server.controller.initialselection import LaunchSelections
        from mop.server.secrets.services import SecretStore
        from mop.server.access.services import Access
        self.ready()
        store = SecretStore(provider=self.grants.secrets.provider, authorizer=Access())
        item = store.create(self.actor, project_id=self.project.pk, name='bounded-bootstrap', kind='file',
            purpose='project', value=b'x')
        def selection(version):
            revision = self.preparation.publish(self.actor, project_id=self.project.pk,
                tasks=b'x'*262144, variables=b'y'*131072, provenance='\U0001f600'*1024,
                commit='b'*64, secret_refs=(version,))
            self.preparation.approve(self.actor, revision.id)
            return self.select(revision=revision, consumers=(('model_config', self.model_config.active_version_id),
                ('bootstrap_input', version)))
        selected = selection(item.active_version_id)
        baseline = LaunchSelections().transfer_profile(selected)['bootstrap_body']
        cap = phase_limit('bootstrap_body')
        # Base64 квантуется по четыре wire bytes; ни один произвольный
        # overhead не угадывается. Exact public metadata и retained sizes те же.
        remaining = min(cap-baseline.http, cap-baseline.private, MAX_FRAME+4-baseline.credential) + base64_size(1)
        raw_size = (remaining//4)*3
        updated = store.update(self.actor, item.id, b'x'*(raw_size + int(over)), expected_revision=item.revision)
        selected = selection(updated.active_version_id)
        measured = LaunchSelections().transfer_profile(selected)['bootstrap_body']
        if over:
            self.assertGreater(measured.private, cap)
            self.assertLessEqual(measured.private-cap, 4)
        else:
            self.assertLessEqual(measured.private, cap)
            self.assertLess(cap-measured.private, 4)
        return selected

    def test_just_above_aggregate_private_cap_refuses_with_zero_provisioning(self):
        self.bootstrap_boundary(over=True)
        self.assert_pre_effect_refusal(self.body())

    def test_largest_base64_quantum_below_aggregate_cap_is_admitted(self):
        self.bootstrap_boundary(over=False)
        class Boundary(Exception):
            pass
        with patch.object(self.grants, 'register_agent', side_effect=Boundary), \
                patch.object(type(self.grants.secrets.provider), 'decrypt', side_effect=AssertionError('admission decrypted')):
            with self.assertRaises(Boundary):
                self.submit(self.body())

    def test_aggregate_bootstrap_consumers_refuse_before_reservation_or_provision(self):
        from mop.server.secrets.services import SecretStore
        self.ready()
        from mop.server.access.services import Access
        store = SecretStore(provider=self.grants.secrets.provider, authorizer=Access())
        secrets = [store.create(self.actor, project_id=self.project.pk, name='bootstrap-'+str(i), kind='file',
            purpose='project', value=b'x'*120000) for i in range(7)]
        refs = tuple(item.active_version_id for item in secrets)
        revision = self.preparation.publish(self.actor, project_id=self.project.pk, tasks=b'x'*150000,
            variables=b'', provenance='fixture', commit='b'*40, secret_refs=refs)
        self.preparation.approve(self.actor, revision.id)
        self.select(revision=revision, consumers=(('model_config', self.model_config.active_version_id),) +
            tuple(('bootstrap_input', version) for version in refs))
        # Latest меньше и launchable, но exact selected oversized policy не
        # заменяется ни им, ни частичным набором consumer bytes.
        latest = self.preparation.publish(self.actor, project_id=self.project.pk,
            tasks=b'x', variables=b'', provenance='later-small', commit='c'*40)
        self.preparation.approve(self.actor, latest.id)
        self.assert_pre_effect_refusal(self.body())

    def test_prepare_consumers_keep_their_256k_aggregate_budget_before_provisioning(self):
        from mop.server.secrets.services import SecretStore
        from mop.server.access.services import Access
        self.ready()
        store = SecretStore(provider=self.grants.secrets.provider, authorizer=Access())
        refs = tuple(store.create(self.actor, project_id=self.project.pk, name='source-'+str(i),
            kind='credential', purpose='git', value=b'x'*75000).active_version_id for i in range(3))
        self.select(consumers=(('model_config', self.model_config.active_version_id),) +
            tuple(('source_credential', version) for version in refs))
        self.assert_pre_effect_refusal(self.body())

    def test_native_model_config_keeps_its_256k_budget_before_provisioning(self):
        from mop.server.secrets.services import SecretStore
        from mop.server.access.services import Access
        self.ready()
        store = SecretStore(provider=self.grants.secrets.provider, authorizer=Access())
        raw = b'{"model":"fixture-model"}'+b' '*200000
        updated = store.update(self.actor, self.model_config.id, raw, expected_revision=self.model_config.revision)
        self.select(consumers=(('model_config', updated.active_version_id),),
            model_config_digest=hashlib.sha256(raw).hexdigest())
        self.assert_pre_effect_refusal(self.body())

    def test_oversized_retained_role_refuses_before_agent_or_credential_provision(self):
        from _grant_cases import manifest
        import json
        self.ready()
        definition = self.grants.publish_definition(self.actor, project_id=self.project.pk,
            manifest=json.dumps(manifest()).encode(), prompt=b'p'*250000, provenance='fixture')
        self.grants.approve_definition(self.actor, definition.id)
        self.select(definition=definition)
        body = self.body()
        body['params']['definition_id'] = str(definition.id)
        self.assert_pre_effect_refusal(body)

    def test_maximum_bootstrap_is_admitted_without_decrypt_or_latest_fallback(self):
        self.ready()
        revision = self.preparation.publish(self.actor, project_id=self.project.pk, tasks=b'x'*262144,
            variables=b'y'*131072, provenance='\U0001f600'*1024, commit='b'*64)
        self.preparation.approve(self.actor, revision.id)
        self.select(revision=revision)
        # Останавливаем на первой границе effects: sizing обязан закончиться
        # без decrypt, а законный maximum -- не получить capacity refusal.
        class Boundary(Exception):
            pass
        with patch.object(self.grants, 'register_agent', side_effect=Boundary), \
                patch.object(type(self.grants.secrets.provider), 'decrypt', side_effect=AssertionError('admission decrypted')):
            with self.assertRaises(Boundary):
                self.submit(self.body())
