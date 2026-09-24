#!/usr/bin/env python3
"""Проверка дороги за манифестом без пула: python3 tests/manifest.py

Сборка образа в рабочей копии читает `.mop` из её файлов, а не из origin
(#46): правку манифеста собирают до коммита. Проверяется ровно это свойство:
каталог, который не является git-репозиторием вовсе, читается как есть —
значит, git по дороге не спрашивают, и незакоммиченное доезжает.
"""
import os
import subprocess
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
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
# рабочей копии для сборки не существовал. SOLUTION: fetch_tree(root, project)
# читает те же два файла из каталога. STATUS: FIXED — see #46
#
# Раскладка манифеста (#61). Имена файлов обещали одно, механизм делал
# другое: workspace.yaml пёкся в образ, node.yaml играл на машине-узле (на
# гипервизоре — на самом Proxmox, где папета нет), а при старте не играло
# ничто. Теперь два файла с честными именами:
#   sandbox.yaml    размеры тела и системные пакеты — печётся в образ
#                   (или играется на host-узле при deploy)
#   bootstrap.yaml  env-файлы и настройка окружения — играется при КАЖДОМ
#                   старте песочницы (#62)
# Старые имена читаются переходно и отдаются как sandbox — с пометкой legacy,
# чтобы вызывающий сказал об этом вслух; непереехавший проект не должен молча
# остаться без манифеста.
# HYPOTHESIS: manifest знает только node.yaml и workspace.yaml (ключи
# node_tasks/ws_*). SOLUTION: sandbox_*/bootstrap_* плюс переходное чтение
# старых имён. STATUS: FIXED — see #61
SANDBOX = """
- name: sandbox
  vars:
    MOP_MEM_MB: 2048
    MOP_SERVER_LAN: 10.0.0.1
    PROJECT_PORT: 8080
  tasks:
    - name: sandbox task
      ansible.builtin.debug: {msg: hi}
"""
BOOTSTRAP = """
- name: bootstrap
  vars:
    MOP_DISK_GB: 10
    ENV_FILE: .env
  tasks:
    - name: bootstrap task
      ansible.builtin.debug: {msg: hi}
"""
EMPTY = {"project": "bare", "asks": {}, "alien": [], "legacy": [],
         "sandbox_vars": None, "sandbox_tasks": None,
         "bootstrap_vars": None, "bootstrap_tasks": None, "bootstrap_text": None}


# #139: `mop project add` висел на «reading .mop» больше минуты: fetch
# клонировал зеркало всей истории (rudesktop: 77 с, 451 МБ), чтобы прочитать
# два файла HEAD. Свойство: клон несёт один коммит и ни одного блоба, кроме
# прочитанных, а манифест при этом читается целиком.
# HYPOTHESIS: `git clone --mirror` тянет всю историю и все блобы.
# SOLUTION: голый клон --depth 1 --filter=blob:none; `git show` догружает
# нужный блоб лениво. RESULT: 0,7 с и 440 КБ на rudesktop.
def run(*argv, cwd=None):
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


