"""mop diagnose: offline summary of the selected server login

Prints which server the next command would talk to, where that choice
came from, where its login directory is, and whether the login is whole:
complete, partial (a leftover half of the old pair), empty or a machine
service identity. No network, no prompts, no writes; the login's secrets
are never shown.

Broken or conflicting directories refuse with exit code 1 -- the refusal
is the diagnosis. partial/empty/machine are informational: exit code 0
with an explicit not-ready line, not a success message.
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
    "partial": "incomplete login: re-run mop login --server {server}",
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
    print(f"profile status {got['status']} (schema version {got['schema']})")
    hint = NOT_READY.get(got["status"])
    if hint:
        print(hint.format(server=got["server"]))
    return 0
