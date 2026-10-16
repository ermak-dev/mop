"""Поведенческие проверки unified read model на независимом PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import close_old_connections, transaction
from django.test import TransactionTestCase, SimpleTestCase, Client, override_settings
from django.utils import timezone

# FEATURE: один ORM read model без Collector, с адресованными наблюдениями.
# HYPOTHESIS: сервер пока не предоставляет ни roster queries, ни source fencing.
# SPEC: READ COMMITTED, один SQL над фактами; pagination хранит bounded снимок.
# RED: PG16 — authenticated /puppets 404; stale producer/readonly ingest были приняты.
# RED: лишний persisted JSON утекал, nullable revision/cursor bound нарушали wire;
# startup/retired clock и safe authority refusal проверены отдельными red checks.
# SOLUTION: один SQL facts/receipts/vector; явные owner ports и producer enrollment.
# RESULT: 34/34 на настоящих PostgreSQL 16 и 17, включая independent COMMIT/pagination.
# STATUS: FIXED — see #626 (query/source core; full epic/live acceptance отдельно).
@override_settings(ALLOWED_HOSTS=['testserver'])
class ProjectionQueries(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        from mop.server.controller.runtime import initialize
        from mop.server.access import models as a, services as auth
        from mop.server.catalog.models import Project, Node, NodeProject
        # #642 integration: прямой Assignments после #648 терял обязательный
        # shared fence (PG16: 33 setup failures). Root даёт настоящий SQL port,
        # а не заглушку «свободно»; все прежние assertions остаются.
        from mop.server.controller.runtime import assignments_service as Assignments
        self.epoch = UUID(initialize()['epoch'])
        self.project = Project.objects.create(name='one', origin='https://example.invalid/one.git')
        self.other = Project.objects.create(name='two', origin='https://example.invalid/two.git')
        self.node = Node.objects.create(name='node', driver='host', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid', bootstrap_address='example.invalid')
        NodeProject.objects.create(node=self.node, project=self.project)
        NodeProject.objects.create(node=self.node, project=self.other)
        self.user = get_user_model().objects.create_user('reader')
        source = a.IdentitySource.objects.create(name='local', kind='local')
        a.Principal.objects.create(user=self.user, kind='human')
        a.LoginIdentity.objects.create(user=self.user, source=source, external_subject='reader')
        a.ProjectMembership.objects.create(user=self.user, project=self.project, source=source)
        self.user.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='catalog', codename='view_pool'))
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label='execution', codename='assign_work'))
        self.token = auth.issue_token(self.user.id)
        self.actor = auth.token_context(self.token)
        self.client = Client(HTTP_AUTHORIZATION='Token ' + self.token)
        self.assignment = Assignments().create(self.actor, project_id=self.project.id, node_id=self.node.id,
            role='worker', bundle_digest='a'*64, grant_id=uuid4(), expires_at=timezone.now()+timedelta(hours=1))
        self.boot = uuid4()
        self.incarnation = Assignments().activate(self.actor, self.assignment.id, expected_revision=1, executor_epoch=self.boot)
        self.service_key = auth.enroll_service('observer', self.node.id, project_ids=[self.project.id],
            permissions=['execution.read_facts', 'execution.ingest_observations'])
        self.machine = auth.token_context(self.service_key)

    def api(self):
        self.assertIn('sourceobservation', apps.all_models['execution'],
                      'normalized addressed observations must exist in the ORM')
        from mop.server.sessions import projections
        return projections

    def writer(self):
        api = self.api()
        run = api.ProjectionWriter.begin_run(self.epoch)
        writer = api.ProjectionWriter(run)
        for kind in ('state','clone','size'):
            writer.bind_producer(self.machine, target_id=self.assignment.id, incarnation_id=self.incarnation.id,
                executor_epoch=self.boot, producer_epoch=self.boot, kind=kind, expected_revision=0)
        return writer, run

    def observed(self, writer, *, sequence=1, kind='state', payload=None, **extra):
        return writer.observe(self.machine, target_id=self.assignment.id, incarnation_id=self.incarnation.id,
            producer_epoch=self.boot, sequence=sequence, kind=kind, observed_at=timezone.now(),
            payload={'session': 'busy 1 1'} if payload is None else payload, **extra)

    def test_controller_startup_invalidates_the_previous_run_without_get_repair(self):
        from unittest.mock import patch
        from django.conf import settings
        from mop.server.controller.runtime import validate_asgi_startup
        writer, run = self.writer()
        self.observed(writer)
        with override_settings(BOOTSTRAP={**settings.BOOTSTRAP, 'role':'web'}), patch('mop.server.controller.runtime.maintenance'):
            validate_asgi_startup()
        row = self.client.get('/api/v1/puppets/'+str(self.assignment.id), secure=True).json()['puppet']
        self.assertEqual(row['observed']['state']['freshness'], 'stale')
        self.assertGreater(int(row['revision']), 4)

    def test_admin_reads_the_same_scoped_projection_without_mutation_controls(self):
        import json
        from unittest.mock import patch
        from django.contrib import admin
        from django.test import RequestFactory
        from mop.server.sessions.models import Assignment
        self.assertTrue(admin.site.is_registered(Assignment), 'Admin must consume the unified read projection')
        adapter = admin.site._registry[Assignment]
        request = RequestFactory().get('/admin/execution/assignment/')
        request.user = self.user
        with patch('mop.server.controller.admin._actor', return_value=self.actor):
            self.assertEqual(list(adapter.get_queryset(request).values_list('pk', flat=True)), [self.assignment.id])
            row = adapter.get_object(request, str(self.assignment.id))
            self.assertEqual(json.loads(adapter.read_projection(row)), self.client.get(
                '/api/v1/puppets/'+str(self.assignment.id), secure=True).json()['puppet'])
            self.assertFalse(adapter.has_add_permission(request))
            self.assertFalse(adapter.has_change_permission(request, row))
            self.assertFalse(adapter.has_delete_permission(request, row))

    def test_projection_read_whitelist_also_checks_persisted_json(self):
        writer, run = self.writer()
        self.observed(writer)
        from mop.server.sessions.projection_models import SourceObservation
        SourceObservation.objects.update(payload={'session':'busy 1 1', 'terminal_bytes':'private-history'})
        import json
        row = self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']
        self.assertNotIn('private-history', json.dumps(row))
        self.assertEqual(row['observed']['state']['freshness'], 'unavailable')
        self.assertEqual(row['observed']['state']['reason'], 'source_incomplete')

    def test_queries_require_live_authority_port_not_a_cached_controller_run(self):
        api = self.api()
        cached = api.ProjectionWriter.begin_run(self.epoch)
        with self.assertRaises(TypeError):
            api.PoolQueries(cached)

    def test_unavailable_restore_authority_is_a_safe_refusal_not_html_or_empty_pool(self):
        from mop.server.controller.models import RestoreGuard
        RestoreGuard.objects.update(state='restoring')
        client = Client(HTTP_AUTHORIZATION='Token '+self.token, raise_request_exception=False)
        result = client.get('/api/v1/puppets', secure=True)
        self.assertEqual(result.status_code, 503, result.content)
        body = result.json()
        self.assertEqual(body['error']['code'], 'authority_unavailable')
        self.assertIn('restore', body['error']['message'])
        self.assertNotIn('results', body)

    def test_v1_wire_revision_fields_are_decimal_strings_not_nullable_metadata(self):
        response = self.client.get('/api/v1/puppets/'+str(self.assignment.id), secure=True)
        self.assertEqual(response.status_code, 200)
        def inspect(value):
            if type(value) is dict:
                for key, item in value.items():
                    if key == 'revision':
                        self.assertIsInstance(item, str, 'v1 entity revision must be a decimal string; nullable applied metadata is distinct')
                        self.assertRegex(item, r'^(0|[1-9][0-9]*)$')
                    inspect(item)
            elif type(value) is list:
                for item in value:
                    inspect(item)
        inspect(response.json())

    def test_snapshot_never_returns_a_vector_cursor_larger_than_the_wire_limit(self):
        from mop.server.catalog.models import Project
        from mop.server.access.models import ProjectMembership, IdentitySource
        extra = Project.objects.bulk_create([Project(name='scope-'+str(index), origin='https://example.invalid/scope-'+str(index)+'.git')
                                            for index in range(300)])
        source = IdentitySource.objects.get(name='local')
        ProjectMembership.objects.bulk_create([ProjectMembership(user=self.user, project=project, source=source) for project in extra])
        with self.assertRaises(self.api().SnapshotCapacity):
            self.api().PoolQueries(epoch=lambda:self.epoch).snapshot(self.actor)

    def test_authenticated_producer_and_observation_http_wire(self):
        run = self.api().ProjectionWriter.begin_run(self.epoch)
        producer = uuid4()
        client = Client(HTTP_AUTHORIZATION='Token '+self.service_key)
        body = {'schema_version':1, 'controller_run':str(run.id), 'target_id':str(self.assignment.id),
            'incarnation_id':str(self.incarnation.id), 'executor_epoch':str(self.boot),
            'kind':'state', 'producer_epoch':str(producer), 'expected_revision':'0'}
        result = client.post('/api/v1/machine/observation-producers', body, content_type='application/json', secure=True)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['revision'], '1')
        current = client.get('/api/v1/machine/observation-producers/'+str(self.assignment.id)+'/state', secure=True)
        self.assertEqual(current.status_code, 200, current.content)
        self.assertEqual(current.json()['producer']['epoch'], str(producer))
        self.assertEqual(current.json()['producer']['revision'], '1')
        fact = {key:value for key,value in body.items() if key not in ('expected_revision','executor_epoch')}
        fact.update(sequence='1', observed_at=timezone.now().isoformat(), status='fresh', reason='', payload={'session':'busy 1 1'})
        result = client.post('/api/v1/machine/observations', fact, content_type='application/json', secure=True)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertTrue(result.json()['recorded'])
        row = self.client.get('/api/v1/puppets/'+str(self.assignment.id), secure=True).json()['puppet']
        self.assertEqual(row['state']['kind'], 'busy')
        self.assertEqual(row['observed']['state']['producer_epoch'], str(producer))

    def test_read_only_fact_permission_does_not_authorize_observation_ingest(self):
        from mop.server.access import services as auth
        writer, run = self.writer()
        key = auth.enroll_service('read-only-facts', self.node.id, project_ids=[self.project.id],
            permissions=['execution.read_facts'])
        actor = auth.token_context(key)
        with self.assertRaises(auth.Denied):
            writer.observe(actor, target_id=self.assignment.id, incarnation_id=self.incarnation.id,
                producer_epoch=self.boot, sequence=1, kind='state', observed_at=timezone.now(), payload={'session':'busy 1 1'})

    def test_executor_epoch_is_not_an_implicitly_enrolled_source_producer(self):
        api = self.api()
        run = api.ProjectionWriter.begin_run(self.epoch)
        writer = api.ProjectionWriter(run)
        with self.assertRaises(api.SourceConflict):
            self.observed(writer)

    def test_producer_rotation_is_explicit_scoped_and_cas_fenced(self):
        api = self.api()
        run = api.ProjectionWriter.begin_run(self.epoch)
        writer = api.ProjectionWriter(run)
        first, second = uuid4(), uuid4()
        self.assertTrue(callable(getattr(writer, 'bind_producer', None)),
                        'producer identity must be explicitly enrolled, not equated with executor epoch')
        self.assertEqual(writer.bind_producer(self.machine, target_id=self.assignment.id,
            incarnation_id=self.incarnation.id, executor_epoch=self.boot, kind='state',
            producer_epoch=first, expected_revision=0), 1)
        self.assertTrue(writer.observe(self.machine, target_id=self.assignment.id, incarnation_id=self.incarnation.id,
            producer_epoch=first, sequence=8, kind='state', observed_at=timezone.now(), payload={'session':'busy 1 1'}))
        writer.bind_producer(self.machine, target_id=self.assignment.id, incarnation_id=self.incarnation.id,
            executor_epoch=self.boot, kind='state', producer_epoch=second, expected_revision=1)
        with self.assertRaises(api.SourceConflict):
            writer.bind_producer(self.machine, target_id=self.assignment.id, incarnation_id=self.incarnation.id,
                executor_epoch=self.boot, kind='state', producer_epoch=first, expected_revision=1)
        with self.assertRaises(api.SourceConflict):
            writer.observe(self.machine, target_id=self.assignment.id, incarnation_id=self.incarnation.id,
                producer_epoch=first, sequence=999, kind='state', observed_at=timezone.now(), payload={'session':'idle 1 1'})
        self.assertTrue(writer.observe(self.machine, target_id=self.assignment.id, incarnation_id=self.incarnation.id,
            producer_epoch=second, sequence=1, kind='state', observed_at=timezone.now(), payload={'session':'busy 1 1'}))
        row = api.PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']
        self.assertEqual(row['observed']['state']['producer_epoch'], str(second))
        self.assertEqual(row['observed']['state']['freshness'], 'fresh')

    def test_controller_restart_keeps_retired_uuid_fenced(self):
        writer, run = self.writer()
        self.observed(writer)
        with transaction.atomic():
            writer.retire(target_id=self.assignment.id)
        newer = self.api().ProjectionWriter.begin_run(self.epoch)
        row = self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']
        self.assertTrue(row['deleted'])
        self.assertEqual(self.api().PoolQueries(epoch=lambda:self.epoch).snapshot(self.actor)['results'], [])

    def test_published_invalidation_metadata_cannot_be_rewritten(self):
        writer, run = self.writer()
        self.observed(writer)
        from mop.server.sessions.projection_models import ProjectionEvent
        with self.assertRaises(ValueError):
            ProjectionEvent.objects.update(revision=99)

    def test_owner_mutation_and_publication_can_join_the_result_transaction(self):
        from mop.server.sessions.projection_writes import lock_projects
        from mop.server.sessions.models import Assignment
        from mop.server.sessions.projection_models import ProjectionEvent
        writer, run = self.writer()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                lock_projects((self.project.id,))
                Assignment.objects.filter(pk=self.assignment.id).update(role='changed', revision=3)
                writer.changed(target_id=self.assignment.id)
                raise RuntimeError('result writer failed')
        self.assertEqual(Assignment.objects.get(pk=self.assignment.id).role, 'worker')
        self.assertEqual(ProjectionEvent.objects.count(), 0)
        with transaction.atomic():
            lock_projects((self.project.id,))
            Assignment.objects.filter(pk=self.assignment.id).update(role='changed', revision=3)
            writer.changed(target_id=self.assignment.id)
        self.assertEqual(str(ProjectionEvent.objects.get().revision),
            self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']['revision'])

    def test_command_cas_revision_is_separate_from_observation_projection_revision(self):
        writer, run = self.writer()
        self.observed(writer)
        row = self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']
        self.assertEqual(row['desired']['revision'], '2')
        self.assertGreater(int(row['revision']), int(row['desired']['revision']))

    def test_small_repeated_reads_do_not_allocate_or_mutate_a_projection_cache(self):
        from mop.server.sessions.projection_models import ReadSnapshot
        for _ in range(10):
            result = self.client.get('/api/v1/puppets', secure=True)
            self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(ReadSnapshot.objects.count(), 0)

    def test_snapshot_vector_and_atomic_outbox_have_a_scoped_read_handshake(self):
        writer, run = self.writer()
        query = self.api().PoolQueries(epoch=lambda:self.epoch)
        snapshot = query.snapshot(self.actor)
        self.observed(writer)
        result = query.events_after(self.actor, snapshot['cursor'])
        self.assertEqual(len(result['results']), 1)
        event = result['results'][0]
        row = query.detail(self.actor, self.assignment.id)['puppet']
        self.assertEqual(event['entity']['revision'], row['revision'])
        self.assertEqual(event['entity']['id'], row['id'])
        self.assertEqual(query.events_after(self.actor, result['cursor'])['results'], [])
        self.assertNotIn('payload', event)

    def test_public_list_is_authenticated_scoped_and_versioned(self):
        response = self.client.get('/api/v1/puppets', secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['schema_version'], 1)
        self.assertEqual(len(body['results']), 1)
        row = body['results'][0]
        self.assertEqual(row['id'], str(self.assignment.id))
        self.assertEqual(row['incarnation'], str(self.incarnation.id))
        self.assertIsInstance(row['revision'], str)
        self.assertEqual(row['desired']['role'], 'worker')
        self.assertIsNone(row['observed']['state']['value'])
        self.assertEqual(row['state']['kind'], 'unknown')
        self.assertIn('applied', row)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_detail_foreign_uuid_and_missing_action_do_not_disclose(self):
        self.assertEqual(self.client.get('/api/v1/puppets/'+str(uuid4()), secure=True).status_code, 404)
        self.user.user_permissions.clear()
        self.assertEqual(self.client.get('/api/v1/puppets', secure=True).status_code, 403)
        self.assertIn(Client().get('/api/v1/puppets', secure=True).status_code, (401, 403))

    def test_real_assignment_not_user_presence_is_puppet_source(self):
        from mop.server.sessions.models import Assignment
        Assignment.objects.filter(pk=self.assignment.id).update(project_id=self.other.id)
        response = self.client.get('/api/v1/puppets', secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'], [])

    def test_source_sequence_rejects_late_poll_and_unknown_fields(self):
        writer, run = self.writer()
        self.assertTrue(self.observed(writer, sequence=5))
        self.assertFalse(self.observed(writer, sequence=4, payload={'session':'idle 1 1'}))
        with self.assertRaises(ValueError):
            self.observed(writer, sequence=6, payload={'history':'private text'})
        body = self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)
        self.assertEqual(body['puppet']['observed']['state']['sequence'], '5')
        self.assertEqual(body['puppet']['state']['kind'], 'busy')

    def test_restart_stales_observations_until_addressed_confirmation(self):
        writer, run = self.writer()
        self.observed(writer)
        newer = self.api().ProjectionWriter.begin_run(self.epoch)
        row = self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']
        self.assertEqual(row['observed']['state']['freshness'], 'stale')
        self.assertEqual(row['state']['kind'], 'unknown')
        with self.assertRaises(self.api().SourceConflict):
            self.observed(writer, sequence=2)
        writer = self.api().ProjectionWriter(newer)
        self.observed(writer, sequence=2)
        self.assertEqual(self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']['state']['kind'], 'busy')

    def test_new_incarnation_rejects_old_producer_even_with_higher_sequence(self):
        from mop.server.controller.runtime import assignments_service as Assignments
        writer, run = self.writer()
        self.observed(writer)
        Assignments().activate(self.actor, self.assignment.id, expected_revision=2, executor_epoch=uuid4())
        with self.assertRaises(self.api().SourceConflict):
            self.observed(writer, sequence=999)
        row = self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']
        self.assertEqual(row['observed']['state']['freshness'], 'stale')
        self.assertNotEqual(row['state']['kind'], 'busy')

    def test_ingest_requires_current_machine_project_and_node_scope(self):
        from mop.server.access.services import Denied
        writer, run = self.writer()
        with self.assertRaises(Denied):
            writer.observe(self.actor, target_id=self.assignment.id, incarnation_id=self.incarnation.id,
                producer_epoch=self.boot, sequence=1, kind='state', observed_at=timezone.now(), payload={'session':'busy 1 1'})
        from mop.server.access.models import ProjectMembership
        ProjectMembership.objects.filter(user_id=self.machine.user_id).delete()
        with self.assertRaises(Denied):
            self.observed(writer)

    def test_source_failure_does_not_make_target_dead_or_free(self):
        writer, run = self.writer()
        self.observed(writer, payload={}, status='unavailable', reason='source_unavailable')
        row = self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']
        self.assertEqual(row['observed']['state']['freshness'], 'unavailable')
        self.assertEqual(row['state']['kind'], 'silent')
        self.observed(writer, kind='size', payload={'disk_bytes':42})
        self.assertEqual(self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']['state']['kind'], 'silent')

    def test_observation_and_revision_and_outbox_rollback_together(self):
        writer, run = self.writer()
        before = self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']['revision']
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.observed(writer)
                raise RuntimeError('rollback')
        from mop.server.sessions.projection_models import ProjectionEvent, SourceObservation
        self.assertFalse(ProjectionEvent.objects.exists())
        self.assertFalse(SourceObservation.objects.exists())
        self.assertEqual(self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']['revision'], before)

    def test_tombstone_old_uuid_never_resurrects(self):
        writer, run = self.writer()
        self.observed(writer)
        with transaction.atomic():
            writer.retire(target_id=self.assignment.id)
        with self.assertRaises(self.api().SourceConflict):
            self.observed(writer, sequence=99)
        row = self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']
        self.assertTrue(row['deleted'])
        self.assertEqual(self.api().PoolQueries(epoch=lambda:self.epoch).snapshot(self.actor)['results'], [])

    def test_pagination_freezes_rows_across_independent_commit(self):
        from mop.server.sessions.models import Assignment
        Assignment.objects.create(project=self.project, node=self.node, role='second', bundle_digest='b'*64,
            grant_id=uuid4(), expires_at=timezone.now()+timedelta(hours=1))
        query = self.api().PoolQueries(epoch=lambda: self.epoch)
        first = query.snapshot(self.actor, page_size=1)
        self.assertIsNotNone(first['next_cursor'])
        def commit():
            close_old_connections()
            try:
                Assignment.objects.filter(project=self.project).update(role='changed')
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(commit).result(timeout=10)
        second = query.snapshot(self.actor, page_size=1, cursor=first['next_cursor'])
        self.assertEqual(first['snapshot_id'], second['snapshot_id'])
        self.assertEqual(first['cursor'], second['cursor'])
        self.assertNotEqual(second['results'][0]['desired']['role'], 'changed')
        self.assertIsNone(second['next_cursor'])

    def test_cursor_reauthorizes_and_rejects_modified_scope(self):
        query = self.api().PoolQueries(epoch=lambda: self.epoch)
        from mop.server.sessions.models import Assignment
        Assignment.objects.create(project=self.project, node=self.node, role='second', bundle_digest='b'*64,
            grant_id=uuid4(), expires_at=timezone.now()+timedelta(hours=1))
        first = query.snapshot(self.actor, page_size=1)
        from mop.server.access.models import ProjectMembership
        ProjectMembership.objects.filter(user=self.user).delete()
        with self.assertRaises(self.api().SnapshotReset):
            query.snapshot(self.actor, page_size=1, cursor=first['next_cursor'])

    def test_single_sql_rows_never_mix_concurrent_assignment_and_observation_commit(self):
        writer, run = self.writer()
        self.observed(writer)
        from django.db import connection
        from mop.server.sessions.models import Assignment
        barrier = Barrier(2)
        def commit():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    Assignment.objects.filter(pk=self.assignment.id).update(role='changed')
                    self.observed(writer, sequence=2, payload={'session':'idle 1 1'})
                barrier.wait(timeout=10)
            finally:
                close_old_connections()
        class Race:
            fired = False
            def __call__(inner, execute, sql, params, many, context):
                if not inner.fired and sql.lstrip().upper().startswith('SELECT') and 'execution_assignment' in sql and 'projection_observation' in sql:
                    inner.fired = True
                    barrier.wait(timeout=10)
                    barrier.wait(timeout=10)
                return execute(sql, params, many, context)
        race = Race()
        with ThreadPoolExecutor(max_workers=1) as executor:
            job = executor.submit(commit)
            with connection.execute_wrapper(race):
                row = self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']
            self.assertTrue(race.fired, 'authoritative rows must be one SQL with correlated observations')
            job.result(timeout=10)
        self.assertEqual(row['desired']['role'], 'changed')
        self.assertEqual(row['observed']['state']['sequence'], '2')

    def test_competing_producers_do_not_relabel_one_sequence_as_two_updates(self):
        writer, run = self.writer()
        barrier = Barrier(2)
        def ingest():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.observed(writer, sequence=7)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: ingest(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        from mop.server.sessions.projection_models import ProjectionEvent
        event = ProjectionEvent.objects.get()
        row = self.api().PoolQueries(epoch=lambda:self.epoch).detail(self.actor, self.assignment.id)['puppet']
        self.assertEqual(str(event.revision), row['revision'])
        self.assertEqual(event.incarnation_id, self.incarnation.id)

    def test_read_does_not_create_source_or_repair_assignments(self):
        from mop.server.sessions.projection_models import SourceObservation, ProjectionClock
        from mop.server.sessions.models import Assignment
        before = list(Assignment.objects.values())
        self.api().PoolQueries(epoch=lambda: self.epoch).detail(self.actor, self.assignment.id)
        self.assertEqual(before, list(Assignment.objects.values()))
        self.assertEqual(SourceObservation.objects.count(), 0)
        self.assertEqual(ProjectionClock.objects.count(), 0)

    def test_http_observation_rejects_forged_authority_and_partial_inventory(self):
        client = Client(HTTP_AUTHORIZATION='Token '+self.service_key)
        response = client.post('/api/v1/machine/observations', {'schema_version':1, 'node_id':str(uuid4()),
            'inventory':[]}, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 400, response.content)


class PureProjection(SimpleTestCase):
    def test_state_policy_reused_and_payload_whitelist_has_no_transcript(self):
        from mop.server.sessions import projections
        from unittest.mock import patch
        from mop.common.state import State
        with patch('mop.common.state.verdict', return_value=State('busy')) as verdict:
            value = projections.project_state({'session':'busy 1 1'}, None, 'fresh')
            self.assertEqual(value['kind'], 'busy')
            verdict.assert_called_once()
        with self.assertRaises(ValueError):
            projections.safe_payload('state', {'terminal_bytes':'secret'})
