#!/usr/bin/env python3
"""Идентичность проекта и значения лимита до записи каталога (#634).

HYPOTHESIS: Django-модель сама не делает project_of единственным правилом
имени и может сохранить credential-bearing origin или bool вместо лимита.
SOLUTION: одна чистая валидация входа, общий driver.project_of и distinct
None/zero/positive для лимита; конкурентную уникальность охраняет БД.
RESULT: новый API отсутствовал (красный прогон), затем отдельные сценарии
remote-helper/credential URL дали 3/82 fail; после исправления проверки зелёные.
Уникальность и CAS остаются отдельными проверками PostgreSQL.
STATUS: FIXED — see #634
"""
import importlib
import importlib.util
import os
import subprocess
import sys
import textwrap
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import hermetic  # noqa: F401
from _lib import Checks

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def import_without_legacy_settings(c):
    # HYPOTHESIS: policy импортирует driver ради project_of, но его HOME
    # при импорте читает node.env/.env даже без обращения к путям тела (#634).
    # Свежий интерпретатор обязателен: уже загруженный driver скрывает дефект.
    # SOLUTION: driver разрешает строки путей только при явном доступе;
    # project_of остаётся владельцем идентичности и точкой подмены policy.
    # RESULT: оба fresh-import сценария падали на HOME=config.get, теперь
    # проходят; настроенные пути и подмены атрибутов также сохранены.
    # STATUS: FIXED — see #634
    code = textwrap.dedent('''
        import sys
        from unittest.mock import patch
        from mop.common import config

        assert 'mop.driver' not in sys.modules
        def refuse(*args, **kwargs):
            raise AssertionError('catalogue identity accessed legacy settings')

        with patch.object(config, 'get', side_effect=refuse), \\
             patch.object(config, '_file', side_effect=refuse), \\
             patch.object(config, 'read_env', side_effect=refuse), \\
             patch.object(config, '_load', side_effect=refuse), \\
             patch.object(config, '_node', side_effect=refuse):
            if sys.argv[1] == 'services':
                from django.conf import settings
                settings.configure(INSTALLED_APPS=['mop.server.controller', 'mop.server.catalog'],
                                   DATABASES={}, DEFAULT_AUTO_FIELD='django.db.models.BigAutoField')
                import django
                django.setup()
                from mop.server.catalog.services import Catalog
                Catalog()
            from mop.server.catalog import policy
            from mop import driver
            origin = 'git@git.example.test:team/repository.git'
            assert driver.project_of(origin) == 'repository'
            assert driver.parse_origin(origin)[4] == 'team/repository'
            assert driver.project_of_name('pu-repository-1') == 'repository'
            assert driver.valid_name('pu-repository-1')
            assert policy.project_origin(origin).name == 'repository'
            with patch.object(driver, 'project_of', return_value='canonical') as derive:
                assert policy.project_origin(origin).name == 'canonical'
                derive.assert_called_once_with(origin)

        # Явный доступ к старым путям обязан брать настроенный дом, не дефолт.
        with patch.object(config, 'get', return_value='/configured/body') as get:
            assert driver.HOME == '/configured/body'
            assert driver.SECRETS_FILE == '/configured/body/.config/mop/secrets.env'
            assert driver.SERVER_PUB == '/configured/body/.config/mop/bootstrap.pub'
            from mop.driver import HOME, SECRETS_FILE, SERVER_PUB
            assert (HOME, SECRETS_FILE, SERVER_PUB) == (
                '/configured/body', '/configured/body/.config/mop/secrets.env',
                '/configured/body/.config/mop/bootstrap.pub')
            assert driver.clone_dir('pu-r-1') == '/configured/body/puppets/pu-r-1'
            assert driver.target_dir('pu-r-1') == '/configured/body/.cache/target-pu-r-1'
            assert driver.project_creds('r') == '/configured/body/.config/mop/bus-r.json'
            assert driver.soul_dir('pu-r-1', '2') == '/configured/body/.config/mop/souls/pu-r-1/2'
            assert driver.project_secrets_dir('r') == '/configured/body/.config/mop/project-secrets/r'
            assert get.call_args_list
            assert all(call.args == ('MOP_HOME',) for call in get.call_args_list)
            with patch.object(driver, 'HOME', '/patched/body'):
                assert driver.clone_dir('pu-r-1') == '/patched/body/puppets/pu-r-1'
                assert driver.SECRETS_FILE == '/patched/body/.config/mop/secrets.env'
                assert driver.SERVER_PUB == '/patched/body/.config/mop/bootstrap.pub'
            with patch.object(driver, 'SECRETS_FILE', '/patched/secrets'), \\
                 patch.object(driver, 'SERVER_PUB', '/patched/key'):
                assert driver.SECRETS_FILE == '/patched/secrets'
                assert driver.SERVER_PUB == '/patched/key'
        assert driver.project_of(origin) == 'repository'
    ''')
    for mode in ('policy', 'services'):
        if mode == 'services' and importlib.util.find_spec('django') is None:
            hermetic.skip('catalogue services import', 'declared server extra is not installed')
            continue
        result = subprocess.run([sys.executable, '-c', code, mode],
                                cwd=os.path.dirname(os.path.dirname(__file__)),
                                capture_output=True, text=True)
        c.check(f'fresh {mode} import and identity avoid legacy settings; runtime paths remain configured',
                result.returncode == 0, result.stdout + result.stderr)


