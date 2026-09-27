"""credentials of LLM providers: the server's registry

  mop cred list                    what the server holds: status, resets, usage
  mop cred status [name]           probe the providers now, then the same table
  mop cred add <name> --profile P --key-file F|-   a provider key (GLM) as a credential
  mop cred rm <name>               take a credential down with its secret
  mop cred login <name>            log a claude.ai account in on the server
                                   without a browser: prints the authorize
                                   url, reads the code from stdin
  mop cred login <name> --setup-token
                                   a one-year token instead of a session
                                   (inference scope only)

list, status, add and rm go over the bus to the cluster service and work
from any operator's machine; login drives the client in a pty and runs on
the server.

A credential is a named authorization of an LLM provider that the pool
hands to puppets. Each one lives in its own home on the
server, ~/.config/mop/creds/<name>/, and the login is done by the official
`claude` client itself, driven in a pty — no OAuth of our own.
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
