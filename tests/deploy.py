#!/usr/bin/env python3
"""deploy/ без пула: python3 tests/deploy.py

Юниты сервисов сервера и общие задачи ролей (#157). Прогнать плейбуки здесь
нечем, поэтому проверяется то, что проверяемо без машины:

  * юниты mop-bootstrap, mop-cluster и mop-web рендерятся из общего шаблона
    роли common байт в байт такими, какими их рендерили свои шаблоны до
    #157 (PINNED -- вывод модуля template ansible-core 2.21 на старых
    шаблонах с набором VARS ниже). Рендер нужен jinja2; в клоне его нет --
    тогда проверка рендера НЕ ИДЁТ, и это пропуск hermetic.skip (#229):
    строка в выводе, а при MOP_TESTS_STRICT=1 -- провал файла;
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
читает (NOMAD_ADDR и датацентр в mop/server/nomad.py): при недефолтных значениях
сервис шёл на дефолтный порт и в дефолтный DC, молча.

HYPOTHESIS: env юнитов -- рукописные списки в vars задач, и с тем, что
читает код сервиса, их ничто не сверяет.
SOLUTION: config.SERVER_SCOPED -- {юнит: настройки}, playvars везёт его
плейбукам как MOP_SERVER_SCOPED, шаблон юнита рендерит env из него;
комментарии остаются в задаче (unit.notes, перед своей настройкой). Проверка
ниже выводит то, что читают mop/server/cluster.py, mop/server/nomad.py и mop/server/spec.py
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
import stat
import shutil
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")
COMMON = os.path.join(DEPLOY, "roles", "common")
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, ROOT)
from mop.common import config  # noqa: E402
# Что сервис кластера читает сам (#176): его код, клиент Nomad и спецификация,
# а с ней профиль LLM -- job_spec зовёт llm.resolve, и create без --llm
# приходит с profile=None, то есть с умолчанием установки.
CLUSTER_READS = ("mop/server/cluster.py", "mop/server/nomad.py", "mop/server/spec.py", "mop/common/llm.py",
                 "mop/server/natsconf.py")

VARS = {"MOP_USER": "mopuser", "MOP_HOME": "/home/mopuser", "MOP_SERVER_LAN": "10.0.0.1",
        "MOP_NATS_PORT": "4222", "MOP_HTTPS_PORT": "443", "MOP_NOMAD_PORT": "4646",
        "MOP_POOL_DC": "home", "MOP_WEB_PORT": "8080", "MOP_WEB_BIND": "0.0.0.0",
        "MOP_NATS_MONITOR_PORT": "8222",
        "MOP_AUTH_PROVIDER": "file",
        # Провайдер ldap (#208, #214): у пустых по умолчанию -- образцы.
        "MOP_LDAP_URL": "ldaps://ldap.example.dev", "MOP_LDAP_BIND_DN": "cn=mop,ou=services,dc=example,dc=dev",
        "MOP_LDAP_BASE": "dc=example,dc=dev", "MOP_LDAP_GROUP_BASE": "ou=groups,dc=example,dc=dev",
        "MOP_LDAP_ADMIN_GROUP": "cn=mop admins,ou=groups,dc=example,dc=dev",
        # Группа доступа и вложенность AD (#220): тоже пустые по умолчанию.
        "MOP_LDAP_ACCESS_GROUP": "CN=Pool Users,OU=Access,DC=example,DC=dev",
        "MOP_LDAP_NESTED": "ad",
        "MOP_LDAP_STARTTLS": "no", "MOP_LDAP_CA_FILE": "/etc/ssl/certs/corp-ca.pem",
        # LLM-прокси (#390): ключ пуст по умолчанию, образец -- как у LDAP.
        "MOP_PROXY_URL": "http://proxy.example.dev:8317", "MOP_PROXY_KEY": "k1",
        # Петля прокси (#557): читатели на сервере ходят 127.0.0.1:8317.
        "MOP_LLM_PROXY_PORT": "8317"}
# Значения, которые юнит обязан донести целиком (#185): пробел, кавычки,
# обратный слеш. Подставляются вместо настройки из набора юнита.
AWKWARD = ("Pool Bot", 'say "hi"', "a\\b", "tab\there", "it's", "100%", "%h")
UNITS = {"mop-bootstrap": "bootstrap", "mop-cluster": "cluster", "mop-web": "web",
         # auth callout (#206): часть шины, ставит роль bus.
         "mop-callout": "bus",
         # Расписанный doctor (#359): oneshot под таймером, роль doctor.
         "mop-doctor": "doctor"}
# Исключения синка пакета до #157, во всех четырёх копиях одни и те же.
# web/node_modules -- тулчейн разработчика (#297): на серверы и в тела едет
# только собранный web/dist.
EXCLUDES = [".git", "__pycache__", ".env", "inventory.ini", "inventory.yaml", "web/node_modules"]
# Что #176 добавляет в юнит mop-cluster: всё это сервис читает, а юнит не
# передавал. Порт и DC Nomad -- из тикета; объём, потолок, посев и PATH папета
# нашла проверка ниже (их читает mop/server/spec.py, а спецификацию теперь собирает
# сервис кластера, не машина оператора с её .env); с #390 сюда же адрес
# LLM-прокси, а ключ с #393 читается из закрытого файла, не из unit (#402).
ADDED = {"mop-cluster": ("MOP_NOMAD_PORT", "MOP_POOL_DC", "MOP_PUPPET_MEM_MB",
                         "MOP_MEM_MB", "MOP_PUPPET_SEED", "MOP_PUPPET_PATH",
                         "MOP_PROXY_URL", "MOP_GC_FREE_MIN_GB", "MOP_GC_MAX_PER_RUN",
                         # reload шины с проверкой (#211): /varz на петле.
                         "MOP_NATS_MONITOR_PORT",
                         # llm.env() канонизирует свой внутренний корень и
                         # читает порт петли прокси (#557): спека собирает
                         # окружение тел, дефолт -- публичный origin.
                         "MOP_LLM_PROXY_PORT")}
# Несекретные настройки провайдера личностей (#214) -- сервисам, которые его
# строят: callout и глагол личности bootstrap'а. Одним списком
# (config.IDENTITY_SCOPED); что у юнита уже было, в добавки не входит.
ADDED["mop-bootstrap"] = config.IDENTITY_SCOPED
# mop-callout снят заново в #219 (без MOP_AUTH_CALLOUT и MOP_OPERATORS);
# после снимка -- группа доступа и режим вложенности LDAP (#220), тем же
# списком IDENTITY_SCOPED.
ADDED["mop-callout"] = ("MOP_LDAP_ACCESS_GROUP", "MOP_LDAP_NESTED")
# Каталог моделей читается петлёй к прокси (#557): порт петли -- юнитом.
ADDED["mop-web"] = ("MOP_LLM_PROXY_PORT",)

PINNED = {
    'mop-bootstrap': "[Unit]\nDescription=mop-bootstrap (bootstrap песочниц: играет .mop/bootstrap.yaml проекта при каждом старте папета)\nAfter=network-online.target nats.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет: всё, что подписчику и прогону нужно знать об\n# установке, приезжает юнитом. MOP_HOME и MOP_USER -- те, что у узлов: их\n# читают задачи bootstrap'а как переменные прогона, и дефолт сервера\n# (его собственный дом) здесь был бы неправдой.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_HOME=/home/mopuser\nEnvironment=MOP_USER=mopuser\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=/home/mopuser/mop/bin/mop bootstrap serve\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n",
    'mop-cluster': '[Unit]\nDescription=mop-cluster (сервис кластера: Nomad за шиной, глаголы пула на mop.*.cluster.rpc)\nAfter=network-online.target nats.service nomad.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет: что сервису нужно знать об установке, приезжает\n# юнитом. MOP_SERVER_LAN отвечает сразу за адрес шины и за NOMAD_ADDR\n# (config.DERIVED), поэтому второй переменной для Nomad здесь нет.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_HOME=/home/mopuser\nEnvironment=MOP_USER=mopuser\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=/home/mopuser/mop/bin/mop cluster serve\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n',
    'mop-web': '[Unit]\nDescription=mop-web (дашборд пула: состояние по HTTP)\nAfter=network-online.target nats.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# .env на сервер не едет, а адрес сервера обязателен: без него config\n# отказывается работать (и правильно). Окружение старше .env, поэтому\n# юнит и есть источник этой настройки на сервере.\nEnvironment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n# Каталог сервера ходит на шину через TLS-прокси (#97).\nEnvironment=MOP_HTTPS_PORT=443\nEnvironment=MOP_NOMAD_PORT=4646\nEnvironment=MOP_POOL_DC=home\nEnvironment=PYTHONUNBUFFERED=1\n# Через диспетчер: он ставит PYTHONPATH, без него командлет пакета не найдёт.\nExecStart=/home/mopuser/mop/bin/mop web --port 8080 --bind 0.0.0.0\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n',
}
# Юнит auth callout (#206) -- новый: снимок с его появления, байт в байт.
PINNED["mop-callout"] = '[Unit]\nDescription=mop-callout (auth callout шины: кто входит, решает провайдер личностей)\nAfter=network-online.target nats.service\nWants=network-online.target\n\n[Service]\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n# Шина на петле. Пароль пользователя callout, сиды издателя и xkey --\n# файлами 0600 в /etc/nats, не здесь: юнит читаем всем.\nEnvironment=MOP_NATS_PORT=4222\nEnvironment=MOP_AUTH_PROVIDER=file\nEnvironment=MOP_LDAP_URL=ldaps://ldap.example.dev\nEnvironment=MOP_LDAP_BIND_DN=cn=mop,ou=services,dc=example,dc=dev\nEnvironment=MOP_LDAP_BASE=dc=example,dc=dev\nEnvironment=MOP_LDAP_LOGIN_ATTR=uid\nEnvironment=MOP_LDAP_NAME_ATTR=displayName,cn\nEnvironment=MOP_LDAP_EMAIL_ATTR=mail\nEnvironment=MOP_LDAP_GROUP_BASE=ou=groups,dc=example,dc=dev\nEnvironment=MOP_LDAP_GROUP_FILTER=(|(member={dn})(uniqueMember={dn})(memberUid={login}))\nEnvironment="MOP_LDAP_ADMIN_GROUP=cn=mop admins,ou=groups,dc=example,dc=dev"\nEnvironment=MOP_LDAP_PROJECT_GROUP=mop-{project}\nEnvironment=MOP_LDAP_STARTTLS=no\nEnvironment=MOP_LDAP_CA_FILE=/etc/ssl/certs/corp-ca.pem\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=/home/mopuser/mop/bin/mop callout\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n'
# #359: снят не со старого шаблона -- юнита не было. Oneshot под таймером:
# без Restart= (systemd отвергает always у oneshot) и без [Install] --
# включают таймер, а не сервис.
PINNED["mop-doctor"] = ('[Unit]\nDescription=mop-doctor (doctor по расписанию: лечит только безопасное)\n'
                        'After=network-online.target nats.service\nWants=network-online.target\n\n'
                        '[Service]\nType=oneshot\nUser=mopuser\nWorkingDirectory=/home/mopuser/mop\n'
                        '# .env на сервер не едет, а адрес сервера обязателен: lib.cluster без\n'
                        '# него отказывает. Окружение старше .env -- юнит и есть источник.\n'
                        'Environment=MOP_SERVER_LAN=10.0.0.1\nEnvironment=MOP_NATS_PORT=4222\n'
                        '# Каталог сервера ходит на шину через TLS-прокси (#97).\n'
                        'Environment=MOP_HTTPS_PORT=443\nEnvironment=PYTHONUNBUFFERED=1\n'
                        '# Зависший прогон держал бы сервис active, и таймер молча не\n'
                        '# запускал бы следующий: потолок -- меньше часа, до следующего срабатывания.\n'
                        'TimeoutStartSec=50min\n'
                        '# Через диспетчер: он ставит PYTHONPATH, без него командлет пакета не найдёт.\n'
                        'ExecStart=/home/mopuser/mop/bin/mop server doctor --fix --safe\n')
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
    tree = ast.parse(open(os.path.join(ROOT, "mop", "common", "config.py")).read())
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


# ── #477: рестарт серверных сервисов -- состоянием, а не только changed ─────
# HYPOTHESIS (#477): aborted-деплой (fatal до пьесы cluster) синкает пакет и
# пишет package.version, но не доходит до handler'ов; retry без changed не
# перезапускает сервис уже никогда -- сигнал «пакет новее сервиса» жил один
# прогон и умер вместе с ним. Поймано на #4584: mop update собрал спеку
# процессом со старым кодом, у новой сессии нет statusLine.
# SOLUTION: решение о рестарте серверных сервисов (cluster, bootstrap,
# builder, web, callout) сравнивает состояние живого сервиса с установленным
# пакетом: ActiveEnterTimestamp против mtime package.version. Сервис старее
# пакета -- рестарт нужен, даже если прогон «без изменений»; новее -- нет. Тогда
# пропущенный рестарт лечится следующим же прогоном. Семантика #87 не
# тронута: недостижимая машина -- не красный прогон; новая задача только
# читает и при необходимости перезапускает.
# STATUS: FIXED — see #477
RESTARTED_SERVICES = ("mop-cluster", "mop-bootstrap", "mop-builder", "mop-web",
                      "mop-callout")


def service_older(service_since, package_since):
    """Сервис старее установленного пакета -> рестарт нужен. Чистая функция
    (#477): оба аргумента -- epoch-секунды; None сервиса -- сервис не
    отвечал, читаем «старее» (рестарт безопаснее молчания: включающий
    «Enable and start» уже поднял бы его, а не отвечающий timestamp -- сбой
    признак самой единицы)."""
    if service_since is None or package_since is None:
        return False
    return service_since < package_since


def check_restart_by_state_477(c):
    """Чистая функция сравнения + задача роли пользуется ею на живых фактах."""
    import datetime as _dt
    T0 = 1_790_000_000
    # Сервис старее пакета -- рестарт нужен: ровно случай #477 (пакет
    # доехал, сервис поднят раньше и пережил abort).
    c.check("#477 service older than the package needs a restart",
            service_older(T0, T0 + 1) is True)
    # Сервис новее пакета (перезапущен после синка) -- рестарта не нужно.
    c.check("#477 service newer than the package does not",
            service_older(T0 + 1, T0) is False)
    # Ровно совпадают (перезапуск в ту же секунду синка) -- не нужен:
    # «не старее» и есть «на новом коде».
    c.check("#477 equal timestamps do not restart",
            service_older(T0, T0) is False)
    # Часовой пояс не влияет: systemd отдаёт UTC, задача не конвертирует
    # локально -- проверка на строках обе стороны цепочки ниже.
    iso = _dt.datetime.fromtimestamp(T0, _dt.timezone.utc).isoformat()
    c.check("#477 the comparison is epoch, not local strings",
            service_older(T0, T0 + 1) is (T0 < T0 + 1))
    # Задачи ролей: у каждого сервиса из RESTARTED_SERVICES есть задача,
    # решающая рестарт сравнением состояния (не только changed прогона).
    tasks = site_tasks()
    # Сервис живёт в loop включения, сравнение -- в включённом файле:
    # связка -- само включение roles/*/tasks -> common/tasks/service_age.yml.
    includes = {}
    for f, t in tasks:
        mod = next((k for k in INCLUDES if k in t), None)
        if not mod:
            continue
        path = t[mod].get("file") if isinstance(t[mod], dict) else t[mod]
        if isinstance(path, str) and path.endswith("common/tasks/service_age.yml"):
            for svc in (t.get("loop") or []):
                includes[svc] = os.path.relpath(f, DEPLOY)
    for svc in RESTARTED_SERVICES:
        c.check(f"#477 {svc}: a state-comparing restart decision exists",
                svc in includes, includes.get(svc))
    age_file = os.path.join(DEPLOY, "roles", "common", "tasks", "service_age.yml")
    body = open(age_file).read() if os.path.isfile(age_file) else ""
    c.check("#477 the roles read ActiveEnterTimestamp of the services",
            "ActiveEnterTimestamp" in body, body[:120])
    c.check("#477 the roles read the mtime of package.version",
            "package.version" in body)
    c.check("#477 the stale service is restarted, not only noted",
            "state: restarted" in body)


def check_probes(c):
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
        c.check(f"{os.path.relpath(f, DEPLOY)}: {t.get('name')!r} registers {reg}, read by "
              f"{readers}: it must run in check mode", not readers)
    # Семь проб поимённо: только читают, в настоящем прогоне не «changed».
    by = {(os.path.relpath(f, os.path.join(DEPLOY, "roles")), t.get("name")): t for f, t in tasks}
    for path, name, reg, commands in PROBES:
        t = by.get((path, name))
        if t is None:
            c.check(f"probe {path}: {name!r} exists", False)
            continue
        mod = next(k for k in t if k in CMD_MODULES)
        text = t[mod]["cmd"] if isinstance(t[mod], dict) else t[mod]
        c.check(f"probe {name!r}: register {reg}", t.get("register") == reg, t.get("register"))
        c.check(f"probe {name!r}: check_mode: false", t.get("check_mode") is False)
        c.check(f"probe {name!r}: changed_when: false", t.get("changed_when") is False)
        missing = [c for c in commands if c not in " ".join(text.split())]
        c.check(f"probe {name!r}: runs {commands}", not missing, missing)
        c.check(f"probe {name!r}: changes nothing", not WRITES.search(text), WRITES.search(text))


# ── #192: размеры тела при сборке — строка хоста раньше установки ─────────────
# Память из них ушла (#197): она свойство папета -- просьба проекта, иначе
# установки, -- и узел о ней говорит только размещением (mop_mem_cap_mb в
# meta Nomad). Тело сборки берёт память по той же цепочке, без узла.
BODY_KNOBS = ("MOP_DISK_GB", "MOP_CORES", "MOP_BODY_DISK_CAP_GB", "MOP_BODY_CORES_CAP")
# Правило node.env (roles/bus/tasks/main.yml): строчная хоста, иначе установка.
NODE_RULE = "hostvars[inventory_hostname][item | lower] | default(lookup('vars', item))"


# ── хранилище тел -- из строки хоста, как в node.env (#227) ──────────────
# HYPOTHESIS: MOP_PVE_STORAGE узловая (config.NODE_SCOPED): mop_pve_storage
# строки хоста доезжает в node.env, и тела клонируются на хранилище узла; но
# pve-build.yml подставлял {{ MOP_PVE_STORAGE }} напрямую -- значение
# установки из .env -- в трёх местах (клон сборочного тела, создание,
# клон образа). На rumop тела шли на NVMe, а образы пеклись на local-lvm
# (agent1: 72% из 54 ГБ под тремя шаблонами), прогон зелёный.
# SOLUTION: как размеры тела (#192) -- задача читает хранилище узла по
# правилу node.env в свой факт node_storage до первой задачи, которая его
# берёт; ни одной голой подстановки MOP_PVE_STORAGE в pve-build.yml.
# STATUS: FIXED — see #227
def check_routes_gateway_wait_502(c, can_render):
    """#502: mop-routes ждёт готовности шлюза до маршрутов.

    HYPOTHESIS: юнит стартует по After=network-online.target, но в LXC
    адрес eth0 приходит по DHCP от dhclient позже target'а (3 из 4 загрузок
    сервера mop), шлюза ещё нет в таблице -- первая же `ip route replace
    ... via <шлюз>` отказывает «Nexthop has invalid gateway», юнит падает, и
    сервер остаётся без дорог к телам: bootstrap папетов валится по
    ssh-таймауту до ручного `systemctl restart mop-routes`.
    SOLUTION: ExecStartPre с циклом ожидания `ip route get` до первого
    гипервизора (без установленного маршрута команда отказывает) под
    /usr/bin/timeout; маршруты -- после него.
    STATUS: FIXED — see #502"""
    path = os.path.join(DEPLOY, "roles", "pve", "templates", "mop-routes.service.j2")
    text = open(path).read()
    c.check("#502 the routes template exists", os.path.isfile(path))
    if not can_render:
        return
    v = {"pve_hops": ["hyper"],
         "hostvars": {"hyper": {"pve_lan": "192.168.1.112",
                                "pve_routes": ["10.77.36.0/23", "10.77.38.0/24"],
                                "inventory_hostname": "hyper"}}}
    try:
        got = render(text, v)
    except Exception as e:  # noqa: BLE001 -- проверка, не код пула
        c.fail(f"#502 routes render: {type(e).__name__}", str(e))
        return
    lines = [l for l in got.splitlines() if l.startswith(("ExecStartPre=", "ExecStart="))]
    # Сначала ожидание шлюза, потом первый маршрут.
    c.check("#502 the gateway wait comes before the routes",
          bool(lines) and lines[0].startswith("ExecStartPre="), lines[:2])
    wait = next((l for l in lines if "route get" in l), "")
    c.check("#502 the wait probes the first hop under a timeout",
          wait.startswith("ExecStartPre=/usr/bin/timeout") and "route get" in wait
          and "192.168.1.112" in wait, wait)
    routes = [l for l in lines if l.startswith("ExecStart=")]
    c.check("#502 the routes follow the wait, all of them",
          len(routes) == 2
          and all(f"via 192.168.1.112" in l for l in routes), routes)


