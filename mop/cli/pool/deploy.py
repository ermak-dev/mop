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
import os
import shutil
import sys

from mop.cli import lib
from mop import bus, config, creds, manifest, puppets, projects

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
                  projects.git_hosts(origins, config.get("MOP_GIT_HOST")), check=dry)
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
