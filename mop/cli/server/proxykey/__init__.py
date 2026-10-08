"""client proxy key rotation: mop server proxykey start|resume|status — on the controller

  mop server proxykey start --nodes n1,n2 --exclusive   frozen recipient set, explicit nodes
  mop server proxykey start --all-registered --exclusive  full known node set, offline included
  mop server proxykey start --no-recipients --exclusive   deliberate empty recipient set
  mop server proxykey resume --exclusive
  mop server proxykey status [--json]

One writer, one operation, checkpoints all the way (the rotation
library). The verbs are a thin front: the adapter supplies the real effects —
loopback management API of the installed proxy, client-key probes — and
every decision stays in the library. Works on the controller only; a
puppet context is refused before any network or file touch.

start requires --exclusive: the caller affirms no other writer (panel,
deploy) is active — the upstream has no compare-and-swap. The recipient
set is frozen at start: offline nodes stay recipients, resume never
re-selects membership. Quiet on a fast success; pending or refusal is
rc 1 with a safe reason (no key material anywhere in the output).
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
