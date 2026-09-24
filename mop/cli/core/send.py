"""send a message to a pool puppet or a session on this machine

  mop send <target> <text>            send
  mop send <target> -                 text from stdin
  mop send <target> <text> --wait[=N] send and wait until the recipient goes free
  mop send --list                     live sessions on this machine

<target> — puppet name (pu-<project>-1), session name, pid, directory, or socket
path. A puppet is recognized by the pu- prefix and reached over the bus; anything
else is a local session, reached directly through its uds inbox.

  --priority now|next|later   where in the recipient's queue (default next)
  --mode bypass|prompting     which permission mode to present as (bypass)
  --quiet                     silent, exit code only
  --force                     take over a puppet another master leads (#161)

Channel protocol — CHANNEL.md, bus subjects — BUS.md.
"""
import argparse
import sys

from mop.cli import lib
from mop import bus, channel, session, puppets
from mop.render import table


def show_sessions():
    rows = [(d.get("name") or "-", str(d.get("pid")),
             "alive" if session.socket_alive(d["messagingSocketPath"]) else "dead",
             d.get("status") or "-", d.get("cwd") or "-")
            for d in session.sessions()]
    print("\n".join(table(rows)) if rows else "no live sessions")


def to_puppet(a, body):
    """Через шину: сокет папета host-local, до него дотягивается агент узла.
    Доставка -- mop/channel.py (#148), здесь только вывод."""
    v = channel.send_to_puppet(puppets.running_alloc(a.target)["NodeName"], a.target,
                               body, a.priority, a.wait, owner=bus.login(), force=a.force)
    if channel.failure(v):
        sys.exit(channel.failure(v))
    if a.quiet:
        return 0 if (a.wait is None or v["idle"]) else 2
    print(f"-> {a.target} msg_id={v['msg_id']}"
          + (f"; {v['owner_note']}" if v["owner_note"] else ""))
    if a.wait is None:
        return 0
    if not v["idle"]:
        print(f"waited {v['wait']}s — puppet never reported going free")
        return 2
    print(f"<- {v['idle']}")
    return 0


def to_session(a, body):
    v = channel.send_local(session.find(a.target), body, a.priority, a.wait,
                           mode=a.mode, from_name="mop")
    if channel.failure(v):
        sys.exit(channel.failure(v))
    if a.quiet:
        return 0 if (a.wait is None or v["idle"]) else 2
    print(f"-> {v['to']} [{v['pid']}] msg_id={v['msg_id']}")
    if a.wait is None:
        return 0
    if v["idle"] is None:
        print(f"waited {a.wait}s — session never reported going idle")
        return 2
    detail = v["idle"].get("detail")
    print(f"<- {v['idle'].get('state', '?')}" + (f": {detail}" if detail else ""))
    return 0


def main(argv):
    if argv and argv[0] in ("--list", "list"):
        return show_sessions()
    if len(argv) < 2:
        lib.usage(__doc__)
    p = argparse.ArgumentParser(add_help=False, usage=__doc__)
    p.add_argument("target")
    p.add_argument("message")
    p.add_argument("--priority", choices=session.PRIORITIES, default="next")
    p.add_argument("--mode", choices=session.MODES, default="bypass")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--wait", nargs="?", type=int, const=channel.MAX_WAIT, default=None)
    a = p.parse_args(argv)

    body = sys.stdin.read().rstrip("\n") if a.message == "-" else a.message
    if not body.strip():
        sys.exit("empty message — nothing to send")
    if not channel.is_puppet(a.target):
        return to_session(a, body)
    lib.guard(a.target)
    return to_puppet(a, body)



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
