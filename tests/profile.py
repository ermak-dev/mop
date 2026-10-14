#!/usr/bin/env python3
"""Профиль мастера как инструмент MCP без запуска второго сервера (#442)."""
import os
import sys

import hermetic  # noqa: F401,E402
from _lib import Checks, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import cli  # noqa: E402
from mop.common import bus, context  # noqa: E402


def main():
    c = Checks()
    path = os.path.join(cli.PACKAGE, "core", "profile.py")
    # HYPOTHESIS: проверка профиля есть только в режиме mop mcp --check,
    # который создаёт отдельный процесс вместо вызова действующего MCP.
    # SOLUTION: объявленный командлет с тем же правилом классификации.
    if not c.check("#442 a profile commandlet exists", os.path.isfile(path)):
        return c.report("profile")
    from mop.cli.core import profile
    decl = cli.declared(path)
    c.expect("#442 profile is exposed read-only", decl.get("annotations") if decl else None,
             "readonly")
    c.expect("#442 node identity", profile.profile_name(False, "cloudpub"), "node")
    c.expect("#442 operator identity", profile.profile_name(True, bus.ADMIN), "operator")
    c.expect("#442 master identity", profile.profile_name(True, "cloudpub"),
             "master of project cloudpub")
    with patched(bus, config=lambda: {"user": "anton"}, PROJECT="cloudpub"):
        with context.use(context.Context(branch="integration/test")):
            text, _, code = run_command(profile.main, [])
    c.expect("#442 command succeeds", code, 0)
    c.expect("#442 profile and effective branch", text.strip(),
             "profile master of project cloudpub; integration branch integration/test")
    with patched(bus, config=lambda: {"user": "anton"}, PROJECT="cloudpub"):
        with context.use(context.Context()):
            text, _, _ = run_command(profile.main, [])
    c.expect("#442 no branch is not origin HEAD", text.strip(),
             "profile master of project cloudpub; integration branch not set")

    # HYPOTHESIS (#585): роль-принципал (grant-<id>) разбирается той же
    # проверкой «не puppet-<проект>», что и человек, и mop profile
    # называет её «master of project» -- а её права из бандла, и хозяин у
    # неё другой. Мастер-роль диспетчерит, рабочая -- принимает: слово
    # «master» здесь ложь дважды.
    # SOLUTION: профиль по виду говорящего (channel.actor): грант --
    # «role <имя роли бандла>», имя -- из MOP_SOUL_DIR, без бандла -- «role
    # unknown (no bundle)»: заявлять роль, которой тело не подтвердило,
    # нельзя. Человек и узел -- прежние строки байт в байт.
    from mop.client import channel
    GRANT = "grant-" + "a" * 32
    import tempfile
    import json as _json
    import uuid
    from mop import driver
    from mop.common import soul as souls
    # Честное тело (#585): бандл -- под инкарнацией своего джоба в
    # Published-раскладке (driver.soul_dir), у корня -- расписка
    # готовности; самообъявленный MOP_SOUL_DIR больше роли не открывает.
    tmp = tempfile.mkdtemp(prefix="mop-test-profile-585-")
    job = "pu-test-585-1"
    inc = str(uuid.uuid4())
    # Дом патчим ДО построения путей: driver.soul_dir считает от него, и
    # каталог, построенный по старому дому, в новом доме не стоит.
    with patched(driver, HOME=tmp):
        root = driver.soul_dir(job, inc)
        bundle = os.path.join(root, driver.SOUL_BUNDLE)
        os.makedirs(bundle)
        with open(os.path.join(bundle, "soul.yaml"), "w") as f:
            f.write("soul: worker\nversion: 1\n"
                    "runtime: {driver: any, receives_dispatch: true}\n"
                    "capabilities: {node_verbs: [send, tail], cluster_verbs: [], "
                    "dispatch: false, spawn: []}\n"
                    "secrets: {files: [], vars: []}\n"
                    "prompt: soul.md\n")
        with open(os.path.join(bundle, "soul.md"), "w") as f:
            f.write("prompt\n")
        loaded = souls.load(bundle)
        with open(os.path.join(root, driver.READY), "w") as f:
            _json.dump({"ready": True, "role": loaded.role, "soul_inc": inc,
                        "soul_digest": loaded.digest}, f)
        c.expect("#585 grant is its own actor", channel.actor(GRANT), channel.GRANT)
        with patched(bus, config=lambda: {"user": GRANT}, PROJECT="cloudpub"):
            with patched_env(MOP_SOUL_DIR=bundle, MOP_PUPPET=job):
                text, _, code = run_command(profile.main, [])
    c.expect("#585 a grant is profiled as its bundle's role", text.strip(),
             "profile role worker; integration branch not set")
    with patched(bus, config=lambda: {"user": GRANT}, PROJECT="cloudpub"):
        text, _, _ = run_command(profile.main, [])
    c.expect("#585 a grant without a bundle is not a master either", text.strip(),
             "profile role unknown (no bundle); integration branch not set")
    # RESULT: красный на «master of project» для гранта, зелёный после.
    # STATUS: FIXED — see #585 (после #442)
    return c.report("profile")


if __name__ == "__main__":
    sys.exit(main())
