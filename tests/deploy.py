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
Размеры тела при сборке (#192): pve-build.yml читал размеры и потолки тела
только прописными -- переменными установки (--extra-vars). Инвентарь пишет
их строчными (mop_mem_mb, mop_body_mem_cap_mb), и так же их ищет шаблон
node.env; сборка строки хоста не видела, и тела шли с размерами установки.
Комментарий плейбука при этом обещал «узел > проект > установка > дефолт».

HYPOTHESIS: выражения задачи «How big a body of this project may be here»
ссылаются на MOP_* напрямую, мимо hostvars.
SOLUTION: одна задача перед ней собирает node_body по правилу node.env --
строчная переменная хоста, иначе значение установки; размеры и потолки
берутся только из неё. Просьба проекта стоит между: потолок узла -- min(),
размер -- просьба, иначе node_body.
STATUS: FIXED — see #192

Ложные changed в `mop deploy --check` (#191), обе задачи роли common:

  * загрузка nomad: зеркало на If-Modified-Since отвечает 200, а не 304, и
    get_url без checksum в check mode шлёт HEAD, пустой временный файл
    сравнивает по sha1 с настоящим -- changed всегда; настоящий прогон при
    этом каждый раз качал zip целиком GET'ом, находил тот же sha1 и говорил ok;
  * pip: модуль в check mode при любом extra_args отвечает changed=True, не
    глядя (ansible-core 2.21, pip.py), а у нас --user --break-system-packages.

HYPOTHESIS: оба changed -- семантика модулей в check mode, не дрейф.
SOLUTION: nomad -- проба `nomad version` (check_mode: false, ничего не
меняет); загрузка и распаковка -- только если стоит не та версия. pip --
настоящая задача как была, но только вне check mode; в check mode её
близнец без extra_args, и модуль честно смотрит pip list (все site, что видит
пользователь пула; PIP_USER сузил бы список до user site и соврал бы про
системный requests).
STATUS: FIXED — see #191

Переменная цикла у include чужих файлов (#194): цикл include_tasks по файлам
установки (MOP_BODY_EXTRA) и проектов (sandbox_tasks манифеста) держал `item`,
и вложенный цикл внутри такого файла её затенял -- ansible печатал «The loop
variable 'item' is already in use» (задача «Old skills go before the new ones»
из setup.yaml установки), а задача файла, сославшаяся бы на внешний item,
молча получила бы внутренний.

HYPOTHESIS: у внешних include-циклов в pve-build.yml и body.yml нет
loop_control.loop_var, и путь они строят из `item`.
SOLUTION: своё имя переменной цикла у каждого include чужого файла (путь
include -- шаблон, а не файл репозитория), и `item` в задаче не встречается.
"""
import ast
import json
import os
import re
import shlex
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")
COMMON = os.path.join(DEPLOY, "roles", "common")
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, ROOT)
from mop import config  # noqa: E402
# Что сервис кластера читает сам (#176): его код, клиент Nomad и спецификация,
# а с ней профиль LLM -- job_spec зовёт llm.resolve, и create без --llm
# приходит с profile=None, то есть с умолчанием установки.
CLUSTER_READS = ("mop/cluster.py", "mop/nomad.py", "mop/spec.py", "mop/llm/__init__.py",
                 "mop/natsconf.py")

VARS = {"MOP_USER": "mopuser", "MOP_HOME": "/home/mopuser", "MOP_SERVER_LAN": "10.0.0.1",
        "MOP_NATS_PORT": "4222", "MOP_HTTPS_PORT": "443", "MOP_NOMAD_PORT": "4646",
        "MOP_POOL_DC": "home", "MOP_WEB_PORT": "8080", "MOP_WEB_BIND": "0.0.0.0",
        "MOP_NATS_MONITOR_PORT": "8222",
        "MOP_AUTH_PROVIDER": "file",
        # Провайдер ldap (#208, #214): у пустых по умолчанию -- образцы.
        "MOP_LDAP_URL": "ldaps://ldap.example.dev", "MOP_LDAP_BIND_DN": "cn=mop,ou=services,dc=example,dc=dev",
        "MOP_LDAP_BASE": "dc=example,dc=dev", "MOP_LDAP_GROUP_BASE": "ou=groups,dc=example,dc=dev",
        "MOP_LDAP_ADMIN_GROUP": "cn=mop admins,ou=groups,dc=example,dc=dev",
        "MOP_LDAP_STARTTLS": "no", "MOP_LDAP_CA_FILE": "/etc/ssl/certs/corp-ca.pem"}
# Значения, которые юнит обязан донести целиком (#185): пробел, кавычки,
# обратный слеш. Подставляются вместо настройки из набора юнита.
AWKWARD = ("Pool Bot", 'say "hi"', "a\\b", "tab\there", "it's", "100%", "%h")
UNITS = {"mop-bootstrap": "bootstrap", "mop-cluster": "cluster", "mop-web": "web",
         # auth callout (#206): часть шины, ставит роль bus.
         "mop-callout": "bus"}
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
                         # reload шины с проверкой (#211): /varz на петле.
                         "MOP_NATS_MONITOR_PORT")}
# Несекретные настройки провайдера личностей (#214) -- сервисам, которые его
# строят: callout и глагол личности bootstrap'а. Одним списком
# (config.IDENTITY_SCOPED); что у юнита уже было, в добавки не входит.
ADDED["mop-bootstrap"] = config.IDENTITY_SCOPED
# mop-callout снят заново в #219 (без MOP_AUTH_CALLOUT и MOP_OPERATORS):
# добавок после снимка нет.
ADDED["mop-callout"] = ()

PINNED = {
    'mop-bootstrap': "[Unit]\nDescription=mop-bootstrap (bootstrap песочниц: играет .mop/bootstrap.yaml проекта при каждом старте папета)\nAfter=network-online.target nats.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет: всё, что подписчику и прогону нужно знать об\n# установке, приезжает юнитом. MOP_HOME и MOP_USER -- те, что у узлов: их\n# читают задачи bootstrap'а как переменные прогона, и дефолт сервера\n# (его собственный дом) здесь был бы неправдой.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_HOME=/home/mopuser\nEnvironment=MOP_USER=mopuser\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=/home/mopuser/mop/bin/mop bootstrap serve\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n",
    'mop-cluster': '[Unit]\nDescription=mop-cluster (сервис кластера: Nomad за шиной, глаголы пула на mop.*.cluster.rpc)\nAfter=network-online.target nats.service nomad.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет: что сервису нужно знать об установке, приезжает\n# юнитом. MOP_SERVER_LAN отвечает сразу за адрес шины и за NOMAD_ADDR\n# (config.DERIVED), поэтому второй переменной для Nomad здесь нет.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_HOME=/home/mopuser\nEnvironment=MOP_USER=mopuser\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=/home/mopuser/mop/bin/mop cluster serve\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n',
    'mop-web': '[Unit]\nDescription=mop-web (дашборд пула: состояние по HTTP)\nAfter=network-online.target nats.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет, а адрес сервера обязателен: без него config\n# отказывается работать (и правильно). Окружение старше .env, поэтому\n# юнит и есть источник этой настройки на сервере.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_NOMAD_PORT=4646\nEnvironment=MOP_POOL_DC=home\nEnvironment=PYTHONUNBUFFERED=1\n# Через диспетчер: он ставит PYTHONPATH, без него командлет пакета не найдёт.\nExecStart=/home/mopuser/mop/bin/mop web --port 8080 --bind 0.0.0.0\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n',
}
# Юнит auth callout (#206) -- новый: снимок с его появления, байт в байт.
PINNED["mop-callout"] = '[Unit]\nDescription=mop-callout (auth callout шины: кто входит, решает провайдер личностей)\nAfter=network-online.target nats.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# Шина на петле. Пароль пользователя callout, сиды издателя и xkey --\n# файлами 0600 в /etc/nats, не здесь: юнит читаем всем.\nEnvironment=MOP_NATS_PORT=4222\nEnvironment=MOP_AUTH_PROVIDER=file\nEnvironment=MOP_LDAP_URL=ldaps://ldap.example.dev\nEnvironment=MOP_LDAP_BIND_DN=cn=mop,ou=services,dc=example,dc=dev\nEnvironment=MOP_LDAP_BASE=dc=example,dc=dev\nEnvironment=MOP_LDAP_LOGIN_ATTR=uid\nEnvironment=MOP_LDAP_NAME_ATTR=displayName,cn\nEnvironment=MOP_LDAP_EMAIL_ATTR=mail\nEnvironment=MOP_LDAP_GROUP_BASE=ou=groups,dc=example,dc=dev\nEnvironment=MOP_LDAP_GROUP_FILTER=(|(member={dn})(uniqueMember={dn})(memberUid={login}))\nEnvironment="MOP_LDAP_ADMIN_GROUP=cn=mop admins,ou=groups,dc=example,dc=dev"\nEnvironment=MOP_LDAP_PROJECT_GROUP=mop-{project}\nEnvironment=MOP_LDAP_STARTTLS=no\nEnvironment=MOP_LDAP_CA_FILE=/etc/ssl/certs/corp-ca.pem\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=/home/mopuser/mop/bin/mop callout\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n'
# Снят заново в #219: окружение -- без MOP_AUTH_CALLOUT (callout всегда включён)
# и без MOP_OPERATORS (людей даёт только провайдер).


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
    def flat(tasks):
        # Юнит под условием -- в block (mop-callout, #206): смотрим и внутрь.
        for t in tasks or []:
            yield t
            yield from flat((t or {}).get("block"))
    for f in os.listdir(os.path.join(DEPLOY, "roles", role, "tasks")):
        with open(os.path.join(DEPLOY, "roles", role, "tasks", f)) as fh:
            for t in flat(yaml.safe_load(fh)):
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
    # lookup('vars', имя[, default=]) шаблона -- как у ansible: значение
    # переменной игры, а без неё -- default, если назван.
    missing = object()

    def lookup(kind, name, default=missing):
        if kind != "vars":
            return None
        return variables[name] if default is missing else variables.get(name, default)
    return env.from_string(text).render(**variables, lookup=lookup)


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


# ── #192: размеры тела при сборке — строка хоста раньше установки ─────────────
# Память из них ушла (#197): она свойство папета -- просьба проекта, иначе
# установки, -- и узел о ней говорит только размещением (mop_mem_cap_mb в
# meta Nomad). Тело сборки берёт память по той же цепочке, без узла.
BODY_KNOBS = ("MOP_DISK_GB", "MOP_CORES", "MOP_BODY_DISK_CAP_GB", "MOP_BODY_CORES_CAP")
# Правило node.env (roles/bus/tasks/main.yml): строчная хоста, иначе установка.
NODE_RULE = "hostvars[inventory_hostname][item | lower] | default(lookup('vars', item))"


def check_body_sizes(check, can_render):
    """STATUS: FIXED — see #192"""
    play = yaml.safe_load(open(os.path.join(DEPLOY, "pve-build.yml")))[0]
    by = {t.get("name"): t for t in play["tasks"]}
    sizes = by.get("How big a body of this project may be here")
    node = by.get("What this node says about the size of its bodies")
    check("pve-build: the body's size task exists", sizes is not None)
    check("pve-build: the node's own sizes are read in one task", node is not None)
    if sizes is None:
        return
    exprs = sizes.get("ansible.builtin.set_fact") or {}
    for k in ("body_disk", "body_cores"):
        bare = re.findall(r"(?<![.'\w])MOP_[A-Z_]+(?!')", exprs.get(k, ""))
        check(f"pve-build: {k} takes no installation setting past the host's", not bare, bare)
    check("pve-build: body_mem takes nothing of the node (#197)",
          "node_body" not in exprs.get("body_mem", "") and "hostvars" not in exprs.get("body_mem", ""),
          exprs.get("body_mem"))
    if node is None:
        return
    names = [t.get("name") for t in play["tasks"]]
    check("pve-build: the node's sizes are read before they are used",
          names.index(node["name"]) < names.index(sizes["name"]))
    check("pve-build: every body setting is read, and only those",
          sorted(node.get("loop") or []) == sorted(BODY_KNOBS), node.get("loop"))
    check("pve-build: every body setting is node-scoped",
          set(BODY_KNOBS) <= set(config.NODE_SCOPED))
    fact = (node.get("ansible.builtin.set_fact") or {}).get("node_body", "")
    check("pve-build: the node's sizes are read the way node.env reads them",
          NODE_RULE in " ".join(fact.split()), fact)
    if not can_render:
        return
    from jinja2.nativetypes import NativeEnvironment
    env = NativeEnvironment(undefined=__import__("jinja2").StrictUndefined)
    env.filters["combine"] = lambda a, b: {**a, **b}
    installed = {"MOP_MEM_MB": "12288", "MOP_DISK_GB": "120", "MOP_CORES": "4",
                 "MOP_BODY_MEM_CAP_MB": "32768", "MOP_BODY_DISK_CAP_GB": "400",
                 "MOP_BODY_CORES_CAP": "16"}

    def build(host, asks=None):
        """node_body циклом set_fact, затем размеры -- как их считает ansible."""
        v = {**installed, "inventory_hostname": "hyper", "hostvars": {"hyper": host}}
        if asks is not None:
            v["mop_project_asks"] = asks
        lookup = lambda kind, name: v[name] if kind == "vars" else None  # noqa: E731
        for item in node["loop"]:
            v["node_body"] = env.from_string(fact).render(**v, item=item, lookup=lookup)
        return tuple(int(env.from_string(exprs[k]).render(**v, lookup=lookup))
                     for k in ("body_mem", "body_disk", "body_cores"))
    cases = [
        # Память хоста не читается (#197): строку mop_mem_mb отвергает deploy.
        ("the host's size over the installation's",
         {"mop_mem_mb": "8192", "mop_disk_gb": 50, "mop_cores": "4"}, None, (12288, 50, 4)),
        ("the installation's without a host line", {}, None, (12288, 120, 4)),
        ("the project's ask over the installation's memory", {},
         {"MOP_MEM_MB": "16384"}, (16384, 120, 4)),
        # Потолок узла памяти тела сборки не режет (#197): он -- ограничение
        # размещения папета, а не размер сборки.
        ("the host's cap under the ask", {"mop_body_mem_cap_mb": "4096", "mop_body_cores_cap": 2},
         {"MOP_MEM_MB": "16384", "MOP_CORES": "8"}, (16384, 120, 2)),
        ("the host's cap under its own size", {"mop_disk_gb": "500", "mop_body_disk_cap_gb": "60"},
         None, (12288, 60, 4)),
    ]
    for what, host, asks, want in cases:
        try:
            got = build(host, asks)
        except Exception as e:  # noqa: BLE001 -- проверка, не код пула
            got = f"{type(e).__name__}: {e}"
        check(f"pve-build: {what}", got == want, got)


# ── #194: include-цикл чужих файлов не занимает item ─────────────────────────
INCLUDES = ("ansible.builtin.include_tasks", "include_tasks",
            "ansible.builtin.import_tasks", "import_tasks")
LOOPS = ("loop", "with_items", "with_list", "with_dict", "with_fileglob")


def all_tasks():
    """Все задачи всех плейбуков и файлов задач deploy/, с блоками. -> [(файл, задача)]."""
    out = []

    def walk(f, items):
        for t in items or []:
            if not isinstance(t, dict):
                continue
            for key in ("tasks", "pre_tasks", "post_tasks", "handlers",
                        "block", "rescue", "always"):
                walk(f, t.get(key))
            out.append((f, t))

    for f in deploy_files():
        if f.endswith(".yml"):
            walk(f, yaml.safe_load(open(f)) or [])
    return out


def check_include_loop_var(check):
    """STATUS: FIXED — see #194"""
    found = []
    for f, t in all_tasks():
        mod = next((k for k in INCLUDES if k in t), None)
        if not mod or not any(k in t for k in LOOPS):
            continue
        arg = t[mod]
        path = arg.get("file") if isinstance(arg, dict) else arg
        # Путь-шаблон -- файл не из репозитория: установки или проекта.
        if "{{" not in str(path):
            continue
        where = f"{os.path.relpath(f, DEPLOY)}: {t.get('name')!r}"
        found.append(where)
        var = (t.get("loop_control") or {}).get("loop_var")
        check(f"{where}: has its own loop_var", var not in (None, "item"), var)
        check(f"{where}: does not refer to item",
              not re.search(r"\bitem\b", yaml.safe_dump(t)), yaml.safe_dump(t))
    # Проверка не пустая: три известных include чужих файлов на месте.
    want = ["body.yml: 'Sandbox tasks of each project that has any'",
            "body.yml: 'What this installation adds to a body'",
            "pve-build.yml: 'Task files this installation names'"]
    check("the include loops over foreign files are the known three", sorted(found) == want,
          sorted(found))


# ── reload nats -- по файлу, а не по тексту (#199) ───────────────────────
# Наблюдение (mop.corp.ermak.dev): новый узел wate-wsl, deploy переписал
# /etc/nats/users.conf, а nats не перезагружался с 23.09 -- агент узла
# получил «Authorization Violation», deploy упал на «Confirm the agent is
# connected».
# HYPOTHESIS: задача `mop cluster users` решала о changed по «, changed» в
# выводе, а #182 сделал команду молчащей на успехе: changed не бывает
# никогда, хендлер reload nats не зовётся.
# SOLUTION: решает файл: stat users.conf с контрольной суммой до команды и
# после, changed у второго stat -- суммы разошлись, notify reload nats там же.
# Сама команда changed не объявляет. Остальные читатели вывода командлетов --
# только pve-facts (JSON, #159/#179/#182 его не трогали), список закреплён.
# RESULT: до правки красные 5 из 8 (включая закреплённый список: в нём был
# cluster users). Живой прогон трёх задач в песочнице (ansible-core 2.21.4,
# файл вместо /etc/nats/users.conf): файл сменился -- changed и хендлер,
# не сменился -- ok без хендлера, файла не было -- хендлер, --check -- ok.
# STATUS: FIXED — see #199
TOUCHED = ("cluster users", "driver build", "sweep", "update", "llm", "doctor",
           "gc", "restart", "node drain", "node forget", "master", "setup", "mcp")


def mop_command(t):
    """Команда `bin/mop ...` задачи словами после bin/mop, или None."""
    for mod in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        arg = t.get(mod)
        if arg is None:
            continue
        if isinstance(arg, dict):
            words = [str(w) for w in arg.get("argv") or []] or \
                str(arg.get("cmd", "")).split()
        else:
            words = str(arg).split()
        for i, w in enumerate(words):
            if w.endswith("bin/mop") or w.endswith("bin/mop'"):
                return " ".join(x for x in words[i + 1:i + 3] if not x.startswith("-"))
    return None


def stdout_readers():
    """Задачи, читающие вывод командлета: [(файл, команда, регистр)]."""
    text = "\n".join(l for f in deploy_files() if f.endswith(".yml")
                     for l in open(f).read().splitlines()
                     if not l.lstrip().startswith("#"))
    out = []
    for f, t in all_tasks():
        cmd, reg = mop_command(t), t.get("register")
        if cmd and reg and re.search(rf"\b{reg}\s*(\.|\[')stdout", text):
            out.append((os.path.relpath(f, DEPLOY), cmd, reg))
    return sorted(out)


def check_users_reload_199(check):
    """users.conf менялся -- nats перезагружается; решает контрольная сумма."""
    # Кто ещё читает вывод командлета: список закреплён. pve-facts печатает
    # JSON -- данные, не отчёт, и выводом #159/#179/#182 не тронут.
    found = stdout_readers()
    check("#199: commandlet output read only by the pinned tasks", found == [
        ("pve-build.yml", "driver pve-facts", "build_facts"),
        ("pve-build.yml", "driver pve-facts", "node_facts"),
        ("roles/pve/tasks/main.yml", "driver pve-facts", "node_facts")], found)
    check("#199: no task reads the output of a commandlet made quiet",
          not [x for x in found if x[1] in TOUCHED], found)
    check("#199: mop_command sees cluster users, cluster check, bootstrap check",
          {"cluster users", "cluster check", "bootstrap check"} <=
          {mop_command(t) for _, t in all_tasks()},
          sorted({c for c in (mop_command(t) for _, t in all_tasks()) if c}))

    f = os.path.join(DEPLOY, "roles", "bus", "tasks", "projects.yml")
    tasks = yaml.safe_load(open(f)) or []
    at = [i for i, t in enumerate(tasks) if mop_command(t) == "cluster users"]
    check("#199: one task runs mop cluster users", len(at) == 1, at)
    if len(at) != 1:
        return
    i, run = at[0], tasks[at[0]]
    check("#199: its changed does not read the command's output",
          "stdout" not in str(run.get("changed_when", "")), run.get("changed_when"))

    def stat_of(t):
        st = t.get("ansible.builtin.stat") or {}
        return st if str(st.get("path", "")).endswith("/etc/nats/users.conf") \
            and st.get("get_checksum", True) is not False else None
    before = [t for t in tasks[:i] if stat_of(t) and t.get("register")]
    after = [t for t in tasks[i + 1:] if stat_of(t) and t.get("register")]
    check("#199: users.conf is stat'ed before the command", len(before) == 1,
          [t.get("name") for t in before])
    check("#199: and after it", len(after) == 1, [t.get("name") for t in after])
    if len(before) != 1 or len(after) != 1:
        return
    b, a = before[0]["register"], after[0]["register"]
    decide = [t for t in tasks[i + 1:] if "reload nats" in str(t.get("notify", ""))]
    check("#199: one task after the command notifies reload nats", len(decide) == 1,
          [t.get("name") for t in decide])
    check("#199: the command itself notifies nothing", not run.get("notify"),
          run.get("notify"))
    if decide:
        cw = str(decide[0].get("changed_when", ""))
        check("#199: changed is the checksums before and after differing",
              f"{b}.stat.checksum" in cw and f"{a}.stat.checksum" in cw
              and "!=" in cw and "stdout" not in cw, cw)


def check_identity_copy_167(check, ptasks):
    """HYPOTHESIS (#167): глагол identity (mop-bootstrap) читает файл
    операторов копией в /etc/nats/identity -- secrets/ контроллера пользователю
    пула закрыт.
    SOLUTION: копию кладёт роль bus. STATUS: FIXED — see #167

    С #219 callout включён всегда, и копия есть всегда: людей даёт только
    провайдер, а сервисы читают его копией. В ней -- файл операторов, если
    он есть у контроллера, и пароль LDAP при провайдере ldap (#214);
    переходных паролей людей (nats-op-*.pass) больше нет.

    Условия when задач роли -- простые выражения; проверка исполняет их на
    сочетаниях (файл операторов, провайдер) и смотрит, какие задачи пошли.
    Берутся задачи роли, касающиеся копии или файла операторов: у прочих
    свои переменные."""
    import itertools
    import types
    ptasks = [t for t in ptasks if "/etc/nats/identity" in json.dumps(t)
              or "operators_file" in json.dumps(t)]

    def runs(task, env):
        conds = task.get("when", [])
        for c in conds if isinstance(conds, list) else [conds]:
            try:
                if not eval(str(c), {}, dict(env)):   # noqa: S307 -- свой yaml
                    return False
            except Exception as e:  # noqa: BLE001 -- выражение не читается
                raise AssertionError(f"when {c!r}: {e}")
        return True

    def ran(tasks, env, out):
        for t in tasks:
            if not runs(t, env):
                continue
            if "block" in t:
                ran(t["block"], env, out)
            else:
                out.append(t)
        return out

    def target(t):
        for mod in ("ansible.builtin.copy", "ansible.builtin.file", "ansible.builtin.stat"):
            if mod in t:
                return mod.rsplit(".", 1)[1], (t[mod] or {}).get("dest") or (t[mod] or {}).get("path"), \
                    (t[mod] or {}).get("state")
        return None, None, None

    for exists, prov in itertools.product((True, False), ("file", "ldap")):
        env = {"MOP_AUTH_PROVIDER": prov,
               "operators_file": types.SimpleNamespace(stat=types.SimpleNamespace(exists=exists))}
        try:
            done = [target(t) for t in ran(ptasks, env, [])]
        except AssertionError as e:
            check(f"bus identity copy, file {exists}, {prov}: conditions read", False, e)
            continue
        what = f"bus identity copy, operators file {exists}, provider {prov}"
        check(f"{what}: the copy's directory is always laid (#219)",
              ("file", "/etc/nats/identity", "directory") in done, done)
        check(f"{what}: the copy is never removed as a whole (#219)",
              ("file", "/etc/nats/identity", "absent") not in done, done)
        check(f"{what}: the operators file is copied exactly when it exists",
              (("copy", "/etc/nats/identity/operators", None) in done) == exists, done)
        check(f"{what}: the LDAP bind password is laid exactly with ldap",
              (("copy", "/etc/nats/identity/ldap-bind.pass", None) in done) == (prov == "ldap"), done)
        check(f"{what}: another provider leaves no bind password",
              (("file", "/etc/nats/identity/ldap-bind.pass", "absent") in done) == (prov != "ldap"),
              done)
        check(f"{what}: the controller's operators file is looked at",
              any(k == "stat" and "operators" in str(p) for k, p, _ in done), done)


def check_one_source_219(check):
    """HYPOTHESIS (#219): deploy заводит людям пароли (nats-op-*.pass),
    кладёт их в base-users.json и копию личностей, а выключатель
    MOP_AUTH_CALLOUT держит вторую дорогу -- статических людей.
    SOLUTION: люди только из провайдера; callout всегда включён.
    STATUS: FIXED — see #219"""
    for path in deploy_files():
        text = open(path).read()
        for gone in ("MOP_AUTH_CALLOUT", "MOP_OPERATORS ", "MOP_OPERATORS\n", "MOP_OPERATORS'",
                     'MOP_OPERATORS"', "MOP_OPERATORS}", "MOP_OPERATOR_SUBJECTS", "nats-op-",
                     "mop_operators_base"):
            check(f"{os.path.relpath(path, DEPLOY)}: no {gone.strip()!r} (#219)", gone not in text)


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
        # И в кавычках (#185): значение с пробелом systemd иначе разрезал бы.
        was = re.findall(r'^Environment="?([A-Z_]+)=', PINNED[unit], re.M)
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

    # ── check mode без ложных changed (#191) ─────────────────────────────
    def tasks(name):
        path = os.path.join(COMMON, "tasks", name)
        return yaml.safe_load(open(path)) or [] if os.path.isfile(path) else []

    def when(t):
        w = t.get("when", [])
        return " and ".join(w) if isinstance(w, list) else str(w)

    nb = tasks("nomad_bin.yml")
    probe = [t for t in nb if "nomad version" in str(t.get("ansible.builtin.command", ""))]
    check("nomad_bin: a version probe", len(probe) == 1, [t.get("name") for t in probe])
    if probe:
        p = probe[0]
        check("nomad_bin: the probe runs in check mode and changes nothing",
              p.get("check_mode") is False and p.get("changed_when") is False
              and p.get("failed_when") is False and p.get("register"), p)
        reg = p.get("register", "")
        for mod in ("ansible.builtin.get_url", "ansible.builtin.unarchive"):
            t = [t for t in nb if mod in t]
            check(f"nomad_bin: {mod} only when the version is not the wanted one",
                  len(t) == 1 and reg in when(t[0]) and "nomad_version" in when(t[0]),
                  [when(x) for x in t])
    pp = [t for t in tasks("pip.yml") if "ansible.builtin.pip" in t]
    real = [t for t in pp if t["ansible.builtin.pip"].get("extra_args")]
    dry = [t for t in pp if not t["ansible.builtin.pip"].get("extra_args")]
    check("pip: the real task keeps --user --break-system-packages, outside check mode",
          len(real) == 1 and real[0]["ansible.builtin.pip"]["extra_args"] ==
          "--user --break-system-packages" and when(real[0]) == "not ansible_check_mode",
          [(t["ansible.builtin.pip"].get("extra_args"), when(t)) for t in real])
    check("pip: a check-mode twin without extra_args", len(dry) == 1
          and when(dry[0]) == "ansible_check_mode", [when(t) for t in dry])
    if len(real) == 1 and len(dry) == 1:
        a, b = real[0], dry[0]
        same = ("become", "become_user")
        check("pip: the twin asks as the same user about the same list",
              all(a.get(k) == b.get(k) for k in same)
              and {k: v for k, v in a["ansible.builtin.pip"].items() if k != "extra_args"}
              == b["ansible.builtin.pip"],
              (a, b))

    # ── хосты инвентаря -- файлом сервису кластера (#178) ─────────────────
    # forget отказывает узлу, который deploy поставил бы снова; файла нет --
    # отказа нет, поэтому разошедшееся имя файла ломало бы защиту молча.
    from mop import cluster
    want = "{{ MOP_HOME }}/.config/mop/" + os.path.basename(
        getattr(cluster, "INVENTORY_HOSTS", "") or "?")
    wrote = [t for f, t in site_tasks() if f.endswith("roles/cluster/tasks/main.yml")
             and "mop_inventory_hosts" in str(t.get("ansible.builtin.copy", {}).get("content"))]
    check("cluster: the role writes the inventory's hosts where the service reads them",
          len(wrote) == 1 and wrote[0]["ansible.builtin.copy"].get("dest") == want
          and wrote[0]["ansible.builtin.copy"].get("owner") == "{{ MOP_USER }}",
          [t.get("ansible.builtin.copy") for t in wrote] or want)
    check("cluster: the host list only when deploy sent one",
          len(wrote) == 1 and "mop_inventory_hosts is defined" in when(wrote[0]),
          [when(t) for t in wrote])

    # ── просьбы проектов -- файлом сервису кластера (#197) ────────────────
    # Спеку строит сервис под учёткой пула, а `.mop` проектов читает deploy на
    # контроллере. Файла нет -- у каждого проекта память установки, поэтому
    # разошедшееся имя файла отменило бы просьбы молча.
    from mop import spec
    want = "{{ MOP_HOME }}/.config/mop/" + os.path.basename(spec.ASKS_FILE)
    wrote = [t for f, t in site_tasks() if f.endswith("roles/cluster/tasks/main.yml")
             and "mop_manifests" in str(t.get("ansible.builtin.copy", {}).get("content"))]
    check("cluster: the role writes the projects' asks where the spec reads them",
          len(wrote) == 1 and wrote[0]["ansible.builtin.copy"].get("dest") == want
          and wrote[0]["ansible.builtin.copy"].get("owner") == "{{ MOP_USER }}",
          [t.get("ansible.builtin.copy") for t in wrote] or want)
    check("cluster: the asks only when deploy sent the manifests",
          len(wrote) == 1 and "mop_manifests is defined" in when(wrote[0]),
          [when(t) for t in wrote])
    if can_render and len(wrote) == 1:
        import jinja2
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters["to_json"] = json.dumps
        env.filters["dict2items"] = lambda d: [{"key": k, "value": v} for k, v in d.items()]
        manifests = {"mop": {"asks": {"MOP_MEM_MB": "6144"}, "sandbox_vars": "/x"},
                     "rugent": {"asks": {}}}
        try:
            got = json.loads(env.from_string(wrote[0]["ansible.builtin.copy"]["content"])
                             .render(mop_manifests=manifests))
        except Exception as e:  # noqa: BLE001 -- проверка, не код пула
            got = f"{type(e).__name__}: {e}"
        check("cluster: the asks file is {project: asks}, nothing else of the manifest",
              got == {"mop": {"MOP_MEM_MB": "6144"}, "rugent": {}}, got)

    # ── потолок памяти узла -- в meta Nomad (#197) ───────────────────────
    # Спека папета требует `${meta.mop_mem_cap_mb} >= потолок`: ключ, которого
    # на узле нет, ограничение не проходит, а имя, разошедшееся со спекой,
    # закрыло бы пул целиком. Значение -- по правилу node.env: строка хоста,
    # иначе установка.
    from mop import spec
    hcl_path = os.path.join(DEPLOY, "roles", "nomad", "templates", "client.hcl.j2")
    hcl = open(hcl_path).read()
    check("client.hcl: the node's memory cap is in its meta under the spec's key",
          re.search(r"^\s*" + re.escape(spec.META_MEM_CAP) + r"\s*=", hcl, re.M) is not None,
          spec.META_MEM_CAP)
    if can_render:
        base = {"inventory_hostname": "hyper", "MOP_POOL_DC": "pool", "MOP_SERVER_LAN": "10.0.0.1",
                "MOP_NOMAD_RPC_PORT": "4647", "MOP_DRIVER": "host", "MOP_BODY_MEM_CAP_MB": "32768"}
        for what, host, want in (("the installation's cap", {}, "32768"),
                                 ("the host's own cap", {"mop_body_mem_cap_mb": "65536"}, "65536"),
                                 ("the host's cap from YAML as a number",
                                  {"mop_body_mem_cap_mb": 65536}, "65536")):
            try:
                out = render(hcl, {**base, **host})
                got = re.findall(r'^\s*' + re.escape(spec.META_MEM_CAP) + r'\s*=\s*"([^"]*)"', out, re.M)
            except Exception as e:  # noqa: BLE001 -- проверка, не код пула
                got = f"{type(e).__name__}: {e}"
            check(f"client.hcl: {what}", got == [want], got)

    # ── ssh-порт узла -- из ansible_port инвентаря (#201) ────────────────
    # Порт, которым сервер ходит на узел, знал только ssh config root'а на
    # контроллере: bootstrap (пользователь пула) шёл на 22 и получал отказ.
    # ansible_port -- стандартная переменная: её же берёт сам прогон, и
    # node.env рендерит её в MOP_SSH_PORT; нет её -- порт установки (22).
    # STATUS: FIXED — see #201
    bus_tasks = yaml.safe_load(open(os.path.join(DEPLOY, "roles", "bus", "tasks", "main.yml")))
    node_env = next((t for t in bus_tasks
                     if t.get("name") == "What this node knows about itself"), None)
    check("bus: the node.env task is found", node_env is not None)
    if can_render and node_env is not None:
        content = node_env["ansible.builtin.copy"]["content"]
        scoped = ",".join(config.NODE_SCOPED)
        installed = {k: config.SETTINGS.get(k, "") for k in config.NODE_SCOPED}
        for what, host, want in (("the host's ansible_port", {"ansible_port": 2222}, "2222"),
                                 ("no ansible_port: the installation's", {}, "22"),
                                 ("the host's mop_driver still read",
                                  {"mop_driver": "pve"}, "22")):
            try:
                out = render(content, {**installed, "MOP_NODE_SCOPED": scoped,
                                       "inventory_hostname": "wsl",
                                       "hostvars": {"wsl": host}})
                lines = dict(ln.split("=", 1) for ln in out.splitlines()
                             if ln and not ln.startswith("#"))
                got = (lines.get("MOP_SSH_PORT"), lines.get("MOP_DRIVER"))
            except Exception as e:  # noqa: BLE001 -- проверка, не код пула
                got = f"{type(e).__name__}: {e}"
            check(f"node.env: MOP_SSH_PORT from {what}", got ==
                  (want, host.get("mop_driver", installed.get("MOP_DRIVER"))), got)

    # ── callout.conf -- рестарт, users.conf -- reload (#206) ────────────
    # Блок auth_callout nats reload'ом не берёт и валит весь reload (стенд
    # #206); смену его файла прогон обязан отдать рестарту.
    # STATUS: FIXED — see #206
    ptasks = yaml.safe_load(open(os.path.join(DEPLOY, "roles", "bus", "tasks", "projects.yml")))
    by = {t.get("name"): t for t in ptasks}
    after = by.get("Callout file of the bus, after") or {}
    check("bus: a changed callout.conf restarts nats",
          (after.get("ansible.builtin.stat") or {}).get("path") == "/etc/nats/callout.conf"
          and after.get("notify") == "restart nats", after)
    check("bus: a changed users.conf still reloads nats",
          (by.get("Users file of the bus, after") or {}).get("notify") == "reload nats")
    users_task = by.get("Users file of the bus, written by the server") or {}
    names = [t.get("name") for t in ptasks]
    check("bus: the callout file is read before and after mop cluster users",
          names.index("Callout file of the bus, before") < names.index(users_task.get("name"))
          < names.index("Callout file of the bus, after") if after and users_task else False)
    check_identity_copy_167(check, ptasks)
    check_one_source_219(check)
    conf = open(os.path.join(DEPLOY, "roles", "bus", "templates", "nats-server.conf.j2")).read()
    check("nats-server.conf includes callout.conf inside authorization",
          re.search(r"authorization \{[^}]*include \./callout\.conf", conf) is not None)

    # ── пароль LDAP сервисам сервера -- файлом 0600 (#214) ───────────────
    # Окружение юнита читаемо всем, поэтому секрет провайдера -- файлом рядом
    # с копией личностей, и только когда провайдер -- ldap.
    # STATUS: FIXED — see #214
    def every(tasks):
        for t in tasks or []:
            yield t, t.get("when")
            for sub in every((t or {}).get("block")):
                yield sub[0], [t.get("when"), sub[1]]
    bind = "/etc/nats/identity/ldap-bind.pass"
    found = [(t, w) for t, w in every(ptasks)
             if ((t.get("ansible.builtin.copy") or {}).get("dest") == bind)]
    check("bus: one task writes the LDAP bind password file", len(found) == 1, len(found))
    if found:
        t, w = found[0]
        cp = t["ansible.builtin.copy"]
        check("bus: the bind password file is 0600, the pool user's",
              cp.get("mode") == "0600" and cp.get("owner") == "{{ MOP_USER }}", cp)
        check("bus: the bind password never reaches the log", t.get("no_log") is True, t)
        check("bus: the bind password comes from the play's env, not extra-vars",
              "lookup('env', 'MOP_LDAP_BIND_PASSWORD')" in str(cp.get("content")), cp.get("content"))
        # Внутри блока копии личностей (#167: при callout или при файле
        # операторов) -- файл пароля следует его условию, своего нет.
        block = next((b for b in ptasks if b.get("name") == "Identity for the server's services"), {})
        check("bus: the bind password file only with the ldap provider, inside the identity copy",
              "MOP_AUTH_PROVIDER == 'ldap'" in str(w) and block.get("when") in (w or [None])
              and t in (block.get("block") or []), w)
    gone = [(t, w) for t, w in every(ptasks)
            if (t.get("ansible.builtin.file") or {}).get("path") == bind
            and (t.get("ansible.builtin.file") or {}).get("state") == "absent"]
    check("bus: no bind password file without the ldap provider",
          len(gone) == 1 and "MOP_AUTH_PROVIDER != 'ldap'" in str(gone[0][1]), gone)

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
    check_body_sizes(check, can_render)
    check_include_loop_var(check)
    check_users_reload_199(check)

    print(f"deploy: {cases - bad}/{cases}" + (" FAILED" if bad else " ok"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
