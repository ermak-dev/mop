"""Настоящий service entrypoint слушает канал ролей (#651).

HYPOTHESIS: direct-answer проверки скрыли отсутствие подписчика role.
SOLUTION: маска из общей таблицы busnames среди subscriptions реального serve.
"""
import asyncio
import hermetic
from _lib import Checks, patched
from mop.common import busnames
from mop.server import cluster


def main():
    c=Checks()
    subjects=[]
    async def capture(name,subj,*args):
        subjects.extend(subj if isinstance(subj,list) else [subj])
    with patched(cluster.proxykey,start=lambda log:None), \
         patched(cluster.service,serve=capture):
        asyncio.run(cluster.serve(lambda line:None))
    c.check('#651 actual cluster serve subscribes to the role family',
            busnames.cluster_role(busnames.ANY) in subjects,subjects)
    c.check('#651 human bare and qualified RPC remain subscribed',
            busnames.cluster(busnames.ANY) in subjects
            and busnames.cluster(busnames.ANY,login=busnames.ANY) in subjects,subjects)
    return c.report('rolesubscription')


if __name__=='__main__':
    raise SystemExit(main())
