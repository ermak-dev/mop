"""NodeHTTP читает только конечный addressed private send input (#625).

HYPOTHESIS: точная restart-only схема отвергает message и его correlation.
SPEC: bounded text, message/request IDs, finite priority; не RPC/shell/owner.
"""
import unittest
from uuid import uuid4
import hermetic
import _lib  # noqa: F401 -- общий временный корень процесса (#338)
from _body_fixture import wire
from mop.node.http_execution import MachineClient, Work, Refused


class MessageInput(unittest.IsolatedAsyncioTestCase):
    async def test_client_accepts_typed_send_payload_and_rejects_unknown_fields(self):
        work = Work.parse(wire())
        value = dict(schema_version=1, operation_id=work.operation_id, step_id=work.step_id,
            git_name='Owner', git_email='owner@example.invalid', message='private message',
            message_id=str(uuid4()), operation_request_id=str(uuid4()), priority='next',
            notify_when_idle=False, holder_endpoint=None, reply_address=None)
        class Transport:
            async def request(self, *args, **kwargs):
                return value
        client = MachineClient(Transport())
        self.assertEqual(await client.action_input(work), value)
        for change in ({'message':'\ud800'}, {'message':'x'*16385}, {'priority':[]},
                {'message_id':str(uuid4()).upper()}, {'notify_when_idle':True},
                {'reply_address':'private:'+str(uuid4())}, {'shell':'untrusted'}):
            saved = dict(value)
            value.update(change)
            with self.subTest(fields=tuple(change)):
                with self.assertRaises(Refused):
                    await client.action_input(work)
            value.clear(); value.update(saved)


if __name__ == '__main__':
    unittest.main()
