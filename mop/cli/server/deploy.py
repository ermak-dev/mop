"""deploy: mop server deploy [--check] [--skip-pipeline] [--from-ci]

The whole installation in one run: one playbook, site.yml, so there is one
ansible process and one PLAY RECAP — a line per machine over every layer.
No separate targets: fixing what fell over is cheaper by rerunning than by
remembering which target owned it.

Machines, not projects. Which projects the pool serves is the registry
(~/.config/mop/projects) and `mop project add|delete`; deploy reads it and
takes no arguments. While it accepted an origin, registering a project
was a side effect of a full run, and there was no way to take one off at all.

--check runs the same playbook in ansible's check mode with --diff: nothing
on the machines changes, and the output is the difference a real run would
make. Nothing after the playbook runs: collecting the server credentials
writes files, and the roster check is not the question a dry run answers.

With MOP_DEPLOY_NEEDS_GREEN=1 in .env, deploy refuses unless the GitLab
pipeline of the working copy's HEAD is success, or still running with every
job outside stage deploy green (CI rolling itself out). --skip-pipeline is
the emergency way past it, and says so. --check changes nothing and is not
gated.

--from-ci is what CI's deploy job runs on the server: the working copy must
be on origin's default branch with no tracked file modified; it fetches,
fast-forwards to origin and rolls that out. The pipeline gate is mandatory
there: it refuses without MOP_DEPLOY_NEEDS_GREEN=1 and with --skip-pipeline.
When the only answer of the gate is "wait", HEAD has moved past the commit
that started the job, and that newer commit's own deploy job rolls it out:
--from-ci then says so and exits 0. Every other refusal stays a refusal.
"""
import json
import os
import shutil
import subprocess
import sys

from mop.cli import lib
from mop.cli.server import _play
from mop.common import bus, config, creds, gitlab, manifest, paths, puppets, projects
from mop import driver
from mop.server import identity

# Это единственная дорога на узел мимо шины. Дороги через неё (alloc exec)
# больше нет, поэтому упавшего агента и битые креды чинят только отсюда — и
# по этой же причине deploy не имеет права ничего занимать у самого mop:
# библиотечные вызовы здесь ходят в Nomad и по файлам, но не на шину.

SITE = "site.yml"
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


# Отказ, пока в .env лежит прежний второй источник людей (#219).
OPERATORS_GONE = ("MOP_OPERATORS is gone: move people with `mop server user import` on the server, "
                  "then remove the line")


def operator_refusals(settings, secrets_dir=identity.SECRETS, leftover=""):
    """Отказы по людям (#205, #219). -> [строка].

    leftover -- значение MOP_OPERATORS из .env: его больше не читает никто,
    и молча лежащий он значил бы людей, которых шина не пустит. Провайдер
    отказывает логину, определённому дважды, по одному; здесь -- громкий
    отказ всего, до плейбука: неизвестный провайдер, битый файл операторов,
    каждый дубль, и файл без единого человека -- на шину не вошёл бы никто.

    Цепочка (#232) -- отказы по звеньям: битый файл, дубль и неполные
    настройки ldap -- отказ, где бы звено ни стояло. Пустой файл -- отказ,
    только когда он -- вся цепочка: при file,ldap людей впускает каталог."""
    if (leftover or "").strip():
        return [OPERATORS_GONE]
    try:
        source = identity.provider(settings, secrets_dir)
        out = source.conflicts()
    except ValueError as e:
        return [str(e)]
    if isinstance(source, identity.PlainFileProvider) and not out and not source.identities():
        out.append(f"no people in {source.path}: nobody could log in to the bus -- "
                   f"add someone with mop server user add <login> on the server")
    return out


def pipeline_refusals(setting, have_creds, sha, fetch, fetch_jobs=None):
    """Отказ по пайплайну катимого коммита (#231). -> [строка].

    setting -- MOP_DEPLOY_NEEDS_GREEN; have_creds -- есть ли чем спросить
    GitLab; sha -- HEAD рабочей копии, пусто -- не назван; fetch(sha) ->
    пайплайн либо None; fetch_jobs(id) -> его джобы, спрашиваются только у
    идущего (#239). Включённая проверка ни при чём не пропускается молча:
    нет кредов, коммита или ответа GitLab -- отказ с причиной."""
    if setting in ("", "0"):
        return []
    if setting != "1":
        return [f"MOP_DEPLOY_NEEDS_GREEN={setting!r}: want 1 (check) or empty (no check)"]
    if not have_creds:
        return ["MOP_DEPLOY_NEEDS_GREEN=1 but no GitLab credentials in .env (GITLAB_TOKEN, "
                "or GITLAB_USER and GITLAB_PASSWORD): cannot check the pipeline of the "
                "commit being rolled out"]
    if not sha:
        return ["cannot name the commit being rolled out: git rev-parse HEAD failed"]
    try:
        got = fetch(sha)
    except RuntimeError as e:
        return [f"cannot read the pipeline for {sha}: {e}"]
    jobs = None
    if got and fetch_jobs and got.get("status") in gitlab.PIPELINE_WAIT:
        try:
            jobs = fetch_jobs(got["id"])
        except RuntimeError as e:
            return [f"cannot read the jobs of pipeline {got['id']}: {e}"]
    why = gitlab.pipeline_verdict(got, sha, jobs)
    return [why] if why else []


