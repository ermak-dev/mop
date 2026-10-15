"""Явный операторский снос снятого host-папета (#631)."""
import os
import shutil
import subprocess

from mop import driver


def absent(name):
    """Отсутствие tmux подтверждается ответом, а не любым ненулевым кодом."""
    uid = os.getuid()
    roots = {f"/tmp/tmux-{uid}", f"/run/user/{uid}/tmux-{uid}"}
    temporary = os.environ.get("TMUX_TMPDIR")
    if temporary:
        roots.update((temporary, os.path.join(temporary, f"tmux-{uid}")))
    for root in sorted(roots):
        reply = subprocess.run(["tmux", "-S", os.path.join(root, name), "has-session"],
                               capture_output=True, text=True, timeout=10)
        if reply.returncode == 0:
            return False
        why = reply.stderr.strip()
        missing = reply.returncode == 1 and (
            why.startswith("no server running on ")
            or (why.startswith("error connecting to ")
                and why.endswith("(No such file or directory)")))
        if not missing:
            raise RuntimeError(f"cannot prove that the puppet session is absent: {why or reply.returncode}")
    return True


def remove(name, orphan=False):
    """Удалённые пути; оператор подтвердил снятие job на контроллере."""
    if not orphan:
        raise ValueError("explicit --orphan confirmation is required after deleting the job")
    if not driver.valid_name(name):
        raise ValueError("not a puppet name")
    if getattr(driver.current(), "IS_CONTAINER", None) is not False:
        raise ValueError("purge-clone is only for the host driver")
    home = os.path.realpath(driver.HOME)
    if home == os.path.sep:
        raise ValueError("the filesystem root cannot be the puppet home")
    targets = (driver.clone_dir(name), driver.target_dir(name))
    expected = (name, "target-" + name)
    for path, leaf in zip(targets, expected):
        real = os.path.realpath(path)
        if (os.path.islink(path) or os.path.basename(path) != leaf
                or real == home or os.path.commonpath((home, real)) != home):
            raise ValueError("purge target is not a safe named path inside the node home")
        if os.path.lexists(path) and not os.path.isdir(path):
            raise ValueError("purge target is not a directory")
    if not absent(name):
        raise ValueError("the puppet still has a live tmux session")
    removed = []
    for path in targets:
        if os.path.exists(path):
            shutil.rmtree(path)
            removed.append(path)
    return removed
