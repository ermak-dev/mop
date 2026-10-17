"""mop dev openapi [--check]: regenerate the public OpenAPI artefact

Runs the offline server-side generation (mop/server/controller/openapi.py)
in a subprocess: the commandlet itself stays in the client layer and never
imports django, like `mop dev web build` keeps npm out of Python. The
generation needs no bootstrap, database or network -- only the tree and the
declared server extra.

Writes docs/API.schema.yaml. Silent when it lands.

  --check   generate into a temporary file instead and compare it with the
            committed docs/API.schema.yaml byte for byte; a difference is
            listed and the exit code is 1: regenerate with `mop dev openapi`
"""
import os
import subprocess
import sys
import tempfile

from mop.common import config
# Без mop.cli.lib (#302): командлету не нужна шина, а lib её тянет.
from mop.cli.term import fail, usage

ARTEFACT = os.path.join(config.PROJECT, "docs", "API.schema.yaml")
MODULE = "mop.server.controller.openapi"


def generate(out_path):
    """Прогон серверной генерации в subprocess; вывод -- только при отказе."""
    got = subprocess.run([sys.executable, "-m", MODULE, out_path],
                         cwd=config.PROJECT, capture_output=True, text=True)
    if got.returncode:
        sys.stderr.write(got.stdout + got.stderr)
        fail(f"openapi generation failed with exit {got.returncode}; the server extra "
             "and the declared drf-spectacular pin are required")
        sys.exit(1)
    return got


def read(path):
    with open(path, "rb") as stream:
        return stream.read()


def main(argv):
    if set(argv) - {"--check"}:
        usage(__doc__)
    if "--check" not in argv:
        generate(ARTEFACT)
        return 0
    with tempfile.NamedTemporaryFile(prefix="mop-openapi-", suffix=".yaml") as tmp:
        generate(tmp.name)
        fresh = read(tmp.name)
    if not os.path.exists(ARTEFACT):
        fail("docs/API.schema.yaml is missing; regenerate it with mop dev openapi")
        return 1
    if read(ARTEFACT) != fresh:
        fail("docs/API.schema.yaml differs from a fresh generation; "
             "regenerate it with mop dev openapi")
        return 1
    return 0
