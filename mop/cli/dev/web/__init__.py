"""the dashboard's build: Vite + React in web/, the result committed in web/dist

  mop dev web build            npm ci and vite build into web/dist and its
                               packaged mirror mop/server/controller/dist
  mop dev web build --check    build aside and compare both trees byte for byte
  mop dev web test [file ...]  run the dashboard interaction tests
  mop dev web test --types     check TypeScript
  mop dev web preview         serve the built dashboard locally, without a pool API

Node and npm are the developer's and CI's: the servers get the built dist
with the package and never run node. `--check` is what CI runs on every
push, so a dist that drifted from its sources is red, not silent. The
packaged mirror is written by the same single build and checked the same
way: one command for every output, no second hand-edited dist.
"""


def main(argv):
    # lib -- здесь, а не на уровне модуля (#302): пакет импортирует и
    # `mop dev web build`, которому библиотека шины не нужна, а lib её тянет.
    from mop.cli import lib
    lib.usage(__doc__)
