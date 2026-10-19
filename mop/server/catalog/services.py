"""Короткие серверные use cases; порты связывает только composition root."""
from dataclasses import dataclass
import hashlib
import re
from typing import Protocol
from urllib.parse import urlsplit

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from . import models as m
from .definitions import DEFINITIONS, resolve, validate_batch
from .policy import project_origin, project_limit
from .retirement import (RemovalGuard, UnsafeRemoval, ProjectRemovalProof, NodeRemovalProof,
    PreparedProjectRetirement, PreparedNodeRetirement, validate_proof,
    validate_project_receipt, validate_node_receipt)


class Conflict(ValueError):
    """Identity или ожидаемая revision уже изменились."""


class ProtectionUnavailable(ValueError):
    """Конфиденциальный ввод не допускается без #637."""


class Authorizer(Protocol):
    def authorize(self, actor, action, *, project_ids=(), node_ids=(), client_ids=()): ...


class PolicyProtection(Protocol):
    def protect(self, content: bytes, *, project_id, digest: str) -> tuple[bytes, str]: ...
    def reveal(self, ciphertext: bytes, *, project_id, digest: str, reference: str) -> bytes: ...


class Deny:
    def authorize(self, actor, action, **refs):
        raise PermissionError('catalogue authorizer is not bound')


def readmission(recheck, *args, reason='readmission'):
    """Общий повторный допуск после I/O и перед эффектом/выдачей (#687).

    Один контракт для scheduler/worker/приватных конвейеров: короткая
    SQL-only секция под locks, сеть и crypto остались снаружи. Вход из
    активной транзакции — ошибка состава, а не вложенный savepoint.
    """
    if transaction.get_connection().in_atomic_block or not transaction.get_connection().get_autocommit():
        raise PermissionError(reason + ' requires an outer transaction-free boundary')
    with transaction.atomic():
        return recheck(*args)


@dataclass(frozen=True)
class Snapshot:
    """Только скалярные значения, ни одной ленивой ORM-ссылки."""
    fields: tuple

    def __getattr__(self, name):
        for key, value in self.fields:
            if key == name:
                return value
        raise AttributeError(name)


@dataclass(frozen=True)
class SettingsSnapshot:
    revision: int
    values: tuple
    applied_revision: int | None = None
    receipt: str = ''


@dataclass(frozen=True)
class EffectiveSettingsSnapshot:
    installation_revision: int
    node_revision: int | None
    values: tuple
    sources: tuple
    missing: tuple
    installation_applied_revision: int | None
    node_applied_revision: int | None


def snapshot(row):
    return Snapshot(tuple((f.attname, bytes(value) if isinstance(value, memoryview) else value)
        for f in row._meta.concrete_fields for value in (getattr(row, f.attname),)))


def expected(row, revision, field='revision'):
    if type(revision) is not int or revision < 1:
        raise ValueError('expected revision must be a positive integer')
    if getattr(row, field) != revision:
        raise Conflict('catalogue revision changed')


def text(value, name, maximum=256):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError('invalid ' + name)
    return value


def flag(value):
    if type(value) is not bool:
        raise ValueError('boolean value required')
    return value


def observation(source, observed_at, receipt=None):
    text(source, 'observation source')
    if not isinstance(observed_at, timezone.datetime) or timezone.is_naive(observed_at) or observed_at > timezone.now():
        raise ValueError('observation requires a nonfuture aware timestamp')
    if receipt is not None:
        text(receipt, 'application receipt', 512)


