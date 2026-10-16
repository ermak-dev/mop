"""Finite build command и branch pin, без Django и инфраструктуры."""
import _lib
import hermetic  # noqa: F401
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import unittest


# HYPOTHESIS: untyped truthiness/branch значения не должны доходить до clear.
# SOLUTION: finite typed command и общая pure pin validation без subprocess в SQL.
# RESULT: malformed inputs и непроверенные source pins отказывают до эффектов.
# STATUS: FIXED — see #648
class BuildCommands(unittest.TestCase):
    def body(self):
        return dict(schema_version=1, epoch=str(uuid4()), request_id=str(uuid4()), expected_revision='1',
            not_after=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat(), policy_id=str(uuid4()),
            target_nodes=[str(uuid4())], mode='rebuild', force_safety=False)

    def test_unknown_shell_fields_and_truthy_force_are_never_commands(self):
        from mop.server.builds.commands import parse
        for key, value in (('argv',[]), ('shell','x'), ('force_safety','false'), ('mode','custom'), ('target_nodes',[])):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    parse(uuid4(), {**self.body(), key:value})

    def test_every_source_pin_is_validated_before_clear_not_inside_ansible(self):
        from mop.server.builds.commands import validate_source
        validate_source('feature/image', 'a'*40)
        for branch in ('--force','refs/heads/main','../main','a..b','main.lock','x/.hidden','x//y','x@{1}','x\nmain','x/'):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError):
                    validate_source(branch, 'a'*40)
        with self.assertRaises(ValueError):
            validate_source('main', 'moving-head')


if __name__ == '__main__':
    unittest.main()
