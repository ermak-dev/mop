#!/usr/bin/env python3
"""deploy/ без пула: python3 tests/deploy.py

Юниты сервисов сервера и общие задачи ролей (#157). Прогнать плейбуки здесь
нечем, поэтому проверяется то, что проверяемо без машины:

  * юниты mop-bootstrap, mop-cluster и mop-web рендерятся из общего шаблона
    роли common байт в байт такими, какими их рендерили свои шаблоны до
    #157 (PINNED -- вывод модуля template ansible-core 2.21 на старых
    шаблонах с набором VARS ниже). Рендер нужен jinja2; в клоне его нет --
    тогда проверка рендера НЕ ИДЁТ и об этом печатается строка SKIPPED, а
    не зелёный итог;
  * у каждой общей вещи одно определение: список исключений пакета (и .env
    в нём -- .env не едет на узлы), синк, pip, регистрация MCP, загрузка
    nomad, пароль через stdin, secrets_dir и projects.

HYPOTHESIS: задачи deploy повторены по ролям -- синк пакета с исключениями
четырьмя копиями, pip четырежды, claude mcp и установка nomad дважды,
secrets_dir пять раз; env сервисов сервера набран в юнитах руками.
SOLUTION: роль common (package, pip, mcp, nomad_bin, secret_stdin, шаблон
юнита), общие переменные в deploy/group_vars/all.yml.
STATUS: FIXED — see #157
"""
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")
COMMON = os.path.join(DEPLOY, "roles", "common")

VARS = {"MOP_USER": "mopuser", "MOP_HOME": "/home/mopuser", "MOP_SERVER_LAN": "10.0.0.1",
        "MOP_NATS_PORT": "4222", "MOP_HTTPS_PORT": "443", "MOP_NOMAD_PORT": "4646",
        "MOP_POOL_DC": "home", "MOP_WEB_PORT": "8080", "MOP_WEB_BIND": "0.0.0.0"}
UNITS = {"mop-bootstrap": "bootstrap", "mop-cluster": "cluster", "mop-web": "web"}
# Исключения синка пакета до #157, во всех четырёх копиях одни и те же.
EXCLUDES = [".git", "__pycache__", ".env", "inventory.ini", "inventory.yaml"]

PINNED = {
    'mop-bootstrap': "[Unit]\nDescription=mop-bootstrap (bootstrap песочниц: играет .mop/bootstrap.yaml проекта при каждом старте папета)\nAfter=network-online.target nats.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет: всё, что подписчику и прогону нужно знать об\n# установке, приезжает юнитом. MOP_HOME и MOP_USER -- те, что у узлов: их\n# читают задачи bootstrap'а как переменные прогона, и дефолт сервера\n# (его собственный дом) здесь был бы неправдой.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_HOME=/home/mopuser\nEnvironment=MOP_USER=mopuser\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=/home/mopuser/mop/bin/mop bootstrap serve\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n",
    'mop-cluster': '[Unit]\nDescription=mop-cluster (сервис кластера: Nomad за шиной, глаголы пула на mop.*.cluster.rpc)\nAfter=network-online.target nats.service nomad.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет: что сервису нужно знать об установке, приезжает\n# юнитом. MOP_SERVER_LAN отвечает сразу за адрес шины и за NOMAD_ADDR\n# (config.DERIVED), поэтому второй переменной для Nomad здесь нет.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_HOME=/home/mopuser\nEnvironment=MOP_USER=mopuser\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=/home/mopuser/mop/bin/mop cluster serve\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n',
    'mop-web': '[Unit]\nDescription=mop-web (дашборд пула: состояние по HTTP)\nAfter=network-online.target nats.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет, а адрес сервера обязателен: без него config\n# отказывается работать (и правильно). Окружение старше .env, поэтому\n# юнит и есть источник этой настройки на сервере.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_NOMAD_PORT=4646\nEnvironment=MOP_POOL_DC=home\nEnvironment=PYTHONUNBUFFERED=1\n# Через диспетчер: он ставит PYTHONPATH, без него командлет пакета не найдёт.\nExecStart=/home/mopuser/mop/bin/mop web --port 8080 --bind 0.0.0.0\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n',
}


def deploy_files():
    for root, _, files in os.walk(DEPLOY):
        for f in files:
            if f.endswith((".yml", ".j2")):
                yield os.path.join(root, f)


def unit_task(role, unit):
    """Задача роли, что ставит юнит: её src и vars."""
    for f in os.listdir(os.path.join(DEPLOY, "roles", role, "tasks")):
        with open(os.path.join(DEPLOY, "roles", role, "tasks", f)) as fh:
            for t in yaml.safe_load(fh) or []:
                tmpl = (t or {}).get("ansible.builtin.template") or {}
                if tmpl.get("dest") == f"/etc/systemd/system/{unit}.service":
                    return tmpl, t.get("vars") or {}
    return None, {}


