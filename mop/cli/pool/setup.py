"""this operator machine's own dependencies: mop setup

Sets up the machine you work with the pool from, and nothing for the pool:
ansible (to run this very setup), git, pip and the python libraries mop is
written on, and a word about claude if it is not in PATH. The server has
its own: mop server setup.

Ansible does the work, as everywhere else in mop: this command only
installs ansible itself through apt when it is missing (Debian 13 ships
the full ansible package, collections included) and runs
deploy/self.yml against localhost. Running it again changes nothing.
"""
from mop.cli.pool import _self


def main(argv):
    return _self.main("operator", argv, __doc__)
