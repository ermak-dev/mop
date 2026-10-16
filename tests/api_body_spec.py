#!/usr/bin/env python3
"""HYPOTHESIS: прежний spec wrapper опирается на ready/bus и не новый registration.
SOLUTION: отдельный pure new-job renderer с capability gate и pinned layout.
RESULT: 3 теста сначала отказали на отсутствии new renderer, затем зелёные;
настоящий bash wrapper исполнил только owned fixture launcher с exact public pins.
Проверка не регистрирует Nomad job и не подтверждает production SessionApi.
STATUS: FIXED — see #643 (new-registration renderer stage)
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from uuid import uuid4

import hermetic  # noqa: F401
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from body_launch import work, bundle
from mop.driver.launch import MachineConfig, Registration, RegisteredLayout
from mop.server import spec


class ApiBodySpecTests(unittest.TestCase):
    def arguments(self, directory):
        b = bundle()
        w = replace(work(), bundle_digest=b.digest)
        registration = Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        layout = RegisteredLayout(Path(directory), registration)
        return dict(layout=layout, machine=MachineConfig('https://control.example.test',
            '10.20.0.4', '192.0.2.6', 2207, 'host'), bundle=b,
            nomad_node_id=str(uuid4()), datacenter='owned-fixture', user='owned',
            memory_mb=512, cpu=100, capabilities=frozenset(('addressed_launch_v1',
            'session_scope_v1', 'scope_retirement_v1', 'external_image_v1', 'bootstrap_ssh_v1')))

    def renderer(self):
        self.assertTrue(hasattr(spec, 'api_job_spec'), 'new API job registration renderer is missing')
        return spec.api_job_spec

    def test_new_registration_has_only_public_pins_and_exact_placement(self):
        render = self.renderer()
        args = self.arguments('/owned')
        job = render(**args)['Job']
        r = args['layout'].registration
        task = job['TaskGroups'][0]['Tasks'][0]
        self.assertEqual(job['ID'], r.work.target_id)
        self.assertEqual(job['Constraints'], [dict(LTarget='${node.unique.id}', Operand='=',
                                                 RTarget=args['nomad_node_id'])])
        self.assertEqual(task['Env']['MOP_REGISTERED_LAUNCHER'], str(args['layout'].launcher))
        self.assertEqual(task['Env']['MOP_BOOTSTRAP_REVISION'], r.bootstrap_revision)
        self.assertEqual(task['Env']['MOP_GENERATION'], str(r.work.generation))
        self.assertEqual(task['Resources'], dict(CPU=100, MemoryMB=512, MemoryMaxMB=512))
        wire = json.dumps(job)
        for forbidden in ('bus.json', 'ready.json', 'node.env', 'secrets.env', 'operator_token',
                          'MOP_TELEMETRY_TOKEN', 'ANTHROPIC_AUTH_TOKEN', 'MOP_BUS_CONFIG'):
            self.assertNotIn(forbidden, wire)
        for name in ('target_id', 'incarnation_id', 'node_id', 'executor_epoch', 'grant_id', 'bundle_digest'):
            self.assertEqual(job['Meta'][name], getattr(r.work, name))
        self.assertNotRegex(task['Config']['args'][-1], r'(?<!\$)\$\{')

    def test_known_unsupported_or_mismatched_capability_refuses_before_registration(self):
        render = self.renderer()
        args = self.arguments('/owned')
        for capability in args['capabilities']:
            with self.subTest(capability=capability), self.assertRaisesRegex(ValueError, 'capabilit'):
                render(**{**args, 'capabilities':args['capabilities']-{capability}})
        for change in ({'nomad_node_id':'node-from-display-name'}, {'cpu':True},
                       {'memory_mb':0}, {'user':'root; sh'}, {'datacenter':''}):
            with self.subTest(change=change), self.assertRaises(ValueError): render(**{**args, **change})
        other = bundle()
        from mop.common import soul
        changed = soul.from_manifest(other.manifest.to_dict(), b'not approved')
        with self.assertRaises(ValueError): render(**{**args, 'bundle':changed})

    def test_wrapper_executes_only_registered_launcher_with_public_pin_arguments(self):
        render = self.renderer()
        with tempfile.TemporaryDirectory(prefix='m643-') as directory:
            args = self.arguments(directory)
            layout = args['layout']
            layout.runtime.mkdir(parents=True, mode=0o700)
            # HYPOTHESIS (#676): shebang /usr/bin/python3 -- это путь хоста
            # установки, не образа: в python:3.12-slim интерпретатор живёт в
            # /usr/local/bin/python3, и bash отдаёт 127 до контракта wrapper.
            # SOLUTION (#676): fixture launcher называет текущий интерпретатор
            # (sys.executable) -- образ, в котором идут проверки, сам по себе
            # источник пути, без пакета хоста или конкретного префикса.
            layout.launcher.write_text(f'#!{sys.executable}\n'
                'import json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            layout.launcher.chmod(0o700)
            job = render(**args)['Job']
            task = job['TaskGroups'][0]['Tasks'][0]
            shell = task['Config']['args'][-1].replace('$${', '${')
            completed = subprocess.run(['/bin/bash', '-c', shell], env={**os.environ, **task['Env']},
                text=True, capture_output=True, timeout=5)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            received = json.loads(completed.stdout)
            r = layout.registration
            self.assertEqual(received, ['--target', r.work.target_id,
                '--incarnation', r.work.incarnation_id, '--executor-epoch', r.work.executor_epoch,
                '--generation', str(r.work.generation), '--bootstrap-revision', r.bootstrap_revision])


if __name__ == '__main__': unittest.main()
