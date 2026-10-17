"""#684 offline контракт: identity читается из Actor+CredentialBinding, без Principal.

Django-зависимые проверки выполняет fixture mop dev controller (#678) в
tests/_actor_auth_cases.py; здесь только offline-метаданные контекста и
исходников, которые нельзя проверить поведением без БД.
"""
# RESULT: 4/4 ok: контекст несёт только typed Actor facts; services/grants/
# directory/admin не знают идентификатора Principal; CredentialBinding и
# correlation-колонки без параллельной identity.
# STATUS: FIXED — see #684
import ast
import importlib.util
import unittest
from pathlib import Path

import _lib  # noqa: F401 — общий временный корень процесса
import hermetic  # noqa: F401 — изоляция раньше mop

ROOT = Path(__file__).resolve().parents[1]


def _tree(relative):
    return ast.parse((ROOT / relative).read_bytes())


class OfflineContract(unittest.TestCase):
    """Контекст и binding не несят параллельной identity."""

    def test_actor_context_references_only_typed_actor_facts(self):
        # FEATURE: #684 — ActorContext строится из проверенного Actor и
        # фактических ссылок; parallel principal_id исчезает вместе с
        # kind-маршрутом 'grant' (делегирование — это grant_id, не вид Actor).
        spec = importlib.util.spec_from_file_location(
            'actor_context_offline', ROOT / 'mop/server/access/context.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fields = tuple(field.name for field in module.ActorContext.__dataclass_fields__.values())
        self.assertEqual(fields, ('user_id', 'kind', 'credential_id', 'human_owner_id', 'node_id', 'grant_id'))
        self.assertFalse(any('principal' in name for name in fields), fields)
        kinds = {member.value for member in module.ActorContext.__annotations__['kind']}
        # Ровно vocabulary самого Actor: вид не выбирает систему проверок.
        self.assertEqual(kinds, {'human', 'service', 'agent'})

    def test_resolution_paths_read_no_principal_identifier(self):
        # HYPOTHESIS: пока резолвер идёт через Principal, вид Actor выбирает
        # альтернативную identity; после #684 эти модули не знают слова.
        # SOLUTION: прямой read Actor+CredentialBinding; Principal остаётся
        # только владельцем permission rows до #685 (models/migrations).
        for relative in ('mop/server/access/services.py', 'mop/server/access/grants.py',
                         'mop/server/access/directory.py', 'mop/server/controller/admin.py'):
            with self.subTest(module=relative):
                tree = _tree(relative)
                names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
                self.assertNotIn('Principal', names | attrs)
                self.assertNotIn('_principal', names | attrs)

    def test_credential_binding_declares_only_proven_metadata(self):
        # CredentialBinding хранит только то, чего нет в стандартных Django
        # credentials: generation HMAC, subject-change digest, node enrollment
        # и ссылки identity; дубль атрибутов Actor убран.
        tree = _tree('mop/server/access/models.py')
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'CredentialBinding')
        # Django-модель объявляет поля обычным Assign (не AnnAssign).
        fields = {node.targets[0].id for node in cls.body if isinstance(node, ast.Assign)
                  and isinstance(node.targets[0], ast.Name)}
        for duplicated in ('principal', 'principal_revision', 'principal_kind'):
            self.assertNotIn(duplicated, fields, f'CredentialBinding.{duplicated} дублирует Actor')
        for proven in ('token_digest', 'identity_digest', 'bound_node', 'identity'):
            self.assertIn(proven, fields)

    def test_correlation_columns_reference_actor_ids_only(self):
        # HYPOTHESIS: колонки correlation (lease/attempt/delivery/cursor/
        # terminal/turn) несут параллельный Principal UUID рядом с user id --
        # вторая identity там, где Actor уже назван своим id.
        # SOLUTION: дубли убраны, исполнители ссылаются user_id (int);
        # SourceRef инициатора обходится user/endpoint/correlation ссылками.
        assigned = set()
        for relative in ('mop/server/sessions/models.py', 'mop/server/sessions/endpoint_models.py',
                         'mop/server/sessions/telemetry_models.py', 'mop/server/sessions/terminal_models.py'):
            for node in ast.walk(_tree(relative)):
                if isinstance(node, ast.ClassDef):
                    assigned |= {child.targets[0].id for child in node.body
                                 if isinstance(child, ast.Assign) and isinstance(child.targets[0], ast.Name)}
        self.assertNotIn('principal_id', assigned)
        self.assertNotIn('sender_principal_id', assigned)
        self.assertNotIn('initiator_principal_id', assigned)
        spec = importlib.util.spec_from_file_location(
            'telemetry_offline', ROOT / 'mop/common/telemetry.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertNotIn('principal_id', module.SourceRef.__dataclass_fields__)


if __name__ == '__main__':
    unittest.main()
