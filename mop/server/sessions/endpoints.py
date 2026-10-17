"""Короткие sync use cases приватной доставки. Socket I/O принадлежит gateway.

Registration — identity/TTL, connection — только lease подписчика. take переводит
сообщение в attempting до I/O; ни timeout, ни смена gateway не повторяют его.
Authority/cipher — узкие root ports, не скрытые обратные ORM-зависимости.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import transaction
from django.utils import timezone

from mop.common.endpoint import (TrustedEndpointContext, EndpointPresence, DeliveryFrame,
                                 DeliveryResult, SealedPayload)
from mop.server.access.context import ActorContext, ActorKind
from mop.server.access.services import Access, Denied, NotVisible, current_actor
from mop.server.catalog.models import ClientRegistration
from . import endpoint_models as m


class Conflict(ValueError):
    """Изменившийся ключ/receiver не разрешает повтор эффекта."""


class Capacity(ValueError):
    """Общий durable admission исчерпан."""


class Unavailable(ValueError):
    """Нужный adapter/authority не связан или недоступен."""


class Unbound:
    def __call__(self, *, lock=False):
        raise Unavailable('endpoint authority is not bound')

    def seal(self, *args):
        raise Unavailable('private delivery cipher is not bound')

    def open(self, *args):
        raise Unavailable('private delivery cipher is not bound')


@dataclass(frozen=True)
class EndpointAuthority:
    epoch: UUID
    installation_id: int
    effects_enabled: bool


def uuid_value(value):
    if type(value) is not UUID:
        raise ValueError('canonical UUID required')
    return value


def bounded_text(value, maximum, name):
    if type(value) is not str or not value.strip() or len(value.encode('utf-8')) > maximum or '\x00' in value:
        raise ValueError('invalid '+name)
    return value


class Endpoints:
    """Владелец identity/leases/receipts; не владелец socket или Operation."""
    def __init__(self, *, authority=None, cipher=None, grants=None):
        self.authority = authority or Unbound()
        self.cipher = cipher or Unbound()
        self.grants = grants
        self.access = Access(grants=grants)

    @contextmanager
    def _transaction(self):
        if transaction.get_connection().in_atomic_block:
            raise RuntimeError('endpoint command requires its own commit boundary')
        with transaction.atomic():
            # Root берёт restore/installation locks перед endpoint rows и shared cap.
            yield self.authority(lock=True)

    def _human(self, actor, action, project_id, client_id):
        if type(actor) is not ActorContext or actor.kind is not ActorKind.HUMAN:
            raise Denied('human endpoint requires a current human credential')
        current_actor(actor)
        self.access.authorize(actor, 'catalog.view_project', project_ids=(project_id,), client_ids=(client_id,))
        self._permission(actor, action)

    def _permission(self, actor, code):
        user = get_user_model().objects.get(pk=actor.user_id)
        if (not Permission.objects.filter(content_type__app_label='execution', codename=code).exists()
                or not user.has_perm('execution.'+code)):
            raise Denied('endpoint action permission required')

    def _sender(self, actor, project_id):
        if type(actor) is not ActorContext:
            raise Denied('trusted actor required')
        if actor.grant_id is not None:
            # Существующая конечная send capability; grant не приобретает human endpoint.
            self.access.authorize(actor, 'sessions.send_message', project_ids=(project_id,))
        else:
            if actor.kind is not ActorKind.HUMAN:
                raise Denied('service cannot send human reports')
            self.access.authorize(actor, 'catalog.view_project', project_ids=(project_id,))
            self._permission(actor, 'deliver_report')

    def _endpoint(self, endpoint_id, *, lock=False):
        rows = m.Endpoint.objects.select_related('session')
        if lock:
            rows = rows.select_for_update(of=('self',))
        endpoint = rows.filter(pk=uuid_value(endpoint_id)).first()
        if endpoint is None:
            raise NotVisible('resource not found')
        return endpoint

    @staticmethod
    def _presence(endpoint):
        return EndpointPresence(endpoint.pk, endpoint.session_id, 'private:'+str(endpoint.address_id),
            endpoint.project_id, endpoint.session.external_session_id, endpoint.session.display_name,
            endpoint.expires_at, 'expired' if endpoint.retired or endpoint.expires_at <= timezone.now() else 'registered')

    def register(self, actor, *, client_id, project_id, external_session_id, display_name):
        uuid_value(client_id)
        uuid_value(project_id)
        bounded_text(external_session_id, 256, 'external session identity')
        bounded_text(display_name, 128, 'display name')
        with self._transaction() as authority:
            self._human(actor, 'register_endpoint', project_id, client_id)
            client = ClientRegistration.objects.select_for_update().filter(pk=client_id,
                installation_id=authority.installation_id).first()
            if client is None:
                raise NotVisible('resource not found')
            session, _ = m.ClientSession.objects.get_or_create(owner_id=actor.user_id, client_id=client_id,
                external_session_id=external_session_id, defaults={'display_name': display_name})
            if session.display_name != display_name:
                session.display_name = display_name
                session.revision += 1
                session.save(update_fields=['display_name', 'revision'])
            endpoint, _ = m.Endpoint.objects.get_or_create(session=session, project_id=project_id,
                defaults={'expires_at': timezone.now()+timedelta(seconds=60)})
            # Registration не оживляет retired endpoint; нужен новый external session.
            if endpoint.retired:
                raise Conflict('endpoint retired; register the new external conversation')
            endpoint.expires_at = timezone.now()+timedelta(seconds=60)
            endpoint.revision += 1
            endpoint.save(update_fields=['expires_at', 'revision'])
            return self._presence(endpoint)

    def addresses(self, actor, project_id, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('presence limit must be within 1..500')
        if type(actor) is not ActorContext or actor.kind is not ActorKind.HUMAN:
            raise Denied('private presence requires a human credential')
        self.authority()
        self.access.authorize(actor, 'catalog.view_project', project_ids=(uuid_value(project_id),))
        self._permission(actor, 'view_delivery')
        rows = m.Endpoint.objects.select_related('session').filter(project_id=project_id,
            session__owner_id=actor.user_id).order_by('pk')
        result = []
        for endpoint in rows[:limit]:
            # Отозванный client ownership не оставляет видимую локальную сессию.
            self._human(actor, 'view_delivery', endpoint.project_id, endpoint.session.client_id)
            result.append(self._presence(endpoint))
        return tuple(result)

    def presence(self, actor, endpoint_id):
        self.authority()
        endpoint = self._endpoint(endpoint_id)
        if endpoint.session.owner_id != actor.user_id:
            raise NotVisible('resource not found')
        self._human(actor, 'view_delivery', endpoint.project_id, endpoint.session.client_id)
        return self._presence(endpoint)

    def connect(self, actor, endpoint_id, *, gateway_id):
        uuid_value(gateway_id)
        with self._transaction() as authority:
            endpoint = self._endpoint(endpoint_id, lock=True)
            self._human(actor, 'register_endpoint', endpoint.project_id, endpoint.session.client_id)
            if endpoint.session.owner_id != actor.user_id:
                raise NotVisible('resource not found')
            if endpoint.retired or endpoint.expires_at <= timezone.now():
                raise Conflict('endpoint expired or retired')
            row = m.ConnectionLease.objects.filter(endpoint=endpoint).first()
            if row and row.connected and row.epoch == authority.epoch and row.expires_at > timezone.now():
                raise Conflict('endpoint already has a current receiver')
            # Новый receiver никогда не забирает attempting payload старого.
            self._abandon(endpoint.pk)
            values = dict(epoch=authority.epoch, gateway_id=gateway_id, nonce=uuid4(),
                generation=(row.generation+1 if row else 1), credential_id=actor.credential_id,
                user_id=actor.user_id, connected=True,
                expires_at=timezone.now()+timedelta(seconds=15))
            if row:
                for name, value in values.items():
                    setattr(row, name, value)
                row.revision += 1
                row.save()
            else:
                row = m.ConnectionLease.objects.create(endpoint=endpoint, **values)
            return self._context(endpoint, row)

    @staticmethod
    def _context(endpoint, row):
        # Holder user id равен владельцу сессии (trusted_context проверяет);
        # отдельного principal id у connection больше нет (#684).
        return TrustedEndpointContext(endpoint.session.owner_id, row.credential_id,
            endpoint.session_id, endpoint.pk, row.gateway_id, row.generation, row.nonce)

    def _connection(self, context, authority):
        if type(context) is not TrustedEndpointContext:
            raise Denied('trusted endpoint context required')
        endpoint = self._endpoint(context.endpoint_id, lock=True)
        row = m.ConnectionLease.objects.filter(endpoint=endpoint).first()
        if (row is None or self._context(endpoint, row) != context or not row.connected
                or row.epoch != authority.epoch or row.expires_at <= timezone.now()
                or endpoint.retired or endpoint.expires_at <= timezone.now()):
            raise Denied('endpoint connection is no longer current')
        actor = ActorContext(context.user_id, ActorKind.HUMAN,
            context.credential_id, context.user_id, None)
        self._human(actor, 'register_endpoint', endpoint.project_id, endpoint.session.client_id)
        return endpoint, row

    def trusted_context(self, actor, endpoint_id, nonce):
        uuid_value(nonce)
        with self._transaction() as authority:
            endpoint = self._endpoint(endpoint_id, lock=True)
            row = m.ConnectionLease.objects.filter(endpoint=endpoint).first()
            if (row is None or row.nonce != nonce or row.credential_id != actor.credential_id
                    or row.user_id != actor.user_id or endpoint.session.owner_id != actor.user_id):
                raise Denied('connection does not belong to authenticated client')
            context = self._context(endpoint, row)
            self._connection(context, authority)
            return context

    def validate(self, context):
        with self._transaction() as authority:
            endpoint, _ = self._connection(context, authority)
            return self._presence(endpoint)

    def renew(self, context):
        with self._transaction() as authority:
            endpoint, row = self._connection(context, authority)
            row.expires_at = timezone.now()+timedelta(seconds=15)
            row.revision += 1
            row.save(update_fields=['expires_at', 'revision'])
            endpoint.expires_at = timezone.now()+timedelta(seconds=60)
            endpoint.save(update_fields=['expires_at'])
            return self._presence(endpoint)

    @staticmethod
    def _finish(row, state):
        row.state = state
        row.payload = row.payload_key_id = row.payload_nonce = None
        row.revision += 1
        row.save(update_fields=['state', 'payload', 'payload_key_id', 'payload_nonce', 'revision'])

    def _abandon(self, endpoint_id):
        for row in m.MessageDelivery.objects.select_for_update().filter(endpoint_id=endpoint_id, state='attempting'):
            self._finish(row, 'unknown')

    def disconnect(self, context):
        # Cleanup не требует ещё живого credential, но сравнивает точный fence.
        with self._transaction():
            endpoint = self._endpoint(context.endpoint_id, lock=True)
            row = m.ConnectionLease.objects.filter(endpoint=endpoint).first()
            if row is None or self._context(endpoint, row) != context:
                return
            row.connected = False
            row.save(update_fields=['connected'])
            self._abandon(endpoint.pk)

    def retire(self, context):
        with self._transaction() as authority:
            endpoint, row = self._connection(context, authority)
            endpoint.retired = True
            endpoint.save(update_fields=['retired'])
            row.connected = False
            row.save(update_fields=['connected'])
            self._abandon(endpoint.pk)
            for delivery in m.MessageDelivery.objects.select_for_update().filter(endpoint=endpoint, state='queued'):
                self._finish(delivery, 'unavailable')

    def _recipient(self, address):
        if type(address) is not str or not address.startswith('private:'):
            raise NotVisible('resource not found')
        try:
            value = UUID(address[8:])
        except ValueError:
            raise NotVisible('resource not found') from None
        if str(value) != address[8:]:
            raise NotVisible('resource not found')
        endpoint = m.Endpoint.objects.select_related('session').filter(address_id=value).first()
        if endpoint is None:
            raise NotVisible('resource not found')
        return endpoint

    def send(self, actor, *, expected_epoch, address, message_id, text, not_after, operation_request_id=None,
             notify_when_idle=False, priority='next', sender=None):
        if transaction.get_connection().in_atomic_block:
            raise RuntimeError('private payload encryption must be outside transaction')
        uuid_value(message_id)
        uuid_value(expected_epoch)
        if operation_request_id is not None:
            uuid_value(operation_request_id)
        bounded_text(text, 65536, 'private message')
        if priority not in ('next', 'now') or type(notify_when_idle) is not bool:
            raise ValueError('invalid delivery options')
        if not hasattr(not_after, 'utcoffset') or not_after.utcoffset() is None:
            raise ValueError('aware delivery deadline required')
        authority = self.authority()
        if authority.epoch != expected_epoch:
            raise Conflict('installation epoch changed; refresh before sending')
        endpoint = self._recipient(address)
        self._sender(actor, endpoint.project_id)
        fingerprint = hashlib.sha256(json.dumps([str(authority.epoch), address, text, priority,
            notify_when_idle, not_after.isoformat(), str(operation_request_id),
            str(sender.endpoint_id) if sender else None], ensure_ascii=False).encode()).hexdigest()
        old = m.MessageDelivery.objects.filter(sender_user_id=actor.user_id, message_id=message_id).first()
        if old:
            if old.fingerprint != fingerprint:
                raise Conflict('message identity has conflicting content or recipient')
            return self._result(old)
        if not authority.effects_enabled:
            raise Unavailable('private delivery effects are disabled')
        delivery_id = uuid4()
        # До шифрования уже выполнен auth; после потенциального I/O весь admission
        # проверяется заново. AAD закрепляет message, recipient и installation.
        sealed = self.cipher.seal(delivery_id, endpoint.pk, text.encode())
        with self._transaction() as current:
            if (current.epoch, current.installation_id) != (authority.epoch, authority.installation_id):
                raise Conflict('installation epoch changed')
            endpoint = self._endpoint(endpoint.pk, lock=True)
            self._sender(actor, endpoint.project_id)
            reply = None
            if sender is not None:
                source, _ = self._connection(sender, current)
                if (sender.user_id, sender.credential_id) != (
                        actor.user_id, actor.credential_id) or source.project_id != endpoint.project_id:
                    raise Denied('sender endpoint does not match authenticated conversation')
                reply = source.pk
            old = m.MessageDelivery.objects.filter(sender_user_id=actor.user_id, message_id=message_id).first()
            if old:
                if old.fingerprint != fingerprint:
                    raise Conflict('message identity has conflicting content or recipient')
                return self._result(old)
            if not current.effects_enabled:
                raise Unavailable('private delivery effects are disabled')
            if not timezone.now() < not_after <= timezone.now()+timedelta(hours=24):
                raise ValueError('delivery deadline must be within 24 hours')
            # Installation lock в root защищает общий cap между gateway processes.
            # Deadline — политика retention/admission, не доказательство доставки.
            for expired in m.MessageDelivery.objects.select_for_update().filter(
                    state__in=('queued', 'attempting'), not_after__lte=timezone.now()).order_by('pk'):
                self._finish(expired, 'unknown' if expired.state == 'attempting' else 'unavailable')
            pending = m.MessageDelivery.objects.filter(state__in=('queued', 'attempting'))
            if pending.count() >= 256 or pending.filter(endpoint=endpoint).count() >= 64:
                raise Capacity('private delivery admission capacity exhausted')
            lease = m.ConnectionLease.objects.filter(endpoint=endpoint, connected=True,
                epoch=current.epoch, expires_at__gt=timezone.now()).first()
            available = lease is not None and not endpoint.retired and endpoint.expires_at > timezone.now()
            row = m.MessageDelivery.objects.create(id=delivery_id, endpoint=endpoint, epoch=current.epoch,
                message_id=message_id, operation_request_id=operation_request_id,
                sender_user_id=actor.user_id,
                sender_credential_id=actor.credential_id, sender_kind=actor.kind.value,
                sender_owner_id=actor.human_owner_id, sender_node_id=actor.node_id, sender_grant_id=actor.grant_id,
                reply_endpoint_id=reply, fingerprint=fingerprint, priority=priority,
                notify_when_idle=notify_when_idle, not_after=not_after,
                state='queued' if available else 'unavailable', payload=sealed.data if available else None,
                payload_key_id=sealed.key_id if available else None, payload_nonce=sealed.nonce if available else None)
            return self._result(row)

    def _result(self, row):
        return DeliveryResult(row.pk, row.message_id, row.operation_request_id, row.state,
            m.DeliveryReceipt.objects.filter(delivery=row, kind='idle').exists())

    @staticmethod
    def _original_actor(row):
        return ActorContext(row.sender_user_id, ActorKind(row.sender_kind),
            row.sender_credential_id, row.sender_owner_id, row.sender_node_id, row.sender_grant_id)

    def _sending_authority(self, row):
        actor = self._original_actor(row)
        self._sender(actor, row.endpoint.project_id)
        if row.reply_endpoint_id is not None:
            source = self._endpoint(row.reply_endpoint_id)
            if source.session.owner_id != actor.user_id:
                raise Denied('sending conversation ownership changed')
            self._human(actor, 'deliver_report', source.project_id, source.session.client_id)

    def delivery(self, actor, delivery_id):
        self.authority()
        row = m.MessageDelivery.objects.select_related('endpoint__session').filter(pk=uuid_value(delivery_id)).first()
        if row is None:
            raise NotVisible('resource not found')
        if actor.user_id == row.sender_user_id:
            self._sender(actor, row.endpoint.project_id)
            self._permission(actor, 'view_delivery')
        elif actor.kind is ActorKind.HUMAN and actor.user_id == row.endpoint.session.owner_id:
            self._human(actor, 'view_delivery', row.endpoint.project_id, row.endpoint.session.client_id)
        else:
            raise NotVisible('resource not found')
        return self._result(row)

    def take(self, context):
        with self._transaction() as authority:
            endpoint, _ = self._connection(context, authority)
            if not authority.effects_enabled:
                raise Unavailable('private delivery effects are disabled')
            row = m.MessageDelivery.objects.select_for_update().filter(endpoint=endpoint, state='queued').order_by('created_at', 'pk').first()
            if row is None:
                return None
            if row.epoch != authority.epoch or row.not_after <= timezone.now():
                self._finish(row, 'unavailable')
                return None
            try:
                self._sending_authority(row)
            except Denied:
                self._finish(row, 'unavailable')
                return None
            row.state = 'attempting'
            row.connection_generation, row.connection_nonce = context.generation, context.nonce
            row.save(update_fields=['state', 'connection_generation', 'connection_nonce'])
            sealed = SealedPayload(bytes(row.payload), row.payload_key_id, bytes(row.payload_nonce))
            reply = self._presence(self._endpoint(row.reply_endpoint_id)).address if row.reply_endpoint_id else None
            frame_values = (row.pk, row.message_id, endpoint.session.external_session_id,
                row.priority, row.notify_when_idle, reply)
        # Provider может ждать внешний key service. Ни SQL lock, ни HTTP request
        # не удерживается; gateway после этого снова проверяет authority.
        text = self.cipher.open(row.pk, endpoint.pk, sealed).decode('utf-8')
        return DeliveryFrame(frame_values[0], frame_values[1], frame_values[2], text, *frame_values[3:])

    def before_dispatch(self, context, delivery_id):
        with self._transaction() as authority:
            endpoint, _ = self._connection(context, authority)
            if not authority.effects_enabled:
                raise Unavailable('private delivery effects are disabled')
            row = self._attempt(context, delivery_id)
            if row.state != 'attempting' or row.not_after <= timezone.now():
                raise Conflict('private delivery is not dispatchable')
            self._sending_authority(row)
            return True

    def _attempt(self, context, delivery_id):
        row = m.MessageDelivery.objects.select_for_update().filter(pk=uuid_value(delivery_id)).first()
        if row is None or row.endpoint_id != context.endpoint_id:
            raise NotVisible('resource not found')
        if (row.connection_generation, row.connection_nonce) != (context.generation, context.nonce):
            if row.connection_generation is None:
                raise Conflict('delivery has not been claimed by a receiver')
            raise Denied('delivery belongs to another receiver generation')
        return row

    def acknowledge(self, context, delivery_id, *, outcome):
        if outcome not in ('accepted', 'unknown', 'unavailable'):
            raise ValueError('invalid application acceptance outcome')
        with self._transaction() as authority:
            self._connection(context, authority)
            row = self._attempt(context, delivery_id)
            receipt = m.DeliveryReceipt.objects.filter(delivery=row, kind='delivery').first()
            if receipt:
                if receipt.outcome != outcome:
                    raise Conflict('delivery receipt conflicts with stored evidence')
            else:
                if row.state != 'attempting':
                    raise Conflict('delivery is no longer awaiting application evidence')
                m.DeliveryReceipt.objects.create(delivery=row, kind='delivery', outcome=outcome,
                    connection_generation=context.generation, connection_nonce=context.nonce)
                self._finish(row, outcome)
            # Физическое evidence уже получено: отзыв не откатывает его в
            # noneffect. Сохраняем receipt, но отказ выводим после commit, чтобы
            # caller не получил authority для следующего stage/idle subscription.
            denied = None
            try:
                self._sending_authority(row)
            except Denied as error:
                denied = error
            result = self._result(row)
        if denied is not None:
            raise denied
        return result

    def idle_message(self, context, delivery_id):
        with self._transaction() as authority:
            self._connection(context, authority)
            row = self._attempt(context, delivery_id)
            if not row.notify_when_idle or row.state != 'accepted':
                raise Conflict('delivery has no accepted idle subscription')
            self._sending_authority(row)
            return row.message_id

    def idle(self, context, delivery_id):
        with self._transaction() as authority:
            self._connection(context, authority)
            row = self._attempt(context, delivery_id)
            if not row.notify_when_idle or row.state != 'accepted':
                raise Conflict('idle is not evidence of accepted message delivery')
            self._sending_authority(row)
            m.DeliveryReceipt.objects.get_or_create(delivery=row, kind='idle', defaults=dict(outcome='observed',
                connection_generation=context.generation, connection_nonce=context.nonce))
            return self._result(row)
