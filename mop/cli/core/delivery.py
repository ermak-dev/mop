"""mop delivery [delivery-id | --epoch UUID --message-id UUID]: read a private delivery receipt

Returns committed controller state, not a WebSocket write acknowledgement.
Queued, attempting and unknown states do not prove native application acceptance.
Terminal unknown is the truthful outcome for an ordinary native 2.1.302
delivery and is never re-sent; recovery is read-only by the original key.
Idle is a separate matched native observation. Access uses the current Actor.
"""
from mop.cli import _control

MCP = {"annotations": "readonly", "actions": ["execution.view_delivery"], "args": [
    {"name": "delivery_id", "type": "string", "help": "controller delivery UUID, exclusive with sender key"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "help": "original controller epoch for key-only recovery"},
    {"name": "message_id", "type": "string", "flag": "--message-id", "help": "original report UUID; lookup only, never a repost"}]}


def main(argv):
    return _control.invoke('delivery', argv)
