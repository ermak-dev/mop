"""node driver: in what a puppet lives

  mop driver                     drivers in the registry, and this node's one
  mop driver list                bodies standing on this node — no Nomad needed
  mop driver run <name>          raise the body and run the puppet inside it
  mop driver sweep [--dry]       destroy bodies with no puppet left in them
  mop driver build [shard|origin] [--fresh] [--force]
                                 bake the shard's image on the hypervisors;
                                 no argument: this working copy, .mop as it lies

`run` is what the job spec calls. The outer wrapper is driver-agnostic because
Nomad picks the node only after the spec is registered, so the master cannot
know what the body will be; everything about the body happens here, on the
node that owns it. Each verb is a module of this package (#77).
"""
from mop.cli import lib
from mop import driver
from mop.render import table


def main(argv):
    """Без глагола: реестр драйверов и тот, которым живёт эта машина."""
    if argv:
        lib.usage(__doc__)
    here = driver.current_name()
    rows = [("DRIVER", "", "WHAT A BODY IS")]
    for name, d in sorted(driver.drivers().items()):
        rows.append((name, "*" if name == here else "", d["doc"]))
    print("\n".join(table(rows)))
    print(f"\nthis machine: {here} — from MOP_DRIVER, which deploy fills from "
          f"the node's inventory group")
    return 0
