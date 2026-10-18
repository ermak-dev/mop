#!/usr/bin/env python3
"""HYPOTHESIS: planned phases have no admitted, physically owned HOST broker.
SPEC: three finite phases, actual Admission, exact retained input/reference,
connection-scoped checkpoints and installed content/resource receipts; no native.
SOLUTION: not implemented yet; tests precede every checkable effect.
RESULT: pending RED.
STATUS: pending — see #752
"""
import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
import inspect
from pathlib import Path
import unittest
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _host_inputs_data import selected
from mop.node.http_execution import EffectGuard, Refused


def api(name='host_phases'):
    assert importlib.util.find_spec('mop.node.'+name) is not None, \
        'actual planned phase '+name+' is missing (#752)'
    return importlib.import_module('mop.node.'+name)


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_passes_the_actual_claim_admission_to_effect_guard(self):
        from mop.node.http_execution import Runtime
        a, _, _ = selected()
        calls = []
        class Client:
            async def claim(self, work, request):
                self.assert_work = work
                return a
            async def recheck(self, admission):
                calls.append(admission)
            async def result(self, admission, outcome, evidence):
                calls.append((admission, outcome, evidence))
        class Executor:
            async def execute(self, work, guard):
                self_guard = getattr(guard, 'admission', None)
                assert self_guard is a, 'Runtime lost the exact Admission before the physical phase boundary'
                await guard()
                guard.check()
                return 'succeeded', 'body-prepared'
        r = Runtime(Client(), Executor(), node_id=a.work.node_id, epoch=a.work.epoch,
                    executor_epoch=a.work.executor_epoch)
        r._ready = True
        await r._execute(a.work)
        self.assertIsNone(r.last_error)
        self.assertIs(calls[0], a)
        self.assertEqual(calls[-1], (a, 'succeeded', 'body-prepared'))

    async def test_legacy_guards_still_work_but_cannot_admit_a_planned_phase(self):
        called = []
        async def authorize(): called.append(True)
        g = EffectGuard(authorize, lambda: None)
        await g()
        self.assertEqual(called, [True])
        m = api()
        with self.assertRaisesRegex(Refused, 'Admission'):
            m.admitted(g, selected()[0].work)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.m = api()
        self.a, self.s, self.v = selected()

    def test_projection_is_exact_and_contains_no_consumer_bytes_or_physical_uuid_checksum(self):
        p = self.m.Projection.from_delivery(self.v)
        self.assertEqual(p.image_revision, self.v['image_revision'])
        self.assertEqual(p.image_policy, self.v['image_policy'])
        self.assertEqual(p.source, self.v['selection']['source'])
        self.assertNotIn('consumers', p.wire())
        self.assertNotIn(self.v['consumers'][0]['value'], repr(p))
        self.assertEqual(self.m.Projection.parse(p.wire()), p)
        with self.assertRaises(Refused):
            self.m.Projection.parse({**p.wire(), 'artifact_hash': p.image_digest})

    def test_submit_requires_exact_admission_descriptor_and_no_previous_prepare_reference(self):
        from mop.node.host_inputs import pack
        from mop.node.host_scope import Reference
        d = pack(self.a, self.s, self.v).descriptor
        p = self.m.Projection.from_delivery(self.v)
        request = self.m.Submission(self.a, d, p)
        self.assertEqual(self.m.Submission.decode(request.encode()), request)
        ref = Reference(self.s, uuid4().hex, str(uuid4()))
        with self.assertRaises(Refused): self.m.Submission(self.a, d, p, ref)
        with self.assertRaises(Refused): self.m.Submission(self.a, replace(d, attempt_id=str(uuid4())), p)
        with self.assertRaises(Refused): self.m.Submission(self.a, replace(d, scope=replace(self.s, launch_id=str(uuid4()))), p)
        for extra in ({'path': '/root/input'}, {'argv': ['sh']}, {'env': {}}, {'unit_properties': {}},
                      {'callback': 'recheck'}, {'cleanup_descriptor': d.wire()}):
            from mop.node.host_scope import decode, encode
            with self.assertRaises(Refused):
                self.m.Submission.decode(encode({**decode(request.encode()), **extra}))

    def test_bootstrap_and_identity_require_original_physical_reference(self):
        from mop.node.host_inputs import Descriptor
        from mop.node.host_scope import Reference
        ref = Reference(self.s, uuid4().hex, str(uuid4()))
        for action in ('bootstrap_body', 'initial_identity'):
            a = replace(self.a, work=replace(self.a.work, action=action))
            d = Descriptor(self.s, a.attempt_id, 'a'*64)
            p = self.m.Projection.from_delivery(self.v)
            with self.assertRaises(Refused): self.m.Submission(a, d, p)
            self.m.Submission(a, d, p, ref)
            with self.assertRaises(Refused):
                self.m.Submission(a, d, p, replace(ref, scope=replace(self.s, node_id=str(uuid4()))))
        a = replace(self.a, work=replace(self.a.work, action='native_exec'))
        with self.assertRaises(Refused):
            self.m.Submission(a, Descriptor(self.s, a.attempt_id, 'a'*64), self.m.Projection.from_delivery(self.v), ref)

    def test_cutoff_keeps_original_nonrenewable_cleanup_reserve(self):
        effect, cutoff = self.m.cutoff(self.a, monotonic=100.)
        remaining = (self.a.lease_until-datetime.now(timezone.utc)).total_seconds()
        self.assertLessEqual(effect, 60.)
        self.assertAlmostEqual(cutoff-100., remaining-20., delta=.2)
        for seconds in (-1, 0, 19):
            with self.assertRaises(Refused):
                self.m.cutoff(replace(self.a, lease_until=datetime.now(timezone.utc)+timedelta(seconds=seconds)), monotonic=100.)

    def test_full_native_guard_remains_closed_for_nine_actions(self):
        from mop.node.native_startup import NativeBindings
        from mop.node.http_execution import ACTIONS, Unavailable
        self.assertEqual(len(ACTIONS), 9)
        with self.assertRaises(Unavailable): NativeBindings(object())


