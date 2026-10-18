"""mop build <project-id> --policy-id <id> --target-node <id> --epoch <id> --expected-revision N

Submit a durable image build from an existing approved immutable policy. Repeat
--target-node for the explicit bounded node set; --mode is missing (default),
update or rebuild. No local manifest, origin, branch or unapproved policy is
uploaded. --not-after is the absolute UTC execution deadline (default one hour).
--request-id fixes the initial identity before I/O; --wait is a bounded GET wait.

An accepted receipt is not a usable image: exit 3 retains its outstanding/unknown
operation. Recover with mop operation using the original key and epoch. There
is no automatic effect retry. Privileged force recovery and branch-policy
approval remain explicitly unsupported before submission.
The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "destructive",
       "actions": ["catalog.build_project", "operations.view_operation"], "args": [
    {"name": "project_id", "type": "string", "required": True, "help": "existing project resource id"},
    {"name": "policy_id", "type": "string", "flag": "--policy-id", "required": True, "help": "approved immutable image policy id"},
    {"name": "target_node", "type": "string", "flag": "--target-node", "repeatable": True, "help": "explicit target node id; repeat for several nodes"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "required": True, "help": "installation epoch"},
    {"name": "expected_revision", "type": "string", "flag": "--expected-revision", "required": True, "help": "project revision"},
    {"name": "mode", "type": "string", "flag": "--mode", "help": "missing, update or rebuild"},
    {"name": "request_id", "type": "string", "flag": "--request-id", "help": "initial command identity"},
    {"name": "not_after", "type": "string", "flag": "--not-after", "help": "absolute UTC execution deadline"},
    {"name": "wait", "type": "integer", "flag": "--wait", "help": "bounded GET wait in seconds"}]}


def main(argv):
    return _control.invoke('build', argv)
