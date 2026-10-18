"""Короткие sync use cases. Fencing не обещает остановить физический процесс."""
from datetime import timedelta
from uuid import UUID
from django.db import IntegrityError, transaction
from django.db.models import Q, F
from django.utils import timezone
from mop.server.access.context import ActorKind
from mop.server.access.services import Access, Denied, current_actor, node_executor
from mop.server.catalog.services import snapshot, readmission
from mop.server.catalog.models import Project, NodeProject
from . import models as m
from .protocol import (WorkV1, AuthorityV1, ReceiptV1, FactPageV1, FACT_FIELDS,
    positive, role_binding, result_codes, WorkPort, GrantAuthority, FactReader,
    InstallationQuery, InspectionV1, EffectInspector, CleanCloneQuery, FactV1, AssignmentBinding,
    CredentialAssignment, ReplacementPort, StoppedSourceAuthority)


class Conflict(ValueError):
    """Текущий источник/ревизия/поколение не совпадает с admission."""


class Unavailable(RuntimeError):
    """Обязательный узкий port не связан или authority недоступна."""


class UnboundWork:
    def pending(self, **scope):
        raise Unavailable('operation work port is not bound')

    def current(self, operation_id, step_id):
        raise Unavailable('operation work port is not bound')

    def abort_unstarted(self, work):
        raise Unavailable('operation work port is not bound')

    def reconcile_expired(self, *, limit):
        raise Unavailable('operation work port is not bound')

    def claimed(self, work, attempt_id):
        raise Unavailable('operation work port is not bound')

    def completed_result(self, work, attempt_id, outcome, evidence):
        raise Unavailable('operation work port is not bound')

    def retired(self, target_id, executor_epoch):
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
    if actor.kind != ActorKind.HUMAN:
        raise Denied('human ownership action required')


