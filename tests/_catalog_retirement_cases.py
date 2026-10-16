"""Безопасное снятие metadata #634, не физическое удаление."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import threading
import uuid

from django.db import connection, connections, models as dbmodels, transaction, IntegrityError, DatabaseError
from django.test import TransactionTestCase
from django.utils import timezone

from mop.server.catalog import models, services
from mop.server.controller.runtime import initialize
from _catalog_cases import Allow


# HYPOTHESIS: unconditional UnsafeRemoval не даёт безопасного retirement,
# а DELETE разрушает retained FK и повторно использует старую identity.
# SPEC: inspection вне SQL, commit под parent lock с новым auth/CAS/receipt;
# jobs включают stopped, unknown не empty, membership не обходится proof.
# Fixture receipt — настоящий SQL record внешнего владельца, не payload actor.
# SOLUTION: два typed prepared/commit use cases, SQL-only guard validation;
# portable zero/UUID identity slots сохраняют retained FKs и tombstones.
# RESULT: PG16/17 — 78 checks green; неизвестный/stale receipt отказывает.
# STATUS: FIXED — see #634 (production guard/Access/Operations wiring позднее).
class InspectionRecord(dbmodels.Model):
    id = dbmodels.UUIDField(primary_key=True)
    target_id = dbmodels.UUIDField()
    revision = dbmodels.PositiveBigIntegerField()
    source_sequence = dbmodels.PositiveBigIntegerField(default=1)
    current_sequence = dbmodels.PositiveBigIntegerField(default=1)
    facts = dbmodels.JSONField()

    class Meta:
        app_label = 'retirement_fixture'


class ReceiptGuard:
    def __init__(self, test):
        self.test = test
        self.jobs = []
        self.scheduler_occupants = []
        self.executor_occupants = []
        self.after_inspection = lambda: None
        self.after_validation = lambda: None
        self.validations = 0

    def inspect_project(self, target):
        return self.inspect(target, False)

    def inspect_node(self, target):
        return self.inspect(target, True)

    def inspect(self, target, node):
        from mop.server.catalog.retirement import ProjectRemovalProof, NodeRemovalProof
        self.test.assertFalse(connection.in_atomic_block, 'external inspection under SQL lock')
        now = timezone.now()
        facts = dict(jobs=self.jobs, scheduler_occupants=self.scheduler_occupants,
            executor_occupants=self.executor_occupants, observed_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=30)).isoformat(), source='fixture-scheduler',
            source_incarnation=str(uuid.uuid4()), receipt='external-receipt-' + uuid.uuid4().hex)
        record = InspectionRecord.objects.create(id=uuid.uuid4(), target_id=target.id,
            revision=target.revision, facts=facts)
        self.after_inspection()
        cls = NodeRemovalProof if node else ProjectRemovalProof
        return cls(target.id, target.revision, record.id)

    def validate_project(self, proof, target):
        from mop.server.catalog.retirement import ProjectRemovalReceipt
        row, obs = self.validate(proof, target)
        jobs = row.facts['jobs']
        return ProjectRemovalReceipt(proof, obs, None if jobs is None else tuple(jobs))

    def validate_node(self, proof, target):
        from mop.server.catalog.retirement import NodeRemovalReceipt
        row, obs = self.validate(proof, target)
        scheduler, executor = row.facts['scheduler_occupants'], row.facts['executor_occupants']
        return NodeRemovalReceipt(proof, obs, replace(obs, source='fixture-executor'),
            None if scheduler is None else tuple(scheduler), None if executor is None else tuple(executor))

    def validate(self, proof, target):
        from mop.server.catalog.retirement import SourceObservation
        self.test.assertTrue(connection.in_atomic_block, 'receipt validation outside target transaction')
        try:
            row = InspectionRecord.objects.select_for_update().get(pk=proof.receipt_id)
        except InspectionRecord.DoesNotExist:
            raise services.UnsafeRemoval('authoritative SQL receipt is missing') from None
        if (row.target_id != target.id or row.revision != target.revision
                or row.source_sequence != row.current_sequence):
            raise services.UnsafeRemoval('receipt no longer matches authoritative SQL source')
        self.validations += 1
        self.after_validation()
        f = row.facts
        return row, SourceObservation(f['source'], uuid.UUID(f['source_incarnation']),
            row.source_sequence, f['receipt'], timezone.datetime.fromisoformat(f['observed_at']),
            timezone.datetime.fromisoformat(f['expires_at']))


class CatalogRetirementCases(TransactionTestCase):
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.schema_editor() as editor:
            editor.create_model(InspectionRecord)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            editor.delete_model(InspectionRecord)
        super().tearDownClass()

    def setUp(self):
        self.assertTrue(callable(getattr(services.Catalog, 'prepare_delete_project', None)),
            'catalogue has no bindable prepared retirement use case')
        InspectionRecord.objects.all().delete()
        initialize()
        self.auth = Allow()
        self.guard = ReceiptGuard(self)
        self.api = services.Catalog(authorizer=self.auth, removal_guard=self.guard)
        self.actor = 'trusted-actor'

    def project(self):
        return self.api.register_project(self.actor, 'https://git.example/alpha.git')

    def node(self, **kwargs):
        values = dict(name='node', driver='host', ssh_host='ssh.example', ssh_port=22,
            control_origin='https://control.example', lan_address='10.0.0.2', bootstrap_address='boot.example')
        values.update(kwargs)
        return self.api.configure_node(self.actor, **values)

    def test_retire_project_keeps_history_and_re_registration_gets_new_identity(self):
        p = self.project()
        policy = self.api.pin_project_policy(self.actor, p.id, b'approved-source', source_commit='a'*40,
            source_ref='main', provenance='fixture', cpu=100, memory_mb=100, disk_mb=100, expected_revision=1)
        p = self.api.get_project(self.actor, p.id)
        image = self.api.create_image_revision(self.actor, p.id, policy.id, source='fixture')
        client = self.api.register_client(self.actor, label='fixture')
        binding = self.api.bind_workspace(self.actor, client.id, 'work', p.id, integration_branch='main', expected_revision=0)
        node = self.node()
        self.api.set_node_projects(self.actor, node.id, [p.id], expected_revision=1)
        self.api.set_image_intent(self.actor, node.id, p.id, image.id, expected_revision=0)
        retired = self.api.delete_project(self.actor, p.id, expected_revision=p.revision)
        self.assertIsNotNone(retired.retired_at)
        self.assertEqual(retired.revision, p.revision + 1)
        self.assertTrue(models.ProjectPolicySnapshot.objects.filter(pk=policy.id, project_id=p.id).exists())
        self.assertTrue(models.ImageRevision.objects.filter(pk=image.id).exists())
        self.assertEqual(models.WorkspaceBinding.objects.get(pk=binding.id).project_id, p.id)
        self.assertTrue(models.NodeImage.objects.filter(project_id=p.id).exists())
        self.assertTrue(models.NodeProject.objects.filter(project_id=p.id).exists())
        self.assertEqual(self.api.list_projects(self.actor), ())
        with self.assertRaises(models.Project.DoesNotExist):
            self.api.get_project(self.actor, p.id)
        with self.assertRaises(models.WorkspaceBinding.DoesNotExist):
            self.api.resolve_workspace(self.actor, client.id, 'work')
        fresh = self.project()
        self.assertNotEqual(fresh.id, p.id)
        self.assertEqual((fresh.revision, fresh.name, fresh.origin), (1, p.name, p.origin))
        with self.assertRaises(services.Conflict):
            self.api.delete_project(self.actor, p.id, expected_revision=p.revision)
        self.assertEqual(self.api.get_project(self.actor, fresh.id), fresh)

    def test_retire_node_preserves_settings_images_and_new_name_is_new_uuid(self):
        n = self.node()
        self.api.set_node_settings(self.actor, n.id, {'log_level': 'debug'}, expected_revision=1)
        retired = self.api.forget_node(self.actor, n.id, expected_revision=1)
        self.assertIsNotNone(retired.retired_at)
        self.assertTrue(models.NodeSetting.objects.filter(node_id=n.id).exists())
        self.assertEqual(self.api.list_nodes(self.actor), ())
        self.assertFalse(self.api.node_is_current(n.id))
        with self.assertRaises(models.Node.DoesNotExist):
            self.api.get_node(self.actor, n.id)
        fresh = self.node()
        self.assertNotEqual(fresh.id, n.id)
        self.assertTrue(self.api.node_is_current(fresh.id))
        self.assertFalse(self.api.node_is_current(uuid.uuid4()))
        with self.assertRaises(services.Conflict):
            self.api.forget_node(self.actor, n.id, expected_revision=1)

    def test_remaining_stopped_jobs_and_unknown_observations_are_not_empty(self):
        p = self.project()
        for jobs in (['stopped-job'], ['running-job'], None):
            self.guard.jobs = jobs
            with self.subTest(jobs=jobs), self.assertRaises(services.UnsafeRemoval):
                self.api.delete_project(self.actor, p.id, expected_revision=1)
        self.assertTrue(self.api.project_is_current(p.id))
        n = self.node()
        for scheduler, executor in ((['allocation'], []), ([], ['stopped-body']), (None, []), ([], None)):
            self.guard.scheduler_occupants, self.guard.executor_occupants = scheduler, executor
            with self.subTest(scheduler=scheduler, executor=executor), self.assertRaises(services.UnsafeRemoval):
                self.api.forget_node(self.actor, n.id, expected_revision=1)
        self.assertTrue(self.api.node_is_current(n.id))

    def test_membership_refuses_even_bound_guard_and_empty_sources(self):
        n = self.node()
        n = self.api.set_deployment_membership(self.actor, n.id, True, expected_revision=1)
        with self.assertRaisesRegex(services.UnsafeRemoval, 'deployment'):
            self.api.forget_node(self.actor, n.id, expected_revision=n.revision)
        self.assertEqual(self.guard.validations, 0)
        n = self.api.set_deployment_membership(self.actor, n.id, False, expected_revision=n.revision)
        self.assertIsNotNone(self.api.forget_node(self.actor, n.id, expected_revision=n.revision).retired_at)

    def test_default_deny_and_no_inspection_inside_outer_atomic(self):
        p, n = self.project(), self.node()
        unbound = services.Catalog(authorizer=self.auth)
        with self.assertRaisesRegex(services.UnsafeRemoval, 'fresh'):
            unbound.delete_project(self.actor, p.id, expected_revision=1)
        with self.assertRaisesRegex(services.UnsafeRemoval, 'fresh'):
            unbound.forget_node(self.actor, n.id, expected_revision=1)
        with transaction.atomic():
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                self.api.prepare_delete_project(self.actor, p.id, expected_revision=1)
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                self.api.forget_node(self.actor, n.id, expected_revision=1)

    def test_receipt_invalidated_or_expired_after_inspection_refuses(self):
        p = self.project()
        for invalidation in ('sequence', 'expired', 'missing', 'target'):
            prepared = self.api.prepare_delete_project(self.actor, p.id, expected_revision=1)
            row = InspectionRecord.objects.get(pk=prepared.proof.receipt_id)
            if invalidation == 'sequence':
                row.current_sequence += 1
            elif invalidation == 'expired':
                row.facts['expires_at'] = (timezone.now() - timedelta(seconds=1)).isoformat()
            elif invalidation == 'target':
                row.target_id = uuid.uuid4()
            else:
                row.delete()
            if invalidation != 'missing':
                row.save()
            with self.subTest(invalidation=invalidation), self.assertRaises(services.UnsafeRemoval):
                self.api.commit_delete_project(self.actor, prepared)
        self.assertTrue(self.api.project_is_current(p.id))

    def test_authorization_and_revision_rechecked_after_external_inspection(self):
        p = self.project()
        self.guard.after_inspection = lambda: setattr(self.auth, 'projects', set())
        with self.assertRaises(PermissionError):
            self.api.delete_project(self.actor, p.id, expected_revision=1)
        self.auth.projects = None
        self.guard.after_inspection = lambda: self.api.set_project_limit(self.actor, p.id, 0, expected_revision=1)
        with self.assertRaises(services.Conflict):
            self.api.delete_project(self.actor, p.id, expected_revision=1)
        self.assertTrue(self.api.project_is_current(p.id))

    def test_commit_is_composable_and_outer_result_failure_rolls_back_retirement(self):
        p = self.project()
        prepared = self.api.prepare_delete_project(self.actor, p.id, expected_revision=1)
        # Внешний inspection больше не вызывается; Operations пишет result/outbox
        # в той же outer transaction. У каталога нет callback из Actor/payload.
        self.guard.inspect_project = lambda *_: self.fail('external I/O during prepared commit')
        with self.assertRaisesRegex(ValueError, 'outbox failed'):
            with transaction.atomic():
                retired = self.api.commit_delete_project(self.actor, prepared)
                self.assertIsNotNone(retired.retired_at)
                raise ValueError('outbox failed')
        self.assertTrue(self.api.project_is_current(p.id))
        self.assertIsNotNone(self.api.commit_delete_project(self.actor, prepared).retired_at)
        fresh = self.project()
        with self.assertRaises(services.Conflict):
            self.api.commit_delete_project(self.actor, prepared)
        self.assertTrue(self.api.project_is_current(fresh.id))

    def test_retirement_and_reconfiguration_serialize_on_real_parent_lock(self):
        for node in (False, True):
            target = self.node() if node else self.project()
            prepare = self.api.prepare_forget_node if node else self.api.prepare_delete_project
            commit = self.api.commit_forget_node if node else self.api.commit_delete_project
            prepared = prepare(self.actor, target.id, expected_revision=1)
            entered, attempted = threading.Event(), threading.Event()
            def locked_validation():
                # Независимое соединение должно наблюдать именно parent lock
                # во время SQL receipt validation, не только конечный CAS.
                def probe():
                    try:
                        with transaction.atomic():
                            model = models.Node if node else models.Project
                            model.objects.select_for_update(nowait=True).get(pk=target.id)
                        return 'unlocked'
                    except DatabaseError as error:
                        self.assertIn('could not obtain lock', str(error))
                        return 'locked'
                    finally:
                        connections.close_all()
                with ThreadPoolExecutor(max_workers=1) as observer:
                    self.assertEqual(observer.submit(probe).result(timeout=5), 'locked')
                entered.set()
                self.assertTrue(attempted.wait(5))
            self.guard.after_validation = locked_validation
            def retire():
                try:
                    return commit(self.actor, prepared)
                finally:
                    connections.close_all()
            def reconfigure():
                try:
                    self.assertTrue(entered.wait(5))
                    attempted.set()
                    try:
                        if node:
                            self.node(node_id=target.id, expected_revision=1, ssh_host='new.example')
                        else:
                            self.api.replace_project_origin(self.actor, target.id, 'https://git.example/new.git', expected_revision=1)
                    except services.Conflict:
                        return 'conflict'
                    return 'changed'
                finally:
                    connections.close_all()
            with ThreadPoolExecutor(max_workers=2) as pool:
                retirement, change = pool.submit(retire), pool.submit(reconfigure)
                self.assertIsNotNone(retirement.result(timeout=10).retired_at)
                self.assertEqual(change.result(timeout=10), 'conflict')
            self.guard.after_validation = lambda: None

    def test_new_incarnation_concurrent_registration_is_unique_and_old_proof_cannot_follow(self):
        p = self.project()
        prepared = self.api.prepare_delete_project(self.actor, p.id, expected_revision=1)
        self.api.commit_delete_project(self.actor, prepared)
        barrier = threading.Barrier(2)
        def create(_):
            try:
                barrier.wait(5)
                return self.project()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.map(create, (0, 1))
        self.assertEqual(a.id, b.id)
        self.assertNotEqual(a.id, p.id)
        with self.assertRaises(services.Conflict):
            self.api.commit_delete_project(self.actor, prepared)
        self.assertEqual(self.api.get_project(self.actor, a.id), a)

    def test_retired_targets_reject_all_new_metadata_writes_and_live_resolvers(self):
        p, n = self.project(), self.node()
        policy = self.api.pin_project_policy(self.actor, p.id, b'source', source_commit='a'*40,
            source_ref='main', provenance='fixture', cpu=100, memory_mb=100, disk_mb=100, expected_revision=1)
        image = self.api.create_image_revision(self.actor, p.id, policy.id, source='fixture')
        client = self.api.register_client(self.actor, label='fixture')
        self.api.delete_project(self.actor, p.id, expected_revision=2)
        self.api.forget_node(self.actor, n.id, expected_revision=1)
        for mutation in (
            lambda: self.api.replace_project_origin(self.actor, p.id, 'https://git.example/new.git', expected_revision=3),
            lambda: self.api.set_project_limit(self.actor, p.id, 0, expected_revision=3),
            lambda: self.api.pin_project_policy(self.actor, p.id, b'new', source_commit='a'*40,
                source_ref='main', provenance='fixture', cpu=100, memory_mb=100, disk_mb=100, expected_revision=3),
            lambda: self.api.create_image_revision(self.actor, p.id, policy.id, source='new'),
            lambda: self.api.bind_workspace(self.actor, client.id, 'new', p.id, integration_branch='main', expected_revision=0),
            lambda: self.node(node_id=n.id, expected_revision=2),
            lambda: self.api.set_node_eligibility(self.actor, n.id, True, expected_revision=2),
            lambda: self.api.set_deployment_membership(self.actor, n.id, True, expected_revision=2),
            lambda: self.api.set_node_projects(self.actor, n.id, (), expected_revision=2),
            lambda: self.api.record_node_application(self.actor, n.id, target_revision=2, receipt='late'),
            lambda: self.api.observe_node(self.actor, n.id, eligible=True, source='late', observed_at=timezone.now()),
            lambda: self.api.set_node_settings(self.actor, n.id, {'log_level': 'debug'}, expected_revision=1),
            lambda: self.api.record_settings_application(self.actor, node_id=n.id, target_revision=1, receipt='late'),
            lambda: self.api.set_image_intent(self.actor, n.id, p.id, image.id, expected_revision=0),
            lambda: self.api.observe_image(self.actor, n.id, p.id, image.id, available=True, source='late',
                observed_at=timezone.now(), receipt='late', expected_revision=1),
        ):
            with self.assertRaises(services.Conflict):
                mutation()
        with self.assertRaises(models.Node.DoesNotExist):
            self.api.get_node_settings(self.actor, n.id)
        with self.assertRaises(models.Node.DoesNotExist):
            self.api.get_effective_settings(self.actor, node_id=n.id)
        fresh = self.node()
        with self.assertRaises(ValueError):
            self.api.set_node_projects(self.actor, fresh.id, (p.id,), expected_revision=1)
        # Retained policy — явная history query, не активный target lookup.
        self.assertEqual(self.api.get_policy(self.actor, policy.id).project_id, p.id)

    def test_node_proof_cannot_survive_membership_or_address_reconfiguration(self):
        n = self.node()
        prepared = self.api.prepare_forget_node(self.actor, n.id, expected_revision=1)
        newer = self.node(node_id=n.id, expected_revision=1, ssh_host='replacement.example')
        with self.assertRaises(services.Conflict):
            self.api.commit_forget_node(self.actor, prepared)
        prepared = self.api.prepare_forget_node(self.actor, n.id, expected_revision=newer.revision)
        self.api.set_deployment_membership(self.actor, n.id, True, expected_revision=newer.revision)
        with self.assertRaises(services.Conflict):
            self.api.commit_forget_node(self.actor, prepared)
        self.assertTrue(self.api.node_is_current(n.id))

    def test_constraints_protect_active_names_and_tombstones_without_partial_indexes(self):
        p = self.project()
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.Project.objects.create(origin=p.origin, name=p.name)
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.Project.objects.filter(pk=p.id).update(retired_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.Project.objects.filter(pk=p.id).update(identity_slot=uuid.uuid4())
        self.api.delete_project(self.actor, p.id, expected_revision=1)
        self.project()
        self.assertEqual(models.Project.objects.filter(name=p.name).count(), 2)