def main():
    c = Checks()
    import_without_legacy_settings(c)
    try:
        present = importlib.util.find_spec("mop.server.catalog.policy") is not None
    except ModuleNotFoundError:
        present = False
    if not c.check("catalogue input policy is available", present):
        return c.report("catalog_policy")
    policy = importlib.import_module("mop.server.catalog.policy")
    from mop import driver

    examples = (
        ("git@git.example.test:team/repository.git", "repository"),
        ("git@git.example.test:repository.git", "repository"),
        ("ssh://git@git.example.test:2222/team/repository.git", "repository"),
        ("https://git.example.test/team/repository.git/", "repository"),
        ("git://git.example.test/team/repository.git", "repository"),
        ("/srv/git/team/repository.git", "repository"),
        ("file:///srv/git/team/repository.git", "repository"),
        ("../team/repository.git", "repository"),
        ("https://git.example.test/team/repository-name_2.3.git", "repository-name_2.3"),
    )
    for origin, name in examples:
        got = policy.project_origin(f"  {origin}  ")
        c.expect(f"trim accepted origin {origin}", got.origin, origin)
        c.expect(f"one identity rule for {origin}", got.name, name)
        c.expect(f"driver agrees for {origin}", got.name, driver.project_of(origin))
        try:
            got.name = "other"
        except FrozenInstanceError:
            c.check("validated origin is immutable", True)
        else:
            c.fail("validated origin is immutable")
    with patch.object(driver, "project_of", return_value="canonical") as derive:
        c.expect("policy does not invent another name algorithm",
                 policy.project_origin(examples[0][0]).name, "canonical")
        c.expect("the origin goes through driver.project_of", derive.call_count, 1)

    def refuses(what, fn, forbidden=()):
        try:
            fn()
        except ValueError as error:
            c.check(what, bool(str(error)))
            for secret in forbidden:
                c.check(f"{what} does not echo credentials", secret not in str(error))
        else:
            c.fail(what, "input was accepted")

    for bad in (None, True, 1, "", "repo", "https://", "git@host:", ":repo.git",
                "https://git.example.test/", "-local/repo.git", "ext::sh -c command",
                "ext::/bin/true", "helper::/tmp/program/repo.git",
                "https://git.example.test/a/repo name.git", "https://git.example.test/r.git\nnext",
                "https://git.example.test/r.git\x00", "https://git.example.test/" + "a" * 2100,
                "https://git.example.test/" + "x" * 129 + ".git",
                "https://git.example.test/.hidden.git", "https://git.example.test/../.git"):
        refuses(f"invalid origin {type(bad).__name__}/{str(bad)[:12]}",
                lambda bad=bad: policy.project_origin(bad))
    for origin in (
        "https://secret-token@git.example.test/team/r.git",
        "https://user:secret-token@git.example.test/team/r.git",
        "ssh://git:secret-token@git.example.test/team/r.git",
        "file://secret-token@localhost/srv/git/r.git",
        "https://git.example.test/team/r.git?access_token=secret-token",
        "https://git.example.test/team/r.git#secret-token",
    ):
        refuses("credential-bearing origin refuses safely",
                lambda origin=origin: policy.project_origin(origin), ("secret-token",))

    for valid in (None, 0, 1, 100):
        c.expect(f"limit {valid!r} retains its meaning", policy.project_limit(valid), valid)
    for bad in (True, False, -1, 1.5, "0", "none", [], {}):
        refuses(f"invalid project limit {bad!r}", lambda bad=bad: policy.project_limit(bad))
    return c.report("catalog_policy")


if __name__ == "__main__":
    sys.exit(main())
