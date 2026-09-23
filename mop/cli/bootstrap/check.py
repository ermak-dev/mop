"""mop bootstrap check: is the service answering on the bus, and for which projects it holds a file
"""
import sys

from mop import bus

def main(_argv):
    got = bus.ask_server("ping", timeout=5)
    if got.get("error"):
        sys.exit(f"bootstrap service: {got['error']}")
    print(f"bootstrap service answers; files for: {', '.join(got.get('projects') or []) or 'no project'}")
    return 0
