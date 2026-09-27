"""credentials of LLM providers: mop cred login <name> [--setup-token]

  mop cred login <name>            log a claude.ai account in on the server
                                   without a browser: prints the authorize
                                   url, reads the code from stdin
  mop cred login <name> --setup-token
                                   a one-year token instead of a session
                                   (inference scope only)

A credential is a named authorization of an LLM provider that the pool
hands to puppets. Each one lives in its own home on the
server, ~/.config/mop/creds/<name>/, and the login is done by the official
`claude` client itself, driven in a pty — no OAuth of our own.
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
