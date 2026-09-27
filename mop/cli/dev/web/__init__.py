"""the dashboard's build: Vite + React in web/, the result committed in web/dist

  mop dev web build            npm ci and vite build into web/dist
  mop dev web build --check    build aside and compare with web/dist byte for byte

Node and npm are the developer's and CI's: the servers get the built dist
with the package and never run node. `--check` is what CI runs on every
push, so a dist that drifted from its sources is red, not silent.
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
