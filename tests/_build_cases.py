"""Сборка #648: реальные commit/fences, внешние эффекты только через ports."""
from dataclasses import replace
from datetime import timedelta
import hashlib
from uuid import uuid4, UUID

from django.contrib.auth.models import Permission
from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from _operation_cases import DurableOperations


# FEATURE: образ — конечный восстанавливаемый process manager общей Operation.
# HYPOTHESIS: отсутствие DRF build и durable intents теряет частично снятые тела.
# SOLUTION: pinned BuildRun, полный plan до clear, receipts и fenced compensation.
@override_settings(ALLOWED_HOSTS=['testserver'])
class ImageBuilds(TransactionTestCase):
    serialized_rollback = True
    setUp = DurableOperations.setUp

    def policy(self):
        from mop.server.catalog.models import ProjectPolicySnapshot
        return ProjectPolicySnapshot.objects.create(project=self.project, sequence=1,
            content=b'{}', digest=hashlib.sha256(b'{}').hexdigest(), source_commit='a'*40,
            source_ref='feature/image', provenance='test', cpu=1, memory_mb=64, disk_mb=64)

    def body(self, policy):
        return dict(schema_version=1, epoch=str(self.epoch), request_id=str(uuid4()),
            expected_revision='1', not_after=(timezone.now()+timedelta(minutes=2)).isoformat(),
            policy_id=str(policy.pk), mode='rebuild', target_nodes=[str(self.node.pk)], force_safety=False)

    def permission(self):
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label='catalog', codename='build_project'))

    def test_rest_acceptance_pins_manifest_and_replays_common_request(self):
        from django.test import Client
        self.permission()
        body = self.body(self.policy())
        client = Client(HTTP_AUTHORIZATION='Token '+self.token)
        url = '/api/v1/projects/'+str(self.project.pk)+'/commands/build'
        response = client.post(url, body, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 202, response.content)
        first = response.json()['operation']
        self.assertEqual(client.post(url, body, content_type='application/json', secure=True).json()['operation']['id'], first['id'])
        from mop.server.builds.models import BuildRun
        run = BuildRun.objects.get(operation_id=first['id'])
        self.assertEqual((run.source_commit, run.source_ref, run.manifest_digest), ('a'*40, 'feature/image', hashlib.sha256(b'{}').hexdigest()))
        self.assertEqual(self.m.Step.objects.get(operation_id=first['id']).name, 'image_build')
        self.assertEqual(client.get(first_url := '/api/v1/operations/'+first['id'], secure=True).status_code, 200, first_url)
        body['mode'] = 'update'
        self.assertEqual(client.post(url, body, content_type='application/json', secure=True).status_code, 409)

    def setup_build(self, *, mode='rebuild', targets=2):
        from mop.server.controller.runtime import builds_service
        from mop.server.builds.commands import parse
        from mop.server.sessions.models import Assignment, ExecutionIncarnation
        from mop.server.catalog.models import Node
        self.permission()
        Node.objects.filter(pk=self.node.pk).update(driver='pve')
        self.fake = BuildPorts()
        for _ in range(targets):
            assignment = Assignment.objects.create(project=self.project, node=self.node,
                owner=self.user, role='worker', bundle_digest='b'*64, grant_id=uuid4(),
                expires_at=timezone.now()+timedelta(hours=1))
            inc = ExecutionIncarnation.objects.create(assignment=assignment, node=self.node, executor_epoch=uuid4())
            assignment.current_incarnation = inc
            assignment.save()
            self.fake.targets.append(dict(target_id=assignment.pk, incarnation_id=inc.pk,
                node_id=self.node.pk, revision=assignment.revision, generation=assignment.generation,
                source_spec=b'{"branch":"feature/work","role":"worker"}', free=True, observed_at=timezone.now()))
        self.builds = builds_service(scheduler=self.fake, image_builder=self.fake, node_executor=self.fake)
        body = self.body(self.policy())
        body['mode'] = mode
        self.command = parse(self.project.pk, body)
        self.operation = self.builds.submit(self.actor, self.command)
        return self.operation

    def test_actual_typed_build_and_restore_uses_pinned_spec_outside_sql(self):
        self.setup_build()
        result = self.builds.run(UUID(self.operation['id']))
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual([x[0] for x in self.fake.calls], ['clear', 'wipe', 'clear', 'wipe', 'build', 'restore', 'restore', 'announce'])
        self.assertEqual(self.fake.build_input.source_commit, 'a'*40)
        self.assertEqual(self.fake.build_input.mode, 'rebuild')
        self.assertTrue(all(spec == b'{"branch":"feature/work","role":"worker"}' for spec in self.fake.restored))
        self.assertFalse(self.m.ResourceClaim.objects.filter(released_at=None).exists())

    def test_partial_clear_restores_all_attempted_targets_and_preserves_both_failures(self):
        self.setup_build()
        self.fake.fail = {'wipe': 2, 'restore': 1}
        result = self.builds.run(UUID(self.operation['id']))
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(len([x for x in self.fake.calls if x[0]=='restore']), 2)
        self.assertIn('wipe failed', result['build']['original_failure'])
        self.assertEqual(len(result['build']['compensation_failures']), 1)
        self.assertNotIn('build', [x[0] for x in self.fake.calls])

    def test_unknown_effect_never_replays_or_releases_mirror_before_readback(self):
        self.setup_build()
        self.fake.crash = 'wipe'
        with self.assertRaises(SimulatedCrash):
            self.builds.run(UUID(self.operation['id']))
        self.fake.crash = None
        self.fake.known = False
        result = self.builds.run(UUID(self.operation['id']))
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(len([x for x in self.fake.calls if x[0]=='wipe']), 1)
        self.assertTrue(self.m.ResourceClaim.objects.filter(resource_id='build:mirror', released_at=None).exists())
        self.fake.known = True
        result = self.builds.run(UUID(self.operation['id']))
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(len([x for x in self.fake.calls if x[0]=='wipe']), 2)

    def test_plan_persisted_for_every_target_before_first_destructive_effect(self):
        self.setup_build()
        from mop.server.builds.models import AffectedTargetPlan, StageIntent
        def check():
            self.assertEqual(AffectedTargetPlan.objects.count(), 2)
            self.assertTrue(StageIntent.objects.filter(stage='clear').exists())
            self.assertEqual(self.m.ResourceClaim.objects.filter(resource_id__startswith='target:', released_at=None).count(), 2)
        self.fake.before_clear = check
        self.builds.run(UUID(self.operation['id']))

    def test_restore_fence_keeps_new_incarnation_and_continues_other_targets(self):
        self.setup_build()
        from mop.server.sessions.models import Assignment
        def change():
            Assignment.objects.filter(pk=self.fake.targets[0]['target_id']).update(revision=9, generation=9)
        self.fake.after_build = change
        result = self.builds.run(UUID(self.operation['id']))
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(len(self.fake.restored), 1)
        self.assertIn('identity changed', ' '.join(result['build']['compensation_failures']))

    def test_skip_requires_fresh_external_confirmation_not_desired_orm(self):
        self.setup_build(mode='missing', targets=0)
        self.fake.available = True
        self.fake.observed_at = timezone.now()-timedelta(hours=1)
        self.assertEqual(self.builds.run(UUID(self.operation['id']))['state'], 'succeeded')
        self.assertIn('build', [x[0] for x in self.fake.calls])

    def test_fresh_matching_image_skips_build(self):
        self.setup_build(mode='missing', targets=0)
        self.fake.available = True
        self.fake.observed_at = timezone.now()
        self.assertEqual(self.builds.run(UUID(self.operation['id']))['state'], 'succeeded')
        self.assertNotIn('build', [x[0] for x in self.fake.calls])

    def test_unknown_busy_clone_refuses_before_clear_unless_explicit_force(self):
        self.setup_build()
        self.fake.targets[0]['free'] = None
        result = self.builds.run(UUID(self.operation['id']))
        self.assertEqual(result['state'], 'refused')
        self.assertEqual(self.fake.calls, [])

    def test_independent_processes_serialize_shared_mirror_and_target_claims(self):
        import multiprocessing
        import os
        from _build_process import claim
        self.setup_build()
        context = multiprocessing.get_context('spawn')
        ready, output = context.Event(), context.Queue()
        children = [context.Process(target=claim, args=(connections['default'].settings_dict['NAME'],
            os.environ['MOP_CONTROLLER_BOOTSTRAP'], UUID(self.operation['id']), ready, output)) for _ in range(2)]
        for child in children:
            child.start()
        ready.set()
        try:
            results = [output.get(timeout=25) for _ in children]
            self.assertEqual(sum(x is not None for x in results), 1)
            self.assertEqual(self.m.ResourceClaim.objects.filter(resource_id='build:mirror', released_at=None).count(), 1)
            for child in children:
                child.join(timeout=10)
                self.assertEqual(child.exitcode, 0)
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
            output.close()

    def test_denied_permission_has_no_operation_or_effect(self):
        from django.test import Client
        body = self.body(self.policy())
        response = Client(HTTP_AUTHORIZATION='Token '+self.token).post(
            '/api/v1/projects/'+str(self.project.pk)+'/commands/build', body, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.m.Operation.objects.exists())

    def test_scoped_privileged_port_cannot_be_absent_or_a_node_service(self):
        from mop.server.controller.runtime import builds_service
        from mop.server.access.services import Denied
        with self.assertRaisesRegex((Denied, RuntimeError), 'privileged|capabilit'):
            builds_service(scheduler=BuildPorts(), image_builder=None, node_executor=BuildPorts()).run(uuid4())


