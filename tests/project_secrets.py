#!/usr/bin/env python3
"""Проверка хранилища секретов проекта без пула: python3 tests/project_secrets.py

Секреты проекта (#127) -- файлы и переменные, которые мастер кладёт из
рабочей копии на сервер, а папет получает при каждом старте. Здесь --
имена, разбор переменных и само хранилище во временном каталоге; глаголы
по шине и доставка в тело -- только на живом пуле.
"""
import os
import stat
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import project_secrets as ps  # noqa: E402


def main():
    c = Checks()
    # HYPOTHESIS (#127): файл для bootstrap должен был лежать на машине,
    # которая его играет, и с машины мастера его туда не доставить.
    # SOLUTION: хранилище на сервере, правит его сервис по шине.
    # STATUS: FIXED — see #127
    for name, want in [(".env", ".env"), ("config/app.json", "config/app.json"),
                       ("./.providers", ".providers")]:
        try:
            got = ps.valid_name(name)
        except (ValueError, AttributeError) as e:
            got = e
        c.expect(f"valid_name({name!r})", got, want)
    # Выход за клон -- отказ: файл лёг бы мимо клона папета.
    for bad in ("", "/etc/passwd", "../x", "a/../../x", "."):
        try:
            ps.valid_name(bad)
            c.fail(f"valid_name({bad!r}) must be refused")
        except ValueError:
            pass
        except AttributeError:
            c.fail("project_secrets.valid_name is missing")
            break

    for text, want in [("TOKEN=abc", ("TOKEN", "abc")),
                       ('URL="http://x?a=b"', ("URL", "http://x?a=b")),
                       ("EMPTY=", ("EMPTY", ""))]:
        try:
            got = ps.parse_var(text)
        except (ValueError, AttributeError) as e:
            got = e
        c.expect(f"parse_var({text!r})", got, want)
    for bad in ("NOEQ", "1X=a", "A-B=c", "K=line\nbreak"):
        try:
            ps.parse_var(bad)
            c.fail(f"parse_var({bad!r}) must be refused")
        except ValueError:
            pass
        except AttributeError:
            break

    # Корень -- ещё не существующий: его заводит само хранилище, как на сервере.
    root = os.path.join(tempfile.mkdtemp(prefix="mop-test-secrets-"), "project-secrets")
    try:
        ps.put_file(root, "proj", ".env", b"A=1\n")
        ps.put_file(root, "proj", "config/app.json", b"{}")
        ps.put_file(root, "proj", ".env", b"A=2\n")          # замена, не дубль
        got = ps.list_files(root, "proj")
        c.check("list_files", not ([f["name"] for f in got] != [".env", "config/app.json"]),
                got)
        path = os.path.join(root, "proj", "files", ".env")
        with open(path, "rb") as f:
            c.expect("put_file must replace the file", f.read(), b"A=2\n")
        c.expect("a secret file must be 0600", stat.S_IMODE(os.stat(path).st_mode), 0o600)
        c.expect("a project's secrets dir must be 0700",
                 stat.S_IMODE(os.stat(os.path.join(root, "proj")).st_mode), 0o700)
        # Корень тоже: его листинг -- это какие у пула проекты с секретами.
        c.expect("the secrets root must be 0700, not what makedirs left",
                 stat.S_IMODE(os.stat(root).st_mode), 0o700)
        c.check("remove_file must say whether there was such a file",
                not (not ps.remove_file(root, "proj", "config/app.json") or
                     ps.remove_file(root, "proj", "config/app.json")))
        c.expect("remove_file must remove the file",
                 [f["name"] for f in ps.list_files(root, "proj")], [".env"])

        ps.set_var(root, "proj", "TOKEN", "abc def")
        ps.set_var(root, "proj", "URL", "http://x?a=b")
        ps.set_var(root, "proj", "TOKEN", "new")
        c.expect("list_vars", ps.list_vars(root, "proj"), ["TOKEN", "URL"])
        with open(os.path.join(root, "proj", "vars.env")) as f:
            c.expect("vars.env must hold KEY=VALUE lines, sorted, values verbatim",
                     f.read(), "TOKEN=new\nURL=http://x?a=b\n")
        c.check("remove_var must say whether there was such a variable",
                not (not ps.remove_var(root, "proj", "URL") or ps.remove_var(root, "proj", "URL")))
        # Слишком большой файл -- отказ до записи: сообщение шины ограничено.
        try:
            ps.put_file(root, "proj", "big", b"x" * (ps.MAX_BYTES + 1))
            c.fail("a file over MAX_BYTES must be refused")
        except ValueError:
            pass
        # Чужой проект не видит этих секретов.
        c.check("another project must see nothing",
                not (ps.list_files(root, "other") or ps.list_vars(root, "other")))
    except AttributeError as e:
        c.fail("project_secrets is incomplete", e)
    return c.report("project_secrets")


if __name__ == "__main__":
    sys.exit(main())
