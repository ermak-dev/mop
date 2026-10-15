"""Явное удаление host-клона снятого папета (#631).

HYPOTHESIS: штатный delete оставляет host-клон даже при разрешённом полном сбросе.
SOLUTION: отдельная узловая команда с подтверждением и проверкой сессии.
STATUS: FIXED — see #631.
"""
import tempfile
from pathlib import Path
from types import SimpleNamespace

import hermetic
from _lib import Checks, patched
from mop import driver


def main():
    c = Checks()
    try:
        from mop.driver import _purgeclone as purge
    except ImportError:
        c.check("#631 explicit orphan clone cleanup is supported", False)
        return c.report("purgeclone")
    name = "pu-example-1"
    with tempfile.TemporaryDirectory(prefix="mop-purge-631-") as root:
        home = Path(root, "home")
        home.mkdir()
        with patched(driver, HOME=str(home), current=lambda: SimpleNamespace(IS_CONTAINER=False)), \
             patched(purge, absent=lambda n: True):
            clone, target = Path(driver.clone_dir(name)), Path(driver.target_dir(name))
            other = Path(driver.clone_dir("pu-example-2"))
            for path in (clone, target, other):
                path.mkdir(parents=True)
                (path / "keep").write_text("fixture")
            for bad in ("../", "", "pu-example-1/../../home"):
                try:
                    purge.remove(bad, orphan=True)
                    c.check("#631 invalid name is refused", False, bad)
                except ValueError:
                    c.check("#631 invalid name changes nothing", clone.exists() and target.exists())
            try:
                purge.remove(name, orphan=False)
                c.check("#631 an explicit orphan confirmation is required", False)
            except ValueError:
                pass
            with patched(purge, absent=lambda n: False):
                try:
                    purge.remove(name, orphan=True)
                    c.check("#631 a live session is refused", False)
                except ValueError:
                    c.check("#631 live session data remains", clone.exists() and target.exists())
            def unknown(_):
                raise RuntimeError("fixture probe failed")
            with patched(purge, absent=unknown):
                try:
                    purge.remove(name, orphan=True)
                    c.check("#631 an unknown session probe is refused", False)
                except RuntimeError:
                    c.check("#631 failed probe changes nothing", clone.exists() and target.exists())
            with patched(driver, current=lambda: SimpleNamespace(IS_CONTAINER=True)):
                try:
                    purge.remove(name, orphan=True)
                    c.check("#631 container driver cannot purge host paths", False)
                except ValueError:
                    pass
            outside = Path(root, "outside")
            outside.mkdir()
            saved_target = target.with_name("saved-target")
            target.rename(saved_target)
            target.symlink_to(outside, target_is_directory=True)
            try:
                purge.remove(name, orphan=True)
                c.check("#631 symlink roots are refused before deleting either path", False)
            except ValueError:
                c.check("#631 a symlink refusal preserves the clone", clone.exists())
            target.unlink()
            saved_target.rename(target)
            with patched(driver, clone_dir=lambda n: str(home)):
                try:
                    purge.remove(name, orphan=True)
                    c.check("#631 HOME is never a purge target", False)
                except ValueError:
                    pass
            result = purge.remove(name, orphan=True)
            c.check("#631 remove exactly the named clone and target",
                    not clone.exists() and not target.exists() and other.exists(), result)
            c.check("#631 an already absent orphan is idempotent",
                    purge.remove(name, orphan=True) == [])
    return c.report("purgeclone")


if __name__ == "__main__":
    raise SystemExit(main())
