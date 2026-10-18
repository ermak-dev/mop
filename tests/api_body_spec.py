#!/usr/bin/env python3
"""HYPOTHESIS: прежний spec wrapper опирается на ready/bus и не новый registration.
SOLUTION: отдельный pure new-job renderer с capability gate и pinned layout.
RESULT: 3 теста сначала отказали на отсутствии new renderer, затем зелёные;
настоящий bash wrapper исполнил только owned fixture launcher с exact public pins.
Проверка не регистрирует Nomad job и не подтверждает production SessionApi.
STATUS: FIXED — see #643 (new-registration renderer stage)
"""
import hashlib
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

import _lib  # noqa: F401 -- общий временный корень процесса (#338)
import hermetic  # noqa: F401
from _body_fixture import work, bundle
from mop.driver.launch import MachineConfig, Registration, RegisteredLayout
from mop.server import launch_spec


class ApiBodySpecTests(unittest.TestCase):
    def arguments(self, directory):
        b = bundle()
        w = replace(work(), bundle_digest=b.digest)
        registration = Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        layout = RegisteredLayout(Path(directory), registration)
        return dict(layout=layout, machine=MachineConfig('https://control.example.test',
            '10.20.0.4', '192.0.2.6', 2207, 'host'), bundle=b,
            nomad_node_id=str(uuid4()), datacenter='owned-fixture', user='owned',
            memory_mb=512, cpu=100, restore_generation=uuid4(), capabilities=frozenset(('addressed_launch_v1',
            'session_scope_v1', 'scope_retirement_v1', 'external_image_v1', 'bootstrap_ssh_v1')))

    def snapshot_arguments(self):
        from uuid import UUID
        args = self.arguments('/owned')
        registration = args['layout'].registration
        # Фиксированные пины: слепок сравнивает байты, не случайные UUID fixture.
        fields = ('epoch', 'operation_id', 'step_id', 'target_id', 'incarnation_id',
                  'node_id', 'executor_epoch', 'grant_id')
        w = replace(registration.work, **{name: str(UUID(int=i))
                    for i, name in enumerate(fields, 1)})
        registration = replace(registration, work=w, bootstrap_revision=str(UUID(int=9)))
        return {**args, 'layout': RegisteredLayout(Path('/owned'), registration),
                'nomad_node_id': str(UUID(int=10)), 'restore_generation': UUID(int=11)}

    # BUG (#714): импорт modern renderer захватывает legacy config/grants/Nomad.
    # HYPOTHESIS: api_job_spec чиста, но её модуль spec инициализирует старую
    # установку; проверять надо холодный импорт и вызов, а не готовый sys.modules.
    # SOLUTION: launch_spec владеет renderer и constants; createbinding берёт
    # их напрямую. Реальные driver/launch и addressed_metadata не подменяются.
    # RESULT: RED -- config.get(NOMAD_ADDR), config.num(MOP_PUPPET_MEM_MB),
    # import server.grants; GREEN -- холодный импорт и рендер с теми же traps.
    # STATUS: FIXED — see #714
    def test_modern_renderer_import_and_render_do_not_read_legacy_authority(self):
        script = r'''
import importlib
import sys
import hermetic
from mop.common import config

mode = sys.argv[2]
if mode in ('get', 'num'):
    def refuse(*args, **kwargs):
        raise AssertionError('legacy config.' + mode + ' read: ' + repr(args))
    setattr(config, mode, refuse)
else:
    class Trap:
        def find_spec(self, fullname, path=None, target=None):
            if fullname in ('mop.server.spec', 'mop.server.grants', 'mop.server.nomad', 'nomad'):
                raise AssertionError('legacy authority import: ' + fullname)
    sys.meta_path.insert(0, Trap())

renderer = importlib.import_module(sys.argv[1])
from api_body_spec import ApiBodySpecTests
args = ApiBodySpecTests().snapshot_arguments()
job = renderer.api_job_spec(**args)
assert job['Job']['TaskGroups'][0]['Tasks'][0]['Config']['args'][-1] == renderer.API_WRAPPER
'''
        env = hermetic.child_env({'PYTHONPATH': os.pathsep.join((hermetic.TESTS, hermetic.ROOT))})
        for mode in ('get', 'num', 'imports'):
            with self.subTest(trap=mode):
                result = subprocess.run([sys.executable, '-c', script,
                    self.renderer().__module__, mode], cwd=hermetic.ROOT, env=env,
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_registration_bytes_match_the_pre_isolation_renderer(self):
        # Слепок spec.api_job_spec на 7424a8a7 до переноса (#714), включая
        # wrapper, Meta hash и Nomad $${...}; JSON кодируется без пробелов.
        wire = json.dumps(self.renderer()(**self.snapshot_arguments()), sort_keys=True,
                          separators=(',', ':'), ensure_ascii=False).encode()
        self.assertEqual(len(wire), 2308)
        self.assertEqual(hashlib.sha256(wire).hexdigest(),
                         '0b24c2fdd837f8e2854c175c7559c569d892e8eeff3eca1abf940acb3b4e78c1')

    def renderer(self):
        self.assertTrue(hasattr(launch_spec, 'api_job_spec'), 'new API job registration renderer is missing')
        return launch_spec.api_job_spec

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
            self.assertEqual(job['Meta']['mop.'+name], getattr(r.work, name))
        self.assertNotRegex(task['Config']['args'][-1], r'(?<!\$)\$\{')

    # HYPOTHESIS (#643): новая регистрация пишет Meta без префикса и без
    # restore_generation/owner_id/revision, а stop/delete (#625) читают job только
    # по job_metadata(pin): тело, поднятое новым путём, нельзя было бы ни
    # остановить, ни удалить -- проверка Meta отказывала бы как чужому job.
    # SOLUTION: одна функция адресных метаданных для запуска и для остановки;
    # пины запуска -- дополнительные ключи, не замена.
    # STATUS: FIXED — see #643
    def test_registered_job_carries_the_stop_path_addressed_metadata(self):
        from types import SimpleNamespace
        from uuid import UUID
        from mop.server.operations.scheduler import JobPin, job_metadata
        render = self.renderer()
        args = self.arguments('/owned')
        restore = uuid4()
        job = render(**{**args, 'restore_generation': restore})['Job']
        w = args['layout'].registration.work
        # Поля RestartTarget без Django-модуля: job_metadata читает только их.
        target = SimpleNamespace(target_id=UUID(w.target_id), node_id=UUID(w.node_id),
            incarnation_id=UUID(w.incarnation_id), executor_epoch=UUID(w.executor_epoch),
            owner_id=w.owner_id, generation=w.generation, revision=w.revision, role=w.role,
            bundle_digest=w.bundle_digest, grant_id=UUID(w.grant_id))
        pin = JobPin(UUID(w.epoch), target, uuid4(), w.target_id, 'default', 1, restore)
        for key, value in job_metadata(pin).items():
            self.assertEqual(job['Meta'].get(key), value, key)
        with self.assertRaises(ValueError):
            render(**{**args, 'restore_generation': str(restore)})

    # HYPOTHESIS (#643): рендер требует soul.Bundle с байтами промпта, а
    # регистрирующий job контроллер их не видит: определение хранится
    # зашифрованным, расшифровывает только credential-сервис. Регистрация job
    # нового тела была бы невозможна без второй копии байтов на сервере.
    # SOLUTION: рендер принимает и пин одобренного определения (роль, digest,
    # драйвер) без байтов; байты сверяет узел в LaunchResponse.check.
    # STATUS: FIXED — see #643
    def test_registration_renders_from_the_approved_definition_pin_without_bytes(self):
        from mop.driver.launch import BundlePin
        render = self.renderer()
        args = self.arguments('/owned')
        w = args['layout'].registration.work
        pin = BundlePin(w.role, w.bundle_digest, 'any')
        job = render(**{**args, 'bundle': pin})['Job']
        self.assertEqual(job, render(**args)['Job'])
        for wrong in (BundlePin('other', w.bundle_digest, 'any'), BundlePin(w.role, 'f'*64, 'any')):
            with self.subTest(pin=wrong), self.assertRaises(ValueError):
                render(**{**args, 'bundle': wrong})
        with self.assertRaises(ValueError):
            render(**{**args, 'bundle': BundlePin(w.role, w.bundle_digest, 'isolating')})
        with self.assertRaises(ValueError):
            BundlePin(w.role, 'not-a-digest', 'any')

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
        # Короткий собственный корень (#677): во вложенном прогоне наследуемый
        # TMPDIR удлиняет runtime-путь за sockaddr_un, и RegisteredLayout
        # отказывает заранее; здесь исполняется launcher, сокет не строится,
        # но layout обязан быть осуществимым, как настоящий.
        with tempfile.TemporaryDirectory(prefix='m643-', dir='/tmp') as directory:
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