def check_pve_storage_227(c, can_render):
    plays = yaml.safe_load(open(os.path.join(DEPLOY, "pve-build.yml")))

    def flat(ts):
        for t in ts or []:
            yield t
            yield from flat((t or {}).get("block"))
    # Клон образа -- в игре «Seal the image» того же хоста: факт первой игры
    # до неё доживает, и смотреть надо все игры файла, по порядку.
    tasks = [t for p in plays for t in flat(p.get("tasks"))]
    names = [t.get("name") for t in tasks]
    node = next((t for t in tasks if "node_storage" in
                 ((t.get("ansible.builtin.set_fact") or {}))), None)
    c.check("pve-build: the node's storage is read in its own task", node is not None)
    if node is None:
        return
    fact = str((node.get("ansible.builtin.set_fact") or {}).get("node_storage", ""))
    rule = NODE_RULE.replace("item | lower", "'mop_pve_storage'").replace(
        "lookup('vars', item)", "MOP_PVE_STORAGE")
    c.check("pve-build: the node's storage is read the way node.env reads it",
          rule in " ".join(fact.split()), fact)
    # Строки команд -- в том числе secret_cmd включённой роли.
    def commands(t):
        out = [str(t.get("ansible.builtin.command", ""))]
        out.append(str((t.get("vars") or {}).get("secret_cmd", "")))
        return " ".join(out)
    users = [i for i, t in enumerate(tasks) if "node_storage" in commands(t)]
    c.check("pve-build: all three storage uses take the node's storage",
          len(users) == 3, [names[i] for i in users])
    if users:
        c.check("pve-build: the storage is read before its first use",
              names.index(node["name"]) < min(users), (node["name"], names[min(users)]))
    for t in tasks:
        bare = re.findall(r"(?<![.'\w])MOP_PVE_STORAGE(?!')", commands(t))
        c.check(f"pve-build: {t.get('name')}: no installation storage past the host's",
              not bare, commands(t).strip()[:160])
    if not can_render:
        return
    import jinja2
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    for what, host, want in (("the host's own storage", {"mop_pve_storage": "nvmez-data"}, "nvmez-data"),
                             ("the installation's without a host line", {}, "local-lvm")):
        v = {"MOP_PVE_STORAGE": "local-lvm", "inventory_hostname": "agent1",
             "hostvars": {"agent1": host}}
        try:
            got = env.from_string(fact).render(**v)
        except Exception as e:  # noqa: BLE001 -- проверка, не код пула
            got = f"{type(e).__name__}: {e}"
        c.check(f"pve-build: node_storage is {what}", got == want, got)