class SourceTests(unittest.TestCase):
    def test_fixed_https_and_ssh_formats_bind_exact_origin_without_ambient_authority(self):
        m = api('host_phase_source')
        https = dict(format='git-https-basic-v1', origin='https://git.example.invalid:8443/team/repo.git',
                     username='fixture-user', password='private-fixture-password')
        source = m.SourceCredential.parse(https, https['origin'])
        self.assertNotIn(https['password'], repr(source))
        ssh = dict(format='git-ssh-key-v1', origin='ssh://git@git.example.invalid:2222/team/repo.git',
                   private_key='-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n-----END OPENSSH PRIVATE KEY-----\n',
                   known_hosts='[git.example.invalid]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n')
        self.assertEqual(m.SourceCredential.parse(ssh, ssh['origin']).port, 2222)
        for item in ({**https, 'format': 'unknown'}, {**https, 'helper': '/tmp/tool'},
                     {**https, 'origin': 'https://other.invalid/repo'}, {**https, 'password': 'bad\x00value'},
                     {**ssh, 'known_hosts': '* ssh-ed25519 AAAA\n'},
                     {**ssh, 'known_hosts': '@cert-authority *.invalid ssh-ed25519 AAAA\n'},
                     {**ssh, 'origin': 'git@git.example.invalid:repo'},
                     {**https, 'origin': 'https://user:password@git.example.invalid/repo'},
                     {**https, 'origin': 'https://git.example.invalid/repo?query'},
                     {**https, 'origin': 'https://git.example.invalid/../repo'}):
            with self.subTest(format=item['format']), self.assertRaises(Refused):
                m.SourceCredential.parse(item, https['origin'] if item.get('format') != 'git-ssh-key-v1' else ssh['origin'])

    def test_compiled_git_environment_and_operands_cannot_inherit_node_or_git_poison(self):
        m = api('host_phase_source')
        env = m.git_environment(Path('/srv/mop/b/fixture'), Path('/opt/runtime/empty'), Path('/opt/runtime/ca.pem'))
        self.assertEqual(env['GIT_CONFIG_NOSYSTEM'], '1')
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], '/dev/null')
        self.assertEqual(env['GIT_CONFIG_SYSTEM'], '/dev/null')
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
        self.assertNotIn('SSH_AUTH_SOCK', env)
        self.assertNotIn('HTTPS_PROXY', env)
        for variable in ('GIT_CONFIG_PARAMETERS', 'GIT_SSH_COMMAND', 'GIT_AUTHOR_NAME', 'NODE_BEARER', 'PYTHONPATH'):
            self.assertNotIn(variable, env)
        argv = m.identity_commands(Path('/usr/bin/git'), '--hostile-name', 'owner@example.invalid')
        self.assertEqual(len(argv), 3)
        for item in argv:
            self.assertIn('--', item)
            self.assertLess(item.index('--'), len(item)-2)
        self.assertEqual(argv[0][-2:], ('user.name', '--hostile-name'))


class ResourceTests(unittest.TestCase):
    def test_cpu_mhz_uses_explicit_installed_capacity_and_disk_is_reservation_not_quota(self):
        m = api('host_phase_manifest')
        profile = m.Capacity(cpu_mhz_per_core=2000, cpu_weight=75, tasks_max=64,
                             memory_mb_max=4096, cpu_mhz_max=8000, disk_mode='shared-reservation')
        limits = profile.project(dict(policy_id=str(uuid4()), cpu=500, memory_mb=128, disk_mb=64))
        self.assertEqual(limits.memory_max, 128*1024*1024)
        self.assertEqual(limits.cpu_quota_usec, 250000)
        self.assertEqual(limits.cpu_weight, 75)
        self.assertEqual(limits.tasks_max, 64)
        self.assertEqual(limits.disk_mode, 'shared-reservation')
        self.assertFalse(limits.disk_isolation)
        self.assertEqual(limits.disk_reservation_bytes, 64*1024*1024)
        with self.assertRaises(Refused):
            replace(profile, disk_mode='hard-quota')
        for policy in (dict(cpu=0, memory_mb=128, disk_mb=64), dict(cpu=True, memory_mb=128, disk_mb=64),
                       dict(cpu=9000, memory_mb=128, disk_mb=64), dict(cpu=500, memory_mb=8192, disk_mb=64)):
            with self.assertRaises(Refused): profile.project(dict(policy_id=str(uuid4()), **policy))

    def test_manifest_requires_real_hashes_and_fixed_root_owned_runtime_not_logical_image_pin(self):
        m = api('host_phase_manifest')
        self.assertTrue(callable(m.RuntimeManifest.load))
        self.assertIn('root_owned', inspect.signature(m.verify_artifact).parameters)
        for raw in (b'{}', b'[]', b'{"schema_version":1,"image_revision":"'+str(uuid4()).encode()+b'"}'):
            with self.assertRaises(Refused): m.RuntimeManifest.parse(raw)


if __name__ == '__main__':
    unittest.main()
