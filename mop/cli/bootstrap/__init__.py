"""sandbox bootstrap: what the server plays at every start of a sandbox (#62)

  mop bootstrap serve            the server's subscriber (unit mop-bootstrap)
  mop bootstrap check            is the service answering on the bus, and for
                                 which puppets it holds a workspace

A puppet's workspace (.mop/bootstrap.yaml) is played by the server into its
sandbox at every start, before the session opens (docs/BOOTSTRAP.md). It is
the puppet's, not the project's (#133): mop add, mop update and mop recycle
send it from the working copy they run in, an absent file removes it, and
mop delete takes it away. The project's sandbox.yaml goes into its image
(mop project add).
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
