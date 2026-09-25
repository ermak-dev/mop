"""Прогон плейбуков установки (#258): серверная сторона, зовёт только deploy.
Здесь, а не в lib: плейбуки, инвентарь и провайдер личностей есть только на
контроллере, а lib импортирует каждая команда на каждой машине."""
import json
import os
import shutil
import subprocess

from mop.common import config
from mop.server import identity, playvars

# Код ansible «часть машин не ответила». Отличать его от настоящего отказа
# обязательно: выключенный узел — не сломанная команда, и сказать про него
# «проект, возможно, не на шине» значит отправить оператора искать поломку
# там, где её нет.
UNREACHABLE = 4


def play_vars(projects, manifests=None, limits=None, git_hosts=None, inventory_hosts=None):
    """Списки плейбуку как --extra-vars, JSON'ом. -> [строки].

    Объектом, а не парой ключ=значение: `--extra-vars mop_projects=[...]`
    ansible принимает как строку, и цикл в шаблоне честно проходится по её
    символам, порождая пользователей `master-[`, `master-"` и так далее.

    Манифесты — только у полной игры: узкому прогону проектов (bus) они не
    нужны, их читают слои узла и тела."""
    # Лимиты папетов (#107) едут рядом с проектами, и пустые тоже: сервис
    # кластера на сервере читает их файлом, и узкий прогон `mop project`
    # обязан класть его так же, как полный. Файл читает play(), не эта
    # функция: она чистая.
    head = {"mop_projects": list(projects)}
    if limits is not None:
        head["mop_limits"] = limits
    # Хосты форжей (#121) -- только полной игре: их читает роль узла.
    if git_hosts is not None:
        head["mop_git_hosts"] = list(git_hosts)
    # Хосты инвентаря (#178) -- только полной игре: роль cluster кладёт их
    # файлом, и по нему forget отказывает узлу, который deploy поставит снова.
    if inventory_hosts is not None:
        head["mop_inventory_hosts"] = list(inventory_hosts)
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
         inventory_hosts=None):
    """Прогон плейбука установки. -> код возврата ansible.

    Один вход для полной игры (site.yml) и для узкого прогона проектов
    (до #117): списки, которые едут плейбуку, собираются одним
    местом, иначе узкий прогон заводил бы проект не так, как полный.

    Списки едут --extra-vars ОБЪЕКТОМ, а не парой ключ=значение:
    `--extra-vars mop_projects=[...]` ansible принимает как строку, и цикл в
    шаблоне честно проходится по её символам, порождая пользователей
    `master-[`, `master-"` и так далее.

    check=True -- прогон без изменений (#177): ansible --check --diff, и
    вывод прогона -- это разница, которую внёс бы настоящий.
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
                         inventory_hosts=inventory_hosts))
    return subprocess.call(
        ["ansible-playbook", "-i", inventory, os.path.join(config.PROJECT, playbook),
         *sum((["--extra-vars", v] for v in extra), []),
         *(["--check", "--diff"] if check else [])],
        env={**os.environ, **play_env(config.get)})
