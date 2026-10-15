"""Точечная запись настроек контроллера (#630).

HYPOTHESIS: server config не умеет менять настройку без внешнего редактора.
SOLUTION: чистое изменение одного ключа и атомарная запись под замком.
STATUS: FIXED — see #630.
"""
import os
import tempfile
import threading
from pathlib import Path

import hermetic
from _lib import Checks, patched, run_command
from mop.common import config
from mop.cli.server import config as command


def main():
    c = Checks()
    try:
        from mop.server import settingsedit as edit
    except ImportError:
        c.check("#630 installation settings have a supported writer", False)
        return c.report("settingsedit")
    known = {"MOP_SOUL_ROLES", "MOP_WEB_PORT"}
    original = "# keep\nPRIVATE_KEY=fixture-private\nMOP_SOUL_ROLES=\n# tail\n"
    wanted = original.replace("MOP_SOUL_ROLES=\n", "MOP_SOUL_ROLES=1\n")
    c.expect("#630 change one setting and no other bytes",
             edit.patch(original, "MOP_SOUL_ROLES", "1", known), wanted)
    c.expect("#630 append without damaging a final unterminated line",
             edit.patch("PRIVATE_KEY=fixture", "MOP_SOUL_ROLES", "1", known),
             "PRIVATE_KEY=fixture\nMOP_SOUL_ROLES=1\n")
    c.expect("#630 duplicate keys become one declaration",
             edit.patch("MOP_SOUL_ROLES=\nOTHER=x\n MOP_SOUL_ROLES = 1\n",
                        "MOP_SOUL_ROLES", "", known),
             "MOP_SOUL_ROLES=\nOTHER=x\n")
    for key, value in (("UNKNOWN", "fixture-private"), ("MOP_SOUL_ROLES", "0"),
                       ("MOP_SOUL_ROLES", "1\nINJECTED=fixture-private"),
                       ("MOP_WEB_PORT", " 9000"), ("MOP_WEB_PORT", "\x00")):
        try:
            edit.patch(original, key, value, known)
            c.check("#630 reject an invalid setting edit", False, key)
        except ValueError as error:
            c.check("#630 refusal reveals no proposed value",
                    "fixture-private" not in str(error), str(error))
    with tempfile.TemporaryDirectory(prefix="mop-settings-630-") as root:
        env = Path(root, ".env")
        lock = Path(root, "settings.lock")
        env.write_text(original)
        edit.set_value(str(env), "MOP_SOUL_ROLES", "1", known, str(lock))
        c.expect("#630 write the expected settings", env.read_text(), wanted)
        c.expect("#630 the settings remain private", env.stat().st_mode & 0o777, 0o600)
        link = Path(root, "link.env")
        link.symlink_to(env)
        try:
            edit.set_value(str(link), "MOP_SOUL_ROLES", "", known, str(lock))
            c.check("#630 do not replace a symlink", False)
        except (OSError, ValueError):
            c.check("#630 symlink target unchanged", env.read_text() == wanted)
        env.chmod(0)
        try:
            edit.set_value(str(env), "MOP_SOUL_ROLES", "", known, str(lock))
            c.check("#630 do not overwrite an unreadable existing file", False)
        except (OSError, ValueError):
            pass
        finally:
            env.chmod(0o600)
        edit.set_value(str(env), "MOP_SOUL_ROLES", "", known, str(lock))
        errors = []
        def write(key, value):
            try:
                edit.set_value(str(env), key, value, known, str(lock))
            except Exception as error:
                errors.append(type(error).__name__)
        threads = [threading.Thread(target=write, args=(key, value))
                   for key, value in (("MOP_SOUL_ROLES", "1"), ("MOP_WEB_PORT", "9001"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        c.check("#630 concurrent edits keep both settings",
                not errors and not any(t.is_alive() for t in threads)
                and "MOP_SOUL_ROLES=1\n" in env.read_text()
                and "MOP_WEB_PORT=9001\n" in env.read_text())
        with patched(config, ENV_FILE=str(env)), \
             patched(command, require_controller=lambda: None), \
             patched(command, lock_file=lambda: str(lock)):
            out, err, code = run_command(command.main,
                            ["set", "MOP_SOUL_ROLES", ""], via_cli=True)
        c.check("#630 the setter is quiet and succeeds", code == 0 and not out and not err,
                (code, out, err))
        c.check("#630 the command changes the requested setting",
                "MOP_SOUL_ROLES=\n" in env.read_text())
        before = env.read_bytes()
        def no_controller():
            raise RuntimeError("server maintenance requires the controller")
        with patched(config, ENV_FILE=str(env)), \
             patched(command, require_controller=no_controller):
            out, err, code = run_command(command.main,
                            ["set", "MOP_SOUL_ROLES", "1"], via_cli=True)
        c.check("#630 a client cannot edit controller settings",
                code != 0 and "controller" in err and env.read_bytes() == before,
                (code, out, err))
    return c.report("settingsedit")


if __name__ == "__main__":
    raise SystemExit(main())
