"""mop diagnose: offline summary of the selected server login

Prints which server the next command would talk to, where that choice
came from (command line, environment, clone binding, or the sole joined
directory), where its login directory is, and whether the login is whole:
complete, empty (no whole connection.json; leftovers of the old
operator.json+client.json pair are no login) or a machine service
identity. No network, no prompts, no writes; the login's secrets are
never shown. The schema line names the version of a validated
connection.json only; other formats carry no schema claim.

Broken or conflicting directories refuse with exit code 1 -- the refusal
is the diagnosis. empty/machine are informational: exit code 0 with an
explicit not-ready line, not a success message.
"""
import json
import sys

from mop.cli import lib
from mop.client import diagnose
from mop.common import creds

MCP = {"annotations": "readonly", "args": [
    {"name": "json", "type": "boolean", "flag": "--json",
     "help": "print the summary as one JSON object"}]}


NOT_READY = {
    "empty": "no login: run mop login --server {server}",
    "machine": "machine service identity: use a separate operator account "
               "or connection directory, not-ready for a human login",
}


def main(argv):
    as_json = argv == ["--json"]
    if argv and not as_json:
        lib.usage(__doc__)
    try:
        got = diagnose.report()
    except creds.ProfileError as e:
        lib.fail(str(e))
        return 1
    except RuntimeError as e:
        lib.fail(str(e))
        return 1
    if as_json:
        print(json.dumps(got))
        return 0
    print(f"server {got['server']} (from {got['source']})")
    print(f"login directory {got['directory']}")
    fmt = got["format"] or "no profile files"
    if got["schema"] is not None:
        print(f"profile status {got['status']} (format {fmt}, "
              f"schema version {got['schema']})")
    else:
        print(f"profile status {got['status']} (format {fmt})")
    hint = NOT_READY.get(got["status"])
    if hint:
        print(hint.format(server=got["server"]))
    return 0
