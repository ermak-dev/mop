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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import project_secrets as ps  # noqa: E402


def main():
    failed = []
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
        if got != want:
            failed.append(f"valid_name({name!r}) -> {got!r}, wanted {want!r}")
    # Выход за клон -- отказ: файл лёг бы мимо клона папета.
    for bad in ("", "/etc/passwd", "../x", "a/../../x", "."):
        try:
            ps.valid_name(bad)
            failed.append(f"valid_name({bad!r}) must be refused")
        except ValueError:
            pass
        except AttributeError:
            failed.append("project_secrets.valid_name is missing")
            break

    for text, want in [("TOKEN=abc", ("TOKEN", "abc")),
                       ('URL="http://x?a=b"', ("URL", "http://x?a=b")),
                       ("EMPTY=", ("EMPTY", ""))]:
        try:
            got = ps.parse_var(text)
        except (ValueError, AttributeError) as e:
            got = e
        if got != want:
            failed.append(f"parse_var({text!r}) -> {got!r}, wanted {want!r}")
    for bad in ("NOEQ", "1X=a", "A-B=c", "K=line\nbreak"):
        try:
            ps.parse_var(bad)
            failed.append(f"parse_var({bad!r}) must be refused")
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
        if [f["name"] for f in got] != [".env", "config/app.json"]:
            failed.append(f"list_files -> {got}")
        path = os.path.join(root, "proj", "files", ".env")
        with open(path, "rb") as f:
            if f.read() != b"A=2\n":
                failed.append("put_file must replace the file")
        if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
            failed.append("a secret file must be 0600")
        if stat.S_IMODE(os.stat(os.path.join(root, "proj")).st_mode) != 0o700:
            failed.append("a project's secrets dir must be 0700")
        # Корень тоже: его листинг -- это какие у пула проекты с секретами.
        if stat.S_IMODE(os.stat(root).st_mode) != 0o700:
            failed.append("the secrets root must be 0700, not what makedirs left")
        if not ps.remove_file(root, "proj", "config/app.json") or \
                ps.remove_file(root, "proj", "config/app.json"):
            failed.append("remove_file must say whether there was such a file")
        if [f["name"] for f in ps.list_files(root, "proj")] != [".env"]:
            failed.append("remove_file must remove the file")

        ps.set_var(root, "proj", "TOKEN", "abc def")
        ps.set_var(root, "proj", "URL", "http://x?a=b")
        ps.set_var(root, "proj", "TOKEN", "new")
        if ps.list_vars(root, "proj") != ["TOKEN", "URL"]:
            failed.append(f"list_vars -> {ps.list_vars(root, 'proj')}")
        with open(os.path.join(root, "proj", "vars.env")) as f:
            if f.read() != "TOKEN=new\nURL=http://x?a=b\n":
                failed.append("vars.env must hold KEY=VALUE lines, sorted, values verbatim")
        if not ps.remove_var(root, "proj", "URL") or ps.remove_var(root, "proj", "URL"):
            failed.append("remove_var must say whether there was such a variable")
        # Слишком большой файл -- отказ до записи: сообщение шины ограничено.
        try:
            ps.put_file(root, "proj", "big", b"x" * (ps.MAX_BYTES + 1))
            failed.append("a file over MAX_BYTES must be refused")
        except ValueError:
            pass
        # Чужой проект не видит этих секретов.
        if ps.list_files(root, "other") or ps.list_vars(root, "other"):
            failed.append("another project must see nothing")
    except AttributeError as e:
        failed.append(f"project_secrets is incomplete: {e}")

    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("project_secrets: FAILED" if failed else "project_secrets: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