def fetch_is_shallow_and_blobless():
    """-> список отказов."""
    src = tree({".mop/bootstrap.yaml": BOOTSTRAP, "heavy.bin": "x" * 100000})
    run("git", "init", "-q", "-b", "master", cwd=src)
    run("git", "config", "uploadpack.allowFilter", "true", cwd=src)
    for i in range(3):
        with open(os.path.join(src, "history.txt"), "w") as f:
            f.write(f"commit {i}\n")
        run("git", "add", "-A", cwd=src)
        run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit",
            "-q", "-m", f"c{i}", cwd=src)
    seen = {}
    real = manifest.subprocess.run

    def spy(argv, *a, **kw):
        r = real(argv, *a, **kw)
        if argv[:2] == ["git", "clone"] and r.returncode == 0:
            dest = argv[-1]
            seen["commits"] = real(["git", "-C", dest, "rev-list", "--count", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
            # --missing=print не догружает: видно, чего клон не принёс.
            objs = real(["git", "-C", dest, "rev-list", "--objects", "--all",
                         "--missing=print"], capture_output=True, text=True).stdout
            seen["missing"] = sum(1 for l in objs.splitlines() if l.startswith("?"))
        return r

    manifest.subprocess.run = spy
    try:
        got = manifest.fetch(f"file://{src}")
    finally:
        manifest.subprocess.run = real
    bad = []
    if got.get("bootstrap_text") != BOOTSTRAP:
        bad.append(f"bootstrap must read through the clone: {got.get('bootstrap_text')!r}")
    if seen.get("commits") != "1":
        bad.append(f"clone must carry one commit, carries {seen.get('commits')}")
    if not seen.get("missing"):
        bad.append("clone must not carry the blobs it doesn't read (heavy.bin, history.txt)")
    return bad


# #141: сервер не знал ключа хоста форжа, и origin не читался нигде на
# нём: ни сборщиком, ни самим `mop deploy`, который читает `.mop` проектов до
# плейбука, а значит до задачи, которая этот ключ положила бы.
# HYPOTHESIS: ключи форжей кладёт плейбук (роль node, затем cluster) --
# курица и яйцо: deploy падает раньше. SOLUTION: клон манифеста принимает
# новый ключ хоста сам (accept-new) и по-прежнему отказывает сменившемуся;
# выбранный оператором ssh (GIT_SSH_COMMAND, GIT_SSH) не трогаем.
# STATUS: FIXED — see #141
def clone_env_accepts_new_host_keys():
    """-> список отказов."""
    bad = []
    env = manifest.clone_env({"PATH": "/bin"})
    if "StrictHostKeyChecking=accept-new" not in env.get("GIT_SSH_COMMAND", ""):
        bad.append(f"a new host key must be accepted: {env.get('GIT_SSH_COMMAND')!r}")
    if env.get("PATH") != "/bin":
        bad.append("the rest of the environment must pass through")
    for mine in ({"GIT_SSH_COMMAND": "ssh -i k"}, {"GIT_SSH": "/usr/bin/plink"}):
        env = manifest.clone_env(dict(mine))
        if env != mine:
            bad.append(f"an operator's own ssh must stay as it is: {mine} -> {env}")
    return bad


def main():
    failed = 0
    got = manifest.fetch_tree(tree({".mop/sandbox.yaml": SANDBOX,
                                    ".mop/bootstrap.yaml": BOOTSTRAP}), "proj")
    if got["project"] != "proj" or got["asks"] != {"MOP_MEM_MB": "2048"}:
        failed += 1
        print(f"FAIL asks: {got}")
    # Чужое имя в sandbox и просьба о размере в bootstrap — оба не на месте,
    # и оба названы, а не проглочены.
    if got["alien"] != ["MOP_DISK_GB", "MOP_SERVER_LAN"]:
        failed += 1
        print(f"FAIL alien: {got['alien']}")
    if got["legacy"]:
        failed += 1
        print(f"FAIL legacy must be empty for the new names: {got['legacy']}")
    for k in ("sandbox_vars", "sandbox_tasks", "bootstrap_vars", "bootstrap_tasks"):
        if not (got.get(k) and os.path.exists(got[k])):
            failed += 1
            print(f"FAIL {k}: {got.get(k)!r}")
    # HYPOTHESIS (#124): `mop project add` советовал запустить deploy, чтобы
    # bootstrap проекта попал на сервер, хотя сервер принимает его глаголом
    # `put` текстом. SOLUTION: манифест отдаёт и сырой текст bootstrap.yaml.
    # STATUS: FIXED — see #124
    if got.get("bootstrap_text") != BOOTSTRAP:
        failed += 1
        print(f"FAIL bootstrap_text must be the file as it lies: {got.get('bootstrap_text')!r}")
    # Старые имена: node.yaml + workspace.yaml читаются как sandbox — просьбы,
    # конфигурация и задачи обоих, — и об этом сказано.
    got = manifest.fetch_tree(tree({".mop/node.yaml": NODE,
                                    ".mop/workspace.yaml": WS}), "old")
    if got["asks"] != {"MOP_MEM_MB": "2048"} or got["alien"] != ["MOP_SERVER_LAN"]:
        failed += 1
        print(f"FAIL legacy asks/alien: {got}")
    if sorted(got["legacy"]) != [".mop/node.yaml", ".mop/workspace.yaml"]:
        failed += 1
        print(f"FAIL legacy names: {got['legacy']}")
    for k in ("sandbox_vars", "sandbox_tasks"):
        if not (got.get(k) and os.path.exists(got[k])):
            failed += 1
            print(f"FAIL legacy {k}: {got.get(k)!r}")
    if got["bootstrap_tasks"] or got["bootstrap_vars"]:
        failed += 1
        print("FAIL legacy files must not become a bootstrap: nothing played "
              "them at start before, nothing should now")
    if got["sandbox_tasks"]:
        import yaml
        with open(got["sandbox_tasks"]) as f:
            names = [t["name"] for t in yaml.safe_load(f)]
        if names != ["node task", "ws task"]:
            failed += 1
            print(f"FAIL legacy tasks must merge into the sandbox, node first: {names}")
    # Без .mop вовсе — общего хватает, все половины None.
    got = manifest.fetch_tree(tree({}), "bare")
    if got != EMPTY:
        failed += 1
        print(f"FAIL bare: {got}")
    # Кривая форма — громко и с именем файла, а не «прочиталось как получилось».
    try:
        manifest.fetch_tree(tree({".mop/sandbox.yaml": "vars: {}\n"}), "bad")
        failed += 1
        print("FAIL malformed: no error")
    except RuntimeError as e:
        if "bad/.mop/sandbox.yaml" not in str(e):
            failed += 1
            print(f"FAIL malformed message: {e}")
    for why in clone_env_accepts_new_host_keys():
        failed += 1
        print(f"FAIL clone env: {why}")
    for why in fetch_is_shallow_and_blobless():
        failed += 1
        print(f"FAIL fetch: {why}")
    print("manifest: FAILED" if failed else "manifest: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
