"""Прогон плейбуков установки (#258): серверная сторона, зовёт только deploy.
Здесь, а не в lib: плейбуки, инвентарь и провайдер личностей есть только на
контроллере, а lib импортирует каждая команда на каждой машине."""
import json
import os
import shutil
import subprocess
import sys

from mop.common import config
from mop.server import identity, playvars

# Переменная плейбука со списком проектов (deploy/group_vars, роль bootstrap).
# Пишется как ключ меты узла spec.META_PROJECTS, но это другое: там -- что
# узел обслуживает, здесь -- какие проекты заведены.
PROJECTS_VAR = "mop_projects"

# Код ansible «часть машин не ответила». Отличать его от настоящего отказа
# обязательно: выключенный узел — не сломанная команда, и сказать про него
# «проект, возможно, не на шине» значит отправить оператора искать поломку
# там, где её нет.
UNREACHABLE = 4

# Провал ansible и «не ответила» -- разные биты одного кода (2 и 4), но
# базовая стратегия ansible (plugins/strategy/__init__.py) недоступные
# проверяет РАНЬШЕ провалившихся, и _tqm._unreachable_hosts копится по
# играм: смешанный прогон (failed=1 и unreachable=1) приходит кодом 4,
# тем же, что и чистый #87. Поэтому код один не спрашивает «был ли
# провал» -- только recap.
_FAILED_RECAP = "failed="


def verdict(rc, output=""):
    """Итог прогона ansible по коду и выводу. -> "ok" | "unreachable" | "failed".

    failed -- у прогона есть провалившаяся машина (failed=N>0 в PLAY
    RECAP или бит 2 в коде): красный прогон, чем бы ни кончился расклад
    недоступных (#475). unreachable -- машины не ответили, но не падали:
    ветка #87, прогон не красный. ok -- чисто."""
    if rc & 2 or _recap_failed(output):
        return "failed"
    if rc & UNREACHABLE:
        return "unreachable"
    return "ok"


def _recap_failed(output):
    """Есть ли failed=N>0 в строках PLAY RECAP вывода ansible."""
    for line in (output or "").splitlines():
        if "failed=" not in line:
            continue
        for part in line.split():
            if part.startswith(_FAILED_RECAP):
                try:
                    if int(part[len(_FAILED_RECAP):]) > 0:
                        return True
                except ValueError:
                    pass
    return False


def play_vars(projects, manifests=None, limits=None, git_hosts=None, inventory_hosts=None,
              registry_seed=None, registry_seed_refused=False):
    """Списки плейбуку как --extra-vars, JSON'ом. -> [строки].

    Объектом, а не парой ключ=значение: `--extra-vars mop_projects=[...]`
    ansible принимает как строку, и цикл в шаблоне честно проходится по её
    символам, порождая пользователей `master-[`, `master-"` и так далее.

    Манифесты — только у полной игры: узкому прогону проектов (bus) они не
    нужны, их читают слои узла и тела.

    registry_seed (#540) -- путь к снятому на контроллере снимку реестра
    v1 для посева на пустой сервер: путь, не содержимое, и только когда
    есть что сеять (None -- переменной нет вовсе, ручной прогон без
    extra-vars видит пустой дефолт из group_vars).

    registry_seed_refused (#540) -- отказ стейджинга: префлайт не смог
    снять снимок (непредставимый реестр контроллера) и напечатал причину
    локально. Плейбуку едет голый признак без текста: причина -- не для
    argv и журналов, fail-задача поднимет её под условиями самого посева.
    False по умолчанию: без extra-vars признака нет вовсе.
    """
    # Лимиты папетов (#107) едут рядом с проектами, и пустые тоже: сервис
    # кластера на сервере читает их файлом, и узкий прогон `mop project`
    # обязан класть его так же, как полный. Файл читает play(), не эта
    # функция: она чистая.
    head = {PROJECTS_VAR: list(projects)}
    if limits is not None:
        head["mop_limits"] = limits
    # Хосты форжей (#121) -- только полной игре: их читает роль узла.
    if git_hosts is not None:
        head["mop_git_hosts"] = list(git_hosts)
    # Хосты инвентаря (#178) -- только полной игре: роль cluster кладёт их
    # файлом, и по нему forget отказывает узлу, который deploy поставит снова.
    if inventory_hosts is not None:
        head["mop_inventory_hosts"] = list(inventory_hosts)
    # Посев реестра (#540): путь без содержимого, None -- переменной нет.
    if registry_seed is not None:
        head["mop_registry_seed"] = registry_seed
    # Отказ стейджинга (#540): признак без текста, False -- переменной нет.
    if registry_seed_refused:
        head["mop_registry_seed_refused"] = True
    out = [json.dumps(head)]
    if manifests is not None:
        out.append(json.dumps({"mop_manifests": manifests}, ensure_ascii=False))
    return out
