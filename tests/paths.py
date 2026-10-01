#!/usr/bin/env python3
"""Пути mop без пула: python3 tests/paths.py

Белый список глагола `write` (#279): файл называется относительно дома
пула, а дом подставляет узел -- единственный, кто его знает.

HYPOTHESIS (#279): `mop login` шлёт узлу абсолютный путь, собранный из
MOP_HOME ЛОКАЛЬНОЙ установки оператора (mate: /home/ermak), а агент сверяет
его со своим домом (rumop: /home/mop) -- и на чужой установке каждый узел
отвечает «agent is not allowed to write to /home/ermak/...». Дом чужого
пула машине оператора неоткуда узнать, и знать его она не должна.
SOLUTION: paths.writable(home, path) -- чистое сопоставление: относительное
имя из белого списка разворачивается под домом узла; абсолютная форма
принимается только с домом самого узла (переход: старые клиенты); всё
остальное, включая обход через `..`, -- None.
STATUS: FIXED — see #279
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import paths  # noqa: E402


def main():
    c = Checks()
    fn = getattr(paths, "writable", None)
    if not c.check("paths.writable exists", fn is not None):
        return c.report("paths")
    home = "/home/mop"
    c.expect("relative secrets name resolves under the node's home",
             fn(home, ".config/mop/secrets.env"), "/home/mop/.config/mop/secrets.env")
    c.expect("the old absolute form with the node's own home still passes (transition)",
             fn(home, "/home/mop/.config/mop/secrets.env"), "/home/mop/.config/mop/secrets.env")
    c.expect("an absolute path with another home is refused",
             fn(home, "/home/ermak/.config/mop/secrets.env"), None)
    c.expect("a file outside the white list is refused", fn(home, ".bashrc"), None)
    c.expect("an absolute path elsewhere is refused", fn(home, "/etc/passwd"), None)
    c.expect("traversal past the white list is refused",
             fn(home, ".config/mop/../secrets.env"), None)
    c.expect("a name that merely starts like a listed one is refused",
             fn(home, ".config/mop/secrets.env.bak"), None)
    names = getattr(paths, "WRITABLE", ())
    # С #387 список -- одно имя: логин claude и метка кредита умерли.
    c.expect("the white list is the one relative name, no home in it (#387)",
             sorted(names), [".config/mop/secrets.env"])
    return c.report("paths")


if __name__ == "__main__":
    sys.exit(main())
