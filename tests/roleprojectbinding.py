"""Проект роли берётся из origin, не административного маршрута (#654).

HYPOTHESIS: add/delete через admin путают область оператора с проектом гранта.
SOLUTION: запись и отзыв используют фактический проект задания.
"""
import hermetic
from _lib import Checks,FakeNomad,soul_env,soul_answer
from mop.common import bus
from mop.server import grants,cluster

ORIGIN='git@h:g/alpha.git'


def main():
    c=Checks()
    api=FakeNomad()
    with soul_env(api) as root:
        got=soul_answer(api,bus.ADMIN,{'verb':'add','origin':ORIGIN,
            'soul':'master','request_id':'admin-role-fixture','workspace':'','_caller':'anton'})
        c.check('#654 an operator can create a role for an origin project',got.get('ok') is True,got)
        if not got.get('ok'):
            return c.report('roleprojectbinding')
        name=got['name']
        actual=grants.by_job('alpha',name,soul_inc=got['soul_inc'],root=root)
        c.check('#654 admin creation binds the role to alpha rather than admin',
                actual is not None,actual)
        replay=soul_answer(api,bus.ADMIN,{'verb':'add','origin':ORIGIN,
            'soul':'master','request_id':'admin-role-fixture','workspace':'','_caller':'anton'})
        c.check('#654 operator replay returns the same job and incarnation',
                replay.get('name')==name and replay.get('soul_inc')==got['soul_inc'],replay)
        if actual:
            result=soul_answer(api,bus.ADMIN,{'verb':'delete','name':name,
                                'purge':True,'_caller':'anton'})
            after=grants.binding(actual['principal'],root=root)
            c.check('#654 operator deletion revokes the origin project binding',
                    result.get('ok') is True and after.get('active') is False,(result,after))
        alpha=soul_answer(api,'alpha',{'verb':'add','origin':ORIGIN,
            'soul':'puppet','request_id':'scoped-delete-fixture','workspace':'','_caller':'anton'})
        c.check('#654 project-scoped setup creates an active role',alpha.get('ok') is True,alpha)
        if alpha.get('ok'):
            scoped=grants.by_job('alpha',alpha['name'],root=root)
            result=soul_answer(api,bus.ADMIN,{'verb':'delete','name':alpha['name'],
                                'purge':True,'_caller':'anton'})
            after=grants.binding(scoped['principal'],root=root)
            c.check('#654 admin deletion revokes a project-scoped role',
                    result.get('ok') is True and after.get('active') is False,(result,after))
    return c.report('roleprojectbinding')


if __name__=='__main__':
    raise SystemExit(main())
