"""this controller's own dependencies: mop server setup

Sets up the machine that runs mop server deploy, and nothing for the pool:
the pool's nodes and bodies are ansible's business (mop server deploy).
Installs ansible with the ansible.posix collection, rsync, git, curl, pip
and the python libraries mop is written on. An operator's machine has its
own: mop setup.

Ansible does the work, as everywhere else in mop: this command only
installs ansible itself through apt when it is missing (Debian 13 ships
the full ansible package, collections included) and runs
deploy/self.yml against localhost. Running it again changes nothing.
"""
from mop.cli.pool import _self


def main(argv):
    return _self.main("controller", argv, __doc__)
