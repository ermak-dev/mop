"""Короткие sync use cases. Fencing не обещает остановить физический процесс."""
from datetime import timedelta
from uuid import UUID
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from mop.server.access.context import PrincipalKind
from mop.server.access.services import Access, Denied, current_actor
from mop.server.catalog.models import Project, NodeProject
from mop.server.catalog.services import snapshot
from . import models as m
from .protocol import (WorkV1, AuthorityV1, ReceiptV1, FactPageV1, FACT_FIELDS,
    positive, role_binding, result_codes, WorkPort, GrantAuthority, FactReader,
    InstallationQuery, InspectionV1, EffectInspector, CleanCloneQuery, FactV1, AssignmentBinding)


class Conflict(ValueError):
    """Текущий источник/ревизия/поколение не совпадает с admission."""


class Unavailable(RuntimeError):
    """Обязательный узкий port не связан или authority недоступна."""


class UnboundWork:
    def pending(self, **scope):
        raise Unavailable('operation work port is not bound')

    def current(self, operation_id, step_id):
        raise Unavailable('operation work port is not bound')

    def claimed(self, work, attempt_id):
        raise Unavailable('operation work port is not bound')

    def completed_result(self, work, attempt_id, outcome, evidence):
        raise Unavailable('operation work port is not bound')


class UnboundGrants:
    def authorize(self, **scope):
        raise Unavailable('current grant authority is not bound')


class UnboundFacts:
    def read(self, **scope):
        raise Unavailable('bounded fact reader is not bound')


class UnboundInspector:
    def inspect(self, work, attempt_id):
        raise Unavailable('effect-specific read-only inspector is not bound')


def unbound_installation():
    raise Unavailable('installation authority is not bound')


def expected(row, revision, generation=None):
    positive(revision)
    if row.revision != revision or generation is not None and row.generation != positive(generation):
        raise Conflict('assignment revision or generation changed')


def human(actor):
    current_actor(actor)
    if actor.kind != PrincipalKind.HUMAN:
        raise Denied('human ownership action required')


def machine(actor, node_id, action, project_ids=()):
    current_actor(actor)
    if actor.kind != PrincipalKind.SERVICE or actor.node_id != node_id:
        raise Denied('enrolled node service required')
    Access().authorize(actor, 'execution.' + action, project_ids=project_ids, node_ids=(node_id,))


def bound(limit, maximum):
    positive(limit)
    if limit > maximum:
        raise ValueError('bounded page limit exceeded')
    return limit


def unbound_target_fence(*args, **kwargs):
    raise Unavailable('shared target fence query is not bound')


def unclaimed(query, target_id, *, excluding_operation=None):
    held = query(target_id, excluding_operation=excluding_operation)
    if type(held) is not bool:
        raise Unavailable('shared target fence query returned an untyped value')
    if held:
        raise Conflict('unresolved operation holds target')


class UnboundClone:
    def observe(self, **scope):
        raise Unavailable('fresh addressed clone query is not bound')


