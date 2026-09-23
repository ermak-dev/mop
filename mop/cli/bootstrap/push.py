"""mop bootstrap push [origin]: send this working copy's .mop/bootstrap.yaml to the server

An absent file removes the shard's bootstrap there. The server holds one copy
per shard: mop deploy puts it from origin, mop add and this push from the
working copy — the master decides.
"""
import os
import sys

from mop.cli import lib
from mop import bootstrap, bus, puppets

def main(argv):
    if len(argv) > 1:
        lib.usage(__doc__)
    origin = argv[0] if argv else lib.cwd_origin()
    shard = puppets.shard_of(origin)
    root = lib.git("rev-parse", "--show-toplevel")
    path = os.path.join(root, bootstrap.FILE)
    if os.path.exists(path):
        with open(path) as f:
            text = f.read()
    else:
        text = ""
    got = bus.ask_server("put", shard=shard, text=text)
    if got.get("error"):
        sys.exit(f"{shard}: {got['error']}")
    if not text:
        print(f"{shard}: no {bootstrap.FILE} here — removed on the server")
        return 0
    print(f"{shard}: {bootstrap.FILE} on the server — {got.get('tasks', 0)} task(s), "
          f"{got.get('vars', 0)} var(s)")
    for k in got.get("alien") or []:
        print(f"  {k} is not a bootstrap's to set — ignored")
    return 0
