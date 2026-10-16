#!/usr/bin/env python3
"""HYPOTHESIS: User/роль не означают живой адрес или человеческие полномочия.

SOLUTION: #642 заменяет legacy bus/profile/bundle store на реальный identity DTO.
RESULT: direct commandlet использует тот же HTTP handler, не git/bus context;
red старого reader проверен в control_consumers до изменения командлета.
STATUS: FIXED — see #642 (profile consumer, не весь scope задачи).
"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

import hermetic  # noqa: F401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop import cli
from mop.cli import _control
from mop.cli.core import profile
from mop.client import control as api
from control_client import Fixture, Response
from control_consumers import NODE, OP, TOKEN


class Profiles(unittest.TestCase):
    def test_actual_server_principal_not_local_branch_or_role_bundle(self):
        for kind, grant_id in (('human', None), ('grant', OP)):
            with self.subTest(kind=kind):
                fixture = Fixture(Response(200, {'schema_version': 1, 'user_id': 2,
                    'principal_id': NODE, 'kind': kind, 'human_owner_id': 2,
                    'node_id': None, 'grant_id': grant_id}))
                out, err = io.StringIO(), io.StringIO()
                with patch.object(api, '_DeadlineHTTPSConnection', fixture.factory), \
                     _control.use(api.ServerContext('https://controller.example', TOKEN)), \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(profile.main([]), 0)
                value = json.loads(out.getvalue())
                self.assertEqual(value['kind'], kind)
                self.assertEqual(value['principal_id'], NODE)
                self.assertEqual(value['grant_id'], grant_id)
                self.assertIsNone(value['action_permissions'])
                self.assertFalse({'token', 'address', 'role', 'branch'} & value.keys())
                self.assertNotIn(TOKEN, out.getvalue()+err.getvalue())
                self.assertEqual(fixture.calls[0][:2], ('GET', '/api/v1/identity'))

    def test_profile_declaration_does_not_grant_legacy_management_verbs(self):
        declaration = cli.declared(profile.__file__)
        self.assertEqual(declaration['annotations'], 'readonly')
        self.assertEqual(declaration['args'], [])
        self.assertEqual(declaration['actions'], [])
        self.assertFalse({'node_verbs', 'cluster_verbs', 'dispatch'} & declaration.keys())


if __name__ == '__main__':
    unittest.main()
