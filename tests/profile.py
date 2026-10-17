#!/usr/bin/env python3
"""HYPOTHESIS: User/роль не означают живой адрес или человеческие полномочия.

SOLUTION: #642 заменяет legacy bus/profile/bundle store на реальный identity DTO.
RESULT: direct commandlet использует тот же HTTP handler, не git/bus context;
red старого reader проверен в control_consumers до изменения командлета.
STATUS: FIXED — see #642 (profile consumer, не весь scope задачи).
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

import hermetic  # noqa: F401
from _lib import run_command
from _control_fixture import Fixture, Response, NODE, OP, TOKEN
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop import cli
from mop.cli import _control
from mop.cli.core import profile
from mop.client import control as api


class Profiles(unittest.TestCase):
    def test_actual_server_actor_not_local_branch_or_role_bundle(self):
        # #684: вид Actor из vocabulary human/agent/service; parallel
        # principal_id в ответе больше нет.
        for kind, grant_id in (('human', None), ('service', OP)):
            with self.subTest(kind=kind):
                fixture = Fixture(Response(200, {'schema_version': 1, 'user_id': 2,
                    'kind': kind, 'human_owner_id': 2 if kind == 'human' else None,
                    'node_id': None, 'grant_id': grant_id}))
                with patch.object(api, '_DeadlineHTTPSConnection', fixture.factory), \
                     _control.use(api.ServerContext('https://controller.example', TOKEN)):
                    out, err, code = run_command(profile.main, [])
                self.assertEqual(code, 0)
                value = json.loads(out)
                self.assertEqual(value['kind'], kind)
                self.assertEqual(value['grant_id'], grant_id)
                self.assertIsNone(value['action_permissions'])
                self.assertFalse({'token', 'address', 'role', 'branch'} & value.keys())
                self.assertNotIn(TOKEN, out+err)
                self.assertEqual(fixture.calls[0][:2], ('GET', '/api/v1/identity'))

    def test_profile_declaration_does_not_grant_legacy_management_verbs(self):
        declaration = cli.declared(profile.__file__)
        self.assertEqual(declaration['annotations'], 'readonly')
        self.assertEqual(declaration['args'], [])
        self.assertEqual(declaration['actions'], [])
        self.assertFalse({'node_verbs', 'cluster_verbs', 'dispatch'} & declaration.keys())


if __name__ == '__main__':
    unittest.main()