class Catalog:
    def __init__(self, *, authorizer: Authorizer | None = None, protection: PolicyProtection | None = None,
                 removal_guard: RemovalGuard | None = None):
        self._authorizer = authorizer if authorizer is not None else Deny()
        self._protection = protection
        self._removal_guard = removal_guard

    def _authorize(self, actor, action, *, projects=(), nodes=(), clients=()):
        self._authorizer.authorize(actor, action, project_ids=tuple(sorted(set(projects), key=str)),
            node_ids=tuple(sorted(set(nodes), key=str)), client_ids=tuple(sorted(set(clients), key=str)))

    def _node_scope(self, node_id):
        return tuple(m.NodeProject.objects.filter(node_id=node_id, project__retired_at__isnull=True)
                     .values_list('project_id', flat=True))

    @staticmethod
    def project_is_current(project_id):
        """Внутренний SQL fact для Access/SessionGate, не authorization."""
        return m.Project.objects.filter(pk=project_id, retired_at__isnull=True).exists()

    @staticmethod
    def node_is_current(node_id):
        """Retained FK не делает SERVICE principal текущим узлом."""
        return m.Node.objects.filter(pk=node_id, retired_at__isnull=True).exists()

    @staticmethod
    def current_project_ids(project_ids=None, *, limit=None):
        """SQL scope query для Access; None — все current, пустой набор — никто."""
        rows = m.Project.objects.filter(retired_at__isnull=True)
        if project_ids is not None:
            rows = rows.filter(pk__in=project_ids)
        rows = rows.order_by('pk').values_list('pk', flat=True)
        if limit is not None:
            if type(limit) is not int or limit < 1:
                raise ValueError('positive project scope limit required')
            rows = rows[:limit]
        return tuple(rows)

    @staticmethod
    def current_node_ids(node_ids):
        """Batch currentness без утечки retirement predicate в Access."""
        return tuple(m.Node.objects.filter(pk__in=node_ids, retired_at__isnull=True)
                     .order_by('pk').values_list('pk', flat=True))

    @staticmethod
    def _live(row):
        if row.retired_at is not None:
            raise Conflict('catalogue target is retired')
        return row

    def _locked_project(self, project_id):
        return self._live(m.Project.objects.select_for_update().get(pk=project_id))

    def _locked_node(self, node_id):
        return self._live(m.Node.objects.select_for_update().get(pk=node_id))

    def _removal_inspection_boundary(self):
        if connection.in_atomic_block:
            raise RuntimeError('removal inspection requires execution outside SQL transactions')

    def _guard(self):
        if self._removal_guard is None:
            raise UnsafeRemoval('fresh jobs, occupants and external guards are not bound')
        return self._removal_guard

    @staticmethod
    def _retire(row):
        row.retired_at, row.identity_slot = timezone.now(), row.id
        row.revision += 1
        row.save(update_fields=['retired_at', 'identity_slot', 'revision'])
        return snapshot(row)

    def create_project(self, actor, origin, *, limit=None):
        """Строгое создание: повторный add не превращается в update без CAS.

        Register остаётся идемпотентным для своих callers; начальная настройка
        имеет известную revision только у действительно новой строки.
        """
        value, limit = project_origin(origin), project_limit(limit)
        self._authorize(actor, 'catalog.add_project')
        try:
            with transaction.atomic():
                row = m.Project.objects.create(origin=value.origin, name=value.name)
                self._authorize(actor, 'catalog.add_project', projects=(row.id,))
                if limit is not None:
                    return self.set_project_limit(actor, row.id, limit, expected_revision=1)
                return snapshot(row)
        except IntegrityError as error:
            raise Conflict('project origin or name is already registered') from error

    def register_project(self, actor, origin):
        value = project_origin(origin)
        self._authorize(actor, 'catalog.add_project')
        try:
            with transaction.atomic():
                row, _ = m.Project.objects.select_for_update().get_or_create(
                    origin=value.origin, retired_at__isnull=True, defaults={'name': value.name})
                self._authorize(actor, 'catalog.add_project', projects=(row.id,))
                return snapshot(row)
        except IntegrityError as error:
            raise Conflict('project name belongs to another origin') from error

    def get_project(self, actor, project_id):
        row = m.Project.objects.get(pk=project_id, retired_at__isnull=True)
        self._authorize(actor, 'catalog.view_project', projects=(row.id,))
        return snapshot(row)

    def list_projects(self, actor):
        self._authorize(actor, 'catalog.view_project')
        result = []
        for row in m.Project.objects.filter(retired_at__isnull=True).order_by('id'):
            try:
                self._authorize(actor, 'catalog.view_project', projects=(row.id,))
            except PermissionError:
                continue
            result.append(snapshot(row))
        # Отзыв, замеченный при обходе, не превращается в частичный private result.
        self._authorize(actor, 'catalog.view_project', projects=tuple(row.id for row in result))
        return tuple(result)

    def configure_project(self, actor, project_id, *, origin, limit, expected_revision):
        """Редактирование aggregate: CAS обязателен даже при совпавших значениях."""
        value, limit = project_origin(origin), project_limit(limit)
        with transaction.atomic():
            row = self._locked_project(project_id)
            self._authorize(actor, 'catalog.change_project', projects=(row.id,))
            expected(row, expected_revision)
            result = snapshot(row)
            if result.origin != value.origin:
                result = self.replace_project_origin(actor, row.id, value.origin, expected_revision=result.revision)
            if result.puppet_limit != limit:
                result = self.set_project_limit(actor, row.id, limit, expected_revision=result.revision)
            return result

    def replace_project_origin(self, actor, project_id, origin, *, expected_revision):
        value = project_origin(origin)
        try:
            with transaction.atomic():
                row = self._locked_project(project_id)
                self._authorize(actor, 'catalog.change_project', projects=(row.id,))
                expected(row, expected_revision)
                row.origin, row.name = value.origin, value.name
                row.revision += 1
                row.save(update_fields=['origin', 'name', 'revision'])
                return snapshot(row)
        except IntegrityError as error:
            raise Conflict('project origin or name is already registered') from error

    def set_project_limit(self, actor, project_id, limit, *, expected_revision):
        value = project_limit(limit)
        with transaction.atomic():
            row = self._locked_project(project_id)
            self._authorize(actor, 'catalog.set_project_limit', projects=(row.id,))
            expected(row, expected_revision)
            row.puppet_limit = value
            row.revision += 1
            row.save(update_fields=['puppet_limit', 'revision'])
            return snapshot(row)

    def prepare_delete_project(self, actor, project_id, *, expected_revision):
        self._removal_inspection_boundary()
        row = m.Project.objects.get(pk=project_id)
        self._authorize(actor, 'catalog.delete_project', projects=(row.id,))
        self._live(row)
        expected(row, expected_revision)
        proof = self._guard().inspect_project(snapshot(row))
        validate_proof(proof, ProjectRemovalProof, row.id, expected_revision)
        return PreparedProjectRetirement(row.id, expected_revision, proof)

    def commit_delete_project(self, actor, prepared):
        if type(prepared) is not PreparedProjectRetirement:
            raise UnsafeRemoval('prepared project retirement is required')
        with transaction.atomic():
            row = m.Project.objects.select_for_update().get(pk=prepared.target_id)
            self._authorize(actor, 'catalog.delete_project', projects=(row.id,))
            self._live(row)
            expected(row, prepared.expected_revision)
            validate_proof(prepared.proof, ProjectRemovalProof, row.id, row.revision)
            receipt = self._guard().validate_project(prepared.proof, snapshot(row))
            validate_project_receipt(receipt, prepared.proof, timezone.now())
            return self._retire(row)

    def delete_project(self, actor, project_id, *, expected_revision):
        prepared = self.prepare_delete_project(actor, project_id, expected_revision=expected_revision)
        return self.commit_delete_project(actor, prepared)

    def configure_node(self, actor, *, name, driver, ssh_host, ssh_port, control_origin,
                       lan_address, bootstrap_address, node_id=None, expected_revision=None):
        text(name, 'node name', 128)
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', name) or driver not in ('host', 'pve'):
            raise ValueError('invalid node name or driver')
        if type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
            raise ValueError('invalid SSH port')
        for address in (ssh_host, lan_address, bootstrap_address):
            text(address, 'node address', 253)
            if any(c.isspace() for c in address) or '/' in address or address.startswith('-'):
                raise ValueError('invalid node address')
        text(control_origin, 'control origin', 2048)
        try:
            url = urlsplit(control_origin)
            if (url.scheme != 'https' or not url.hostname or url.username is not None
                    or url.password is not None or url.path not in ('', '/') or url.query or url.fragment
                    or (url.port is not None and not 1 <= url.port <= 65535)):
                raise ValueError('invalid HTTPS control origin')
        except ValueError:
            raise ValueError('invalid HTTPS control origin') from None
        values = dict(name=name, driver=driver, ssh_host=ssh_host, ssh_port=ssh_port,
            control_origin=control_origin, lan_address=lan_address, bootstrap_address=bootstrap_address)
        try:
            with transaction.atomic():
                if node_id is None:
                    self._authorize(actor, 'catalog.add_node')
                    return snapshot(m.Node.objects.create(**values))
                row = self._locked_node(node_id)
                self._authorize(actor, 'catalog.change_node', nodes=(row.id,), projects=self._node_scope(row.id))
                expected(row, expected_revision)
                for key, value in values.items():
                    setattr(row, key, value)
                row.revision += 1
                row.save()
                return snapshot(row)
        except IntegrityError as error:
            raise Conflict('node name is already registered') from error

    def configure_pool_node(self, actor, *, name, driver, ssh_host, ssh_port, control_origin,
                            lan_address, bootstrap_address, deployment_member, project_ids,
                            node_id=None, expected_revision=None):
        """Единая транзакция desired configuration/membership, без deploy/eligibility."""
        flag(deployment_member)
        requested = set(project_ids)
        with transaction.atomic():
            result = self.configure_node(actor, name=name, driver=driver, ssh_host=ssh_host,
                ssh_port=ssh_port, control_origin=control_origin, lan_address=lan_address,
                bootstrap_address=bootstrap_address, node_id=node_id, expected_revision=expected_revision)
            if result.deployment_member != deployment_member:
                result = self.set_deployment_membership(actor, result.id, deployment_member,
                                                       expected_revision=result.revision)
            previous = set(m.NodeProject.objects.filter(node_id=result.id).values_list('project_id', flat=True))
            if requested != previous:
                result = self.set_node_projects(actor, result.id, requested, expected_revision=result.revision)
            return result

    def get_node(self, actor, node_id):
        row = m.Node.objects.get(pk=node_id, retired_at__isnull=True)
        self._authorize(actor, 'catalog.view_node', nodes=(row.id,), projects=self._node_scope(row.id))
        return snapshot(row)

    def list_nodes(self, actor):
        self._authorize(actor, 'catalog.view_node')
        result = []
        for row in m.Node.objects.filter(retired_at__isnull=True).order_by('id'):
            try:
                self._authorize(actor, 'catalog.view_node', nodes=(row.id,), projects=self._node_scope(row.id))
            except PermissionError:
                continue
            result.append(snapshot(row))
        return tuple(result)

    def _change_node_flag(self, actor, node_id, field, value, revision, *, action='catalog.change_node'):
        flag(value)
        with transaction.atomic():
            row = self._locked_node(node_id)
            self._authorize(actor, action, nodes=(row.id,), projects=self._node_scope(row.id))
            expected(row, revision)
            setattr(row, field, value)
            row.revision += 1
            row.save(update_fields=[field, 'revision'])
            return snapshot(row)

    def set_deployment_membership(self, actor, node_id, member, *, expected_revision):
        return self._change_node_flag(actor, node_id, 'deployment_member', member, expected_revision)

    def set_node_eligibility(self, actor, node_id, eligible, *, expected_revision):
        return self._change_node_flag(actor, node_id, 'desired_eligible', eligible, expected_revision)

    def request_node_eligibility(self, actor, node_id, eligible, *, expected_revision):
        """Command intent: отдельное CONTROL право, не permission обход."""
        flag(eligible)
        action = 'catalog.enable_node' if eligible else 'catalog.drain_node'
        return self._change_node_flag(actor, node_id, 'desired_eligible', eligible, expected_revision, action=action)

    def set_node_projects(self, actor, node_id, project_ids, *, expected_revision):
        ids = set(project_ids)
        with transaction.atomic():
            projects = tuple(m.Project.objects.select_for_update().filter(pk__in=ids, retired_at__isnull=True)
                             .order_by('id').values_list('id', flat=True))
            if len(projects) != len(ids):
                raise ValueError('unknown project reference')
            row = self._locked_node(node_id)
            old = self._node_scope(row.id)
            self._authorize(actor, 'catalog.change_node', nodes=(row.id,), projects=(*old, *projects))
            expected(row, expected_revision)
            m.NodeProject.objects.filter(node=row).exclude(project_id__in=projects).delete()
            for project_id in projects:
                m.NodeProject.objects.get_or_create(node=row, project_id=project_id)
            row.revision += 1
            row.save(update_fields=['revision'])
            return snapshot(row)

    def record_node_application(self, actor, node_id, *, target_revision, receipt):
        text(receipt, 'application receipt', 512)
        with transaction.atomic():
            row = self._locked_node(node_id)
            self._authorize(actor, 'catalog.record_node_application', nodes=(row.id,), projects=self._node_scope(row.id))
            if type(target_revision) is not int or row.revision != target_revision:
                return False
            row.applied_revision, row.applied_receipt, row.applied_at = target_revision, receipt, timezone.now()
            row.save(update_fields=['applied_revision', 'applied_receipt', 'applied_at'])
            return True

    def observe_node(self, actor, node_id, *, eligible, source, observed_at):
        flag(eligible)
        observation(source, observed_at)
        with transaction.atomic():
            row = self._locked_node(node_id)
            self._authorize(actor, 'catalog.observe_node', nodes=(row.id,), projects=self._node_scope(row.id))
            if row.observed_at is not None and observed_at <= row.observed_at:
                raise Conflict('stale node observation')
            row.observed_eligible, row.observed_source = eligible, source
            row.observed_at, row.received_at = observed_at, timezone.now()
            row.save(update_fields=['observed_eligible', 'observed_source', 'observed_at', 'received_at'])
            return snapshot(row)

    def prepare_forget_node(self, actor, node_id, *, expected_revision):
        self._removal_inspection_boundary()
        row = m.Node.objects.get(pk=node_id)
        self._authorize(actor, 'catalog.delete_node', nodes=(row.id,), projects=self._node_scope(row.id))
        self._live(row)
        expected(row, expected_revision)
        if row.deployment_member:
            raise UnsafeRemoval('deployment membership would recreate this node')
        proof = self._guard().inspect_node(snapshot(row))
        validate_proof(proof, NodeRemovalProof, row.id, expected_revision)
        return PreparedNodeRetirement(row.id, expected_revision, proof)

    def commit_forget_node(self, actor, prepared):
        if type(prepared) is not PreparedNodeRetirement:
            raise UnsafeRemoval('prepared node retirement is required')
        with transaction.atomic():
            row = m.Node.objects.select_for_update().get(pk=prepared.target_id)
            self._authorize(actor, 'catalog.delete_node', nodes=(row.id,), projects=self._node_scope(row.id))
            self._live(row)
            expected(row, prepared.expected_revision)
            if row.deployment_member:
                raise UnsafeRemoval('deployment membership would recreate this node')
            validate_proof(prepared.proof, NodeRemovalProof, row.id, row.revision)
            receipt = self._guard().validate_node(prepared.proof, snapshot(row))
            validate_node_receipt(receipt, prepared.proof, timezone.now())
            return self._retire(row)

    def forget_node(self, actor, node_id, *, expected_revision):
        prepared = self.prepare_forget_node(actor, node_id, expected_revision=expected_revision)
        return self.commit_forget_node(actor, prepared)

    def _installation(self, *, lock=False):
        model = m.InstallationConfiguration._meta.get_field('installation').remote_field.model
        query = model.objects.select_for_update() if lock else model.objects
        return query.get(pk=1)

    def _settings_snapshot(self, owner, *, node):
        if node:
            values = m.NodeSetting.objects.filter(node=owner).order_by('key')
            revision, applied, receipt = owner.settings_revision, owner.settings_applied_revision, owner.settings_receipt
        else:
            values = m.InstallationSetting.objects.filter(installation=owner).order_by('key')
            state = m.InstallationConfiguration.objects.filter(installation=owner).first()
            revision, applied, receipt = (state.revision, state.applied_revision, state.applied_receipt) if state else (1, None, '')
        return SettingsSnapshot(revision, tuple((v.key, getattr(v, v.value_kind + '_value')) for v in values), applied, receipt)

    def get_installation_settings(self, actor):
        with transaction.atomic():
            owner = self._installation(lock=True)
            self._authorize(actor, 'catalog.view_installationsetting')
            return self._settings_snapshot(owner, node=False)

    def get_node_settings(self, actor, node_id):
        with transaction.atomic():
            owner = m.Node.objects.select_for_update().get(pk=node_id, retired_at__isnull=True)
            self._authorize(actor, 'catalog.view_nodesetting', nodes=(node_id,), projects=self._node_scope(node_id))
            return self._settings_snapshot(owner, node=True)

    def get_effective_settings(self, actor, *, node_id=None, require=()):
        # Тот же installation lock, что у обоих writers: revision pair и rows
        # не смешиваются с параллельным edit. Никаких внешних I/O/fallback.
        with transaction.atomic():
            installation = self._installation(lock=True)
            self._authorize(actor, 'catalog.view_installationsetting')
            installed = self._settings_snapshot(installation, node=False)
            local = None
            if node_id is not None:
                node = m.Node.objects.select_for_update().get(pk=node_id, retired_at__isnull=True)
                self._authorize(actor, 'catalog.view_nodesetting', nodes=(node_id,), projects=self._node_scope(node_id))
                local = self._settings_snapshot(node, node=True)
            result = resolve(dict(installed.values), dict(local.values) if local else None, require=require)
            return EffectiveSettingsSnapshot(installed.revision, local.revision if local else None,
                result.values, result.sources, result.missing, installed.applied_revision,
                local.applied_revision if local else None)

    def _set_settings(self, actor, batch, revision, node_id):
        values = validate_batch(batch, node=node_id is not None)
        with transaction.atomic():
            # Один порядок lock: installation, затем node. Так глобальный edit
            # проверяет все существующие overrides без гонки с узловым edit.
            installation = self._installation(lock=True)
            installed = dict(self._settings_snapshot(installation, node=False).values)
            if node_id is None:
                owner = installation
                self._authorize(actor, 'catalog.change_installationsetting')
                state, _ = m.InstallationConfiguration.objects.get_or_create(installation=owner)
                field, model, reference = 'revision', m.InstallationSetting, {'installation': owner}
                proposed = {**installed, **values}
                resolve(proposed)
                by_node = {}
                for row in m.NodeSetting.objects.filter(node__retired_at__isnull=True).order_by('node_id', 'key'):
                    by_node.setdefault(row.node_id, {})[row.key] = getattr(row, row.value_kind + '_value')
                for overrides in by_node.values():
                    resolve(proposed, overrides)
            else:
                owner = self._locked_node(node_id)
                self._authorize(actor, 'catalog.change_nodesetting', nodes=(node_id,), projects=self._node_scope(node_id))
                state, field, model, reference = owner, 'settings_revision', m.NodeSetting, {'node': owner}
                local = dict(self._settings_snapshot(owner, node=True).values)
                resolve(installed, {**local, **values})
            expected(state, revision, field)
            for key, value in values.items():
                definition = DEFINITIONS[key]
                defaults = {kind + '_value': value if definition.kind == kind else None for kind in ('text', 'int', 'bool')}
                defaults.update(value_kind=definition.kind, revision=revision+1)
                model.objects.update_or_create(**reference, key=key, defaults=defaults)
            setattr(state, field, revision+1)
            state.save(update_fields=[field])
            return self._settings_snapshot(owner, node=node_id is not None)

    def set_installation_settings(self, actor, batch, *, expected_revision):
        return self._set_settings(actor, batch, expected_revision, None)

    def set_node_settings(self, actor, node_id, batch, *, expected_revision):
        return self._set_settings(actor, batch, expected_revision, node_id)

    def record_settings_application(self, actor, *, target_revision, receipt, node_id=None):
        text(receipt, 'application receipt', 512)
        with transaction.atomic():
            if node_id is None:
                owner = self._installation(lock=True)
                self._authorize(actor, 'catalog.record_settings_application')
                row, _ = m.InstallationConfiguration.objects.get_or_create(installation=owner)
                current, prefix = row.revision, ''
            else:
                row = self._locked_node(node_id)
                self._authorize(actor, 'catalog.record_settings_application', nodes=(row.id,), projects=self._node_scope(row.id))
                current, prefix = row.settings_revision, 'settings_'
            if type(target_revision) is not int or target_revision != current:
                return False
            names = [prefix + 'applied_revision', 'settings_receipt' if prefix else 'applied_receipt', prefix + 'applied_at']
            for name, value in zip(names, (target_revision, receipt, timezone.now())):
                setattr(row, name, value)
            row.save(update_fields=names)
            return True

    def pin_project_policy(self, actor, project_id, content, *, source_commit, source_ref, provenance,
                           cpu, memory_mb, disk_mb, expected_revision, confidential=False):
        if type(content) is not bytes or not content:
            raise ValueError('policy requires exact nonempty bytes')
        if not isinstance(source_commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', source_commit):
            raise ValueError('policy requires a source commit')
        text(source_ref, 'source ref')
        text(provenance, 'provenance')
        for value in (cpu, memory_mb, disk_mb):
            if type(value) is not int or not 1 <= value <= 2147483647:
                raise ValueError('policy resources must be positive integers')
        flag(confidential)
        digest = hashlib.sha256(content).hexdigest()
        reference = ''
        # Protection может обращаться к внешнему key provider: вне SQL/locks.
        if confidential:
            if connection.in_atomic_block:
                raise RuntimeError('policy protection requires execution outside SQL transactions')
            self.get_project(actor, project_id)
            self._authorize(actor, 'catalog.pin_project_policy', projects=(project_id,))
            if self._protection is None:
                raise ProtectionUnavailable('confidential policy protection is not bound')
            content, reference = self._protection.protect(content, project_id=project_id, digest=digest)
            if type(content) is not bytes or not content or not reference:
                raise ProtectionUnavailable('policy protector returned no protected bytes/reference')
        with transaction.atomic():
            project = self._locked_project(project_id)
            self._authorize(actor, 'catalog.pin_project_policy', projects=(project.id,))
            expected(project, expected_revision)
            last = m.ProjectPolicySnapshot.objects.filter(project=project).order_by('-sequence').first()
            row = m.ProjectPolicySnapshot.objects.create(project=project, sequence=last.sequence+1 if last else 1,
                content=content, digest=digest, source_commit=source_commit, source_ref=source_ref,
                provenance=provenance, confidential=confidential, protection_reference=reference,
                cpu=cpu, memory_mb=memory_mb, disk_mb=disk_mb)
            project.revision += 1
            project.save(update_fields=['revision'])
            result = snapshot(row)
        return self._reveal_policy(actor, result)

    def _reveal_policy(self, actor, result):
        if not result.confidential:
            return result
        if self._protection is None:
            raise ProtectionUnavailable('confidential policy protection is not bound')
        if connection.in_atomic_block:
            raise RuntimeError('policy protection requires execution outside SQL transactions')
        content = self._protection.reveal(result.content, project_id=result.project_id,
            digest=result.digest, reference=result.protection_reference)
        if type(content) is not bytes or hashlib.sha256(content).hexdigest() != result.digest:
            raise ProtectionUnavailable('protected policy digest mismatch')
        self._authorize(actor, 'catalog.view_projectpolicysnapshot', projects=(result.project_id,))
        return Snapshot(tuple((key, content if key == 'content' else value) for key, value in result.fields))

    def get_policy(self, actor, policy_id):
        row = m.ProjectPolicySnapshot.objects.get(pk=policy_id)
        self._authorize(actor, 'catalog.view_projectpolicysnapshot', projects=(row.project_id,))
        return self._reveal_policy(actor, snapshot(row))

    def create_image_revision(self, actor, project_id, policy_id, *, source):
        text(source, 'image source')
        with transaction.atomic():
            self._locked_project(project_id)
            policy = m.ProjectPolicySnapshot.objects.get(pk=policy_id)
            self._authorize(actor, 'catalog.add_imagerevision', projects=(project_id, policy.project_id))
            if policy.project_id != project_id:
                raise ValueError('policy belongs to another project')
            return snapshot(m.ImageRevision.objects.create(policy=policy, source=source))

    def _image(self, actor, node_id, project_id, image_id):
        image = m.ImageRevision.objects.select_related('policy').get(pk=image_id)
        self._authorize(actor, 'catalog.change_nodeimage', nodes=(node_id,), projects=(project_id, image.policy.project_id))
        if image.policy.project_id != project_id:
            raise ValueError('image belongs to another project')
        return image

    def set_image_intent(self, actor, node_id, project_id, image_id, *, expected_revision):
        with transaction.atomic():
            self._locked_project(project_id)
            self._locked_node(node_id)
            image = self._image(actor, node_id, project_id, image_id)
            row = m.NodeImage.objects.select_for_update().filter(node_id=node_id, project_id=project_id).first()
            if row is None:
                if type(expected_revision) is not int or expected_revision != 0:
                    raise Conflict('node image does not exist')
                row = m.NodeImage(node_id=node_id, project_id=project_id, desired_image=image)
            else:
                expected(row, expected_revision)
                row.desired_image = image
                row.revision += 1
            row.save()
            return snapshot(row)

    def observe_image(self, actor, node_id, project_id, image_id, *, available, source, observed_at, receipt, expected_revision):
        flag(available)
        observation(source, observed_at, receipt)
        with transaction.atomic():
            self._locked_project(project_id)
            self._locked_node(node_id)
            image = self._image(actor, node_id, project_id, image_id)
            row = m.NodeImage.objects.select_for_update().get(node_id=node_id, project_id=project_id)
            expected(row, expected_revision)
            if row.observed_at is not None and observed_at <= row.observed_at:
                raise Conflict('stale image observation')
            row.observed_image, row.available, row.source = image, available, source
            row.observed_at, row.received_at, row.receipt = observed_at, timezone.now(), receipt
            row.revision += 1
            row.save()
            return snapshot(row)

    def register_client(self, actor, *, label):
        text(label, 'client label', 128)
        with transaction.atomic():
            installation = self._installation()
            self._authorize(actor, 'catalog.add_clientregistration')
            return snapshot(m.ClientRegistration.objects.create(installation=installation, label=label))

    def bind_workspace(self, actor, client_id, workspace_key, project_id, *, integration_branch, expected_revision):
        text(workspace_key, 'workspace key')
        text(integration_branch, 'integration branch')
        if (integration_branch.startswith(('-', '/')) or '..' in integration_branch
                or any(c.isspace() or c in '~^:?*[\\' for c in integration_branch)):
            raise ValueError('invalid integration branch')
        with transaction.atomic():
            self._locked_project(project_id)
            client = m.ClientRegistration.objects.select_for_update().get(pk=client_id)
            row = m.WorkspaceBinding.objects.filter(client=client, workspace_key=workspace_key).first()
            self._authorize(actor, 'catalog.change_workspacebinding', clients=(client.id,),
                projects=(project_id, row.project_id) if row else (project_id,))
            if row is None:
                if type(expected_revision) is not int or expected_revision != 0:
                    raise Conflict('workspace binding does not exist')
                row = m.WorkspaceBinding(client=client, workspace_key=workspace_key, project_id=project_id,
                                         integration_branch=integration_branch)
            else:
                expected(row, expected_revision)
                row.project_id, row.integration_branch = project_id, integration_branch
                row.revision += 1
            row.save()
            return snapshot(row)

    def resolve_workspace(self, actor, client_id, workspace_key):
        row = m.WorkspaceBinding.objects.get(client_id=client_id, workspace_key=workspace_key,
                                             project__retired_at__isnull=True)
        self._authorize(actor, 'catalog.view_workspacebinding', clients=(row.client_id,), projects=(row.project_id,))
        return snapshot(row)
