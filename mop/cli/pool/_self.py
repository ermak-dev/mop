"""Настройка самой машины, общее у `mop setup` (машина оператора) и `mop
server setup` (контроллер, #259): ставит ansible, если его нет, и играет
deploy/self.yml с ролью машины.

Этот файл не импортирует ничего, что setup собирается поставить: lib тянет
клиент шины при импорте, и setup, которому для установки nats-py нужен
nats-py, отказывался стартовать на свежей машине.
"""
import json
import os
import shutil
import subprocess
import sys


from mop.common import config
from mop.server import playvars  # noqa: E402  (stdlib only)

PLAYBOOK = os.path.join(config.PROJECT, "deploy", "self.yml")


def elevate(uid=None):
    """Чем запускать команду, которой нужны права root. -> префикс argv.

    Под root — ничем. Это не оптимизация: на выделенном сервере `sudo` часто
    просто не стоит, и безусловный префикс роняет первый же шаг установки
    трассировкой про отсутствующий файл (#91)."""
    return [] if (os.getuid() if uid is None else uid) == 0 else ["sudo"]


def _step(text):
    """Шаг долгой команды (#159): строка на терминале, стёртая по завершении;
    не на терминале -- тишина до ошибки. Своя, а не lib.Progress: lib тянет
    клиент шины, а этот файл обязан подниматься без него (докстринг выше)."""
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K" + (f"setup: {text}" if text else ""))
        sys.stderr.flush()


def run(role):
    """Сыграть self.yml для роли машины: controller | operator."""
    if not shutil.which("ansible-playbook"):
        root = elevate()
        if root and not shutil.which("sudo"):
            sys.exit("ansible is missing and this user cannot install it: "
                     "no sudo here. Install ansible, or run the setup as root")
        _step("installing ansible (apt)")
        subprocess.run([*root, "apt-get", "update", "-q"], check=True)
        subprocess.run([*root, "apt-get", "install", "-y", "-q", "ansible"],
                       check=True)
        _step(None)
    settings = json.dumps(playvars.playbook_vars(), ensure_ascii=False)
    r = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local",
                        PLAYBOOK, "--extra-vars", settings,
                        "--extra-vars", json.dumps({"mop_role": role})])
    if r.returncode:
        sys.exit(r.returncode)
    if role == "operator" and not shutil.which("claude"):
        print("claude is not in PATH: install Claude Code before mop master",
              file=sys.stderr)


def main(role, argv, doc):
    """Команда целиком: без аргументов, отказы одной строкой."""
    if argv:
        sys.exit(doc.strip())
    try:
        return run(role)
    except subprocess.CalledProcessError as e:
        sys.exit(f"setup failed: {' '.join(e.cmd)} exited {e.returncode}")
    except OSError as e:
        # Трассировка про отсутствующий файл говорит о питоне, а не о машине.
        sys.exit(f"setup failed: {e}")
