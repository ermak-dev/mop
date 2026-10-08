"""the rotation status: mop server proxykey status [--json]

Reads the private checkpoint and prints the library's safe projection:
stage, per-recipient codes, refusal reasons — never a key, never a raw
agent tail. No operation is a quiet zero; an invalid state file is rc 1
with the validator's reason.
"""
import json

from mop.cli import lib
from mop.server import proxykey


def main(argv):
    if argv and argv != ["--json"]:
        lib.usage(__doc__)
        return 1
    as_json = argv == ["--json"]
    out = proxykey.status()
    if out is None:
        return 0
    if as_json:
        print(json.dumps(out))
    elif out.get("stage") == "invalid":
        lib.fail(out.get("reason") or "invalid")
        return 1
    elif out.get("stage") == "completed":
        print("completed")
    else:
        print(f"pending: stage {out.get('stage')}")
    return 0
