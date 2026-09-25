"""mop server cluster check: is the service answering on the bus, and does it see Nomad
"""
import sys

from mop import bus


def main(_argv):
    got = bus.ask_cluster("ping", timeout=5)
    if got.get("error") and not got.get("ok"):
        sys.exit(f"cluster service: {got['error']}")
    where = "answers" if got.get("reachable") else "answers, but Nomad does not"
    print(f"cluster service {where} as {got.get('project')}: "
          f"{got.get('nomad')}, {got.get('nodes')} node(s)"
          + (f" — {got['error']}" if got.get("error") else ""))
    return 0 if got.get("reachable") else 1