class Assignments:
    """Человек владеет работой; этот lifecycle не worker lease и не PTY handle."""
    def __init__(self, *, clones: CleanCloneQuery | None = None, clone_freshness_seconds=10, target_fence=None):
        self.clones = clones if clones is not None else UnboundClone()
        self.target_fence = target_fence if target_fence is not None else unbound_target_fence
        self.clone_freshness_seconds = bound(clone_freshness_seconds, 60)

    def current(self, *, project_id, target_id):
        """Внутренний запрос для портов Access/Secrets; внешний вызов требует авторизации.

        Projection не является разрешением: потребитель проверяет текущие права,
        grant и expected revision ещё раз перед выдачей допуска/credential bytes.
        """
        row = m.Assignment.objects.select_related('current_incarnation', 'owner').filter(
            pk=target_id, project_id=project_id).first()
        if row is None or row.owner_id is None or row.current_incarnation_id is None:
            return None
        incarnation = row.current_incarnation
        if (not row.owner.is_active or row.expires_at <= timezone.now() or not incarnation.accepting
                or incarnation.assignment_id != row.id or incarnation.node_id != row.node_id
                or not NodeProject.objects.filter(node_id=row.node_id, project_id=row.project_id).exists()):
            return None
        role_binding(row.role, row.bundle_digest, row.grant_id)
        scope = dict(attempt__assignment_id=row.id, attempt__incarnation_id=incarnation.id,
            attempt__generation=row.generation, attempt__role=row.role,
            attempt__bundle_digest=row.bundle_digest, attempt__grant_id=row.grant_id,
            attempt__state='succeeded')
        return AssignmentBinding(row.id, row.project_id, row.node_id, incarnation.id,
            incarnation.executor_epoch, row.owner_id, row.role, row.bundle_digest, row.grant_id,
            row.generation, row.revision, m.IdentityApplication.objects.filter(**scope).exists(),
            m.ReadinessReceipt.objects.filter(**scope).exists())

    def create(self, actor, *, project_id, node_id, role, bundle_digest, grant_id, expires_at):
        human(actor)
        Access().authorize(actor, 'execution.assign_work', project_ids=(project_id,))
        role_binding(role, bundle_digest, grant_id)
        if not isinstance(expires_at, timezone.datetime) or timezone.is_naive(expires_at) or expires_at <= timezone.now():
            raise ValueError('future assignment expiry required')
        # Новый target имеет новую identity; здесь нет присвоения существующего clone.
        with transaction.atomic():
            if not NodeProject.objects.filter(node_id=node_id, project_id=project_id).exists():
                raise Denied('assignment node is not in project')
            return snapshot(m.Assignment.objects.create(project_id=project_id, node_id=node_id,
                owner_id=actor.user_id, role=role, bundle_digest=bundle_digest,
                grant_id=grant_id, expires_at=expires_at))

    def reassign(self, actor, target_id, *, expected_revision, role, bundle_digest, grant_id, expires_at):
        human(actor)
        role_binding(role, bundle_digest, grant_id)
        if not isinstance(expires_at, timezone.datetime) or timezone.is_naive(expires_at) or expires_at <= timezone.now():
            raise ValueError('future assignment expiry required')
        if transaction.get_connection().in_atomic_block:
            raise Unavailable('clone inspection requires no surrounding transaction')
        source = m.Assignment.objects.get(pk=target_id)
        Access().authorize(actor, 'execution.assign_work', project_ids=(source.project_id,))
        expected(source, expected_revision)
        if source.current_incarnation_id is None:
            raise Conflict('existing clone incarnation is unknown')
        if source.owner_id is not None:
            raise Conflict('existing assignment must be explicitly released')
        # Fresh read должен причинно следовать quiescence старого эффекта.
        # Проверки только после I/O недостаточно: эффект мог испортить clone,
        # завершиться и снять fence между clean observation и повторной проверкой.
        if m.AssignmentAttempt.objects.filter(assignment=source, state__in=('claimed', 'unknown')).exists():
            raise Conflict('unresolved external effect holds resource')
        proof = self.clones.observe(target_id=source.id, incarnation_id=source.current_incarnation_id,
                                    node_id=source.node_id)
        if type(proof) is not FactV1:
            raise Conflict('complete addressed clone observation required')
        FactPageV1(source.node_id, 'clone', True, (proof,)).validate(node_id=source.node_id, kind='clone', limit=1)
        with transaction.atomic():
            row = m.Assignment.objects.select_for_update().get(pk=target_id)
            unclaimed(self.target_fence, row.pk)
            human(actor)
            Access().authorize(actor, 'execution.assign_work', project_ids=(row.project_id,))
            expected(row, expected_revision)
            facts = dict(proof.values)
            if (row.owner_id is not None or row.current_incarnation_id != source.current_incarnation_id
                    or proof.target_id != row.id or proof.incarnation_id != row.current_incarnation_id
                    or proof.state != 'fresh' or facts.get('clean') is not True
                    or facts.get('modified', 0) != 0 or facts.get('untracked', 0) != 0
                    or timezone.now() - proof.observed_at > timedelta(seconds=self.clone_freshness_seconds)):
                raise Conflict('assignment requires a free target and fresh clean current clone')
            if m.AssignmentAttempt.objects.filter(assignment=row, state__in=('claimed', 'unknown')).exists():
                raise Conflict('unresolved external effect holds resource')
            row.owner_id = actor.user_id
            row.role, row.bundle_digest, row.grant_id = role, bundle_digest, grant_id
            row.expires_at = expires_at
            row.generation += 1
            row.revision += 1
            row.save(update_fields=('owner', 'role', 'bundle_digest', 'grant_id', 'expires_at', 'generation', 'revision'))
            return snapshot(row)

    def _owned(self, actor, target_id):
        human(actor)
        row = m.Assignment.objects.select_for_update().get(pk=target_id)
        unclaimed(self.target_fence, row.pk)
        Access().authorize(actor, 'execution.assign_work', project_ids=(row.project_id,))
        if row.owner_id != actor.user_id:
            raise Denied('assignment belongs to another human')
        return row

    def activate(self, actor, target_id, *, expected_revision, executor_epoch,
                 incarnation_id=None, grant_id=None):
        if type(executor_epoch) is not UUID or any(value is not None and type(value) is not UUID
                for value in (incarnation_id, grant_id)):
            raise ValueError('typed executor, incarnation and grant UUIDs required')
        with transaction.atomic():
            row = self._owned(actor, target_id)
            expected(row, expected_revision)
            if row.expires_at <= timezone.now():
                raise Conflict('human assignment expired')
            if m.AssignmentAttempt.objects.filter(assignment=row, state__in=('claimed', 'unknown')).exists():
                raise Conflict('unresolved external effect holds resource')
            if row.current_incarnation_id:
                m.ExecutionIncarnation.objects.filter(pk=row.current_incarnation_id).update(accepting=False)
            # Прикладной handler передаёт уже сохранённый Operation result binding;
            # новое значение не генерируется вместо зарезервированного на admission.
            identity = {'id': incarnation_id} if incarnation_id is not None else {}
            incarnation = m.ExecutionIncarnation.objects.create(assignment=row, node_id=row.node_id,
                executor_epoch=executor_epoch, **identity)
            row.current_incarnation = incarnation
            if grant_id is not None:
                row.grant_id = grant_id
            row.revision += 1
            row.generation += 1
            row.save(update_fields=('current_incarnation', 'grant_id', 'revision', 'generation'))
            return snapshot(incarnation)

    def release(self, actor, target_id, *, expected_revision, expected_generation=None):
        with transaction.atomic():
            # Сначала CAS: повторный rollback не приобретает права на новое поколение.
            row = m.Assignment.objects.select_for_update().get(pk=target_id)
            expected(row, expected_revision, expected_generation)
            self._owned(actor, target_id)
            row.owner = None
            row.generation += 1
            row.revision += 1
            row.save(update_fields=('owner', 'generation', 'revision'))
            # Запущенный эффект не отменён release; его fence остаётся до сверки.
            return snapshot(row)


