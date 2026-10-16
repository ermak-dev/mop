"""Новая закрытая broker boundary; human/grant не получают NATS principal."""
from uuid import UUID
from .events import subject


def broker_permissions(kind, installation, *, project_ids=(), node_id=None):
    denied={'deny':['>']}
    if kind in ('gateway','publisher'):
        if node_id is not None or not 1<=len(project_ids)<=16:
            raise ValueError('broker service requires a bounded configured project scope')
        subjects=[subject(installation,project) for project in project_ids]
        if len(set(subjects))!=len(subjects):
            raise ValueError('duplicate broker project scope')
        if kind=='gateway':
            return {'publish':denied,'subscribe':{'allow':subjects}}
        return {'publish':{'allow':subjects},'subscribe':denied}
    if kind=='node' and not project_ids:
        # subject() проверяет installation, node UUID не берётся из browser JSON.
        subject(installation,UUID(str(node_id)))
        prefix=f'mop.rt.v1.{installation}.node.{UUID(str(node_id))}'
        return {'publish':{'allow':[prefix+'.progress']},'subscribe':{'allow':[prefix+'.wake']}}
    raise ValueError('only internal gateway, publisher and enrolled node broker principals are supported')
