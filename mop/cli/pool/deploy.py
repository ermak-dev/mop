"""deploy: mop deploy [--check]

The whole installation in one run: one playbook, site.yml, so there is one
ansible process and one PLAY RECAP — a line per machine over every layer.
No separate targets: fixing what fell over is cheaper by rerunning than by
remembering which target owned it.

Machines, not projects. Which projects the pool serves is the registry
(~/.config/mop/projects) and `mop project add|delete`; deploy reads it and
takes no arguments (#79). While it accepted an origin, registering a project
was a side effect of a full run, and there was no way to take one off at all.

--check runs the same playbook in ansible's check mode with --diff: nothing
on the machines changes, and the output is the difference a real run would
make. Nothing after the playbook runs: collecting the server credentials
writes files, and the roster check is not the question a dry run answers.
"""
import json
import os
import shutil
import subprocess
import sys

from mop.cli import lib
from mop import bus, config, creds, driver, manifest, puppets, projects

# Это единственная дорога на узел мимо шины. Дороги через неё (alloc exec)
# больше нет, поэтому упавшего агента и битые креды чинят только отсюда — и
# по этой же причине deploy не имеет права ничего занимать у самого mop:
# библиотечные вызовы здесь ходят в Nomad и по файлам, но не на шину.

SITE = "site.yml"
# Прежние цели запуска. Отвергаем, а не молча трактуем как имя проекта: старая
# привычка `mop deploy pool` завела бы на шине пользователя master-pool, и
# разбирались бы с этим уже по симптомам.
RUN_TARGETS = ("nomad", "pool", "homelab", "claude", "nats", "all")


def refused_target(argv):
    """Старая цель запуска в аргументах либо None."""
    return argv[0] if argv and argv[0] in RUN_TARGETS else None


def inventory_hosts(listing):
    """Все хосты `ansible-inventory --list`: у хоста без своих переменных
    нет строки в _meta.hostvars, он есть только в списке группы."""
    hosts = set((listing.get("_meta") or {}).get("hostvars") or {})
    for name, group in listing.items():
        if name != "_meta" and isinstance(group, dict):
            hosts.update(group.get("hosts") or [])
    return sorted(hosts)


def uniform_refusals(listing, installed, default):
    """Отказы по настройкам, одинаковым для всего пула (#190). -> [строка на
    хост и настройку]. installed -- {настройка: значение установки}, default
    -- MOP_DRIVER для хостов без своего mop_driver (сегодня не нужен: все
    такие настройки общие для любого драйвера).

    Перекрытие в инвентаре -- имя настройки строчными (так его находит
    шаблон node.env). Число из YAML и та же строка -- одно значение."""
    hostvars = (listing.get("_meta") or {}).get("hostvars") or {}
    out = []
    for host in inventory_hosts(listing):
        mine = hostvars.get(host) or {}
        for name in sorted(installed):
            key = name.lower()
            if key in mine and str(mine[key]) != str(installed[name]):
                out.append(f"{host}: {key}={mine[key]} differs from the installation's "
                           f"{name}={installed[name]}: the server builds every puppet's "
                           f"spec with its own value")
    return out


def memory_refusals(listing, cap):
    """Отказы по памяти в инвентаре (#197). -> [строка]. cap -- потолок
    установки, MOP_BODY_MEM_CAP_MB.

    Строка mop_mem_mb -- отказ на любом узле и в любой группе: память --
    свойство папета, и строка, которая раньше не действовала нигде, молча не
    действовала бы и дальше. Группы смотрим отдельно: `ansible-inventory
    --list` сводит их vars в hostvars, `--export` -- нет.

    Потолок узла едет в meta Nomad, где `>=` сравнивает численно только два
    целых (scheduler/feasible.go, checkOrder), а иначе -- лексически: "32G"
    или " 32768" открыли бы узел любому потолку молча. Поэтому -- одно целое
    число, и у установки тоже, если хоть одному хосту он достаётся."""
    key, cap_key = config.NOT_NODE[0].lower(), "mop_body_mem_cap_mb"
    hostvars = (listing.get("_meta") or {}).get("hostvars") or {}
    where = [(h, hostvars.get(h) or {}) for h in inventory_hosts(listing)]
    where += [(name, group.get("vars") or {}) for name, group in sorted(listing.items())
              if name != "_meta" and isinstance(group, dict)]
    out = []
    for name, mine in where:
        for k in (n.lower() for n in config.NOT_NODE):
            if k in mine:
                out.append(f"{name}: {k}={mine[k]} -- {k} is not a node setting any more: "
                           f"the puppet's memory comes from .env ({k.upper()}) and the "
                           f"project's .mop -- remove the line")
    inherit = False
    for host, mine in where[:len(inventory_hosts(listing))]:
        if cap_key not in mine:
            inherit = True
        elif not whole_mb(mine[cap_key]):
            out.append(f"{host}: {cap_key}={mine[cap_key]!r} is not a whole number of "
                       f"megabytes: Nomad would compare it as text")
    if inherit and not whole_mb(cap):
        out.append(f"MOP_BODY_MEM_CAP_MB={cap!r} in .env is not a whole number of "
                   f"megabytes: Nomad would compare it as text")
    return out


