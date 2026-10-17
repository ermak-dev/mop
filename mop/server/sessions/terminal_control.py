"""Короткие SQL use cases для private attach; внешних эффектов здесь нет."""
from datetime import timedelta
from uuid import UUID
from django.db import transaction
from django.utils import timezone
from mop.server.access.services import Access, Denied, NotVisible
from .models import Assignment
from .terminal_models import TerminalControl, TerminalAttachment


class Conflict(ValueError):
    """Control уже занят, заменён или требует нового явного открытия."""


class Capacity(ValueError):
    """Общий bounded reader limit исчерпан."""


def size(cols, rows):
    if type(cols) is not int or type(rows) is not int or not 20 <= cols <= 500 or not 5 <= rows <= 200:
        raise ValueError('terminal size must be within 20..500 by 5..200')


BINDING = ('id','target_id','incarnation_id','credential_id','user_id',
           'gateway_id','connection_id','generation','mode')


def _dto(row):
    result={name: str(getattr(row,name)) if name.endswith('_id') or name=='id'
            else getattr(row,name) for name in (*BINDING,'physical_instance_id','conversation_epoch',
                                                 'terminal_epoch','cols','rows','sequence')}
    result['executor_epoch']=str(row.incarnation.executor_epoch)
    return result


class Terminals:
    """Один control fence на target; state старого socket не оживает при restart."""
    def __init__(self, *, authorizer=None, retirement_authority=None):
        self.authorizer=authorizer or Access()
        self.retirement_authority=retirement_authority

    def _scope(self, actor, target, mode, *, current=True):
        # Вид Actor прав не даёт и не отнимает (#689): решает право и scope.
        # Писатель терминала по-прежнему привязан к браузерной сессии.
        self.authorizer.authorize(actor,'execution.control_terminal' if mode=='rw' else 'execution.read_terminal',
                                  project_ids=(target.project_id,))
        if current and (target.expires_at <= timezone.now() or target.current_incarnation_id is None):
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
            # Не связанный reserve ещё не мог дойти до resolver. Bound unknown
            # сохраняет fence даже после срока: expiry не доказывает retirement.
            TerminalAttachment.objects.filter(target=target,state='reserved',expires_at__lte=now).update(state='closed')
            TerminalAttachment.objects.filter(target=target,state='active',expires_at__lte=now).update(state='unknown')
            active=TerminalAttachment.objects.filter(target=target,state__in=('reserved','active','unknown'))
            if mode=='rw' and active.filter(mode='rw').exists():
                raise Conflict('terminal writer is already reserved')
            if mode=='ro' and active.filter(mode='ro').count() >= 8:
                raise Capacity('terminal reader limit reached')
            # Родительская блокировка сериализует open между ASGI workers.
            control.generation+=1
            control.save(update_fields=['generation'])
            row=TerminalAttachment.objects.create(target=target,incarnation_id=incarnation_id,
                credential_id=actor.credential_id,user_id=actor.user_id,mode=mode,
                generation=control.generation,cols=cols,rows=rows,expires_at=now+timedelta(seconds=30))
            return _dto(row)

    def _locked(self, attachment_id):
        target_id=TerminalAttachment.objects.filter(pk=attachment_id).values_list('target_id',flat=True).first()
        if target_id is None:
            raise NotVisible('resource not found')
        # Как в open: Assignment, затем attachment. Никаких inverse row locks.
        target=Assignment.objects.select_for_update().get(pk=target_id)
        row=TerminalAttachment.objects.select_for_update().get(pk=attachment_id)
        return target,row

    def _row(self, actor, attachment_id):
        target,row=self._locked(attachment_id)
        if row.credential_id != actor.credential_id or row.user_id != actor.user_id:
            raise Denied('terminal attachment belongs to another credential or browser')
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

    def pin_instance(self, actor, context, *, physical_instance_id, conversation_epoch, terminal_epoch):
        instance=UUID(physical_instance_id)
        if any(type(value) is not str or not 1<=len(value)<=128 for value in (conversation_epoch,terminal_epoch)):
            raise ValueError('complete physical terminal epochs required')
        with transaction.atomic():
            row=self._live(actor,context)
            if row.physical_instance_id is not None or row.sequence:
                raise Conflict('physical terminal instance is already pinned')
            row.physical_instance_id=instance
            row.conversation_epoch,row.terminal_epoch=conversation_epoch,terminal_epoch
            row.save(update_fields=['physical_instance_id','conversation_epoch','terminal_epoch'])
            return _dto(row)

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
        # Отмена/revocation разрешает остановить собственный attach, но не
        # объявить физический эффект завершённым. Нет доказательства — unknown.
        with transaction.atomic():
            _,row=self._locked(UUID(context['id']))
            if (row.credential_id!=actor.credential_id or row.user_id!=actor.user_id or
                    any(_dto(row)[key]!=context[key] for key in BINDING)):
                raise Denied('terminal cleanup binding changed')
            if row.state=='active':
                row.state='unknown'
                row.save(update_fields=['state'])

    def retire(self, context, proof):
        # Только composition-root capability. JSON/LeaseExpired/наличие ORM row
        # не являются свидетельством закрытия конкретного physical instance.
        if self.retirement_authority is None:
            raise Conflict('physical terminal retirement authority is not bound')
        self.retirement_authority.verify_retirement(context,proof)
        with transaction.atomic():
            _,row=self._locked(UUID(context['id']))
            actual=_dto(row)
            if (any(actual[key]!=context[key] for key in (*BINDING,'executor_epoch')) or
                    actual['physical_instance_id']!=proof.instance_id or
                    actual['conversation_epoch']!=proof.conversation_epoch or actual['terminal_epoch']!=proof.terminal_epoch):
                raise Conflict('terminal retirement binding or physical instance changed')
            row.state='closed'
            row.save(update_fields=['state'])

    def reconcile(self, actor, context, proof):
        with transaction.atomic():
            target,row=self._locked(UUID(context['id']))
            self._scope(actor,target,row.mode,current=False)
            if row.credential_id!=actor.credential_id or row.user_id!=actor.user_id:
                raise Denied('terminal reconciliation belongs to another credential or browser')
            self.retire(context,proof)

    def inspect(self, actor, attachment_id):
        # Public service seam для trusted inspector, не обход current authority.
        with transaction.atomic():
            target,row=self._locked(attachment_id)
            self._scope(actor,target,row.mode,current=False)
            if row.credential_id!=actor.credential_id or row.user_id!=actor.user_id:
                raise Denied('terminal attachment belongs to another credential or browser')
            return {**_dto(row),'state':row.state}

    def abandon(self, actor, attachment_id):
        with transaction.atomic():
            try:
                target,row=self._locked(attachment_id)
            except NotVisible:
                return
            self._scope(actor,target,row.mode,current=False)
            if row.credential_id!=actor.credential_id or row.user_id!=actor.user_id:
                raise Denied('terminal attachment belongs to another credential or browser')
            if row.state in ('reserved','active'):
                row.state='closed' if row.connection_id is None else 'unknown'
                row.save(update_fields=['state'])