# ─── --from-ci (#239) ────────────────────────────────────────────────────
# CI катит установку сам: ключ джобы на сервере с forced command, и что бы
# клиент ни прислал, сервер выполнит `mop server deploy --from-ci`. Украденный ключ
# поэтому умеет одно -- катить зелёный master, и держат это отказы ниже.
def from_ci_refusals(setting, skip_pipeline, dry, branch, default, dirty):
    """Можно ли катить из CI. -> [строка].

    Гейт обязателен: без него --from-ci катил бы что угодно, что лежит в
    master. Ветка -- по умолчанию у origin; отслеживаемое не тронуто
    (dirty -- строки `git status --porcelain` без неотслеживаемых: .env,
    inventory.yaml и бэкапы помехой не считаются)."""
    out = []
    if setting != "1":
        out.append("--from-ci needs MOP_DEPLOY_NEEDS_GREEN=1 in .env: "
                   "a CI rollout must pass the pipeline gate")
    if skip_pipeline:
        out.append("--from-ci and --skip-pipeline together: "
                   "a CI rollout never skips the pipeline gate")
    if dry:
        out.append("--from-ci and --check together: --from-ci rolls out, "
                   "--check is a dry run by hand")
    if not default:
        out.append("--from-ci: cannot name origin's default branch -- "
                   "run git remote set-head origin --auto")
    elif branch != default:
        out.append(f"--from-ci: the working copy is on {branch}, not {default}")
    if dirty:
        out.append("--from-ci: tracked files are modified: "
                   + ", ".join(line[3:] for line in dirty))
    return out


def from_ci_deferred(refusals, sha):
    """Отказы гейта под --from-ci -> строка хода вместо отказа, либо None.

    Единственный отказ «жди» под --from-ci значит одно: HEAD ушёл дальше
    коммита, запустившего джобу (её needs: гарантирует, что её тесты уже
    кончились), а у нового коммита своя джоба deploy стоит за нашей в
    resource_group. Отказ красил бы старый пайплайн красным при здоровом
    master; катить новый коммит -- работа его собственной джобы. Любой другой
    отказ остаётся отказом, упавший тест нового HEAD тоже: master сломан, и
    красный здесь правда."""
    if len(refusals) == 1 and refusals[0].startswith(gitlab.WAIT):
        return (f"  from-ci: {sha[:12]} is still being tested; its own pipeline's deploy "
                f"job rolls it out — nothing to do now")
    return None


def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


def ci_state(root):
    """(ветка, ветка origin по умолчанию, [изменённое отслеживаемое]).
    Ветка по умолчанию -- origin/HEAD, а не литерал: её знает git."""
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    head = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    default = head.stdout.strip().removeprefix("origin/") if head.returncode == 0 else ""
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no").stdout
    return branch, default, [l for l in dirty.splitlines() if l.strip()]


def ci_sync(root):
    """git fetch и --ff-only к origin/<ветка>. -> (было, стало, отказ | None).
    Только вперёд: разошедшийся с origin сервер -- не то, что CI вправе
    перезаписать."""
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    old = _git(root, "rev-parse", "HEAD").stdout.strip()
    r = _git(root, "fetch", "--quiet", "origin")
    if r.returncode:
        return old, old, f"--from-ci: git fetch failed: {(r.stderr or r.stdout).strip()}"
    r = _git(root, "merge", "--ff-only", "--quiet", f"origin/{branch}")
    if r.returncode:
        return old, old, (f"--from-ci: {branch} does not fast-forward to origin/{branch}: "
                          f"{(r.stderr or r.stdout).strip()}")
    return old, _git(root, "rev-parse", "HEAD").stdout.strip(), None


def head_sha(root):
    """HEAD рабочей копии, которую катит deploy; пусто -- не рабочая копия."""
    r = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return r.stdout.strip() if r.returncode == 0 else ""


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
            print(f"  {got['project']}/{old}: read as .mop/sandbox.yaml for the transition — "
                  f"rename it, the old name will stop being read", flush=True)
    return out


