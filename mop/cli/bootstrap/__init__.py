"""sandbox bootstrap: what the server plays at every start of a sandbox (#62)

  mop bootstrap serve            the server's subscriber (unit mop-bootstrap)
  mop bootstrap push [origin]    send this working copy's .mop/bootstrap.yaml
                                 to the server; an absent file removes it there
  mop bootstrap check            is the service answering on the bus, and for
                                 which projects it holds a file

A project's .mop/bootstrap.yaml is played by the server into the sandbox at
every start of a puppet, before its session opens (docs/BOOTSTRAP.md). The
server holds one copy per project: mop deploy puts it from origin, mop add and
this push from the working copy.
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
