#!/usr/bin/env python3
"""Проверка дороги за манифестом без пула: python3 tests/manifest.py

Сборка образа в рабочей копии читает `.mop` из её файлов, а не из origin
(#46): правку манифеста собирают до коммита. Проверяется ровно это свойство:
каталог, который не является git-репозиторием вовсе, читается как есть —
значит, git по дороге не спрашивают, и незакоммиченное доезжает.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import manifest  # noqa: E402

NODE = """
- name: node
  vars:
    MOP_MEM_MB: 2048
    MOP_SERVER_LAN: 10.0.0.1
  tasks:
    - name: node task
      ansible.builtin.debug: {msg: hi}
"""
WS = """
- name: ws
  vars:
    PROJECT_PORT: 8080
  tasks:
    - name: ws task
      ansible.builtin.debug: {msg: hi}
"""


def tree(files):
    d = tempfile.mkdtemp(prefix="mop-test-tree-")
    for rel, text in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
    return d


# HYPOTHESIS: до #46 единственная дорога — fetch(origin) через зеркало, и файл
# рабочей копии для сборки не существовал. SOLUTION: fetch_tree(root, shard)
# читает те же два файла из каталога. STATUS: FIXED — see #46
def main():
    failed = 0
    got = manifest.fetch_tree(tree({".mop/node.yaml": NODE,
                                    ".mop/workspace.yaml": WS}), "proj")
    want_asks = {"MOP_MEM_MB": "2048"}
    if got["shard"] != "proj" or got["asks"] != want_asks:
        failed += 1
        print(f"FAIL asks: {got}")
    if got["alien"] != ["MOP_SERVER_LAN"]:
        failed += 1
        print(f"FAIL alien: {got['alien']}")
    for k in ("node_tasks", "ws_vars", "ws_tasks"):
        if not (got[k] and os.path.exists(got[k])):
            failed += 1
            print(f"FAIL {k}: {got[k]!r}")
    # Без .mop вовсе — общего хватает, все половины None.
    got = manifest.fetch_tree(tree({}), "bare")
    if got != {"shard": "bare", "asks": {}, "alien": [],
               "node_tasks": None, "ws_vars": None, "ws_tasks": None}:
        failed += 1
        print(f"FAIL bare: {got}")
    # Кривая форма — громко и с именем файла, а не «прочиталось как получилось».
    try:
        manifest.fetch_tree(tree({".mop/node.yaml": "vars: {}\n"}), "bad")
        failed += 1
        print("FAIL malformed: no error")
    except RuntimeError as e:
        if "bad/.mop/node.yaml" not in str(e):
            failed += 1
            print(f"FAIL malformed message: {e}")
    print("manifest: FAILED" if failed else "manifest: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
