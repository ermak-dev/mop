"""mop driver pve-facts --base N [--project P --listing TEXT]: what the playbooks know of the pve driver, as JSON

The facts of one hypervisor, from its VMID base (the host's line in the
inventory): the bodies' network, gateway and prefix, the routes to its
bodies, the wrapper path, the body key and known_hosts, the top of its VMID
range. With --project, also the project's image and build body (vmid, name,
address, whether it stands) and the project's bodies standing there;
--listing is the output of the wrapper's `list` on that node.

Run by deploy/pve-build.yml and the pve role on the controller; one
computation in mop/driver/pve.py instead of copies in the playbooks (#158).
"""
import argparse
import json

from mop.cli import lib
from mop import driver


def main(argv):
    p = argparse.ArgumentParser(add_help=False, usage=__doc__)
    p.error = lambda why: lib.usage(f"{why}\n\n{__doc__}")
    p.add_argument("--base", type=int, required=True)
    p.add_argument("--project")
    p.add_argument("--listing", default="")
    a = p.parse_args(argv)
    # Модуль драйвера, а не driver.current(): факты нужны на управляющей
    # машине, у которой свой драйвер -- или никакого.
    print(json.dumps(driver.module("pve").facts(a.base, a.project, a.listing)))
    return 0
