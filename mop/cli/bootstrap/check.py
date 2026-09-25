"""mop bootstrap check: is the service answering on the bus, and for which puppets it holds a workspace
"""
import sys

from mop.common import bus

def main(_argv):
    got = bus.ask_server("ping", timeout=5)
    if got.get("error"):
        sys.exit(f"bootstrap service: {got['error']}")
    print(f"bootstrap service answers; workspace for: {', '.join(got.get('puppets') or []) or 'no puppet'}")
    return 0
