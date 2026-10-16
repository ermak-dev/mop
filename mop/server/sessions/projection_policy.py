"""Чистая нормализация фактов. Байты истории/PTY сюда не проходят."""
from datetime import datetime, timedelta
import re
from mop.common import state

KINDS = ('state', 'clone', 'size')
ERRORS = ('source_unavailable', 'source_timeout', 'source_incomplete')


def safe_payload(kind, payload, *, status='fresh'):
    if kind not in KINDS or type(payload) is not dict:
        raise ValueError('named source and object payload required')
    fields = {'state': {'session'}, 'clone': {'dirty', 'ahead', 'branch', 'default_branch'},
              'size': {'disk_bytes'}}[kind]
    if set(payload) - fields:
        raise ValueError('unknown source fields')
    if status == 'unavailable':
        if payload:
            raise ValueError('unavailable source cannot supply facts')
        return {}
    required = {'state': {'session'}, 'clone': {'dirty', 'ahead'}, 'size': {'disk_bytes'}}[kind]
    if not required <= payload.keys():
        raise ValueError('incomplete source payload')
    for key, value in payload.items():
        if key in ('dirty', 'ahead', 'disk_bytes'):
            if type(value) is not int or not 0 <= value < 2**63:
                raise ValueError('bounded source counter required')
        elif key == 'session':
            if type(value) is not str or re.fullmatch(r'(idle|busy|shell|requires_action|waiting|offline) [01] [01]', value) is None:
                raise ValueError('explicit session status and liveness required')
        elif value is not None and (type(value) is not str or len(value) > 256 or
                                    any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError('bounded source label required')
    return dict(payload)


def project_state(session, clone, freshness):
    if freshness == 'unavailable':
        verdict = state.State('silent', 'source unavailable')
    elif freshness != 'fresh' or not session:
        verdict = state.State('unknown', 'no current session observation')
    else:
        facts = {'present': True, 'session': session['session']}
        if clone:
            facts['clone'] = {'cur': clone.get('branch'), 'def': clone.get('default_branch'),
                              'dirty': clone['dirty'], 'ahead': clone['ahead']}
        verdict = state.verdict(facts)
    return {'kind': verdict.kind, 'detail': verdict.detail, 'branch': verdict.branch}


def observation(value, *, kind, incarnation, producer_epoch, run_id, accepting, now, ttl):
    if value is None:
        return {'value': None, 'freshness': 'unavailable', 'reason': 'not_observed',
                'sequence': None, 'producer_epoch': None, 'incarnation': None,
                'observed_at': None, 'received_at': None, 'expires_at': None}
    stamp = datetime.fromisoformat(value['observed_at'])
    expires = stamp + timedelta(seconds=ttl)
    freshness, reason = value['status'], value['reason'] if value['reason'] in ERRORS else None
    try:
        payload = safe_payload(kind, value['payload'], status=value['status'])
    except ValueError:
        payload = None
        freshness, reason = 'unavailable', 'source_incomplete'
    if (value['incarnation'] != incarnation or value['producer_epoch'] != producer_epoch or not accepting):
        freshness, reason = 'stale', 'incarnation_changed'
    elif run_id is None or value['controller_run'] != run_id:
        freshness, reason = 'stale', 'previous_controller_run'
    elif now > expires:
        freshness, reason = 'stale', 'expired'
    return {'value': payload if value['status'] != 'unavailable' else None,
            'freshness': freshness, 'reason': reason, 'sequence': value['sequence'],
            'producer_epoch': value['producer_epoch'], 'incarnation': value['incarnation'],
            'observed_at': value['observed_at'], 'received_at': value['received_at'],
            'expires_at': expires.isoformat()}