def machine(actor, node_id, action, project_ids=()):
    current_actor(actor)
    node_executor(actor, node_id, reason='enrolled node service required')
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

    def replacement_execution(self, *, authority: ReplacementPort | None = None,
                              installation: InstallationQuery | None = None):
        """SQL lifecycle #625; отсутствие Root proof/restore port -- отказ."""
        from .replacement import ReplacementExecution
        return ReplacementExecution(self, authority=authority, installation=installation)

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

    def target(self, target_id, *, lock=False):
        """Внутренний SQL-only binding; lock берётся после installation guard."""
        if lock and not transaction.get_connection().in_atomic_block:
            raise Unavailable('assignment locking requires caller transaction')
        rows = m.Assignment.objects.select_for_update() if lock else m.Assignment.objects
        row = rows.filter(pk=target_id).first()
        return self.current(project_id=row.project_id, target_id=row.id) if row else None

    def credential_proof(self, actor, target_id, *, epoch):
        """SQL-only query в caller transaction; grant/secret versions проверяет Root.

        Assignment lock сохраняет verdict до commit, не переживает provider wait.
        Ready/identity — подтверждённые результаты именно текущего admission,
        не lease/claim и не просто наличие старого marker/receipt. Expiry попытки
        не отменяет подтверждённый эффект и ничего не говорит о transport revoke.
        """
        current_actor(actor)
        node_executor(actor, reason='enrolled node service required for addressed credentials')
        if type(target_id) is not UUID or type(epoch) is not UUID:
            raise Denied('current assignment target and installation epoch required')
        binding = self.target(target_id, lock=True)
        if binding is None:
            raise Denied('current addressed assignment is unavailable')
        Access().authorize(actor, 'secrets.deliver_secret', project_ids=(binding.project_id,),
            node_ids=(binding.node_id,))
        receipts = self.readiness(binding, epoch=epoch)
        if receipts is None:
            raise Denied('current identity and readiness confirmations required for addressed credentials')
        return CredentialAssignment(binding, epoch, *receipts)

    def management_proof(self, actor, request, *, epoch):
        """SQL-only post-activation runtime proof, без stage/lease authority.

        Sessions владеет accepting assignment, exact current producer, native и
        ready receipts и committed model event. Access видит только projection.
        """
        from mop.common.management_context import ManagementRequest
        from .protocol import ManagementAssignment, AuthorityV1
        from .producer import CurrentSessions
        from .producer_models import SessionProducer
        from .telemetry_models import SessionObservation, ModelObservation, TelemetryReceipt
        if (type(request) is not ManagementRequest or type(epoch) is not UUID
                or request.scope.epoch != epoch or not transaction.get_connection().in_atomic_block):
            raise Denied('current management runtime scope required')
        scope = request.scope
        current_actor(actor)
        node_executor(actor, scope.node_id, reason='own enrolled node executor required for management context')
        binding = self.target(scope.target_id, lock=True)
        if (binding is None or request.generation != binding.generation
                or any(getattr(scope, name) != getattr(binding, name) for name in
                    ('target_id', 'incarnation_id', 'node_id', 'executor_epoch'))):
            raise Denied('management assignment binding changed')
        Access().authorize(actor, 'access.read_management_context', project_ids=(binding.project_id,),
            node_ids=(binding.node_id,))
        receipts = self.readiness(binding, epoch=epoch)
        if receipts is None:
            raise Denied('current identity and readiness required for management context')
        source = CurrentSessions(installation=lambda: AuthorityV1(epoch, True)).current(binding)
        if source is None or (source.session_id, source.producer_epoch) != (scope.session_id, scope.producer_epoch):
            raise Denied('current native producer required for management context')
        producer = SessionProducer.objects.filter(currentproducer__incarnation_id=binding.incarnation_id,
            producer_epoch=scope.producer_epoch, executor_user_id=actor.user_id).first()
        ready = m.ResultReceipt.objects.select_related('attempt').get(pk=receipts[1])
        if producer is None or not m.ResultReceipt.objects.filter(attempt_id=producer.attempt_id,
                attempt__assignment_id=binding.target_id, attempt__incarnation_id=binding.incarnation_id,
                attempt__epoch=epoch, attempt__executor_epoch=binding.executor_epoch,
                attempt__owner_id=binding.owner_id, attempt__generation=binding.generation,
                attempt__assignment_revision=binding.revision, attempt__role=binding.role,
                attempt__bundle_digest=binding.bundle_digest, attempt__grant_id=binding.grant_id,
                attempt__action='native_exec', attempt__state='succeeded', attempt__executor_user_id=actor.user_id,
                attempt__operation_id=producer.operation_id, attempt__step_id=producer.step_id,
                outcome='succeeded', evidence='native-started', resulting_revision=F('attempt__revision'),
                expected_revision=F('attempt__revision')-1).exists():
            raise Denied('current successful native execution required for management context')
        if (ready.attempt.operation_id != producer.operation_id
                or ready.attempt.executor_user_id != producer.executor_user_id):
            raise Denied('readiness belongs to a different native producer')
        observation = SessionObservation.objects.filter(assignment_id=binding.target_id,
            incarnation_id=binding.incarnation_id, node_id=binding.node_id, epoch=epoch,
            executor_epoch=binding.executor_epoch, producer_epoch=scope.producer_epoch,
            session_id=scope.session_id, model_proven=True, confirmed_sequence__gte=1).first()
        model = None if observation is None else ModelObservation.objects.filter(observation=observation,
            sequence=observation.confirmed_sequence, model=observation.confirmed_model, kind='PostModelSwitch').first()
        receipt = None if model is None else TelemetryReceipt.objects.filter(observation=observation,
            sequence=model.sequence).first()
        if receipt is None:
            raise Denied('committed current-producer model receipt required for management context')
        expiry = m.Assignment.objects.get(pk=binding.target_id).expires_at
        return ManagementAssignment(binding, epoch, *receipts, producer.id, receipt.id, expiry)

    def readiness(self, binding, *, epoch):
        """SQL-only: подтверждённые identity и готовность именно текущего
        admission назначения -> (identity receipt, ready receipt) | None.

        Общая часть адресных credentials и квитанции активации гранта (#643):
        не lease/claim и не просто наличие старого receipt."""
        scope = dict(attempt__assignment_id=binding.target_id, attempt__incarnation_id=binding.incarnation_id,
            attempt__generation=binding.generation, attempt__assignment_revision=binding.revision,
            attempt__owner_id=binding.owner_id, attempt__role=binding.role,
            attempt__bundle_digest=binding.bundle_digest, attempt__grant_id=binding.grant_id,
            attempt__executor_epoch=binding.executor_epoch, attempt__epoch=epoch,
            attempt__state='succeeded', receipt__attempt_id=F('attempt_id'),
            receipt__outcome='succeeded', receipt__resulting_revision=F('attempt__revision'),
            receipt__expected_revision=F('attempt__revision') - 1)
        identity = m.IdentityApplication.objects.filter(**scope).filter(
            Q(attempt__action='apply_identity', receipt__evidence='identity-applied') |
            Q(attempt__action='initial_identity', receipt__evidence='initial-identity-applied')
        ).order_by('id').values_list('receipt_id', flat=True).first()
        ready = m.ReadinessReceipt.objects.filter(**scope, attempt__action='confirm_ready',
            receipt__evidence='ready').order_by('id').values_list('receipt_id', flat=True).first()
        return None if identity is None or ready is None else (identity, ready)

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
        if transaction.get_connection().in_atomic_block or not transaction.get_connection().get_autocommit():
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
            if row.deleted_at is not None:
                raise Conflict('assignment was deleted')
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

    def confirm_job_stopped(self, *, target_id, incarnation_id, expected_revision,
                            expected_generation, executor_epoch):
        """SQL-only historical confirmation; не отзываем новое назначение.

        Вызывается внутри caller result transaction после physical quiescence.
        Истечение человеческого назначения не отменяет historical stop receipt.
        """
        if not transaction.get_connection().in_atomic_block:
            raise Unavailable('stop confirmation requires caller transaction')
        row = m.Assignment.objects.select_for_update().get(pk=target_id)
        if (row.current_incarnation_id!=incarnation_id or row.revision!=expected_revision
                or row.generation!=expected_generation):
            return None
        incarnation = m.ExecutionIncarnation.objects.select_for_update().get(pk=incarnation_id)
        if incarnation.executor_epoch!=executor_epoch or incarnation.assignment_id!=row.id:
            return None
        incarnation.accepting = False
        incarnation.save(update_fields=('accepting',))
        row.revision += 1
        row.generation += 1
        row.save(update_fields=('revision','generation'))
        return row.revision

    def confirm_deleted(self, *, target_id, incarnation_id, expected_revision,
                        expected_generation, executor_epoch):
        """SQL-only закрытие назначения навсегда после подтверждённого purge (#625).

        Вызывается внутри caller result transaction. Как и historical stop, не
        трогает уже другое назначение: при сдвинутых revision/generation/
        инкарнации ничего не меняет. Владелец и инкарнация снимаются, поэтому
        current() его больше не видит, а activate() отказывает.
        """
        if not transaction.get_connection().in_atomic_block:
            raise Unavailable('delete confirmation requires caller transaction')
        row = m.Assignment.objects.select_for_update().get(pk=target_id)
        if (row.current_incarnation_id != incarnation_id or row.revision != expected_revision
                or row.generation != expected_generation):
            return None
        incarnation = m.ExecutionIncarnation.objects.select_for_update().get(pk=incarnation_id)
        if incarnation.executor_epoch != executor_epoch or incarnation.assignment_id != row.id:
            return None
        incarnation.accepting = False
        incarnation.save(update_fields=('accepting',))
        row.owner = None
        row.current_incarnation = None
        row.deleted_at = timezone.now()
        row.revision += 1
        row.generation += 1
        row.save(update_fields=('owner', 'current_incarnation', 'deleted_at', 'revision', 'generation'))
        return row.revision

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
    def get(self, actor, project_id):
        """Конечный authorized query; отсутствие строки не материализует токен."""
        Access().authorize(actor, 'execution.view_landing', project_ids=(project_id,))
        row = m.LandingToken.objects.filter(project_id=project_id).first()
        return None if row is None else snapshot(row)

    def acquire(self, actor, project_id, *, expected_revision):
        human(actor)
        Access().authorize(actor, 'execution.acquire_landing', project_ids=(project_id,))
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=project_id)
            row = m.LandingToken.objects.select_for_update().filter(project_id=project_id).first()
            human(actor)
            Access().authorize(actor, 'execution.acquire_landing', project_ids=(project_id,))
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
            row = m.LandingToken.objects.select_for_update().filter(project_id=project_id).first()
            human(actor)
            Access().authorize(actor, 'execution.release_landing', project_ids=(project_id,))
            if row is None:
                raise Conflict('landing token is unmaterialized')
            if type(expected_revision) is int and expected_revision == 0:
                raise Conflict('landing token was already created')
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
                 inspector: EffectInspector | None = None, lease_seconds=30, target_fence=None,
                 stopped_sources: StoppedSourceAuthority | None = None):
        from .replacement import UnboundReplacement
        self.stopped_sources = stopped_sources if stopped_sources is not None else UnboundReplacement()
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
                or incarnation.executor_epoch != work.executor_epoch):
            raise Conflict('executor restarted; explicit reconciliation required')
        if not incarnation.accepting:
            if work.action != 'wipe_clone':
                raise Conflict('executor retired; explicit reconciliation required')
            self.stopped_sources.authorize(work)
        if (row.generation != work.generation or row.revision != work.revision
                or row.owner_id != work.owner_id or row.role != work.role
                or row.bundle_digest != work.bundle_digest or row.grant_id != work.grant_id):
            raise Conflict('assignment admission changed')
        return row

    def _admit(self, row, work):
        if (row.owner_id is None or not row.owner.is_active
                or row.expires_at <= timezone.now() or work.not_after <= timezone.now()):
            raise Conflict('assignment or command is no longer live')
        if not m.ExecutionIncarnation.objects.get(pk=work.incarnation_id).accepting:
            # Не ослабляем generic grant gate: old binding для exact recycle
            # wipe проверяется отдельным SQL Root port, не current().
            self.stopped_sources.authorize(work)
        else:
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
        if transaction.get_connection().in_atomic_block or not transaction.get_connection().get_autocommit():
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
                # Abort без новой attempt должен пережить HTTP-отказ, поэтому
                # исключение выходит лишь после commit этой короткой транзакции.
                aborted = self.work.abort_unstarted(work)
                if not aborted:
                    # current проверяет initiating actor/operation без I/O.
                    if self.work.current(work.operation_id, work.step_id) != work:
                        raise Conflict('admitted operation step changed')
                    self._admit(row, work)
                    previous = m.AssignmentAttempt.objects.filter(executor_user_id=actor.user_id,
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
                    seconds = (self.work.effect_lease(work) if hasattr(self.work, 'effect_lease') else self.lease_seconds)
                    if type(seconds) is not int or not 1 <= seconds <= 320:
                        raise Unavailable('bounded phase effect lease required')
                    attempt = m.AssignmentAttempt.objects.create(assignment=row,
                        incarnation_id=work.incarnation_id, executor_user_id=actor.user_id,
                        request_id=request_id, assignment_revision=work.revision,
                        lease_until=min(work.not_after, timezone.now()+timedelta(seconds=seconds)), **values)
                    self.work.claimed(work, attempt.id)
                    return snapshot(attempt)
            raise Conflict('insufficient intent remaining for phase and cleanup; create aborted')
        except IntegrityError:
            collision = m.AssignmentAttempt.objects.filter(
                Q(executor_user_id=actor.user_id, request_id=request_id)
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

    def ready_receipt(self, actor, request):
        """Короткая read-only транзакция без mint/эффекта; result повторяет proof."""
        from .readiness import query
        with transaction.atomic():
            return query(self, actor, request)

    def result(self, actor, attempt_id, *, expected_revision, receipt_id, outcome, evidence, ready_evidence=None):
        if transaction.get_connection().in_atomic_block or not transaction.get_connection().get_autocommit():
            raise Unavailable('machine result requires no surrounding transaction')
        try:
            return self._result(actor, attempt_id, expected_revision=expected_revision,
                receipt_id=receipt_id, outcome=outcome, evidence=evidence, ready_evidence=ready_evidence)
        except IntegrityError:
            # Другой target имеет другой row lock, но receipt UUID общая identity.
            if m.ResultReceipt.objects.filter(pk=receipt_id).exists():
                raise Conflict('receipt identity already committed') from None
            raise

    def _result(self, actor, attempt_id, *, expected_revision, receipt_id, outcome, evidence, ready_evidence=None):
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
            if attempt.executor_user_id != actor.user_id:
                raise Denied('attempt belongs to another executor actor')
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
            return self._record_result(attempt, expected_revision, receipt_id, outcome, evidence,
                actor=actor, ready_evidence=ready_evidence)

    def _record_result(self, attempt, revision, receipt_id, outcome, evidence, *, actor=None, ready_evidence=None):
        result_codes(outcome, evidence)
        if ready_evidence is not None and (attempt.action != 'confirm_ready' or outcome != 'succeeded'):
            raise ValueError('readiness evidence is only valid for successful confirm-ready')
        if outcome == 'failed' and evidence not in ('not-started', 'failed-before-effect', 'confirmed'):
            raise ValueError('failure requires confirmed effect boundary')
        if outcome == 'succeeded':
            confirmations = {'restart_session': ('session-replaced', 'confirmed'),
                'apply_identity': ('identity-applied',), 'confirm_ready': ('ready',), 'send_message': ('delivered',),
                'wipe_clone': ('clone-wiped',)}
            from mop.common.initialcreate import CONFIRMATIONS
            confirmations.update({name: (code,) for name, code in CONFIRMATIONS.items()})
            if evidence not in confirmations[attempt.action]:
                raise ValueError('named action confirmation required')
        if outcome == 'succeeded' and attempt.action in ('native_exec', 'confirm_ready'):
            from .producer_models import CurrentProducer
            producer = CurrentProducer.objects.select_related('producer').filter(incarnation_id=attempt.incarnation_id).first()
            if attempt.action == 'native_exec':
                if (producer is None or producer.producer.attempt_id != attempt.id
                        or producer.producer.operation_id != attempt.operation_id or producer.producer.step_id != attempt.step_id
                        or producer.producer.executor_user_id != attempt.executor_user_id):
                    raise Conflict('exact native exec producer registration ACK required')
            elif hasattr(self.work, 'initial_native_attempt'):
                native_attempt = self.work.initial_native_attempt(self._work(attempt))
                if native_attempt is not None:
                    from mop.common.readiness import ReadyEvidence
                    from .readiness import query
                    if (actor is None or type(ready_evidence) is not ReadyEvidence
                            or ready_evidence.request.attempt_id != attempt.id
                            or ready_evidence.request.revision != revision):
                        raise Conflict('exact initial ready query evidence required for result')
                    # Не достаточно найти какое-то новое valid evidence: нужен тот
                    # же snapshot query. Assignment lock держит telemetry/producer
                    # до completed_result и COMMIT, время проверяется заново.
                    if query(self, actor, ready_evidence.request) != ready_evidence:
                        raise Conflict('initial readiness evidence changed before result commit')
            if attempt.action == 'confirm_ready' and producer is not None:
                # Post-launch readiness не превращается в initial-create admission.
                if (producer.producer.assignment_id != attempt.assignment_id
                        or producer.producer.owner_id != attempt.owner_id or producer.producer.generation != attempt.generation
                        or producer.producer.executor_epoch != attempt.executor_epoch):
                    raise Conflict('current readiness producer binding changed')
        # Результат после deadline подтверждает эффект, но не запускает новый.
        self.work.completed_result(self._work(attempt), attempt.id, outcome, evidence)
        attempt.state = outcome
        attempt.revision += 1
        attempt.save(update_fields=('state', 'revision'))
        receipt = m.ResultReceipt.objects.create(id=receipt_id, attempt=attempt,
            expected_revision=revision, resulting_revision=attempt.revision, outcome=outcome, evidence=evidence)
        if outcome == 'succeeded' and attempt.action in ('apply_identity', 'initial_identity'):
            m.IdentityApplication.objects.create(attempt=attempt, receipt=receipt)
        if outcome == 'succeeded' and attempt.action == 'confirm_ready':
            m.ReadinessReceipt.objects.create(attempt=attempt, receipt=receipt)
        return self._receipt(receipt)

    def reconcile(self, actor, attempt_id, *, expected_revision, receipt_id):
        """Явная read-only сверка; quiescence приходит из trusted inspector, не JSON."""
        human(actor)
        if type(receipt_id) is not UUID:
            raise ValueError('receipt UUID required')
        if transaction.get_connection().in_atomic_block or not transaction.get_connection().get_autocommit():
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
        # Gaps не создают фиктивных attempt IDs: прежний return остаётся
        # списком изменённых attempts, а abort публикуется Operation event'ом.
        self.work.reconcile_expired(limit=limit)
        ids = tuple(m.AssignmentAttempt.objects.filter(state='claimed', lease_until__lte=timezone.now())
                    .order_by('assignment_id').values_list('id', 'assignment_id')[:limit])
        changed = []
        for attempt_id, target_id in ids:
            with transaction.atomic():
                self._installation_state()
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

    def action_work(self, actor, *, operation_id, step_id):
        """Текущий addressed input gate; JSON owner/project не даёт полномочий."""
        work, _ = self.action_admission(actor, operation_id=operation_id, step_id=step_id)
        return work

    def action_admission(self, actor, *, operation_id, step_id):
        """Публичный SQL proof: собственная Work и id её текущего claim.

        Durable operation проверяет свой token по этому id, не читая execution ORM.
        """
        current_actor(actor)
        with transaction.atomic():
            self._installation_state()
            source = m.AssignmentAttempt.objects.select_related('incarnation').get(
                operation_id=operation_id, step_id=step_id, executor_user_id=actor.user_id)
            work = self._work(source)
            self._authority(work)
            row = self._assignment(actor, work, 'claim_work')
            attempt = m.AssignmentAttempt.objects.select_for_update().get(pk=source.id)
            if attempt.state != 'claimed' or attempt.lease_until <= timezone.now():
                raise Conflict('action input admission expired or completed')
            if self.work.current(operation_id, step_id) != work:
                raise Conflict('action input operation changed')
            self._admit(row, work)
            return work, attempt.id

    def executor_state(self, actor, node_id):
        """Предыдущий epoch приходит из controller DB, не имени/marker нового агента."""
        machine(actor, node_id, 'claim_work')
        with transaction.atomic():
            self._installation_state()
            rows = tuple(m.Assignment.objects.select_for_update().filter(node_id=node_id).order_by('pk')[:501])
            if len(rows) > 500:
                raise Unavailable('executor discovery exceeds bounded assignment set')
            epochs = set()
            for row in rows:
                machine(actor, node_id, 'claim_work', (row.project_id,))
                incarnation = m.ExecutionIncarnation.objects.filter(pk=row.current_incarnation_id).first()
                if incarnation is not None:
                    epochs.add(incarnation.replaced_by_epoch or incarnation.executor_epoch)
            # Заявленная эпоха (#643) обязана совпасть с инкарнациями, а на
            # пустом узле она -- единственный источник.
            registered = m.NodeExecutor.objects.filter(node_id=node_id).values_list('executor_epoch', flat=True).first()
            if registered is not None:
                epochs.add(registered)
            if len(epochs) != 1:
                raise Unavailable('executor epoch requires explicit installation registration or reconciliation')
            return epochs.pop()

    def host_executor_state(self, actor, node_id):
        """Registry + отсутствие незавершённых scopes; не physical process proof."""
        from mop.server.catalog.models import Node
        machine(actor, node_id, 'claim_work')
        with transaction.atomic():
            self._installation_state()
            node = Node.objects.select_for_update().get(pk=node_id)
            if node.driver != 'host':
                raise Conflict('HOST backend binding requires the enrolled HOST node')
            previous = self.executor_state(actor, node_id)
            # Историческая non-accepting incarnation сама по себе не доказывает
            # physical retirement. Не превращаем потерю root ownership в adoption.
            empty = not (m.ExecutionIncarnation.objects.filter(node_id=node_id).exists()
                or m.AssignmentAttempt.objects.filter(assignment__node_id=node_id,
                    state__in=('claimed', 'unknown')).exists())
            return previous, empty

    def host_executor_bound(self, actor, node_id, *, epoch, previous_epoch, new_epoch):
        """Empty-only CAS без generic restarted/retire и без replay effects."""
        if any(type(value) is not UUID or value.int == 0 for value in (epoch, previous_epoch, new_epoch)):
            raise ValueError('nonzero typed HOST binding epochs required')
        if transaction.get_connection().in_atomic_block or not transaction.get_connection().get_autocommit():
            raise Unavailable('HOST backend binding requires no surrounding transaction')
        with transaction.atomic():
            if self._installation_state().epoch != epoch:
                raise Conflict('HOST installation epoch changed; no automatic backend rebind')
            current_epoch, empty = self.host_executor_state(actor, node_id)
            current = m.NodeExecutor.objects.select_for_update().filter(node_id=node_id).first()
            if current is None or current.executor_epoch != current_epoch:
                raise Conflict('HOST backend requires explicit node executor registration')
            if current.executor_epoch == new_epoch:
                return new_epoch
            if not empty:
                raise Conflict('HOST old incarnation/effect ownership unresolved; no automatic retirement')
            if current.executor_epoch != previous_epoch:
                raise Conflict('HOST node executor epoch changed')
            current.executor_epoch, current.registered_at = new_epoch, timezone.now()
            current.save(update_fields=('executor_epoch', 'registered_at'))
        return new_epoch

    def executor_restarted(self, actor, node_id, *, previous_epoch, new_epoch):
        if transaction.get_connection().in_atomic_block or not transaction.get_connection().get_autocommit():
            raise Unavailable('executor restart requires no surrounding transaction')
        machine(actor, node_id, 'claim_work')
        if type(previous_epoch) is not UUID or type(new_epoch) is not UUID or previous_epoch == new_epoch:
            raise ValueError('distinct executor epoch UUIDs required')
        with transaction.atomic():
            self._installation_state()
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
                self.work.retired(row.id, previous_epoch)
            # #643: заявление процесса фиксируется и на узле без назначений;
            # повтор того же заявления идемпотентен, чужая предыдущая эпоха --
            # конфликт, как у инкарнаций выше.
            current = m.NodeExecutor.objects.select_for_update().filter(node_id=node_id).first()
            if current is None:
                m.NodeExecutor.objects.create(node_id=node_id, executor_epoch=new_epoch)
            elif current.executor_epoch != new_epoch:
                if current.executor_epoch != previous_epoch:
                    raise Conflict('executor process epoch changed')
                current.executor_epoch, current.registered_at = new_epoch, timezone.now()
                current.save(update_fields=('executor_epoch', 'registered_at'))
        return new_epoch

    def current_epoch(self, node_id):
        """Эпоха executor для новой инкарнации (#643): заявленная узлом, иначе
        общая у его текущих инкарнаций; неизвестна -- None, не догадка."""
        registered = m.NodeExecutor.objects.filter(node_id=node_id).values_list('executor_epoch', flat=True).first()
        if registered is not None:
            return registered
        current = m.Assignment.objects.filter(node_id=node_id).values('current_incarnation_id')
        epochs = {row.replaced_by_epoch or row.executor_epoch
                  for row in m.ExecutionIncarnation.objects.filter(pk__in=current)}
        return epochs.pop() if len(epochs) == 1 else None

    def facts(self, actor, node_id, *, kind, limit=100):
        machine(actor, node_id, 'read_facts')
        bound(limit, 500)
        if kind not in FACT_FIELDS:
            raise ValueError('unknown fact kind')
        if transaction.get_connection().in_atomic_block or not transaction.get_connection().get_autocommit():
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
        # Повторный допуск после потенциального I/O — одна общая короткая
        # SQL-only секция под locks (#687), теми же словами допуска.
        readmission(lambda: machine(actor, node_id, 'read_facts'), reason='fact readmission')
        return page
