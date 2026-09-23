"""this machine's own dependencies: mop setup [--operator]

Sets up the machine running mop, and nothing for the pool: the pool's
nodes and bodies are ansible's business (mop deploy). Without a flag the
machine is the controller, the one that runs mop deploy: ansible with the
ansible.posix collection, rsync, git, curl, pip and the python libraries
mop is written on. With --operator it is an operator's machine: ansible
(to run this very setup), git, pip and the same libraries, and a word
about claude if it is not in PATH.

Ansible does the work, as everywhere else in mop: this command only
installs ansible itself through apt when it is missing (Debian 13 ships
the full ansible package, collections included) and runs
deploy/self.yml against localhost. Running it again changes nothing.

This file imports nothing that setup is about to install: bin/lib.py
pulls in the bus client at import, and a setup that needs nats-py to
install nats-py refused to start on a fresh machine.
"""
import json
import os
import shutil
import subprocess
import sys


from mop import config  # noqa: E402  (stdlib only)

PLAYBOOK = os.path.join(config.PROJECT, "deploy", "self.yml")


def elevate(uid=None):
    """Чем запускать команду, которой нужны права root. -> префикс argv.

    Под root — ничем. Это не оптимизация: на выделенном сервере `sudo` часто
    просто не стоит, и безусловный префикс роняет первый же шаг установки
    трассировкой про отсутствующий файл (#91)."""
    return [] if (os.getuid() if uid is None else uid) == 0 else ["sudo"]


def _setup(argv):
    if argv not in ([], ["--operator"]):
        sys.exit(__doc__.strip())
    role = "operator" if argv else "controller"
    if not shutil.which("ansible-playbook"):
        print("installing ansible (apt)")
        root = elevate()
        if root and not shutil.which("sudo"):
            sys.exit("ansible is missing and this user cannot install it: "
                     "no sudo here. Install ansible, or run mop setup as root")
        subprocess.run([*root, "apt-get", "update", "-q"], check=True)
        subprocess.run([*root, "apt-get", "install", "-y", "-q", "ansible"],
                       check=True)
    settings = json.dumps(config.playbook_vars(), ensure_ascii=False)
    r = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local",
                        PLAYBOOK, "--extra-vars", settings,
                        "--extra-vars", json.dumps({"mop_role": role})])
    if r.returncode:
        sys.exit(r.returncode)
    if role == "operator" and not shutil.which("claude"):
        print("claude is not in PATH: install Claude Code before mop master")


def main(argv):
    try:
        return _setup(argv)
    except subprocess.CalledProcessError as e:
        sys.exit(f"setup failed: {' '.join(e.cmd)} exited {e.returncode}")
    except OSError as e:
        # Трассировка про отсутствующий файл говорит о питоне, а не о машине.
        sys.exit(f"setup failed: {e}")
