"""Оффлайн-контракт #686: программа агента отдельно от identity, без ORM.

Проверяется словарь wire-протокола и доменные имена записей программы:
версия определения программы агента существует под целевым именем из docs/API,
старый bundle-словарь не остаётся второй параллельной номенклатурой, словарь
технических предусловий ACTION_CAPABILITIES сведён к предусловию spawn (#688).
"""
import ast
import unittest
from pathlib import Path

import _lib  # noqa: F401 — общий вход проверок (#338)
import hermetic  # noqa: F401 — до любого mop импорта

ROOT = Path(__file__).resolve().parents[1]


def _tree(relative):
    return ast.parse((ROOT / relative).read_text())


class AgentProgramVocabulary(unittest.TestCase):
    # FEATURE: #686 — AgentProfile + AgentDefinitionVersion вместо RoleBundle.
    # HYPOTHESIS: два параллельных имени одной записи программы (RoleBundle и
    # AgentDefinitionVersion) размывают границу identity/программы.
    # SOLUTION: целевое имя из docs/API одним переименованием семейства;
    # wire-словарь переименован когерентно, протокол эпика не выпущен.
    # RED: отсутствие AgentDefinitionVersion/AgentProfile и старые wire-имена.
    def test_definition_family_replaces_bundle_vocabulary_in_models(self):
        source = _tree('mop/server/access/models.py')
        classes = {node.name for node in ast.walk(source) if isinstance(node, ast.ClassDef)}
        for expected in ('AgentProfile', 'AgentDefinitionVersion', 'AgentDefinitionSeal',
                         'AgentDefinitionApproval', 'DefinitionCapability', 'DefinitionSpawn',
                         'DefinitionSecretName'):
            self.assertIn(expected, classes, f'domain record {expected} is missing')
        for stale in ('RoleBundle', 'RoleBundleSeal', 'RoleBundleApproval', 'RoleCapability',
                      'RoleSpawn', 'RoleSecretName'):
            self.assertNotIn(stale, classes, f'stale bundle-era record {stale} is still defined')

    def test_wire_protocol_names_definitions_without_bundle_commands(self):
        source = _tree('mop/common/credentials.py')
        classes = {node.name for node in ast.walk(source) if isinstance(node, ast.ClassDef)}
        self.assertIn('DefinitionInfo', classes, 'definition metadata DTO is missing')
        self.assertIn('DefinitionBytes', classes, 'definition bytes DTO is missing')
        self.assertNotIn('BundleInfo', classes, 'stale bundle DTO survives in the wire protocol')
        self.assertNotIn('BundleBytes', classes, 'stale bundle DTO survives in the wire protocol')
        text = (ROOT / 'mop/common/credentials.py').read_text()
        for command in ("'publish_definition'", "'read_definition'", "'approve_definition'"):
            self.assertIn(command, text, f'wire command {command} is missing')
        for stale in ("'publish_bundle'", "'read_bundle'", "'approve_bundle'", "'bundle_id'"):
            self.assertNotIn(stale, text, f'stale bundle wire token {stale} survives')
        self.assertIn("'definition_id'", text, 'definition reference argument is missing')

    # FEATURE: #688 — словарь сведён к техническим предусловиям программы.
    # HYPOTHESIS: action→verb словарь работал вторым каталогом прав рядом с
    # Django RBAC #685: делегированное действие требовало verb манифеста.
    # SOLUTION: права решает только RBAC; в словаре осталось лишь предусловие
    # spawn (программа объявила cluster add), dispatch/send/landing ушли.
    # RED: словарь держал 16 действий (на базе 59f70c0f эта проверка уже
    # падала: она ждала прежние sessions.* имена до #685).
    def test_capability_map_is_reduced_to_technical_preconditions(self):
        source = _tree('mop/server/access/delegation.py')
        for node in ast.walk(source):
            if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == 'ACTION_CAPABILITIES'
                    for target in node.targets):
                keys = {element.value for element in node.value.keys}
                self.assertEqual(keys, {'execution.create_puppet'},
                    'capability map is a second permission catalog again')
                break
        else:
            self.fail('capability precondition map is missing')

    def test_access_package_reads_no_legacy_registry_files(self):
        # Новая схема не читает старые bundle/registry файлы: файловый ввод
        # остаётся за legacy-машиной, access работает только с ORM.
        for path in sorted((ROOT / 'mop/server/access').glob('*.py')):
            for node in ast.walk(_tree(path.relative_to(ROOT))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'open':
                    self.fail(f'{path.name} performs a legacy file read')


if __name__ == '__main__':
    unittest.main()
