"""mop operation [id] [--request-id id --epoch id] [--wait seconds]

Retrieve an admitted operation, or recover an uncertain admission by its original
request-id and epoch. Recovery and waiting use GET only; they never resubmit the
effect. accepted/running/reconciling/partial/unknown return exit 3, not success.
An expired wait does not cancel the operation. Keep the original key and epoch.

The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "readonly", "actions": ["operations.view_operation"], "args": [
    {"name": "operation_id", "type": "string", "help": "operation id"},
    {"name": "request_id", "type": "string", "flag": "--request-id", "help": "original request id"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "help": "original installation epoch"},
    {"name": "wait", "type": "integer", "flag": "--wait", "help": "bounded GET wait in seconds"}]}


def main(argv):
    return _control.invoke('operation', argv)