class Landing:
    """Только сериализация человеческого landing; expiry worker не участвует."""
    def acquire(self, actor, project_id, *, expected_revision):
        human(actor)
        Access().authorize(actor, 'execution.acquire_landing', project_ids=(project_id,))
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=project_id)
            row = m.LandingToken.objects.select_for_update().filter(project_id=project_id).first()
            if row is None:
                if type(expected_revision) is not int or expected_revision != 0:
                    raise Conflict('landing token has no matching revision')
                return snapshot(m.LandingToken.objects.create(project_id=project_id, owner_id=actor.user_id))
            if type(expected_revision) is int and expected_revision == 0:
                raise Conflict('landing token was already created')
            expected(row, expected_revision)
            if row.owner_id is not None:
                raise Conflict('landing token is already held')
            row.owner_id = actor.user_id
            row.revision += 1
            row.generation += 1
            row.save(update_fields=('owner', 'revision', 'generation'))
            return snapshot(row)

    def release(self, actor, project_id, *, expected_revision):
        human(actor)
        Access().authorize(actor, 'execution.release_landing', project_ids=(project_id,))
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=project_id)
            row = m.LandingToken.objects.select_for_update().get(project_id=project_id)
            expected(row, expected_revision)
            if row.owner_id != actor.user_id:
                raise Denied('landing belongs to another human')
            row.owner = None
            row.revision += 1
            row.generation += 1
            row.save(update_fields=('owner', 'revision', 'generation'))
            return snapshot(row)


