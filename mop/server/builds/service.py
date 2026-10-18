"""Явный finite process manager: plan → clear/wipe → bake → restore all → announce.

Долгие ports вне SQL. COMMIT intent предшествует каждому эффекту; старый intent
разрешает только inspect, никогда повтор apply. Quiescence не выводится из lease.
"""
from datetime import timedelta
from dataclasses import replace
import hashlib
import json
from uuid import uuid4
from django.utils import timezone

from ..access.services import Denied
from ..catalog.models import Project, ProjectPolicySnapshot, Node, NodeProject
from ..operations import models as op
from ..operations.lifecycle import State
from ..operations.requests import resolve_retry, RequestRefused, Retry
from ..operations.service import _independent, _binding, _actor, _event, _move, Refused
from ..sessions.models import Assignment, AssignmentAttempt
from . import models as m
from .commands import BuildCommand, validate_source
from .adapters import confirmed
from .contracts import BuildClaim, BuildInput, TargetSnapshot, StageTicket, StageResult


class Builds:
    """Один конкретный handler общей очереди; composition root владеет ports."""
    def __init__(self, operations, *, scheduler=None, image_builder=None, node_executor=None):
        self.operations = operations
        self.scheduler, self.image_builder, self.node_executor = scheduler, image_builder, node_executor

    def _authorize(self, actor, run, action='catalog.build_project'):
        nodes = tuple(run.buildnodeplan_set.values_list('node_id', flat=True))
        self.operations.access.authorize(actor, action, project_ids=(run.project_id,), node_ids=nodes)
        if action == 'catalog.build_project' and run.force_safety:
            self.operations.access.authorize(actor, 'builds.force_build', project_ids=(run.project_id,), node_ids=nodes)

    def _input(self, run):
        return BuildInput(run.operation_id, run.project_id, run.project_name, run.origin, run.policy_id,
            bytes(run.manifest), run.manifest_digest, run.source_ref, run.source_commit, run.source_digest,
            run.mode, tuple(run.buildnodeplan_set.order_by('node_id').values_list('node_id', flat=True)), run.force_safety,
            run.operation.not_after)

    def _snapshot(self, row):
        run = m.BuildRun.objects.get(operation=row)
        plans = m.AffectedTargetPlan.objects.filter(build=run).order_by('assignment_id')
        receipts = m.StageReceipt.objects.filter(intent__build=run).select_related('intent')
        return dict(id=str(row.pk), request_id=str(row.request_id), epoch=str(row.epoch), state=row.state,
            revision=row.revision, code=row.code, reason=row.reason,
            target={'kind':'project', 'id':str(run.project_id), 'source_revision':str(row.expected_revision)},
            resources_held=op.ResourceClaim.objects.filter(operation=row, released_at=None).exists(),
            build=dict(policy_id=str(run.policy_id), source_commit=run.source_commit, source_ref=run.source_ref,
                manifest_digest=run.manifest_digest, source_digest=run.source_digest, mode=run.mode,
                target_nodes=[str(pk) for pk in run.buildnodeplan_set.order_by('node_id').values_list('node_id', flat=True)],
                planned=run.planned_at is not None, skipped=run.skipped, affected_targets=[str(p.assignment_id) for p in plans],
                unresolved_stages=[{'id':str(i.pk),'stage':i.stage,'key':i.key,'status':'needs_readback'}
                    for i in m.StageIntent.objects.filter(build=run).exclude(stagereceipt__resolved=True).order_by('created_at','id')],
                stages=[{'stage':r.intent.stage, 'key':r.intent.key, 'outcome':r.outcome, 'quiescent':r.quiescent,
                    'resolved':r.resolved, 'reason':r.reason} for r in receipts.order_by('received_at', 'id')],
                original_failure=run.original_failure,
                compensation_failures=[r.reason for r in receipts if r.intent.stage=='restore' and r.outcome=='failed']))

    def disclose(self, actor, row):
        run = m.BuildRun.objects.get(operation=row)
        self._authorize(actor, run, 'operations.view_operation')
        if row.result_pruned:
            raise Refused('result_expired')
        return self._snapshot(row)

    def submit(self, actor, command):
        if type(command) is not BuildCommand:
            raise ValueError('finite typed build command required')
        _independent()
        api = self.operations
        with api._transaction():
            state = api._installation(command.epoch)
            api.access.authorize(actor, 'operations.view_operation', project_ids=(command.project_id,))
            row = op.Operation.objects.filter(actor_id=actor.user_id, request_id=command.request_id).first()
            try:
                retry = resolve_retry(expected_epoch=command.epoch, current_epoch=state.epoch,
                    submitted_digest=command.digest(), stored_digest=row.digest if row else None,
                    result_available=not row.result_pruned if row else True)
            except RequestRefused as error:
                raise Refused(error.code) from None
            if retry is Retry.EXPIRED:
                raise Refused('result_expired')
            if retry is Retry.REPLAY:
                if row.action != 'build':
                    raise Refused('request_conflict')
                return self.disclose(actor, row)
            project = Project.objects.select_for_update().get(pk=command.project_id, retired_at=None)
            api.access.authorize(actor, 'catalog.build_project', project_ids=(project.pk,), node_ids=command.target_nodes)
            if command.force_safety:
                api.access.authorize(actor, 'builds.force_build', project_ids=(project.pk,), node_ids=command.target_nodes)
            if not state.effects_enabled:
                raise Refused('effects_disabled')
            now = timezone.now()
            if command.not_after <= now:
                raise Refused('deadline_expired')
            if command.not_after > now + timedelta(hours=2):
                raise Refused('deadline_horizon', safe_reason='build execution deadline exceeds two hours')
            if project.revision != command.expected_revision:
                raise Refused('revision_conflict')
            policy = ProjectPolicySnapshot.objects.get(pk=command.policy_id, project=project)
            # Защищённые bytes раскрываются отдельным Preparation/root adapter;
            # ciphertext нельзя передавать Ansible как будто это manifest.
            if policy.confidential or not 0 < len(policy.content) <= 262144:
                raise Denied('build requires an approved bounded public policy snapshot')
            try:
                validate_source(policy.source_ref, policy.source_commit)
            except ValueError:
                raise Denied('pinned policy requires a verified immutable image source') from None
            content = bytes(policy.content)
            if hashlib.sha256(content).hexdigest() != policy.digest:
                raise ValueError('pinned manifest digest mismatch')
            nodes = list(Node.objects.select_for_update().filter(pk__in=command.target_nodes, retired_at=None).order_by('pk'))
            if len(nodes) != len(command.target_nodes) or set(NodeProject.objects.filter(node_id__in=command.target_nodes,
                    project=project).values_list('node_id', flat=True)) != set(command.target_nodes):
                raise Denied('build nodes must belong to current project')
            api._admission(state, (project.pk,), actor=actor, request_id=command.request_id)
            row = op.Operation.objects.create(actor_id=actor.user_id, actor_binding=_binding(actor),
                request_id=command.request_id, digest=command.digest(), epoch=command.epoch, restore_generation=state.generation,
                action='build', node=nodes[0], expected_revision=command.expected_revision, eligible=False, not_after=command.not_after)
            row.projects.set((project,))
            op.Step.objects.create(operation=row, name='image_build')
            source = hashlib.sha256(json.dumps([str(project.pk), project.origin, policy.source_ref,
                policy.source_commit, policy.digest], separators=(',', ':')).encode()).hexdigest()
            run = m.BuildRun.objects.create(operation=row, project=project, policy=policy, project_name=project.name,
                origin=project.origin, manifest=content, manifest_digest=policy.digest, source_ref=policy.source_ref,
                source_commit=policy.source_commit, source_digest=source, mode=command.mode, force_safety=command.force_safety)
            for node in nodes:
                m.BuildNodePlan.objects.create(build=run, node=node, source_revision=node.revision, driver=node.driver)
            _event(row)
            return self._snapshot(row)

    def claim(self, operation_id):
        _independent()
        api = self.operations
        with api._transaction():
            state = api._installation()
            row = op.Operation.objects.select_for_update().get(pk=operation_id, action='build')
            if row.state != State.ACCEPTED or row.epoch != state.epoch or row.restore_generation != state.generation or not state.effects_enabled:
                return None
            run = m.BuildRun.objects.get(operation=row)
            try:
                self._authorize(_actor(row), run)
            except Denied as error:
                _move(row, State.REFUSED, 'access_denied', expected_revision=row.revision, safe_reason=str(error))
                return None
            if row.not_after <= timezone.now():
                _move(row, State.EXPIRED, 'deadline_expired', expected_revision=row.revision)
                return None
            project = Project.objects.select_for_update().get(pk=run.project_id)
            nodes = list(run.buildnodeplan_set.select_related('node').order_by('node_id'))
            if (project.revision != row.expected_revision or project.retired_at is not None
                    or any(n.node.revision != n.source_revision or n.node.driver != n.driver or n.node.retired_at is not None for n in nodes)):
                _move(row, State.REFUSED, 'revision_conflict', expected_revision=row.revision)
                return None
            # Общий mirror плюс project/node claims в том же переносимом протоколе.
            keys = sorted(['build:mirror', 'project:'+str(run.project_id), *('node:'+str(n.node_id) for n in nodes)])
            for key in keys:
                op.Resource.objects.get_or_create(key=key)
            tuple(op.Resource.objects.select_for_update().filter(pk__in=keys).order_by('pk'))
            if op.ResourceClaim.objects.filter(resource_id__in=keys, released_at=None).exists():
                return None
            if op.Operation.objects.filter(state__in=('running','unknown','reconciling'), resourceclaim__released_at=None).distinct().count() >= api.execution_limit:
                return None
            step = op.Step.objects.select_for_update().get(operation=row)
            step.token, step.started_at = uuid4(), timezone.now()
            step.lease_until = step.started_at+timedelta(seconds=30)
            step.save(update_fields=('token','started_at','lease_until'))
            for key in keys:
                op.ResourceClaim.objects.create(resource_id=key, operation=row, step=step, token=step.token)
            _move(row, State.RUNNING, 'build_claimed', expected_revision=row.revision)
            return BuildClaim(row.pk, step.pk, step.token, row.epoch)

    def _bound(self, claim):
        if type(claim) is not BuildClaim:
            raise Refused('claim_conflict')
        state = self.operations._installation(claim.epoch)
        row = op.Operation.objects.select_for_update().get(pk=claim.operation_id, action='build')
        step = op.Step.objects.select_for_update().get(pk=claim.step_id, operation=row)
        if step.token != claim.token or row.restore_generation != state.generation:
            raise Refused('claim_conflict')
        return row, step, m.BuildRun.objects.get(operation=row), state

    def _resume(self, operation_id):
        with self.operations._transaction():
            row = op.Operation.objects.select_for_update().get(pk=operation_id, action='build')
            step = op.Step.objects.get(operation=row)
            if row.state not in ('running','reconciling','unknown'):
                return None
            claim = BuildClaim(row.pk, step.pk, step.token, row.epoch)
            self._bound(claim)
            return claim

    def _preflight(self):
        for port, required in ((self.scheduler, {'scheduler','inspect','fenced_restore'}),
                               (self.image_builder, {'image_builder','inspect'}),
                               (self.node_executor, {'node_executor','inspect'})):
            if port is None or not required <= getattr(port, 'capabilities', frozenset()):
                raise RuntimeError('privileged build port capabilities are not bound')

    def _gate(self, row, run, state):
        self._authorize(_actor(row), run)
        if not state.effects_enabled:
            raise Refused('effects_disabled')
        if row.not_after <= timezone.now():
            raise Refused('deadline_expired')
        if Project.objects.get(pk=run.project_id).revision != row.expected_revision:
            raise Refused('revision_conflict')
        for node in run.buildnodeplan_set.select_related('node'):
            if node.node.retired_at is not None or node.node.revision != node.source_revision or node.node.driver != node.driver:
                raise Refused('revision_conflict')

    def _plan(self, claim, *, availability=()):
        with self.operations._transaction():
            row, step, run, state = self._bound(claim)
            if run.planned_at is not None:
                return
            self._gate(row, run, state)
            build = self._input(run)
            container_nodes = tuple(run.buildnodeplan_set.filter(driver='pve').values_list('node_id', flat=True))
        # Host strategy имеет пустой clear/bake plan: host не является шаблоном.
        skip = (build.mode == 'missing' and bool(container_nodes) and len(availability) == len(container_nodes)
            and all(confirmed(fact, build, node) for node, fact in zip(container_nodes, availability)))
        targets = self.scheduler.plan(build) if container_nodes and not skip else ()
        if type(targets) is not tuple or len(targets) > 256 or any(type(p) is not TargetSnapshot for p in targets):
            raise ValueError('complete bounded target plan required')
        with self.operations._transaction():
            row, step, run, state = self._bound(claim)
            if run.planned_at is not None:
                return
            self._gate(row, run, state)
            if skip:
                for fact in availability:
                    if not confirmed(fact, build, fact.node_id):
                        raise ValueError('physical skip confirmation expired before commit')
                    m.AvailabilityReceipt.objects.create(build=run, node_id=fact.node_id, source_digest=fact.source_digest,
                        external_reference=fact.external_reference, observed_at=fact.observed_at)
            expected_ids = set() if skip else set(Assignment.objects.filter(project_id=run.project_id, node_id__in=container_nodes,
                current_incarnation__isnull=False).values_list('pk', flat=True))
            if {p.target_id for p in targets} != expected_ids or len(targets) != len(expected_ids):
                raise ValueError('incomplete affected target plan')
            for p in sorted(targets, key=lambda p: str(p.target_id)):
                assignment = Assignment.objects.select_for_update().get(pk=p.target_id)
                if not self._same(assignment, p.incarnation_id, p.revision, p.generation, p.node_id):
                    raise ValueError('target identity changed before plan')
                if (p.node_id not in container_nodes or not timezone.now()-timedelta(seconds=10) <= p.observed_at <= timezone.now()
                        or p.free is not True and not run.force_safety):
                    raise ValueError('affected clone is not confirmed free')
                if AssignmentAttempt.objects.filter(assignment=assignment, state__in=('claimed','unknown')).exists():
                    raise ValueError('unresolved machine effect holds target')
                key = 'target:'+str(p.target_id)
                op.Resource.objects.get_or_create(key=key)
                op.Resource.objects.select_for_update().get(pk=key)
                if op.ResourceClaim.objects.filter(resource_id=key, released_at=None).exclude(operation=row).exists():
                    raise ValueError('unresolved operation holds target')
                op.ResourceClaim.objects.get_or_create(resource_id=key, operation=row, step=step, token=step.token)
                m.AffectedTargetPlan.objects.create(build=run, assignment=assignment, incarnation_id=p.incarnation_id,
                    node_id=p.node_id, source_revision=p.revision, generation=p.generation, role=assignment.role,
                    bundle_digest=assignment.bundle_digest, grant_id=assignment.grant_id,
                    source_spec=p.source_spec, spec_digest=p.spec_digest)
            run.planned_at, run.skipped = timezone.now(), skip
            run.save(update_fields=('planned_at','skipped'))

    @staticmethod
    def _same(row, incarnation_id, revision, generation, node_id):
        return (row.current_incarnation_id == incarnation_id and row.revision == revision
                and row.generation == generation and row.node_id == node_id)

    def _ticket(self, intent, claim, run):
        target = intent.target
        build = self._input(run)
        if intent.stage == 'build':
            build = replace(build, target_nodes=tuple(run.buildnodeplan_set.filter(driver='pve').order_by('node_id').values_list('node_id', flat=True)))
        return StageTicket(intent.pk, intent.key, intent.stage, claim, build, intent.node_id,
            target.assignment_id if target else None, target.incarnation_id if target else None,
            target.source_revision if target else None, target.generation if target else None,
            bytes(target.source_spec) if target else b'', target.spec_digest if target else '')

    def _intent(self, claim, stage, *, target_id=None, node_id=None):
        with self.operations._transaction():
            row, step, run, state = self._bound(claim)
            if row.state not in ('running','unknown','reconciling'):
                raise Refused('operation_conflict')
            key = stage+':'+str(target_id or node_id or run.project_id)
            intent = m.StageIntent.objects.filter(build=run, key=key).first()
            if intent:
                receipt = m.StageReceipt.objects.filter(intent=intent, resolved=True).first()
                if receipt:
                    return None, False, StageResult(receipt.outcome, receipt.quiescent, receipt.reason)
                return self._ticket(intent, claim, run), False, None
            # Компенсация имеет ровно прежний plan scope; revoke forward action
            # не разрешает overwrite новой identity и не бросает снятых остальных.
            if stage != 'restore':
                self._gate(row, run, state)
            target = m.AffectedTargetPlan.objects.select_related('assignment').get(pk=target_id, build=run) if target_id else None
            intent = m.StageIntent.objects.create(build=run, step=step, token=step.token, key=key,
                stage=stage, target=target, node_id=target.node_id if target else node_id)
            if target:
                assignment = Assignment.objects.select_for_update().get(pk=target.assignment_id)
                same = self._same(assignment, target.incarnation_id, target.source_revision, target.generation, target.node_id)
                same = same and assignment.role == target.role and assignment.bundle_digest == target.bundle_digest and assignment.grant_id == target.grant_id
                if not same:
                    result = StageResult('failed', True, 'target identity changed; original spec was not restored')
                    m.StageReceipt.objects.create(intent=intent, outcome=result.outcome, quiescent=True, resolved=True, reason=result.reason)
                    return None, False, result
            step.dispatched_at = step.dispatched_at or timezone.now()
            step.save(update_fields=('dispatched_at',))
            return self._ticket(intent, claim, run), True, None

    def resolve_ticket(self, *, intent_id, step_id, token, epoch, verb):
        """Trusted executor root gate. HTTP никогда не получает произвольный adapter."""
        _independent()
        with self.operations._transaction():
            intent = m.StageIntent.objects.get(pk=intent_id, step_id=step_id, token=token)
            claim = BuildClaim(intent.build_id, step_id, token, epoch)
            row, step, run, state = self._bound(claim)
            if (row.state not in ('running','unknown','reconciling') or step.quiescent_at is not None
                    or not op.ResourceClaim.objects.filter(operation=row, step=step, token=token,
                        resource_id='build:mirror', released_at=None).exists()):
                raise Denied('privileged executor claim is not current')
            if verb == 'apply':
                if m.StageReceipt.objects.filter(intent=intent).exists():
                    raise Denied('stage requires inspection, not replay')
                if intent.stage != 'restore':
                    self._gate(row, run, state)
                if m.ExecutorDispatch.objects.filter(intent=intent).exists():
                    raise Denied('privileged dispatch consumed; inspect instead of replay')
                m.ExecutorDispatch.objects.create(intent=intent)
            elif verb != 'inspect':
                raise Denied('unknown executor verb')
            return self._ticket(intent, claim, run)

    def _record(self, claim, ticket, result):
        _independent()
        if type(result) is not StageResult:
            raise ValueError('typed build stage result required')
        with self.operations._transaction():
            row, step, run, state = self._bound(claim)
            intent = m.StageIntent.objects.get(pk=ticket.id, build=run, token=claim.token)
            final = m.StageReceipt.objects.filter(intent=intent, resolved=True).first()
            if final:
                if (final.outcome, final.reason) != (result.outcome, result.reason):
                    raise Refused('claim_conflict')
                return
            resolved = result.outcome != 'unknown' and result.quiescent
            if not m.StageReceipt.objects.filter(intent=intent, resolved=resolved).exists():
                m.StageReceipt.objects.create(intent=intent, outcome=result.outcome, quiescent=result.quiescent, resolved=resolved, reason=result.reason)

    def _stage(self, claim, stage, *, target_id=None, node_id=None):
        ticket, fresh, result = self._intent(claim, stage, target_id=target_id, node_id=node_id)
        if result is not None:
            return result
        port = self.image_builder if stage == 'build' else self.node_executor if stage == 'wipe' else self.scheduler
        try:
            result = port.apply(ticket) if fresh else port.inspect(ticket)
        except (Denied, Refused) as error:
            result = StageResult('unknown', False, str(error)[:512])
        except Exception as error:
            # Чужой raw exception может содержать ключ/argv; только тип публичен.
            result = StageResult('unknown', False, 'external stage unavailable: '+type(error).__name__)
        self._record(claim, ticket, result)
        return result

    def _unknown(self, claim):
        with self.operations._transaction():
            row, step, run, state = self._bound(claim)
            if row.state == State.RUNNING:
                _move(row, State.RECONCILING, 'build_readback_required', expected_revision=row.revision)
            if row.state == State.RECONCILING:
                _move(row, State.UNKNOWN, 'build_outcome_unknown', expected_revision=row.revision)
            return self._snapshot(row)

    def _failure(self, claim, reason):
        with self.operations._transaction():
            row, step, run, state = self._bound(claim)
            if not run.original_failure:
                run.original_failure = reason[:512]
                run.save(update_fields=('original_failure',))

    def _finish(self, claim, state, code):
        with self.operations._transaction():
            row, step, run, installation = self._bound(claim)
            # Ни timeout, ни failed compensation не доказывают quiescence.
            for intent in m.StageIntent.objects.filter(build=run):
                if not m.StageReceipt.objects.filter(intent=intent, resolved=True).exists():
                    return self._unknown(claim)
            if row.state == State.UNKNOWN:
                _move(row, State.RECONCILING, 'build_readback_confirmed', expected_revision=row.revision)
            _move(row, state, code, expected_revision=row.revision)
            step.quiescent_at = timezone.now()
            step.save(update_fields=('quiescent_at',))
            op.ResourceClaim.objects.filter(operation=row, step=step, token=claim.token, released_at=None).update(released_at=step.quiescent_at)
            return self._snapshot(row)

    def run(self, operation_id):
        """Blocking entrypoint вызывается только через Runtime.blocking/effect owner."""
        _independent()
        self._preflight()
        claim = self.claim(operation_id) or self._resume(operation_id)
        if claim is None:
            with self.operations._transaction():
                return self._snapshot(op.Operation.objects.get(pk=operation_id))
        with self.operations._transaction():
            row, step, run, state = self._bound(claim)
            probe_build = self._input(run)
            probe_nodes = tuple(run.buildnodeplan_set.filter(driver='pve').order_by('node_id').values_list('node_id', flat=True))
            probe = run.mode == 'missing' and run.planned_at is None
        availability = tuple(self.image_builder.availability(probe_build, node) for node in probe_nodes) if probe else ()
        try:
            self._plan(claim, availability=availability)
        except (ValueError, Denied, Refused) as error:
            self._failure(claim, str(error))
            return self._finish(claim, State.REFUSED, 'build_plan_refused')
        with self.operations._transaction():
            row, step, run, state = self._bound(claim)
            build = self._input(run)
            nodes = tuple(run.buildnodeplan_set.filter(driver='pve').values_list('node_id', flat=True))
            plans = tuple(run.affectedtargetplan_set.order_by('assignment_id').values_list('pk', flat=True))
            failed = bool(run.original_failure)
            # Skip — сохранённое внешнее доказательство до любого clear.
            # После начала clear новый image-exists не отменяет unresolved stages.
            ready = run.skipped
        if not ready and nodes and not failed:
            for plan in plans:
                for stage in ('clear','wipe'):
                    try:
                        result = self._stage(claim, stage, target_id=plan)
                    except (Denied, Refused) as error:
                        self._failure(claim, str(error))
                        failed = True
                        break
                    if result.outcome == 'unknown' or not result.quiescent:
                        return self._unknown(claim)
                    if result.outcome == 'failed':
                        self._failure(claim, result.reason)
                        failed = True
                        break
                if failed:
                    break
            if not failed:
                try:
                    result = self._stage(claim, 'build')
                except (Denied, Refused) as error:
                    self._failure(claim, str(error))
                    failed = True
                else:
                    if result.outcome == 'unknown' or not result.quiescent:
                        return self._unknown(claim)
                    if result.outcome == 'failed':
                        self._failure(claim, result.reason)
                        failed = True
        compensation_failed, unresolved = False, False
        # Каждый attempted clear уже мог снять тело даже при failed receipt.
        for plan in plans:
            if not m.StageIntent.objects.filter(build_id=operation_id, target_id=plan, stage='clear').exists():
                continue
            result = self._stage(claim, 'restore', target_id=plan)
            compensation_failed |= result.outcome == 'failed'
            unresolved |= result.outcome == 'unknown' or not result.quiescent
        if unresolved:
            return self._unknown(claim)
        if failed or compensation_failed:
            return self._finish(claim, State.PARTIAL if compensation_failed else State.FAILED, 'build_compensated_with_errors' if compensation_failed else 'build_failed_compensated')
        for node in nodes:
            try:
                result = self._stage(claim, 'announce', node_id=node)
            except (Denied, Refused) as error:
                self._failure(claim, str(error))
                return self._finish(claim, State.PARTIAL, 'build_announcement_refused')
            if result.outcome == 'unknown' or not result.quiescent:
                return self._unknown(claim)
            if result.outcome == 'failed':
                self._failure(claim, result.reason)
                return self._finish(claim, State.PARTIAL, 'build_announcement_failed')
        return self._finish(claim, State.SUCCEEDED, 'build_confirmed')