def ci_pull(skip_pipeline, dry):
    """--from-ci до прогона (#239): отказы, fetch, --ff-only. -> можно ли
    катить дальше. Строки хода печатаются: они и есть лог джобы CI.

    Сдвинувшийся HEAD -- перезапуск `mop server deploy --from-ci` уже на новом коде:
    этот процесс держит в памяти старый пакет, а катить обязан тот, что
    приехал. Второй проход fetch не сдвигает и идёт дальше сам."""
    branch, default, dirty = ci_state(lib.PROJECT)
    refusals = from_ci_refusals(config.get("MOP_DEPLOY_NEEDS_GREEN"), skip_pipeline, dry,
                                branch, default, dirty)
    for why in refusals:
        lib.fail(why)
    if refusals:
        return False
    print(f"  from-ci: {branch}, tracked files clean; fetching origin", flush=True)
    old, new, why = ci_sync(lib.PROJECT)
    if why:
        lib.fail(why)
        return False
    if old == new:
        print(f"  from-ci: {branch} at {new[:12]}", flush=True)
        return True
    print(f"  from-ci: {branch} {old[:12]} -> {new[:12]}; restarting on the new code",
          flush=True)
    launcher = os.path.join(lib.BIN, "mop")
    os.execv(launcher, [launcher, "deploy", "--from-ci"])


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
    # До первого сетевого вызова: зависимости контроллера ставит mop server setup,
    # и отказ его называет.
    if not shutil.which("ansible-playbook"):
        lib.fail("no ansible-playbook on this machine -- run mop server setup")
        return 1
    # Полный REQUIRED спрашивает только deploy: остальным хватает адреса
    # сервера, а MOP_GIT_HOST читают одни плейбуки.
    config.require()
    skip_pipeline = "--skip-pipeline" in argv
    from_ci = "--from-ci" in argv
    argv = [a for a in argv if a not in ("--skip-pipeline", "--from-ci")]
    dry = argv == ["--check"]
    if dry:
        argv = []
    if argv:
        # Origin в аргументах заводил проект побочным эффектом прогона (#79).
        lib.fail(f"mop server deploy takes no arguments; {argv[0]} looks like a project.\n"
                 f"Register it: mop project add {argv[0]}")
        return 1
    if from_ci and not ci_pull(skip_pipeline, dry):
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
                + memory_refusals(listing, config.get("MOP_BODY_MEM_CAP_MB"))
                + operator_refusals({n: config.get(n) for n in identity.SETTINGS},
                                    leftover=config.get("MOP_OPERATORS")))
    # Пайплайн катимого коммита (#231) -- до плейбука, как и прочие отказы.
    # Сухой прогон ничего не катит и не спрашивает.
    if skip_pipeline:
        print("  pipeline check skipped (--skip-pipeline): rolling out "
              f"{head_sha(lib.PROJECT) or 'HEAD'} without asking GitLab", flush=True)
    elif not dry:
        sha = head_sha(lib.PROJECT)
        gate = pipeline_refusals(config.get("MOP_DEPLOY_NEEDS_GREEN"),
                                 gitlab.has_credentials(), sha, gitlab.pipeline, gitlab.jobs)
        deferred = from_ci and not refusals and from_ci_deferred(gate, sha)
        if deferred:
            print(deferred, flush=True)
            return 0
        refusals += gate
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
    rc = _play.play(SITE, projects.names(origins, legacy), manifests(origins),
                  projects.git_hosts(origins, config.get("MOP_GIT_HOST")), check=dry,
                  inventory_hosts=inventory_hosts(listing))
    if dry:
        # Всё ниже пишет (creds.collect) или отвечает на другой вопрос
        # (ростер): прогон без изменений кончается на плейбуке (#177).
        return rc
    if rc and rc != _play.UNREACHABLE:
        # Сборка кредов и проверка ростера не идут после красного прогона, и
        # это сказано, а не проглочено.
        lib.fail(f"ansible exited {rc}; server credentials and the roster check skipped")
        return rc
    if rc == _play.UNREACHABLE:
        # Выключенная машина — не красный прогон: на всех, кто ответил, слои
        # разложены. Остановиться здесь значило бы не собрать креды и не
        # показать ростер до тех пор, пока узел не вернут, — а именно тогда
        # они и нужны. Машине по возвращении нужен свой прогон, и это
        # сказано.
        lib.fail("some machines did not answer; everything that did is "
                 "configured. Run mop server deploy again when they are back")

    # Контроллер — тоже машина оператора: его каталог сервера собирается здесь
    # из secrets/ и bootstrap.json. Самоподписанный сертификат закрепляется,
    # настоящий -- нет (#97); ролевые пароли до #106 снимаются.
    dest = creds.server_dir()
    got = creds.collect(paths.local(paths.SECRETS), dest,
                        pin=not config.get("MOP_TLS_CERT"))
    print(f"  server credentials: {', '.join(got) or 'nothing to pin'} in {dest}")
    if creds.operator(dest) is None:
        # Проверка ниже ходит на шину, а войти этой машине пока нечем:
        # человек входит своим именем, и выбрать его за оператора deploy не
        # может. Громко, а не красной проверкой с непонятной причиной.
        lib.fail(f"installed, but this machine is nobody on the bus yet: "
                 f"log in with mop join <login> (a person from mop server user, or "
                 f"from the directory), then mop list")
        return 1

    lib.section("check")
    check()
    return 0