def play_env(get):
    """Окружение процесса ansible сверх своего. -> {имя: значение}.

    Пароль служебной учётки LDAP (#214) -- только когда ldap в цепочке (#232), и
    окружением, а не --extra-vars: argv виден в списке процессов, а
    настройки едут плейбукам все. Плейбук берёт его lookup('env') и кладёт
    файлом 0600 в /etc/nats/identity."""
    if "ldap" not in identity.links(get("MOP_AUTH_PROVIDER")):
        return {}
    return {"MOP_LDAP_BIND_PASSWORD": get("MOP_LDAP_BIND_PASSWORD") or ""}
def play(playbook, projects, manifests=None, git_hosts=None, check=False,
         inventory_hosts=None, registry_seed=None, registry_seed_refused=False):
    """Прогон плейбука установки. -> (код возврата ansible, его вывод).

    Один вход для полной игры (site.yml) и для узкого прогона проектов
    (до #117): списки, которые едут плейбуку, собираются одним
    местом, иначе узкий прогон заводил бы проект не так, как полный.

    Списки едут --extra-vars ОБЪЕКТОМ, а не парой ключ=значение:
    `--extra-vars mop_projects=[...]` ansible принимает как строку, и цикл в
    шаблоне честно проходится по её символам, порождая пользователей
    `master-[`, `master-"` и так далее.

    check=True -- прогон без изменений (#177): ansible --check --diff, и
    вывод прогона -- это разница, которую внёс бы настоящий.

    Вывод собирается здесь (#475): код один не говорит, был ли провал --
    ansible маскирует смешанный прогон (failed=1 и unreachable=1) кодом
    чистых недоступных узлов, и только PLAY RECAP называет провалившуюся
    машину. Игра идёт в терминал как раньше, вывод читается из тех же
    stdout/stderr.
    """
    if not shutil.which("ansible-playbook"):
        raise RuntimeError("no ansible-playbook on this machine -- run mop server setup")
    inventory = os.environ["INVENTORY"]
    if not os.path.isfile(inventory):
        raise RuntimeError(f"no inventory {inventory} -- create it from the example: "
                           f"cp inventory.yaml.example inventory.yaml")
    vars_ = playvars.playbook_vars()
    # Лимиты (#107) больше не едут: их держит и правит сервер (#117).
    extra = ([json.dumps(vars_, ensure_ascii=False)]
             + play_vars(projects, manifests, git_hosts=git_hosts,
                         inventory_hosts=inventory_hosts, registry_seed=registry_seed,
                         registry_seed_refused=registry_seed_refused))
    proc = subprocess.run(
        ["ansible-playbook", "-i", inventory, os.path.join(config.PROJECT, playbook),
         *sum((["--extra-vars", v] for v in extra), []),
         *(["--check", "--diff"] if check else [])],
        env={**os.environ, **play_env(config.get)}, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode, proc.stdout + proc.stderr
