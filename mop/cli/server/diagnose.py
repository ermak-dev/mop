"""mop server diagnose: offline summary of this installation only

Reports two local facts, no network, no migration, no writes: where each
effective setting of this installation comes from (the same resolver as
`mop server config`, values themselves are not printed here), and what
the local project registry runs on -- a v1 snapshot, the legacy pair of
files, or nothing yet. This is not the registry of a remote selected
server; client-side selection is `mop diagnose`.

A registry that cannot be read refuses with exit code 1 naming the file;
a missing registry is informational: exit code 0 with an explicit
not-ready line.
"""
import json
import os
import sys

from mop.cli import lib
from mop.server import diagnose

MCP = {"annotations": "readonly", "args": [
    {"name": "json", "type": "boolean", "flag": "--json",
     "help": "print the summary as one JSON object"}]}

NOT_READY = {
    "legacy pair": "registry still on the legacy pair: needs a representable "
                   "migration (bare names or colliding origins would refuse); "
                   "no changes made here, not-ready as v1",
    "empty": "no registry yet: not-ready, add the first project to create it",
}


def main(argv):
    as_json = argv == ["--json"]
    if argv and not as_json:
        lib.usage(__doc__)
    try:
        got = diagnose.report()
    except RuntimeError as e:
        lib.fail(str(e))
        return 1
    if as_json:
        print(json.dumps(got))
        return 0
    print(f"settings of this installation, {len(got['settings'])} values, "
          f"sources only (values: mop server config)")
    counts = {}
    for source in got["settings"].values():
        counts[source] = counts.get(source, 0) + 1
    print("settings sources " +
          ", ".join(f"{src} {n}" for src, n in sorted(counts.items())))
    reg = got["registry"]
    files = ", ".join(os.path.basename(f) for f in reg["files"])
    print(f"registry {reg['source']} ({reg['projects']} projects, "
          f"files {files}, at {os.path.dirname(reg['files'][0])})")
    hint = NOT_READY.get(reg["source"])
    if hint:
        print(hint)
    return 0
