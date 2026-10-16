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
# RED: PG16 — build REST 404; PG17 — replay dispatch, busy skip, force и driver;
#      PG16/17 — shared target fence, unknown/quiescent, restart queue, immutable
#      inputs, mixed host/PVE и long-build deadline показали отдельные отказы.
# RESULT: реальные PG16/17 crash/claim/recovery и regressions зелёные;
#         отдельная UID/Unix fixture проверяет настоящий owned subprocess.
# STATUS: FIXED — see #648; реальная новая PVE установка не проверена.
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

    def setup_build(self, *, mode='rebuild', targets=2, driver='pve'):
        from mop.server.controller.runtime import builds_service
        from mop.server.builds.commands import parse
        from mop.server.sessions.models import Assignment, ExecutionIncarnation
        from mop.server.catalog.models import Node
        self.permission()
        Node.objects.filter(pk=self.node.pk).update(driver=driver)
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
        from mop.server.catalog.models import Project, Node, NodeProject, ProjectPolicySnapshot
        project = Project.objects.create(name='other', origin='https://example.invalid/other.git')
        node = Node.objects.create(name='other', driver='pve', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid', bootstrap_address='example.invalid')
        NodeProject.objects.create(node=node, project=project)
        policy = ProjectPolicySnapshot.objects.create(project=project, sequence=1, content=b'{}',
            digest=hashlib.sha256(b'{}').hexdigest(), source_commit='b'*40, source_ref='main', provenance='test', cpu=1, memory_mb=64, disk_mb=64)
        other = self.builds.submit(self.actor, replace(self.command, request_id=uuid4(), project_id=project.pk,
            policy_id=policy.pk, target_nodes=(node.pk,)))
        context = multiprocessing.get_context('spawn')
        ready, output = context.Event(), context.Queue()
        # Эти Operation не делят project/node: единственный общий ресурс — mirror.
        children = [context.Process(target=claim, args=(connections['default'].settings_dict['NAME'],
            os.environ['MOP_CONTROLLER_BOOTSTRAP'], UUID(operation_id), ready, output)) for operation_id in (self.operation['id'], other['id'])]
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

    def test_privileged_root_gate_consumes_dispatch_once_but_retains_inspection(self):
        self.setup_build(targets=0)
        claim = self.builds.claim(UUID(self.operation['id']))
        self.builds._plan(claim)
        ticket, fresh, result = self.builds._intent(claim, 'build')
        from mop.server.access.services import Denied
        request = dict(intent_id=ticket.id, step_id=claim.step_id, token=claim.token, epoch=claim.epoch)
        self.assertEqual(self.builds.resolve_ticket(**request, verb='apply').id, ticket.id)
        with self.assertRaisesRegex(Denied, 'dispatch|replay'):
            self.builds.resolve_ticket(**request, verb='apply')
        self.assertEqual(self.builds.resolve_ticket(**request, verb='inspect').id, ticket.id)

    def test_fresh_available_image_does_not_stop_a_busy_clone(self):
        self.setup_build(mode='missing')
        self.fake.available = True
        self.fake.targets[0]['free'] = False
        result = self.builds.run(UUID(self.operation['id']))
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual([x[0] for x in self.fake.calls], ['announce'])

    def crash_after(self, stage):
        self.setup_build(targets=0 if stage in ('build','announce') else 2)
        self.fake.crash = stage
        with self.assertRaises(SimulatedCrash):
            self.builds.run(UUID(self.operation['id']))
        self.fake.crash = None
        result = self.builds.run(UUID(self.operation['id']))
        self.assertEqual(result['state'], 'succeeded')
        expected = 2 if stage in ('clear','wipe','restore') else 1
        self.assertEqual(len([x for x in self.fake.calls if x[0] == stage]), expected)

    def test_crash_after_clear(self):
        self.crash_after('clear')

    def test_crash_after_wipe(self):
        self.crash_after('wipe')

    def test_crash_after_build(self):
        self.crash_after('build')

    def test_crash_after_restore(self):
        self.crash_after('restore')

    def test_crash_after_announce(self):
        self.crash_after('announce')

    def test_force_safety_needs_explicit_server_permission_not_just_build_right(self):
        from django.test import Client
        self.permission()
        body = self.body(self.policy())
        body['force_safety'] = True
        response = Client(HTTP_AUTHORIZATION='Token '+self.token).post(
            '/api/v1/projects/'+str(self.project.pk)+'/commands/build', body, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.m.Operation.objects.exists())

    def test_db_outage_after_physical_clear_preserves_intent_and_readbacks_once(self):
        from unittest.mock import patch
        from django.db import OperationalError
        self.setup_build()
        original = self.builds._record
        def outage(claim, ticket, result):
            if ticket.stage == 'clear':
                raise OperationalError('fixture outage after external clear')
            return original(claim, ticket, result)
        with patch.object(self.builds, '_record', side_effect=outage):
            with self.assertRaises(OperationalError):
                self.builds.run(UUID(self.operation['id']))
        self.assertEqual(self.builds.run(UUID(self.operation['id']))['state'], 'succeeded')
        self.assertEqual(len([x for x in self.fake.calls if x[0] == 'clear']), 2)

    def test_driver_cannot_change_under_accepted_pinned_plan(self):
        self.setup_build(targets=0)
        from mop.server.catalog.models import Node
        Node.objects.filter(pk=self.node.pk).update(driver='host')
        result = self.builds.run(UUID(self.operation['id']))
        self.assertNotEqual(result['state'], 'succeeded')
        self.assertEqual(self.fake.calls, [])

    def test_host_strategy_does_not_build_templates_or_wipe_host(self):
        self.setup_build(targets=0, driver='host')
        self.assertEqual(self.builds.run(UUID(self.operation['id']))['state'], 'succeeded')
        self.assertEqual(self.fake.calls, [])

    def test_runtime_worker_dispatches_build_outside_event_loop_and_owns_cancelled_waiter(self):
        import asyncio
        from threading import Event
        from mop.server.controller.runtime import Runtime
        from mop.server.operations.worker import Worker
        self.setup_build()
        entered, release = Event(), Event()
        self.fake.before_clear = lambda: (entered.set(), release.wait(timeout=10))
        async def exercise():
            runtime = Runtime(blocking_limit=1, effect_limit=1)
            worker = Worker(runtime, self.builds.operations, None, image_builds=self.builds)
            waiter = asyncio.create_task(worker.run_one(UUID(self.operation['id'])))
            for _ in range(500):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(entered.is_set())
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertGreater(runtime.pending, 0)
            release.set()
            await runtime.aclose(timeout=10)
        asyncio.run(exercise())
        self.assertEqual(self.builds.operations.status(self.actor, UUID(self.operation['id']))['state'], 'succeeded')

    def test_assignment_release_cannot_cross_unresolved_build_target_claim(self):
        self.setup_build()
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label='execution', codename='assign_work'))
        from mop.server.sessions.services import Conflict
        from mop.server.controller.runtime import assignments_service
        claim = self.builds.claim(UUID(self.operation['id']))
        self.builds._plan(claim)
        with self.assertRaisesRegex(Conflict, 'unresolved'):
            assignments_service().release(self.actor, self.fake.targets[0]['target_id'], expected_revision=1)

    def test_unknown_but_quiescent_is_not_a_final_success_or_failed_receipt(self):
        from unittest.mock import patch
        from mop.server.builds.contracts import StageResult
        self.setup_build()
        apply = self.fake.apply
        def incomplete(ticket):
            result = apply(ticket)
            return StageResult('unknown', True, 'process stopped; effect still unknown') if ticket.stage == 'wipe' else result
        with patch.object(self.fake, 'apply', side_effect=incomplete):
            self.assertEqual(self.builds.run(UUID(self.operation['id']))['state'], 'unknown')
        self.assertEqual(self.builds.run(UUID(self.operation['id']))['state'], 'succeeded')

    def test_restarted_worker_discovers_unresolved_build_in_the_same_operation_queue(self):
        self.setup_build(targets=0)
        self.fake.crash = 'build'
        with self.assertRaises(SimulatedCrash):
            self.builds.run(UUID(self.operation['id']))
        self.m.Step.objects.filter(operation_id=self.operation['id']).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.assertIn(UUID(self.operation['id']), self.builds.operations.pending())

    def test_build_outbox_uses_project_identity_not_anchor_node(self):
        self.setup_build(targets=0)
        events = []
        self.builds.operations.publish_one(events.append)
        self.assertEqual(events[0]['target']['kind'], 'project')
        self.assertEqual(events[0]['target']['id'], str(self.project.pk))

    def test_pinned_build_inputs_cannot_be_edited_through_orm_save_or_queryset(self):
        self.setup_build(targets=0)
        from mop.server.builds.models import BuildRun
        run = BuildRun.objects.get(operation_id=self.operation['id'])
        run.source_commit = 'b'*40
        with self.assertRaisesRegex(ValueError, 'immutable'):
            run.save(update_fields=('source_commit',))
        with self.assertRaisesRegex(ValueError, 'immutable'):
            BuildRun.objects.filter(pk=run.pk).update(mode='update')

    def test_mixed_host_and_pve_scope_builds_only_the_container_targets(self):
        self.setup_build(targets=0)
        from mop.server.catalog.models import Node, NodeProject
        node = Node.objects.create(name='host-only', driver='host', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid', bootstrap_address='example.invalid')
        NodeProject.objects.create(node=node, project=self.project)
        self.builds.operations.cancel(self.actor, UUID(self.operation['id']), expected_revision=1)
        command = replace(self.command, request_id=uuid4(), target_nodes=tuple(sorted((node.pk,self.node.pk), key=str)))
        operation = self.builds.submit(self.actor, command)
        self.assertEqual(self.builds.run(UUID(operation['id']))['state'], 'succeeded')
        self.assertEqual(self.fake.build_input.target_nodes, (self.node.pk,))

    def test_real_ansible_build_has_bounded_long_deadline_not_node_five_minute_cap(self):
        self.setup_build(targets=0)
        command = replace(self.command, request_id=uuid4(), not_after=timezone.now()+timedelta(hours=1))
        operation = self.builds.submit(self.actor, command)
        self.assertEqual(operation['state'], 'accepted')
        self.assertEqual(self.m.Operation.objects.get(pk=operation['id']).not_after, command.not_after)

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
