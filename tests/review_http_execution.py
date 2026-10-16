#!/usr/bin/env python3
"""Изолированные поведенческие проверки frozen #640, без пула.

HYPOTHESIS: gate до create_task оставляет scheduling gap; отсутствующие ports
обнаруживаются после мутации; --local уступает worktree config; fact values
проверяются по объединению типов вместо схемы каждого kind.
SOLUTION: синхронный final gate внутри owned spawn; preflight до первой мутации;
адресованный --worktree config + effective Git var; единая строгая схема фактов.
RESULT: пять исходных контрпримеров воспроизведены на 14ffc136; после правок
нет просроченной записи/замены неподдерживаемой pane, identity верна при send,
невалидный source явно unavailable и не complete, client отвергает подмену типов.
STATUS: FIXED — see #640
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import unittest

import hermetic
from http_execution import wire
from mop.node.http_execution import Work, EffectGuard, Refused, Unavailable
from mop.node.execution_adapters import BoundTarget, ActionInput, LocalExecutor, ProcessRunner
from mop.node.execution_facts import LocalFacts, FactScope


def binding(work, root):
    return BoundTarget(work.target_id, work.incarnation_id, work.node_id,
        work.executor_epoch, work.generation, work.role, work.bundle_digest,
        work.grant_id, root/'repo', root/'tmux', '%0', root/'launcher')


class Review(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_restart_must_not_replace_live_pane(self):
        runner = ProcessRunner()
        with tempfile.TemporaryDirectory(prefix='mop640-review-restart-') as tmp:
            root = Path(tmp)
            (root/'repo').mkdir()
            (root/'launcher').write_text('#!/bin/sh\nexec sleep 60\n')
            (root/'launcher').chmod(0o700)
            work = Work.parse(wire(action='restart_session'))
            target = binding(work, root)
            await runner.run(('tmux','-S',str(target.tmux_socket),'new-session','-d','-s','review','sleep 60'))
            pane = (await runner.run(('tmux','-S',str(target.tmux_socket),'display-message','-p','-t','review','#{pane_id}'))).strip()
            from dataclasses import replace
            target = replace(target, pane=pane)
            class Resolver:
                async def resolve(self, work): return target, ActionInput('Owner','owner@example.test')
            async def authorize(): pass
            adapter = LocalExecutor(runner, Resolver())
            command = ('tmux','-S',str(target.tmux_socket),'display-message','-p','-t',pane,'#{pane_pid}')
            try:
                old = (await runner.run(command)).strip()
                refused = False
                try:
                    await adapter.execute(work, EffectGuard(authorize, lambda: None))
                except Unavailable:
                    refused = True
                new = (await runner.run(command)).strip()
                print('unsupported restart refused:', refused, 'old PID', old, 'new PID', new, flush=True)
                self.assertTrue(refused, 'known unsupported replacement must be refused')
                self.assertEqual(old, new, 'missing ReplacementPort must deny before respawn-pane -k')
            finally:
                await runner.run(('tmux','-S',str(target.tmux_socket),'kill-server'))
                await adapter.aclose()

    async def test_missing_send_channel_must_not_write_identity(self):
        runner = ProcessRunner()
        with tempfile.TemporaryDirectory(prefix='mop640-review-send-') as tmp:
            root = Path(tmp)
            work = Work.parse(wire())
            target = binding(work, root)
            await runner.run(('git','init','-q',str(target.clone)))
            class Resolver:
                async def resolve(self, work): return target, ActionInput('Owner','owner@example.test','hello')
            async def authorize(): pass
            adapter = LocalExecutor(runner, Resolver())
            try:
                with self.assertRaises(Unavailable):
                    await adapter.execute(work, EffectGuard(authorize, lambda: None))
                config = (target.clone/'.git/config').read_text()
                print('unsupported send wrote user.name:', 'name = Owner' in config, flush=True)
                self.assertNotIn('name = Owner', config, 'missing session channel must deny before Git mutation')
            finally:
                await adapter.aclose()

    async def test_effective_identity_must_match_before_send(self):
        runner = ProcessRunner()
        with tempfile.TemporaryDirectory(prefix='mop640-review-identity-') as tmp:
            root = Path(tmp)
            work = Work.parse(wire())
            target = binding(work, root)
            git = ('git','-C',str(target.clone))
            # Настоящее linked worktree: общий local config не должен менять соседа.
            main = root/'main'
            await runner.run(('git','init','-q',str(main)))
            main_git = ('git', '-C', str(main))
            await runner.run((*main_git, 'config', 'user.name', 'Shared Owner'))
            await runner.run((*main_git, 'config', 'user.email', 'shared@example.test'))
            await runner.run((*main_git, 'commit', '--allow-empty', '-qm', 'fixture'))
            await runner.run((*main_git, 'worktree', 'add', '-qb', 'worker', str(target.clone)))
            await runner.run((*git,'config','extensions.worktreeConfig','true'))
            await runner.run((*git,'config','--worktree','user.name','Previous Owner'))
            await runner.run((*git,'config','--worktree','user.email','previous@example.test'))
            class Resolver:
                async def resolve(self, work): return target, ActionInput('Current Owner','current@example.test','hello')
            observed = []
            class Sender:
                async def send(self, target, inputs, before_send):
                    await before_send()
                    observed.append((await runner.run((*git,'var','GIT_AUTHOR_IDENT'))).strip())
                    return 'succeeded','delivered'
                async def aclose(self): pass
            async def authorize(): pass
            adapter = LocalExecutor(runner, Resolver(), sender=Sender())
            try:
                result = await adapter.execute(work, EffectGuard(authorize, lambda: None))
                print('send result:', result, 'effective author at send:', observed, flush=True)
                self.assertTrue(observed[0].startswith('Current Owner <current@example.test>'), 'successful config --local does not prove effective identity')
                self.assertTrue((await runner.run((*main_git, 'var', 'GIT_AUTHOR_IDENT'))).startswith(
                    'Shared Owner <shared@example.test> '), 'addressed work must not rewrite a sibling identity')
            finally:
                await adapter.aclose()

    async def test_final_gate_must_run_in_spawn_task_before_child_effect(self):
        runner = ProcessRunner()
        with tempfile.TemporaryDirectory(prefix='mop640-review-deadline-') as tmp:
            root = Path(tmp)
            await runner.run(('git','init','-q',str(root/'repo')))
            deadline = datetime.now(timezone.utc) + timedelta(seconds=.15)
            work = Work.parse(wire(action='apply_identity', not_after=deadline.isoformat()))
            target = binding(work, root)
            class Resolver:
                async def resolve(self, work): return target, ActionInput('Expired Owner','expired@example.test')
            owned_checks = []
            def local_check():
                if asyncio.current_task() in runner._spawns:
                    owned_checks.append(datetime.now(timezone.utc))
                work.gate(node_id=work.node_id, epoch=work.epoch, executor_epoch=work.executor_epoch)
            async def authorize():
                local_check()
                # Другой runnable callback удерживает loop между gate и новым spawn task.
                asyncio.get_running_loop().call_soon(time.sleep, .25)
            adapter = LocalExecutor(runner, Resolver())
            try:
                with self.assertRaises(Refused):
                    await adapter.execute(work, EffectGuard(authorize, local_check))
                config = (target.clone/'.git/config').read_text()
                print('deadline:', deadline.isoformat(), 'now:', datetime.now(timezone.utc).isoformat(),
                      'expired work wrote name:', 'name = Expired Owner' in config, flush=True)
                self.assertNotIn('name = Expired Owner', config, 'first Git child started after expiry; only the next effect was denied')
                self.assertEqual(runner.active, 0)
                self.assertEqual(runner._spawns, set())
                self.assertEqual(len(owned_checks), 1, 'final check must run in the owned spawn task')
                self.assertGreaterEqual(owned_checks[0], deadline)
            finally:
                await adapter.aclose()

    async def test_typed_facts_must_reject_invalid_boolean_and_counter_fields(self):
        work = Work.parse(wire())
        with tempfile.TemporaryDirectory(prefix='mop640-review-facts-') as tmp:
            target = binding(work, Path(tmp))
            class Authority:
                async def authorize(self, node_id, kind, limit): return FactScope(node_id,kind,(target,),True)
            class Supplemental:
                async def junk(self, target): return dict(candidate_count=False, bytes='unknown', inventory_complete='false')
            facts = LocalFacts(None, Authority(), Supplemental())
            page = await facts.read(node_id=work.node_id, kind='junk')
            from mop.node.http_execution import MachineClient
            class Transport:
                async def request(self, *args, **kwargs): return page
            accepted = await MachineClient(Transport()).facts(work.node_id, 'junk')
            print('invalid source represented explicitly through MachineClient:', accepted, flush=True)
            self.assertFalse(page['complete'], 'invalid fact types must not become fresh/complete observations')
            self.assertEqual(page['items'][0]['state'], 'unavailable')
            self.assertEqual(page['items'][0]['values'], {})
            self.assertTrue(facts.last_error)

    async def test_each_fact_field_has_its_own_type_and_complete_schema(self):
        from copy import deepcopy
        from mop.node.http_execution import MachineClient
        schemas = {
            'states': dict(running=True, dirty=False, session_count=1),
            'clone': dict(clean=True, branch='main', untracked=0, modified=0),
            'sizes': dict(bytes=0),
            'usage': dict(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0),
            'disk': dict(free_bytes=0, total_bytes=1),
            'junk': dict(candidate_count=0, bytes=0, inventory_complete=True),
            'health': dict(healthy=True, latency_ms=0),
        }
        work = Work.parse(wire())
        target = binding(work, Path('/tmp/mop640-fact-types'))
        class Authority:
            async def authorize(self, node_id, kind, limit): return FactScope(node_id, kind, (target,), True)
        class Transport:
            async def request(self, *args, **kwargs): return page
        facts = LocalFacts(None, Authority())
        client = MachineClient(Transport())
        for kind, valid in schemas.items():
            async def values(*args): return valid
            facts._values = values
            page = await facts.read(node_id=work.node_id, kind=kind)
            self.assertTrue((await client.facts(work.node_id, kind))['complete'])
            good = deepcopy(page)
            for field, value in valid.items():
                invalid = ([0, 'false', None] if type(value) is bool else
                           [False, '0', -1, 2**63, None] if type(value) is int else
                           [True, 0, '\x7f', None])
                for bad in invalid:
                    with self.subTest(kind=kind, field=field, bad=bad):
                        page = deepcopy(good)
                        page['items'][0]['values'][field] = bad
                        with self.assertRaises(Refused):
                            await client.facts(work.node_id, kind)
                        malformed = page['items'][0]['values']
                        async def values(*args): return malformed
                        facts._values = values
                        local = await facts.read(node_id=work.node_id, kind=kind)
                        self.assertFalse(local['complete'])
                        self.assertEqual(local['items'][0]['state'], 'unavailable')
                        self.assertEqual(local['items'][0]['values'], {})
            for field in valid:
                page = deepcopy(good)
                del page['items'][0]['values'][field]
                with self.subTest(kind=kind, missing=field), self.assertRaises(Refused):
                    await client.facts(work.node_id, kind)
            for state in ('stale', 'unavailable'):
                page = deepcopy(good)
                page['items'][0]['state'] = state
                with self.subTest(kind=kind, state=state), self.assertRaises(Refused):
                    await client.facts(work.node_id, kind)

    async def test_missing_socket_or_token_preflight_preserves_git_config(self):
        from dataclasses import replace
        runner = ProcessRunner()
        with tempfile.TemporaryDirectory(prefix='mop640-preflight-') as tmp:
            root = Path(tmp)
            work = Work.parse(wire())
            base = binding(work, root)
            await runner.run(('git', 'init', '-q', str(base.clone)))
            config = base.clone/'.git/config'
            before = config.read_bytes()
            socket = root/'session.sock'
            async def receive(reader, writer):
                writer.close()
                await writer.wait_closed()
            server = await asyncio.start_unix_server(receive, path=socket)
            class Resolver:
                async def resolve(self, work): return target, inputs
            async def authorize(): pass
            adapter = LocalExecutor(runner, Resolver())
            try:
                for path, token in ((socket, None), (root/'missing.sock', 'token')):
                    target = replace(base, session_socket=path)
                    inputs = ActionInput('Owner', 'owner@example.test', 'hello', peer_token=token)
                    with self.subTest(path=path, token=token):
                        with self.assertRaises(Unavailable):
                            await adapter.execute(work, EffectGuard(authorize, lambda: None))
                        self.assertEqual(config.read_bytes(), before)
            finally:
                server.close()
                await server.wait_closed()
                await adapter.aclose()


if __name__ == '__main__':
    unittest.main()
