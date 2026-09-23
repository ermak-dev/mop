"""mop driver list: bodies standing on this node — no Nomad needed

The roster of this machine, asked from its driver: on a hypervisor the
containers, on a host node the live tmux servers. Runs on the node itself.
"""
import asyncio

from mop import driver


def main(_argv):
    names = asyncio.run(driver.current().bodies())
    print("\n".join(names) if names else "no bodies on this machine")
    return 0
