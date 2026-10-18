"""Отдельный RED после byte-budget fix #751: maximum-ref SQL path не успевает.

Не skip и не weakened maximum: отдельный runner изолирует продолжающийся
provider worker от flush следующих TransactionTestCase после socket timeout.
RED: 63 full authority gates, 25257 SQL queries, 14.928 s внутри gates против
неизменного 5 s socket deadline. Authority/timeout не меняются ради GREEN.
Master должен разобрать этот counterexample до полного acceptance #751.
"""
from django.test import TransactionTestCase, override_settings

import _bootstrap_transfer_cases as transfer_cases


@override_settings(ALLOWED_HOSTS=['testserver'])
class MaximumSelectionDeadline(TransactionTestCase):
    serialized_rollback = True
    setUp = transfer_cases.TransferDelivery.setUp
    fixture = transfer_cases.TransferDelivery.fixture
    exercise = transfer_cases.TransferDelivery.exercise
    reserved_grant = transfer_cases.TransferDelivery.reserved_grant
    grant_assignment = transfer_cases.TransferDelivery.grant_assignment
    post = transfer_cases.TransferDelivery.post
    run_delivery = transfer_cases.TransferDelivery.run_delivery
    initial_phase_roundtrip = transfer_cases.TransferDelivery.initial_phase_roundtrip

    def test_maximum_feasible_31_bootstrap_refs_plus_model_config_cross_real_tls(self):
        # Codec допускает 32 refs, SQL create требует distinct model_config.
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        actor = self.access.token_context(self.token)
        refs = tuple(app.store.create(actor, project_id=self.project.pk, name='input-'+str(i),
            kind='file', purpose='project', value=b'x').active_version_id for i in range(31))
        self.initial_phase_roundtrip('bootstrap_body', bootstrap_content=dict(tasks=b'x'*262144,
            variables=b'y'*131072, provenance='\U0001f600'*1024, commit='a'*64, secret_refs=refs))