def render(text, variables):
    """Рендер как у модуля template: trim_blocks, хвостовой перевод строки
    сохраняется, строки в переменных доразворачиваются."""
    import jinja2
    env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True,
                             undefined=jinja2.StrictUndefined)

    def deep(v):
        if isinstance(v, str) and "{{" in v:
            return env.from_string(v).render(**variables)
        if isinstance(v, dict):
            return {k: deep(x) for k, x in v.items()}
        if isinstance(v, list):
            return [deep(x) for x in v]
        return v
    variables = dict(variables)
    for k in list(variables):
        variables[k] = deep(variables[k])
    # lookup('vars', имя) шаблона -- как у ansible: значение переменной игры.
    return env.from_string(text).render(
        **variables, lookup=lambda kind, name: variables[name] if kind == "vars" else None)


def main():
    cases = bad = 0

    def check(what, ok, detail=""):
        nonlocal cases, bad
        cases += 1
        if not ok:
            bad += 1
            print(f"FAILED  {what}" + (f": {detail}" if detail else ""))

    # ── юниты из общего шаблона ───────────────────────────────────────────
    template = os.path.join(COMMON, "templates", "service.j2")
    check("common unit template exists", os.path.isfile(template))
    try:
        import jinja2  # noqa: F401
        can_render = True
    except ImportError:
        can_render = False
        print("SKIPPED rendering of the units: no jinja2 on this machine — "
              "only the structure below is checked")
    for unit, role in UNITS.items():
        tmpl, uvars = unit_task(role, unit)
        check(f"{unit}: the role installs it", tmpl is not None)
        if tmpl is None:
            continue
        check(f"{unit}: from the common template",
              tmpl.get("src", "").endswith("common/templates/service.j2"), tmpl.get("src"))
        own = os.path.join(DEPLOY, "roles", role, "templates", f"{unit}.service.j2")
        check(f"{unit}: its own template is gone", not os.path.exists(own))
        # Набор переменных окружения -- тот же, что был (#157 его не меняет).
        was = re.findall(r"^Environment=([A-Z_]+)=", PINNED[unit], re.M)
        now = [e for e in (uvars.get("unit") or {}).get("env", []) if not e.startswith("#")]
        now += ["PYTHONUNBUFFERED"] if os.path.isfile(template) and \
            "PYTHONUNBUFFERED=1" in open(template).read() else []
        check(f"{unit}: the same env set", sorted(now) == sorted(was),
              f"{sorted(now)} != {sorted(was)}")
        if can_render and os.path.isfile(template):
            got = render(open(template).read(), {**VARS, **uvars})
            check(f"{unit}: rendered byte for byte as before", got == PINNED[unit],
                  "\n" + "\n".join(f"  -{a!r}\n  +{b!r}" for a, b in
                                   zip(PINNED[unit].splitlines(), got.splitlines()) if a != b))

    # ── одно определение у каждой общей вещи ─────────────────────────────
    gv_path = os.path.join(DEPLOY, "group_vars", "all.yml")
    gv = yaml.safe_load(open(gv_path)) if os.path.isfile(gv_path) else {}
    check("group_vars: package excludes, .env among them",
          gv.get("mop_package_excludes") == EXCLUDES, gv.get("mop_package_excludes"))
    check("group_vars: secrets_dir", gv.get("secrets_dir") ==
          "{{ lookup('env', 'HOME') }}/.config/mop/secrets", gv.get("secrets_dir"))
    check("group_vars: projects", gv.get("projects") == "{{ mop_projects | default([]) }}",
          gv.get("projects"))
    check("group_vars: the package's source is the playbook's tree",
          "playbook_dir" in str(gv.get("mop_source")), gv.get("mop_source"))

    only = {
        "ansible.posix.synchronize": "roles/common/tasks/package.yml",
        "MOP_PIP_DEPS": "roles/common/tasks/pip.yml",
        "claude mcp add": "roles/common/tasks/mcp.yml",
        "nomad_{{ MOP_NOMAD_VERSION }}_linux_amd64.zip": "roles/common/tasks/nomad_bin.yml",
        "ansible.builtin.tempfile": "roles/common/tasks/secret_stdin.yml",
        # Флаги исключений синк и tar строят из списка: своих копий нет.
        "exclude=.env": None,
        "exclude=inventory": None,
        "secrets_dir:": "group_vars/all.yml",
        "projects: \"{{ mop_projects": "group_vars/all.yml",
        "lookup('env', 'HOME') }}/mop": None,
    }
    for needle, home in only.items():
        where = sorted(os.path.relpath(f, DEPLOY) for f in deploy_files()
                       if needle in "\n".join(l for l in open(f).read().splitlines()
                                              if not l.lstrip().startswith("#")))
        want = [home] if home else []
        check(f"{needle!r} lives only in {home or 'no file'}", where == want, where)

    print(f"deploy: {cases - bad}/{cases}" + (" FAILED" if bad else " ok"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