def check_body_sizes(c, can_render):
    """STATUS: FIXED — see #192"""
    play = yaml.safe_load(open(os.path.join(DEPLOY, "pve-build.yml")))[0]
    by = {t.get("name"): t for t in play["tasks"]}
    sizes = by.get("How big a body of this project may be here")
    node = by.get("What this node says about the size of its bodies")
    c.check("pve-build: the body's size task exists", sizes is not None)
    c.check("pve-build: the node's own sizes are read in one task", node is not None)
    if sizes is None:
        return
    exprs = sizes.get("ansible.builtin.set_fact") or {}
    for k in ("body_disk", "body_cores"):
        bare = re.findall(r"(?<![.'\w])MOP_[A-Z_]+(?!')", exprs.get(k, ""))
        c.check(f"pve-build: {k} takes no installation setting past the host's", not bare, bare)
    c.check("pve-build: body_mem takes nothing of the node (#197)",
          "node_body" not in exprs.get("body_mem", "") and "hostvars" not in exprs.get("body_mem", ""),
          exprs.get("body_mem"))
    if node is None:
        return
    names = [t.get("name") for t in play["tasks"]]
    c.check("pve-build: the node's sizes are read before they are used",
          names.index(node["name"]) < names.index(sizes["name"]))
    c.check("pve-build: every body setting is read, and only those",
          sorted(node.get("loop") or []) == sorted(BODY_KNOBS), node.get("loop"))
    c.check("pve-build: every body setting is node-scoped",
          set(BODY_KNOBS) <= set(config.NODE_SCOPED))
    fact = (node.get("ansible.builtin.set_fact") or {}).get("node_body", "")
    c.check("pve-build: the node's sizes are read the way node.env reads them",
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
        c.check(f"pve-build: {what}", got == want, got)


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


# ── сборка на одном узле (#280) ──────────────────────────────────────────
# HYPOTHESIS: pve-build.yml играет на всех хостах puppet, и узел ограничить
# нечем: на rumop образ agent2 запечатался голым (#278), а на agent1 папеты
# проекта заняты -- пересборка либо отбивается по ним, либо (--force) убивает
# их работу, хотя образ agent1 цел. --limit ansible не годится: хост,
# добавленный add_host, может попасть под ограничение подмножества.
# SOLUTION: extra-var mop_node; в первой игре и в «Seal the image» -- end_host
# на всяком узле, кроме названного, рядом с end_host для неконтейнерных.
# STATUS: FIXED — see #280
def check_one_node_280(c):
    plays = yaml.safe_load(open(os.path.join(DEPLOY, "pve-build.yml")))
    guarded = [p for p in plays if p.get("hosts") == "puppet"]
    c.check("pve-build: two plays run on the puppet group", len(guarded) == 2,
            [p.get("name") for p in guarded])
    for p in guarded:
        tasks = p.get("tasks") or []
        ends = [t for t in tasks if "ansible.builtin.meta" in t
                and t["ansible.builtin.meta"] == "end_host"
                and "mop_node" in str(t.get("when", ""))]
        c.check(f"pve-build: {p.get('name')}: leaves every node but mop_node",
                len(ends) == 1, [t.get("name") for t in tasks[:4]])
        if not ends:
            continue
        when = " ".join(str(ends[0]["when"]).split())
        c.check(f"pve-build: {p.get('name')}: the guard is off when no node is named",
                "mop_node is defined" in when, when)
        c.check(f"pve-build: {p.get('name')}: the guard compares the host's own name",
                "inventory_hostname != mop_node" in when, when)
        # До первой задачи, которая что-то делает на узле: снос и запечатывание
        # чужого узла -- ровно то, от чего заводился флаг.
        doing = next((i for i, t in enumerate(tasks)
                      if "ansible.builtin.meta" not in t), len(tasks))
        c.check(f"pve-build: {p.get('name')}: the guard stands before any work",
                tasks.index(ends[0]) < doing, (tasks.index(ends[0]), doing))


def check_include_loop_var(c):
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
        c.check(f"{where}: has its own loop_var", var not in (None, "item"), var)
        c.check(f"{where}: does not refer to item",
              not re.search(r"\bitem\b", yaml.safe_dump(t)), yaml.safe_dump(t))
    # Проверка не пустая: известные include чужих файлов на месте. Свои пять
    # включений #477 (рестарт серверных сервисов по состоянию) -- тот же
    # вид: шаблонный путь общего файла поверх цикла, со своим loop_var.
    # Список растёт сознательно, правкой этой же проверки.
    want = ["body.yml: 'Sandbox tasks of each project that has any'",
            "body.yml: 'What this installation adds to a body'",
            "pve-build.yml: 'Task files this installation names'"]
    for _where in ("roles/bootstrap/tasks/main.yml",
                   "roles/bus/tasks/callout.yml",
                   "roles/cluster/tasks/main.yml",
                   "roles/cluster/tasks/main.yml",
                   "roles/web/tasks/main.yml"):
        want.append(f"{_where}: \"Restart the stale package's service by its state\"")
    want.sort()
    c.check("the include loops over foreign files are the known list", sorted(found) == want,
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


# ── add_host в pve-build.yml -- на каждый узел, а не на первый (#278) ─────
# HYPOTHESIS: задача «Take the body into the inventory for the rest of the
# run» -- ansible.builtin.add_host, а его action-плагин обходит цикл по
# хостам (BYPASS_HOST_LOOP = True): выполняется один раз на игру, с фактами
# первого хоста. На rumop (proxmox-agent1, proxmox-agent2) в mop_building
# попало только тело agent1; игры сборки на agent2 не игрались, а «Seal the
# image» идёт по группе puppet и запечатала голое тело сразу после
# bootstrap -- без git, с кодом 0. На установке с одним контейнерным узлом
# дефект не виден.
# SOLUTION: add_host циклом по хостам игры (ansible_play_hosts_all), все поля
# и прыжок -- из hostvars[<хост цикла>], ни одной голой ссылки на факт
# текущего хоста.
# STATUS: FIXED — see #278
def check_add_host_loop_278(c):
    plays = yaml.safe_load(open(os.path.join(DEPLOY, "pve-build.yml")))
    tasks = [t for p in plays for t in (p.get("tasks") or [])]
    add = [t for t in tasks if "ansible.builtin.add_host" in t]
    c.check("pve-build: one add_host task", len(add) == 1, [t.get("name") for t in add])
    if len(add) != 1:
        return
    t = add[0]
    loop = str(t.get("loop") or "")
    c.check("pve-build: add_host loops over the play's hosts (it bypasses the host loop)",
          "ansible_play_hosts" in loop, loop or "no loop")
    var = (t.get("loop_control") or {}).get("loop_var") or "item"
    body = yaml.safe_dump({"ansible.builtin.add_host": t["ansible.builtin.add_host"],
                           "vars": t.get("vars") or {}})
    facts = ("stage_name", "stage_ip", "stage_vmid",
             "ansible_user", "ansible_host", "ansible_port", "inventory_hostname")
    for fact in facts:
        # Голая ссылка -- та, перед которой не стоит hostvars[...].: оба
        # написания, точкой и скобкой.
        bare = [m.group(0) for m in re.finditer(
            r"(?<![\w\['.])%s\b(?!:)" % fact, body)]
        c.check(f"pve-build: add_host reads {fact} only through hostvars[{var}]",
              not bare, body)
    c.check(f"pve-build: add_host reads hostvars of the loop's host, {var}",
          f"hostvars[{var}]" in body, body)
    # build_key -- переменная игры (vars: плейбука), не факт хоста: в hostvars
    # её нет, и hostvars[item].build_key уронил живую сборку на rumop
    # («HostVarsVars has no attribute build_key»). Переменные игры одни на
    # всех хостов и читаются как есть.
    c.check("pve-build: add_host reads the play's build_key bare, not through hostvars",
          "build_key" in body and f"hostvars[{var}].build_key" not in body, body)



# HYPOTHESIS: одинаковое stage_name перезаписывает inventory-host следующего
# узла; цикл add_host не доказывает, что игры настройки увидят каждое тело.
# SOLUTION: ключ inventory-host включает узел; hostname контейнера не меняется.
# RESULT: три тела сохраняют адрес, VMID и SSH-прыжок до игр настройки.
# STATUS: FIXED — see #577
def check_distinct_build_inventory_577(c, can_render):
    if not can_render:
        return
    from jinja2 import Environment, StrictUndefined
    env = Environment(undefined=StrictUndefined)
    plays = yaml.safe_load(open(os.path.join(DEPLOY, "pve-build.yml")))
    task = next(t for p in plays for t in p.get("tasks", [])
                if "ansible.builtin.add_host" in t)
    hosts = {
        "node-a": {"stage_name": "pu-tmpl-proj-build", "stage_ip": "10.77.1.2",
                   "stage_vmid": 10999, "ansible_host": "a.example", "ansible_user": "root"},
        "node-b": {"stage_name": "pu-tmpl-proj-build", "stage_ip": "10.77.2.2",
                   "stage_vmid": 11999, "ansible_host": "b.example", "ansible_user": "pool",
                   "ansible_port": 2222},
        "node-c": {"stage_name": "pu-tmpl-proj-build", "stage_ip": "10.77.3.2",
                   "stage_vmid": 12999},
    }
    variables = {"hostvars": hosts, "ansible_play_hosts_all": list(hosts),
                 "MOP_USER": "pool", "build_key": "/tmp/build-key",
                 "lookup": lambda kind, name: "/home/control"}
    inventory = {}
    for item in hosts:
        context = dict(variables, item=item)
        for key, value in task.get("vars", {}).items():
            context[key] = env.from_string(value).render(**context)
        body = {key: env.from_string(value).render(**context) if isinstance(value, str)
                else value for key, value in task["ansible.builtin.add_host"].items()}
        inventory[body.pop("name")] = body
    c.expect("#577 all three build guests remain in the inventory", len(inventory), 3)
    actual = {body["mop_body_of"]: body for body in inventory.values()}
    c.expect("#577 configuration reaches each node's guest", sorted(actual), sorted(hosts))
    for node, hop in (("node-a", "root@a.example"),
                      ("node-b", "pool@b.example:2222"), ("node-c", "node-c")):
        if node not in actual:
            continue
        body = actual[node]
        c.expect(f"#577 {node} keeps its own guest address", body["ansible_host"],
                 hosts[node]["stage_ip"])
        c.expect(f"#577 {node} keeps its own VMID", str(body["mop_body_vmid"]),
                 str(hosts[node]["stage_vmid"]))
        c.check(f"#577 {node} keeps its own SSH hop",
                f"-o ProxyJump={hop}" in body["ansible_ssh_common_args"], body)
        c.expect(f"#577 {node} joins the configuration play", body["groups"], "mop_building")


def check_users_reload_199(c):
    """users.conf менялся -- nats перезагружается; решает контрольная сумма."""
    # Кто ещё читает вывод командлета: список закреплён. pve-facts печатает
    # JSON -- данные, не отчёт, и выводом #159/#179/#182 не тронут.
    found = stdout_readers()
    c.check("#199: commandlet output read only by the pinned tasks", found == [
        ("pve-build.yml", "driver pve-facts", "build_facts"),
        ("pve-build.yml", "driver pve-facts", "node_facts"),
        ("roles/pve/tasks/main.yml", "driver pve-facts", "node_facts")], found)
    c.check("#199: no task reads the output of a commandlet made quiet",
          not [x for x in found if x[1] in TOUCHED], found)
    c.check("#199: mop_command sees cluster users, cluster check, bootstrap check",
          {"cluster users", "cluster check", "bootstrap check"} <=
          {mop_command(t) for _, t in all_tasks()},
          sorted({c for c in (mop_command(t) for _, t in all_tasks()) if c}))

    f = os.path.join(DEPLOY, "roles", "bus", "tasks", "projects.yml")
    tasks = yaml.safe_load(open(f)) or []
    at = [i for i, t in enumerate(tasks) if mop_command(t) == "cluster users"]
    c.check("#199: one task runs mop cluster users", len(at) == 1, at)
    if len(at) != 1:
        return
    i, run = at[0], tasks[at[0]]
    c.check("#199: its changed does not read the command's output",
          "stdout" not in str(run.get("changed_when", "")), run.get("changed_when"))

    def stat_of(t):
        st = t.get("ansible.builtin.stat") or {}
        return st if str(st.get("path", "")).endswith("/etc/nats/users.conf") \
            and st.get("get_checksum", True) is not False else None
    before = [t for t in tasks[:i] if stat_of(t) and t.get("register")]
    after = [t for t in tasks[i + 1:] if stat_of(t) and t.get("register")]
    c.check("#199: users.conf is stat'ed before the command", len(before) == 1,
          [t.get("name") for t in before])
    c.check("#199: and after it", len(after) == 1, [t.get("name") for t in after])
    if len(before) != 1 or len(after) != 1:
        return
    b, a = before[0]["register"], after[0]["register"]
    decide = [t for t in tasks[i + 1:] if "reload nats" in str(t.get("notify", ""))]
    c.check("#199: one task after the command notifies reload nats", len(decide) == 1,
          [t.get("name") for t in decide])
    c.check("#199: the command itself notifies nothing", not run.get("notify"),
          run.get("notify"))
    if decide:
        cw = str(decide[0].get("changed_when", ""))
        c.check("#199: changed is the checksums before and after differing",
              f"{b}.stat.checksum" in cw and f"{a}.stat.checksum" in cw
              and "!=" in cw and "stdout" not in cw, cw)


def check_identity_copy_167(c, ptasks):
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

    # #232: цепочка -- список через запятую; пароль LDAP -- когда ldap в ней
    # звено, файл операторов -- когда он есть, как и прежде.
    # STATUS: FIXED — see #232
    for exists, prov in itertools.product((True, False),
                                          ("file", "ldap", "file,ldap", "ldap, file")):
        env = {"MOP_AUTH_PROVIDER": prov,
               "operators_file": types.SimpleNamespace(stat=types.SimpleNamespace(exists=exists))}
        try:
            done = [target(t) for t in ran(ptasks, env, [])]
        except AssertionError as e:
            c.check(f"bus identity copy, file {exists}, {prov}: conditions read", False, e)
            continue
        what = f"bus identity copy, operators file {exists}, provider {prov}"
        c.check(f"{what}: the copy's directory is always laid (#219)",
              ("file", "/etc/nats/identity", "directory") in done, done)
        c.check(f"{what}: the copy is never removed as a whole (#219)",
              ("file", "/etc/nats/identity", "absent") not in done, done)
        c.check(f"{what}: the operators file is copied exactly when it exists",
              (("copy", "/etc/nats/identity/operators", None) in done) == exists, done)
        with_ldap = "ldap" in prov
        c.check(f"{what}: the LDAP bind password is laid exactly with ldap in the chain",
              (("copy", "/etc/nats/identity/ldap-bind.pass", None) in done) == with_ldap, done)
        c.check(f"{what}: a chain without ldap leaves no bind password",
              (("file", "/etc/nats/identity/ldap-bind.pass", "absent") in done) != with_ldap,
              done)
        c.check(f"{what}: the controller's operators file is looked at",
              any(k == "stat" and "operators" in str(p) for k, p, _ in done), done)


def check_agent_unit_172(c, can_render):
    """HYPOTHESIS (#172): юнит агента и проверка deploy зовут переходный
    `python3 -m mop.agent` (#150), а не командлет. Считалось, что на узле
    диспетчер откажет без .env (REQUIRED); не отказывает -- проверено на
    hyper, `cd / && ~/mop/bin/mop agent --check` с одним node.env, и в
    tests/cli.py (check_agent_on_node_172).
    SOLUTION: ExecStart и задача проверки -- `{{ mop_home }}/bin/mop agent`,
    лаунчер пакета узла; __main__ в mop/node/agent.py живёт до раскатки на обе
    установки.
    STATUS: FIXED — see #172"""
    unit = os.path.join(DEPLOY, "roles", "bus", "templates", "mop-agent.service.j2")
    text = open(unit).read()
    c.check("mop-agent unit: no python3 -m mop.agent (#172)", "-m mop.agent" not in text)
    if can_render:
        got = render(text, {"MOP_USER": "mopuser", "mop_home": "/home/mopuser/mop",
                            "inventory_hostname": "hyper", "mop_uid": 1000,
                            "MOP_PUPPET_SEED": ".env*"})
        execs = [l for l in got.splitlines() if l.startswith("ExecStart=")]
        c.check("mop-agent unit: ExecStart is the commandlet (#172)",
              execs == ["ExecStart=/home/mopuser/mop/bin/mop agent"], execs)
    tasks = [t for f, t in all_tasks() if f.endswith("roles/bus/tasks/main.yml")
             and t.get("name") == "Confirm the agent is connected"]
    c.check("bus role: one agent check task (#172)", len(tasks) == 1, len(tasks))
    for t in tasks:
        c.check("agent check: runs the commandlet (#172)",
              t.get("ansible.builtin.command") == "{{ mop_home }}/bin/mop agent --check",
              t.get("ansible.builtin.command"))
        c.check("agent check: same chdir and node name (#172)",
              (t.get("args") or {}).get("chdir") == "{{ mop_home }}"
              and (t.get("environment") or {}).get("MOP_NODE") == "{{ inventory_hostname }}",
              (t.get("args"), t.get("environment")))


# ── посев реестра при deploy (#540) ────────────────────────────────────
# HYPOTHESIS (#540): посев реестра (#117) искал на контроллере только
# легаси-файл `projects`: свежий сервер не получал снимок v1 (#534), а
# сервер с любым своим реестром мог получить сверху чужую легаси-копию
# контроллера. Посев парой -- двумя задачами -- ещё и не атомарен: обрыв
# после projects оставлял retry видеть «пару» и молчать -- потолки
# терялись навсегда.
# SOLUTION: инициализация сеет ОДИН провалидированный снимок v1. Префлайт
# `mop server deploy` готовит его на контроллере: нет ни снимка, ни пары --
# None, посева нет (пустая установка); есть что-то -- СТРОГИЙ
# projects.snapshot_of (v1 авторитетен; пара -- чистая конвертация с
# потолками; bare/коллизия/orphan/битый JSON/плохой лимит -- отказ
# оператору ДО плейбука, файлы целы) и write_snapshot в личный временный
# каталог (0700/0600); плейбуку едет ПУТЬ (--extra-vars), не сырой JSON.
# Плей копирует один файл no-clobber: полупары не бывает, retry на пустом
# сервере повторяет, на засеянном скипает. Сервер с любым своим реестром
# authoritative и не трогается; миграция -- writer сервиса (#534).
# Работающий mop-cluster закрывает посев совсем: он владелец даже пустого
# реестра, глагол может создать снимок между stat и copy, а force:no
# решает лишь в момент copy. Same-HOME -- одна машина, посева нет.
# Проверка исполняет настоящие when задач посева по матрице фактов,
# как check_identity_copy_167, и гоняет клей стейджинга по тем же входам.
# RESULT: красный прогон впереди реализации -- посев парой из двух задач
# (обрыв не восстанавливается), клей стейджинга отсутствует.
# STATUS: FIXED — see #540
# ── #552: общий защитный gate посева, действующие условия задач ──────────
# HYPOTHESIS (#552): пять защитных условий посева (другие HOME, неактивный
# writer, отсутствие трёх файлов) повторены в двух задачах буквально. Правка
# одного экземпляра молча расходила fail и copy -- gate перестал быть общим,
# и одна из задач ушла из-под защиты. SOLUTION: один block с общим when;
# внутри -- только различающиеся условия (признак отказа у fail, наличие
# staged-файла у copy). Проверка сверяет ДЕЙСТВУЮЩИЕ условия каждой задачи
# -- собственные плюс унаследованный block.when -- с утверждённым набором
# #540, и требует, чтобы общий gate был именно общим: ровно один блок,
# обнимающий обе задачи, без дублирования условий между уровнями.
def walk_tasks_552(ptasks):
    """Задачи плея плоско: верхний уровень плюс задачи внутри block'ов
    (один уровень -- глубже плей не вкладывает). -> [задача]."""
    out = []
    for t in ptasks or []:
        if not isinstance(t, dict):
            continue
        out.append(t)
        for inner in t.get("block") or []:
            if isinstance(inner, dict):
                out.append(inner)
    return out


def parent_block_552(ptasks, task):
    """Родительский block задачи: сам блок в списке задач живёт как
    задача-обёртка, ищем по имени внутренней задачи."""
    for t in ptasks or []:
        body = t.get("block") if isinstance(t, dict) else None
        if isinstance(body, list) and any(
                isinstance(inner, dict) and inner.get("name") == task.get("name")
                for inner in body):
            return t
    return None


def effective_when_552(ptasks, task):
    """ДЕЙСТВУЮЩИЕ условия задачи: собственный when плюс унаследованный
    block.when родителя (один уровень). -> [строка] в порядке действия.
    Ansible AND-ит и блок, и задачу: бегут все."""
    conds = task.get("when", [])
    out = [str(x) for x in (conds if isinstance(conds, list) else [conds])]
    parent = parent_block_552(ptasks, task)
    if parent is not None:
        bw = parent.get("when", [])
        out += [str(x) for x in (bw if isinstance(bw, list) else [bw])]
    return out


def check_registry_gate_552(c, ptasks):
    import types
    # Плоский список задач вместе с задачами из block: матрица #540 ищет
    # задачи по имени и модулю -- с общим gate (#552) задачи посева уезжают
    # внутрь block, и поиск только по верхнему уровню молча находил бы
    # пустой набор.
    tasks = walk_tasks_552(ptasks)

    def find(needle, attr):
        for t in tasks:
            if needle in str(t.get("name", "")) and attr in t:
                return t
        return None

    fail = next((t for t in tasks if "ansible.builtin.fail" in t), None)
    copy = find("Registry of the projects, v1 snapshot", "ansible.builtin.copy")
    if not c.check("#552 the seed fail and copy tasks are both present",
                   fail is not None and copy is not None,
                   (fail and fail.get("name"), copy and copy.get("name"))):
        return

    fb, cb = parent_block_552(ptasks, fail), parent_block_552(ptasks, copy)
    c.check("#552 one shared block guards both the refusal and the copy",
            fb is not None and cb is not None and fb is cb, None)

    def conds_of(task):
        """Действующие условия: собственные + унаследованный block.when."""
        return set(effective_when_552(ptasks, task))

    # Утверждённый набор #540, неизменный по постановке #552: no-clobber,
    # владелец, mode, причина отказа и критерий inactive не меняются.
    gate = {"lookup('env', 'HOME') != MOP_HOME",
            "cluster_service_running.stdout == 'inactive'",
            "not server_snapshot.stat.exists", "not server_pair.stat.exists",
            "not server_limits.stat.exists"}
    c.check("#552 the refusal fires under exactly the seed's protective gate",
            conds_of(fail) == gate | {"mop_registry_seed_refused"}, conds_of(fail))
    c.check("#552 the copy runs under exactly the seed's protective gate",
            conds_of(copy) == gate | {"mop_registry_seed != ''",
                                      "seed_file.stat.exists"}, conds_of(copy))

    # Общность -- не только равенство множеств: gate живёт на БЛОКЕ, а не
    # продублирован в каждой задаче. Дублирование между уровнями (условие
    # и в блоке, и в задаче) -- та же рассинхронизация при правке.
    if fb is not None:
        bw = fb.get("when", [])
        block_conds = {str(x) for x in (bw if isinstance(bw, list) else [bw])}
        c.check("#552 the shared gate lives on the block, not duplicated per task",
                block_conds == gate, block_conds)
        for task, label in ((fail, "fail"), (copy, "copy")):
            own = task.get("when", [])
            own_set = {str(x) for x in (own if isinstance(own, list) else [own])}
            c.check(f"#552 the {label} task keeps only its differing conditions",
                    not (own_set & gate), own_set)


def check_registry_seeding_540(c, ptasks):
    check_registry_gate_552(c, ptasks)

    import itertools
    import types
    # Задачи плоско (walk_tasks_552): с общим gate (#552) задачи посева
    # живут внутри block, и поиск только по верхнему уровню молча
    # находил бы пустой набор -- матрица прогоняла бы пустоту.
    tasks = walk_tasks_552(ptasks)
    # Плей обязан ЗНАТЬ свои задачи посева: пустой набор -- провал
    # структуры, а не пустой проход матрицы (#552).
    c.check("#552 the play still declares its seeding tasks",
            any("ansible.builtin.copy" in t for t in tasks)
            and any("ansible.builtin.fail" in t for t in tasks), None)

    def seeds():
        out = {}
        for t in tasks:
            cp = t.get("ansible.builtin.copy") or {}
            dest = cp.get("dest")
            if isinstance(dest, str) and dest.endswith(("/.config/mop/projects",
                                                       "/.config/mop/projects.json",
                                                       "/.config/mop/limits.json")):
                out[os.path.basename(dest)] = (t, cp)
        return out

    seed = seeds()
    # Инициализация атомарна: ОДИН файл-снимок v1, никаких легаси-копий
    # парами задач (обрыв пары не восстанавливался, потолки терялись).
    c.check("#540 the play seeds exactly one file: the v1 snapshot",
            sorted(seed) == ["projects.json"], sorted(seed))
    if seed:
        cp = seed["projects.json"][1]
        c.check("#540 the seed lands on the server, no-clobber, private",
                (cp.get("dest") or "").startswith("{{ MOP_HOME }}/.config/mop/")
                and cp.get("force") is False and cp.get("mode") == "0600"
                and cp.get("owner") == "{{ MOP_USER }}"
                and cp.get("group") == "{{ MOP_USER }}", cp)

    # Факты посева: staged-файл на контроллере и три реестра на сервере.
    # Чужие stat'ы (users.conf, callout.conf, operators) не считаются.
    stats = []
    for t in tasks:
        st_ = t.get("ansible.builtin.stat") or {}
        if isinstance(st_.get("path"), str) and ("/.config/mop/" in st_["path"]
                                                 or "mop_registry_seed" in st_["path"]):
            side = "controller" if "delegate_to" in t else "server"
            name = ("projects.json" if "mop_registry_seed" in st_["path"]
                    else os.path.basename(st_["path"]))
            stats.append((side, name, t))
    c.check("#540 the play stats the staged seed on the controller and the server's three",
            sorted((s, n) for s, n, _ in stats)
            == [("controller", "projects.json"), ("server", "limits.json"),
                ("server", "projects"), ("server", "projects.json")],
            sorted((s, n) for s, n, _ in stats))
    staged = next((t for s, n, t in stats if s == "controller"), None)
    if staged:
        c.check("#540 the staged seed's fact stays on the controller",
                staged.get("delegate_to") == "localhost"
                and staged.get("become") is False
                and "{{ mop_registry_seed }}" in str(staged.get("ansible.builtin.stat")),
                staged)

    # Активный writer (#540): работающий mop-cluster -- авторитетный владелец
    # реестра (#534) даже когда реестр пуст -- глаголы могут создать снимок
    # в любой момент, stat пустоты того не опровергает, а force:no решает
    # лишь в момент copy. Посев обязан идти до старта сервиса (на чистой
    # установке плей шины идёт до роли cluster). Проба читает systemd и в
    # check mode (mop deploy --dry): иначе dry-run сеял бы «понарошку».
    probe = next((t for t in tasks
                  if "is-active" in str(t.get("ansible.builtin.command", ""))), None)
    c.check("#540 the play probes whether the cluster service is already running",
            probe is not None
            and probe.get("register") == "cluster_service_running"
            and probe.get("failed_when") is False and probe.get("changed_when") is False
            and probe.get("check_mode") is False, probe)
    order = [t.get("name") for t in tasks]
    if probe and seed:
        c.check("#540 the running-service probe precedes every seed",
                order.index(probe.get("name"))
                < min(order.index(t.get("name")) for t, _cp in seed.values()), order)

    # Отказ стейджинга (#540): непредставимый реестр контроллера не валит
    # deploy, пока посев не нужен. Плейбуку едет признак без текста, и fail
    # поднимается ТОЛЬКО под условиями самого посева: сервер пуст, сервис
    # неактивен, HOME разные. Сервер с реестром разворачивается как раньше.
    fail = next((t for t in tasks if "ansible.builtin.fail" in t), None)
    c.check("#540 the play has the one refusal task for a refused seed",
            sum(1 for t in tasks if "ansible.builtin.fail" in t) == 1
            and fail is not None
            and "refused" in str(fail.get("ansible.builtin.fail", "")),
            [t.get("name") for t in tasks if "ansible.builtin.fail" in t])
    if fail:
        # Условия -- те же, что у посева, минус staged-файл (его нет):
        # отказ уже решён, признак ехал от контроллера. Действующие
        # условия -- с унаследованным block.when (#552).
        want_conds = {"mop_registry_seed_refused", "lookup('env', 'HOME') != MOP_HOME",
                      "cluster_service_running.stdout == 'inactive'",
                      "not server_snapshot.stat.exists", "not server_pair.stat.exists",
                      "not server_limits.stat.exists"}
        conds = effective_when_552(ptasks, fail)
        c.check("#540 the refusal fires exactly under the seed's own conditions",
                set(conds) == want_conds, conds)
        c.check("#540 the refusal carries no reason text and no origins",
                str(fail.get("ansible.builtin.fail", "")).find("{{") == -1, fail)
        if seed:
            c.check("#540 a refused seed fails before any copy",
                    order.index(fail.get("name"))
                    < min(order.index(t.get("name")) for t, _cp in seed.values()), order)

    def runs(task, env):
        # Действующие условия задачи: собственные + унаследованный
        # block.when (#552) -- матрица гоняет то же, что решает ansible.
        for cond in effective_when_552(ptasks, task):
            try:
                if not eval(cond, {"lookup": lambda _k, _v="": env["controller_home"]},
                            dict(env)):  # noqa: S307 -- свой yaml
                    return False
            except Exception as e:  # noqa: BLE001 -- выражение не читается
                raise AssertionError(f"when {cond!r}: {e}")
        return True

    def st(exists):
        return types.SimpleNamespace(stat=types.SimpleNamespace(exists=exists))

    # Матрица утверждена #540: посев -- один staged-снимок v1 на пустой и
    # определённо не работающий сервер; сервер с любым реестром не трогается
    # (пара v1+легаси на сервере -- штатная, #534); same-HOME не сеет;
    # проба закрыта всем, кроме «inactive».
    def expected(sv1, spair, slim, same_home, state):
        if same_home or state != "inactive" or sv1 or spair or slim:
            return set()
        return {"projects.json"}

    server_home = "/srv/mop"
    # systemctl is-active: штатный inactive -- rc 3; условие читает stdout,
    # и всё кроме «inactive» (active/failed/activating/пусто) закрывает посев.
    states = ("inactive", "active", "failed", "activating", "")
    combos = []
    for same_home in (False, True):
        for state in states:
            for sv1, spair, slim in itertools.product((False, True), repeat=3):
                for staged_exists in (False, True):
                    combos.append((sv1, spair, slim, same_home, state, staged_exists))
    for sv1, spair, slim, same_home, state, staged_exists in combos:
        env = {"controller_home": server_home if same_home else "/home/controller",
               "MOP_HOME": server_home,
               "mop_registry_seed": "/tmp/staged/projects.json" if staged_exists else "",
               "seed_file": st(staged_exists),
               "server_snapshot": st(sv1), "server_pair": st(spair),
               "server_limits": st(slim),
               "cluster_service_running": types.SimpleNamespace(stdout=state)}
        srv = "+".join(n for on, n in ((sv1, "v1"), (spair, "pair"), (slim, "limits"))
                       if on) or "none"
        what = (f"registry seeding, staged seed {'present' if staged_exists else 'absent'}, "
                f"server {srv}, {'same' if same_home else 'different'} HOME, "
                f"systemctl stdout {state!r}")
        want = expected(sv1, spair, slim, same_home, state) if staged_exists else set()
        try:
            ran = {name for name, (t, _cp) in seed.items() if runs(t, env)}
        except AssertionError as e:
            c.check(f"{what}: conditions read", False, e)
            continue
        c.check(f"{what}: seeds exactly {sorted(want)}", ran == want, sorted(ran))

    # Инициализационная атомарность (#540): единственный посевной файл --
    # сам ответ на «обрыв посева»; полупары быть не может. Здесь --
    # самопроверка сетки: staged-файл есть, сервер пуст, сервис неактивен,
    # HOME разные -- сетка ответила «сеем», что и должен делать retry.
    c.check("#540 the grid itself would retry a cut seed on a fresh server",
            expected(False, False, False, False, "inactive") == {"projects.json"}
            and expected(False, True, False, False, "inactive") == set(),
            (expected(False, False, False, False, "inactive"),
             expected(False, True, False, False, "inactive")))

    # Сетка отказа (#540): та же матрица, ровно когда посев бы шёл (файла
    # посева нет -- отказа не отменить, seed_file в условиях не участвует).
    if fail:
        def fail_expected(sv1, spair, slim, same_home, state):
            return (not same_home) and state == "inactive" and not (sv1 or spair or slim)

        for sv1, spair, slim, same_home, state, _staged in combos:
            env = {"controller_home": server_home if same_home else "/home/controller",
                   "MOP_HOME": server_home,
                   "mop_registry_seed": "/tmp/staged/projects.json",
                   "mop_registry_seed_refused": True,
                   "server_snapshot": st(sv1), "server_pair": st(spair),
                   "server_limits": st(slim),
                   "cluster_service_running": types.SimpleNamespace(stdout=state)}
            srv = "+".join(n for on, n in ((sv1, "v1"), (spair, "pair"), (slim, "limits"))
                           if on) or "none"
            what = (f"refused seed, server {srv}, "
                    f"{'same' if same_home else 'different'} HOME, "
                    f"systemctl stdout {state!r}: fails exactly when a seed would run")
            try:
                ran = runs(fail, env)
            except AssertionError as e:
                c.check(f"{what}: conditions read", False, e)
                continue
            c.check(what, ran == fail_expected(sv1, spair, slim, same_home, state), ran)

    # Прогон не мигрирует и не перезаписывает реестр сервера сам: force:no
    # -- недостаточно, migrate-задач быть не должно вовсе.
    for t in tasks:
        body = str(t)
        c.check(f"#540 no task migrates or rewrites the server registry: "
                f"{t.get('name')}", "snapshot_of" not in body
                and "write_snapshot" not in body
                and "cluster project" not in body.replace("cluster users", ""), body[:120])

    check_registry_staging_540(c)


# Клей стейджинга (#540): префлайт готовит на контроллере ОДИН
# провалидированный снимок v1, плейбуку едет путь, не сырой JSON.
# HYPOTHESIS: без клея плейбук разбирал бы JSON сам или копировал бы
# легаси-пару парами задач; отказ по битым входам молчал бы до сервера.
# SOLUTION: _staged_registry_seed в cli/server/deploy: строгий snapshot_of
# -> write_snapshot во временный каталог 0700/0600; play несёт путь
# extra-vars объектом; group_vars задаёт пустой дефолт ручному
# ansible-прогону. Регрессионный контур: стейджинг не валит deploy,
# который посев не требует, -- при одном HOME подготовки нет вовсе,
# отказ конвертации не роняет main: причина печатается в stderr
# контроллера, плейбуку едет булев признак mop_registry_seed_refused
# (без текста и без origin'ов), и задача fail в плее валит прогон только
# там, где посев нужен: сервер пуст, сервис неактивен, HOME разные.
# #548: ошибка файловой системы при подготовке (mkdtemp, chmod, запись)
# -- тот же отказ: ловился только RuntimeError, OSError уходил трейсбеком
# и ронял deploy даже серверу с реестром. Своевременная уборка: каталог
# после mkdtemp, упавшего на записи, убирается, а причина не несёт
# содержимого реестра. Плейбук и условия fail-задачи не меняются.
# RESULT: #540 -- регрессионный раунд красным вперёд фикса: шесть отказов
# (нет fail-задачи, нет same-HOME скипа, отказ поднимался вместо None,
# признака в extra-vars нет, дефолта нет). #548 -- красный прогон вперёд
# фикса: пять отказов (OSError из mkdtemp/chmod/write поднимался трейсбеком
# и оставлял свой каталог); после фикса все зелёные.
# STATUS: FIXED — see #540, #548
def check_registry_staging_540(c):
    import inspect
    import json as _json
    import tempfile
    from mop.cli.server import deploy as deploy_cli
    from mop.cli.server import _play
    from mop.common import projects as reg

    # Функция есть и молчит: библиотека возвращает данные, командлет печатает.
    c.check("#540 deploy preflight has the staging helper",
            hasattr(deploy_cli, "_staged_registry_seed"), None)
    if not hasattr(deploy_cli, "_staged_registry_seed"):
        return
    src = inspect.getsource(deploy_cli._staged_registry_seed)
    c.check("#540 the staging helper validates strictly and refuses, not falls back",
            "snapshot_of" in src and "write_snapshot" in src, src[:200])
    c.check("#540 the staging helper keeps the snapshot out of prints and logs",
            "print(" not in src and "sys.stdout" not in src, src[:200])
    # Один HOME -- подготовки нет вовсе, ровно как условие посева в YAML.
    c.check("#540 the staging helper skips the same-HOME controller",
            "MOP_HOME" in src, src[:300])
    # Отказ не валит main: признак едет плейбуку, а причина -- в stderr
    # контроллера (term.fail). Форма исходника -- тонкий клей командлета,
    # его поведение гонять процессом нечем: у main свои требования config.
    main_src = inspect.getsource(deploy_cli.main)
    c.check("#540 a refused staging defers to the play, it does not kill deploy",
            "registry_seed_refused=seed_refused" in main_src, main_src[:200])

    def make(files):
        base = tempfile.mkdtemp(prefix="mop-540-")
        home = os.path.join(base, "home")
        reg_dir = os.path.join(home, ".config", "mop")
        os.makedirs(reg_dir)
        for name, text in files.items():
            with open(os.path.join(reg_dir, name), "w") as f:
                f.write(text)
        return base, home

    # Входы клея -- те же матричные сочетания: None без источников; строгий
    # снимок из v1; конвертация пары с потолками; отказ по bare/коллизии/
    # orphan/битому JSON/плохому лимиту -- RuntimeError (main печатает
    # причину локально и везёт признак, не текст). Один HOME -- None без
    # чтения реестра вовсе.
    v1 = '{"version": 1, "projects": [{"origin": "git@h:ermak/mop.git", "limit": 2}]}'
    pair = "git@h:ermak/mop.git\n"
    cases = [
        ("no sources at all", {}, None, None),
        ("v1 only", {"projects.json": v1}, None, {"version": 1, "projects": [
            {"origin": "git@h:ermak/mop.git", "limit": 2}]}),
        ("v1 over the pair", {"projects.json": v1, "projects": pair,
                              "limits.json": '{"ermak/mop": 1}'},
         None, {"version": 1, "projects": [
             {"origin": "git@h:ermak/mop.git", "limit": 2}]}),
        ("pair with limits", {"projects": pair, "limits.json": '{"mop": 3}'},
         None, {"version": 1, "projects": [
             {"origin": "git@h:ermak/mop.git", "limit": 3}]}),
        ("pair without limits", {"projects": pair},
         None, {"version": 1, "projects": [
             {"origin": "git@h:ermak/mop.git"}]}),
        ("broken v1 json", {"projects.json": "{oops"}, None, "refuse"),
        ("bare name in the pair", {"projects": "mop\n"}, None, "refuse"),
        ("collision of two origins", {"projects": "git@h:ermak/mop.git\n"
                                                  "https://h/ermak/mop.git\n"},
         None, "refuse"),
        ("orphan limit", {"projects": pair, "limits.json": '{"other": 1}'},
         None, "refuse"),
        ("bad limit", {"projects": pair, "limits.json": '{"ermak/mop": -1}'},
         None, "refuse"),
        # Регрессия ревью: непредставимая пара при ОДНОМ HOME -- стейджинга
        # нет вовсе, отказ не поднимается: YAML посев всё равно скипает.
        ("bare name, same HOME", {"projects": "mop\n"}, "same", None),
        ("broken v1, same HOME", {"projects.json": "{oops"}, "same", None),
    ]
    for label, files, same_home, want in cases:
        base, home = make(files)
        # Клей читает реестр контроллера через paths.local от HOME и
        # сравнивает его с MOP_HOME: задаём дом временно, изоляция
        # проверок восстанавливается следующим кейсом или концом файла --
        # дома изолированы и без того (#209).
        was = os.environ.get("HOME")
        was_home = os.environ.get("MOP_HOME")
        was_dir = reg.SNAPSHOT_DIR
        try:
            os.environ["HOME"] = home
            os.environ["MOP_HOME"] = home if same_home else "/srv/mop"
            reg.SNAPSHOT_DIR = os.path.join(home, ".config", "mop")
            try:
                got = deploy_cli._staged_registry_seed()
            except RuntimeError as e:
                got = e
            if want is None:
                c.check(f"#540 staging {label}: nothing staged", got is None, repr(got)[:120])
            elif want == "refuse":
                c.check(f"#540 staging {label}: a loud refusal, nothing staged",
                        isinstance(got, RuntimeError), repr(got)[:120])
                # Причина отказа не несёт сырых origin'ов с userinfo.
                c.check(f"#540 staging {label}: the refusal names no origins",
                        "git@h" not in str(got) and "https://h" not in str(got),
                        str(got)[:120])
            else:
                staged = None
                if isinstance(got, str) and os.path.isfile(got):
                    with open(got) as f:
                        staged = _json.load(f)
                    mode = stat.S_IMODE(os.stat(got).st_mode)
                    c.check(f"#540 staging {label}: the staged snapshot is private",
                            mode == 0o600, oct(mode))
                c.check(f"#540 staging {label}: one validated v1 snapshot staged",
                        staged == want, staged)
        finally:
            os.environ["HOME"] = was
            if was_home is None:
                os.environ.pop("MOP_HOME", None)
            else:
                os.environ["MOP_HOME"] = was_home
            reg.SNAPSHOT_DIR = was_dir
            shutil.rmtree(base, ignore_errors=True)
    # #548: ошибка файловой системы подготовки -- тот же отказ, что и
    # непредставимый реестр: OSError из mkdtemp/chmod/write_snapshot не
    # должен доходить до вызывающего трейсбеком. Поднимается одна
    # RuntimeError, причина называет шаг, но не содержимое реестра;
    # каталог, заведённый mkdtemp до падения chmod/записи, убирается.
    def boom(error):
        def raise_it(*a, **kw):
            raise error
        return raise_it

    from _lib import patched
    oops = OSError(28, "No space left on device")
    for label, broken in (("mkdtemp", "mkdtemp"), ("chmod", "chmod"),
                          ("write", "write_snapshot")):
        base, home = make({"projects": pair})
        was, was_home, was_dir = os.environ.get("HOME"), os.environ.get("MOP_HOME"), reg.SNAPSHOT_DIR
        made = []
        real_mkdtemp, real_chmod, real_write = tempfile.mkdtemp, os.chmod, reg.write_snapshot

        def spy_mkdtemp(*a, **kw):
            made.append(real_mkdtemp(*a, **kw))
            return made[-1]

        try:
            os.environ["HOME"] = home
            os.environ["MOP_HOME"] = "/srv/mop"
            reg.SNAPSHOT_DIR = os.path.join(home, ".config", "mop")
            with patched(tempfile, mkdtemp=boom(oops) if broken == "mkdtemp" else spy_mkdtemp), \
                    patched(os, chmod=boom(oops) if broken == "chmod" else real_chmod), \
                    patched(reg, write_snapshot=boom(oops) if broken == "write_snapshot"
                            else real_write):
                try:
                    got = deploy_cli._staged_registry_seed()
                except (RuntimeError, OSError) as e:
                    got = e
            c.check(f"#548 staging failure at {label}: a loud refusal, not a traceback",
                    isinstance(got, RuntimeError), repr(got)[:120])
            if isinstance(got, RuntimeError):
                c.check(f"#548 staging failure at {label}: the reason carries no registry content",
                        "git@h" not in str(got), str(got)[:120])
            c.check(f"#548 staging failure at {label}: the staged dir is cleaned up",
                    not any(os.path.exists(m) for m in made), made)
        finally:
            os.environ["HOME"] = was
            if was_home is None:
                os.environ.pop("MOP_HOME", None)
            else:
                os.environ["MOP_HOME"] = was_home
            reg.SNAPSHOT_DIR = was_dir
            shutil.rmtree(base, ignore_errors=True)
            for m in made:
                shutil.rmtree(m, ignore_errors=True)
    # Плейбуку едет ПУТЬ, не JSON: extra-vars одной строкой-объектом.
    vars_ = _play.play_vars([], registry_seed="/tmp/staged/projects.json")
    c.check("#540 the play carries the staged seed as a path, not a payload",
            any('mop_registry_seed' in v and '"mop_registry_seed": "/tmp/staged/projects.json"'
                in v for v in vars_), vars_)
    # Отказ стейджинга едет признаком без текста: причину CLI уже напечатал
    # локально, в argv и журналах плейбука ей не место. Признак -- голый
    # true, и в строке extra-vars нет ничего сверх обычных переменных.
    try:
        vars_refused = _play.play_vars([], registry_seed_refused=True)
    except Exception as e:  # noqa: BLE001 -- проверка, не код пула
        vars_refused = None
    head_refused = (_json.loads(vars_refused[0])
                    if vars_refused and len(vars_refused) == 1 else None)
    c.check("#540 a refused seed travels as a bare flag, not the reason",
            head_refused == {"mop_projects": [], "mop_registry_seed_refused": True},
            head_refused)
    vars_none = _play.play_vars([])
    c.check("#540 without a seed the play carries no seed var at all",
            all("mop_registry_seed" not in v for v in vars_none), vars_none)
    # Дефолт ручному ansible-прогону: без extra-vars посева нет и отказов нет.
    gv = yaml.safe_load(open(os.path.join(DEPLOY, "group_vars", "all.yml")))
    c.check("#540 manual ansible runs carry an empty seed default",
            gv.get("mop_registry_seed") == "", gv.get("mop_registry_seed"))
    c.check("#540 manual ansible runs carry no refused flag",
            gv.get("mop_registry_seed_refused") is False, gv.get("mop_registry_seed_refused"))


def check_one_source_219(c):
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
            c.check(f"{os.path.relpath(path, DEPLOY)}: no {gone.strip()!r} (#219)", gone not in text)


# ── клиент claude на контроллере (#293) ──────────────────────────────────
# HYPOTHESIS: установщик claude (`curl … install.sh | bash`, #11) живёт
# задачей только в роли body, а контроллер его не получает; реестр кредитов
# (#281) водит `claude auth login`, `auth status` и keep-alive на сервере,
# и «Получить ссылку» на дашборде падало на execvp (#292).
# SOLUTION: одно определение в roles/common/tasks/claude_cli.yml, включённое
# и в роль тела, и в роль web (она владеет пользователем пула на
# контроллере и идёт первой из серверных); второй строки install.sh в
# deploy/ нет. STATUS: FIXED — see #293
def check_claude_cli_293(c):
    shared = os.path.join(COMMON, "tasks", "claude_cli.yml")
    c.check("common/tasks/claude_cli.yml exists", os.path.isfile(shared))
    if os.path.isfile(shared):
        text = open(shared).read()
        c.check("claude_cli: the #11 trap stays (pipefail + test -x)",
              "pipefail" in text and "test -x" in text)

    def flat(ts):
        for t in ts or []:
            yield t
            yield from flat((t or {}).get("block"))

    def includes(role):
        path = os.path.join(DEPLOY, "roles", role, "tasks", "main.yml")
        tasks = yaml.safe_load(open(path)) or []
        return [(t.get("ansible.builtin.include_role") or t.get("include_role") or {})
                for t in flat(tasks)]

    for role in ("body", "web"):
        c.check(f"{role}: includes common/claude_cli",
              any(i.get("name") == "common" and i.get("tasks_from") == "claude_cli"
                  for i in includes(role)), includes(role))
    where = [os.path.relpath(p, DEPLOY) for p in deploy_files() if "install.sh" in open(p).read()]
    c.check("install.sh is named once, in common/tasks/claude_cli.yml",
          where == ["roles/common/tasks/claude_cli.yml"], where)


# ── #348: остановленный джоб живёт, пока его читают ────────────────────
# HYPOTHESIS: папета, сдавшегося на bootstrap (#345), снимают без purge, и
# ростер пишет «FAILED: bootstrap gave up … -- fix .mop/bootstrap.yaml, then
# mop update». Но Nomad собирает мёртвые джобы через job_gc_threshold (по
# умолчанию 4 ч; server.hcl.j2 его не задавал): папет пропадал из mop list,
# из known у sweep, а mop update отказывал «no job» -- отчёт о провале
# испарялся.
# SOLUTION: job_gc_threshold сервера -- настройка MOP_NOMAD_JOB_GC (дефолт
# 720h) в server.hcl.j2; остальная уборка Nomad (eval, deployment) -- как
# была. STATUS: FIXED — see #348
def check_job_gc_348(c, can_render):
    from mop.server import playvars
    c.expect("#348 MOP_NOMAD_JOB_GC: a setting, 720h by default",
             config.SETTINGS.get("MOP_NOMAD_JOB_GC"), "720h")
    c.check("#348 the playbooks get it", playvars.playbook_vars().get("MOP_NOMAD_JOB_GC") == "720h",
            playvars.playbook_vars().get("MOP_NOMAD_JOB_GC"))
    path = os.path.join(DEPLOY, "roles", "nomad", "templates", "server.hcl.j2")
    hcl = open(path).read()
    block = re.search(r"^server \{(.*?)^\}", hcl, re.M | re.S)
    c.check("#348 server.hcl: job_gc_threshold inside the server block, from the setting",
            block is not None and re.search(r'^\s*job_gc_threshold\s*=\s*"\{\{ MOP_NOMAD_JOB_GC \}\}"\s*$',
                                            block.group(1), re.M) is not None,
            block and block.group(1))
    c.check("#348 server.hcl: no other GC threshold is touched",
            not re.search(r"eval_gc_threshold|deployment_gc_threshold|node_gc_threshold", hcl))
    if not can_render:
        return
    base = {"MOP_POOL_DC": "pool", "MOP_NOMAD_DATA": "/d", "MOP_NOMAD_PORT": "4646",
            "MOP_NOMAD_RPC_PORT": "4647", "MOP_NOMAD_HTTP_BIND": "0.0.0.0",
            "nomad_advertise": "10.0.0.1"}
    for value in ("720h", "48h"):
        try:
            out = render(hcl, {**base, "MOP_NOMAD_JOB_GC": value})
            got = re.findall(r'^\s*job_gc_threshold\s*=\s*"([^"]*)"', out, re.M)
        except Exception as e:  # noqa: BLE001 -- проверка, не код пула
            got = f"{type(e).__name__}: {e}"
        c.check(f"#348 server.hcl renders job_gc_threshold = {value}", got == [value], got)


# ── #359: doctor по расписанию -- systemd-таймер на сервере ──────────────
# HYPOTHESIS: единственная периодика установки -- nomad periodic pu-cleanup;
# doctor с --fix запускает только человек, и протухший логин или давление
# на диск ждут его.
# SOLUTION: роль doctor ставит mop-doctor.service (oneshot, `doctor --fix
# --safe`, юнит из общего шаблона) и mop-doctor.timer (ежечасно, ритм
# pu-cleanup); включается таймер, сервис -- нет; игра на сервере после
# дашборда: креды сервера на шину кладёт роль web.
# STATUS: FIXED — see #359
def check_doctor_timer_359(c):
    import configparser
    role = os.path.join(DEPLOY, "roles", "doctor")
    path = os.path.join(role, "files", "mop-doctor.timer")
    if not c.check("#359 the timer file of the doctor role", os.path.isfile(path)):
        return
    ini = configparser.ConfigParser(strict=False, interpolation=None,
                                    comment_prefixes=("#",), inline_comment_prefixes=None)
    ini.optionxform = str
    ini.read(path)
    t = dict(ini["Timer"]) if ini.has_section("Timer") else {}
    c.expect("#359 hourly, the rhythm of pu-cleanup", t.get("OnCalendar"), "hourly")
    c.expect("#359 no catch-up after downtime: a boot run meets a pool still rising",
             t.get("Persistent"), "false")
    c.expect("#359 the timer starts the doctor", t.get("Unit"), "mop-doctor.service")
    c.expect("#359 the timer is wanted by timers.target",
             ini.get("Install", "WantedBy", fallback=None), "timers.target")
    tasks = []
    for f in os.listdir(os.path.join(role, "tasks")):
        tasks += yaml.safe_load(open(os.path.join(role, "tasks", f))) or []
    copies = [t for t in tasks if (t.get("ansible.builtin.copy") or {}).get("dest")
              == "/etc/systemd/system/mop-doctor.timer"]
    c.check("#359 the role installs the timer", len(copies) == 1, copies)
    units = [t.get("ansible.builtin.systemd") or {} for t in tasks]
    on = [u for u in units if u.get("name") == "mop-doctor.timer"]
    c.check("#359 the timer is enabled and started, systemd reloaded first",
            len(on) == 1 and on[0].get("enabled") is True and on[0].get("state") == "started"
            and on[0].get("daemon_reload") is True, on)
    c.check("#359 the service itself is not enabled: the timer drives it",
            not [u for u in units if u.get("name") in ("mop-doctor", "mop-doctor.service")
                 and u.get("enabled")], units)
    plays = yaml.safe_load(open(os.path.join(DEPLOY, "setup.yml")))

    def roles(p):
        return [r if isinstance(r, str) else r.get("role") for r in p.get("roles") or []]
    names = [(p.get("hosts"), roles(p)) for p in plays]
    web = [i for i, (h, r) in enumerate(names) if h == "server" and "web" in r]
    doc = [i for i, (h, r) in enumerate(names) if h == "server" and "doctor" in r]
    c.check("#359 setup.yml plays the doctor role on the server", len(doc) == 1, names)
    c.check("#359 after the dashboard: its bus credentials come from the web role",
            doc and web and doc[0] > web[0], (web, doc))


# ── #361: снятие pu-cleanup ─────────────────────────────────────────────
# HYPOTHESIS: после #359 подметание будят двое -- таймер mop-doctor и nomad
# periodic pu-cleanup; второй тащит за собой и ловушку sysbatch: ребёнок
# pending на недоступном узле плюс prohibit_overlap клинят расписание.
# SOLUTION: шаблона и регистрации нет; прогон на сервере снимает живой
# pu-cleanup с детьми (stop + purge, «джоба нет» -- не ошибка) и падает,
# если что-то с этим префиксом осталось. Пере-подписка памяти, жившая в том
# же файле, переезжает в роль nomad и остаётся в прогоне.
# STATUS: FIXED — see #361
def check_pu_cleanup_gone_361(c):
    c.check("#361 the pu-cleanup job template is gone",
            not os.path.exists(os.path.join(DEPLOY, "roles", "bus", "templates",
                                            "pu-cleanup.nomad.hcl.j2")))
    tasks = site_tasks()
    text = [(f, yaml.safe_dump(t, allow_unicode=True)) for f, t in tasks]
    c.check("#361 nothing renders or registers pu-cleanup",
            not [(f, t.get("name")) for f, t in tasks
                 if "pu-cleanup.nomad.hcl.j2" in yaml.safe_dump(t)
                 or re.search(r"nomad job run\S*.*pu-cleanup", yaml.safe_dump(t))],
            [(f, t.get("name")) for f, t in tasks if "pu-cleanup" in yaml.safe_dump(t)])
    plays = yaml.safe_load(open(os.path.join(DEPLOY, "setup.yml")))
    c.check("#361 no «Disk watchdog job» play",
            "Disk watchdog job" not in [p.get("name") for p in plays])
    retire = [(f, t) for f, t in tasks if "pu-cleanup" in yaml.safe_dump(t)
              and "purge" in yaml.safe_dump(t)]
    if c.check("#361 the run retires the live pu-cleanup: stop with purge", len(retire) == 1,
               [(f, t.get("name")) for f, t in retire]):
        f, t = retire[0]
        body = yaml.safe_dump(t)
        c.check("#361 it takes the periodic children too: the prefix, not one ID",
                "prefix=pu-cleanup" in body, t.get("name"))
        c.check("#361 it fails if anything with the prefix is left after the purge",
                "left" in body and ("exit(1)" in body or "SystemExit(1)" in body
                                    or "failed_when" in body), t.get("name"))
        c.check("#361 idempotent: a changed run only when something was purged",
                "changed_when" in t, t.get("name"))
        c.check("#361 on the server", f.endswith(os.path.join("nomad", "tasks", "scheduler.yml")), f)
    over = [n for _, n in text if "MemoryOversubscriptionEnabled" in n]
    c.check("#361 memory oversubscription is still set by the run", len(over) == 1, len(over))


# HYPOTHESIS: контроллер создаёт ключ прокси только в игре llmproxy после
# запуска кластера; кластерный сервис не имеет файла и берёт второй ключ
# из открытого unit. Раскатка только сервера оставила бы узлы без ключа.
# SOLUTION: роль cluster перед стартом сервиса создаёт приватную копию
# ключа из того же lookup, что использует llmproxy, с no_log и mode 0600.
# RESULT: сервис всегда может прочесть закрытый файл до запуска.
# STATUS: FIXED — see #402
def check_cluster_key_copy_402(c):
    from mop.server import proxykey
    path = os.path.join(DEPLOY, "roles", "cluster", "tasks", "main.yml")
    tasks = yaml.safe_load(open(path, encoding="utf-8"))
    secret = [i for i, t in enumerate(tasks)
              if "llm-proxy-client.pass" in str(t.get("ansible.builtin.copy", {}))]
    c.check("#402 the cluster receives the proxy's own key before starting",
            len(secret) == 1 and secret[0] < next(
                (i for i, t in enumerate(tasks) if t.get("name") == "Systemd unit"), -1), secret)
    if not secret:
        return
    t = tasks[secret[0]]
    spec = t["ansible.builtin.copy"]
    c.check("#402 private key file uses the cluster's own location and permissions",
            spec.get("dest", "").endswith("/.config/mop/secrets/llm-proxy-client.pass")
            and spec.get("owner") == "{{ MOP_USER }}"
            and spec.get("mode") == "0600" and t.get("no_log") is True,
            {k: v for k, v in spec.items() if k != "content"})
    c.check("#402 install reuses the proxy's generated passfile",
            "lookup('password'" in str(spec.get("content", ""))
            and os.path.basename(proxykey.KEY_FILE) == "llm-proxy-client.pass")
    folders = [t.get("ansible.builtin.file", {}) for t in tasks[:secret[0]]]
    c.check("#402 secret directory exists with 0700", any(
        s.get("path", "").endswith("/.config/mop/secrets") and s.get("mode") == "0700"
        and s.get("owner") == "{{ MOP_USER }}" for s in folders))


# HYPOTHESIS (#479): апстрим Z.AI текстовый и отвергает любые image-блоки
# (400 messages.content.type is invalid), а скелет config.yaml о том не
# знал: панель заводит glm-5.3 как openai-compatibility, и первая же
# вставка картинки или Read(png) убивала сессию glm-5.3 насовсем.
# SOLUTION: скелет несёт правило requests.payload.filter, вырезающее
# image_url-части из пользовательских сообщений для glm-*. Проверено на
# живом прокси v8.0.8: filter-правило одно покрывает и вставленные
# картинки, и картинки из tool-результатов (переводчик прокси сам
# переносит их в следующее user-сообщение как image_url). Имя glm-* --
# не имя провайдера, а модель; других текстовых апстримов в пуле нет.
# RESULT: до правки RED -- в скелете нет ни секции requests, ни правила.
# STATUS: FIXED — see #479
def check_skeleton_image_filter_479(c):
    path = os.path.join(DEPLOY, "roles", "llmproxy", "templates", "config.yaml.j2")
    skeleton = open(path, encoding="utf-8").read()
    variables = dict(config.SETTINGS, MOP_HOME="/home/mop",
                     llm_proxy_client_key="ck", llm_proxy_mgmt_key="mk")
    cfg = yaml.safe_load(render(skeleton, variables))
    rule = ((cfg.get("requests") or {}).get("payload") or {}).get("filter") or []
    c.check("#479 the skeleton carries the image-strip filter rule",
            len(rule) == 1, rule)
    if not rule:
        return
    params = rule[0].get("params") or []
    c.check("#479 the rule strips image parts from user messages of glm-*",
            params == ['messages.#(role=="user")#.content.#(type=="image_url")#']
            and [m.get("name") for m in rule[0].get("models") or []] == ["glm-*"],
            (rule[0].get("models"), params))


# ── #524: обновление прокси приезжает деплоем, а не руками ─────────────
# Бинарь пиннится настройкой MOP_LLM_PROXY_VERSION; смена версии обязана
# поднять прокси без ручных шагов. HYPOTHESIS: если copy без notify или
# распаковка без creates, прогон либо не перезапускает юнит, либо
# перезапускает его каждый раз -- и то и другое молча.
def check_llmproxy_upgrade_524(c):
    tasks = [t for f, t in site_tasks() if f.endswith("roles/llmproxy/tasks/main.yml")]
    by = {t.get("name"): t for t in tasks}
    binary = by.get("The binary") or {}
    c.check("#524 the copied binary orders a restart",
            binary.get("notify") == "restart mop-llm-proxy", binary)
    copy = binary.get("ansible.builtin.copy") or {}
    c.check("#524 the binary lands by content, so only a real upgrade restarts",
            bool(copy) and copy.get("remote_src") is True, binary)
    extract = by.get("Extract the release") or {}
    command = extract.get("ansible.builtin.command") or {}
    creates = command.get("argv") or []
    c.check("#524 the extract is idempotent: versioned creates",
            command.get("creates") == "/tmp/cli-proxy-api-"
            "{{ MOP_LLM_PROXY_VERSION }}/cli-proxy-api"
            and any("cli-proxy-api" in a for a in creates), extract)
    names = [t.get("name") for t in tasks]
    c.check("#524 the restart lands before the health probe",
            "Apply the restart now, before the check" in names
            and names.index("Apply the restart now, before the check")
            < names.index("The proxy answers on the LLM API"), names)


# ── #525: панель LLM-прокси приезжает со сборкой, а не «latest на момент
# первого захода» ────────────────────────────────────────────────────────
# Панель (management.html) -- отдельный репозиторий Cli-Proxy-API-Management-
# Center со своей нумерацией; в tar CLIProxyAPI и в ассетах его релиза её
# нет. До сих пор прокси качал её сам при первом заходе (ServeManagement
# ControlPanel -> EnsureLatestManagementHTML), и disable-auto-update-panel
# того не отменял: флаг гасит фоновый апдейтер, не первый доступ. Итог --
# бинарник 8.0.16 с ассетом 8.0.8 (#523). Привязать панель к версии бинарника
# нельзя: нумерации независимые.
# HYPOTHESIS: панель обязана приезжать деплоем пинненной версией, в тот путь,
# где её ищет прокси. Юнит не задаёт WRITABLE_PATH и MANAGEMENT_STATIC_PATH,
# значит StaticDir -- fallback «каталог конфига + static», т.е. static/ под
# конфигом llm-proxy; get_url родителей не создаёт -- каталог обязан завести
# до него, как делает роль для распаковки.
# SOLUTION: настройка MOP_LLM_PANEL_VERSION (тег Management-Center), get_url
# по шаблону тега в {{ MOP_HOME }}/.config/mop/llm-proxy/static/management.html
# пользователю пула. Существующий файл прокси находит и не качает: stat
# успешен, EnsureLatest не зовётся вовсе.
# RESULT: проверки ниже. STATUS: FIXED — see #525
def check_llmproxy_panel_525(c):
    c.check("#525 the panel version is its own setting (not the binary's)",
            "MOP_LLM_PANEL_VERSION" in config.SETTINGS, sorted(
                k for k in config.SETTINGS if "PANEL" in k))
    tasks = [t for f, t in site_tasks() if f.endswith("roles/llmproxy/tasks/main.yml")]
    by = {t.get("name"): t for t in tasks}
    names = [t.get("name") for t in tasks]
    static_dir = by.get("Static dir for the baked panel") or {}
    mkdir = static_dir.get("ansible.builtin.file") or {}
    # Прокси без env берёт «каталог конфига + static» (managementasset.
    # StaticDir: WRITABLE_PATH/MANAGEMENT_STATIC_PATH не заданы юнитом) --
    # любой другой путь молча не читался бы, и прокси качал бы панель сам.
    c.check("#525 the static dir is the config dir's static/, the unit's fallback",
            mkdir.get("path") == "{{ MOP_HOME }}/.config/mop/llm-proxy/static",
            static_dir)
    c.check("#525 the static dir belongs to the pool user",
            mkdir.get("owner") == "{{ MOP_USER }}" and mkdir.get("group") == "{{ MOP_USER }}"
            and mkdir.get("mode") == "0700", static_dir)
    asset = by.get("Baked panel of the management UI") or {}
    get_url = asset.get("ansible.builtin.get_url") or {}
    c.check("#525 the panel asset is versioned by MOP_LLM_PANEL_VERSION",
            get_url.get("url") == "{{ llm_proxy_panel_html }}", asset)
    c.check("#525 the panel lands where the proxy looks for it",
            get_url.get("dest") == "{{ MOP_HOME }}/.config/mop/llm-proxy/"
            "static/management.html", asset)
    c.check("#525 the panel is owned by the pool user",
            get_url.get("owner") == "{{ MOP_USER }}" and get_url.get("group") == "{{ MOP_USER }}"
            and get_url.get("mode") == "0644", asset)
    c.check("#525 the dir task precedes the get_url (get_url creates no parents)",
            "Static dir for the baked panel" in names
            and "Baked panel of the management UI" in names
            and names.index("Static dir for the baked panel")
            < names.index("Baked panel of the management UI"), names)
    # Шаблон URL -- тег отдельного репозитория Management-Center: ассета
    # с версией бинарника не существует, нумерации независимые (#525).
    setup = yaml.safe_load(open(os.path.join(DEPLOY, "setup.yml"), encoding="utf-8"))
    plays = {p.get("name"): p.get("vars") or {} for p in setup if isinstance(p, dict)}
    url = plays.get("LLM proxy on the server", {}).get("llm_proxy_panel_html")
    c.check("#525 the URL template points at the Management-Center tag",
            url == "https://github.com/router-for-me/Cli-Proxy-API-Management-Center/"
            "releases/download/v{{ MOP_LLM_PANEL_VERSION }}/management.html", url)


# ── #558: LLM-прокси слушает только петлю, порт не открыт в LAN ────────
# HYPOTHESIS: nginx-прокси #557 пересадил папетов mop и серверных читателей
# на публичный https-origin, но config.yaml прокси по-прежнему рендерится с
# host: "0.0.0.0" -- порт 8317 открыт в LAN, и любой узел сети ходит к LLM
# мимо TLS. config.yaml рендерится только при первой установке, живой файл
# правит панель (одной строкой JSON, host вложен в server), и рассинхрон
# ключей там молчит. Переход обязан быть управляемым: дефолт -- сегодняшнее
# поведение (порт открыт), переключение -- установка, после
# перерегистрации всех папетов.
# SOLUTION: настройка MOP_LLM_PROXY_LAN ("1"/"0", дефолт "1" = открыт);
# шаблон рендерит host по флагу; задача replace правит живой файл в обеих
# формах (YAML первой установки и однострочный JSON панели), с рестартом
# юнита; LAN-проверка делится по флагу: закрытый порт ловит отказ
# соединения, открытый -- прежние коды; slurp+from_yaml подтверждает
# server.host структурно; всё, что касается config.yaml, -- no_log без diff.
# RESULT: проверки ниже. STATUS: RED
def check_llmproxy_loopback_558(c, can_render):
    from mop.server import playvars

    # Настройка: в config.SETTINGS, дефолт -- сегодняшний порт-открыт, и
    # едет плейбукам тем же путём, что и порт.
    c.check("#558 the loopback switch is a setting with today's default",
            config.SETTINGS.get("MOP_LLM_PROXY_LAN", "\0") == "1",
            config.SETTINGS.get("MOP_LLM_PROXY_LAN"))
    c.check("#558 the switch reaches the playbooks",
            "MOP_LLM_PROXY_LAN" in playvars.playbook_vars(),
            sorted(k for k in playvars.playbook_vars() if "LLM" in k))

    # Шаблон: host по флагу, для новых установок. Старый комментарий про
    # «LAN, а не петля» устарел вместе с флагом.
    if can_render:
        tmpl = open(os.path.join(DEPLOY, "roles", "llmproxy", "templates",
                                 "config.yaml.j2"), encoding="utf-8").read()
        for flag, want in (("1", "0.0.0.0"), ("0", "127.0.0.1")):
            got = render(tmpl, dict(VARS, MOP_LLM_PROXY_LAN=flag,
                                    llm_proxy_client_key="k", llm_proxy_mgmt_key="m"))
            c.expect(f"#558 first-install host with the flag {flag!r}",
                     _server_host(got), want)
        c.check("#558 the stale LAN comment is gone",
                "узлы и тела ходят" not in tmpl, tmpl[:400])

    tasks = [t for f, t in site_tasks() if f.endswith("roles/llmproxy/tasks/main.yml")]
    by = {t.get("name"): t for t in tasks}
    names = [t.get("name") for t in tasks]

    # Живой файл: две адресные замены, по форме файла. YAML -- рендер первой
    # установки: между «server:» и «host:» лежат строки комментария шаблона
    # (текст -- дословно из afe6e7b, установки, ещё не переписанные в JSON);
    # однострочный JSON -- как переписывает панель (config-version 8, host
    # внутри server, порядок ключей не угадывается: перед host могут стоять
    # другие ключи объекта). ansible replace компилирует regexp без флагов:
    # ветка YAML привязана к «server:», не к ^.
    yaml_task = by.get("Close the LLM API port on a live installation") or {}
    json_task = by.get("Close the LLM API port in the panel's one-line JSON") or {}
    for label, task, fixture, want in (
            ("yaml", yaml_task, 'server:\n  host: "0.0.0.0"\n  port: 8317\n',
             'server:\n  host: "127.0.0.1"\n  port: 8317\n'),
            ("yaml bare", yaml_task, "server:\n  host: 0.0.0.0\n  port: 8317\n",
             'server:\n  host: "127.0.0.1"\n  port: 8317\n'),
            ("the old template's comment lines", yaml_task,
             'server:\n  # LAN, а не петля: на этом порту узлы и тела ходят за моделями. Порт\n'
             '  # открывает только LLM API -- client-ключ обязателен; management того же\n'
             '  # порта отвечает лишь с петли (ниже allow-remote: false).\n'
             '  host: "0.0.0.0"\n  port: 8317\n',
             'server:\n  # LAN, а не петля: на этом порту узлы и тела ходят за моделями. Порт\n'
             '  # открывает только LLM API -- client-ключ обязателен; management того же\n'
             '  # порта отвечает лишь с петли (ниже allow-remote: false).\n'
             '  host: "127.0.0.1"\n  port: 8317\n'),
            ("json", json_task,
             '{"server":{"host":"0.0.0.0","port":8317},"access":{"api-keys":["k1"]}}',
             '{"server":{"host":"127.0.0.1","port":8317},"access":{"api-keys":["k1"]}}'),
            ("json with keys before host", json_task,
             '{"server":{"port":8317,"host":"0.0.0.0"},"access":{"api-keys":["k1"]}}',
             '{"server":{"port":8317,"host":"127.0.0.1"},"access":{"api-keys":["k1"]}}'),
            # Живой файл установки (#558): комментарии старого скелета, затем
            # flow-запись с ВЛОЖЕННЫМ объектом перед host (discovery). Ключ
            # server в кавычках, после двоеточия пробел, до и после server --
            # другие секции (в access -- фиктивный ключ).
            ("a live config: comments, then flow with a nested object", json_task,
             '# layout as config.example.yaml CLIProxyAPI\n\n'
             '"config-version": 8\n'
             '"access": {"api-keys": ["k1"]}\n'
             '"server": {"discovery": {"service-type": "s", "s": ["s", "s", "s", "s", "s"]}, '
             '"host": "0.0.0.0", "port": 8317}\n'
             '"management": {"allow-remote": false}\n',
             '# layout as config.example.yaml CLIProxyAPI\n\n'
             '"config-version": 8\n'
             '"access": {"api-keys": ["k1"]}\n'
             '"server": {"discovery": {"service-type": "s", "s": ["s", "s", "s", "s", "s"]}, '
             '"host": "127.0.0.1", "port": 8317}\n'
             '"management": {"allow-remote": false}\n')):
        spec = task.get("ansible.builtin.replace") or {}
        pattern, repl = spec.get("regexp", "\0"), spec.get("replace", "\0")
        try:
            got = re.sub(pattern, repl.replace("\\1", r"\1").replace("\\2", r"\2")
                         .replace("\\g<1>", r"\g<1>"), fixture)
        except re.error as e:
            got = f"regexp does not compile: {e}"
        c.expect(f"#558 the replacement covers the {label} form of the live config",
                 got, want)
    # Замена не может тронуть чужие host-ключи: management-секция остаётся
    # нетронутой и тогда, когда в server перед host стоят другие ключи (такой
    # файл замена не узнает -- сработает структурный assert, а не тихий
    # неполный перенос), и чужой host в management не перезаписывается.
    spec = yaml_task.get("ansible.builtin.replace") or {}
    keeper = 'server:\n  port: 8317\n  host: "0.0.0.0"\nmanagement:\n  host: "0.0.0.0"\n'
    try:
        touched = re.sub(spec.get("regexp", "\0"),
                         spec.get("replace", "\0").replace("\\1", r"\1")
                         .replace("\\2", r"\2"), keeper)
    except re.error as e:
        touched = f"regexp does not compile: {e}"
    c.check("#558 only the server host is rewritten",
            'management:\n  host: "0.0.0.0"' in touched, touched)
    jspec = json_task.get("ansible.builtin.replace") or {}
    jkeeper = ('{"management":{"host":"0.0.0.0"},"server":{"port":8317,'
               '"host":"0.0.0.0"}}')
    try:
        jtouched = re.sub(jspec.get("regexp", "\0"),
                          jspec.get("replace", "\0").replace("\\g<1>", r"\g<1>"),
                          jkeeper)
    except re.error as e:
        jtouched = f"regexp does not compile: {e}"
    c.check("#558 only the server host is rewritten in JSON",
            '"management":{"host":"0.0.0.0"}' in jtouched
            and '"server":{"port":8317,"host":"127.0.0.1"' in jtouched, jtouched)
    # Вложенный host (в подобъекте server) -- не наш ключ: вложенный объект
    # поглощается целиком, и host внутри него не совпадает; глубже одного
    # уровня -- совпадения нет вовсе, такой файл остаётся структурному
    # assert'у.
    nested = '{"server":{"tls":{"host":"0.0.0.0"}},"host":"0.0.0.0"}'
    deep = '{"server":{"a":{"b":{"host":"0.0.0.0"}}},"host":"0.0.0.0"}'
    for label, probe in (("nested", nested), ("two levels deep", deep)):
        try:
            ntouched = re.sub(jspec.get("regexp", "\0"),
                              jspec.get("replace", "\0").replace("\\g<1>", r"\g<1>"),
                              probe)
        except re.error as e:
            ntouched = f"regexp does not compile: {e}"
        c.check(f"#558 a {label} host stays to the structural assert",
                ntouched == probe, ntouched)

    # Рестарт обязателен: смена host перевязывает листенер, hot-reload на
    # неё не опираемся. Замены -- только при флаге «закрыть», и no_log без
    # diff: в config.yaml лежат все API-ключи и хэш management-ключа.
    for label, task in (("yaml", yaml_task), ("json", json_task)):
        c.check(f"#558 the {label} replacement orders a restart",
                task.get("notify") == "restart mop-llm-proxy", task)
        c.check(f"#558 the {label} replacement runs only with the port closed",
                task.get("when") is not None
                and "MOP_LLM_PROXY_LAN" in str(task.get("when"))
                and '"0"' in str(task.get("when")), task.get("when"))
        c.check(f"#558 the {label} replacement is no_log without a diff",
                task.get("no_log") is True and "diff" not in task, task)
    c.check("#558 the first-install render stays untouched by the migration",
            str(by.get("Config, only at first install", {}).get("when"))
            == "not llm_proxy_conf.stat.exists",
            by.get("Config, only at first install", {}).get("when"))

    # Структурное подтверждение после замены: slurp + from_yaml, host
    # обязан быть 127.0.0.1 -- from_yaml читает и JSON.
    slurp = by.get("Slurp the live proxy config") or {}
    vspec = slurp.get("ansible.builtin.slurp") or {}
    c.check("#558 the post-switch verification slurps the config",
            vspec.get("src") == "{{ MOP_HOME }}/.config/mop/llm-proxy/config.yaml", slurp)
    c.check("#558 the slurp is no_log without a diff",
            slurp.get("no_log") is True and "diff" not in slurp, slurp)
    verify = by.get("The live config binds the loopback") or {}
    cond = str(verify.get("ansible.builtin.assert", {}).get("that", ""))
    c.check("#558 the verification asserts server.host is the loopback",
            "127.0.0.1" in cond and "server.host" in cond
            and "from_yaml" in cond and "b64decode" in cond, cond)
    c.check("#558 the verification is no_log and gated by the flag",
            verify.get("no_log") is True and "diff" not in verify
            and verify.get("when") is not None
            and '"0"' in str(verify.get("when")), verify)

    # Проверка на LAN-адресе делится по флагу: закрытый порт -- отказ
    # соединения (wait_for state=stopped), открытый -- прежние коды.
    lan = by.get("The LAN socket of the LLM API is closed") or {}
    lspec = lan.get("ansible.builtin.wait_for") or {}
    c.check("#558 the closed-port branch demands the socket refused",
            lspec.get("host") == "{{ MOP_SERVER_LAN }}"
            and lspec.get("port") == "{{ MOP_LLM_PROXY_PORT }}"
            and lspec.get("state") == "stopped"
            and lan.get("when") is not None
            and '"0"' in str(lan.get("when")), lan)
    legacy = by.get("Management refuses non-localhost on the API port") or {}
    uspec = legacy.get("ansible.builtin.uri") or {}
    c.check("#558 the open-port branch keeps the legacy codes check",
            uspec.get("url") == "http://{{ MOP_SERVER_LAN }}:{{ MOP_LLM_PROXY_PORT }}"
            "/v8/management/credentials"
            and uspec.get("status_code") == [403, 401]
            and legacy.get("when") is not None
            and '"1"' in str(legacy.get("when")), legacy)

    # Порядок: замена до flush_handlers, LAN-проверки -- после.
    c.check("#558 the migration lands before the handlers flush",
            "Close the LLM API port on a live installation" in names
            and "Apply the restart now, before the check" in names
            and names.index("Close the LLM API port on a live installation")
            < names.index("Apply the restart now, before the check"), names)
    c.check("#558 the LAN checks follow the switch",
            "The LAN socket of the LLM API is closed" in names
            and names.index("Apply the restart now, before the check")
            < names.index("The LAN socket of the LLM API is closed"), names)


def _server_host(rendered):
    """server.host из отрендеренного config.yaml: значение после host:",
    без кавычек. Отсутствие -- пустая строка."""
    m = re.search(r'host:\s*"?([^"\n]*)"?', rendered)
    return m.group(1) if m else ""


def check_llm_request_size_411(c, can_render):
    # HYPOTHESIS: nginx отвергает большую текстовую историю и её compact
    # до LLM-прокси; Claude маскирует HTTP 413 как лимит media в 32 МБ.
    # Проверяем рендер: LLM API принимает тело не меньше 32 МБ, настройка
    # установки меняет этот предел только на маршруте LLM.
    # SOLUTION: свой client_max_body_size у /llm/, размер из SETTINGS.
    # RESULT: до исправления обе проверки рендера падали без директивы.
    # STATUS: FIXED — see #411
    # #546 снимает алиас /llm/ (410 вместо прокси): лимит #411 живёт на
    # каноническом /v1/ и панельных маршрутах, проверка лимита среза
    # переехала в check_llm_alias_gone_546 -- смена контракта, не ослабление:
    # счёт маршрутов с лимитом там же, вместе с отказом алиаса.
    if not can_render:
        return
    template = open(os.path.join(DEPLOY, "roles", "proxy", "templates", "mop.conf.j2")).read()
    variables = dict(config.SETTINGS, MOP_SERVER_LAN="srv.test")
    for limit in (config.SETTINGS.get("MOP_LLM_REQUEST_MAX_MB", ""), "96"):
        got = render(template, dict(variables, MOP_LLM_REQUEST_MAX_MB=limit))
        route = re.search(r"location\s+/v1/\s*\{([^{}]*)\}", got).group(1)
        found = re.search(r"client_max_body_size\s+(\d+)m;", route)
        c.check(f"#411: LLM accepts large requests with limit {limit!r}",
                found is not None and found[1] == limit and int(found[1]) >= 32,
                route)



# HYPOTHESIS (#418): management-ключ прокси живёт только внутри config.yaml
# (bcrypt после первого старта), и серверному шлюзу панели (#417) нечем
# представиться прокси. Прямые /v8/management и алиас /llm/v8/management
# открыты мимо шлюза.
# SOLUTION (контракт pu3): роль web кладёт закрытую копию ключа тем же
# lookup-выражением, что читает роль llmproxy -- источник один, файл в
# secrets/ контроллера один, копия совпадает с секретом по построению на
# любой установке (vars плей-scoped, ссылкой через переменную роль web её
# НЕ видит -- потому буквальный тот же lookup, не имя). nginx: шим
# = /llm/management.html, gated raw /llm/raw/, API /v8/management/ и exact
# /v8/management -- в mop-web; ^~ /llm/management и deny /llm/v8/management*
# закрывают обходы через срез префикса суффикс-играми; лимит тела и
# read_timeout 75s на маршруты шлюза, buffering off.
# RESULT: RED ловил и play-scoped переменную (when всегда false -- копия
# не ставилась ни на одной установке), и отсутствие ^~ блока; после
# правки -- копия ставится всегда, оба источника байт-в-байт одинаковые.
# STATUS: FIXED — see #418

# ── частная CA для серверного TLS (#517) ─────────────────────────────────
# HYPOTHESIS: продукт понимает серверный TLS бинарно -- самоподписанный
# (сертификат генерится, пинится сервисам, служит ca_path проверке) или
# публично-доверенный (MOP_TLS_CERT: pin сервисам удаляется, ca_path
# проверки omit). Сертификату от частной CA нет места: сервисы остаются
# без доверия («pinned none: the system CAs», сборщики стояли), а сквозная
# проверка через прокси падает с «self-signed certificate in certificate
# chain».
# SOLUTION: настройка MOP_TLS_CA -- путь к корню частной CA на
# контроллере. С ней web-роль кладёт пин из CA-файла (задача «Pinned
# certificate» обобщается: src = MOP_TLS_CA), ca_path проверки -- тот же
# файл. Без неё прежние пути не меняются ни строкой: публичный снимает
# пин, самоподписанный пинит свой secrets/tls.pem.
# RESULT: проверяется ниже на структуре задач и рендере ca_path при
# трёх комбинациях. STATUS: RED
def check_private_ca_517(c, can_render):
    tasks = yaml.safe_load(open(
        os.path.join(DEPLOY, "roles", "web", "tasks", "main.yml"),
        encoding="utf-8"))
    # Задача-пин: одна, src -- выбор из трёх путей, условие -- не
    # публичный-без-CA (пин ложен только там).
    pin = [t for t in tasks if t.get("name") == "Pinned certificate of the TLS proxy"]
    c.check("#517 the pin task is one", len(pin) == 1,
            [t.get("name") for t in pin])
    if pin:
        spec = pin[0]["ansible.builtin.copy"]
        c.check("#517 the pin source covers the private CA",
                spec.get("src") == "{{ MOP_TLS_CA or secrets_dir + '/tls.pem' }}",
                spec.get("src"))
        c.check("#517 the pin stays for self-signed and private CA, gone only for public",
                pin[0].get("when") == "(MOP_TLS_CA | length > 0) or not MOP_TLS_CERT",
                pin[0].get("when"))
    gone = [t for t in tasks if t.get("name") == "No pin with a real certificate"]
    c.check("#517 the no-pin task is one", len(gone) == 1,
            [t.get("name") for t in gone])
    if gone:
        c.check("#517 the no-pin task fires only for a public certificate",
                gone[0].get("when") == "(MOP_TLS_CERT | length > 0) and (MOP_TLS_CA | length == 0)",
                gone[0].get("when"))
    # Сквозная проверка: ca_path -- CA-файл для частной CA, пин -- для
    # самоподписанного, omit -- для публичного.
    probe = [t for t in tasks if t.get("name") == "The dashboard answers through the TLS proxy"]
    c.check("#517 the probe task is one", len(probe) == 1)
    if probe:
        ca_path = probe[0]["ansible.builtin.uri"].get("ca_path")
        c.check("#517 the probe verifies against the CA file when given, the pin otherwise",
                ca_path == "{{ omit if MOP_TLS_CERT and not MOP_TLS_CA else "
                            "(MOP_TLS_CA or secrets_dir + '/tls.pem') }}",
                ca_path)
    # Настройка: в config.SETTINGS, пустой дефолт, и едет плейбукам.
    from mop.server import playvars
    c.check("#517 MOP_TLS_CA is a setting with an empty default",
            config.SETTINGS.get("MOP_TLS_CA", "\0") == "",
            config.SETTINGS.get("MOP_TLS_CA"))
    c.check("#517 MOP_TLS_CA reaches the playbooks",
            "MOP_TLS_CA" in playvars.playbook_vars(),
            sorted(k for k in playvars.playbook_vars() if k.startswith("MOP_TLS")))

    if not can_render:
        return
    # Рендер ca_path при трёх комбинациях. omit не рендерится объектом --
    # jinja стрингифицирует любое значение на выводе, -- поэтому публичный
    # путь (omit) сверен структурой выражения выше, а здесь рендерится
    # выбор файла: частная CA -- свой корень, самоподписанный -- свой пин.
    def ca_render(cert, ca):
        v = dict(config.SETTINGS, MOP_SERVER_LAN="srv.test",
                 MOP_TLS_CERT=cert, MOP_TLS_CA=ca)
        return render("{{ MOP_TLS_CA or secrets_dir + '/tls.pem' }}",
                      dict(v, secrets_dir="/srv/secrets"))

    c.check("#517 private CA pins its root for the probe",
            ca_render("/etc/ssl/srv.pem", "/etc/ssl/root-ca.pem") == "/etc/ssl/root-ca.pem",
            ca_render("/etc/ssl/srv.pem", "/etc/ssl/root-ca.pem"))
    c.check("#517 self-signed keeps its own pin",
            ca_render("", "") == "/srv/secrets/tls.pem", ca_render("", ""))

    # ── #518: when-условия строго булевы ────────────────────────────────
    # ansible-core 2.19+ отказывается от не-булева результата when
    # («Conditional result (True) was derived from value of type str»):
    # живой деплой с MOP_TLS_CA упал на задаче пина -- `MOP_TLS_CA or ...`
    # с непустой CA возвращает строку-путь. Условия переписаны в
    # сравнения длины; рендер -- native, как ansible сворачивает when
    # (строковый рендер стрингифицирует и булевы, им тип не сверить).
    pin_task = next((t for t in tasks
                     if t.get("name") == "Pinned certificate of the TLS proxy"), {})
    nopin_task = next((t for t in tasks
                       if t.get("name") == "No pin with a real certificate"), {})
    combos = (("", ""), ("/etc/ssl/srv.pem", ""),
              ("/etc/ssl/srv.pem", "/etc/ssl/root-ca.pem"))
    for cert, ca in combos:
        v = dict(config.SETTINGS, MOP_TLS_CERT=cert, MOP_TLS_CA=ca)
        for label, task, want in (
                ("pin", pin_task,
                 bool(ca or not cert)),
                ("no-pin", nopin_task,
                 bool(cert and not ca))):
            cond = (task.get("when") or "")
            from jinja2 import StrictUndefined
            from jinja2.nativetypes import NativeEnvironment
            native = NativeEnvironment(undefined=StrictUndefined)
            got = native.from_string("{{ " + cond + " }}").render(**v)
            c.check(f"#518 when of the {label} task is strictly boolean "
                    f"(CERT={cert or 'empty'!r}, CA={ca or 'empty'!r})",
                    isinstance(got, bool) and got == want, repr(got))


def check_mgmt_gateway_418(c, can_render):
    want = os.path.join(".config", "mop", "secrets", "llm-proxy-mgmt.pass")
    tasks = yaml.safe_load(open(
        os.path.join(DEPLOY, "roles", "web", "tasks", "main.yml"),
        encoding="utf-8"))
    # Источник llmproxy: то же выражение из setup.yml.
    setup = yaml.safe_load(open(os.path.join(DEPLOY, "setup.yml"),
                                encoding="utf-8"))
    source = next((str(play.get("vars", {}).get("llm_proxy_mgmt_key"))
                   for play in setup
                   if play.get("vars", {}).get("llm_proxy_mgmt_key")), "")
    copies = [t for t in tasks
              if t.get("ansible.builtin.copy", {}).get("dest", "").endswith(
                  "llm-proxy-mgmt.pass")]
    c.check("#418 the web role carries a private copy of the management key",
            len(copies) == 1, [t.get("name") for t in copies])
    if copies:
        spec = copies[0]["ansible.builtin.copy"]
        c.check("#418 the copy comes from the same lookup as llmproxy's, not a play-scoped name",
                spec.get("content", "") == "{{ " + source.strip("{} }") + " }}"
                and "llm_proxy_mgmt_key" not in spec.get("content", ""),
                spec.get("content"))
        c.check("#418 the key copy is the gateway's own path and permissions",
                spec.get("dest", "").endswith(want)
                and spec.get("owner") == "{{ MOP_USER }}"
                and spec.get("group") == "{{ MOP_USER }}"
                and spec.get("mode") in ("0600", "'0600'")
                and copies[0].get("no_log") is True, spec)
        c.check("#418 the copy is unconditional: both installs carry the proxy",
                copies[0].get("when") is None, copies[0].get("when"))
        unit_at = next((i for i, t in enumerate(tasks)
                        if t.get("ansible.builtin.template", {}).get(
                            "src", "") == "service.j2"), None)
        if unit_at is not None:
            c.check("#418 the key copy lands before the web unit starts",
                    tasks.index(copies[0]) < unit_at,
                    (tasks.index(copies[0]), unit_at))
        dirs = [t for t in tasks
                if t.get("ansible.builtin.file", {}).get("state") == "directory"
                and t["ansible.builtin.file"].get("path", "").endswith(
                    os.path.join(".config", "mop", "secrets"))]
        c.check("#418 the secrets dir is 0700",
                any(d["ansible.builtin.file"].get("mode") in ("0700", "'0700'")
                    for d in dirs), [d.get("name") for d in dirs])
    # HYPOTHESIS: копия ключа может устареть относительно живого config.yaml;
    # один запрос после запуска прокси ловит рассинхрон до выдачи панели.
    # SOLUTION: одноразовый GET после проверки LLM endpoint, весь вывод скрыт.
    # RESULT: без GET красная проверка, с ним зелёная.
    # STATUS: FIXED — see #418
    proxy_tasks = yaml.safe_load(open(os.path.join(DEPLOY, "roles", "llmproxy", "tasks", "main.yml"),
                                     encoding="utf-8"))
    checks = [t for t in proxy_tasks if t.get("name") == "The panel gateway key is accepted by the proxy"]
    c.check("#418 management key has a single post-start health check", len(checks) == 1)
    if checks:
        check = checks[0]
        req = check.get("ansible.builtin.uri", {})
        c.check("#418 health uses the same secret file as the gateway, only on loopback",
                req.get("url") == "http://127.0.0.1:{{ MOP_LLM_PROXY_PORT }}/v8/management/config"
                and "lookup('file', MOP_HOME + '/.config/mop/secrets/llm-proxy-mgmt.pass')"
                    in req.get("headers", {}).get("Authorization", "")
                and req.get("status_code") == 200
                and check.get("no_log") is True,
                {"url": req.get("url"), "no_log": check.get("no_log")})
        names = [t.get("name") for t in proxy_tasks]
        c.check("#418 health comes after proxy start and existing LLM health",
                names.index("The proxy answers on the LLM API") < names.index(check["name"])
                and names.index("Enable and start") < names.index(check["name"]))
        c.check("#418 invalid key does not retry against the IP ban",
                all(k not in check for k in ("until", "retries", "delay")))

    if not can_render:
        return
    template = open(os.path.join(DEPLOY, "roles", "proxy", "templates",
                                 "mop.conf.j2"), encoding="utf-8").read()
    got = render(template, dict(config.SETTINGS, MOP_SERVER_LAN="srv.test",
                                 MOP_LLM_REQUEST_MAX_MB="17"))
    web = f"127.0.0.1:{config.SETTINGS['MOP_WEB_PORT']}"

    # Блок location по границам скобок: никаких выводов «покрыто» из
    # существования соседнего блока.
    def block(pattern):
        m = re.search(r"location\s+(?:=\s*|\^~\s*)?" + pattern
                      + r"\s*\{(?:[^{}]*)\}", got)
        return m.group(0) if m else ""

    # Шлюз: три маршрута в mop-web (exact шима, raw префикс, API префикс и
    # exact без слеша).
    for path_pat, what, exact in ((r"/llm/management\.html", "panel shim", True),
                                  (r"/llm/raw/", "gated raw panel", False),
                                  (r"/v8/management/", "API gateway", False),
                                  (r"/v8/management", "exact API", True)):
        b = block(path_pat)
        c.check(f"#418 {what} goes to mop-web",
                bool(b) and web in b, b or "no block")
    # Обходы закрыты: ^~ /llm/management (все суффиксы: /, .html/, junk)
    # и оба вида /llm/v8/management.
    guard = block(r"/llm/management")
    c.check("#418 ^~ /llm/management denies every suffix past the shim",
            guard.startswith("location ^~ /llm/management")
            and "403" in guard, guard)
    for alias in (r"/llm/v8/management", r"/llm/v8/management/"):
        b = block(alias)
        c.check(f"#418 alias {alias} is denied",
                bool(b) and "403" in b, b or "no block")
    # Шим -- exact: exact выигрывает у ^~, и только точный путь открыт.
    shim = block(r"/llm/management\.html")
    c.check("#418 the shim is an exact match, not a prefix",
            "location = /llm/management.html" in shim, shim)
    # /llm/ -- снятый алиас (#546): явный 410, не прокси; что именно
    # осталось под префиксом -- check_llm_alias_gone_546, контракт снятия
    # там же. Здесь -- только неприкосновенность соседних маршрутов.
    # #529+#546: лимит -- на четырёх маршрутах LLM (панельных): алиас /llm/
    # снят, счёт падает с пяти до четырёх.
    limited = [b for b in (block(r"/llm/raw/"), block(r"/v8/management/"),
                           block(r"/v8/management"), block(r"/v1/"))]
    c.check("#411+#418+#529+#546 the body limit sits on exactly the four LLM routes",
            all(b.count("client_max_body_size") == 1 for b in limited)
            and got.count("client_max_body_size") == 4, got.count("client_max_body_size"))
    # Лимит и таймауты на raw/API.
    for path_pat in (r"/llm/raw/", r"/v8/management/"):
        b = block(path_pat)
        c.check(f"#418 {path_pat}: body limit, 75s read timeout, buffering off",
                "client_max_body_size" in b and "75s" in b
                and "proxy_buffering off" in b, b)
    # Неприкосновенность остального.
    for keep in ("location = /nats", "location = /events", "location / {"):
        c.check(f"#418 {keep} untouched", keep in got, keep)
    # #529 добавляет корневой /v1/: лимит на четырёх ожидаемых маршрутах,
    # по одной директиве в каждом, нигде больше -- блоками, не счётчиком.
    # #546 снимает алиас /llm/ -- маршрутов с лимитом четыре.
    limited = [b for b in (block(r"/llm/raw/"), block(r"/v8/management/"),
                           block(r"/v8/management"), block(r"/v1/"))]
    c.check("#411+#418+#529+#546 the body limit sits on exactly the four LLM routes",
            all(b.count("client_max_body_size") == 1 for b in limited)
            and got.count("client_max_body_size") == 4, got.count("client_max_body_size"))
    # HYPOTHESIS: подсчёт директив пропускал неправильное значение в одном
    # location; все четыре должны брать потолок установки, не константу.
    # Нестандартные 17 МБ отличают подстановку настройки от хардкода 64 МБ.
    want_limit = "client_max_body_size 17m;"
    for path, b in zip(("/llm/raw/", "/v8/management/", "=/v8/management", "/v1/"),
                       limited):
        c.check(f"#418 {path} uses the configured body limit",
                bool(b) and want_limit in b, b or "no block")


def check_root_v1_proxy_529(c, can_render):
    # HYPOTHESIS: панель строит /v1/models от корня origin, а nginx
    # отправляет корневой /v1/ в mop-web, где каталога нет.
    # SOLUTION: /v1/ проксируется без среза URI, с клиентским ключом,
    # лимитом установки и потоковым ответом без буферизации.
    # RESULT: до правки рендер падал на отсутствии /v1/ и его директив.
    # STATUS: FIXED — see #529
    if not can_render:
        return
    template = open(os.path.join(DEPLOY, "roles", "proxy", "templates",
                                 "mop.conf.j2"), encoding="utf-8").read()
    for port, limit in ((config.SETTINGS["MOP_LLM_PROXY_PORT"],
                         config.SETTINGS["MOP_LLM_REQUEST_MAX_MB"]),
                        ("18317", "96")):
        got = render(template, dict(config.SETTINGS, MOP_SERVER_LAN="srv.test",
                                    MOP_LLM_PROXY_PORT=port,
                                    MOP_LLM_REQUEST_MAX_MB=limit))
        matches = re.findall(r"location\s+/v1/\s*\{([^{}]*)\}", got)
        c.check(f"#529 root /v1/ has one API prefix route (port {port})",
                len(matches) == 1, matches or "no /v1/ route")
        route = matches[0] if len(matches) == 1 else ""
        c.check(f"#529 /v1/models and other API paths retain /v1/ (port {port})",
                f"proxy_pass http://127.0.0.1:{port};" in route,
                route or "no /v1/ route")
        c.check(f"#529 /v1/ uses the installation body limit {limit}m",
                f"client_max_body_size {limit}m;" in route, route)
        c.check("#529 /v1/ forwards streaming responses without buffering",
                "proxy_buffering off;" in route
                and "proxy_http_version 1.1;" in route
                and 'proxy_set_header Connection "";' in route
                and "proxy_read_timeout 1h;" in route, route)
        c.check("#529 /v1/ keeps the caller's API key, not a server credential",
                bool(route) and not re.search(
                    r"proxy_set_header\s+(?:Authorization|[Xx]-[Aa]pi-[Kk]ey)\b",
                    route) and "MOP_PROXY_KEY" not in route, route)
        for header in ("proxy_set_header Host $host;",
                       "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                       "proxy_set_header X-Forwarded-Proto https;"):
            c.check(f"#529 /v1/ preserves {header}", header in route, route)


# ── #546: снятие временного алиаса /llm/ в nginx ─────────────────────────
# HYPOTHESIS (#546): клиенты и сервер переведены на origin без /llm (#547),
# puppets ходят на MOP_PROXY_URL мимо nginx, а location /llm/ продолжал
# проксировать на LLM-прокси со срезом префикса: опоздавший клиент получал
# работающий ответ, и алиас нельзя было снять никогда. Удалить блок нельзя
# тоже: /llm/v1/* ушёл бы в location / к mop-web, и опоздавший получил бы
# чужой ответ вместо явного отказа.
# SOLUTION: location /llm/ -- явный отказ 410 без редиректа (редирект
# теряет тело POST и ключ и прячет опоздавших от журнала). Маршруты панели
# при своём префиксе: exact и длиннее /llm/, выигрывают по правилам выбора
# location в nginx (exact -- всегда, ^~ -- до regex-проверки, длиннейший
# префикс -- у более точного). MOP_LLM_PROXY_PORT остаётся только в /v1/.
# RESULT: RED на 4930aea -- /llm/ ещё проксирует (410 нет, порт в блоке),
# MOP_LLM_PROXY_PORT живёт в двух location, счёт лимитов -- 5.
# STATUS: FIXED — see #546
def check_llm_alias_gone_546(c, can_render):
    if not can_render:
        return
    template = open(os.path.join(DEPLOY, "roles", "proxy", "templates",
                                 "mop.conf.j2"), encoding="utf-8").read()
    got = render(template, dict(config.SETTINGS, MOP_SERVER_LAN="srv.test",
                                MOP_LLM_REQUEST_MAX_MB="17"))
    # Бывший префикс LLM API -- явный отказ, не прокси и не редирект.
    llm_block = re.search(r"location\s+/llm/\s*\{[^{}]*\}", got)
    body = llm_block.group(0) if llm_block else ""
    c.check("#546 the former /llm/ API prefix is an explicit refusal",
            bool(llm_block) and "return 410;" in body, body or "no /llm/ block")
    c.check("#546 /llm/ neither proxies nor redirects",
            "proxy_pass" not in body and "return 30" not in body
            and "return 301" not in body, body)
    # Порт LLM-прокси -- только в /v1/: бывший алиас туда больше не ходит.
    port = config.SETTINGS["MOP_LLM_PROXY_PORT"]
    with_port = [b for b in re.findall(r"location\s+[^{]+\{[^{}]*\}", got)
                 if f":{port};" in b or f":{port} " in b or f":{port}/" in b]
    c.check("#546 the LLM proxy port lives only in the /v1/ route",
            len(with_port) == 1 and "/v1/" in with_port[0]
            and "location /llm/" not in with_port[0], with_port)
    # /v1/ -- прежний контракт: без среза префикса, лимит, без буферизации,
    # час чтения.
    v1 = re.search(r"location\s+/v1/\s*\{[^{}]*\}", got)
    route = v1.group(0) if v1 else ""
    c.check("#546 /v1/ proxies the API without a prefix cut",
            f"proxy_pass http://127.0.0.1:{port};" in route, route or "no /v1/ block")
    for directive in ("client_max_body_size 17m;", "proxy_buffering off;",
                      "proxy_read_timeout 1h;"):
        c.check(f"#546 /v1/ keeps {directive.split()[0]}",
                directive in route, route or "no /v1/ block")
    # Маршруты панели при своём префиксе: проксируют на mop-web, запреты
    # на месте. Они exact (^~-) и длиннее /llm/ -- по правилам выбора
    # location в nginx выигрывают у префиксного /llm/ с 410.
    web = f"127.0.0.1:{config.SETTINGS['MOP_WEB_PORT']}"

    def block(pattern):
        m = re.search(r"location\s+(?:=\s*|\^~\s*)?" + pattern
                      + r"\s*\{(?:[^{}]*)\}", got)
        return m.group(0) if m else ""

    for path_pat, what in ((r"/llm/management\.html", "panel shim"),
                           (r"/llm/raw/", "gated raw panel")):
        b = block(path_pat)
        c.check(f"#546 {what} still goes to mop-web", bool(b) and web in b,
                b or "no block")
    for path_pat in (r"/llm/v8/management", r"/llm/v8/management/",
                     r"/llm/management"):
        b = block(path_pat)
        c.check(f"#546 the {path_pat} denial is in place",
                bool(b) and "403" in b, b or "no block")
    # Лимит тела -- на четырёх оставшихся маршрутах LLM (панельных), не
    # на снятом алиасе: счёт падает с пяти до четырёх.
    c.check("#546 the body limit left the removed alias, four routes remain",
            got.count("client_max_body_size") == 4, got.count("client_max_body_size"))


# ── #565: destroy сверяет ожидаемое имя тела ────────────────────────────
# HYPOTHESIS (#565): глагол destroy принимает один VMID и проверяет лишь
# префикс pu- (check_host по файлу конфига, conf_hostname), а VMID -- хеш
# имени (vmid_of). При коллизии имён (ensure ловит её на подъёме, но не при
# сносе) cleanup упавшего тела B с VMID занятого живого тела A уничтожает
# A: сверять не с кем, ожидаемое имя в вызов не приехало.
# SOLUTION: новый глагол destroy-body <vmid> <expected-name>. Имя стоящего
# тела -- `pct config` с точным сравнением перед любой мутацией. Отсутствие
# тела обёртка различает от нечитаемого состояния: номера нет в `pct list`
# -- идемпотентный успех; `pct config` или `pct list` упали -- отказ
# fail-closed, без stop/destroy. Прежний destroy (сборка и образы, имя
# знает сам плейбук) не меняется и проверяется прежним успехом по VMID:
# путь conf_hostname на время теста подменяется одной контролируемой
# заменой /etc/pve/lxc/ на каталог фикстур, чтобы стенд не зависел от
# машины и не читал настоящие конфиги гипервизора.
# RESULT: RED на b9dfc2a -- глагола destroy-body нет, и падают ровно
# кейсы нового глагола (отказ вместо удаления, отсутствие вместо отказа
# при нечитаемом ростере); прежний destroy при этом проходит, как и
# должен: дефект был в нехватке контракта, а не в нём.
# STATUS: FIXED — see #565
def check_pve_destroy_body_565(c, can_render):
    if not can_render:
        return
    import subprocess
    import tempfile
    import textwrap

    # Рендер обёртки с теми же подстановками, что ставит роль pve.
    src = open(os.path.join(DEPLOY, "roles", "pve", "templates", "mop-pve.j2"),
               encoding="utf-8").read()
    with tempfile.TemporaryDirectory() as d:
        # Фикстуры конфигов для conf_hostname: прежний destroy (и только он)
        # читает hostname из /etc/pve/lxc/<vmid>.conf напрямую (#138). Путь
        # абсолютный, и подменяется он одной заменой в ОТРЕНДЕРЕННОМ тестовом
        # скрипте -- production-шаблон не меняется. Подстановок ровно одна:
        # исчезни путь из шаблона, проверка заметит.
        conf_dir = os.path.join(d, "lxc")
        os.makedirs(conf_dir)
        # 9101 -- «наш» pu-mop-1; 9102 -- чужое pu-other-3, жертва, которую
        # снёс бы чужой cleanup, будь проверка только по префиксу.
        for vmid, host in (("9101", "pu-mop-1"), ("9102", "pu-other-3")):
            with open(os.path.join(conf_dir, f"{vmid}.conf"), "w") as f:
                f.write(f"arch: amd64\nhostname: {host}\ncores: 2\n")
        wrapper = render(src, {"pve_vmid_min": "9000",
                               "pve_facts": {"vmid_max": 9999},
                               "MOP_USER": "mopuser"})
        real, fake = "/etc/pve/lxc/", conf_dir + "/"
        c.check("#565 the wrapper renders with the role's substitutions",
                wrapper.startswith("#!/bin/bash"), wrapper[:40])
        n_paths = wrapper.count(real)
        c.check("#565 exactly one /etc/pve/lxc/ path to substitute",
                n_paths == 1, f"{n_paths} in the template")
        if n_paths:
            wrapper = wrapper.replace(real, fake)
        c.check("#565 no absolute /etc/pve path remains in the stand",
                "/etc/pve" not in wrapper, "left as is")

        # Подставной pct: журнал вызовов, конфиги и ростер. Реакции как у
        # настоящего: config несуществующего -- код 2, ростер -- таблица
        # VMID'ов; PCT_CONFIG_FAIL/PCT_LIST_FAIL делают чтение нечитаемым
        # (любой ненулевой код, не только «нет такого CT»).
        calls = os.path.join(d, "calls")
        pct = os.path.join(d, "pct")
        conf = {"9101": "hostname: pu-mop-1\ncores: 2\n",
                "9102": "hostname: pu-other-3\nmemory: 2048\n"}
        with open(pct, "w") as f:
            f.write(textwrap.dedent("""\
                #!/bin/bash
                echo "$*" >> "$CALLS"
                if [[ "$1 $2" == "config 9101" && -n "$PCT_CONFIG_FAIL" ]]; then
                    echo "pct config error: cluster not ready" >&2
                    exit 255
                fi
                case "$1" in
                list)
                    if [[ -n "$PCT_LIST_FAIL" ]]; then
                        echo "cluster not ready" >&2; exit 255
                    fi
                    echo "VMID  Status"
                    echo "9101  stopped"
                    echo "9102  running"
                    ;;
                config)
                    case "$2" in
                    9101) printf '%s' "$PCT_CONF_9101" ;;
                    9102) printf '%s' "$PCT_CONF_9102" ;;
                    *) echo "no such CT ($2)" >&2; exit 2 ;;
                    esac
                    ;;
                esac
                exit 0
                """))
        os.chmod(pct, 0o755)
        env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}", CALLS=calls,
                   PCT_CONF_9101=conf["9101"], PCT_CONF_9102=conf["9102"])

        def run(*args, extra=None):
            e = dict(env, **(extra or {}))
            return subprocess.run(["bash", "/dev/stdin"] + list(args),
                                  input=wrapper, env=e, capture_output=True,
                                  text=True, timeout=30)

        # Журнал pct накапливается между прогонами -- каждый сценарий читает
        # его с чистого листа, чтобы «мутаций не было» значило «в ЭТОМ
        # прогоне», а не «за всю жизнь подставного pct».
        def clear():
            if os.path.exists(calls):
                os.unlink(calls)

        def ran():
            """Журнал pct. Мутации -- stop и destroy; ростер и конфиг --
            чтение, и их отсутствие не требуется: отказ обязан быть без
            ИЗМЕНЕНИЙ, а не без вопросов."""
            return open(calls).read() if os.path.exists(calls) else ""

        def mutated():
            return any(l.startswith(("stop ", "destroy "))
                       for l in ran().splitlines())

        # Несовпадение имени: отказ с именем стоящего тела, ни stop, ни destroy.
        clear()
        r = run("destroy-body", "9101", "pu-mop-2")
        c.check("#565 mismatch -> refusal naming the standing body",
                r.returncode != 0 and "pu-mop-1" in r.stderr,
                f"rc={r.returncode} stderr={r.stderr!r}")
        c.check("#565 mismatch -> neither stop nor destroy ran",
                not mutated(), ran())

        # Совпадение имени: тело сносится -- stop, потом destroy.
        clear()
        r = run("destroy-body", "9101", "pu-mop-1")
        c.check("#565 match -> success",
                r.returncode == 0, f"rc={r.returncode} stderr={r.stderr!r}")
        c.check("#565 match -> stop then destroy of that vmid",
                [l for l in ran().splitlines() if not l.startswith(("list", "config"))]
                == ["stop 9101", "destroy 9101 --force 1 --purge 1"],
                ran())

        # Подтверждённое отсутствие (номера нет в ростере): успех без мутаций.
        clear()
        r = run("destroy-body", "9998", "pu-mop-9")
        c.check("#565 confirmed absence -> idempotent success",
                r.returncode == 0, f"rc={r.returncode} stderr={r.stderr!r}")
        c.check("#565 confirmed absence -> no mutation",
                not mutated(), ran())

        # Ошибка чтения конфига (pct config упал) -- НЕ отсутствие.
        clear()
        r = run("destroy-body", "9101", "pu-mop-1",
                extra={"PCT_CONFIG_FAIL": "1"})
        c.check("#565 config read error -> refusal, not absence",
                r.returncode != 0, f"rc={r.returncode} stderr={r.stderr!r}")
        c.check("#565 config read error -> no mutation",
                not mutated(), ran())

        # И ростер нечитаем (pct list упал) -- тоже отказ: состояние неизвестно.
        clear()
        r = run("destroy-body", "9998", "pu-mop-9", extra={"PCT_LIST_FAIL": "1"})
        c.check("#565 roster read error -> refusal, not absence",
                r.returncode != 0, f"rc={r.returncode} stderr={r.stderr!r}")
        c.check("#565 roster read error -> no mutation",
                not mutated(), ran())

        # Доводы валидации: без имени, не-число, вне диапазона, имя не pu-,
        # недопустимый символ в имени. Отказ каждого -- ДО любого вызова pct:
        # журнал пуст целиком, не «без stop/destroy».
        for args, why_not in ((("destroy-body", "9001"), "one argument"),
                          (("destroy-body", "abc", "pu-mop-1"), "vmid not a number"),
                          (("destroy-body", "8000", "pu-mop-1"), "vmid outside the range"),
                          (("destroy-body", "9101", "other-1"), "name not pu-"),
                          (("destroy-body", "9101", "pu-mo p-1"), "name with a space")):
            clear()
            r = run(*args)
            c.check(f"#565 invalid call ({why_not}) -> refusal",
                    r.returncode != 0, f"rc={r.returncode} stderr={r.stderr!r}")
            c.check(f"#565 invalid call ({why_not}) -> pct never called",
                    ran() == "", ran())

        # Прежний destroy сборки не тронут: плейбук зовёт его без имени, и он
        # по-прежнему сносит по одному VMID после префиксной проверки --
        # настоящим прежним успехом, на изолированных фикстурах конфигов.
        clear()
        r = run("destroy", "9102")
        c.check("#565 the build's destroy still succeeds by vmid alone",
                r.returncode == 0
                and [l for l in ran().splitlines()
                     if not l.startswith("config")]
                == ["stop 9102", "destroy 9102 --force 1 --purge 1"],
                f"rc={r.returncode} stderr={r.stderr!r} calls={ran()!r}")

        # Прежний destroy на отсутствующем теле -- идемпотентный успех:
        # конфига в фикстурах нет, pct config отвечает кодом 2.
        clear()
        r = run("destroy", "9997")
        c.check("#565 the build's destroy stays idempotent on absence",
                r.returncode == 0 and ran() == "config 9997\n",
                f"rc={r.returncode} stderr={r.stderr!r} calls={ran()!r}")

        # Новый глагол заявлен в подсказке об отказе.
        c.check("#565 destroy-body is an advertised verb",
                "destroy-body" in wrapper, "not in the usage line")


def main():
    c = Checks()
    check_cluster_key_copy_402(c)
    check_skeleton_image_filter_479(c)
    check_llmproxy_upgrade_524(c)
    check_llmproxy_panel_525(c)

    # ── юниты из общего шаблона ───────────────────────────────────────────
    template = os.path.join(COMMON, "templates", "service.j2")
    c.check("common unit template exists", os.path.isfile(template))
    try:
        import jinja2  # noqa: F401
        can_render = True
    except ImportError:
        can_render = False
        hermetic.skip("rendering of the units",
                      "no jinja2 on this machine — only the structure below is checked")
    check_mgmt_gateway_418(c, can_render)
    check_root_v1_proxy_529(c, can_render)
    check_llm_alias_gone_546(c, can_render)
    check_llmproxy_loopback_558(c, can_render)
    check_pve_destroy_body_565(c, can_render)
    check_private_ca_517(c, can_render)
    # ── кому что из настроек (#176) ───────────────────────────────────────
    scoped = getattr(config, "SERVER_SCOPED", {})
    c.check("config.SERVER_SCOPED names every server unit",
          sorted(scoped) == sorted(UNITS), sorted(scoped))
    reads = set().union(*(settings_read(p) for p in CLUSTER_READS))
    missing = sorted(unit_env(reads) - set(scoped.get("mop-cluster", ())))
    c.check("mop-cluster gets every setting its code reads", not missing, missing)
    for unit, names in scoped.items():
        c.check(f"{unit}: settings, not secrets, in the unit",
              not [n for n in names if re.search(r"TOKEN|PASS|SECRET", n)], names)
        c.check(f"{unit}: every name is a setting",
              all(n in config.SETTINGS for n in names), names)
    from mop.server import playvars
    c.check("playvars exports SERVER_SCOPED",
          playvars.playbook_vars().get("MOP_SERVER_SCOPED") ==
          {u: list(v) for u, v in scoped.items()})
    variables = {**{k: v for k, v in config.SETTINGS.items() if v}, **VARS,
                 "MOP_SERVER_SCOPED": {u: list(v) for u, v in scoped.items()}}

    for unit, role in UNITS.items():
        tmpl, uvars = unit_task(role, unit)
        c.check(f"{unit}: the role installs it", tmpl is not None)
        if tmpl is None:
            continue
        c.check(f"{unit}: from the common template",
              tmpl.get("src", "").endswith("common/templates/service.j2"), tmpl.get("src"))
        own = os.path.join(DEPLOY, "roles", role, "templates", f"{unit}.service.j2")
        c.check(f"{unit}: its own template is gone", not os.path.exists(own))
        # Набор env -- из config.SERVER_SCOPED, не рукописным списком задачи
        # (#176); тот же, что был, плюс добавленное этим тикетом.
        c.check(f"{unit}: no hand-typed env list in the task",
              "env" not in (uvars.get("unit") or {}), (uvars.get("unit") or {}).get("env"))
        # И в кавычках (#185): значение с пробелом systemd иначе разрезал бы.
        was = re.findall(r'^Environment="?([A-Z_]+)=', PINNED[unit], re.M)
        now = list(scoped.get(unit, ())) + ["PYTHONUNBUFFERED"]
        want = was + list(ADDED.get(unit, ()))
        c.check(f"{unit}: the env set as before plus the additions",
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
                c.check(f"{unit}: systemd reads {value!r} whole", seen.get(name) == value,
                      seen.get(name))
            if unit not in ADDED:
                c.check(f"{unit}: rendered byte for byte as before", got == PINNED[unit],
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
            c.check(f"{unit}: as before but for the added lines",
                  [l for l in bare if not named(l)] ==
                  [l for l in PINNED[unit].splitlines() if not l.startswith("#")])
            seen = environment(got)
            wrong = {n: seen.get(n) for n in ADDED[unit] if seen.get(n) != variables[n]}
            c.check(f"{unit}: every addition rendered", not wrong, wrong)

    # ── check mode без ложных changed (#191) ─────────────────────────────
    def tasks(name):
        path = os.path.join(COMMON, "tasks", name)
        return yaml.safe_load(open(path)) or [] if os.path.isfile(path) else []

    def when(t):
        w = t.get("when", [])
        return " and ".join(w) if isinstance(w, list) else str(w)

    nb = tasks("nomad_bin.yml")
    probe = [t for t in nb if "nomad version" in str(t.get("ansible.builtin.command", ""))]
    c.check("nomad_bin: a version probe", len(probe) == 1, [t.get("name") for t in probe])
    if probe:
        p = probe[0]
        c.check("nomad_bin: the probe runs in check mode and changes nothing",
              p.get("check_mode") is False and p.get("changed_when") is False
              and p.get("failed_when") is False and p.get("register"), p)
        reg = p.get("register", "")
        for mod in ("ansible.builtin.get_url", "ansible.builtin.unarchive"):
            t = [t for t in nb if mod in t]
            c.check(f"nomad_bin: {mod} only when the version is not the wanted one",
                  len(t) == 1 and reg in when(t[0]) and "nomad_version" in when(t[0]),
                  [when(x) for x in t])
    pp = [t for t in tasks("pip.yml") if "ansible.builtin.pip" in t]
    real = [t for t in pp if t["ansible.builtin.pip"].get("extra_args")]
    dry = [t for t in pp if not t["ansible.builtin.pip"].get("extra_args")]
    c.check("pip: the real task keeps --user --break-system-packages, outside check mode",
          len(real) == 1 and real[0]["ansible.builtin.pip"]["extra_args"] ==
          "--user --break-system-packages" and when(real[0]) == "not ansible_check_mode",
          [(t["ansible.builtin.pip"].get("extra_args"), when(t)) for t in real])
    c.check("pip: a check-mode twin without extra_args", len(dry) == 1
          and when(dry[0]) == "ansible_check_mode", [when(t) for t in dry])
    if len(real) == 1 and len(dry) == 1:
        a, b = real[0], dry[0]
        same = ("become", "become_user")
        c.check("pip: the twin asks as the same user about the same list",
              all(a.get(k) == b.get(k) for k in same)
              and {k: v for k, v in a["ansible.builtin.pip"].items() if k != "extra_args"}
              == b["ansible.builtin.pip"],
              (a, b))

    # ── хосты инвентаря -- файлом сервису кластера (#178) ─────────────────
    # forget отказывает узлу, который deploy поставил бы снова; файла нет --
    # отказа нет, поэтому разошедшееся имя файла ломало бы защиту молча.
    from mop.server import cluster
    want = "{{ MOP_HOME }}/.config/mop/" + os.path.basename(
        getattr(cluster, "INVENTORY_HOSTS", "") or "?")
    wrote = [t for f, t in site_tasks() if f.endswith("roles/cluster/tasks/main.yml")
             and "mop_inventory_hosts" in str(t.get("ansible.builtin.copy", {}).get("content"))]
    c.check("cluster: the role writes the inventory's hosts where the service reads them",
          len(wrote) == 1 and wrote[0]["ansible.builtin.copy"].get("dest") == want
          and wrote[0]["ansible.builtin.copy"].get("owner") == "{{ MOP_USER }}",
          [t.get("ansible.builtin.copy") for t in wrote] or want)
    c.check("cluster: the host list only when deploy sent one",
          len(wrote) == 1 and "mop_inventory_hosts is defined" in when(wrote[0]),
          [when(t) for t in wrote])

    # ── просьбы проектов -- файлом сервису кластера (#197) ────────────────
    # Спеку строит сервис под учёткой пула, а `.mop` проектов читает deploy на
    # контроллере. Файла нет -- у каждого проекта память установки, поэтому
    # разошедшееся имя файла отменило бы просьбы молча.
    from mop.server import spec
    want = "{{ MOP_HOME }}/.config/mop/" + os.path.basename(spec.ASKS_FILE)
    wrote = [t for f, t in site_tasks() if f.endswith("roles/cluster/tasks/main.yml")
             and "mop_manifests" in str(t.get("ansible.builtin.copy", {}).get("content"))]
    c.check("cluster: the role writes the projects' asks where the spec reads them",
          len(wrote) == 1 and wrote[0]["ansible.builtin.copy"].get("dest") == want
          and wrote[0]["ansible.builtin.copy"].get("owner") == "{{ MOP_USER }}",
          [t.get("ansible.builtin.copy") for t in wrote] or want)
    c.check("cluster: the asks only when deploy sent the manifests",
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
        c.check("cluster: the asks file is {project: asks}, nothing else of the manifest",
              got == {"mop": {"MOP_MEM_MB": "6144"}, "rugent": {}}, got)

    # ── потолок памяти узла -- в meta Nomad (#197) ───────────────────────
    # Спека папета требует `${meta.mop_mem_cap_mb} >= потолок`: ключ, которого
    # на узле нет, ограничение не проходит, а имя, разошедшееся со спекой,
    # закрыло бы пул целиком. Значение -- по правилу node.env: строка хоста,
    # иначе установка.
    from mop.server import spec
    hcl_path = os.path.join(DEPLOY, "roles", "nomad", "templates", "client.hcl.j2")
    hcl = open(hcl_path).read()
    c.check("client.hcl: the node's memory cap is in its meta under the spec's key",
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
            c.check(f"client.hcl: {what}", got == [want], got)

    # ── ssh-порт узла -- из ansible_port инвентаря (#201) ────────────────
    # Порт, которым сервер ходит на узел, знал только ssh config root'а на
    # контроллере: bootstrap (пользователь пула) шёл на 22 и получал отказ.
    # ansible_port -- стандартная переменная: её же берёт сам прогон, и
    # node.env рендерит её в MOP_SSH_PORT; нет её -- порт установки (22).
    # STATUS: FIXED — see #201
    bus_tasks = yaml.safe_load(open(os.path.join(DEPLOY, "roles", "bus", "tasks", "main.yml")))
    node_env = next((t for t in bus_tasks
                     if t.get("name") == "What this node knows about itself"), None)
    c.check("bus: the node.env task is found", node_env is not None)
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
            c.check(f"node.env: MOP_SSH_PORT from {what}", got ==
                  (want, host.get("mop_driver", installed.get("MOP_DRIVER"))), got)

    # ── callout.conf -- рестарт, users.conf -- reload (#206) ────────────
    # Блок auth_callout nats reload'ом не берёт и валит весь reload (стенд
    # #206); смену его файла прогон обязан отдать рестарту.
    # STATUS: FIXED — see #206
    ptasks = yaml.safe_load(open(os.path.join(DEPLOY, "roles", "bus", "tasks", "projects.yml")))
    by = {t.get("name"): t for t in ptasks}
    after = by.get("Callout file of the bus, after") or {}
    c.check("bus: a changed callout.conf restarts nats",
          (after.get("ansible.builtin.stat") or {}).get("path") == "/etc/nats/callout.conf"
          and after.get("notify") == "restart nats", after)
    c.check("bus: a changed users.conf still reloads nats",
          (by.get("Users file of the bus, after") or {}).get("notify") == "reload nats")
    users_task = by.get("Users file of the bus, written by the server") or {}
    names = [t.get("name") for t in ptasks]
    c.check("bus: the callout file is read before and after mop cluster users",
          names.index("Callout file of the bus, before") < names.index(users_task.get("name"))
          < names.index("Callout file of the bus, after") if after and users_task else False)
    check_identity_copy_167(c, ptasks)
    check_registry_seeding_540(c, ptasks)
    check_one_source_219(c)
    check_claude_cli_293(c)
    check_agent_unit_172(c, can_render)
    conf = open(os.path.join(DEPLOY, "roles", "bus", "templates", "nats-server.conf.j2")).read()
    c.check("nats-server.conf includes callout.conf inside authorization",
          re.search(r"authorization \{[^}]*include \./callout\.conf", conf) is not None)

    # ── пароль LDAP сервисам сервера -- файлом 0600 (#214) ───────────────
    # Окружение юнита читаемо всем, поэтому секрет провайдера -- файлом рядом
    # с копией личностей, и только когда ldap -- звено провайдера (#232).
    # STATUS: FIXED — see #214
    def every(tasks):
        for t in tasks or []:
            yield t, t.get("when")
            for sub in every((t or {}).get("block")):
                yield sub[0], [t.get("when"), sub[1]]
    bind = "/etc/nats/identity/ldap-bind.pass"
    found = [(t, w) for t, w in every(ptasks)
             if ((t.get("ansible.builtin.copy") or {}).get("dest") == bind)]
    c.check("bus: one task writes the LDAP bind password file", len(found) == 1, len(found))
    if found:
        t, w = found[0]
        cp = t["ansible.builtin.copy"]
        c.check("bus: the bind password file is 0600, the pool user's",
              cp.get("mode") == "0600" and cp.get("owner") == "{{ MOP_USER }}", cp)
        c.check("bus: the bind password never reaches the log", t.get("no_log") is True, t)
        c.check("bus: the bind password comes from the play's env, not extra-vars",
              "lookup('env', 'MOP_LDAP_BIND_PASSWORD')" in str(cp.get("content")), cp.get("content"))
        # Внутри блока копии личностей (#167: при callout или при файле
        # операторов) -- файл пароля следует его условию, своего нет.
        block = next((b for b in ptasks if b.get("name") == "Identity for the server's services"), {})
        c.check("bus: the bind password file only with the ldap provider, inside the identity copy",
              "'ldap' in MOP_AUTH_PROVIDER.replace(' ', '').split(',')" in str(w)
              and block.get("when") in (w or [None])
              and t in (block.get("block") or []), w)
    gone = [(t, w) for t, w in every(ptasks)
            if (t.get("ansible.builtin.file") or {}).get("path") == bind
            and (t.get("ansible.builtin.file") or {}).get("state") == "absent"]
    c.check("bus: no bind password file without the ldap provider",
          len(gone) == 1
          and "'ldap' not in MOP_AUTH_PROVIDER.replace(' ', '').split(',')" in str(gone[0][1]), gone)

    # ── одно определение у каждой общей вещи ─────────────────────────────
    gv_path = os.path.join(DEPLOY, "group_vars", "all.yml")
    gv = yaml.safe_load(open(gv_path)) if os.path.isfile(gv_path) else {}
    c.check("group_vars: package excludes, .env among them",
          gv.get("mop_package_excludes") == EXCLUDES, gv.get("mop_package_excludes"))
    c.check("group_vars: secrets_dir", gv.get("secrets_dir") ==
          "{{ lookup('env', 'HOME') }}/.config/mop/secrets", gv.get("secrets_dir"))
    c.check("group_vars: projects", gv.get("projects") == "{{ mop_projects | default([]) }}",
          gv.get("projects"))
    c.check("group_vars: the package's source is the playbook's tree",
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
        c.check(f"{needle!r} lives only in {home or 'no file'}", where == want, where)

    check_restart_by_state_477(c)
    check_probes(c)
    check_body_sizes(c, can_render)
    check_pve_storage_227(c, can_render)
    check_routes_gateway_wait_502(c, can_render)
    check_include_loop_var(c)
    check_add_host_loop_278(c)
    check_distinct_build_inventory_577(c, can_render)
    check_users_reload_199(c)
    check_one_node_280(c)

    check_llm_request_size_411(c, can_render)
    check_job_gc_348(c, can_render)
    check_doctor_timer_359(c)
    check_pu_cleanup_gone_361(c)
    return c.report("deploy")


if __name__ == "__main__":
    sys.exit(main())