def whole_mb(value):
    """Целое число мегабайт одной строкой, как его прочтёт Nomad."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0 \
        or isinstance(value, str) and value.isdigit() and int(value) > 0


def driver_refusals(listing, default):
    """Отказы по драйверам хостов инвентаря (#186). -> [строка на хост].

    listing -- `ansible-inventory --list`: ansible сам сводит переменные групп
    и хоста, и драйвер здесь тот же, что получит плейбук. Хост без своей
    переменной есть только в списке группы, и драйвер у него default
    (MOP_DRIVER) -- ровно `mop_driver | default(MOP_DRIVER)` шаблонов.
    Правило имени одно -- driver.of_node (#175): опечатка, дошедшая до meta
    Nomad, ловилась бы только читателями, по узлу за раз."""
    hostvars = (listing.get("_meta") or {}).get("hostvars") or {}
    out = []
    for host in inventory_hosts(listing):
        value = (hostvars.get(host) or {}).get("mop_driver", default)
        try:
            driver.of_node({"mop_driver": value}, node=host)
        except RuntimeError as e:
            out.append(str(e))
    return out


def inventory_listing(inventory):
    """Инвентарь глазами ansible. -> (listing | None, отказ | None)."""
    r = subprocess.run(["ansible-inventory", "-i", inventory, "--list"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode:
        return None, f"inventory {inventory} does not parse: {(r.stderr or r.stdout).strip()}"
    return json.loads(r.stdout), None


def missing_extras(setting, root):
    """Файлы MOP_BODY_EXTRA, которых нет в установке. Проверяем до прогона:
    у ansible пропавший include_tasks валит прогон в середине, когда полпула
    уже перенастроено, и читается это как поломка плейбука, а не «файла нет»."""
    return [p.strip() for p in setting.split(",")
            if p.strip() and not os.path.isfile(os.path.join(root, p.strip()))]


def link(name, target):
    """Симлинк ~/etc/<name> -> каталог плейбука. Плейбуки находятся по одному
    лишь соглашению о пути (`net setup <имя>` -> ~/etc/<имя>/setup.yml),
    таблицы имён нигде нет: симлинки и есть эта таблица, поэтому deploy
    заводит их сам, а не требует от оператора."""
    want = os.path.join(lib.PROJECT, target)
    path = os.path.expanduser(f"~/etc/{name}")
    if os.path.realpath(path) == os.path.realpath(want):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.lexists(path):
        os.remove(path)
    os.symlink(want, path)
    lib.ok(f"  symlink ~/etc/{name} -> {want}")


def manifests(origins):
    """Манифесты проектов (#25, #61): режутся из ORIGIN библиотечным разбором
    здесь, на управляющей машине. Плейбук получает готовые пути и не
    заводит второго, снисходительного парсера. Недоступный origin валит
    прогон громко: названный проект обязан существовать."""
    out = {}
    for origin in sorted(origins):
        got = manifest.fetch(origin)
        entry = {"asks": got["asks"]}
        for side in ("sandbox_vars", "sandbox_tasks", "bootstrap_vars", "bootstrap_tasks"):
            if got[side]:
                entry[side] = got[side]
        out[got["project"]] = entry
        for k in got["alien"]:
            print(f"  {got['project']}/.mop: {k} is not a project's to set — ignored", flush=True)
        for old in got["legacy"]:
            print(f"  {got['project']}/{old}: read as .mop/sandbox.yaml for the transition "
                  f"(#61) — rename it, the old name will stop being read", flush=True)
    return out


def check():
    """Прогон без неё не отличает «плейбук зелёный» от «пул отвечает»: агент,
    упавший в бесконечный реконнект, systemd вполне устраивает. Своя сводка,
    а не `mop list`: командлеты друг друга не зовут."""
    items = puppets.roster()
    silent = [i["job"]["ID"] for i in items if i["kind"] == "silent"]
    print(f"  puppets: {len(items)}"
          + (f", agent silent for: {', '.join(silent)}" if silent else ", every node answers"))
    print("\n".join(lib.pool_lines()))


def main(argv):
    # До первого сетевого вызова: зависимости контроллера ставит mop setup,
    # и отказ его называет.
    if not shutil.which("ansible-playbook"):
        lib.fail("no ansible-playbook on this machine -- run mop setup")
        return 1
    # Полный REQUIRED спрашивает только deploy: остальным хватает адреса
    # сервера, а MOP_GIT_HOST читают одни плейбуки.
    config.require()
    dry = argv == ["--check"]
    if dry:
        argv = []
    # Git identity папетов (#167) -- обе или ни одной: половина доехала бы до
    # спеки молча пропущенной, и коммит в клоне падал бы, как без неё.
    git = {k: config.get(k) for k in ("MOP_GIT_NAME", "MOP_GIT_EMAIL")}
    if any(git.values()) and not all(git.values()):
        lib.fail(f"{' and '.join(k for k, v in git.items() if not v)} is empty in .env: "
                 f"the puppets' git identity takes both MOP_GIT_NAME and MOP_GIT_EMAIL, or neither")
        return 1
    if refused_target(argv):
        lib.fail("run targets are gone: mop deploy takes no arguments")
        return 1
    if argv:
        # Origin в аргументах заводил проект побочным эффектом прогона (#79).
        lib.fail(f"mop deploy takes no arguments; {argv[0]} looks like a project.\n"
                 f"Register it: mop project add {argv[0]}")
        return 1
    inventory = os.environ["INVENTORY"]
    if not os.path.isfile(inventory):
        lib.fail(f"no inventory {inventory} -- create it from the example: "
                 f"cp inventory.yaml.example inventory.yaml")
        return 1
    # Драйвер каждого хоста -- до плейбука (#186): опечатка иначе доезжала
    # до meta Nomad и всплывала у читателей по узлу за раз.
    listing, why = inventory_listing(inventory)
    if why:
        lib.fail(why)
        return 1
    refusals = (driver_refusals(listing, config.get("MOP_DRIVER"))
                + uniform_refusals(listing, {n: config.get(n) for n in config.POOL_UNIFORM},
                                   config.get("MOP_DRIVER"))
                + memory_refusals(listing, config.get("MOP_BODY_MEM_CAP_MB")))
    for why in refusals:
        lib.fail(why)
    if refusals:
        return 1
    for extra in missing_extras(config.get("MOP_BODY_EXTRA"), lib.PROJECT):
        lib.fail(f"no body environment file {extra} -- create it from the example: "
                 f"cp sandbox.yaml.example sandbox.yaml (or empty MOP_BODY_EXTRA in .env)")
        return 1
    # Ключ пула к git: роль узла копирует его с контроллера, и без него прогон
    # падал бы на середине. Дефолт считает config (первый из стандартных имён).
    key = config.get("MOP_GIT_KEY")
    if not key or not os.path.isfile(key) or not os.path.isfile(key + ".pub"):
        lib.fail(f"no git key for the pool on this machine (MOP_GIT_KEY={key or 'unset'}) "
                 f"-- make one: ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519, "
                 f"register its .pub on MOP_GIT_HOST, or name yours in .env")
        return 1

    # Цель симлинка может меняться, имя — нет: ~/etc/site.yml импортирует
    # ~/etc/nomad/setup.yml по этому пути и о переезде каталога не знает.
    link("nomad", "deploy/nomad")
    link("nats", "deploy")

    # Проекты: реестр сервера, и только он (#117). Пользователей NATS по нему
    # заводит сам сервер; прогону он нужен ради манифестов и хостов форжей.
    # Пустой список законен: пустой пул, мастеров ещё нет.
    try:
        answer = bus.ask_cluster("projects", project=bus.ADMIN, timeout=10)
    except Exception as e:
        answer = {"error": str(e)}
    lines, note = projects.for_deploy(answer, projects.read())
    if note:
        print(f"  {note}", file=sys.stderr, flush=True)
    origins, legacy = puppets.project_ids(lines)

    lib.section("ansible: site.yml")
    rc = lib.play(SITE, projects.names(origins, legacy), manifests(origins),
                  projects.git_hosts(origins, config.get("MOP_GIT_HOST")), check=dry,
                  inventory_hosts=inventory_hosts(listing))
    if dry:
        # Всё ниже пишет (creds.collect) или отвечает на другой вопрос
        # (ростер): прогон без изменений кончается на плейбуке (#177).
        return rc
    if rc and rc != lib.UNREACHABLE:
        # Сборка кредов и проверка ростера не идут после красного прогона, и
        # это сказано, а не проглочено.
        lib.fail(f"ansible exited {rc}; server credentials and the roster check skipped")
        return rc
    if rc == lib.UNREACHABLE:
        # Выключенная машина — не красный прогон: на всех, кто ответил, слои
        # разложены. Остановиться здесь значило бы не собрать креды и не
        # показать ростер до тех пор, пока узел не вернут, — а именно тогда
        # они и нужны. Машине по возвращении нужен свой прогон, и это
        # сказано.
        lib.fail("some machines did not answer; everything that did is "
                 "configured. Run mop deploy again when they are back")

    # Контроллер — тоже машина оператора: его каталог сервера собирается здесь
    # из secrets/ и bootstrap.json. Самоподписанный сертификат закрепляется,
    # настоящий -- нет (#97); ролевые пароли до #106 снимаются.
    dest = creds.server_dir()
    got = creds.collect(os.path.expanduser("~/.config/mop/secrets"), dest,
                        pin=not config.get("MOP_TLS_CERT"))
    print(f"  server credentials: {', '.join(got) or 'nothing to pin'} in {dest}")
    if creds.operator(dest) is None:
        # Проверка ниже ходит на шину, а войти этой машине пока нечем:
        # человек входит своим именем, и выбрать его за оператора deploy не
        # может. Громко, а не красной проверкой с непонятной причиной.
        lib.fail(f"installed, but this machine is nobody on the bus yet: "
                 f"log in with mop join --user <name> (a name from "
                 f"MOP_OPERATORS), then mop list")
        return 1

    lib.section("check")
    check()
    return 0
