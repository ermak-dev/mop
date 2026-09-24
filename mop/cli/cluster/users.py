"""mop cluster users [--reload]: write the bus users file from the server's registry

On the server, as the pool user. Reads the people, services and nodes that
mop deploy put into /etc/nats/base-users.json, the projects from the
server's registry (~/.config/mop/projects), gives a project without a bus
password a new one, and writes /etc/nats/users.conf and, for the auth
callout (MOP_AUTH_CALLOUT, #206), /etc/nats/callout.conf with its keys.
--reload sends nats-server SIGHUP when users.conf changed and checks that
nats took it; a changed callout.conf needs a restart instead, and --reload
refuses rather than fire a reload nats would reject.
"""
import sys

from mop.cli import lib
from mop import bootstrap, natsconf, projects, puppets


def main(argv):
    if [a for a in argv if a != "--reload"]:
        lib.usage(__doc__)
    names = projects.names(*puppets.project_ids(projects.read()))
    try:
        changed, _ = natsconf.apply(names, bootstrap.PUPPET_CREDS)
    except (OSError, ValueError) as e:
        sys.exit(f"no base users ({e}) -- mop deploy writes {natsconf.BASE}")
    try:
        restart = natsconf.apply_callout()
    except ImportError as e:
        sys.exit(f"MOP_AUTH_CALLOUT=on needs {e.name}: pip install nkeys pynacl (MOP_PIP_DEPS)")
    except (OSError, ValueError) as e:
        sys.exit(f"callout file: {e}")
    # На успехе молчит (#182): «changed/unchanged» -- отчёт о сделанном.
    # Рестарт -- у deploy (контрольная сумма callout.conf): учётка пула
    # nats не перезапускает.
    if restart and "--reload" in argv:
        sys.exit(f"{natsconf.CALLOUT} changed: nats takes it only by a restart "
                 f"(systemctl restart {natsconf.UNIT}, or mop deploy)")
    if changed and "--reload" in argv:
        try:
            natsconf.reload()
        except RuntimeError as e:
            sys.exit(f"nats reload refused: {e}")
    return 0