class SimulatedCrash(BaseException):
    pass


class BuildPorts:
    """Внешний мир отдельно от SQL; fault receipts переживают manager instance."""
    capabilities = frozenset({'scheduler', 'image_builder', 'node_executor', 'inspect', 'fenced_restore'})
    def __init__(self):
        self.targets, self.calls, self.restored = [], [], []
        self.fail, self.crash, self.known = {}, None, True
        self.before_clear = self.after_build = None
        self.available, self.observed_at = False, timezone.now()
        self.effects = {}

    def assert_io(self):
        assert not connections['default'].in_atomic_block, 'external I/O inside SQL'

    def plan(self, build):
        self.assert_io()
        from mop.server.builds.contracts import TargetSnapshot
        return tuple(TargetSnapshot(**x) for x in self.targets)

    def availability(self, build, node_id):
        self.assert_io()
        from mop.server.builds.contracts import ImageAvailability
        return ImageAvailability(node_id, build.source_digest, self.available, self.observed_at, 'external-template')

    def apply(self, intent):
        self.assert_io()
        if intent.stage == 'clear' and self.before_clear:
            self.before_clear()
        self.calls.append((intent.stage, intent.key))
        count = sum(x[0] == intent.stage for x in self.calls)
        from mop.server.builds.contracts import StageResult
        result = StageResult('failed' if self.fail.get(intent.stage) == count else 'succeeded', True,
            intent.stage+' failed' if self.fail.get(intent.stage) == count else 'confirmed')
        self.effects[intent.key] = result
        if intent.stage == 'restore' and result.outcome == 'succeeded':
            self.restored.append(intent.source_spec)
        if intent.stage == 'build':
            self.build_input = intent.build
            if self.after_build:
                self.after_build()
        if self.crash == intent.stage:
            raise SimulatedCrash()
        return result

    def inspect(self, intent):
        self.assert_io()
        from mop.server.builds.contracts import StageResult
        return self.effects.get(intent.key, StageResult('unknown', False, 'no readback')) if self.known else StageResult('unknown', False, 'no readback')
