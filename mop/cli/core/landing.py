"""landing token of this project: mop landing [take <puppet>|give|show] [--force]

  mop landing                   who holds the token, if anyone
  mop landing take pu-mop-3     grant the token to pu-mop-3 before it lands
  mop landing give              take it back on the puppet's report
  mop landing take pu-mop-3 --force
                                take it from a gone master; says whose it was
  mop landing give --force      release a gone master's token

Exactly one puppet between merge and push per project: two parallel gates
race, and the second push bounces non-fast-forward after its gate ran against
an integration that no longer exists. The token is kept by the cluster
service, so every master of the project sees the same queue.

take refuses while another master holds the token, naming who, for which
puppet and since when; asking again for the same puppet is not an error. The
holder is your login on the bus. There is no expiry: a holder that went away
is released with --force.
"""
from mop.cli import lib
from mop import bus, landing, puppets


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"args": [
    {"name": "action", "type": "string", "help": "take, give or show; show by default"},
    {"name": "puppet", "type": "string", "help": "for take: the puppet that lands, pu-<project>-<n>"},
    {"name": "force", "type": "boolean", "flag": "--force",
     "help": "take or release a token another master holds"}]}


def _held(token):
    return f"{token['holder']} for {token['puppet']} since {token['since']}"


def main(argv):
    force = "--force" in argv
    words = [a for a in argv if a != "--force"]
    action = words[0] if words else "show"
    if (action == "take" and len(words) != 2) or \
            (action in ("give", "show") and len(words) > 1) or \
            action not in ("take", "give", "show") or \
            any(w.startswith("-") for w in words):
        lib.usage(__doc__)
    project = puppets.project_of(lib.origin(None, __doc__))
    if action == "show":
        token = bus.call_cluster("landing", project=project, action="show").get("token")
        print(f"{project}: held by {_held(token)}" if token else f"{project}: free")
        return 0
    who = landing.holder()
    if not who:
        raise bus.Refused("the landing token is taken by a person's login on the bus, "
                          "and this process has none: run it from a master shell")
    fields = {"puppet": words[1]} if action == "take" else {}
    got = bus.call_cluster("landing", project=project, action=action, holder=who,
                           force=force, **fields)
    prev = got.get("previous")
    # Молча -- обычный исход; забранный у другого токен называется всегда:
    # тот мастер, вернувшись, будет считать его своим.
    if force and prev and prev.get("holder") != who:
        verb = "took" if action == "take" else "released"
        print(f"{verb} the landing token of {project} from {_held(prev)}")
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
