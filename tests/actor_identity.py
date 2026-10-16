"""Unified Actor identity #683: один AUTH_USER_MODEL, kind только классифицирует.

Django-зависимые проверки выполняются fixture'ом mop dev controller (#678)
в tests/_actor_cases.py; этот файл держит offline-контракт метаданных.

HYPOTHESIS: единственный субъект требует отдельное app раньше domain apps,
иначе AUTH_USER_MODEL не резолвится до swappable FK; whitelist schema/CLI
должен принимать actor, не отказывая как unknown app.
SOLUTION: mop.server.actor с AbstractUser, INSTALLED_APPS порядок,
schema/CLI whitelist, логический auth.* namespace в permission_names.
RED: все три случая падали утверждениями (app отсутсутствовал, строка
AUTH_USER_MODEL отсутствовала, whitelist не содержал 'actor').
RESULT: offline 3/3 green; PG17 fixture _actor_cases 6/6 green; полный
controller gate green на ветке.
STATUS: FIXED — see #683
"""
import ast
import importlib.util
from pathlib import Path
import unittest

import _lib  # noqa: F401 — общий временный корень процесса
import hermetic  # noqa: F401 — изоляция раньше mop

ROOT = Path(__file__).resolve().parents[1]


def _settings_list(name):
    """Список INSTALLED_APPS из controller settings, без bootstrap/БД."""
    tree = ast.parse((ROOT / 'mop/server/controller/settings.py').read_bytes())
    assign = next(node for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == name
                          for target in node.targets))
    return [element.value for element in assign.value.elts]


def _fixture():
    """Общий setUp: серверный extra обязателен, иначе честный skip."""
    if importlib.util.find_spec('django') is None:
        hermetic.skip('unified actor schema', 'declared server extra is required')
        return None
    from mop.server.controller.runtime import configure
    configure()
    return True


class OfflineContract(unittest.TestCase):
    """Метаданные приложения не требуют ни bootstrap, ни БД."""

    def test_actor_app_is_installed_before_every_domain_app(self):
        # Actor должен существовать раньше любой swappable FK на него:
        # INSTALLED_APPS порядок определяет резолв settings.AUTH_USER_MODEL
        # и app label владельца у action permissions.
        apps = _settings_list('INSTALLED_APPS')
        self.assertIn('mop.server.actor', apps)
        last_std = max(apps.index(name) for name in
                       ('django.contrib.auth', 'django.contrib.contenttypes'))
        # domain apps — модели со swappable FK на Actor; actor обязан
        # регаться раньше их, чтобы AUTH_USER_MODEL резолвился. Controller
        # foundation и Admin config моделей с FK на Actor не держат.
        domain_apps = [name for name in apps if name.startswith('mop.server.')
                       and name not in ('mop.server.actor',
                                        'mop.server.controller.apps.ControllerConfig',
                                        'mop.server.controller.adminapps.ControllerAdminConfig')]
        self.assertGreater(apps.index('mop.server.actor'), last_std)
        self.assertLess(apps.index('mop.server.actor'), min(apps.index(name) for name in domain_apps))

    def test_auth_user_model_points_at_the_single_actor(self):
        # AST, не импорт: settings требует protected bootstrap (#632).
        source = (ROOT / 'mop/server/controller/settings.py').read_text()
        self.assertIn("AUTH_USER_MODEL = 'actor.Actor'", source)
        tree = ast.parse((ROOT / 'mop/server/actor/models.py').read_bytes())
        cls = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == 'Actor')
        bases = [base.id for base in cls.bases if isinstance(base, ast.Name)]
        self.assertEqual(bases, ['AbstractUser'])
        # kind объявлен, но не добавляет ни permission, ни manager-маршрут.
        fields = {node.targets[0].id: node for node in cls.body
                  if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
        self.assertIn('kind', fields)

    def test_offline_schema_app_whitelist_accepts_actor(self):
        source = (ROOT / 'mop/server/controller/schema.py').read_text()
        self.assertIn("'actor'", source)


if __name__ == '__main__':
    unittest.main()
