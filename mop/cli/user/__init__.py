"""people of the operators file: mop user add|passwd|delete|import — on the server

  mop user add <login> --role admin|user [--projects a,b|*] [--name "..."] [--email ...] [--stdin]
  mop user passwd <login> [--stdin]
  mop user delete <login>
  mop user import      transitional: move MOP_OPERATORS into the file, passwords kept

People live in the identity provider (#205): with MOP_AUTH_PROVIDER=file, in
the operators file on the server (~/.config/mop/secrets/operators, or
MOP_OPERATORS_FILE), one line per person, the password as a scrypt hash.
With file,ldap (a chain, #232) the file holds local people on top of LDAP and
mop user edits it; with ldap alone they live in LDAP, and mop user refuses.

Works on the server, where the file lives. The password is asked twice
without echo, or read as one line from stdin with --stdin. The server's
services (callout, the identity verb) read a copy in /etc/nats/identity: mop
user refreshes it when it can, otherwise it says to run mop deploy.
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
