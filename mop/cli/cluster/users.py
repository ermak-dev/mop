"""mop cluster users [--reload]: write the bus users file from the server's registry

On the server, as the pool user. Reads the people, services and nodes that
mop deploy put into /etc/nats/base-users.json, the projects from the
server's registry (~/.config/mop/projects), gives a project without a bus
password a new one, and writes /etc/nats/users.conf. --reload sends
nats-server SIGHUP when the file changed and checks that nats took it.
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
    # На успехе молчит (#182): «changed/unchanged» -- отчёт о сделанном.
    if changed and "--reload" in argv:
        try:
            natsconf.reload()
        except RuntimeError as e:
            sys.exit(f"nats reload refused: {e}")
    return 0
