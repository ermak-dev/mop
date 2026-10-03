"""list models offered by the selected LLM proxy: mop models

Reads the live /v1/models catalog using the selected server's client key.
The key is never printed. The proxy catalog may include models that Claude
Code cannot use; this command does not test model compatibility.
"""
from mop.cli import lib
from mop.client import models


MCP = {"annotations": "readonly", "args": []}


def main(argv):
    if argv:
        lib.usage(__doc__)
    try:
        found = models.available()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    print("\n".join(found) if found else "no models")
