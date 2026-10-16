"""Короткие SQL use cases для private attach; внешних эффектов здесь нет."""
from datetime import timedelta
from uuid import UUID
from django.db import transaction
from django.utils import timezone
from mop.server.access.services import Access, Denied, NotVisible
from mop.server.access.context import PrincipalKind
from .models import Assignment
from .terminal_models import TerminalControl, TerminalAttachment


class Conflict(ValueError):
    """Control уже занят, заменён или требует нового явного открытия."""


class Capacity(ValueError):
    """Общий bounded reader limit исчерпан."""


def size(cols, rows):
    if type(cols) is not int or type(rows) is not int or not 20 <= cols <= 500 or not 5 <= rows <= 200:
        raise ValueError('terminal size must be within 20..500 by 5..200')


def _dto(row):
    return {name: str(getattr(row,name)) if name in ('id','target_id','incarnation_id','gateway_id','connection_id')
            else getattr(row,name) for name in ('id','target_id','incarnation_id','gateway_id','connection_id',
                                               'mode','generation','cols','rows','sequence')}


class Terminals:
    """Один control fence на target; state старого socket не оживает при restart."""
    def __init__(self, *, authorizer=None):
        self.authorizer=authorizer or Access()

    def _scope(self, actor, target, mode):
        if actor.kind is not PrincipalKind.HUMAN:
            raise Denied('terminal requires a human principal')
        self.authorizer.authorize(actor,'sessions.control_terminal' if mode=='rw' else 'sessions.read_terminal',
                                  project_ids=(target.project_id,))
        if target.expires_at <= timezone.now() or target.current_incarnation_id is None:
            raise Conflict('terminal target is not current')
        if mode=='rw' and target.owner_id not in (None,actor.user_id):
            raise Denied('terminal cannot control another human assignment')

    def open(self, actor, *, target_id, incarnation_id, mode, cols, rows):
        size(cols,rows)
        if mode not in ('rw','ro') or type(target_id) is not UUID or type(incarnation_id) is not UUID:
            raise ValueError('explicit terminal target, incarnation and mode required')
        with transaction.atomic():
            target=Assignment.objects.select_for_update().filter(pk=target_id).first()
            if target is None:
                raise NotVisible('resource not found')
            self._scope(actor,target,mode)
            if (target.current_incarnation_id != incarnation_id or
                    not target.current_incarnation.accepting or target.current_incarnation.node_id != target.node_id):
                raise Conflict('terminal incarnation changed')
            control,_=TerminalControl.objects.get_or_create(target=target)
            now=timezone.now()
            TerminalAttachment.objects.filter(target=target,state__in=('reserved','active'),expires_at__lte=now).update(state='unknown')
            active=TerminalAttachment.objects.filter(target=target,state__in=('reserved','active'))
            if mode=='rw' and active.filter(mode='rw').exists():
                raise Conflict('terminal writer is already reserved')
            if mode=='ro' and active.filter(mode='ro').count() >= 8:
                raise Capacity('terminal reader limit reached')
            # Родительская блокировка сериализует open между ASGI workers.
            control.generation+=1
            control.save(update_fields=['generation'])
            row=TerminalAttachment.objects.create(target=target,incarnation_id=incarnation_id,
                credential_id=actor.credential_id,principal_id=actor.principal_id,mode=mode,
                generation=control.generation,cols=cols,rows=rows,expires_at=now+timedelta(seconds=30))
            return _dto(row)

    def _row(self, actor, attachment_id):
        row=TerminalAttachment.objects.select_for_update().filter(pk=attachment_id).first()
        if row is None:
            raise NotVisible('resource not found')
        if row.credential_id != actor.credential_id or row.principal_id != actor.principal_id:
            raise Denied('terminal attachment belongs to another credential or browser')
        target=Assignment.objects.get(pk=row.target_id)
        self._scope(actor,target,row.mode)
        if (row.expires_at <= timezone.now() or target.current_incarnation_id != row.incarnation_id or
                not row.incarnation.accepting):
            raise Conflict('terminal attachment expired or incarnation changed')
        return row

    def bind(self, actor, connection_id, attachment_id, *, gateway_id):
        if any(type(x) is not UUID for x in (connection_id,attachment_id,gateway_id)):
            raise ValueError('complete live terminal connection binding required')
        with transaction.atomic():
            row=self._row(actor,attachment_id)
            if row.state!='reserved':
                raise Conflict('terminal requires a fresh REST attachment')
            row.connection_id,row.gateway_id,row.state=connection_id,gateway_id,'active'
            row.save(update_fields=['connection_id','gateway_id','state'])
            return _dto(row)

    def _live(self, actor, context):
        row=self._row(actor,UUID(context['id']))
        if (row.state!='active' or str(row.connection_id)!=context['connection_id'] or
                str(row.gateway_id)!=context['gateway_id'] or row.generation!=context['generation']):
            raise Conflict('terminal control connection changed')
        return row

    def validate(self, actor, context):
        with transaction.atomic():
            row=self._live(actor,context)
            row.expires_at=timezone.now()+timedelta(seconds=30)
            row.save(update_fields=['expires_at'])
            return _dto(row)

    def before_input(self, actor, context, seq):
        if type(seq) is not int or seq < 1:
            raise ValueError('terminal input sequence must be positive')
        with transaction.atomic():
            row=self._live(actor,context)
            if row.mode!='rw':
                raise Denied('read-only terminal cannot send input or resize')
            if row.sequence+1 != seq:
                raise Conflict('terminal input sequence cannot be replayed')
            # COMMIT до PTY: потерянный ACK остаётся unknown, не retry.
            row.sequence=seq
            row.expires_at=timezone.now()+timedelta(seconds=30)
            row.save(update_fields=['sequence','expires_at'])
            return _dto(row)

    def release(self, actor, context):
        # Cleanup не требует ещё действующего допуска; binding не позволяет
        # отозванному caller закрыть чужой новый attach.
        return TerminalAttachment.objects.filter(id=UUID(context['id']),credential_id=actor.credential_id,
            connection_id=UUID(context['connection_id']),gateway_id=UUID(context['gateway_id']),
            generation=context['generation'],state='active').update(state='closed')

    def abandon(self, actor, attachment_id):
        return TerminalAttachment.objects.filter(pk=attachment_id,credential_id=actor.credential_id,
            state__in=('reserved','active')).update(state='closed')
