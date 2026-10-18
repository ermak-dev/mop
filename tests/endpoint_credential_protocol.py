"""Конечный stdlib контракт private reports, не crypto oracle #669.

HYPOTHESIS: common transport не имеет application send/take без actor claims.
SPEC: точный message/deadline, только endpoint+nonce для take, private repr.
SOLUTION: два конечных typed commands с закрытой схемой и private DTO.
RESULT: отсутствующие commands/client дали red; все пять проверок green.
STATUS: FIXED — see #669 (только private application protocol).
"""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401

from mop.common import credentials as p
from mop.common.credential_client import CredentialClient


class PrivateReportProtocol(unittest.TestCase):
    def setUp(self):
        self.auth = p.Credential('token', 'a'*40)
        self.args = dict(expected_epoch=str(uuid4()), address='private:'+str(uuid4()),
            message_id=str(uuid4()), operation_request_id=None, text='private-marker',
            not_after='2026-10-18T18:01:02.123456+03:00', notify_when_idle=True, priority='now',
            sender_endpoint_id=None, sender_connection_nonce=None)

    def request(self, command, args):
        return dict(version=1, auth=dict(kind='token', value=self.auth.value), command=command, args=args)

    def test_finite_send_retains_deadline_and_message_options_without_authority(self):
        _, command, value = p.parse_request(self.request('send_private_report', self.args))
        self.assertEqual(command.value, 'send_private_report')
        self.assertEqual(str(value['message_id']), self.args['message_id'])
        self.assertEqual(value['not_after'].isoformat(), self.args['not_after'])
        self.assertEqual(value['text'], 'private-marker')
        self.assertIs(value['notify_when_idle'], True)
        for name in ('actor', 'sender', 'owner_id', 'credential_id', 'ciphertext', 'aad', 'purpose'):
            with self.subTest(name=name), self.assertRaises(p.CredentialError):
                p.parse_request(self.request('send_private_report', {**self.args, name: 'untrusted'}))

    def test_only_endpoint_and_nonce_can_select_take(self):
        args = dict(endpoint_id=str(uuid4()), connection_nonce=str(uuid4()))
        _, command, parsed = p.parse_request(self.request('take_private_delivery', args))
        self.assertEqual(command.value, 'take_private_delivery')
        self.assertEqual(str(parsed['connection_nonce']), args['connection_nonce'])
        for name in ('delivery_id', 'user_id', 'client_id', 'project_id', 'gateway_id', 'generation', 'credential_id'):
            with self.subTest(name=name), self.assertRaises(p.CredentialError):
                p.parse_request(self.request('take_private_delivery', {**args, name: str(uuid4())}))

    def test_malformed_and_nonfinite_inputs_have_safe_finite_errors(self):
        for key, value in (('priority', []), ('priority', 'later'), ('notify_when_idle', 1),
                           ('text', None), ('text', 'x'*65537), ('not_after', '2026-10-18'),
                           ('not_after', '2026-10-18T18:00:00Z'), ('expected_epoch', 'bad'),
                           ('sender_endpoint_id', str(uuid4())), ('sender_connection_nonce', str(uuid4()))):
            with self.subTest(key=key), self.assertRaises(p.CredentialError):
                p.parse_request(self.request('send_private_report', {**self.args, key: value}))

    def test_typed_send_forwards_exact_fields_and_returns_only_receipt(self):
        self.assertTrue(callable(getattr(CredentialClient, 'send_private_report', None)), 'protected send is missing')
        result = dict(id=str(uuid4()), message_id=self.args['message_id'], operation_request_id=None,
                      state='queued', idle=False)
        args = {**self.args, 'expected_epoch': p.identifier(self.args['expected_epoch']),
            'message_id': p.identifier(self.args['message_id']), 'not_after': p.instant('2026-10-18T15:01:02+00:00')}
        args['not_after'] = datetime.fromisoformat(self.args['not_after'])
        client = CredentialClient('/tmp/fixture/service.sock', server_uid=0)
        with patch.object(client, '_invoke', return_value=result) as invoke:
            receipt = client.send_private_report(self.auth, **args)
        self.assertEqual(str(receipt.id), result['id'])
        self.assertEqual(invoke.call_args.args[2], self.args)
        self.assertNotIn(self.auth.value, repr(receipt))

    def test_delivery_frame_has_no_plaintext_repr_or_unsolicited_fields(self):
        self.assertTrue(callable(getattr(CredentialClient, 'take_private_delivery', None)), 'protected take is missing')
        args = dict(endpoint_id=uuid4(), connection_nonce=uuid4())
        result = dict(id=str(uuid4()), message_id=str(uuid4()), external_session_id='external',
            text='private-marker', priority='next', notify_when_idle=False, reply_address=None)
        client = CredentialClient('/tmp/fixture/service.sock', server_uid=0)
        with patch.object(client, '_invoke', return_value=result):
            frame = client.take_private_delivery(self.auth, **args)
            self.assertNotIn('private-marker', repr(frame))
        for change in (dict(owner_id=1), dict(priority=[]), dict(notify_when_idle=1), dict(text=None)):
            with self.subTest(change=change), patch.object(client, '_invoke', return_value={**result, **change}), \
                    self.assertRaises(p.CredentialError):
                client.take_private_delivery(self.auth, **args)
        with patch.object(client, '_invoke', return_value=None):
            self.assertIsNone(client.take_private_delivery(self.auth, **args))


if __name__ == '__main__':
    unittest.main()
