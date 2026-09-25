"""the installation's server: commands run on the controller, not on a client

  mop server deploy      roll out the pool: Nomad, the bus, the agent, the disk watchdog
  mop server config      the installation's settings and where each one comes from
  mop server setup       this controller's own dependencies: ansible, rsync, git, libraries
  mop server user        people of the operators file: add, passwd, delete, import
  mop server cluster     the cluster service: Nomad behind the bus
  mop server bootstrap   what the server plays at every start of a sandbox
  mop server web         the pool's dashboard in a browser (unit mop-web)
  mop server callout     the bus's auth callout (unit mop-callout)
  mop server pve-facts   a Proxmox node's image and build facts, for the build playbook

These need what lives only on the server: the inventory, the playbooks,
secrets/ and the Nomad token. The old one-word names (mop server deploy, mop server user,
mop server cluster, ...) still work for one release.
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
