"""node agent: answers the bus about the puppets on this node (unit mop-agent)

  mop agent            subscribe and serve, until stopped
  mop agent --check    ask this node's agent over the bus whether it answers

One per node, under systemd, outside the job spec (docs/BUS.md). The unit
starts it as `mop agent`; units not yet rolled out still start
`python3 -m mop.agent`, which lands here too.

Runs on a node: nothing here may import what a node does not have
(python-nomad, the installation's .env) -- hence no mop.cli.lib.
"""
import asyncio
import json
import sys

NATS_MISSING = "bus library needed: pip install --user --break-system-packages nats-py"


async def serve(nats, agent, bus):
    c = bus.config(bus.NODE_FILE)
    node = agent.node_name()
    conn = await nats.connect(
        **bus.auth(c), name=f"mop-agent/{node}",
        allow_reconnect=True, max_reconnect_attempts=-1, reconnect_time_wait=2)
    subjects = await agent.attach(conn, node)
    print(f"mop-agent: node {node}, subscribed to {', '.join(subjects)}", flush=True)
    await asyncio.Event().wait()


async def check(nats, agent, bus):
    """Проверка прогоном, а не чтением конфига: юнит, упавший в бесконечный
    реконнект, systemd вполне устраивает, и «запущен» не значит «подписан».
    Спрашиваем через публичный субъект узла, а не через .rpc: туда узлу писать
    и не положено — это и есть та граница прав, ради которой шину заводили.
    Первый прогон проверки уткнулся ровно в неё, и был неправ он, а не права."""
    c = bus.config(bus.NODE_FILE)
    print(f"mop-agent: node {agent.node_name()}, bus {c['url']}, "
          f"{len(agent.VERBS)} verbs ({len(agent.PUBLIC_VERBS)} public)")
    nc = await nats.connect(**bus.auth(c), name="mop-agent/check",
                            allow_reconnect=False, connect_timeout=5)
    try:
        msg = await nc.request(bus.subject(agent.node_name(), "msg", project=bus.ADMIN),
                               json.dumps({"verb": "ping"}).encode(), timeout=5)
        print(f"subscribed: {msg.data.decode()}")
    finally:
        await nc.close()


def main(argv):
    # nats -- до пакета: без него агенту нечего делать, и отказ должен
    # называть лечение, а не падать трассой из импорта.
    try:
        import nats
    except ImportError:
        sys.exit(NATS_MISSING)
    from mop.node import agent
    from mop.common import bus
    if "--check" in argv:
        try:
            asyncio.run(check(nats, agent, bus))
        except Exception as e:
            print(f"agent is NOT answering on its subject: {e}", file=sys.stderr)
            return 1
        return 0
    try:
        asyncio.run(serve(nats, agent, bus))
    except KeyboardInterrupt:
        return 0
    return 0
