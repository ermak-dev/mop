"""Durable stop через настоящий HTTP scheduler и отдельный управляемый процесс."""
from datetime import timedelta
from dataclasses import astuple
from uuid import UUID, uuid4
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from _restart_vertical_cases import RestartVertical


# FEATURE: stop job — конечный handler общего Operation/Step, не DELETE по имени.
# HYPOTHESIS: restart-only route не принимает stop и не охраняет scheduler CAS.
# SPEC: текущие actor/owner/role/grant, точный JobModifyIndex, unknown не replay,
#       физическая quiescence до снятия shared resource fence; clone не удаляется.
# RED: PG16 route 404; PG17 scheduler-terminal ошибочно succeeded при живом child;
#      PG17 activate прошёл при unknown; PG16 worker не видел stop; потеря physical
#      port всё ещё выполняла POST; PG17 restore принимал старый enrollment;
#      PG16 result возвращал revision 2 вместо committed 3.
# SOLUTION: конечный StopJobCommand, общий Step/claims, conditional JobModifyIndex,
#           отдельный addressed body proof, restore/owner/grant gates и read-only
#           reconciliation. Spawn fixtures не наследуют DB sockets/locks родителя;
#           external approval proof передаётся value и восстанавливается после setup.
# RESULT: PG16/17 — по 118 narrow/related checks, из них 21 stop check; HTTP CAS
#         reference scheduler завершает собственный child, dirty Git clone остаётся.
#         Это не native Nomad/PVE/body-adapter acceptance и не полный gate #625.
# STATUS: FIXED — see #625
@override_settings(ALLOWED_HOSTS=['testserver'])
class StopVertical(TransactionTestCase):
    serialized_rollback = True
    setUp = RestartVertical.setUp
    fixture = RestartVertical.fixture
    body = RestartVertical.body

    def test_restored_generation_requires_new_external_job_enrollment(self):
        body = self.prepare()
        from mop.server.controller.models import RestoreGuard
        RestoreGuard.objects.filter(pk=1).update(generation=uuid4())
        response = self.submit(body)
        self.assertEqual(response.status_code,503,response.content)
        self.assertEqual(self.scheduler.writes(),0)
        self.assertTrue(self.scheduler.alive())

    def test_missing_body_inspector_is_unsupported_before_admission(self):
        body = self.prepare()
        self.scheduler.adapter.body_inspector = None
        response = self.submit(body)
        self.assertEqual(response.status_code,503,response.content)
        from mop.server.operations.models import Operation
        self.assertEqual(Operation.objects.count(),0)
        self.assertTrue(self.scheduler.alive())

    def test_lost_physical_capability_never_writes_even_with_prepared_ticket(self):
        body = self.prepare()
        operation = UUID(self.submit(body).json()['operation']['id'])
        ticket = self.ops.stops.claim(operation)
        self.assertTrue(self.ops.stops.prepare(ticket))
        self.scheduler.adapter.body_inspector = None
        proof = self.scheduler.adapter.stop_job(ticket)
        self.assertEqual(self.scheduler.writes(),0)
        self.assertTrue(self.scheduler.alive())
        self.assertEqual(proof.outcome,'failed')

    def test_machine_credential_never_inherits_human_admin_stop_rights(self):
        body = self.prepare()
        response = self.machine.post('/api/v1/puppets/'+str(self.target.pk)+'/commands/stop-job',body,
            content_type='application/json',secure=True)
        self.assertEqual(response.status_code,403,response.content)
        self.assertEqual(self.scheduler.writes(),0)

    def test_unknown_fences_new_stop_and_assignment_activation(self):
        body = self.prepare()
        self.scheduler.mode('live-after-stop')
        operation = UUID(self.submit(body).json()['operation']['id'])
        result = self.ops.stops.run(operation)
        self.assertEqual(result['state'],'unknown',result)
        self.assertTrue(self.scheduler.alive())
        second = UUID(self.submit(self.body(params={})).json()['operation']['id'])
        self.assertIsNone(self.ops.stops.claim(second))
        from mop.server.controller.runtime import assignments_service
        from mop.server.sessions.services import Conflict
        with self.assertRaises(Conflict):
            assignments_service().activate(self.actor,self.target.id,expected_revision=self.target.revision,
                executor_epoch=uuid4())
        self.assertEqual(self.scheduler.writes(),1)

    def test_wrong_incarnation_body_proof_remains_unknown(self):
        body = self.prepare()
        from dataclasses import replace
        inspector = self.scheduler.adapter.body_inspector
        original = inspector.inspect_stopped
        def incorrect(ticket,allocations):
            return replace(original(ticket,allocations),incarnation_id=uuid4())
        with patch.object(inspector,'inspect_stopped',incorrect):
            operation = UUID(self.submit(body).json()['operation']['id'])
            result = self.ops.stops.run(operation)
        self.assertEqual(result['state'],'unknown',result)
        self.assertTrue(result['resources_held'])
        self.assertFalse(self.scheduler.alive())

    def test_migrations_match_registered_models(self):
        from django.core.management import call_command
        call_command('makemigrations','operations',check=True,dry_run=True,verbosity=0)

    def test_unbound_scheduler_refuses_before_durable_admission(self):
        self.fixture()
        from mop.server.operations.models import Operation
        body = self.body(params={})
        response = self.client.post('/api/v1/puppets/'+str(self.target.pk)+'/commands/stop-job', body,
            content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(Operation.objects.count(), 0)

    def prepare(self):
        self.fixture()
        from _stop_fixture import SchedulerFixture
        from mop.server.controller.stopbinding import service
        self.scheduler = SchedulerFixture(self.target, self.epoch)
        self.addCleanup(self.scheduler.close)
        self.ops = service(grants=self.grants, scheduler=self.scheduler.adapter)
        self.ops.stops.enroll(self.actor, self.target.id, job_id=self.scheduler.job_id, namespace='default')
        self.addCleanup(patch.stopall)
        patch('mop.server.controller.operationviews.stop_api', lambda: self.ops).start()
        return self.body(params={})

    def submit(self, body):
        return self.client.post('/api/v1/puppets/'+str(self.target.id)+'/commands/stop-job', body,
            content_type='application/json', secure=True)

    def test_http_worker_physical_stop_result_outbox_and_retry(self):
        body = self.prepare()
        dirty = self.scheduler.clone/'untracked-work'
        dirty.write_text('preserve human work')
        tracked = self.scheduler.clone/'notes.txt'
        tracked.write_text('unfinished human changes')
        import subprocess
        before = subprocess.check_output(['git','-C',str(self.scheduler.clone),'status','--porcelain=v1'])
        self.assertIn(b' M notes.txt',before)
        self.assertIn(b'?? untracked-work',before)
        response = self.submit(body)
        self.assertEqual(response.status_code, 202, response.content)
        operation = UUID(response.json()['operation']['id'])
        self.assertTrue(self.scheduler.alive())
        self.ops.stops.run(operation)
        result = self.ops.status(self.actor, operation)
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertFalse(result['resources_held'])
        self.assertEqual(result['target']['revision'],str(self.target.revision+1))
        self.assertFalse(self.scheduler.alive())
        self.assertTrue(self.scheduler.request('GET')['all_allocations'])
        self.assertEqual(dirty.read_text(), 'preserve human work')
        self.assertEqual(tracked.read_text(),'unfinished human changes')
        self.assertEqual(subprocess.check_output(['git','-C',str(self.scheduler.clone),'status','--porcelain=v1']),before)
        self.assertEqual(self.scheduler.writes(), 1)
        self.assertEqual(self.submit(body).json()['operation']['id'], str(operation))
        self.ops.stops.run(operation)
        self.assertEqual(self.scheduler.writes(), 1)
        from mop.server.operations.models import AuditEvent, OutboxEvent, EffectReceipt
        self.assertEqual(AuditEvent.objects.filter(operation_id=operation).count(),
                         OutboxEvent.objects.filter(operation_id=operation).count())
        self.assertEqual(EffectReceipt.objects.filter(step__operation_id=operation, applied=True).count(), 1)
        self.assertEqual(OutboxEvent.objects.filter(operation_id=operation).latest('revision').target_revision,self.target.revision+1)
        from mop.server.sessions.models import ExecutionIncarnation
        self.assertFalse(ExecutionIncarnation.objects.get(pk=self.target.current_incarnation_id).accepting)

    def test_shared_runtime_worker_discovers_stop_and_owns_physical_effect(self):
        body = self.prepare()
        operation = UUID(self.submit(body).json()['operation']['id'])
        self.assertIn(operation,self.ops.pending(limit=8))
        from mop.server.controller.runtime import Runtime
        from mop.server.operations.worker import Worker
        import asyncio
        async def work():
            runtime = Runtime(database_limit=2,blocking_limit=2,effect_limit=2)
            try:
                return await Worker(runtime,self.ops,None).run_one(operation)
            finally:
                await runtime.aclose()
        result = asyncio.run(work())
        self.assertEqual(result['state'],'succeeded',result)
        self.assertFalse(self.scheduler.alive())
        self.assertEqual(self.scheduler.writes(),1)

    def test_lost_scheduler_response_reconciles_without_second_write(self):
        body = self.prepare()
        self.scheduler.mode('lose-response')
        response = self.submit(body)
        operation = UUID(response.json()['operation']['id'])
        self.ops.stops.run(operation)
        result = self.ops.status(self.actor, operation)
        self.assertEqual(result['state'], 'unknown', result)
        self.assertTrue(result['resources_held'])
        self.assertEqual(self.scheduler.writes(), 1)
        self.ops.stops.run(operation)
        self.assertEqual(self.scheduler.writes(), 1)
        self.ops.stops.reconcile(self.actor, operation)
        result = self.ops.status(self.actor, operation)
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertFalse(result['resources_held'])
        self.assertEqual(self.scheduler.writes(), 1)

    def test_index_replacement_is_never_stopped_or_reported_confirmed(self):
        body = self.prepare()
        operation = UUID(self.submit(body).json()['operation']['id'])
        self.scheduler.mode('replace')
        self.ops.stops.run(operation)
        result = self.ops.status(self.actor, operation)
        self.assertEqual(result['state'], 'failed', result)
        self.assertFalse(result['resources_held'])
        self.assertTrue(self.scheduler.alive())
        self.assertEqual(self.scheduler.writes(), 0)

    def test_foreign_owner_and_revoked_grant_never_reach_scheduler(self):
        body = self.prepare()
        from mop.server.sessions.models import Assignment
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user('foreign')
        Assignment.objects.filter(pk=self.target.id).update(owner=other)
        response = self.submit(body)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(self.scheduler.writes(), 0)

    def test_scheduler_status_does_not_prove_body_process_quiescence(self):
        body = self.prepare()
        self.scheduler.mode('false-terminal')
        operation = UUID(self.submit(body).json()['operation']['id'])
        self.ops.stops.run(operation)
        result = self.ops.status(self.actor,operation)
        self.assertEqual(result['state'],'unknown',result)
        self.assertTrue(result['resources_held'])
        self.assertTrue(self.scheduler.alive())

    def test_grant_revoked_after_preflight_refuses_before_external_write(self):
        body = self.prepare()
        operation = UUID(self.submit(body).json()['operation']['id'])
        original = self.scheduler.adapter.observe
        def observe(job):
            index = original(job)
            grant = self.grants.get(self.actor,self.target.grant_id)
            self.grants.revoke(self.actor,grant.id,expected_revision=grant.revision)
            return index
        with patch.object(self.scheduler.adapter,'observe',observe):
            self.ops.stops.run(operation)
        from mop.server.operations.models import Operation
        self.assertEqual(Operation.objects.get(pk=operation).state,'refused')
        self.assertTrue(self.scheduler.alive())
        self.assertEqual(self.scheduler.writes(),0)

    def test_changed_owner_identity_is_rechecked_before_external_write(self):
        body = self.prepare()
        operation = UUID(self.submit(body).json()['operation']['id'])
        self.user.email = ''
        self.user.save(update_fields=('email',))
        self.ops.stops.run(operation)
        from mop.server.operations.models import Operation
        self.assertEqual(Operation.objects.get(pk=operation).state,'refused')
        self.assertTrue(self.scheduler.alive())
        self.assertEqual(self.scheduler.writes(),0)

    def test_same_request_concurrent_processes_commit_one_operation(self):
        body = self.prepare()
        from mop.server.operations.commands import parse_stop_job
        command = parse_stop_job(self.target.id,body)
        import multiprocessing
        import os
        from django.db import connections
        from _stop_process import submit
        context = multiprocessing.get_context('spawn')
        ready,result = context.Event(),context.Queue()
        config = (connections['default'].settings_dict['NAME'],os.environ['MOP_CONTROLLER_BOOTSTRAP'],
            self.scheduler.origin,self.scheduler.adapter.scheduler_id,astuple(self.grants.approvals.proof))
        processes = [context.Process(target=submit,args=(*config,self.actor,command,ready,result)) for _ in range(2)]
        for process in processes:
            process.start()
        ready.set()
        values = [result.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode,0)
        self.assertEqual(values[0],values[1])
        from mop.server.operations.models import Operation
        self.assertEqual(Operation.objects.count(),1)
        self.assertEqual(self.scheduler.writes(),0)

    def test_request_key_conflict_and_payload_authority_are_refused(self):
        body = self.prepare()
        first = self.submit(body)
        self.assertEqual(first.status_code,202,first.content)
        changed = {**body,'not_after':(timezone.now()+timedelta(minutes=1)).isoformat()}
        self.assertEqual(self.submit(changed).status_code,409)
        self.assertEqual(self.submit({**body,'owner':'forged'}).status_code,400)
        self.assertEqual(self.submit({**body,'params':{'job_id':'foreign'}}).status_code,400)
        self.assertEqual(self.scheduler.writes(),0)

    def test_independent_processes_can_claim_only_one_stop_effect(self):
        body = self.prepare()
        operation = UUID(self.submit(body).json()['operation']['id'])
        import multiprocessing
        import os
        from django.db import connections
        from _stop_process import claim
        context = multiprocessing.get_context('spawn')
        ready,result = context.Event(),context.Queue()
        config = (connections['default'].settings_dict['NAME'],os.environ['MOP_CONTROLLER_BOOTSTRAP'],
            self.scheduler.origin,self.scheduler.adapter.scheduler_id,astuple(self.grants.approvals.proof))
        processes = [context.Process(target=claim,args=(*config,operation,ready,result)) for _ in range(2)]
        for process in processes:
            process.start()
        ready.set()
        values = [result.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode,0)
        self.assertEqual(sum(value is not None for value in values),1,values)
        self.assertEqual(self.scheduler.writes(),0)
        self.assertTrue(self.scheduler.alive())

    def test_worker_crash_after_physical_stop_is_read_only_reconciled(self):
        body = self.prepare()
        operation = UUID(self.submit(body).json()['operation']['id'])
        import multiprocessing
        import os
        from django.db import connections
        from _stop_process import crash
        context = multiprocessing.get_context('spawn')
        process = context.Process(target=crash,args=(connections['default'].settings_dict['NAME'],
            os.environ['MOP_CONTROLLER_BOOTSTRAP'],self.scheduler.origin,self.scheduler.adapter.scheduler_id,astuple(self.grants.approvals.proof),operation))
        process.start()
        process.join(30)
        self.assertEqual(process.exitcode,73)
        self.assertFalse(self.scheduler.alive())
        self.assertEqual(self.scheduler.writes(),1)
        self.ops.stops.run(operation)
        self.assertEqual(self.scheduler.writes(),1)
        result = self.ops.stops.reconcile(self.actor,operation)
        self.assertEqual(result['state'],'succeeded',result)
        self.assertFalse(result['resources_held'])
        self.assertEqual(self.scheduler.writes(),1)

    def test_revoked_actor_between_admission_and_effect_is_refused(self):
        body = self.prepare()
        operation = UUID(self.submit(body).json()['operation']['id'])
        from mop.server.access.services import revoke_token
        revoke_token(self.actor)
        self.ops.stops.run(operation)
        from mop.server.operations.models import Operation
        self.assertEqual(Operation.objects.get(pk=operation).state, 'refused')
        self.assertTrue(self.scheduler.alive())
        self.assertEqual(self.scheduler.writes(), 0)
