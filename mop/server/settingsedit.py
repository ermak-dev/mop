"""Точечное изменение файла настроек установки (#630)."""
import fcntl
import os
import re
import stat
import threading

from mop.common import fsutil

_LOCK = threading.Lock()
_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


def patch(text, name, value, known):
    """Новый текст .env: только один объявленный ключ, без изменения соседей."""
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name not in known:
        raise ValueError("unknown or read-only installation setting")
    if not isinstance(value, str) or any(c in value for c in "\r\n\0"):
        raise ValueError("setting values must occupy one line")
    if name == "MOP_SOUL_ROLES" and value not in ("", "1"):
        raise ValueError("the role switch accepts only 1 or an empty value")
    if fsutil.read_kv(f"{name}={value}\n").get(name) != value:
        raise ValueError("setting value cannot be represented exactly in this file format")
    lines, found = [], False
    for line in text.splitlines(keepends=True):
        if name in fsutil.read_kv(line):
            if not found:
                lines.append(f"{name}={value}\n")
                found = True
        else:
            lines.append(line)
    if not found:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines.append("\n")
        lines.append(f"{name}={value}\n")
    return "".join(lines)


def set_value(path, name, value, known, lock_path):
    """Атомарная read/modify/write под межпроцессным и локальным замками."""
    patch("", name, value, known)
    with _LOCK, open(lock_path, "a+", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r", encoding="utf-8", newline="") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o444:
                    raise ValueError("installation settings must be a readable regular file")
                original = stream.read()
            changed = patch(original, name, value, known)
            if changed != original:
                fsutil.write_private(path, changed)
            return changed != original
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
