"""mop server pve-facts --base N [--project P --listing TEXT]: the pve driver's facts as JSON

One hypervisor's facts from its VMID base (the host's inventory line):
the bodies' network, gateway and prefix, the routes, the wrapper path,
the body key and known_hosts, the top of its VMID range. With --project
also its image and build body, and the project's bodies standing there;
--listing is the wrapper's `list` output on that node.

Controller-side facts for the image build and the hypervisor role.
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
