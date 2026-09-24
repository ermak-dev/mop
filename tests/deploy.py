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

Кому что из настроек (#176): .env на сервер не едет, и сервис сервера видит
установку только окружением своего юнита. Набор env у юнитов был набран
руками, и mop-cluster не получал MOP_NOMAD_PORT и MOP_POOL_DC, которые сам же
читает (NOMAD_ADDR и датацентр в mop/nomad.py): при недефолтных значениях
сервис шёл на дефолтный порт и в дефолтный DC, молча.

HYPOTHESIS: env юнитов -- рукописные списки в vars задач, и с тем, что
читает код сервиса, их ничто не сверяет.
SOLUTION: config.SERVER_SCOPED -- {юнит: настройки}, playvars везёт его
плейбукам как MOP_SERVER_SCOPED, шаблон юнита рендерит env из него;
комментарии остаются в задаче (unit.notes, перед своей настройкой). Проверка
ниже выводит то, что читают mop/cluster.py, mop/nomad.py и mop/spec.py
(спецификацию собирает сервис кластера), из AST, а не руками.
STATUS: FIXED — see #176

Кавычки в env юнитов (#185): шаблон писал Environment=K=V как есть, а systemd
делит строку по пробелам. Значение с пробелом доезжало до сервиса первым
словом, остальное systemd отбрасывал с предупреждением в журнал -- молча.

HYPOTHESIS: service.j2 не заключает значение в кавычки.
SOLUTION: Environment="K=V" с экранированием кавычки и обратного слеша --
только когда в значении есть пробел, кавычка или обратный слеш; процент
удваивается всегда (systemd раскрывает %-спецификаторы и в Environment=).
Нынешние юниты такого не содержат и остаются байт в байт. Проверка читает
юнит по правилам systemd.
STATUS: FIXED — see #185
"""
import ast
import os
import re
import shlex
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")
COMMON = os.path.join(DEPLOY, "roles", "common")
sys.path.insert(0, ROOT)
from mop import config  # noqa: E402
# Что сервис кластера читает сам (#176): его код, клиент Nomad и спецификация,
# а с ней профиль LLM -- job_spec зовёт llm.resolve, и create без --llm
# приходит с profile=None, то есть с умолчанием установки.
CLUSTER_READS = ("mop/cluster.py", "mop/nomad.py", "mop/spec.py", "mop/llm/__init__.py")

VARS = {"MOP_USER": "mopuser", "MOP_HOME": "/home/mopuser", "MOP_SERVER_LAN": "10.0.0.1",
        "MOP_NATS_PORT": "4222", "MOP_HTTPS_PORT": "443", "MOP_NOMAD_PORT": "4646",
        "MOP_POOL_DC": "home", "MOP_WEB_PORT": "8080", "MOP_WEB_BIND": "0.0.0.0",
        "MOP_GIT_NAME": "Pool Bot", "MOP_GIT_EMAIL": "bot@example.dev"}
# Значения, которые юнит обязан донести целиком (#185): пробел, кавычки,
# обратный слеш. Подставляются вместо настройки из набора юнита.
AWKWARD = ("Pool Bot", 'say "hi"', "a\\b", "tab\there", "it's", "100%", "%h")
UNITS = {"mop-bootstrap": "bootstrap", "mop-cluster": "cluster", "mop-web": "web"}
# Исключения синка пакета до #157, во всех четырёх копиях одни и те же.
EXCLUDES = [".git", "__pycache__", ".env", "inventory.ini", "inventory.yaml"]
# Что #176 добавляет в юнит mop-cluster: всё это сервис читает, а юнит не
# передавал. Порт и DC Nomad -- из тикета; объём, потолок, посев и PATH папета
# нашла проверка ниже (их читает mop/spec.py, а спецификацию теперь собирает
# сервис кластера, не машина оператора с её .env), как и профиль LLM по
# умолчанию (llm.resolve из job_spec).
ADDED = {"mop-cluster": ("MOP_NOMAD_PORT", "MOP_POOL_DC", "MOP_PUPPET_MEM_MB",
                         "MOP_MEM_MB", "MOP_PUPPET_SEED", "MOP_PUPPET_PATH",
                         "MOP_DEFAULT_LLM",
                         # git identity папета (#167): её кладёт в спеку job_spec.
                         "MOP_GIT_NAME", "MOP_GIT_EMAIL")}

PINNED = {
    'mop-bootstrap': "[Unit]\nDescription=mop-bootstrap (bootstrap песочниц: играет .mop/bootstrap.yaml проекта при каждом старте папета)\nAfter=network-online.target nats.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет: всё, что подписчику и прогону нужно знать об\n# установке, приезжает юнитом. MOP_HOME и MOP_USER -- те, что у узлов: их\n# читают задачи bootstrap'а как переменные прогона, и дефолт сервера\n# (его собственный дом) здесь был бы неправдой.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_HOME=/home/mopuser\nEnvironment=MOP_USER=mopuser\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=/home/mopuser/mop/bin/mop bootstrap serve\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n",
    'mop-cluster': '[Unit]\nDescription=mop-cluster (сервис кластера: Nomad за шиной, глаголы пула на mop.*.cluster.rpc)\nAfter=network-online.target nats.service nomad.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет: что сервису нужно знать об установке, приезжает\n# юнитом. MOP_SERVER_LAN отвечает сразу за адрес шины и за NOMAD_ADDR\n# (config.DERIVED), поэтому второй переменной для Nomad здесь нет.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_HOME=/home/mopuser\nEnvironment=MOP_USER=mopuser\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=/home/mopuser/mop/bin/mop cluster serve\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n',
    'mop-web': '[Unit]\nDescription=mop-web (дашборд пула: состояние по HTTP)\nAfter=network-online.target nats.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет, а адрес сервера обязателен: без него config\n# отказывается работать (и правильно). Окружение старше .env, поэтому\n# юнит и есть источник этой настройки на сервере.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_NOMAD_PORT=4646\nEnvironment=MOP_POOL_DC=home\nEnvironment=PYTHONUNBUFFERED=1\n# Через диспетчер: он ставит PYTHONPATH, без него командлет пакета не найдёт.\nExecStart=/home/mopuser/mop/bin/mop web --port 8080 --bind 0.0.0.0\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n',
}


def settings_read(path):
    """Настройки, что модуль читает: config.<что угодно>("MOP_...") с
    литералом-именем из SETTINGS."""
    tree = ast.parse(open(os.path.join(ROOT, path)).read())
    return {n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "config"
            and n.args and isinstance(n.args[0], ast.Constant)
            and n.args[0].value in config.SETTINGS}


def derived_inputs():
    """{выводимая настройка: из каких она выводится} -- по get("...") в
    лямбдах config.DERIVED."""
    tree = ast.parse(open(os.path.join(ROOT, "mop", "config.py")).read())
    for n in tree.body:
        if isinstance(n, ast.Assign) and [getattr(t, "id", "") for t in n.targets] == ["DERIVED"]:
            return {k.value: {c.args[0].value for c in ast.walk(v)
                              if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "get"
                              and c.args and isinstance(c.args[0], ast.Constant)}
                    for k, v in zip(n.value.keys, n.value.values)}
    return {}


def unit_env(names):
    """Что юнит должен получить окружением: прочитанное, выводимое -- через
    то, из чего оно выводится (NOMAD_ADDR -- из адреса сервера и порта);
    настройки процесса ставит родитель, а не установка."""
    derived, out, todo = derived_inputs(), set(), list(names)
    while todo:
        n = todo.pop()
        if n in derived:
            todo += derived[n]
        elif n not in config.PROCESS_SCOPED:
            out.add(n)
    return out


def environment(unit_text):
    """{переменная: значение} юнита так, как его разбирает systemd: строка
    Environment= -- присваивания через пробел, в кавычках с экранированием;
    слово без «=» отбрасывается. Спецификаторы systemd раскрывает раньше
    разбора: %% -- это %, а любой другой %x подменяет значение (здесь -- на
    метку, чтобы подмена была видна)."""
    out = {}
    for line in unit_text.splitlines():
        if line.startswith("Environment="):
            rest = re.sub(r"%(.)", lambda m: "%" if m[1] == "%" else f"<%{m[1]} expanded>",
                          line[len("Environment="):])
            out.update(a.split("=", 1) for a in shlex.split(rest) if "=" in a)
    return out


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


# ── #184: пробы только для чтения в check-режиме ─────────────────────────────
# HYPOTHESIS: в check-режиме ansible пропускает command/shell, пропущенная
# задача ничего не регистрирует (ни stdout, ни rc), и задача, читающая её
# результат, падает. Первая такая — «Trust the git host keys» в роли node,
# поэтому `mop deploy --check` (#177) останавливался на каждой машине.
# SOLUTION: check_mode: false на пробах, которые только читают; их результат
# нужен и прогону без изменений.
CMD_MODULES = {"command", "shell", "ansible.builtin.command", "ansible.builtin.shell"}
# (файл роли, имя задачи, что она регистрирует, команды, из которых состоит)
PROBES = [
    ("node/tasks/main.yml", "Host keys of the git servers", "git_hostkeys", ["ssh-keyscan"]),
    ("pve/tasks/main.yml", "The node's address as the server sees it", "pve_toward_server",
     ["getent", "ip -4 -o route get"]),
    ("pve/tasks/server.yml", "Ask the kernel where each hypervisor's slice goes", "route_check",
     ["ip -4 route get"]),
    ("nomad/tasks/server.yml", "How MOP_SERVER_LAN resolves on the server itself",
     "server_name_here", ["getent"]),
    ("nomad/tasks/server.yml", "Address of the server's default route", "server_route",
     ["ip -4 route get"]),
    ("web/tasks/package.yml", "Version of the package on the control machine",
     "mop_package_fingerprint", ["git rev-parse", "git status --porcelain", "git diff", "sha256sum"]),
    ("bus/tasks/watchdog.yml", "Check the pu-cleanup job is registered", "cleanup_status",
     ["nomad job status"]),
]
# Чего в пробе быть не может: всё, что меняет машину.
WRITES = re.compile(r"\b(rm|mv|cp|tee|install|mkdir|chmod|chown|systemctl|apt|apt-get|pip|"
                    r"sed -i|nomad job run|touch|ln)\b|[^2&]>\s*[^&\s]")


def site_tasks():
    """Задачи, до которых доходит site.yml: import_playbook, роли, tasks_from,
    include/import_tasks со статическим путём. -> [(файл, задача)]."""
    seen, out = set(), []

    def load(p):
        with open(p) as fh:
            return yaml.safe_load(fh) or []

    def role(name, tasks_from="main"):
        for sub in (f"tasks/{tasks_from}.yml", "handlers/main.yml"):
            p = os.path.join(DEPLOY, "roles", name, sub)
            if os.path.isfile(p) and p not in seen:
                seen.add(p)
                walk(load(p), p)

    def walk(tasks, f):
        for t in tasks or []:
            if not isinstance(t, dict):
                continue
            for k in ("block", "rescue", "always"):
                walk(t.get(k), f)
            for inc in ("include_tasks", "import_tasks", "ansible.builtin.include_tasks",
                        "ansible.builtin.import_tasks"):
                v = t.get(inc)
                v = v.get("file") if isinstance(v, dict) else v
                if isinstance(v, str) and "{{" not in v:
                    p = os.path.normpath(os.path.join(os.path.dirname(f), v))
                    if os.path.isfile(p) and p not in seen:
                        seen.add(p)
                        walk(load(p), p)
            for rk in ("include_role", "import_role", "ansible.builtin.include_role",
                       "ansible.builtin.import_role"):
                if rk in t:
                    role(t[rk]["name"], t[rk].get("tasks_from", "main"))
            out.append((f, t))

    def playbook(p):
        for play in load(p):
            if "import_playbook" in play:
                playbook(os.path.normpath(os.path.join(os.path.dirname(p), play["import_playbook"])))
                continue
            for sec in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                walk(play.get(sec), p)
            for r in play.get("roles") or []:
                role(r if isinstance(r, str) else r.get("role") or r.get("name"))
    playbook(os.path.join(os.path.dirname(DEPLOY), "site.yml"))
    return out


def check_probes(check):
    """STATUS: FIXED — see #184"""
    tasks = site_tasks()
    # Общее свойство: command/shell, чей результат читает другая задача,
    # в check-режиме обязан выполняться.
    for f, t in tasks:
        reg = t.get("register")
        if not reg or not CMD_MODULES & set(t) or t.get("check_mode") is False:
            continue
        readers = [o.get("name") for _, o in tasks if o is not t
                   and re.search(r"\b" + re.escape(reg) + r"\b", yaml.safe_dump(o))]
        check(f"{os.path.relpath(f, DEPLOY)}: {t.get('name')!r} registers {reg}, read by "
              f"{readers}: it must run in check mode", not readers)
    # Семь проб поимённо: только читают, в настоящем прогоне не «changed».
    by = {(os.path.relpath(f, os.path.join(DEPLOY, "roles")), t.get("name")): t for f, t in tasks}
    for path, name, reg, commands in PROBES:
        t = by.get((path, name))
        if t is None:
            check(f"probe {path}: {name!r} exists", False)
            continue
        mod = next(k for k in t if k in CMD_MODULES)
        text = t[mod]["cmd"] if isinstance(t[mod], dict) else t[mod]
        check(f"probe {name!r}: register {reg}", t.get("register") == reg, t.get("register"))
        check(f"probe {name!r}: check_mode: false", t.get("check_mode") is False)
        check(f"probe {name!r}: changed_when: false", t.get("changed_when") is False)
        missing = [c for c in commands if c not in " ".join(text.split())]
        check(f"probe {name!r}: runs {commands}", not missing, missing)
        check(f"probe {name!r}: changes nothing", not WRITES.search(text), WRITES.search(text))


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
    # ── кому что из настроек (#176) ───────────────────────────────────────
    scoped = getattr(config, "SERVER_SCOPED", {})
    check("config.SERVER_SCOPED names every server unit",
          sorted(scoped) == sorted(UNITS), sorted(scoped))
    reads = set().union(*(settings_read(p) for p in CLUSTER_READS))
    missing = sorted(unit_env(reads) - set(scoped.get("mop-cluster", ())))
    check("mop-cluster gets every setting its code reads", not missing, missing)
    for unit, names in scoped.items():
        check(f"{unit}: settings, not secrets, in the unit",
              not [n for n in names if re.search(r"TOKEN|PASS|SECRET", n)], names)
        check(f"{unit}: every name is a setting",
              all(n in config.SETTINGS for n in names), names)
    from mop import playvars
    check("playvars exports SERVER_SCOPED",
          playvars.playbook_vars().get("MOP_SERVER_SCOPED") ==
          {u: list(v) for u, v in scoped.items()})
    variables = {**{k: v for k, v in config.SETTINGS.items() if v}, **VARS,
                 "MOP_SERVER_SCOPED": {u: list(v) for u, v in scoped.items()}}

    for unit, role in UNITS.items():
        tmpl, uvars = unit_task(role, unit)
        check(f"{unit}: the role installs it", tmpl is not None)
        if tmpl is None:
            continue
        check(f"{unit}: from the common template",
              tmpl.get("src", "").endswith("common/templates/service.j2"), tmpl.get("src"))
        own = os.path.join(DEPLOY, "roles", role, "templates", f"{unit}.service.j2")
        check(f"{unit}: its own template is gone", not os.path.exists(own))
        # Набор env -- из config.SERVER_SCOPED, не рукописным списком задачи
        # (#176); тот же, что был, плюс добавленное этим тикетом.
        check(f"{unit}: no hand-typed env list in the task",
              "env" not in (uvars.get("unit") or {}), (uvars.get("unit") or {}).get("env"))
        was = re.findall(r"^Environment=([A-Z_]+)=", PINNED[unit], re.M)
        now = list(scoped.get(unit, ())) + ["PYTHONUNBUFFERED"]
        want = was + list(ADDED.get(unit, ()))
        check(f"{unit}: the env set as before plus the additions",
              sorted(now) == sorted(want), f"{sorted(now)} != {sorted(want)}")
        if can_render and os.path.isfile(template):
            got = render(open(template).read(), {**variables, **uvars})
            # Environment= так, как его читает systemd (#185): присваивания
            # через пробел, кавычки снимаются; слово без «=» systemd
            # отбрасывает с предупреждением. Каждое значение -- целиком.
            name = scoped[unit][0]
            for value in AWKWARD:
                try:
                    seen = environment(render(open(template).read(),
                                              {**variables, **uvars, name: value}))
                except ValueError as e:  # незакрытая кавычка: строку не разобрать
                    seen = {name: f"unparsable: {e}"}
                check(f"{unit}: systemd reads {value!r} whole", seen.get(name) == value,
                      seen.get(name))
            if unit not in ADDED:
                check(f"{unit}: rendered byte for byte as before", got == PINNED[unit],
                      "\n" + "\n".join(f"  -{a!r}\n  +{b!r}" for a, b in
                                       zip(PINNED[unit].splitlines(), got.splitlines()) if a != b))
                continue
            # Юнит с добавками: без комментариев и без добавленных строк он
            # тот же, что был, а добавленные строки -- со значениями игры.
            # Добавленная строка узнаётся по имени, разобранному как у systemd:
            # значение с пробелом (git identity, #167) идёт в кавычках (#185).
            def named(line):
                return line.startswith("Environment=") and \
                    set(environment(line)) & set(ADDED[unit])
            bare = [l for l in got.splitlines() if not l.startswith("#")]
            check(f"{unit}: as before but for the added lines",
                  [l for l in bare if not named(l)] ==
                  [l for l in PINNED[unit].splitlines() if not l.startswith("#")])
            seen = environment(got)
            wrong = {n: seen.get(n) for n in ADDED[unit] if seen.get(n) != variables[n]}
            check(f"{unit}: every addition rendered", not wrong, wrong)

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

    check_probes(check)

    print(f"deploy: {cases - bad}/{cases}" + (" FAILED" if bad else " ok"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
