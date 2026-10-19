"""Конечный private WS wire #763, независимо от Django/native реализации.

HYPOTHESIS: у общих endpoint DTO нет строгого ограниченного WS codec.
SPEC: точные schema/types/UUID/decimal/UTF-8 и направление, без общего RPC.
SOLUTION: один общий конечный codec проверяет форму и байтовые границы.
RESULT: отсутствие decoder дало RED; три строгие проверки прошли GREEN.
STATUS: FIXED — see #763.
"""
import json
import unittest
from uuid import uuid4
import _lib  # noqa: F401
from mop.common import endpoint


class InboxWire(unittest.TestCase):
    def codec(self):
        self.assertTrue(callable(getattr(endpoint,'inbox_decode',None)),
            'finite shared private inbox decoder is missing')
        self.assertTrue(callable(getattr(endpoint,'inbox_encode',None)),
            'finite shared private inbox encoder is missing')
        return endpoint.inbox_decode, endpoint.inbox_encode

    def test_exact_handshake_and_byte_bound(self):
        decode, encode=self.codec()
        ready=dict(schema_version=1,type='peer_ready',external_session_id='native-conversation')
        self.assertEqual(decode(encode(ready),direction='client'),ready)
        for value in [dict(ready,schema_version=True),dict(ready,extra=1),dict(ready,external_session_id=''),
                      dict(ready,external_session_id='é'*129),dict(ready,external_session_id='a\x00b')]:
            with self.assertRaises(ValueError): decode(json.dumps(value),direction='client')
        for value in ['{"schema_version":1,"schema_version":1,"type":"peer_ready","external_session_id":"x"}',
                      '{"schema_version":1,"type":"peer_ready","external_session_id":NaN}',
                      '['*40+'0'+']'*40, '"'+'é'*4100+'"']:
            with self.assertRaises(ValueError): decode(value,direction='client')
        with self.assertRaises(ValueError): decode(encode(ready),direction='server')

    def test_delivery_maximum_escaped_text_and_result_are_exact(self):
        decode, encode=self.codec()
        ids=[str(uuid4()) for _ in range(3)]
        frame=dict(id=ids[0],message_id=ids[1],external_session_id='native',text='\x01'*65536,
                   priority='now',notify_when_idle=True,reply_address='private:'+str(uuid4()))
        delivery=dict(schema_version=1,type='delivery',connection_nonce=ids[2],frame=frame)
        wire=encode(delivery)
        self.assertLessEqual(len(wire.encode()),401408)
        self.assertGreater(len(wire.encode()),8192)
        self.assertEqual(decode(wire,direction='server'),delivery)
        for changes in [dict(text='é'*32769),dict(text='a\x00b'),dict(notify_when_idle=1),
                        dict(id=ids[1]),dict(reply_address='https://example.invalid'),dict(priority='other')]:
            with self.assertRaises(ValueError): encode(dict(delivery,frame=dict(frame,**changes)))
        bound=dict(schema_version=1,type='bound',endpoint_id=str(uuid4()),external_session_id='native',
            epoch=str(uuid4()),generation='1',connection_nonce=ids[2],lease_seconds=15)
        self.assertEqual(decode(encode(bound),direction='server'),bound)
        for generation in ['0','01','-1','1.0','١',True,1,'1'*21]:
            with self.assertRaises(ValueError): encode(dict(bound,generation=generation))
        for nonce in [ids[2].upper(),'{'+ids[2]+'}',ids[2].replace('-',''),True]:
            with self.assertRaises(ValueError): encode(dict(bound,connection_nonce=nonce))

    def test_all_control_shapes_reject_reflection_and_type_confusion(self):
        decode,encode=self.codec()
        nonce,delivery,message,operation,probe=[str(uuid4()) for _ in range(5)]
        ids=dict(connection_nonce=nonce,delivery_id=delivery,message_id=message)
        client=[dict(type='bound_received',connection_nonce=nonce),dict(type='prewrite',**ids),
            dict(type='application_acceptance',outcome='accepted',**ids),dict(type='idle_observed',**ids),
            dict(type='peer_ready_again',connection_nonce=nonce,probe_id=probe,external_session_id='native')]
        receipt=dict(type='receipt',connection_nonce=nonce,result=dict(id=delivery,message_id=message,
            operation_request_id=operation,state='accepted',idle=False))
        server=[dict(type='write_allowed',**ids),dict(type='probe',connection_nonce=nonce,probe_id=probe),receipt]
        for direction,values in [('client',client),('server',server)]:
            for value in values:
                value=dict(value,schema_version=1)
                self.assertEqual(decode(encode(value),direction=direction),value)
                with self.assertRaises(ValueError):
                    decode(encode(value),direction='client' if direction=='server' else 'server')
                for bad in [dict(value,schema_version=1.0),dict(value,connection_nonce=True),
                            dict(value,extra=None),dict(value,type=[]),dict(value,type='rpc')]:
                    with self.assertRaises(ValueError): decode(json.dumps(bad),direction=direction)
        for outcome in ['delivered','held','queued',True,None,{},1]:
            with self.assertRaises(ValueError):
                encode(dict(schema_version=1,type='application_acceptance',outcome=outcome,**ids))
        for result in [dict(receipt['result'],idle=1),dict(receipt['result'],id=message),
                       dict(receipt['result'],state='unknown',idle=True),dict(receipt['result'],state='delivered')]:
            with self.assertRaises(ValueError): encode(dict(receipt,schema_version=1,result=result))


if __name__=='__main__':
    unittest.main()