class MachineExecution:
    def __init__(self, *, work: WorkPort | None = None, grants: GrantAuthority | None = None,
                 facts: FactReader | None = None, installation: InstallationQuery | None = None,
                 inspector: EffectInspector | None = None, lease_seconds=30, target_fence=None):
        self.target_fence = target_fence if target_fence is not None else unbound_target_fence
        self.work = work if work is not None else UnboundWork()
        self.grants = grants if grants is not None else UnboundGrants()
        self.reader = facts if facts is not None else UnboundFacts()
        self.installation = installation if installation is not None else unbound_installation
        self.inspector = inspector if inspector is not None else UnboundInspector()
        self.lease_seconds = bound(lease_seconds, 60)

    def _installation_state(self):
        value = self.installation()
        if type(value) is not AuthorityV1:
            raise Unavailable('invalid installation authority')
        return value

    def _epoch(self, work):
        value = self._installation_state()
        if value.epoch != work.epoch:
            raise Conflict('installation epoch changed; reconcile')
        return value

    def _authority(self, work):
        if not self._epoch(work).effects_enabled:
            raise Unavailable('installation effects are disabled')

    def _assignment(self, actor, work, action):
        if type(work) is not WorkV1:
            raise ValueError('typed version 1 work required')
        row = m.Assignment.objects.select_for_update().get(pk=work.target_id)
        machine(actor, row.node_id, action, (row.project_id,))
        if row.node_id != work.node_id or row.current_incarnation_id != work.incarnation_id:
            raise Conflict('target node or incarnation changed')
        incarnation = m.ExecutionIncarnation.objects.select_for_update().get(pk=work.incarnation_id)
        if (incarnation.assignment_id != row.id or incarnation.node_id != row.node_id
                or incarnation.executor_epoch != work.executor_epoch or not incarnation.accepting):
            raise Conflict('executor restarted; explicit reconciliation required')
        if (row.generation != work.generation or row.revision != work.revision
                or row.owner_id != work.owner_id or row.role != work.role
                or row.bundle_digest != work.bundle_digest or row.grant_id != work.grant_id):
            raise Conflict('assignment admission changed')
        return row

    def _admit(self, row, work):
        if (row.owner_id is None or not row.owner.is_active
                or row.expires_at <= timezone.now() or work.not_after <= timezone.now()):
            raise Conflict('assignment or command is no longer live')
        self.grants.authorize(owner_id=row.owner_id, project_id=row.project_id,
            target_id=row.id, grant_id=row.grant_id, role=row.role, bundle_digest=row.bundle_digest,
            incarnation_id=work.incarnation_id, action=work.action)
        if work.action == 'send_message' and not m.IdentityApplication.objects.filter(
                attempt__assignment_id=row.id, attempt__incarnation_id=work.incarnation_id,
                attempt__generation=row.generation, attempt__role=row.role,
                attempt__bundle_digest=row.bundle_digest, attempt__grant_id=row.grant_id).exists():
            raise Conflict('current identity must be confirmed before delivery')

    def poll(self, actor, node_id, *, executor_epoch, limit=10):
        machine(actor, node_id, 'poll_work')
        bound(limit, 50)
        if type(executor_epoch) is not UUID:
            raise ValueError('executor epoch UUID required')
        # Scope участвует в selection до limit, иначе чужой project заполнит
        # каждую страницу и легальная работа на общем node станет недостижимой.
        projects = tuple(NodeProject.objects.filter(node_id=node_id).order_by('project_id')
                         .values_list('project_id', flat=True)[:501])
        if len(projects) > 500:
            raise Unavailable('bounded node project scope exceeded')
        allowed = []
        for project_id in projects:
            try:
                machine(actor, node_id, 'poll_work', (project_id,))
            except Denied:
                continue
            allowed.append(project_id)
        machine(actor, node_id, 'poll_work')
        candidates = self.work.pending(node_id=node_id, executor_epoch=executor_epoch,
                                       project_ids=tuple(allowed), limit=limit)
        if type(candidates) is not tuple or len(candidates) > limit:
            raise Unavailable('work port returned an unbounded projection')
        result = []
        for work in candidates:
            if type(work) is not WorkV1 or work.node_id != node_id or work.executor_epoch != executor_epoch:
                raise Unavailable('work projection exceeds enrolled executor scope')
            row = m.Assignment.objects.get(pk=work.target_id)
            try:
                machine(actor, row.node_id, 'poll_work', (row.project_id,))
            except Denied:
                # Список отфильтровывается по текущему project scope, не раскрывает чужие ID.
                continue
            if not m.AssignmentAttempt.objects.filter(operation_id=work.operation_id, step_id=work.step_id).exists():
                result.append(work)
        return tuple(result)

    def claim(self, actor, work, *, request_id):
        current_actor(actor)
        # Разрешение может уйти исполнителю только после настоящего commit,
        # не после release savepoint неизвестной внешней транзакции caller.
        if transaction.get_connection().in_atomic_block:
            raise Unavailable('machine admission requires no surrounding transaction')
        if type(work) is not WorkV1:
            raise ValueError('typed version 1 work required')
        if type(request_id) is not UUID:
            raise ValueError('request UUID required')
        try:
            with transaction.atomic():
                # Infrastructure restore fence предшествует предметным locks.
                self._authority(work)
                row = self._assignment(actor, work, 'claim_work')
                unclaimed(self.target_fence, row.pk, excluding_operation=work.operation_id)
                # current обязуется проверить initiating actor/operation и не делать I/O.
                if self.work.current(work.operation_id, work.step_id) != work:
                    raise Conflict('admitted operation step changed')
                self._admit(row, work)
                previous = m.AssignmentAttempt.objects.filter(principal_id=actor.principal_id,
                                                               request_id=request_id).first()
                if previous:
                    if (self._work(previous) != work or previous.state != 'claimed'
                            or previous.lease_until <= timezone.now()):
                        raise Conflict('claim retry cannot replay or revive an effect')
                    return snapshot(previous)
                if m.AssignmentAttempt.objects.filter(assignment=row, state__in=('claimed', 'unknown')).exists():
                    raise Conflict('unresolved external effect holds resource')
                values = {name: getattr(work, name) for name in ('epoch', 'operation_id', 'step_id',
                    'executor_epoch', 'owner_id', 'generation', 'role', 'bundle_digest', 'grant_id', 'action', 'not_after')}
                attempt = m.AssignmentAttempt.objects.create(assignment=row,
                    incarnation_id=work.incarnation_id, principal_id=actor.principal_id,
                    request_id=request_id, assignment_revision=work.revision,
                    lease_until=min(work.not_after, timezone.now()+timedelta(seconds=self.lease_seconds)), **values)
                self.work.claimed(work, attempt.id)
                return snapshot(attempt)
        except IntegrityError:
            collision = m.AssignmentAttempt.objects.filter(
                Q(principal_id=actor.principal_id, request_id=request_id)
                | Q(operation_id=work.operation_id, step_id=work.step_id)
                | Q(assignment_id=work.target_id, state__in=('claimed', 'unknown'))).exists()
            if collision:
                raise Conflict('request, operation step or resource already claimed') from None
            raise

    def _work(self, attempt):
        return WorkV1(**{name: getattr(attempt, name) for name in ('epoch', 'operation_id', 'step_id',
            'executor_epoch', 'owner_id', 'generation', 'role', 'bundle_digest', 'grant_id', 'action', 'not_after')},
            target_id=attempt.assignment_id, incarnation_id=attempt.incarnation_id,
            node_id=attempt.incarnation.node_id, revision=attempt.assignment_revision)

    def _receipt(self, row):
        return ReceiptV1(row.id, row.attempt_id, row.resulting_revision, row.outcome, row.evidence)

    def result(self, actor, attempt_id, *, expected_revision, receipt_id, outcome, evidence):
        try:
            return self._result(actor, attempt_id, expected_revision=expected_revision,
                receipt_id=receipt_id, outcome=outcome, evidence=evidence)
        except IntegrityError:
            # Другой target имеет другой row lock, но receipt UUID общая identity.
            if m.ResultReceipt.objects.filter(pk=receipt_id).exists():
                raise Conflict('receipt identity already committed') from None
            raise

    def _result(self, actor, attempt_id, *, expected_revision, receipt_id, outcome, evidence):
        if type(receipt_id) is not UUID:
            raise ValueError('receipt UUID required')
        current_actor(actor)
        # Незаблокированная immutable binding задаёт порядок: guard -> Assignment -> Attempt.
        source = m.AssignmentAttempt.objects.select_related('incarnation').get(pk=attempt_id)
        target_id = source.assignment_id
        with transaction.atomic():
            self._epoch(self._work(source))
            row = m.Assignment.objects.select_for_update().get(pk=target_id)
            machine(actor, row.node_id, 'report_result', (row.project_id,))
            attempt = m.AssignmentAttempt.objects.select_for_update().get(pk=attempt_id)
            if attempt.principal_id != actor.principal_id:
                raise Denied('attempt belongs to another executor principal')
            previous = m.ResultReceipt.objects.filter(pk=receipt_id).first()
            if previous:
                if (previous.attempt_id != attempt.id or previous.expected_revision != expected_revision
                        or previous.outcome != outcome or previous.evidence != evidence):
                    raise Conflict('receipt identity already has different evidence')
                return self._receipt(previous)
            expected(attempt, expected_revision)
            work = self._work(attempt)
            self._assignment(actor, work, 'report_result')
            if attempt.state not in ('claimed', 'unknown'):
                raise Conflict('attempt is already terminal')
            return self._record_result(attempt, expected_revision, receipt_id, outcome, evidence)

    def _record_result(self, attempt, revision, receipt_id, outcome, evidence):
        result_codes(outcome, evidence)
        if outcome == 'failed' and evidence not in ('not-started', 'failed-before-effect', 'confirmed'):
            raise ValueError('failure requires confirmed effect boundary')
        if outcome == 'succeeded':
            confirmations = {'restart_session': ('session-replaced', 'confirmed'),
                'apply_identity': ('identity-applied',), 'confirm_ready': ('ready',), 'send_message': ('delivered',)}
            if evidence not in confirmations[attempt.action]:
                raise ValueError('named action confirmation required')
        # Результат после deadline подтверждает эффект, но не запускает новый.
        self.work.completed_result(self._work(attempt), attempt.id, outcome, evidence)
        attempt.state = outcome
        attempt.revision += 1
        attempt.save(update_fields=('state', 'revision'))
        receipt = m.ResultReceipt.objects.create(id=receipt_id, attempt=attempt,
            expected_revision=revision, resulting_revision=attempt.revision, outcome=outcome, evidence=evidence)
        if outcome == 'succeeded' and attempt.action == 'apply_identity':
            m.IdentityApplication.objects.create(attempt=attempt, receipt=receipt)
        if outcome == 'succeeded' and attempt.action == 'confirm_ready':
            m.ReadinessReceipt.objects.create(attempt=attempt, receipt=receipt)
        return self._receipt(receipt)

    def reconcile(self, actor, attempt_id, *, expected_revision, receipt_id):
        """Явная read-only сверка; quiescence приходит из trusted inspector, не JSON."""
        human(actor)
        if type(receipt_id) is not UUID:
            raise ValueError('receipt UUID required')
        if transaction.get_connection().in_atomic_block:
            raise Unavailable('effect inspection requires no surrounding transaction')
        source = m.AssignmentAttempt.objects.select_related('incarnation', 'assignment').get(pk=attempt_id)
        Access().authorize(actor, 'execution.reconcile_work', project_ids=(source.assignment.project_id,))
        previous = m.ResultReceipt.objects.filter(pk=receipt_id).first()
        if previous:
            if previous.attempt_id != source.id or previous.expected_revision != expected_revision:
                raise Conflict('reconciliation receipt identity changed')
            return self._receipt(previous)
        expected(source, expected_revision)
        if source.state != 'unknown':
            raise Conflict('explicit reconciliation requires an unknown attempt')
        work = self._work(source)
        # Исторический admission остаётся старого epoch. Recovery авторизуется
        # текущим controller, иначе restore оставил бы вечный неразрешимый fence.
        recovery_epoch = self._installation_state().epoch
        proof = self.inspector.inspect(work, source.id)
        if (type(proof) is not InspectionV1 or proof.attempt_id != source.id
                or proof.incarnation_id != work.incarnation_id or proof.executor_epoch != work.executor_epoch
                or not proof.quiescent or proof.outcome == 'unknown'):
            raise Conflict('old effect is not confirmed quiescent; resource remains held')
        try:
            with transaction.atomic():
                if self._installation_state().epoch != recovery_epoch:
                    raise Conflict('installation changed during effect inspection')
                row = m.Assignment.objects.select_for_update().get(pk=source.assignment_id)
                human(actor)
                Access().authorize(actor, 'execution.reconcile_work', project_ids=(row.project_id,))
                attempt = m.AssignmentAttempt.objects.select_for_update().get(pk=source.id)
                previous = m.ResultReceipt.objects.filter(pk=receipt_id).first()
                if previous:
                    if previous.attempt_id != attempt.id or previous.expected_revision != expected_revision:
                        raise Conflict('reconciliation receipt identity changed')
                    return self._receipt(previous)
                expected(attempt, expected_revision)
                if attempt.state != 'unknown' or self._work(attempt) != work:
                    raise Conflict('attempt changed during external inspection')
                # Закрываем только старый fence. Не присваиваем новую работу/инкарнацию.
                return self._record_result(attempt, expected_revision, receipt_id, proof.outcome, proof.evidence)
        except IntegrityError:
            if m.ResultReceipt.objects.filter(pk=receipt_id).exists():
                raise Conflict('receipt identity already committed') from None
            raise

    def reconcile_expired(self, *, limit=50):
        bound(limit, 100)
        ids = tuple(m.AssignmentAttempt.objects.filter(state='claimed', lease_until__lte=timezone.now())
                    .order_by('assignment_id').values_list('id', 'assignment_id')[:limit])
        changed = []
        for attempt_id, target_id in ids:
            with transaction.atomic():
                m.Assignment.objects.select_for_update().get(pk=target_id)
                attempt = m.AssignmentAttempt.objects.select_for_update().get(pk=attempt_id)
                if attempt.state != 'claimed' or attempt.lease_until > timezone.now():
                    continue
                self.work.completed_result(self._work(attempt), attempt.id, 'unknown', 'effect-running')
                attempt.state = 'unknown'
                attempt.revision += 1
                attempt.save(update_fields=('state', 'revision'))
                changed.append(attempt.id)
        return tuple(changed)

    def executor_restarted(self, actor, node_id, *, previous_epoch, new_epoch):
        machine(actor, node_id, 'claim_work')
        if type(previous_epoch) is not UUID or type(new_epoch) is not UUID or previous_epoch == new_epoch:
            raise ValueError('distinct executor epoch UUIDs required')
        with transaction.atomic():
            rows = list(m.Assignment.objects.select_for_update().filter(node_id=node_id).order_by('pk')[:501])
            if len(rows) > 500:
                raise Unavailable('executor restart exceeds bounded assignment set')
            for row in rows:
                machine(actor, node_id, 'claim_work', (row.project_id,))
            for row in rows:
                incarnation = m.ExecutionIncarnation.objects.select_for_update().filter(pk=row.current_incarnation_id).first()
                if incarnation is None:
                    continue
                effective_epoch = incarnation.replaced_by_epoch or incarnation.executor_epoch
                if effective_epoch == new_epoch:
                    # Потерянный ответ на прежний report не отзывает уже новую
                    # инкарнацию этого процесса, принятую после первого report.
                    continue
                if effective_epoch != previous_epoch:
                    raise Conflict('executor process epoch changed')
                incarnation.accepting = False
                incarnation.replaced_by_epoch = new_epoch
                incarnation.revision += 1
                incarnation.save(update_fields=('accepting', 'replaced_by_epoch', 'revision'))
                # Прежний expected revision не должен воскресить старый процесс
                # через activate после того, как restart сменил связанные факты.
                row.revision += 1
                row.save(update_fields=('revision',))
                for attempt in m.AssignmentAttempt.objects.select_for_update().filter(assignment=row, state='claimed'):
                    self.work.completed_result(self._work(attempt), attempt.id, 'unknown', 'connection-lost')
                    attempt.state = 'unknown'
                    attempt.revision += 1
                    attempt.save(update_fields=('state', 'revision'))
        return new_epoch

    def facts(self, actor, node_id, *, kind, limit=100):
        machine(actor, node_id, 'read_facts')
        bound(limit, 500)
        if kind not in FACT_FIELDS:
            raise ValueError('unknown fact kind')
        if transaction.get_connection().in_atomic_block:
            raise Unavailable('fact reads require no surrounding transaction')
        page = self.reader.read(node_id=node_id, kind=kind, limit=limit)
        if type(page) is not FactPageV1:
            raise Unavailable('invalid bounded fact projection')
        page.validate(node_id=node_id, kind=kind, limit=limit)
        for item in page.items:
            if item.target_id is None:
                if kind not in ('disk', 'health'):
                    raise Unavailable('target-bound fact identity required')
                continue
            target = m.Assignment.objects.get(pk=item.target_id)
            machine(actor, target.node_id, 'read_facts', (target.project_id,))
            if target.node_id != node_id:
                raise Denied('fact target is outside enrolled node')
            if item.incarnation_id != target.current_incarnation_id:
                raise Conflict('fact source incarnation changed')
        # Авторизацию повторяем после потенциального I/O, даже при пустой странице.
        machine(actor, node_id, 'read_facts')
        return page
