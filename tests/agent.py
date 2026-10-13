#!/usr/bin/env python3
"""Агент узла без пула: python3 tests/agent.py

Права глаголов агента жили в четырёх параллельных структурах (VERBS,
PUBLIC_VERBS, ADMIN_VERBS, NAMED_VERBS): новый глагол правился в четырёх
местах, и забытое место молча давало или отнимало право. Адресация tmux
`tmux -L <имя> ... -t <имя>` была набрана девять раз (#150).

HYPOTHESIS: права -- четыре структуры, которые обязаны сходиться, и сходятся
только внимательностью.
SOLUTION: одна таблица {глагол: Verb(fn, scope, named)}, прежние наборы
выводятся из неё; решение о праве -- чистая функция. Характеризация: наборы
и решение по каждому глаголу, субъекту и проекту те же, что до правки --
образцы ниже переписаны из агента до #150. Строки tmux -- те же символ в
символ.
STATUS: FIXED — see #150
"""
import dataclasses
import json
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, Msg, bash, canned, patched, patched_env, restored, GATE_NOW, gate_table_267  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.node import agent  # noqa: E402
from mop.common import busnames  # noqa: E402

# ─── до #150: наборы и логика handle, переписанные дословно ──────────────
OLD_VERBS = ("ping", "local", "state", "states", "sizes", "send", "tail",
             "type", "write", "disk", "wipe", "usage", "junk")
OLD_PUBLIC = ("ping", "local", "state", "states", "send", "tail")
OLD_ADMIN = ("disk", "junk")
OLD_NAMED = ("state", "send", "tail", "type", "wipe")


def old_decision(verb, public, project, name, mine):
    """Отказ, как его давал handle до #150, либо None -- глагол исполняется.
    Правила прежние; таблица -- с добавленными после (ADDED, ниже): они
    мастерские и именующие."""
    verbs, named = OLD_VERBS + ADDED + ADDED_NODE, OLD_NAMED + ADDED
    if verb not in verbs:
        return f"no such verb {verb}; available: {', '.join(sorted(verbs))}"
    if public and verb not in OLD_PUBLIC:
        return f"verb {verb} is available to the master only"
    if verb in OLD_ADMIN + ADDED_NODE and project != busnames.ADMIN:
        return f"verb {verb} is node-level, not given to project {project}"
    if verb in named and not mine:
        return f"puppet {name} is not in project {project}"
    return None


def new_decision(verb, public, project, name, mine):
    """То же через таблицу -- в том порядке, в каком это делает handle."""
    why = agent.refusal(verb, public, project)
    if why is None and agent.VERBS[verb].named and not mine:
        why = agent.foreign(name, project)
    return why


# После #150 -- глаголы, добавленные с тех пор, в конце таблицы: clone --
# факты клона для ворот сервиса кластера (#40), мастерский и именующий.
ADDED = ("clone",)
# Узловые глаголы, добавленные после (#358): sweep -- подметание диска узла по
# просьбе doctor. Место на диске -- факт про хост со всеми его жильцами, и
# глагол, как disk, только оператору.
ADDED_NODE = ("sweep",)


def check_sets(c):
    """Прежние четыре набора выводятся из таблицы -- те же и в том же порядке;
    добавленные после (ADDED) -- в конце."""
    for got, want, what in ((tuple(agent.VERBS), OLD_VERBS + ADDED + ADDED_NODE, "VERBS"),
                            (agent.PUBLIC_VERBS, OLD_PUBLIC, "PUBLIC_VERBS"),
                            (agent.ADMIN_VERBS, OLD_ADMIN + ADDED_NODE, "ADMIN_VERBS"),
                            (agent.NAMED_VERBS, OLD_NAMED + ADDED, "NAMED_VERBS")):
        c.expect(what, tuple(got), want)


def check_decisions(c):
    """Каждый глагол x субъект (публичный .msg / мастерский .rpc) x проект
    (admin / проект) x папет свой или чужой: тот же ответ, что до #150.
    Отказ, ставший допуском, -- смена прав, а не рефакторинг."""
    for verb in OLD_VERBS + ADDED + ADDED_NODE + ("nosuch", None, ""):
        for public in (True, False):
            for project in (busnames.ADMIN, "mop"):
                for mine in (True, False):
                    want = old_decision(verb, public, project, "pu-mop-1", mine)
                    got = new_decision(verb, public, project, "pu-mop-1", mine)
                    c.expect(f"{verb} public={public} project={project} mine={mine}", got, want)


def check_tmux(c):
    """Места адресации tmux -- те же строки, что до #150. Снимок экрана для
    фактов ушёл вместе с ключом screen (#236)."""
    n = "pu-mop-1"
    t = agent.Tmux(n)
    for got, want in (
            # tmux_alive
            (t.alive(), f"tmux -L {n} has-session -t {n} 2>/dev/null"),
            # pane_lines
            (t.buffer(), f"tmux -L {n} capture-pane -p -t {n} -S -"),
            # v_type: голая клавиша
            (t.press("Escape"), f"tmux -L {n} send-keys -t {n} Escape; "
                                f"sleep 1; tmux -L {n} capture-pane -p -t {n}"),
            # v_type: слэш-команда -- очистка строки, команда, Enter, экран
            (t.type("/model opus"),
             f"tmux -L {n} send-keys -t {n} C-u; sleep 0.3; "
             f"tmux -L {n} send-keys -t {n} '/model opus'; sleep 0.3; "
             f"tmux -L {n} send-keys -t {n} Enter; "
             f"sleep 2; tmux -L {n} capture-pane -p -t {n}"),
            # v_type: пустая команда -- только Enter и экран
            (t.type(""), f"tmux -L {n} send-keys -t {n} Enter; "
                         f"sleep 2; tmux -L {n} capture-pane -p -t {n}")):
        c.expect("tmux", got, want)


def check_quiet(c):
    """Библиотека не печатает и не выходит: программа -- командлет
    `mop agent` (mop/cli/service/agent.py)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                        "mop", "node", "agent.py")
    in_main = False
    with open(path) as f:
        for n, line in enumerate(f, 1):
            # Переход (#150): блок __main__ -- вход `python3 -m mop.agent` из
            # юнитов узлов; его строки -- программа, а не библиотека.
            if line.startswith('if __name__ == "__main__":'):
                in_main = True
                continue
            if in_main and (line.startswith((" ", "\t")) or not line.strip()):
                continue
            in_main = False
            code = line.split("#")[0]
            if "print(" in code or "sys.exit(" in code:
                c.fail(f"mop/node/agent.py:{n} prints or exits", line.strip())
    c.check("mop/node/agent.py keeps no global connection: pass it explicitly",
            not (hasattr(agent, "_conn")))



# ── #347: проба клона -- одна на агента и сторожа host-узла ──────────────
# Характеристика: clone_facts(name) шлёт в тело ровно этот скрипт и читает
# его вывод в те же факты. Зелёная до разделения пробы на скрипт и разбор
# и после; с #564 скрипт несёт `|| exit 1` на обязательных командах
# (fail-closed), и характеристику переписали под новый безопасный контракт:
# пустой ahead -- неполные данные, а не ноль.
CLONE_PROBE_347 = (
    'cd <HOME>/puppets/pu-mop-3 2>/dev/null || exit 0; '
    'echo "cur=$(git branch --show-current 2>/dev/null)"; '
    'echo "def=$(git rev-parse --abbrev-ref origin/HEAD 2>/dev/null)"; '
    'echo "home=$(git config mop.home 2>/dev/null)"; '
    'echo "origin=$(git remote get-url origin 2>/dev/null)"; '
    'st=$(git status --porcelain 2>/dev/null) || exit 1; '
    'dirty=0; [ -n "$st" ] && dirty=$(printf \'%s\\n\' "$st" | wc -l); '
    'git rev-parse --verify -q --end-of-options HEAD >/dev/null 2>&1; '
    'case $? in '
    '0) ahead=$(git rev-list --count HEAD --not --remotes 2>/dev/null) || exit 1;; '
    '1) ahead=$(git rev-list --count --all 2>/dev/null) || exit 1;; '
    '*) exit 1;; esac; '
    '[ -n "$ahead" ] || exit 1; '
    'echo "dirty=$dirty"; echo "ahead=$ahead"; '
    'echo "owner=$(head -1 .git/mop-owner 2>/dev/null)"')
CLONE_OUT_347 = [
    ("a clone", "cur=fix/7-x\ndef=origin/master\nhome=master\norigin=git@h:g/mop.git\n"
                "dirty=2\nahead=1\nowner=anton\t5\n",
     {"cur": "fix/7-x", "def": "master", "home": "master", "origin": "git@h:g/mop.git",
      "dirty": 2, "ahead": 1, "owner": {"user": "anton", "at": 5}}),
    # #564: прежний образец «detached, ahead пуст -- ноль» закреплял дефект
    # (пустой ahead -- непрочитанная обязательная команда, не «отправлено
    # всё»); здоровый detached-клон обязан принести обе цифры. Нули без
    # ahead -- unknown.
    ("detached, counts came", "cur=\ndef=\nhome=\norigin=\ndirty=0\nahead=0\nowner=\n",
     {"cur": "(detached)", "def": None, "origin": None, "dirty": 0, "ahead": 0, "owner": None}),
    ("no clone dir", "", None),
    ("counts unreadable", "cur=master\ndirty=lots\nahead=0\n", None),
    ("#564 no ahead is incomplete data, not zero", "cur=\ndef=\nhome=\norigin=\n"
     "dirty=0\nahead=\nowner=\n", None),
]


def check_clone_probe_347(c):
    import asyncio
    for what, out, want in CLONE_OUT_347:
        sent = []

        async def bsh(name, script, timeout=20, out=out):
            sent.append(script)
            return out, 0
        with restored(agent, "bsh"):
            agent.bsh = bsh
            got = asyncio.run(agent.clone_facts("pu-mop-3"))
        c.expect(f"#347 clone_facts sends the same probe ({what})",
                 [s.replace(agent.HOME, "<HOME>") for s in sent], [CLONE_PROBE_347])
        c.expect(f"#347 clone_facts reads the same facts ({what})", got, want)

# ── #564: ошибки обязательных команд -- «не знаю», а не нули ──────────────
# HYPOTHESIS: проба глотала отказы обязательных git-команд: пустой вывод
# `git status` считался нулём (`| wc -l` маскирует отказ: код конвейера --
# код wc, а не git-а), пустой `rev-list` -- нулём (`kv.get("ahead") or 0`),
# и побитый клон (мусорный .git/HEAD, битый index) читался «clean» --
# разрешение на уборку клона с работой.
# SOLUTION: каждая обязательная команда роняет пробу; парсер обязан видеть
# непустые dirty и ahead. Unborn -- единственный случай, когда rev-list по
# HEAD отказывает законно, и он доказывается своей проверкой состояния
# (rev-parse --verify), а не лечится запасным запросом при любой ошибке:
# born-клон с упавшим rev-list обязан читать unknown, а не «--all как
# будто ahead». Отказ обязательной команды отличают по коду возврата, не по
# пустому stdout: git может напечатать число и отказаться (обёртка ниже
# печатает 0 и выходит 128), и проверка состояния различает законное
# отсутствие HEAD (rc 1) от ошибки чтения репозитория (rc 128): `|| echo y`
# превращал любую ошибку rev-parse в «unborn». Грань названа: клон с
# удалённым ref ветки git сам читает как unborn («No commits yet»),
# висячий коммит виден только fsck -- проба это не различает и не должна:
# несохранённое она видит, а тяжёлый fsck в пробе ростера не место.
# STATUS: FIXED — see #564
def check_clone_probe_fail_closed_564(c):
    """Шелл и git настоящие: проба гоняет настоящим шеллом над временным
    клоном (driver.sh, как на узле), командлет -- тоже настоящий; пул не
    нужен, всё read-only."""
    import asyncio
    import subprocess
    import tempfile
    from mop.common.domain import CloneFacts
    root = tempfile.mkdtemp(prefix="mop-test-564-")

    def repo(broken=None):
        """Клон с одним коммитом и untracked work.txt; broken портит .git."""
        d = tempfile.mkdtemp(dir=root)
        subprocess.run(["git", "init", "-q", "-b", "master", d],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "init"],
                       check=True, capture_output=True)
        with open(os.path.join(d, "work.txt"), "w") as f:
            f.write("uncommitted ticket work\n")
        if broken == "head":
            # Мусор вместо HEAD: не ref: и не sha -- git отказывается
            # читать репозиторий целиком (проверено на git 2.39: status
            # «fatal: not a git repository»).
            with open(os.path.join(d, ".git", "HEAD"), "w") as f:
                f.write("garbage, not a ref and not a sha\n")
        elif broken == "index":
            # Битый index: мусор вместо дампа статуса; status отказывает,
            # и именно здесь прятался дефект -- код конвейера `status |
            # wc -l` был кодом wc, а не git-а, и пустота читалась нулём.
            os.remove(os.path.join(d, ".git", "index"))
            with open(os.path.join(d, ".git", "index"), "w") as f:
                f.write("not an index\n")
        return d

    async def facts(d, env=None):
        """Факты клона настоящей пробой: как их читает агент на узле."""
        from mop import driver
        out, _ = await driver.sh(agent.clone_probe(d), env=env)
        return agent.clone_facts_of(out)

    healthy = asyncio.run(facts(repo()))
    c.check("#564 a healthy clone with work reads dirty and ahead",
            not (not healthy or healthy.get("dirty") != 1 or healthy.get("ahead") != 1),
            repr(healthy))
    head = asyncio.run(facts(repo("head")))
    c.check("#564 garbage .git/HEAD: the clone reads unknown, not clean",
            head is None, repr(head))
    index = asyncio.run(facts(repo("index")))
    c.check("#564 a broken index: unknown, not clean",
            index is None, repr(index))

    # Отказ обязательной команды на born-клоне -- unknown, а не число от
    # запасного запроса: обёртка над git валит ровно `rev-list ... HEAD`,
    # `--all` бы прошёл -- и его ответ не должен читаться как ahead.
    # Воспроизводит дыру первого варианта фикса: `|| rev-list --all` подставлял
    # «все коммиты» при любой ошибке обязательного запроса.
    wrap = os.path.join(root, "bin")
    os.makedirs(wrap, exist_ok=True)
    real = shutil_which("git")
    if real:
        with open(os.path.join(wrap, "git"), "w") as f:
            f.write('#!/bin/sh\nif [ "$1" = rev-list ] && [ "$3" = HEAD ]; then\n'
                    '  echo "forced rev-list failure" >&2; exit 128\nfi\n'
                    f'exec {real} "$@"\n')
        os.chmod(os.path.join(wrap, "git"), 0o755)
        born = repo()
        env = dict(os.environ, PATH=wrap + os.pathsep + os.environ["PATH"])
        got = asyncio.run(facts(born, env=env))
        c.check("#564 a born clone with a failed rev-list reads unknown, "
                "not --all as ahead", got is None, repr(got))

    # Unborn-репозиторий -- здоровый: один untracked, ветка без коммитов,
    # считать нечего, но и ошибки нет. Факты собираются, dirty = 1.
    unborn = tempfile.mkdtemp(dir=root)
    subprocess.run(["git", "init", "-q", "-b", "master", unborn],
                   check=True, capture_output=True)
    with open(os.path.join(unborn, "work.txt"), "w") as f:
        f.write("first scratch\n")
    got = asyncio.run(facts(unborn))
    c.check("#564 an unborn repo is healthy: facts, not unknown",
            not (not got or got.get("dirty") != 1 or got.get("ahead") != 0
                 or got.get("cur") != "master"),
            repr(got))
    # Характеристика #347 прежняя: старый вывод без ошибок читается теми
    # же фактами (проба изменилась только на отказах обязательных команд).
    for what, out, want in CLONE_OUT_347:
        c.expect(f"#564 the #347 outputs still parse the same ({what})",
                 agent.clone_facts_of(out), want)
    # Неполные данные не собираются в факты: пустой ahead или мусорный
    # dirty -- это неизвестный клон, а не нули. Парсер обязан отказаться.
    c.check("#564 no-ahead output must not read as facts",
            agent.clone_facts_of("cur=master\ndirty=0\nahead=\nowner=\n") is None)
    c.check("#564 junk dirty must not read as facts",
            agent.clone_facts_of("cur=master\ndirty=x\nahead=0\n") is None)
    # Проба обязана ронять шелл на отказе обязательных команд -- иначе
    # потребитель (clone-work) читает exit-0 и верит нулям.
    probe = agent.clone_probe("/x")
    c.check("#564 the probe must fail on a mandatory command's failure",
            "|| exit 1" in probe and "|| exit 1;" in probe, probe)
    # Запасной счёт -- только доказанному unborn, не любой ошибке.
    c.check("#564 the fallback count must be gated on the unborn check",
            "rev-parse --verify" in probe, probe)

    # Отказ rev-list обязан ронять пробу по коду возврата, а не по пустому
    # stdout: git может напечатать число и отказаться (fault injection:
    # обёртка печатает 0 и выходит 128). Текущая проба доверилась цифре.
    born2 = repo()
    with open(os.path.join(wrap, "git"), "w") as f:
        f.write('#!/bin/sh\nif [ "$1" = rev-list ] && [ "$3" = HEAD ]; then\n'
                '  echo 0; exit 128\nfi\n'
                f'exec {real} "$@"\n')
    env2 = dict(os.environ, PATH=wrap + os.pathsep + os.environ["PATH"])
    got = asyncio.run(facts(born2, env=env2))
    c.check("#564 a numeric stdout with rev-list's rc 128 reads unknown",
            got is None, repr(got))

    # Ошибка проверки состояния (rev-parse) -- тоже unknown: `|| echo y`
    # читал любой её отказ как «unborn» и звал запасной запрос. Git-контракт:
    # 0 -- HEAD доказан (born), 1 -- HEAD законно нет (unborn), прочее --
    # репозиторий не читается. Обёртка валит ровно rev-parse (rc 128), все
    # счётные команды прошли бы -- «unborn» было бы ложным.
    with open(os.path.join(wrap, "git"), "w") as f:
        f.write('#!/bin/sh\nif [ "$1" = rev-parse ] && [ "$2" = --verify ]; then\n'
                '  exit 128\nfi\n'
                f'exec {real} "$@"\n')
    got = asyncio.run(facts(born2, env=env2))
    c.check("#564 a rev-parse operational failure reads unknown, not unborn",
            got is None, repr(got))

    # Командлет, как его зовёт сторож: повреждённый HEAD с несохранённым
    # файлом обязан получить отказ уборки (exit 3), а не «clean». Тут
    # настоящий clone_work.main -- тот самый код, что решает судьбу клона.
    import importlib
    from _lib import run_command
    clone_work = importlib.import_module("mop.cli.driver.clone-work")
    home = tempfile.mkdtemp(dir=root)
    os.makedirs(os.path.join(home, "puppets"))
    broken_dir = os.path.join(home, "puppets", "pu-review-1")
    os.rename(repo("head"), broken_dir)
    with patched_env(HOME=home):
        out, err, rc = run_command(clone_work.main, [broken_dir])
    c.expect("#564 clone-work on a broken clone with uncommitted work: keep",
             (rc, out.strip()), (3, "no session, clone unreadable"))
    # Здоровый клон с несохранённым -- тоже keep, но по-другому: работа
    # названа, различие читается в причине.
    work_dir = os.path.join(home, "puppets", "pu-review-2")
    os.rename(repo(), work_dir)
    with patched_env(HOME=home):
        out, err, rc = run_command(clone_work.main, [work_dir])
    c.check("#564 clone-work on a healthy clone with work: keep, work named",
            rc == 3 and "clone holds work" in out, (rc, out))


def shutil_which(name):
    import shutil
    return shutil.which(name)


# ── write: дом подставляет узел (#279) ───────────────────────────────────
# HYPOTHESIS: v_write сверял присланный абсолютный путь с белым списком под
# своим домом; клиент с чужой установки слал путь со своим MOP_HOME и
# получал отказ. SOLUTION: paths.writable(HOME, path): относительное имя
# из белого списка ложится под дом агента; чужой дом -- отказ как прежде.
# STATUS: FIXED — see #279
def check_write_home_279(c):
    import asyncio
    import base64
    import types
    written = []

    # Узел без отдельных тел и без живых папетов: v_write пишет одну копию
    # узла. Подменён ровно тот драйвер, который зовёт v_write (#342).
    async def no_bodies():
        return []
    node = types.SimpleNamespace(IS_CONTAINER=False, bodies=no_bodies)
    from mop import driver
    with restored(driver, "write_private"), patched(agent, DRIVER=node):
        driver.write_private = lambda path, data: written.append((path, data))
        b64 = base64.b64encode(b"{}").decode()
        got = asyncio.run(agent.v_write(None, {"_project": "admin",
                                               "files": [[".config/mop/secrets.env", b64]]}))
        c.expect("write: a relative name lands under the agent's home",
                 written, [(os.path.join(agent.HOME, ".config/mop/secrets.env"), b"{}")])
        c.expect("write: the answer names the node's file",
                 got.get("written"), [os.path.join(agent.HOME, ".config/mop/secrets.env")])
        written.clear()
        got = asyncio.run(agent.v_write(None, {"_project": "admin",
                                               "files": [["/home/nobody/.claude/.credentials.json", b64]]}))
        c.check("write: another home is refused", "not allowed to write" in (got.get("error") or ""), got)
        c.check("write: nothing written on refusal", written == [])


def check_addressed_write_312(c):
    """HYPOTHESIS (#312): `write` кладёт файлы в копию узла и во ВСЕ живые
    тела узла, кто бы ни просил: чужой проект получал чужие ключи. С #387
    меток нет и раздаёт один сервер, но выборка целей остаётся одной
    чистой функцией. STATUS: FIXED — see #312, #387"""
    from mop import driver
    wt = getattr(agent, "write_targets", None)
    if wt is None:
        c.fail("#312 no agent.write_targets: every write goes to every body")
        return
    own = {"pu-mop-1": "mop", "pu-mop-2": "mop", "pu-rug-1": "rug"}
    live = ["pu-mop-1", "pu-mop-2", "pu-rug-1"]

    def pick(project, requested=None, container=True):
        return wt(project, requested, live, own, container)
    # (тела, копия узла, отказ, не живые)
    for what, got, want in (
        ("addressed, admin: only the named bodies, no node copy",
         pick("admin", ["pu-mop-2", "pu-rug-1"]), (["pu-mop-2", "pu-rug-1"], False, None, [])),
        ("addressed, a body not live: reported, the rest written",
         pick("admin", ["pu-mop-2", "pu-x-9"]), (["pu-mop-2"], False, None, ["pu-x-9"])),
        ("addressed, a foreign body of a project: refused",
         pick("mop", ["pu-rug-1"]), ([], False, "refused", [])),
        ("addressed, host: the node copy is the body",
         pick("mop", ["pu-mop-1"], container=False), ([], True, None, [])),
        ("unaddressed, admin, containers: node copy and all live bodies",
         pick("admin"), (live, True, None, [])),
        ("unaddressed, admin, host: the node copy only",
         pick("admin", container=False), ([], True, None, [])),
        ("unaddressed, a project: only its own live bodies",
         pick("mop"), (["pu-mop-1", "pu-mop-2"], False, None, [])),
        ("unaddressed, a project on a host node with strangers: no node copy",
         pick("mop", container=False), ([], False, None, [])),
    ):
        got = list(got[:2]) + [("refused" if isinstance(got[2], str) else got[2])] + [got[3]] \
            if isinstance(got, tuple) else got
        want = list(want)
        c.expect(f"#312 {what}", (got[0], got[1], got[3]), (want[0], want[1], want[3]))


def check_timeouts_171(c):
    import asyncio

    async def facts(name):
        return {"owner": None, "dirty": 0, "ahead": 0}
    with restored(agent, "bsh", "clone_facts"):
        agent.clone_facts = facts
        for code in (None, 0):
            agent.bsh = canned("", code)
            for cmd in ("/status", "Escape"):
                got = asyncio.run(agent.v_type(None, {"name": "pu-mop-1", "command": cmd}))
                if code is None:
                    c.check(f"type {cmd}, timeout", not ("timed out" not in (got.get("error") or "")),
                            repr(got))
                if code == 0:
                    c.check(f"type {cmd}, success", not (got != {"screen": ""}), repr(got))
            refused, undo, _ = asyncio.run(agent._claim("pu-mop-1", {"owner": "m-1"}))
            if code is None:
                c.check("owner write, timeout", not ("timed out" not in (refused or "")),
                        repr(refused))
            if code == 0:
                c.check("owner write, success", not (refused or not undo),
                        f"{refused!r} {undo!r}")
        # Кривое имя до шелла не доходит: bsh отдал бы None, и это прочиталось
        # бы как таймаут, а то и как успех.
        got = asyncio.run(agent.v_type(None, {"name": "pu-x; rm", "command": "/status"}))
        c.check("type with a bad name", not ("doesn't look like" not in (got.get("error") or "")),
                repr(got))
        # Пробы только для чтения: ответ прежний.
        agent.bsh = canned("", None)
        c.check("pane_lines must tolerate a timeout",
                not (asyncio.run(agent.pane_lines("pu-mop-1")) != []))
        c.check("session_probe must read a timeout as none",
                not (asyncio.run(agent.session_probe("pu-mop-1")) != "none"))


# ── откат владельца -- с результатом (#181) ──────────────────────────────
# HYPOTHESIS: _unclaim пишет прежнего владельца назад (или rm -f) и код
# шелла не смотрит: на таймауте (#171) или ошибке аренда остаётся за
# мастером, чей send не доехал, и следующему мастеру send отказывает,
# называя не того владельца -- а отказ первого об этом молчит.
# SOLUTION: _unclaim отдаёт причину неудачи (driver.why, как в #171), и
# отказ v_send дописывает «owner not restored: <причина>»; удачный откат
# текст не меняет.
# STATUS: FIXED — see #181
def check_unclaim_181(c):
    import asyncio
    delivery = {"error": "pu-mop-1: no live session"}

    def fake(rollback):
        calls = []

        async def bsh(name, script, timeout=20):
            calls.append(script)
            # Первый шелл -- запись аренды, второй -- откат: прежнюю запись
            # откат берёт из реестра захватов (#573), отдельного чтения нет.
            return ("", 0) if len(calls) <= 1 else rollback
        return bsh, calls

    async def facts(name):
        return {"owner": None, "dirty": 0, "ahead": 0}

    async def session_json(name, cmd, timeout=20):
        return dict(delivery)
    with restored(agent, "bsh", "clone_facts", "session_json"):
        agent.clone_facts, agent.session_json = facts, session_json
        for rollback, want in ((("", None), delivery["error"] + "; owner not restored: timed out"),
                               (("rm: Permission denied", 1),
                                delivery["error"] + "; owner not restored: rm: Permission denied"),
                               (("", 0), delivery["error"])):
            agent.bsh, calls = fake(rollback)
            got = asyncio.run(agent.v_send(None, {"name": "pu-mop-1", "owner": "m-1",
                                                  "message": "hi"}))
            c.check(f"send refused, rollback {rollback}: a rollback shell",
                    not (len(calls) != 2 or "rm -f" not in calls[-1]), calls)
            c.expect(f"send refused, rollback {rollback}: error", got.get("error"), want)


def check_intake(c):
    """HYPOTHESIS (#168): тело-JSON не объект (`[1]`, `"x"`) роняет задачу
    handle на req["_project"], а нехэшируемый глагол (`{"verb": [1]}`) -- на
    поиске в таблице внутри refusal(): ответа нет, проситель ждёт таймаут.
    SOLUTION: handle отказывает телу-не-объекту до таблицы; refusal читает
    нестроковый глагол как неизвестный -- тот же отказ, что у неизвестной
    строки.
    STATUS: FIXED — see #168"""
    import asyncio
    import json
    called = []

    def spy(verb):
        async def fn(conn, req):
            called.append(verb)
            return {"verb": verb}
        return fn

    keep = dict(agent.VERBS)
    try:
        for v, d in keep.items():
            agent.VERBS[v] = dataclasses.replace(d, fn=spy(v))

        def ask(body, subject="mop.admin.node.hyper.rpc"):
            called.clear()
            msg = Msg(subject, body)
            asyncio.run(agent.handle(None, msg, public=subject.endswith(".msg")))
            return msg.replies

        for body in (b"[1]", b'"x"', b"7", b"null"):
            got = ask(body)
            c.check(f"body {body!r}: refused before any verb",
                    not (got != [{"error": "request is not a JSON object"}] or called),
                    f"replies {got!r}, verbs called {called}")

        def unknown(v):
            return f"no such verb {v}; available: {', '.join(sorted(keep))}"
        for verb in ([1], {}, 7, True):
            got = ask(json.dumps({"verb": verb}).encode())
            c.check(f"verb {verb!r}: the unknown-verb refusal",
                    not (got != [{"error": unknown(verb)}] or called),
                    f"replies {got!r}, verbs called {called}")
        # Прежнее не меняется: не-JSON, неизвестная строка, обычный запрос.
        c.expect("a non-JSON body must keep its old refusal",
                 ask(b"junk"), [{"error": "request is not JSON"}])
        c.expect("an unknown string verb must keep its refusal",
                 ask(b'{"verb": "nosuch"}'), [{"error": unknown("nosuch")}])
        got = ask(b'{"verb": "ping"}', "mop.mop.node.hyper.msg")
        c.check("a well-formed ping must reach its verb",
                not (got != [{"verb": "ping"}] or called != ["ping"]), f"{got!r}, {called}")
    finally:
        agent.VERBS.clear()
        agent.VERBS.update(keep)


def check_main_169(c):
    """HYPOTHESIS (#169): как только bus при импорте без nats-py бросает, а не
    выходит, переходный вход юнитов `python3 -m mop.agent` печатал бы трассу:
    импорты модуля идут раньше его блока __main__.
    SOLUTION: блок __main__ -- до импортов пакета; командлет проверяет nats
    первым и отказывает одной строкой.
    STATUS: FIXED — see #169"""
    import subprocess
    import tempfile
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    shadow = tempfile.mkdtemp(prefix="mop-test-nonats-")
    os.makedirs(os.path.join(shadow, "nats"))
    with open(os.path.join(shadow, "nats", "__init__.py"), "w") as f:
        f.write("raise ImportError('no nats')\n")
    r = subprocess.run([sys.executable, "-m", "mop.node.agent", "--check"], cwd=root,
                       env=dict(os.environ, PYTHONPATH=shadow),
                       capture_output=True, text=True)
    want = "bus library needed: pip install --user --break-system-packages nats-py\n"
    c.check("python3 -m mop.node.agent without nats refuses in one line",
            not (r.returncode != 1 or r.stdout or r.stderr != want),
            f"code {r.returncode}, stdout {r.stdout!r}, stderr {r.stderr[-300:]!r}")


def check_subject_173(c):
    """HYPOTHESIS (#173): агент брал проект из субъекта своим правилом
    (`parts[1] if len(parts) > 1`), а не service.project_from_subject (#149):
    на его субъектах они сходятся, но это второе определение.
    SOLUTION: агент зовёт общую функцию; на его субъектах ответ тот же.
    STATUS: FIXED — see #173"""
    from mop.common import service

    def old(subject):
        parts = subject.split(".")
        return parts[1] if len(parts) > 1 else ""
    for p in ("mop", busnames.ADMIN, "a-b_c"):
        subs = [busnames.node(p, "hyper", "rpc"), busnames.node(p, "hyper", "msg"),
                busnames.broadcast(p)]
        for subj in subs:
            c.check(f"{subj}: the shared project rule",
                    not (service.project_from_subject(subj) != old(subj) or old(subj) != p),
                    f"shared {service.project_from_subject(subj)!r}, "
                    f"old {old(subj)!r}, wanted {p!r}")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                        "mop", "node", "agent.py")
    text = open(path).read()
    c.check("mop/node/agent.py must take the project via service.project_from_subject",
            not ("parts[1]" in text or "service.project_from_subject(" not in text))


# ── одинаковый владелец в одну секунду -- разные захваты (#573) ──────────
# HYPOTHESIS: _claim рендерит Owner(me, int(time.time())) и _unclaim
# сравнивает всю строку. Две отправки одного логина в одну секунду пишут
# байт в байт одно значение: A записал и ждёт доставки, B с force записал
# поверх и доставил, доставка A падает -- и откат A («в файле всё ещё наша
# запись» -- она и есть наша, строка та же) сносит аренду успешного B:
# возвращает прежнего владельца или стирает файл, чей держатель только что
# получил работу. Замок (#189) защищает сравнение/запись от вклинивания,
# но значения не уникальны: равенство строк -- не равенство захвата.
# Clock заморожен, Events рулят порядком -- уникальность обязана родиться
# не из времени.
# SOLUTION: захват нумеруется: монотонный счётчик на путь аренды в
# процессе агента, у каждого _claim своё поколение, и захват живёт в
# реестре (поколение -> исход live/settled/failed, «прежде»-байты, ссылка
# на предшественника). Откат условен по поколению: провал отмечается в
# реестре ВСЕГДА, даже когда чужой захват поверх делает запись отката
# невозможной, -- иначе поздний откат не узнает, что запись провалившегося
# старшего восстановлению не подлежит. Запись отката исполняется, только
# когда живых или завершённых захватов поверх нет; успешный преемник не
# трогается вовсе. Что восстанавливать -- решает причинная цепочка
# предшественников, а не соседство номеров и не совпадение байт: у каждого
# захвата предшественник -- поколение, чья запись физически лежала в
# файле на момент захвата (откат B исполнен -- держателем снова стал A,
# и захват C поверх его записи ссылается на A, а не на соседний номер B).
# Откат спускается по ссылкам: проваленное звено перепрыгивает, на живом
# или внешних байтах останавливается и возвращает их. Так и двойной
# провал одного логина в обоих порядках, и тройной провал с уже откаты
# стоят в файле -- все оставляют файл прежним/пустым, а счётчик остаётся
# монотонным: вернуть активного предшественника -- не то же самое, что
# уменьшить аллокатор, и откат его не уменьшает. Успешная доставка
# завершает свой захват (settled): откат ему больше не приедет, и реестр
# не копит историю завершённых отправок -- 20 успешных send подряд
# оставляют одну подтверждённую запись-держателя; держатель -- экран
# чужих откатов, без него поздний провал стирал бы файл успешного.
# Переход реестра подтверждает сама запись файла, не решение о ней:
# bsh отката упал или молчит (#171) -- захват остаётся живым и держателем,
# причина уходит с отказом (#181), никакого «восстановились метаданными».
# Реестр -- не аудит каждого send: недостижимое для отката выметается,
# остаются только live-захваты, их цепочки предшественников и подтвер-
# ждённый держатель. Проверка реестра -- ВНУТРИ замка и ДО записи отката:
# ранний предчек до замка -- только оптимизация, между ним и входом в
# замок поколение меняет чужой захват, и откат без повторной проверки
# стирал бы успешную аренду (гонка ниже: A ждёт замка, пока B внутри).
# Прежняя запись для отката -- из фактов под тем же замком: файл аренды
# пишет только агент, и устареть раньше нас некому; отдельного чтения
# файла откат не делает -- реестр уже помнит байты на момент захвата.
# Файл на диске не меняется вовсе (user\tat, Owner.render), чтение чужое
# -- прежним: никакой миграции и окна нечитаемости при rollout. Реестр
# живёт, пока живёт процесс; перезапуск его сбрасывает, но это ничьих
# откатов не рвёт: undo живёт в том же процессе, что и захват. Короткий
# замок через idle wait не держится.
# STATUS: FIXED — see #573
def check_same_owner_generation_573(c):
    """Шелл и файл настоящие, как #189: сравнение-и-запись проверяется
    как есть."""
    import asyncio
    import tempfile
    from mop.common import lease
    from mop.common.domain import Owner
    root = tempfile.mkdtemp(prefix="mop-test-573-")
    os.makedirs(os.path.join(root, ".git"))
    path = os.path.join(root, lease.FILE)

    async def facts(name):
        text = open(path).read() if os.path.exists(path) else ""
        owner = Owner.parse(text)
        return {"owner": owner and owner.to_dict(), "dirty": 0, "ahead": 0,
                "cur": "master", "def": "master"}

    async def no_event(*a, **k):
        return None

    def owner_now():
        return Owner.parse(open(path).read()) if os.path.exists(path) else None
    with restored(agent, "bsh", "clone_facts", "clone_dir", "session_json", "_event",
                  "_owner_locks", "_lease_generations", "_lease_claims", "_lease_holder"), restored(agent.time, "time"):
        agent.bsh, agent.clone_facts, agent._event = bash, facts, no_event
        agent.clone_dir = lambda name: root
        # Часы -- только аренды: verdict/Owner видят одну секунду на все
        # захваты, остальное (события) времени не касается.
        agent.time.time = lambda: 1000
        agent._owner_locks = {}
        agent._lease_generations = {}
        agent._lease_claims = {}
        agent._lease_holder = {}
        # A и B -- один логин в одну секунду: строки записи равны.
        # Перекрытие отправок умерло вместе с окном (передача -- под
        # замком), но захват force поверх ЖИВОЙ аренды остаётся реальным
        # сценарием: доставка A транспортно-неопределённа (timeout) --
        # откатов нет, захват жив; B берёт с force и доставляет. Откат A
        # не приедет вовсе -- и подавно не имеет права тронуть аренду B.
        async def race():
            async def deliver(name, cmd, timeout=20):
                if "--from-name a " in cmd:
                    return {"error": "the body did not answer in 20s",
                            "timeout": True}
                return {"msg_id": "m-b"}
            agent.session_json = deliver

            async def a():
                return await agent.v_send(None, {"name": "pu-mop-1", "message": "x",
                                                 "owner": "erin", "from_name": "a"})
            ra = await a()
            rb = await agent.v_send(None, {"name": "pu-mop-1", "message": "y",
                                           "owner": "erin", "from_name": "b",
                                           "force": True})
            return ra, rb
        ra, rb = asyncio.run(race())
        c.check("same-owner race: a must fail, b must deliver",
                not (not ra.get("error") or not ra.get("timeout")
                     or rb.get("error")), f"a {ra!r}, b {rb!r}")
        c.check("same-owner race: the lease must stay with the second taker",
                not (getattr(owner_now(), "user", None) != "erin"
                     or owner_now().at != 1000), repr(owner_now()))

        # Тот же сценарий без force: живая (indeterminate) аренда A и тот
        # же логин у B -- verdict пропускает своего без force, запись
        # обновляется поколением, не байтами: равные строки не дают B
        # «уже стоящего» файла.
        async def race2():
            async def deliver(name, cmd, timeout=20):
                if "--from-name a " in cmd:
                    return {"error": "the body did not answer in 20s",
                            "timeout": True}
                return {"msg_id": "m-b"}
            agent.session_json = deliver

            async def a():
                return await agent.v_send(None, {"name": "pu-mop-1", "message": "x",
                                                 "owner": "erin", "from_name": "a"})
            ra = await a()
            rb = await agent.v_send(None, {"name": "pu-mop-1", "message": "y",
                                           "owner": "erin", "from_name": "b"})
            return ra, rb
        ra, rb = asyncio.run(race2())
        c.check("same-owner no-force race: a must fail, b must deliver",
                not (not ra.get("error") or not ra.get("timeout")
                     or rb.get("error")), f"a {ra!r}, b {rb!r}")
        c.check("same-owner no-force race: the lease must stay with the second taker",
                not (getattr(owner_now(), "user", None) != "erin"
                     or owner_now().at != 1000), repr(owner_now()))

        # Гонка замка в старом порядке (предчек до замка) больше не
        # воспроизводима: захват -> identity -> передача -- одна критическая
        # секция, interleaving'а нет (review-2 #574). Инвариант, который она
        # защищала, -- откат провалившегося захвата не стирает аренду
        # успешного поверх -- проверяем напрямую, последовательными
        # отправками: A провалился и завершился, B доставил поверх.
        if os.path.exists(path):
            os.remove(path)

        async def deliver_a_fails(name, cmd, timeout=20):
            if "--from-name a " in cmd:
                return {"error": "not delivered"}
            return {"msg_id": "m-b"}

        async def lock_race():
            agent.clone_facts, agent.session_json = facts, deliver_a_fails

            async def a():
                return await agent.v_send(None, {"name": "pu-mop-1", "message": "x",
                                                 "owner": "erin", "from_name": "a"})
            ra = await a()
            rb = await agent.v_send(None, {"name": "pu-mop-1", "message": "y",
                                           "owner": "erin", "from_name": "b",
                                           "force": True})
            return ra, rb
        agent._owner_locks = {}
        agent._lease_generations = {}
        agent._lease_claims = {}
        agent._lease_holder = {}
        ra, rb = asyncio.run(lock_race())
        c.check("lock race: a must fail, b must deliver",
                not (not ra.get("error") or rb.get("error")), f"a {ra!r}, b {rb!r}")
        c.check("lock race: the rollback must not erase the later lease",
                not (getattr(owner_now(), "user", None) != "erin"
                     or owner_now().at != 1000), repr(owner_now()))

        # Два последовательных неудачных захвата одного логина (в старом
        # виде -- перекрывающиеся; interleaving умер вместе с окном, но
        # инвариант жив): B взял поверх A (строки равны), оба провалились.
        # Откат B обязан понять, что под ним проваленный A, и вернуть
        # «прежде» A (файла не было) -- не воскресить запись A; двойной
        # провал не оставляет «живого» владельца.
        if os.path.exists(path):
            os.remove(path)

        async def nested_race():
            async def deliver(name, cmd, timeout=20):
                return {"error": "not delivered"}
            agent.clone_facts, agent.session_json = facts, deliver

            async def a():
                return await agent.v_send(None, {"name": "pu-mop-1", "message": "x",
                                                 "owner": "erin", "from_name": "a"})
            ra = await a()
            rb = await agent.v_send(None, {"name": "pu-mop-1", "message": "y",
                                           "owner": "erin", "from_name": "b",
                                           "force": True})
            return ra, rb
        agent._owner_locks = {}
        agent._lease_generations = {}
        agent._lease_claims = {}
        agent._lease_holder = {}
        ra, rb = asyncio.run(nested_race())
        c.check("nested double failure (B fails first): both must fail",
                not (not ra.get("error") or not rb.get("error")),
                f"a {ra!r}, b {rb!r}")
        c.check("nested double failure (B fails first): no lease of a failed send may stay",
                owner_now() is None, repr(owner_now()))

        # Тот же двойной провал «в обратном порядке» (A закончился раньше
        # B -- под замком это порядок возвратов): B взял аренду, A успел
        # провалиться и завершиться (его провал отмечен в реестре, файла он
        # не трогал -- поверх стоял B), затем падает B -- её откат обязан
        # перепрыгнуть проваленную запись A, а не восстановить её. Оба
        # провалились -- файла быть не должно, а не «ложная аренда A висит
        # до TTL».
        if os.path.exists(path):
            os.remove(path)

        async def nested_race_reversed():
            async def deliver(name, cmd, timeout=20):
                return {"error": "not delivered"}
            agent.clone_facts, agent.session_json = facts, deliver

            async def a():
                return await agent.v_send(None, {"name": "pu-mop-1", "message": "x",
                                                 "owner": "erin", "from_name": "a"})
            ra = await a()
            rb = await agent.v_send(None, {"name": "pu-mop-1", "message": "y",
                                           "owner": "erin", "from_name": "b",
                                           "force": True})
            return ra, rb
        agent._owner_locks = {}
        agent._lease_generations = {}
        agent._lease_claims = {}
        agent._lease_holder = {}
        ra, rb = asyncio.run(nested_race_reversed())
        c.check("nested double failure (A still in flight): both must fail",
                not (not ra.get("error") or not rb.get("error")),
                f"a {ra!r}, b {rb!r}")
        c.check("nested double failure (A still in flight): no lease of a failed send may stay",
                owner_now() is None, repr(owner_now()))

        # Три последовательных захвата (перекрытие умерло с окном,
        # причинная схема жива): A(gen1,1000) взял и провалился с откатом
        # (файла снова нет); B(gen2,1001) взял с force поверх indeterminate
        # A? Нет -- здесь все провалы подтверждённые, откат исполнен;
        # C(gen3,1002) берёт поверх. Инвариант: предшественник C -- чья
        # запись лежала в файле на момент захвата, а не соседний номер;
        # провалившийся A не воскресает из отката C; исходно файла не было
        # -- файла быть не должно.
        if os.path.exists(path):
            os.remove(path)

        async def rolled_back_predecessor_race():
            clock = {"t": 1000}
            writes = {"n": 0}

            async def tracked_bsh(name, script, timeout=20):
                out, code = await bash(name, script, timeout)
                # Часы двигаются между захватами: записи A/B/C различны
                # байтами (1000/1001/1002), равенство строк никого не спасает.
                if script.startswith("printf"):
                    writes["n"] += 1
                    clock["t"] = 1000 + writes["n"]
                return out, code

            async def deliver(name, cmd, timeout=20):
                return {"error": "not delivered"}
            agent.clone_facts, agent.session_json = facts, deliver
            agent.bsh = tracked_bsh
            agent.time.time = lambda: clock["t"]

            async def a():
                return await agent.v_send(None, {"name": "pu-mop-1", "message": "x",
                                                 "owner": "erin", "from_name": "a"})
            ra = await a()
            saw_after_a = owner_now()
            rb = await agent.v_send(None, {"name": "pu-mop-1", "message": "y",
                                           "owner": "erin", "from_name": "b",
                                           "force": True})
            saw_after_b = owner_now()
            rc = await agent.v_send(None, {"name": "pu-mop-1", "message": "z",
                                           "owner": "erin", "from_name": "c",
                                           "force": True})
            return ra, rb, rc, saw_after_a, saw_after_b
        agent._owner_locks = {}
        agent._lease_generations = {}
        agent._lease_claims = {}
        agent._lease_holder = {}
        ra, rb, rc, after_a, after_b = asyncio.run(rolled_back_predecessor_race())
        agent.time.time = lambda: 1000
        c.check("rolled-back predecessor: all three must fail",
                not (not ra.get("error") or not rb.get("error") or not rc.get("error")),
                f"a {ra!r}, b {rb!r}, c {rc!r}")
        c.check("rolled-back predecessor: each rollback stood in the file before the next claim",
                not (after_a is not None or after_b is not None),
                f"after A {after_a!r}, after B {after_b!r}")
        c.check("rolled-back predecessor: no lease of a failed send may stay",
                owner_now() is None, repr(owner_now()))
        c.check("rolled-back predecessor: the ledger keeps no audit of settled sends",
                not agent._lease_claims.get(path),
                repr(agent._lease_claims.get(path)))
        c.check("rolled-back predecessor: the holder pointer is empty again",
                agent._lease_holder.get(path) is None,
                repr(agent._lease_holder.get(path)))

        # Успешный преемник не трогается (инвариант #573 жив и без
        # перекрытия): A провалился и завершился, B доставил поверх --
        # файл держит erin (B), реестр -- только подтверждённого держателя.
        if os.path.exists(path):
            os.remove(path)

        async def successor_race():
            async def deliver(name, cmd, timeout=20):
                if "--from-name a " in cmd:
                    return {"error": "not delivered"}
                return {"msg_id": "m-b"}
            agent.clone_facts, agent.session_json = facts, deliver

            async def a():
                return await agent.v_send(None, {"name": "pu-mop-1", "message": "x",
                                                 "owner": "erin", "from_name": "a"})
            ra = await a()
            rb = await agent.v_send(None, {"name": "pu-mop-1", "message": "y",
                                           "owner": "erin", "from_name": "b",
                                           "force": True})
            return ra, rb
        agent._owner_locks = {}
        agent._lease_generations = {}
        agent._lease_claims = {}
        agent._lease_holder = {}
        ra, rb = asyncio.run(successor_race())
        c.check("successful successor: a must fail, b must deliver",
                not (not ra.get("error") or rb.get("error")), f"a {ra!r}, b {rb!r}")
        c.check("successful successor: the lease must stay with the successor",
                not (getattr(owner_now(), "user", None) != "erin"
                     or owner_now().at != 1000), repr(owner_now()))
        c.check("successful successor: the ledger keeps only the confirmed holder",
                list(agent._lease_claims.get(path, {})) == [2]
                and agent._lease_claims[path][2][0] == "settled"
                and agent._lease_holder.get(path) == 2,
                repr(agent._lease_claims.get(path)))
        agent.bsh = bash

        # Успешная доставка завершает захват: подтверждённая база -- последняя
        # успешная аренда, историю завершённых отправок реестр не копит.
        # 20 успешных send подряд (никаких in-flight к концу) -- состояние
        # пути остаётся константным: одна подтверждённая запись, файл верен.
        async def delivered(name, cmd, timeout=20):
            return {"msg_id": "m"}
        agent.clone_facts, agent.session_json = facts, delivered
        if os.path.exists(path):
            os.remove(path)
        agent._owner_locks = {}
        agent._lease_generations = {}
        agent._lease_claims = {}
        agent._lease_holder = {}
        for _ in range(20):
            got = asyncio.run(agent.v_send(None, {"name": "pu-mop-1", "message": "m",
                                                  "owner": "erin", "from_name": "s"}))
            if got.get("error"):
                break
        c.check("20 successful sends: all delivered",
                not got.get("error"), repr(got))
        c.check("20 successful sends: the lease file holds the last owner",
                not (getattr(owner_now(), "user", None) != "erin"
                     or owner_now().at != 1000), repr(owner_now()))
        c.check("20 successful sends: settled history is not kept",
                len(agent._lease_claims.get(path, {})) <= 1,
                repr(agent._lease_claims.get(path)))

        # Откат подтверждается записью файла, а не решением о ней: если bsh
        # записи упал или молчит (#171), holder/prune обязаны ждать исхода --
        # иначе физический файл держит чужую (нашу) запись, а реестр уже
        # «восстановился»: расхождение, и следующий откат верит неверной базе.
        # Диагностика #181 при этом не теряется: причина уходит с отказом.
        async def undelivered(name, cmd, timeout=20):
            return {"error": "not delivered"}
        for label, reply in (("a failed rollback shell", ("rm: Permission denied", 1)),
                             ("a timed-out rollback shell", ("", None))):
            if os.path.exists(path):
                os.remove(path)
            agent._owner_locks = {}
            agent._lease_generations = {}
            agent._lease_claims = {}
            agent._lease_holder = {}
            calls = {"n": 0}

            async def failing_bsh(name, script, timeout=20):
                calls["n"] += 1
                if script.startswith("printf"):
                    return await bash(name, script, timeout)
                return reply
            agent.bsh = failing_bsh
            agent.session_json = undelivered
            got = asyncio.run(agent.v_send(None, {"name": "pu-mop-1", "message": "z",
                                                  "owner": "dave", "from_name": "d"}))
            c.check(f"{label}: the refusal carries the rollback failure (#181)",
                    "owner not restored" in (got.get("error") or ""), repr(got))
            c.check(f"{label}: the claim is not settled in the ledger before the file",
                    agent._lease_claims.get(path, {}).get(1, [None])[0] == "live"
                    and agent._lease_holder.get(path) == 1,
                    repr(agent._lease_claims.get(path)))
        agent.bsh = bash

        # Одинокий провал откатывает как прежде: к прежнему владельцу, а
        # без него файл снимается. Прежний владелец здесь истёкшая carol
        # (аренда мертва при now=1000 -- окно больше секунды), захват dave
        # законен и откат обязан вернуть carol; чужой живой аренды нет --
        # иначе это refusal и откатывать нечего.
        async def undelivered(name, cmd, timeout=20):
            return {"error": "not delivered"}
        agent.clone_facts, agent.session_json = facts, undelivered
        stale = Owner("carol", 400).render()
        for before, want in ((None, None), (stale, stale)):
            if before is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                open(path, "w").write(before)
            agent._owner_locks = {}
            agent._lease_generations = {}
            agent._lease_claims = {}
            agent._lease_holder = {}
            got = asyncio.run(agent.v_send(None, {"name": "pu-mop-1", "message": "z",
                                                  "owner": "dave", "from_name": "d"}))
            now = open(path).read() if os.path.exists(path) else None
            c.check(f"single failed send over {before!r} rolls back as before",
                    not (not got.get("error") or now != want),
                    f"{got!r}, file {now!r}, wanted {want!r}")


def check_unclaim_race_189(c):
    """HYPOTHESIS (#189): v_send ставит аренду под _owner_locks, а откат
    неудачной доставки делает ВНЕ замка и не глядя: второй мастер (force)
    успевает взять папета, пока первый ждёт доставки, и откат первого
    перетирает его запись прежним владельцем или стирает файл.
    SOLUTION: откат -- под тем же замком и только если в файле всё ещё наша
    запись. STATUS: FIXED — see #189

    Шелл настоящий: bsh исполняет скрипт агента bash'ем над временным
    клоном, так что сравнение-и-запись проверяется как есть, а не заглушкой."""
    import asyncio
    import tempfile
    from mop.common import lease
    from mop.common.domain import Owner
    root = tempfile.mkdtemp(prefix="mop-test-189-")
    os.makedirs(os.path.join(root, ".git"))
    path = os.path.join(root, lease.FILE)

    async def facts(name):
        text = open(path).read() if os.path.exists(path) else ""
        owner = Owner.parse(text)
        return {"owner": owner and owner.to_dict(), "dirty": 0, "ahead": 0,
                "cur": "master", "def": "master"}

    async def no_event(*a, **k):
        return None

    def owner_now():
        return Owner.parse(open(path).read()) if os.path.exists(path) else None
    with restored(agent, "bsh", "clone_facts", "clone_dir", "session_json", "_event",
                  "_owner_locks", "_lease_generations"), restored(agent.time, "time"):
        agent.bsh, agent.clone_facts, agent._event = bash, facts, no_event
        agent.clone_dir = lambda name: root
        agent._owner_locks = {}
        agent._lease_generations = {}
        agent._lease_claims = {}
        agent._lease_holder = {}
        # Часы фиксированы и достаточно поздние: carol из отката ниже --
        # истёкшая аренда, которую dave законно берёт (#573 уточрил возраст).

        # Гонка в старом виде (перекрытие отправок) умерла вместе с окном:
        # захват -> identity -> передача -- одна критическая секция
        # (review-2 #574). Живой сценарий force: A взял аренду, доставка
        # транспортно-неопределённа (timeout, откатов нет, захват жив);
        # B берёт с force и доставляет. Откат A не приедет -- и подавно
        # не перетрёт запись B прежним владельцем и не сотрёт файл.
        agent.time.time = lambda: 10_000_000
        async def race():
            async def deliver(name, cmd, timeout=20):
                if "--from-name a " in cmd:
                    return {"error": "the body did not answer in 20s",
                            "timeout": True}
                return {"msg_id": "m-b"}
            agent.session_json = deliver

            async def a():
                return await agent.v_send(None, {"name": "pu-mop-1", "message": "x",
                                                 "owner": "alice", "from_name": "a"})
            ra = await a()
            rb = await agent.v_send(None, {"name": "pu-mop-1", "message": "y",
                                           "owner": "bob", "from_name": "b",
                                           "force": True})
            return ra, rb
        ra, rb = asyncio.run(race())
        c.check("race: a must fail, b must deliver",
                not (not ra.get("error") or not ra.get("timeout")
                     or rb.get("error")), f"a {ra!r}, b {rb!r}")
        c.check("race: the lease must stay with bob",
                not (getattr(owner_now(), "user", None) != "bob"), repr(owner_now()))

        # Одна неудачная доставка -- откат как прежде: к прежнему владельцу,
        # а без него файл снимается.
        async def undelivered(name, cmd, timeout=20):
            return {"error": "not delivered"}
        agent.session_json = undelivered
        stale = Owner("carol", 1000).render()
        for before, want in ((None, None), (stale, stale)):
            if before is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                open(path, "w").write(before)
            got = asyncio.run(agent.v_send(None, {"name": "pu-mop-1", "message": "z",
                                                  "owner": "dave", "from_name": "d"}))
            now = open(path).read() if os.path.exists(path) else None
            c.check(f"single failed send over {before!r}",
                    not (not got.get("error") or now != want),
                    f"{got!r}, file {now!r}, wanted {want!r}")


def check_gates_40(c):
    """HYPOTHESIS (#40): владельца сверяет только send; type (slash) и wipe
    пускают любого мастера проекта к папету, которого ведёт другой.
    SOLUTION: те же ворота (lease.may_touch) под тем же замком на папета,
    что у send; оператор (субъект admin) проходит всегда, force -- называя,
    у кого. Глагол clone отдаёт факты клона и при мёртвой сессии: по ним
    решает сервис кластера.
    STATUS: FIXED — see #40"""
    import asyncio
    import time
    from mop.common.domain import Owner
    olga = Owner("olga", int(time.time()) - 60).to_dict()
    clone = {"cur": "bug/1-x", "def": "master", "dirty": 2, "ahead": 0, "owner": olga}
    shelled, destroyed = [], []

    async def facts(name):
        return dict(clone)

    async def dead(name):
        return False

    async def no_event(*a, **k):
        return None

    class Driver:
        async def destroy(self, name, branch=None):
            destroyed.append(name if branch is None else (name, branch))
            return {"target": "gone"}
    with restored(agent, "bsh", "clone_facts", "tmux_alive", "DRIVER", "_event"):
        agent.bsh, agent.clone_facts, agent.tmux_alive = canned(calls=shelled), facts, dead
        agent.DRIVER, agent._event = Driver(), no_event
        base = {"name": "pu-mop-1", "_project": "mop"}
        # #257: ветка мастера едет глаголом wipe до драйвера. STATUS: FIXED — see #257
        destroyed.clear()
        got = asyncio.run(agent.v_wipe(None, {**base, "owner": "olga", "branch": "swarm"}))
        c.check("#257 wipe must hand the branch to the driver",
                not (got.get("error") or destroyed != [("pu-mop-1", "swarm")]),
                f"{got} {destroyed}")
        for verb, fn, extra, touched in (("type", agent.v_type, {"command": "/status"}, shelled),
                                         ("wipe", agent.v_wipe, {}, destroyed)):
            for who, more, allowed in (("another master", {"owner": "anton"}, False),
                                       ("anonymous", {}, False),
                                       ("the owner", {"owner": "olga"}, True),
                                       ("force", {"owner": "anton", "force": True}, True),
                                       ("the operator", {"owner": "anton", "_project": "admin"}, True)):
                touched.clear()
                got = asyncio.run(fn(None, {**base, **extra, **more}))
                if allowed:
                    c.check(f"{verb} by {who} must pass",
                            not (got.get("error") or not touched), repr(got))
                if not allowed:
                    c.check(f"{verb} by {who} must be refused naming olga, touching nothing",
                            not ("olga" not in (got.get("error") or "") or touched),
                            f"{got!r}, {touched}")
                if who == "force":
                    c.check(f"{verb} with force must name whom",
                            not ("olga" not in (got.get("owner_note") or "")), repr(got))
        # Ничей -- как до #40: запрос без owner проходит.
        clone["owner"] = None
        shelled.clear()
        got = asyncio.run(agent.v_type(None, {**base, "command": "/status"}))
        c.check("type on nobody's puppet must pass as before",
                not (got.get("error") or not shelled), repr(got))
        fn = getattr(agent, "v_clone", None)
        if c.check("agent.v_clone exists", not (fn is None)):
            got = asyncio.run(fn(None, dict(base)))
            c.expect("clone must return the clone's facts", got, {"clone": clone})


def check_caller_207(c):
    """HYPOTHESIS (#207): агент видит из субъекта только проект, а кто
    просит -- поле owner в теле, которое пишет сам отправитель; ворота (#40)
    и аренда send (#161) стоят на названном, а не на проверенном.
    SOLUTION: логин человека -- токеном субъекта (mop.<p>.node.<n>.rpc.<логин>),
    публиковать туда NATS даёт только ему (natsconf, операторы); агент берёт
    вызывающего из субъекта и тело в этом не участвует. Переход: прежний rpc
    без логина принимается, и логин там -- названный телом (self-declared)
    до уборки; публичный msg (папеты) владельца не несёт вовсе.
    STATUS: FIXED — see #207"""
    import asyncio
    from mop.common import bus, lease
    got = {s: busnames.caller(s) for s in (
        "mop.mop.node.hyper.rpc.alice", "mop.mop.node.hyper.rpc",
        "mop.mop.node.hyper.msg", "mop.mop.cluster.rpc.alice", "mop.mop.cluster.rpc",
        "mop.mop.node.hyper.msg.alice", "mop.mop.master.h-1.inbox",
        "mop.mop.node.hyper.rpc.anton%2Eermak", "mop.mop.cluster.rpc.anton%2Eermak",
        "mop.mop.node.hyper.rpc.a%2eb", "mop.mop.node.hyper.rpc.*")}
    want = {"mop.mop.node.hyper.rpc.alice": "alice", "mop.mop.node.hyper.rpc": None,
            "mop.mop.node.hyper.msg": None, "mop.mop.cluster.rpc.alice": "alice",
            "mop.mop.cluster.rpc": None, "mop.mop.node.hyper.msg.alice": None,
            "mop.mop.master.h-1.inbox": None,
            "mop.mop.node.hyper.rpc.anton%2Eermak": "anton.ermak",
            "mop.mop.cluster.rpc.anton%2Eermak": "anton.ermak",
            "mop.mop.node.hyper.rpc.a%2eb": None, "mop.mop.node.hyper.rpc.*": None}
    c.expect("busnames.caller", got, want)
    # Логин -- любой, кроме пустого и управляющих символов: в LDAP/AD
    # anton.ermak -- норма (#208). В токен субъекта он кодируется обратимо.
    for bad in ("", None, "a\tb", "a\nb", "a\x00b", "a\x7fb"):
        c.check(f"{bad!r} must not be a login", not (busnames.valid_login(bad)))
    for fine in ("anton", "ivan_p-2", "anton.ermak", "a b", "a*", "a>", "a%b", "Антон"):
        c.check(f"{fine!r} must be a login", not (not busnames.valid_login(fine)))
    for login, token in (("anton", "anton"), ("anton.ermak", "anton%2Eermak"),
                         ("a b", "a%20b"), ("a*>", "a%2A%3E"), ("50%", "50%25"),
                         ("Антон", "Антон"), ("a b", "a%C2%A0b")):
        c.expect(f"login_token({login!r})", busnames.login_token(login), token)
        c.expect(f"login_of({token!r})", busnames.login_of(token), login)
        c.check(f"token {token!r} is a single literal NATS token",
                not (any(ch in token for ch in ".*> ") or any(ch.isspace() for ch in token)))
    # Инъективно: «a.b» и буквальное «a%2Eb» -- разные токены.
    c.check("two logins must never share a token: a.b vs a%2Eb",
            not (busnames.login_token("a.b") == busnames.login_token("a%2Eb")))
    # Неканоничный токен -- не логин: иначе один логин читался бы из двух.
    for token in ("a%2eb", "a%41", "a%", "a%zz", "*"):
        c.check(f"login_of({token!r}) must be None: not a canonical token",
                not (busnames.login_of(token) is not None))
    subs = busnames.agent_subscriptions("hyper")
    c.check("the agent must listen on both the login and the old rpc",
            not ("mop.*.node.hyper.rpc.*" not in subs["rpc"]
                 or "mop.*.node.hyper.rpc" not in subs["rpc"]), subs)

    # Разбор в handle: глагол-заглушка отдаёт то, что видят ворота.
    seen = []

    async def probe(_conn, req):
        seen.append(lease.caller(req))
        return {"ok": True}

    async def mine(name):
        return "mop"
    keep = dict(agent.VERBS)
    with restored(agent, "puppet_project"):
        try:
            agent.VERBS["send"] = dataclasses.replace(agent.VERBS["send"], fn=probe)
            agent.puppet_project = mine
            body = {"verb": "send", "name": "pu-mop-1", "owner": "bob", "_caller": "bob"}
            for subj, public, want in (
                    ("mop.mop.node.hyper.rpc.alice", False, ("alice", True)),
                    ("mop.mop.node.hyper.rpc", False, ("bob", False)),
                    ("mop.mop.node.hyper.msg", True, (None, False))):
                seen.clear()
                asyncio.run(agent.handle(None, Msg(subj, body=body), public))
                c.expect(f"caller over {subj} with a forged body", seen, [want])
        finally:
            agent.VERBS.clear()
            agent.VERBS.update(keep)

    # Клиент: человек спрашивает по субъекту со своим логином, машина -- без.
    with restored(bus, "login"):
        bus.login = lambda: "alice"
        c.expect("a human's rpc subject must carry the login",
                 bus.subject("hyper", project="mop"), "mop.mop.node.hyper.rpc.alice")
        c.check("msg carries no login",
                not (bus.subject("hyper", "msg", project="mop") != "mop.mop.node.hyper.msg"))
        c.expect("a human's cluster subject must carry the login",
                 bus.cluster_subject("mop"), "mop.mop.cluster.rpc.alice")
        bus.login = lambda: "anton.ermak"
        c.check("a dotted login must travel encoded",
                not (bus.subject("hyper", project="mop") != "mop.mop.node.hyper.rpc.anton%2Eermak"
                     or bus.cluster_subject("mop") != "mop.mop.cluster.rpc.anton%2Eermak"))
        bus.login = lambda: None
        c.check("a machine asks without a login token",
                not (bus.subject("hyper", project="mop") != "mop.mop.node.hyper.rpc"
                     or bus.cluster_subject("mop") != "mop.mop.cluster.rpc"))
    c.check("without_caller must strip exactly the login token (the fallback to "
            "an agent from before #207)",
            not (busnames.without_caller("mop.mop.node.hyper.rpc.alice") != "mop.mop.node.hyper.rpc"
                 or busnames.without_caller("mop.mop.cluster.rpc.alice") != "mop.mop.cluster.rpc"
                 or busnames.without_caller("mop.mop.events") != "mop.mop.events"))


def check_git_identity_167(c):
    """HYPOTHESIS (#167): в клоне папета нет user.name/user.email, и пул
    коммитит разовым `git -c`, который назвал мастер; оператор решил, что
    автор коммита -- человек, прошедший проверку на шине.
    SOLUTION: записав ПРОВЕРЕННОГО владельца (логин из субъекта, #207) после
    доставки, агент спрашивает профиль у сервиса сервера (глагол identity,
    server.rpc) и ставит repo-local user.name/user.email в клон. Правило одно:
    после проверенной аренды в клоне identity нового владельца или никакой --
    профиля нет или сервер не ответил, значит identity снята, а не оставлена
    прежнему (иначе коммиты нового владельца шли бы под чужим именем), и
    заметка говорит мастеру коммитить с -c. Названный телом владелец (прежний
    субъект) identity не трогает; неудачная доставка -- тоже: аренда
    откатывается, и identity остаётся той, что была.
    STATUS: FIXED — see #167

    Шелл и git настоящие: bsh исполняет скрипт агента bash'ем над временным
    клоном, identity читается `git config --local`."""
    import asyncio
    import json
    import subprocess
    import tempfile
    from mop.common import lease
    from mop.common.domain import Owner
    profiles = {"olga": {"name": "Ольга Петрова", "email": "olga@example.dev"},
                "petr": {"name": "Pyotr O'Neil", "email": "petr@example.dev"}}
    asked = []

    class Reply:
        def __init__(self, body):
            self.data = json.dumps(body).encode()

    class Conn:
        async def request(self, subject, data, timeout=None):
            req = json.loads(data.decode())
            asked.append((subject, req))
            if req.get("login") == "down":
                raise asyncio.TimeoutError()
            return Reply({"login": req["login"], **profiles.get(req["login"], {})})

    root = tempfile.mkdtemp(prefix="mop-test-167-")

    def clone():
        """Свежий клон: git init, без identity."""
        d = tempfile.mkdtemp(dir=root)
        subprocess.run(["git", "init", "-q", d], check=True)
        return d

    state = {}

    async def facts(name):
        path = os.path.join(state["clone"], lease.FILE)
        owner = Owner.parse(open(path).read()) if os.path.exists(path) else None
        return {"owner": owner and owner.to_dict(), "dirty": 1, "ahead": 0,
                "cur": "feat/1", "def": "master"}

    async def delivered(name, cmd, timeout=20):
        return {"error": "not delivered"} if state.get("fail") else {"msg_id": "m-1"}

    async def no_event(*a, **k):
        return None

    async def project(name):
        return "mop"

    def ident():
        got = {}
        for key in ("name", "email"):
            r = subprocess.run(["git", "-C", state["clone"], "config", "--local", f"user.{key}"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                got[key] = r.stdout.strip()
        return got

    def send(caller=None, owner=None, force=False):
        req = {"name": "pu-mop-1", "message": "x", "from_name": "m"}
        if caller:
            req["_caller"] = caller
        if owner:
            req["owner"] = owner
        if force:
            req["force"] = True
        return asyncio.run(agent.v_send(Conn(), req))

    with restored(agent, "bsh", "clone_facts", "clone_dir", "session_json",
                  "_event", "puppet_project"):
        agent.bsh, agent.clone_facts, agent.session_json = bash, facts, delivered
        agent._event, agent.puppet_project = no_event, project
        agent.clone_dir = lambda name: state["clone"]

        # Проверенный владелец с профилем -- identity в клоне; спросили
        # сервер своего проекта его логином.
        state["clone"] = clone()
        got = send(caller="olga")
        c.expect("verified owner: identity", ident(), profiles["olga"])
        c.expect("verified owner: the server's identity verb asked",
                 asked, [("mop.mop.server.rpc", {"verb": "identity", "login": "olga"})])
        c.check("verified owner: the reply must name the identity",
                not ("olga@example.dev" not in (got.get("owner_note") or "")), got)

        # Названный телом (прежний субъект) -- identity не трогаем, сервер
        # не спрашиваем.
        state["clone"], asked[:] = clone(), []
        got = send(owner="bob")
        c.check("self-declared: identity untouched, server not asked",
                not (ident() or asked or got.get("error")),
                f"identity {ident()}, asked {asked}, reply {got}")

        # Профиля нет -- identity не ставится, заметка велит -c.
        state["clone"] = clone()
        got = send(caller="ivan")
        note = got.get("owner_note") or ""
        c.check("no profile: no identity, the note says git -c",
                not (ident() or got.get("error") or "ivan" not in note or "git -c" not in note),
                f"identity {ident()}, reply {got}")

        # Сервер не ответил -- доставка не страдает, identity нет, заметка.
        state["clone"] = clone()
        got = send(caller="down")
        c.check("server silent: no identity, the note says git -c",
                not (ident() or got.get("error") or "git -c" not in (got.get("owner_note") or "")),
                f"identity {ident()}, reply {got}")

        # force: аренда olga (работа в клоне) переходит к petr -- и identity.
        state["clone"] = clone()
        send(caller="olga")
        got = send(caller="petr", force=True)
        c.check("force: identity moves to petr",
                not (ident() != profiles["petr"]),
                f"identity {ident()}, wanted {profiles['petr']}; reply {got}")
        # ...к ivan без профиля -- identity olga'и снята, не оставлена ему.
        send(caller="olga", force=True)
        got = send(caller="ivan", force=True)
        c.check("force to a login without a profile: the old identity is removed",
                not (ident()), f"stays {ident()}")
        # Названный телом с force -- identity не трогает.
        send(caller="olga", force=True)
        send(owner="bob", force=True)
        c.expect("self-declared force: identity must stay", ident(), profiles["olga"])

        # Доставка не удалась -- аренда откатывается, identity прежняя.
        state["clone"] = clone()
        send(caller="olga")
        state["fail"] = True
        got = send(caller="petr", force=True)
        state["fail"] = False
        c.check("failed delivery: identity stays",
                not (not got.get("error") or ident() != profiles["olga"]),
                f"identity {ident()}, reply {got}")


# ── identity до доставки, а не после (#574) ───────────────────────────────
# HYPOTHESIS: v_send ставит аренду, потом ждёт доставки (при --wait -- до
# 600с) и только ПОСЛЕ неё _follow_owner ставит identity владельца. Весь ход
# папета идёт под прежней identity: первый коммит уходит под чужим именем,
# #313 ловит не всё (pre-commit зовётся не каждым действием). При wait=0
# остаётся гонка первого коммита: сессия стартует мгновенно. #167 проверял
# профиль ПОСЛЕ возврата, не в момент доставки; #313 и #573 порядок не чинили.
# SOLUTION: identity ставится ДО доставки, и захват -> identity -> короткая
# передача идут ПОД ОДНИМ замком папета (review-2 #574, п.1): проверка
# владения в шелле была check-then-use -- между cat-сравнением и session.py
# владение успевало уйти, и сообщение доставлялось под чужой identity.
# Взаимное исключение -- сам замок: session.py send уходит с --wait 0 и
# потолком 20с, окно короткое и ограниченное; ожидание простоя (до 600с) --
# отдельный глагол wait-idle ВНЕ замка, и ошибка ожидания -- не недоставка
# уже доставленного. Гонки #573/#189 остаются инвариантами координации
# реестра (экран, причинная цепочка, prune) -- но их хореография теперь
# сериализована замком, и тесты проверяют инварианты напрямую, не держат
# дырявый порядок ради старого interleaving'а. Снимок прежнего состояния
# печатает тот же шелл установки ДО первой записи кодированными метками
# mop574-<ключ>\t<флаг><base64> (review-2, п.3/4): присутствие -- флагом
# (P/A/X), значение -- всегда base64 -- literal '-' валиден и не сентинел,
# многострочные значения не вытекают в диагностику; чтение различает «нет
# ключа» (rc 1, пустой stderr) от «не прочиталось» (rc 1, непустой stderr):
# второе -- X, и установка отказывает, не «восстанавливая» неизвестное.
# Установка с компенсацией (review-2, п.2): точка отказа возвращает точные
# bytes/режим/присутствие каждого затронутого элемента и ПОДТВЕРЖДАЕТ
# возврат сверкой; код 1 -- «отказала, прежнее вернулось» (аренду можно
# откатывать с причиной), код 2 -- «прежнее НЕ вернулось»: аренда остаётся
# за пославшим, обе причины едут мастеру, «восстановлено» не говорится.
# Восстановление -- по цепочке предшественников реестра #573, не новым
# вопросом серверу; чужие hooks/core.hooksPath не трогаются; нечитанная
# секция снимка (identity или hooks) -- отказ, а не молчаливый пропуск.
# Подтверждённая недоставка (структурированный {"error"} от session.py)
# откатывает пару; тишина тела и ответ без JSON (разрыв транспорта) --
# исход неизвестен: помечен timeout, откатов нет. Ошибка подготовки
# identity -- отказ ДО доставки; не пускать задание под неверной identity
# важнее, чем доставить вовремя. Смена identity -- только подтверждённому
# caller (#207): self-declared владелец identity не трогает вовсе.
# STATUS: FIXED — see #574
def check_identity_before_delivery_574(c):
    """git и шелл настоящие: скрипты агента исполняются bash'ем над
    временными клонами с настоящими user.name/email и hooks."""
    import asyncio
    import shlex
    import subprocess
    import tempfile
    from mop.common import lease
    from mop.common.domain import Owner
    profiles = {"olga": {"name": "Ольга Петрова", "email": "olga@example.dev"},
                "petr": {"name": "Pyotr O'Neil", "email": "petr@example.dev"}}
    root = tempfile.mkdtemp(prefix="mop-test-574-")
    real_identity_script = agent.identity_script

    def clone(profile=None, own_hook=None):
        """Свежий клон: git init, опционально identity и свой хук."""
        d = tempfile.mkdtemp(dir=root)
        subprocess.run(["git", "init", "-q", "-b", "master", d], check=True)
        for key in ("name", "email"):
            if profile:
                subprocess.run(["git", "-C", d, "config", "--local", f"user.{key}",
                                profile[key]], check=True)
        if own_hook:
            hp = os.path.join(d, ".git", "hooks", "pre-commit")
            with open(hp, "w") as f:
                f.write(own_hook)
            os.chmod(hp, 0o755)
        return d

    state = {}

    def ident(d):
        got = {}
        for key in ("name", "email"):
            r = subprocess.run(["git", "-C", d, "config", "--local", f"user.{key}"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                got[key] = r.stdout.strip()
        return got
    real_session_json = agent.session_json

    def hooks(d):
        h = os.path.join(d, ".git", "hooks")
        return sorted(f for f in os.listdir(h) if not f.endswith(".sample"))

    async def facts(name):
        path = os.path.join(state["clone"], lease.FILE)
        owner = Owner.parse(open(path).read()) if os.path.exists(path) else None
        return {"owner": owner and owner.to_dict(), "dirty": 0, "ahead": 0,
                "cur": "master", "def": "master"}

    async def no_event(*a, **k):
        return None

    async def project(name):
        return "mop"

    def send(**extra):
        req = {"name": "pu-mop-1", "message": "x", "from_name": "m"}
        req.update(extra)
        return asyncio.run(agent.v_send(None, req))

    with restored(agent, "bsh", "clone_facts", "clone_dir", "session_json",
                  "_event", "puppet_project", "owner_profile", "_owner_locks",
                  "_lease_generations", "_lease_claims", "_lease_holder",
                  "IDENTITY_WAIT"), restored(agent.time, "time"):
        agent.bsh, agent.clone_facts = bash, facts
        agent._event, agent.puppet_project = no_event, project
        agent.time.time = lambda: 1000
        # Профиль -- заглушка профиля, не шина: у сценария спрашивают только
        # логин, серверная сторона #167 проверена своим набором.
        asked = []

        async def profile_of(conn, name, login):
            asked.append(login)
            if state.get("server_down") or login not in profiles:
                return None, "no name/email in the profile"
            return profiles[login], None
        agent.owner_profile = profile_of
        agent.IDENTITY_WAIT = 0.1
        agent.clone_dir = lambda name: state["clone"]
        # Все сценарии -- один путь аренды во времени теста, как #573.
        rearm = lambda: (agent.__dict__.update(  # noqa: E731
            _owner_locks={}, _lease_generations={}, _lease_claims={},
            _lease_holder={}), os.path.exists(os.path.join(state["clone"], lease.FILE))
            and os.remove(os.path.join(state["clone"], lease.FILE)))

        # 1. identity новой владелицы стоит УЖЕ в момент доставки: захват ->
        # identity -> доставка, не наоборот. Прежняя (petr) не остаётся.
        state["clone"] = clone(profiles["petr"])
        rearm()
        seen = {}

        async def deliver(name, cmd, timeout=20):
            seen["ident"] = ident(state["clone"])
            seen["cmd"] = cmd
            return {"msg_id": "m-1"}
        agent.session_json = deliver
        got = send(_caller="olga")
        c.check("574 identity is in place before the body sees the message",
                not (seen["ident"] != profiles["olga"]),
                f"at delivery {seen.get('ident')}, reply {got}")
        c.check("574 delivery goes without the in-band idle wait",
                not ("--wait 0" not in seen["cmd"]), repr(seen.get("cmd")))

        # 2. wait не держит подготовку: v_send с wait>0 доставляет тем же
        # коротким путём, простой ждёт отдельный вызов session.py после
        # доставки. Ошибка ожидания -- не недоставка: msg_id стоит, аренда
        # жива, откатов нет.
        state["clone"] = clone(profiles["petr"])
        rearm()
        waits = []

        async def deliver_wait(name, cmd, timeout=20):
            waits.append(cmd)
            return {"msg_id": "m-2"}
        agent.session_json = deliver_wait
        got = send(_caller="olga", wait=30)
        c.check("574 wait: the send call carries no in-band wait",
                not (not waits or "--wait 0" not in waits[0]), repr(waits))
        c.check("574 wait: idle is awaited by a separate wait-idle call",
                len(waits) == 2 and "wait-idle" in waits[1]
                and " 30 " in waits[1], repr(waits))
        c.check("574 idle wait error is not a delivery failure",
                not (got.get("error") or not got.get("msg_id")), repr(got))

        # 3. владелец без профиля: прежняя identity снята ДО доставки,
        # заметка велит коммитить с -c.
        state["clone"] = clone(profiles["petr"])
        rearm()
        seen.clear()
        agent.session_json = deliver
        got = send(_caller="ivan")
        c.check("574 no profile: the old identity is gone before delivery",
                not (seen.get("ident") or got.get("error")),
                f"at delivery {seen.get('ident')}, reply {got}")

        # 4. ошибка подготовки -- отказ ДО доставки: сообщение не ушло,
        # аренда не берётся, старая identity не перетёрта. Молчание сервера
        # профиля -- не отказ подготовки (владелец остаётся без identity).
        state["clone"] = clone(profiles["petr"])
        rearm()
        sent = []

        async def deliver_spy(name, cmd, timeout=20):
            sent.append(cmd)
            return {"msg_id": "m-3"}
        agent.session_json = deliver_spy

        def broken_hook(clone, path, mine, profile):
            return "chmod: changing permissions: Operation not permitted"
        agent.identity_script = broken_hook
        got = send(_caller="olga")
        # Код вне контракта (0/1/2): компенсация не подтверждена -- аренда
        # НЕ откатывается как будто пара восстановлена, а остаётся за
        # пославшим; обе части причины доезжают (#181, review-2 п.2).
        c.check("574 broken prep: no delivery, the lease stays with the sender",
                not (not got.get("error")
                     or "git identity not set" not in got["error"]
                     or sent
                     or not os.path.exists(os.path.join(state["clone"], lease.FILE))
                     or ident(state["clone"]) != profiles["petr"]),
                f"reply {got!r}, delivered {sent!r}, "
                f"lease {os.path.exists(os.path.join(state['clone'], lease.FILE))}, "
                f"identity {ident(state['clone'])}")
        agent.identity_script = real_identity_script

        # 5. подтверждённая недоставка возвращает прежнюю согласованную пару:
        # аренду и identity petr -- не вычисляя её по изменившемуся профилю
        # (сервер к этому моменту молчит, «petr» больше не ответит).
        state["clone"] = clone(profiles["petr"])
        rearm()

        async def undelivered(name, cmd, timeout=20):
            return {"error": "no live session"}
        agent.session_json = undelivered
        state["server_down"] = True
        got = send(_caller="olga", force=True)
        state["server_down"] = False
        path = os.path.join(state["clone"], lease.FILE)
        c.check("574 confirmed failure: the lease and the identity roll back together",
                not (not got.get("error") or os.path.exists(path)
                     or ident(state["clone"]) != profiles["petr"]),
                f"reply {got!r}, lease {os.path.exists(path)}, "
                f"identity {ident(state['clone'])}")

        # 6. транспортно-неопределённый исход (тело молчит) -- без отката:
        # сообщение могло доехать, и откат испортил бы чужой ход. Аренда и
        # identity остаются за тем, чей send молчит (его же подготовка),
        # слово timeout уходит с отказом.
        state["clone"] = clone(profiles["petr"])
        rearm()
        agent.session_json = deliver
        got = send(_caller="olga", force=True)
        path = os.path.join(state["clone"], lease.FILE)

        async def silent_body(name, cmd, timeout=20):
            return {"error": "pu-mop-1: the body did not answer in 20s", "timeout": True}
        agent.session_json = silent_body
        got = send(_caller="petr", force=True)
        c.check("574 transport-indeterminate: no rollback of lease or identity",
                not (not got.get("error") or not got.get("timeout")
                     or ident(state["clone"]) != profiles["petr"]
                     or not os.path.exists(path)),
                f"reply {got!r}, identity {ident(state['clone'])}, "
                f"lease {os.path.exists(path)}")

        # 7. takeover не перетирается чужим откатом: olga доставила (аренда и
        # identity её), petr с force провалился -- откат petr обязан вернуть
        # согласованную пару olga, включая хук стража.
        state["clone"] = clone(profiles["petr"])
        rearm()
        agent.session_json = deliver
        got = send(_caller="olga", force=True)
        c.check("574 takeover: the winner holds the lease and the identity",
                not (got.get("error") or ident(state["clone"]) != profiles["olga"]
                     or hooks(state["clone"]) != ["pre-commit", "pre-merge-commit"]),
                f"reply {got!r}, identity {ident(state['clone'])}, hooks {hooks(state['clone'])}")
        agent.session_json = undelivered
        got = send(_caller="petr", force=True)
        c.check("574 takeover: a failed challenger restores the winner",
                not (not got.get("error") or ident(state["clone"]) != profiles["olga"]
                     or hooks(state["clone"]) != ["pre-commit", "pre-merge-commit"]),
                f"reply {got!r}, identity {ident(state['clone'])}, hooks {hooks(state['clone'])}")

        # 8. self-declared caller: прежняя политика -- identity не трогаем,
        # сервер не спрашиваем, доставка без подготовки.
        state["clone"] = clone(profiles["petr"])
        rearm()
        asked.clear()
        agent.session_json = deliver
        got = send(owner="bob")
        c.check("574 self-declared: identity untouched, profile not asked",
                not (ident(state["clone"]) != profiles["petr"] or asked
                     or got.get("error")),
                f"identity {ident(state['clone'])}, asked {asked}, reply {got}")

        # 9. чужие хуки и core.hooksPath не перетираются и при восстановлении.
        own = "#!/bin/sh\n# the project's own\nexit 0\n"
        state["clone"] = clone(own_hook=own)
        rearm()
        agent.session_json = deliver
        got = send(_caller="olga")
        c.check("574 the project's hook survives install",
                not (got.get("error")
                     or open(os.path.join(state["clone"], ".git", "hooks",
                                          "pre-commit")).read() != own),
                f"reply {got!r}")
        agent.session_json = undelivered
        got = send(_caller="petr", force=True)
        c.check("574 the project's hook survives the restore",
                not (not got.get("error")
                     or open(os.path.join(state["clone"], ".git", "hooks",
                                          "pre-commit")).read() != own),
                f"reply {got!r}")
        d = clone()
        subprocess.run(["git", "-C", d, "config", "core.hooksPath", ".githooks"],
                       check=True)
        state["clone"] = d
        rearm()
        agent.session_json = deliver
        got = send(_caller="olga")
        c.check("574 core.hooksPath is not touched",
                not (got.get("error") or hooks(d)
                     or os.path.exists(os.path.join(d, ".githooks"))),
                f"reply {got!r}, hooks {hooks(d)}")
        del seen, waits

        # ── матрица review574: семь поведенческих отказов на 5f13fe2 ──────

        # R1. Перекрытие «A готовит, B берёт force, A продолжает» умерло
        # вместе с окном: захват -> identity -> передача -- одна
        # критическая секция (review-2 #574). Живой вариант гонки: A взял
        # владение, доставка транспортно-неопределённа (timeout, откатов
        # нет, захват жив); B(force) доставляет поверх; затем A «продолжил»
        # -- поздний подтверждённый провал A не может наступить (откат A
        # уже не приедет), но повторный send A поверх живой записи B
        # обязан видеть чужую аренду и отказываться, не стартуя тело под
        # чужой identity. Порядок -- события/последовательность, без
        # sleep-доказательств.
        state["clone"] = clone(profiles["petr"])
        rearm()
        at_handoff = {}

        async def ra_deliver(name, cmd, timeout=20):
            if "from-name a" in cmd:
                return {"error": "pu-mop-1: the body did not answer in 20s",
                        "timeout": True}
            at_handoff["b_ident"] = ident(state["clone"])
            return {"msg_id": "m-r1"}
        agent.session_json = ra_deliver

        async def ra():
            return await agent.v_send(None, {"name": "pu-mop-1", "message": "x",
                                             "_caller": "olga", "from_name": "a"})
        ra_out = asyncio.run(ra())

        async def rb():
            return await agent.v_send(None, {"name": "pu-mop-1", "message": "y",
                                            "_caller": "petr", "from_name": "b",
                                            "force": True})
        rb_out = asyncio.run(rb())

        async def ra_again():
            return await agent.v_send(None, {"name": "pu-mop-1", "message": "x2",
                                             "_caller": "olga", "from_name": "a"})
        ra2_out = asyncio.run(ra_again())
        path = os.path.join(state["clone"], lease.FILE)
        c.check("R1 a sender that lost the lease is refused, not delivered",
                not (not rb_out.get("msg_id")
                     or not ra_out.get("timeout")
                     or not ra2_out.get("error")
                     or "force" not in (ra2_out.get("error") or "")
                     or at_handoff.get("b_ident") != profiles["petr"]
                     or ident(state["clone"]) != profiles["petr"]
                     or open(path).read().strip() != "petr\t1000"),
                f"a {ra_out!r}, b {rb_out!r}, a2 {ra2_out!r}, "
                f"at handoff {at_handoff!r}, identity {ident(state['clone'])}")

        # R2. Fused shell не атомарен: name записался, email отказал. Откат
        # обязан вернуть фактическую прежнюю пару по валидному снимку, а не
        # «откатывать нечего»; причина неудачи отката едет с отказом (#181).
        state["clone"] = clone(profiles["petr"])
        rearm()

        def half_broken(clone, path, mine, profile):
            real = real_identity_script(clone, path, mine, profile)
            # точка отказа -- установка email: всё до неё исполнено
            return real.replace(
                f"config --local user.email {shlex.quote(profile['email'])}",
                "config --local user.email; false") if profile else real
        agent.identity_script = half_broken
        agent.session_json = deliver
        got = send(_caller="olga")
        agent.identity_script = real_identity_script
        c.check("R2 broken half-install rolls the prior pair back",
                not (not got.get("error")
                     or ident(state["clone"]) != profiles["petr"]),
                f"reply {got!r}, identity {ident(state['clone'])}")

        # R3. Профиль None + отказ записи (--unset-all под config.lock):
        # подготовка не подтверждена -- сообщение не доставляется, прежняя
        # identity остаётся, отказ называет причину, а не «clone has none».
        state["clone"] = clone(profiles["petr"])
        rearm()
        lock = os.path.join(state["clone"], ".git", "config.lock")
        open(lock, "w").close()
        sent[:] = []
        agent.session_json = deliver_spy
        got = send(_caller="ivan")
        os.remove(lock)
        c.check("R3 unset failure refuses before delivery",
                not (not got.get("error") or sent
                     or ident(state["clone"]) != profiles["petr"]
                     or "clone has none" in (got.get("owner_note") or "")),
                f"reply {got!r}, delivered {sent!r}, "
                f"identity {ident(state['clone'])}")

        # R4. Снимок хранит присутствие каждого ключа отдельно: локальное
        # имя без почты -- не «пары нет», после достоверной недоставки имя
        # обязано вернуться на место.
        state["clone"] = clone()
        subprocess.run(["git", "-C", state["clone"], "config", "--local",
                        "user.name", "Half Old"], check=True)
        rearm()
        agent.session_json = undelivered
        got = send(_caller="olga", force=True)
        c.check("R4 a half identity restores its present half",
                not (not got.get("error")
                     or ident(state["clone"]) != {"name": "Half Old"}),
                f"reply {got!r}, identity {ident(state['clone'])}")

        # R5. Свои guards входят в снимок: их не было до установки --
        # после достоверной недоставки их быть не должно; права/содержимое
        # прежних своих -- на месте. Диагностика не печатает содержимое.
        state["clone"] = clone(profiles["petr"])
        rearm()
        agent.session_json = undelivered
        got = send(_caller="olga", force=True)
        c.check("R5 restore leaves no guards that were absent before",
                not (not got.get("error") or hooks(state["clone"])),
                f"reply {got!r}, hooks {hooks(state['clone'])}")
        # ...и наоборот: свой guard был (с нашей меткой) -- вернулся на
        # место с прежним режимом доступа.
        state["clone"] = clone(profiles["petr"])
        hook_path = os.path.join(state["clone"], ".git", "hooks", "pre-commit")
        os.makedirs(os.path.dirname(hook_path), exist_ok=True)
        with open(hook_path, "w") as f:
            f.write(agent.owner_hook())
        os.chmod(hook_path, 0o700)
        rearm()
        agent.session_json = undelivered
        got = send(_caller="olga", force=True)
        c.check("R5 a prior own guard returns with its mode",
                not (not got.get("error")
                     or open(hook_path).read() != agent.owner_hook()
                     or (os.stat(hook_path).st_mode & 0o777) != 0o700),
                f"reply {got!r}, mode {oct(os.stat(hook_path).st_mode & 0o777)}")

        # R6. Причина неудачи ожидания простоя доезжает до вызывающего
        # через общую операцию и форматтер (проверка в tests/channel.py,
        # здесь -- транспорт: поле не теряется на выходе v_send).
        state["clone"] = clone(profiles["petr"])
        rearm()

        async def broken_idle(name, cmd, timeout=20):
            if "wait-idle" in cmd:
                return {"error": "idle observation failed"}
            return {"msg_id": "m-r6"}
        agent.session_json = broken_idle
        got = send(_caller="olga", wait=30)
        c.check("R6 an idle error keeps the delivery and names the reason",
                not (not got.get("msg_id") or not got.get("idle_error")),
                f"reply {got!r}")

        # R7. Разрыв транспорта (код 255 без итогового JSON) -- потеря
        # ответа, не доказанная недоставка: контекст остаётся за пославшим,
        # причина едёт с отказом. Откат только по структурированному
        # «доставлено не было».
        state["clone"] = clone(profiles["petr"])
        rearm()
        # сломанная R6 заглушка не годится: здесь путь идёт через настоящий
        # session_json над мусорным ответом bsh.
        agent.session_json = real_session_json
        calls_255 = {"n": 0}

        async def torn_transport(name, cmd, timeout=20):
            calls_255["n"] += 1
            return ("ssh: connect to host: Connection reset by peer", 255)
        # session_json не зовётся -- bsh отдаёт мусор; подменяем сам bsh
        # для команды send (printf-запись аренды и identity пусть работают).
        real_bsh = bash

        async def bsh_255(name, script, timeout=20):
            if "session.py send" in script:
                return await torn_transport(name, script, timeout)
            return await real_bsh(name, script, timeout)
        agent.bsh = bsh_255
        got = send(_caller="olga")
        agent.bsh = bash
        c.check("R7 a torn transport keeps the context, no rollback",
                not (not got.get("error")
                     or ident(state["clone"]) != profiles["olga"]
                     or not os.path.exists(os.path.join(state["clone"], lease.FILE))),
                f"reply {got!r}, identity {ident(state['clone'])}")

        # ── вторая матрица review574 (f61ca0c): четыре оставшихся отказа ──

        # S1. Окно «после проверки, до передачи» закрыто самим замком: A
        # задержан ВНУТРИ передачи (session.py стоит на первой инструкции --
        # ровно то, чем мастер вскрывал окно check-then-use: процесс
        # передачи остановлен на месте), B(force) тем временем входит в
        # свой v_send. Исход S1 старого кода: оба msg_id, A доставил под
        # petr-identity, поставленной B. Здесь A доставляется под olga, а
        # доставка B наблюдается строго после возврата A -- по событиям,
        # без sleep-доказательств.
        state["clone"] = clone(profiles["petr"])
        rearm()
        a_release = asyncio.Event()
        s1 = {"a_at_handoff": False, "b_started": False, "b_returned": False,
              "a_ident": None, "order": []}

        async def handoff_stall(name, cmd, timeout=20):
            if "session.py send" in cmd and "from-name a" in cmd:
                s1["a_at_handoff"] = True
                s1["a_ident"] = ident(state["clone"])
                await a_release.wait()
                s1["order"].append("a-delivered")
                return {"msg_id": "m-s1a"}
            if "session.py send" in cmd:
                s1["order"].append("b-delivered")
                return {"msg_id": "m-s1b"}
            return await real_session_json(name, cmd, timeout)
        agent.session_json = handoff_stall

        async def sa():
            got = await agent.v_send(None, {"name": "pu-mop-1", "message": "x",
                                            "_caller": "olga", "from_name": "a"})
            s1["order"].append("a-returned")
            return got

        async def sb():
            # B стартует строго после того, как A дошёл до передачи: окно,
            # которое раньше было дырой, теперь -- очередь на замок.
            while not s1["a_at_handoff"]:
                await asyncio.sleep(0.01)
            s1["b_started"] = True
            got = await agent.v_send(None, {"name": "pu-mop-1", "message": "y",
                                            "_caller": "petr", "from_name": "b",
                                            "force": True})
            s1["b_returned"] = True
            return got

        async def s1_race():
            # Один цикл: A входит в v_send и доходит до передачи; B —
            # строго после этого (окно, которое раньше было дырой), входит
            # в свой v_send и встаёт в очередь на замок; когда B доказуемо
            # начал (b_started) и отработал такт планировщика (sleep(0) —
            # уступка, не ожидание), A размыкают.
            ta = asyncio.ensure_future(sa())
            while not s1["a_at_handoff"]:
                await asyncio.sleep(0.01)
            tb = asyncio.ensure_future(sb())
            while not s1["b_started"]:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0)
            a_release.set()
            return await asyncio.gather(ta, tb)
        ra, rb = asyncio.run(s1_race())
        c.check("S1 the handoff inside the lock: A delivers under its own identity",
                not (s1["a_ident"] != profiles["olga"] or ra.get("error")
                     or rb.get("error")),
                f"a saw {s1['a_ident']!r}, a {ra!r}, b {rb!r}")
        # Позиция a-returned зависит от планировщика (замок отпускается
        # внутри v_send раньше возврата sa); инвариант -- доставка B строго
        # после доставки A: под чужой identity ничего не стартует.
        c.check("S1 B delivers only after A's handoff is done",
                not (s1["order"].index("b-delivered")
                     < s1["order"].index("a-delivered")
                     or ident(state["clone"]) != profiles["petr"]),
                f"order {s1['order']!r}, identity {ident(state['clone'])!r}")
        agent.session_json = real_session_json

        # S2. Частичный отказ установки возвращает точные bytes/режим/
        # присутствие каждого затронутого элемента и ПОДТВЕРЖДАЕТ возврат
        # сверкой (review-2, п.2). До отказа: собственный guard-хук агента
        # (с меткой, но с дополнительной строкой -- байты отличаются от
        # свежеустанавливаемого) с mode 0700; pre-merge-commit.mop -- КАТАЛОГ,
        # поэтому установка второго хука падает ПОСЛЕ перезаписи первого.
        # Старый код возвращал O-хук одним chmod: после отказа pre-commit
        # уже новый (лишь режим старый). Код 1 -- компенсация подтверждена:
        # откат аренды едет с той же причиной.
        state["clone"] = clone()
        prior_hook = agent.owner_hook() + "# the owner's extension\n"
        hook_path = os.path.join(state["clone"], ".git", "hooks", "pre-commit")
        os.makedirs(os.path.dirname(hook_path), exist_ok=True)
        with open(hook_path, "w") as f:
            f.write(prior_hook)
        os.chmod(hook_path, 0o700)
        os.makedirs(os.path.join(state["clone"], ".git", "hooks",
                                 "pre-merge-commit.mop"), exist_ok=True)
        rearm()
        sent[:] = []
        agent.session_json = deliver_spy
        got = send(_caller="olga")
        after_mode = os.stat(hook_path).st_mode & 0o777
        c.check("S2 a partial install failure restores the prior hook exactly",
                not (not got.get("error") or sent
                     or open(hook_path).read() != prior_hook
                     or after_mode != 0o700),
                f"reply {got!r}, delivered {sent!r}, "
                f"hook {open(hook_path).read()!r}, mode {oct(after_mode)}")
        c.check("S2 the compensated failure rolls the lease back with the reason",
                not ("git identity not set" not in (got.get("error") or "")
                     or os.path.exists(os.path.join(state["clone"], lease.FILE))),
                f"reply {got!r}, "
                f"lease {os.path.exists(os.path.join(state['clone'], lease.FILE))}")

        # S2b. Компенсация не сошлась (код 2): email setter пишет значение,
        # создаёт config.lock и отказывает -- откатить запись уже нечем
        # (запись под замком не проходит), сверка видит чужое значение.
        # Аренда НЕ возвращается прежнему владельцу как будто пара
        # восстановлена: остаётся за пославшим, обе причины едут мастеру.
        state["clone"] = clone(profiles["petr"])
        rearm()
        sent[:] = []
        lock = os.path.join(state["clone"], ".git", "config.lock")

        def lock_after_email(clone, path, mine, profile):
            real = real_identity_script(clone, path, mine, profile)
            # email пишется, затем возникает config.lock и команда
            # «отказывает»: точка отказа ПОСЛЕ состоявшейся записи.
            return real.replace(
                f"config --local user.email {shlex.quote(profile['email'])}",
                f"config --local user.email {shlex.quote(profile['email'])} "
                f"&& touch {shlex.quote(lock)}; false") if profile else real
        agent.identity_script = lock_after_email
        agent.session_json = deliver_spy
        got = send(_caller="olga", force=True)
        agent.identity_script = real_identity_script
        os.path.exists(lock) and os.remove(lock)
        c.check("S2b an unconfirmed compensation keeps the lease with the sender",
                not (not got.get("error")
                     or "not confirmed restored" not in got["error"]
                     or not os.path.exists(os.path.join(state["clone"], lease.FILE))
                     or sent),
                f"reply {got!r}, delivered {sent!r}, "
                f"lease {os.path.exists(os.path.join(state['clone'], lease.FILE))}")

        # S3. Присутствие отделено от значения (review-2, п.3): literal
        # user.name='-' -- законное значение, после подтверждённой
        # недоставки оно обязано вернуться; '-' не сентинел. Нечитаемый
        # конфиг -- операционный отказ (X), а не «ключа нет»: установка
        # отказывает ДО записи, значение не превращается в отсутствие.
        state["clone"] = clone()
        subprocess.run(["git", "-C", state["clone"], "config", "--local",
                        "user.name", "-"], check=True)
        subprocess.run(["git", "-C", state["clone"], "config", "--local",
                        "user.email", "old@example.dev"], check=True)
        rearm()
        agent.session_json = undelivered
        got = send(_caller="olga", force=True)
        c.check("S3 a literal '-' name survives a failed delivery",
                not (not got.get("error")
                     or ident(state["clone"]) != {"name": "-",
                                                  "email": "old@example.dev"}),
                f"reply {got!r}, identity {ident(state['clone'])}")
        state["clone"] = clone(profiles["petr"])
        rearm()
        sent[:] = []
        agent.session_json = deliver_spy
        config_path = os.path.join(state["clone"], ".git", "config")
        os.chmod(config_path, 0)
        got = send(_caller="olga", force=True)
        os.chmod(config_path, 0o644)
        c.check("S3 an unreadable config is a read error, not absence",
                not (not got.get("error") or sent
                     or ident(state["clone"]) != profiles["petr"]),
                f"reply {got!r}, delivered {sent!r}, "
                f"identity {ident(state['clone'])}")

        # S4. Многострочные значения не вытекают в диагностику (review-2,
        # п.4): прежний name='Old\nprivate_snapshot_574', отказ setter'а
        # email -- в ошибке нет ни второй строки значения, ни тел хуков:
        # кадр метки вырезается целиком, значение закодировано base64.
        state["clone"] = clone()
        subprocess.run(["git", "-C", state["clone"], "config", "--local",
                        "user.name", "Old\nprivate_snapshot_574"], check=True)
        rearm()
        sent[:] = []

        def broken_email(clone, path, mine, profile):
            real = real_identity_script(clone, path, mine, profile)
            return real.replace(
                f"config --local user.email {shlex.quote(profile['email'])}",
                "config --local user.email; false") if profile else real
        agent.identity_script = broken_email
        agent.session_json = deliver_spy
        got = send(_caller="olga")
        agent.identity_script = real_identity_script
        c.check("S4 multiline values never reach the refusal",
                not (not got.get("error") or sent
                     or "private_snapshot_574" in got["error"]
                     or "Old" in got["error"]),
                f"reply {got!r}, delivered {sent!r}")

        # ── третья матрица review574 (218dd00): границы I/O и квитанций ──

        # T1. Документ отсутствия ключа -- код 1 getter'а; любой другой
        # код -- unknown/ошибка ДО записи. rc128 с пустым stderr -- фатал,
        # а не «ключа нет»: пустой stderr не доказательство отсутствия.
        state["clone"] = clone(profiles["petr"])
        rearm()
        sent[:] = []
        agent.session_json = deliver_spy
        # «Фатальный» git: обёртка, умирающая 128 МОЛЧА на чтении имени.
        # Вызов чтения -- «git -C <клон> config --local user.name»:
        # $3=config, $5=user.name, аргументов ровно пять (setter несёт
        # шестой -- значение). Все git-вызовы идут через обёртку: читающее
        # имя умирает, остальное -- честный git.
        wrap = os.path.join(root, "git-fatal-128")
        with open(wrap, "w") as f:
            f.write("#!/bin/sh\n"
                    '[ "$3" = config ] && [ "$5" = user.name ] '
                    "&& [ $# -eq 5 ] && exit 128\n"
                    "exec git \"$@\"\n")
        os.chmod(wrap, 0o755)

        def fatal_read(clone, path, mine, profile):
            real = real_identity_script(clone, path, mine, profile)
            return real.replace("git -C", f"{wrap} -C")
        agent.identity_script = fatal_read
        got = send(_caller="olga")
        agent.identity_script = real_identity_script
        c.check("T1 a read error without stderr is not absence",
                not (not got.get("error")
                     or "read failed" not in got["error"]
                     or sent
                     or ident(state["clone"]) != profiles["petr"]),
                f"reply {got!r}, delivered {sent!r}, "
                f"identity {ident(state['clone'])}")

        # T2. Код 0 без полного снимка: установка реально исполнилась
        # (identity уже New), кадры потеряны на границе. Это не
        # «безопасный отказ»: прежняя пара неизвестна, вслепую аренду
        # прежнему владельцу не отдают -- контекст остаётся за пославшим,
        # причина называет снимок.
        state["clone"] = clone(profiles["petr"])
        rearm()
        sent[:] = []
        agent.session_json = deliver_spy

        async def frame_eater(name, script, timeout=20):
            out, code = await bash(name, script, timeout)
            if code == 0 and "mop574-name" in out:
                # Настоящая установка прошла; из ответа кадры исчезли --
                # ровно потеря кадров на границе транспорта.
                out = "\n".join(l for l in out.splitlines()
                                 if not l.startswith("mop574-"))
            return out, code
        agent.bsh = frame_eater
        got = send(_caller="olga")
        agent.bsh = bash
        c.check("T2 code0 without a full snapshot keeps the context with the sender",
                not (not got.get("error")
                     or "snapshot" not in got["error"]
                     or not os.path.exists(os.path.join(state["clone"], lease.FILE))
                     or ident(state["clone"]) != profiles["olga"]
                     or sent),
                f"reply {got!r}, delivered {sent!r}, "
                f"lease {os.path.exists(os.path.join(state['clone'], lease.FILE))}, "
                f"identity {ident(state['clone'])}")

        # T3. Откат доставки подтверждает свои изменения и несёт причину:
        # созданный при установке guard (A-хук до установки не было)
        # неудаляем -- откат обязан отказаться, а не «exit 0»: lease не
        # возвращается вслепую, причина едет мастеру, хуки на месте
        # (разбирать оператору, не молча бросать).
        state["clone"] = clone()
        rearm()
        agent.session_json = undelivered

        # Надёжнее файловой подменой: каталог hooks -- только чтение.
        # restore-скрипт узнаём по отсутствию кадров снимка: установка
        # всегда печатает mop574-метки, откат их не печатает никогда
        # (оба начинаются охраной файла аренды и оба зовут rev-parse).
        async def ro_hooks(name, script, timeout=20):
            if script.startswith("[ \"$(cat") and "printf 'mop574" not in script:
                d = os.path.join(state["clone"], ".git", "hooks")
                os.chmod(d, 0o500)
                try:
                    return await bash(name, script, timeout)
                finally:
                    os.chmod(d, 0o755)
            return await bash(name, script, timeout)
        agent.bsh = ro_hooks
        got = send(_caller="olga", force=True)
        agent.bsh = bash
        c.check("T3 a failed guard removal fails the rollback loudly",
                not (not got.get("error")
                     or "not restored" not in (got.get("error") or "")
                     or not os.path.exists(os.path.join(state["clone"], lease.FILE))
                     or hooks(state["clone"]) != ["pre-commit", "pre-merge-commit"]),
                f"reply {got!r}, lease "
                f"{os.path.exists(os.path.join(state['clone'], lease.FILE))}, "
                f"hooks {hooks(state['clone'])}")

        # T4. Временный файл stderr не остаётся:mktemp установки убирает
        # свой файл trap'ом на каждом выходе. Проверка изолирована: свой
        # приватный TMPDIR (mktemp установки пишет туда), а СТАРЫЙ
        # hardcoded-путь /tmp/mop574-err.$$ подменяется в тексте скрипта
        # туда же -- контролируемый redirect в фикстуру, не уборка чужих
        # файлов из общего /tmp. Чужой sentinel с тем же префиксом стоит
        # в той же папке: проверка обязана его не тронуть и не счесть
        # своим остатком. Остатки этой вызванной установки -- ровно всё,
        # что в фикстуре сверх sentinel.
        t4dir = tempfile.mkdtemp(prefix="mop574-isolated-")
        sentinel = os.path.join(t4dir, "mop574.foreign-sentinel")
        with open(sentinel, "w") as f:
            f.write("another process's scratch\n")

        async def isolated_bsh(name, script, timeout=20):
            run = script.replace("/tmp/mop574-err.$$",
                                 os.path.join(t4dir, "mop574-err.$$"))
            r = subprocess.run(["bash", "-c", run], capture_output=True,
                               text=True, env={**os.environ, "TMPDIR": t4dir})
            return r.stdout + r.stderr, r.returncode
        state["clone"] = clone(profiles["petr"])
        rearm()
        agent.session_json = deliver_spy
        agent.bsh = isolated_bsh
        got = send(_caller="olga")
        agent.bsh = bash
        leftovers = sorted(f for f in os.listdir(t4dir)
                           if f != os.path.basename(sentinel))
        c.check("T4 the install leaves no temp file behind",
                not leftovers,
                f"reply {got!r}, leftovers {leftovers!r}")
        c.check("T4 a foreign temp file survives the check",
                open(sentinel).read() == "another process's scratch\n",
                sentinel)

        # T5. Обязательное чтение каталога хуков тоже fail closed
        # (review-4): _hooks_var глотал rc rev-parse --absolute-git-dir и
        # подставлял /hooks -- «каталога нет». Обёртка git отказывает
        # ТОЛЬКО ПЕРВОМУ process-wide вызову rev-parse (метка-файл):
        # lookup установки падает, повторный lookup отката -- успешен.
        # Установка принимает неизвестный каталог за отсутствие хуков
        # (ложный A в снимке) и доставку пытается выполнить; откат затем
        # УДАЛЯЕТ исходный собственный хук. Требования: доставка не
        # начинается при неизвестном каталоге; исходный хук остаётся.
        own_hook = agent.owner_hook() + "# the project's own line\n"
        state["clone"] = clone(own_hook=own_hook)
        rearm()
        sent[:] = []
        agent.session_json = undelivered
        hook_path = os.path.join(state["clone"], ".git", "hooks", "pre-commit")
        mark = os.path.join(root, "git-lookup-failed-once")
        wrap = os.path.join(root, "git-lookup-once")
        # Вызов -- «git -C <клон> rev-parse --absolute-git-dir»:
        # $3=rev-parse, $4=--absolute-git-dir.
        with open(wrap, "w") as f:
            f.write("#!/bin/sh\n"
                    f'if [ "$3" = rev-parse ] && [ "$4" = --absolute-git-dir ] '
                    f'&& [ ! -f {shlex.quote(mark)} ]; then\n'
                    f": > {shlex.quote(mark)}\n"
                    "exit 128\n"
                    "fi\n"
                    "exec git \"$@\"\n")
        os.chmod(wrap, 0o755)

        def lookup_once(clone, path, mine, profile):
            real = real_identity_script(clone, path, mine, profile)
            return real.replace("git -C", f"{wrap} -C")
        agent.identity_script = lookup_once
        got = send(_caller="olga", force=True)
        agent.identity_script = real_identity_script
        hook_now = (open(hook_path).read()
                    if os.path.exists(hook_path) else None)
        c.check("T5 an unknown hooks directory stops the delivery",
                not (not got.get("error")
                     or "hooks" not in got["error"]
                     or sent
                     or hook_now != own_hook),
                f"reply {got!r}, delivered {sent!r}, hook {hook_now!r}")
        # Исходный хук обязан остаться и после отката подтверждённой
        # недоставки: установки не было, ложного A в реестре нет, снимать
        # нечего. Аренда возвращается прежнему владельцу (код 1: до
        # неизвестного каталога ни одной записи не случилось).
        c.check("T5 the project's own hook survives the failed install",
                not (hook_now != own_hook
                     or os.path.exists(os.path.join(state["clone"], lease.FILE))),
                f"hook {hook_now!r}, "
                f"lease {os.path.exists(os.path.join(state['clone'], lease.FILE))}")

        # T5b (review-5): текст stderr «not a git repository» -- НЕ документ
        # отсутствия хуков. Тот же сценарий first-fails/second-succeeds, но
        # обёртка отказывает ПЕРВОМУ rev-parse --absolute-git-dir с ЭТИМ
        # stderr (rc 128): эвристика по тексту принимала отказ за «каталога
        # нет», ложный A шёл в доставку, а откат со вторым, успешным lookup
        # УДАЛЯЛ исходный собственный хук. Требования те же: доставка не
        # начинается, исходный хук остаётся, аренда возвращается (до отказа
        # ни одной записи не было).
        own_hook = agent.owner_hook() + "# the project's own line\n"
        state["clone"] = clone(own_hook=own_hook)
        rearm()
        agent.session_json = undelivered
        hook_path = os.path.join(state["clone"], ".git", "hooks", "pre-commit")
        mark = os.path.join(root, "gitdir-stderr-once")
        wrap = os.path.join(root, "git-stderr-once")
        with open(wrap, "w") as f:
            f.write("#!/bin/sh\n"
                    f'if [ "$3" = rev-parse ] && [ "$4" = --absolute-git-dir ] '
                    f'&& [ ! -f {shlex.quote(mark)} ]; then\n'
                    f": > {shlex.quote(mark)}\n"
                    'echo "fatal: not a git repository (or any of the parent '
                    'directories): .git" >&2\n'
                    "exit 128\n"
                    "fi\n"
                    "exec git \"$@\"\n")
        os.chmod(wrap, 0o755)

        def stderr_once(clone, path, mine, profile):
            real = real_identity_script(clone, path, mine, profile)
            return real.replace("git -C", f"{wrap} -C")
        agent.identity_script = stderr_once
        got = send(_caller="olga", force=True)
        agent.identity_script = real_identity_script
        hook_now = (open(hook_path).read()
                    if os.path.exists(hook_path) else None)
        c.check("T5b stderr text is not hook absence: no delivery, the hook survives",
                not (not got.get("error")
                     or "hooks directory lookup failed" not in got["error"]
                     or hook_now != own_hook
                     or os.path.exists(os.path.join(state["clone"], lease.FILE))),
                f"reply {got!r}, hook {hook_now!r}, "
                f"lease {os.path.exists(os.path.join(state['clone'], lease.FILE))}")

        # T6 (review-5): ворота снимка -- ПОСЛЕ всех обязательных чтений и
        # ДО первой записи. Отказ stat'а первого хука (X) в старом порядке
        # замечался воротами, стоявшими ДО snap_hooks: identity уже была
        # перезаписана, собственный хук уже заменён свежим стражем, и
        # Python видел неполный снимок лишь по exit 0 -- прежние байты
        # терялись. Обёртка валит РОВНО ПЕРВЫЙ process-wide stat
        # (метка-файл): это чтение режима pre-commit в snap_hooks.
        prior_hook = agent.owner_hook() + "# the owner's line\n"
        state["clone"] = clone(profiles["petr"], own_hook=prior_hook)
        rearm()
        sent[:] = []
        agent.session_json = deliver_spy
        hook_path = os.path.join(state["clone"], ".git", "hooks", "pre-commit")
        mark = os.path.join(root, "stat-failed-once")
        wrap = os.path.join(root, "stat-once-bin")
        with open(wrap, "w") as f:
            f.write("#!/bin/sh\n"
                    f"if [ ! -f {shlex.quote(mark)} ]; then\n"
                    f": > {shlex.quote(mark)}\n"
                    "exit 1\n"
                    "fi\n"
                    'exec stat "$@"\n')
        os.chmod(wrap, 0o755)

        def stat_once(clone, path, mine, profile):
            real = real_identity_script(clone, path, mine, profile)
            return real.replace("stat -c", f"{wrap} -c")
        agent.identity_script = stat_once
        got = send(_caller="olga")
        agent.identity_script = real_identity_script
        hook_now = (open(hook_path).read()
                    if os.path.exists(hook_path) else None)
        c.check("T6 a hook read failure stops the install before any mutation",
                not (not got.get("error")
                     or "hook snapshot read failed" not in got["error"]
                     or sent
                     or hook_now != prior_hook
                     or ident(state["clone"]) != profiles["petr"]
                     or os.path.exists(os.path.join(state["clone"], lease.FILE))),
                f"reply {got!r}, delivered {sent!r}, hook {hook_now!r}, "
                f"identity {ident(state['clone'])!r}, "
                f"lease {os.path.exists(os.path.join(state['clone'], lease.FILE))}")



# ── факт state: исход хода едет мастеру (#224) ───────────────────────────
# HYPOTHESIS: исход хода (StopFailure и код ошибки) пишут хуки в запись,
# которую отдаёт `session.py state` (#222), а агент звал только probe и
# отдавал мастеру строку "<status> <alive> <listen>" — записи хода мастер не
# видел, и провал хода посреди работы читался «idle».
# SOLUTION: facts зовёт state (один процесс вместо probe) и шлёт разобранный
# словарь новым ключом state, а строку session собирает из него же — мастер со
# старой библиотекой читает только session. Старый session.py на узле (state
# не отвечает) — откат на probe ровно как раньше, без ключа state.
# STATUS: FIXED — see #224
def check_state_fact_224(c):
    import asyncio
    import json
    rec = {"status": "idle", "waitingFor": None, "alive": True, "listen": False,
           "turn": {"event": "StopFailure", "at": 1790245436,
                    "error": "authentication_failed", "detail": "Login expired"}}
    none = {"status": None, "waitingFor": None, "alive": False, "listen": False, "turn": None}
    calls = []

    # Своя заглушка: отвечает по глаголу session.py, а не одним ответом.
    def fake(answers):
        async def bsh(name, script, timeout=20):
            verb = script.split()[2]
            calls.append(verb)
            return answers.get(verb, ("", 1))
        return bsh

    async def alive(name):
        return True

    async def clone(name):
        return {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
    with restored(agent, "bsh", "clone_facts", "tmux_alive"):
        agent.clone_facts, agent.tmux_alive = clone, alive
        for what, answers, want_session, want_state, want_calls in (
                ("new session.py", {"state": ("banner\n" + json.dumps(rec), 0)},
                 "idle 1 0", rec, ["state"]),
                ("no session", {"state": (json.dumps(none), 0)}, "none", none, ["state"]),
                ("old session.py: state is an unknown verb",
                 {"state": ("usage: session.py ...", 2), "probe": ("busy 1 1", 0)},
                 "busy 1 1", None, ["state", "probe"]),
                # Таймаут -- тело не ответило, и probe ждал бы те же 20 секунд
                # впустую: ответ "none", как у таймаута probe до #224.
                ("state timed out", {"state": ("", None)}, "none", None, ["state"])):
            calls.clear()
            agent.bsh = fake(answers)
            got = asyncio.run(agent.facts("pu-mop-1"))
            c.check(f"facts, {what}",
                    not (got.get("session") != want_session or got.get("state") != want_state
                         or ("state" in got) != (want_state is not None) or calls != want_calls),
                    f"{got!r}, calls {calls}")


# ── факты без экрана (#236) ──────────────────────────────────────────────
# HYPOTHESIS: после #235 вердикт facts["screen"] не читает, а агент всё равно
# снимает пейн (capture-pane) у каждого папета на каждом опросе ростера — в
# теле pve это лишний заход в тело и 20 строк по шине. Держали его на переход
# для мастеров со старой библиотекой; все они теперь не старше b0aca2c.
# SOLUTION: facts не снимает экран и не шлёт ключ screen; вердикт по тем же
# фактам прежний. tail/slash/attach снимают пейн своими глаголами.
# STATUS: FIXED — see #236
def check_no_screen_fact_236(c):
    import asyncio
    import json
    from mop.common.state import verdict
    scripts = []
    rec = {"status": "idle", "waitingFor": None, "alive": True, "listen": True,
           "turn": {"event": "Stop", "at": 1790245436, "error": None, "detail": None}}

    async def bsh(name, script, timeout=20):
        scripts.append(script)
        if "has-session" in script:
            return "", 0
        if "capture-pane" in script:
            return "Herding bytes", 0
        return json.dumps(rec), 0

    async def clone(name):
        return {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
    with restored(agent, "bsh", "clone_facts"):
        agent.bsh, agent.clone_facts = bsh, clone
        got = asyncio.run(agent.facts("pu-mop-1"))
        c.check("facts must carry no screen key", not ("screen" in got), repr(got))
        c.check("facts must not capture the pane",
                not (any("capture-pane" in s for s in scripts)), repr(scripts))
        with_screen = {**got, "screen": "Not logged in · Run /login"}
        c.check("verdict must not change",
                not (str(verdict(got)) != "free (master)"
                     or str(verdict(with_screen)) != str(verdict(got))),
                f"{verdict(got)} vs {verdict(with_screen)}")


# ── расход по логину в глаголе usage (#244) ───────────────────────────────
# HYPOTHESIS: глагол usage отдаёт расход по папету и дню, но не по логину
# мастера — оператор не видит, кто сколько тратит.
# SOLUTION: usage.py зовётся с --by-login, агент кладёт рядом с прежним полем
# usage новое by_login: {папет: {логин: {дата: {вид: n}}}}; usage не
# меняется. Старый usage.py в теле флага не знает и печатает прежнюю форму —
# тогда весь расход папета за «-»: инвариант (сумма по логинам == usage)
# держится, а приписать его некому.
# STATUS: FIXED — see #244
def check_usage_by_login_244(c):
    import asyncio
    import json
    row = {"input": 1, "output": 2, "cache_write": 3, "cache_read": 4}
    new = {"usage": {"2026-09-24": row},
           "by_login": {"anton": {"2026-09-24": row}}}
    answers = {"pu-mop-1": (json.dumps(new), 0),
               "pu-mop-2": (json.dumps({"2026-09-24": row}), 0),   # старый usage.py
               "pu-mop-3": ("boom", 1)}
    scripts = []

    async def bsh(name, script, timeout=20):
        scripts.append(script)
        return answers[name]

    class Driver:
        SESSION_PY = "/x/mop/session.py"

        async def bodies(self):
            return list(answers)

        def projects_dir(self, name):
            return "/p"

    async def yes(*a, **k):
        return True
    with restored(agent, "bsh", "DRIVER", "_mine"):
        agent.bsh, agent.DRIVER, agent._mine = bsh, Driver(), yes
        got = asyncio.run(agent.v_usage(None, {"days": 7}))
    want_usage = {"pu-mop-1": {"2026-09-24": row}, "pu-mop-2": {"2026-09-24": row},
                  "pu-mop-3": {}}
    want_by = {"pu-mop-1": {"anton": {"2026-09-24": row}},
               "pu-mop-2": {"-": {"2026-09-24": row}}, "pu-mop-3": {}}
    c.expect("usage must stay as it was", got.get("usage"), want_usage)
    c.expect("by_login", got.get("by_login"), want_by)
    c.check("usage.py must be asked --by-login",
            not (not all(s.endswith(" 7 --by-login") for s in scripts)), repr(scripts))


def check_owner_gate_267(c):
    """HYPOTHESIS (#267): ворота владения агента (_gate) -- вторая копия
    cluster.gate: свой вызов lease.may_touch, своя сборка отказа, своя
    owner_note. SOLUTION: обе стороны -- lease.gate и lease.noted; агент
    держит только своё: факты клона из тела и оператора по субъекту.
    Проверка: по той же таблице, что tests/cluster.py, агент отвечает ровно
    lease.gate. STATUS: FIXED — see #267"""
    import asyncio
    from mop.common import lease
    gate = getattr(lease, "gate", None)
    if not c.check("lease.gate exists (else the gate is written on each side)",
                   not (gate is None)):
        return
    with restored(agent, "clone_facts"), restored(agent.time, "time"):
        agent.time.time = lambda: GATE_NOW
        for name, clone, caller, force in gate_table_267():
            async def facts(_n, clone=clone):
                return clone.to_dict()
            agent.clone_facts = facts
            for project, operator in (("mop", False), (busnames.ADMIN, True)):
                req = {"name": name, "_project": project,
                       **({"_caller": caller} if caller else {}),
                       **({"force": True} if force else {})}
                got = asyncio.run(agent._gate(name, req))
                want = gate(name, clone, caller, GATE_NOW, force, operator)
                c.expect(f"agent._gate {caller} over {clone.owner} (force {force}, "
                         f"{project}) vs lease.gate", got, want)
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    src = open(os.path.join(root, "mop", "node", "agent.py")).read()
    c.check("agent.py must gate through lease.gate and note through lease.noted",
            not ("lease.may_touch(" in src or "lease.gate(" not in src
                 or "lease.noted(" not in src))


def check_junk_without_templates_276(c):
    """HYPOTHESIS (#276): v_junk звал DRIVER.templates() у любого драйвера, и
    host держал пустой глагол только ради этого вызова. SOLUTION: глагол
    гипервизора берётся через driver.hypervisor_verb; драйвер без него
    отвечает прежним [] -- ответ по шине тот же. STATUS: FIXED — see #276"""
    import asyncio
    import types

    async def bodies():
        return ["pu-mop-1"]

    async def facts(_n):
        return {}
    bare = types.SimpleNamespace(bodies=bodies)
    with restored(agent, "DRIVER", "clone_facts", "node_name"):
        agent.DRIVER, agent.clone_facts = bare, facts
        agent.node_name = lambda: "n1"
        try:
            got = asyncio.run(agent.v_junk(None, {}))
        except AttributeError as e:
            got = {"raised": str(e)}
    c.expect("junk from a driver without templates: the old reply",
             {k: got.get(k) for k in ("bodies", "work", "templates", "raised")},
             {"bodies": ["pu-mop-1"], "work": {}, "templates": [], "raised": None})


def check_owner_hook_313(c):
    """HYPOTHESIS (#313): агент ставит в клон identity владельца (#167), но
    `git -c user.name=… -c user.email=…` старше .git/config, и ничто этому
    не мешает: 27.09 коммиты #307 ушли как Claude <noreply@anthropic.com>,
    #308/#310 -- как anton@ermak.us, в master 46 коммитов Claude с 25.09;
    сам mop советовал «commit with git -c».
    SOLUTION: вместе с identity агент ставит в клон хуки pre-commit и
    pre-merge-commit (чистое слияние pre-commit не зовёт): есть локальная
    почта клона (`git config --local`, -c её не видит), а у автора или
    коммитера (`git var`, видит -c) другая -- отказ с подсказкой. Нет
    локальной identity -- пропуск: владелец без профиля коммитит с -c.
    Хук помечен второй строкой (первая -- shebang); чужой хук без метки не
    перезаписывается, и заметка это говорит; снятие identity снимает только
    свои хуки. STATUS: FIXED — see #313

    git и bash настоящие: хук ставит identity_script во временный клон, и
    решает он на настоящих коммитах и слиянии."""
    import asyncio
    import subprocess
    import tempfile
    hook = getattr(agent, "owner_hook", None)
    if hook is None:
        c.fail("#313 no agent.owner_hook: the owner's identity is not enforced")
        return
    text = hook()
    c.expect("#313 hook: shebang first, the marker second",
             text.splitlines()[:2], ["#!/bin/sh", agent.OWNER_GUARD])
    root = tempfile.mkdtemp(prefix="mop-test-313-")
    olga = {"name": "Ольга Петрова", "email": "olga@example.dev"}

    def repo(own_hook=None, hooks_path=None):
        d = tempfile.mkdtemp(dir=root)
        subprocess.run(["git", "init", "-q", "-b", "master", d], check=True)
        if own_hook:
            with open(os.path.join(d, ".git", "hooks", "pre-commit"), "w") as f:
                f.write(own_hook)
            os.chmod(os.path.join(d, ".git", "hooks", "pre-commit"), 0o755)
        if hooks_path:
            subprocess.run(["git", "-C", d, "config", "core.hooksPath", hooks_path], check=True)
        lease_file = os.path.join(d, ".git", "mop-owner")
        with open(lease_file, "w") as f:
            f.write("olga\t1\n")
        return d, lease_file

    def install(d, lease_file, profile):
        r = subprocess.run(["bash", "-c", agent.identity_script(d, lease_file, "olga\t1\n",
                                                                profile)],
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def git(d, *args):
        r = subprocess.run(["git", "-C", d, *args], capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def hooks(d):
        h = os.path.join(d, ".git", "hooks")
        return sorted(f for f in os.listdir(h) if not f.endswith(".sample"))

    d, lf = repo()
    code, out = install(d, lf, olga)
    c.expect("#313 install: both hooks in the clone", (code, hooks(d)),
             (0, ["pre-commit", "pre-merge-commit"]))
    code, out = git(d, "commit", "-q", "--allow-empty", "-m", "plain")
    c.expect("#313 a plain commit (the owner's identity) passes", (code, out), (0, ""))
    code, out = git(d, "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com",
                    "commit", "-q", "--allow-empty", "-m", "forged")
    c.check("#313 git -c user.* is refused, naming the owner",
            code != 0 and "drop -c user.*" in out and "olga@example.dev" in out, (code, out))
    code, out = git(d, "-c", "user.email=olga@example.dev", "commit", "-q", "--allow-empty",
                    "-m", "same")
    c.expect("#313 -c with the owner's own email passes", (code, out), (0, ""))
    # Слияние без конфликтов pre-commit не зовёт: его держит pre-merge-commit.
    git(d, "checkout", "-q", "-b", "side")
    git(d, "commit", "-q", "--allow-empty", "-m", "side")
    git(d, "checkout", "-q", "master")
    code, out = git(d, "-c", "user.email=noreply@anthropic.com", "merge", "-q", "--no-ff",
                    "side", "-m", "forged merge")
    c.check("#313 a merge with -c user.* is refused", code != 0 and "drop -c user.*" in out,
            (code, out))
    git(d, "merge", "--abort")
    code, out = git(d, "merge", "-q", "--no-ff", "side", "-m", "merge")
    c.expect("#313 a plain merge passes", (code, out), (0, ""))

    # Снятие identity снимает свои хуки; без локальной почты -c снова можно.
    code, out = install(d, lf, None)
    c.expect("#313 unset: our hooks are gone", (code, hooks(d)), (0, []))
    code, out = git(d, "-c", "user.name=X", "-c", "user.email=x@y.z", "commit", "-q",
                    "--allow-empty", "-m", "no owner")
    c.expect("#313 no local identity: git -c passes (owner without a profile)", (code, out), (0, ""))

    # Свой хук проекта не перезаписывается, и заметка это говорит.
    own = "#!/bin/sh\n# the project's own\nexit 0\n"
    d, lf = repo(own_hook=own)
    code, out = install(d, lf, olga)
    c.expect("#313 a project's pre-commit is kept",
             open(os.path.join(d, ".git", "hooks", "pre-commit")).read(), own)
    c.check("#313 the skip is said", code == 0 and "pre-commit" in out and "skipped" in out,
            (code, out))
    c.expect("#313 the merge hook still goes in", hooks(d), ["pre-commit", "pre-merge-commit"])
    code, out = install(d, lf, None)
    c.expect("#313 unset keeps the project's hook", hooks(d), ["pre-commit"])

    # core.hooksPath: .git/hooks git не читает -- ставить туда незачем, сказать.
    d, lf = repo(hooks_path=".githooks")
    code, out = install(d, lf, olga)
    c.check("#313 core.hooksPath: nothing written, the skip is said",
            code == 0 and hooks(d) == [] and "hooksPath" in out
            and not os.path.exists(os.path.join(d, ".githooks")), (code, out, hooks(d)))

    # Заметка мастеру называет пропуск.
    d, lf = repo(own_hook=own)

    async def profile(conn, name, login):
        return olga, None
    with restored(agent, "bsh", "owner_profile", "clone_dir"):
        agent.bsh, agent.owner_profile = bash, profile
        agent.clone_dir = lambda name: d
        # #574: установка -- _install_identity, до доставки; заметка та же.
        note, failed, _snap, _safe = asyncio.run(agent._install_identity(
            None, "pu-mop-1", "olga", (lf, None, "olga\t1\n", 1)))
    c.check("#313 the master's note names the skipped guard",
            "olga@example.dev" in note and "skipped" in note, (note, failed))


# ── #358: итог pu-sweep для doctor ───────────────────────────────────────
# HYPOTHESIS: подметание дёргает только nomad periodic pu-cleanup, и его итог
# лежит в логах аллокации -- doctor не может ни запустить его, ни прочесть.
# SOLUTION: глагол узла sweep зовёт /usr/local/bin/pu-sweep и отдаёт итог
# данными: последняя строка скрипта машиночитаема (pu_sweep_freed_kb=
# pu_sweep_free_gb=), предупреждения -- строки stderr с «!». Сам запуск живёт
# только на пуле; здесь -- разбор.
# STATUS: FIXED — see #358
SWEEP_OUT = ("=== disk before ===\n  /home/u  10G used of 100G\n\n"
             "tier 1: orphaned puppet dirs (1/2 puppets live)\n"
             "  orphaned clone pu-mop-3                        1.5 GB\n"
             "freed 1.5 GB\n=== disk after ===\n"
             "pu_sweep_freed_kb=1572864 pu_sweep_free_gb=41\n")
SWEEP_WARN = ("  ! 2 clone(s), 0 live tmux servers -- tmux unreachable, not 2 orphans\n"
              "  ! refusing tier 1; tiers 2-3 still run\n"
              "some noise without a bang\n")


def check_sweep_report_358(c):
    rep = agent.sweep_report
    c.expect("#358 a clean run: totals and the threshold",
             rep(0, SWEEP_OUT, "", 60),
             {"freed_kb": 1572864, "free_gb": 41, "min_gb": 60, "warnings": [],
              "bodies": None})
    c.expect("#358 warnings are the stderr lines with a bang, stripped",
             rep(0, SWEEP_OUT, SWEEP_WARN, 60)["warnings"],
             ["2 clone(s), 0 live tmux servers -- tmux unreachable, not 2 orphans",
              "refusing tier 1; tiers 2-3 still run"])
    # Узел с телами-контейнерами уходит после яруса 0, ecryptfs без монтирования
    # -- после отказа: итоговой строки нет, и это не ноль, а «не знаю».
    got = rep(0, "tier 0: orphaned bodies (driver pve)\n",
              "  ! $HOME has no puppets/ (unmounted ecryptfs?) -- refusing to sweep\n", 60)
    c.expect("#358 no totals line: None, not zero",
             (got["freed_kb"], got["free_gb"]), (None, None))
    c.expect("#358 the refusal survives as a warning", got["warnings"],
             ["$HOME has no puppets/ (unmounted ecryptfs?) -- refusing to sweep"])
    got = rep(2, "tier 1: …\n", "rm: cannot remove 'x': Permission denied\n", 60)
    c.check(f"#358 a non-zero exit is an error naming the code and the last line: {got}",
            "2" in got.get("error", "") and "Permission denied" in got.get("error", ""))
    c.check("#358 the error carries no totals", "freed_kb" not in got)


# ── #363: сколько тел снимет ярус 0 ──────────────────────────────────────
# HYPOTHESIS: у контейнерного узла в ответе sweep только freed_kb=None --
# doctor не отличит узел с брошенными телами от узла без них.
# SOLUTION: `mop driver sweep` заканчивает строкой pu_sweep_bodies=N, pu-sweep
# пропускает её в stdout, sweep_report отдаёт поле bodies; нет строки -- None.
# STATUS: FIXED — see #363
TIER0_OUT = ("=== disk before ===\n  /home/u  5G used of 94G\n\n"
             "tier 0: orphaned bodies (driver pve)\n"
             "  bodies: 6/8 with a live session\n"
             "    would destroy pu-mop-5: no session, clean clone\n"
             "    would destroy pu-mop-6: no session, clean clone\n"
             "  pu_sweep_bodies=2\n\n  /home/u  5G used of 94G\n")


def check_sweep_bodies_363(c):
    rep = agent.sweep_report
    got = rep(0, TIER0_OUT, "", 60)
    c.expect("#363 tier 0: the body count, the totals still unknown",
             (got.get("bodies"), got["freed_kb"], got["free_gb"]), (2, None, None))
    c.expect("#363 a host node's run has no body count",
             rep(0, SWEEP_OUT, "", 60).get("bodies", "missing"), None)


# ── #366: отказ тела -- полем ответа write, а не строкой в written ────────
# HYPOTHESIS: отказ записи в тело агент кладёт строкой в written
# («<тело> FAILED — <причина>», «<тело> NOT LIVE»), поля error в ответе
# нет; bus.results_from решает по error, и `mop login` печатал узел, где
# запись во все тела упала, как OK.
# SOLUTION: ответ write несёт failed {тело: причина} и absent [тело];
# written прежний -- его печатают старые клиенты.
# STATUS: FIXED — see #366
def check_write_fields_366(c):
    import asyncio
    import base64
    from mop import driver
    from mop.common import paths

    class Drv:
        IS_CONTAINER = True
        SESSION_PY = agent.DRIVER.SESSION_PY

        @staticmethod
        async def bodies():
            return ["pu-mop-1", "pu-mop-2"]

        @staticmethod
        async def push_many(name, files):
            if name == "pu-mop-2":
                return {"error": "body 9002 is stopped"}
            return {"written": [p for p, _ in files]}

        @staticmethod
        def argv(name):
            return []

    async def project(name):
        return "mop"
    cred = [".config/mop/secrets.env", base64.b64encode(b"{}").decode()]
    with restored(agent, "DRIVER", "puppet_project"), restored(driver, "write_private"):
        agent.DRIVER, agent.puppet_project = Drv, project
        driver.write_private = lambda path, data: None
        got = asyncio.run(agent.v_write(None, {
            "_project": "admin", "files": [cred],
            "bodies": ["pu-mop-1", "pu-mop-2", "pu-mop-9"]}))
        c.expect("#366 write: a refused body is a field with its reason",
                 got.get("failed"), {"pu-mop-2": "body 9002 is stopped"})
        c.expect("#366 write: a body not live is a field", got.get("absent"), ["pu-mop-9"])
        c.check("#366 write: written keeps the old lines for old clients",
                "pu-mop-2 FAILED — body 9002 is stopped" in got.get("written", [])
                and "pu-mop-9 NOT LIVE" in got.get("written", []), got.get("written"))
        c.check("#366 write: no error field -- the node answered", "error" not in got, got)
        got = asyncio.run(agent.v_write(None, {"_project": "admin", "files": [cred],
                                               "bodies": ["pu-mop-1"]}))
        c.expect("#366 write: all written -- the fields are empty",
                 (got.get("failed"), got.get("absent")), ({}, []))


def check_hung_sweep_373(c):
    """HYPOTHESIS (#373): v_sweep ждал pu-sweep голым proc.communicate() без
    срока, держа _sweep_lock. Зависни pu-sweep (mop driver sweep ждёт
    недоступный гипервизор) -- каждый следующий sweep отвечал «a sweep is
    already running on this node» до рестарта юнита.
    SOLUTION: v_sweep идёт через driver.sh (env=, раздельный stderr) со
    сроком MOP_SWEEP_TIMEOUT -- меньше, чем клиент (doctor disk) ждёт ответа:
    отказ агента доезжает до doctor, а не читается как «did not answer».
    По сроку процесс убит, ответ {"error"}, замок отпущен.
    STATUS: FIXED — see #373

    Вместо pu-sweep -- скрипт, который висит; срок -- доли секунды. Внешний
    wait_for держит проверку от зависания на старом коде."""
    import asyncio
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        hung = os.path.join(tmp, "pu-sweep")
        with open(hung, "w") as f:
            f.write("#!/bin/sh\nexec sleep 30\n")
        os.chmod(hung, 0o755)

        async def twice():
            first = await asyncio.wait_for(agent.v_sweep(None, {"dry": True}), 5)
            second = await asyncio.wait_for(agent.v_sweep(None, {"dry": True}), 5)
            return first, second
        with patched(agent, SWEEP=hung, _sweep_lock=asyncio.Lock()), \
                patched_env(MOP_SWEEP_TIMEOUT="0.3"):
            try:
                first, second = asyncio.run(twice())
            except asyncio.TimeoutError:
                first = second = {"error": "v_sweep did not return (outer 5s bound)"}
    c.expect("#373 a hung pu-sweep is refused by the deadline",
             first, {"error": "pu-sweep did not finish in 0.3s"})
    c.check("#373 the next sweep is not «already running»",
            "already running" not in str(second.get("error")), second)
    c.check("#373 the node deadline is shorter than the client's wait",
            float(agent.config.SETTINGS["MOP_SWEEP_TIMEOUT"]) < _doctor_sweep_timeout(),
            agent.config.SETTINGS.get("MOP_SWEEP_TIMEOUT"))
    c.check("#373 MOP_SWEEP_TIMEOUT reaches the node (NODE_SCOPED)",
            "MOP_SWEEP_TIMEOUT" in agent.config.NODE_SCOPED)


def _doctor_sweep_timeout():
    """Сколько doctor disk ждёт ответа sweep -- из его кода: серверный
    модуль тянет шину, а проверке нужно одно число."""
    import re
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    src = open(os.path.join(root, "mop", "server", "doctor", "disk.py")).read()
    return float(re.search(r"^SWEEP_TIMEOUT = (\d+)", src, re.M)[1])


# ─── #584: делегированный актёр на узле ──────────────────────────────────
A, B, W, R, X = ("grant-" + ch * 32 for ch in "abcde")
GRANTS_584 = {
    # Мастер-роль хозяина anton: диспетчерит, обычное управление телами.
    A: {"role": "master", "owner": "anton", "project": "mop", "active": True,
        "dispatch": True, "receives_dispatch": False, "soul_inc": "inc-a",
        "node_verbs": ["send", "tail", "state", "states", "ping", "local",
                       "type", "wipe", "usage", "clone"]},
    # Вторая мастер-роль того же человека (#584: «один человек, два мастера»).
    B: {"role": "master", "owner": "anton", "project": "mop", "active": True,
        "dispatch": True, "receives_dispatch": False, "soul_inc": "inc-b",
        "node_verbs": ["send", "tail", "state", "states", "ping", "local",
                       "type", "wipe", "usage", "clone"]},
    # Рабочий: send есть, dispatch нет.
    W: {"role": "puppet", "owner": "anton", "project": "mop", "active": True,
        "dispatch": False, "receives_dispatch": True, "soul_inc": "inc-w",
        "node_verbs": ["send", "tail", "state", "states", "ping", "local"]},
    # Старая инкарнация: привязка отозвана, токен в субъекте по-прежнему
    # правильной формы, соединение живо (факт 1 SOUL.md).
    R: {"role": "master", "owner": "anton", "project": "mop", "active": False,
        "dispatch": True, "receives_dispatch": False, "soul_inc": "inc-old",
        "node_verbs": ["send", "wipe"]},
    # Чужой проект.
    X: {"role": "master", "owner": "olga", "project": "other", "active": True,
        "dispatch": True, "receives_dispatch": False, "soul_inc": "inc-x",
        "node_verbs": ["send", "wipe"]},
}
TARGETS_584 = {
    "pu-mop-1": {"found": True, "job": "pu-mop-1", "project": "mop",
                 "role": "puppet", "receives_dispatch": True},
    "pu-mop-2": {"found": True, "job": "pu-mop-2", "project": "mop",
                 "role": "master", "receives_dispatch": False},
    "pu-mop-3": {"found": False},
}


def resolver_584(req, mode=None):
    """Резолвер сервера, каким его обещает #581: решает реестр, не тело."""
    principal, verb, target = req.get("principal"), req.get("verb_name"), req.get("target")
    g = GRANTS_584.get(principal)
    if g is None:
        return {"allow": False, "reason": f"unknown principal {principal}"}
    out = {"principal": principal, "role": g["role"], "owner": g["owner"],
           "project": g["project"], "soul_inc": g["soul_inc"],
           "dispatch": g["dispatch"], "receives_dispatch": g["receives_dispatch"]}
    if target is not None:
        out["target"] = dict(TARGETS_584.get(target, {"found": False}))
    if mode == "other-principal":
        out["principal"] = B if principal != B else A
    if mode == "other-job" and target is not None:
        out["target"] = dict(TARGETS_584["pu-mop-1"], job="pu-mop-9")
    if mode == "other-target-project" and target is not None:
        out["target"] = dict(out["target"], project="other")
    if mode == "no-owner":
        out["owner"] = ""
    if not g["active"]:
        return dict(out, allow=False, reason=f"principal {principal} is not active")
    if g["project"] != req.get("project"):
        return dict(out, allow=False, reason=f"principal {principal} is not in project {req.get('project')}")
    if verb == "dispatch":
        ok = g["dispatch"] and TARGETS_584.get(target, {}).get("receives_dispatch") is True
    else:
        ok = verb in g["node_verbs"]
    return dict(out, allow=ok, reason=None if ok else f"verb {verb} is not granted to {g['role']}")


def check_role_node_gates_584(c):
    """HYPOTHESIS (#584): агент узла не знает делегированных принципалов.
    rpc.grant-<id> читается как логин человека: send захватывает аренду и
    ставит identity, глагол вне node_verbs исполняется, отозванная привязка
    и молчащий резолвер ничего не останавливают; dispatch-субъекта агент не
    слушает вовсе, а в handle он идёт прежним self-declared путём, где
    владельца называет тело.
    SOLUTION: см. agent.resolve_actor / handle -- разрешение принципала
    свежим запросом к резолверу сервера (server.rpc, узловой кред) перед
    любым эффектом; send нового актёра -- без аренды и identity; dispatch --
    своим субъектом, аренда за актёром, identity -- хозяина-человека из
    ответа резолвера, транзакция #574 целиком.
    RESULT: RED на fa60d1e (21 провал: аренда и identity у рабочего, wipe
    вне node_verbs и по dispatch-субъекту, отозванный и молчащий резолвер
    пропускают); зелёный после правки агента.
    STATUS: FIXED — see #584"""
    import asyncio
    import subprocess
    import tempfile
    from mop.common import bus, lease
    from mop.common.domain import Owner
    profiles = {"anton": {"name": "Anton E", "email": "anton@example.dev"},
                "petr": {"name": "Petr P", "email": "petr@example.dev"}}
    root = tempfile.mkdtemp(prefix="mop-test-584-")
    clones = {}

    def fresh(name):
        d = tempfile.mkdtemp(dir=root)
        subprocess.run(["git", "init", "-q", "-b", "master", d], check=True)
        for key in ("name", "email"):
            subprocess.run(["git", "-C", d, "config", "--local", f"user.{key}",
                            profiles["petr"][key]], check=True)
        clones[name] = d

    def ident(name):
        got = {}
        for key in ("name", "email"):
            r = subprocess.run(["git", "-C", clones[name], "config", "--local", f"user.{key}"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                got[key] = r.stdout.strip()
        return got

    def holder(name):
        path = os.path.join(clones[name], lease.FILE)
        return Owner.parse(open(path).read()).user if os.path.exists(path) else None

    async def facts(name):
        path = os.path.join(clones[name], lease.FILE)
        owner = Owner.parse(open(path).read()) if os.path.exists(path) else None
        return {"owner": owner and owner.to_dict(), "dirty": 0, "ahead": 0,
                "cur": "master", "def": "master"}

    st = {"mode": None, "deliver_error": None}
    asks, delivered, events, destroyed = [], [], [], []

    class Reply:
        def __init__(self, body):
            self.data = json.dumps(body).encode()

    async def arequest(conn, subject, data, timeout):
        req = json.loads(data)
        asks.append((subject, req))
        if st["mode"] == "down":
            raise asyncio.TimeoutError()
        if req.get("verb") == "identity":
            p = profiles.get(req.get("login"))
            return Reply(p if p else {"error": f"no profile for {req.get('login')}"})
        if req.get("verb") == "resolve":
            return Reply(resolver_584(req, st["mode"]))
        return Reply({"error": f"no such verb {req.get('verb')}"})

    async def deliver(name, cmd, timeout=20):
        if "wait-idle" in cmd:
            return {"state": "idle"}
        delivered.append({"name": name, "cmd": cmd, "ident": ident(name),
                          "holder": holder(name)})
        if st["deliver_error"]:
            return {"error": st["deliver_error"]}
        return {"msg_id": f"m-{len(delivered)}"}

    async def event(conn, kind, name=None, **fields):
        events.append((kind, name, fields))

    async def project(name):
        return "mop"

    async def dead(name):
        return False

    class Drv:
        SESSION_PY = "/x/session.py"

        async def destroy(self, name, branch=None):
            destroyed.append(name)
            return {"wiped": name}

    def ask(subject, body):
        m = Msg(subject, body=body)
        asyncio.run(agent.handle(None, m, public=subject.endswith(".msg")))
        return m.replies[-1] if m.replies else None

    def reset(*names):
        asks.clear(), delivered.clear(), events.clear(), destroyed.clear()
        st.update(mode=None, deliver_error=None)
        agent.__dict__.update(_owner_locks={}, _lease_generations={},
                              _lease_claims={}, _lease_holder={})
        for n in names or ("pu-mop-1", "pu-mop-2", "pu-mop-3"):
            fresh(n)

    rpc = lambda g: f"mop.mop.node.hyper.rpc.{g}"                      # noqa: E731
    dsp = lambda g: busnames.dispatch_subject("mop", "hyper", g[len("grant-"):])  # noqa: E731
    resolves = lambda: [r for _s, r in asks if r.get("verb") == "resolve"]  # noqa: E731

    with restored(agent, "bsh", "clone_facts", "clone_dir", "session_json",
                  "_event", "puppet_project", "_owner_locks", "_lease_generations",
                  "_lease_claims", "_lease_holder", "IDENTITY_WAIT", "tmux_alive",
                  "DRIVER"), \
            restored(agent.time, "time"), restored(bus, "arequest"):
        agent.bsh, agent.clone_facts, agent.session_json = bash, facts, deliver
        agent._event, agent.puppet_project, agent.tmux_alive = event, project, dead
        agent.DRIVER, agent.IDENTITY_WAIT = Drv(), 0.1
        agent.clone_dir = lambda name: clones[name]
        agent.time.time = lambda: 1000
        bus.arequest = arequest
        send = {"verb": "send", "name": "pu-mop-1", "message": "hi", "from_name": "anton"}

        # 1. Обычный send нового актёра -- сообщение соседу: доставка, без
        # аренды и без identity; резолвер спрошен; from-name -- актёр, не тело.
        reset()
        got = ask(rpc(W), send)
        c.check("584 grant send is authorized by the server resolver",
                [(r.get("principal"), r.get("verb_name"), r.get("target"))
                 for r in resolves()] == [(W, "send", "pu-mop-1")], asks)
        c.check("584 grant send is delivered", bool(got and got.get("msg_id")), got)
        c.check("584 grant send takes no lease", holder("pu-mop-1") is None,
                holder("pu-mop-1"))
        c.check("584 grant send sets no git identity",
                ident("pu-mop-1") == profiles["petr"]
                and not [r for _s, r in asks if r.get("verb") == "identity"],
                (ident("pu-mop-1"), asks))
        c.check("584 grant send: from-name is the actual actor, not the body",
                bool(delivered) and f"--from-name {W}" in delivered[0]["cmd"]
                and "--from-name anton" not in delivered[0]["cmd"], delivered)
        c.check("584 grant send: the audit event names the actual actor",
                [(k, f.get("actor")) for k, _n, f in events if k == "send"] == [("send", W)],
                events)

        # 2. Поддельные каркасные и авторитетные поля в теле ничего не дают.
        reset()
        forged = dict(send, _caller="anton", _actor=A, _owner="anton", _dispatch=True,
                      _project="admin", owner="anton", role="master", dispatch=True,
                      soul_digest="0" * 32, grant=A, force=True)
        got = ask(rpc(W), forged)
        c.check("584 forged '_' and authority fields: still a plain send, no lease",
                holder("pu-mop-1") is None and bool(got and got.get("msg_id")),
                (holder("pu-mop-1"), got))
        c.check("584 forged fields: the resolver is asked about the subject's actor",
                [r.get("principal") for r in resolves()] == [W], resolves())

        # 3. Глагол вне node_verbs -- отказ с причиной до эффекта.
        reset()
        got = ask(rpc(W), {"verb": "wipe", "name": "pu-mop-1"})
        c.check("584 a verb outside node_verbs is refused before effect",
                not destroyed and bool(got and got.get("error"))
                and "wipe" in got["error"], (destroyed, got))

        # 4. Отозванная привязка (старая инкарнация) -- отказ, хотя токен
        # в субъекте правильной формы и соединение живо.
        reset()
        got = ask(rpc(R), send)
        c.check("584 a revoked principal is refused before delivery",
                not delivered and bool(got and got.get("error"))
                and "not active" in got["error"], (delivered, got))

        # 5. Резолвер молчит -- отказ до эффекта, не «пропустить».
        reset()
        st["mode"] = "down"
        got = ask(rpc(W), send)
        c.check("584 a silent resolver denies before effect",
                not delivered and holder("pu-mop-1") is None
                and bool(got and got.get("error")), (delivered, got))
        reset()
        st["mode"] = "down"
        got = ask(dsp(A), dict(send, verb="dispatch"))
        c.check("584 a silent resolver denies dispatch before the lease",
                not delivered and holder("pu-mop-1") is None
                and bool(got and got.get("error")), (delivered, got))

        # 6. dispatch: свой субъект; аренда -- за актёром, identity -- хозяина
        # из резолвера, до доставки (#574); from-name -- актёр.
        reset()
        got = ask(dsp(A), dict(send, verb="dispatch"))
        c.check("584 dispatch asks the resolver for verb dispatch on the target",
                [(r.get("principal"), r.get("verb_name"), r.get("target"))
                 for r in resolves()] == [(A, "dispatch", "pu-mop-1")], asks)
        c.check("584 dispatch is delivered", bool(got and got.get("msg_id")), got)
        c.check("584 dispatch: the lease is held by the actor, identity is the "
                "human owner's, both before delivery",
                bool(delivered) and delivered[0]["holder"] == A
                and delivered[0]["ident"] == profiles["anton"], delivered)
        c.check("584 dispatch: from-name is the actor",
                bool(delivered) and f"--from-name {A}" in delivered[0]["cmd"], delivered)
        c.check("584 dispatch: the audit event names the actor and the owner",
                [(f.get("actor"), f.get("owner")) for k, _n, f in events if k == "send"]
                == [(A, "anton")], events)

        # 7. Один человек, два мастера: B не забирает живую аренду A, даже
        # если тело просит force -- его владелец тот же человек.
        got = ask(dsp(B), dict(send, verb="dispatch", force=True))
        c.check("584 the same human's second master cannot take a live lease",
                len(delivered) == 1 and holder("pu-mop-1") == A
                and bool(got and got.get("error")), (delivered, got))

        # 8. Роль без dispatch -- отказ.
        reset()
        got = ask(dsp(W), dict(send, verb="dispatch"))
        c.check("584 a role without dispatch cannot dispatch",
                not delivered and holder("pu-mop-1") is None
                and bool(got and got.get("error")), (delivered, got))

        # 9. Цель без receives_dispatch -- отказ, хотя она свободна: право
        # цели -- отдельная проверка от свободы клона.
        reset()
        got = ask(dsp(A), dict(send, verb="dispatch", name="pu-mop-2"))
        c.check("584 a free target without receives_dispatch is refused",
                not delivered and holder("pu-mop-2") is None
                and bool(got and got.get("error")), (delivered, got))

        # 10. Непривязанная цель -- отказ.
        reset()
        got = ask(dsp(A), dict(send, verb="dispatch", name="pu-mop-3"))
        c.check("584 an unbound target is refused",
                not delivered and holder("pu-mop-3") is None
                and bool(got and got.get("error")), (delivered, got))

        # 11. Чужой проект: принципал из other в субъекте mop -- отказ; ответ
        # о цели из чужого проекта -- отказ на узле, не только на сервере.
        reset()
        got = ask(rpc(X), send)
        c.check("584 a principal of another project is refused",
                not delivered and bool(got and got.get("error")), (delivered, got))
        reset()
        st["mode"] = "other-target-project"
        got = ask(dsp(A), dict(send, verb="dispatch"))
        c.check("584 a target the resolver places in another project is refused",
                not delivered and holder("pu-mop-1") is None
                and bool(got and got.get("error")), (delivered, got))

        # 12. Ответ не о том: другой принципал или другая цель (имя
        # переиспользовано) -- отказ.
        for mode in ("other-principal", "other-job"):
            reset()
            st["mode"] = mode
            got = ask(dsp(A), dict(send, verb="dispatch"))
            c.check(f"584 a resolver answer about someone else ({mode}) is refused",
                    not delivered and holder("pu-mop-1") is None
                    and bool(got and got.get("error")), (delivered, got))

        # 13. Нет хозяина-человека в ответе -- dispatch отказан до эффекта.
        reset()
        st["mode"] = "no-owner"
        got = ask(dsp(A), dict(send, verb="dispatch"))
        c.check("584 dispatch without a resolved human owner is refused",
                not delivered and holder("pu-mop-1") is None
                and bool(got and got.get("error")), (delivered, got))

        # 14. Путаница операций: субъект dispatch с глаголом wipe -- отказ;
        # rpc с телом «dispatch: true» -- обычный send; глагол dispatch по
        # rpc -- не глагол.
        reset()
        got = ask(dsp(A), {"verb": "wipe", "name": "pu-mop-1"})
        c.check("584 the dispatch subject executes dispatch only, not wipe",
                not destroyed and bool(got and got.get("error")), (destroyed, got))
        reset()
        got = ask(rpc(A), dict(send, dispatch=True, mode="dispatch"))
        c.check("584 an rpc body asking for dispatch stays a plain send",
                holder("pu-mop-1") is None and bool(got and got.get("msg_id")),
                (holder("pu-mop-1"), got))
        reset()
        got = ask(rpc(A), dict(send, verb="dispatch"))
        c.check("584 verb dispatch over rpc is refused (the subject decides)",
                not delivered and holder("pu-mop-1") is None
                and bool(got and got.get("error")), (delivered, got))

        # 15. Частичный провал: доставка упала -- аренда и identity
        # откатываются (#574) за актёром.
        reset()
        st["deliver_error"] = "socket gone"
        got = ask(dsp(A), dict(send, verb="dispatch"))
        c.check("584 a failed dispatch delivery rolls the lease and identity back",
                holder("pu-mop-1") is None and ident("pu-mop-1") == profiles["petr"]
                and bool(got and got.get("error")),
                (holder("pu-mop-1"), ident("pu-mop-1"), got))

        # 16. Изменяющий глагол актёра -- через ворота аренды актёра: чужая
        # живая аренда (A) закрывает wipe второму мастеру (B).
        reset()
        ask(dsp(A), dict(send, verb="dispatch"))
        got = ask(rpc(B), {"verb": "wipe", "name": "pu-mop-1"})
        c.check("584 another actor's live lease gates a mutating verb",
                not destroyed and bool(got and got.get("error")), (destroyed, got))

        # 17. Легаси без изменений: человек по rpc -- аренда и identity, без
        # резолвера; публичный msg -- без владельца и без резолвера.
        reset()
        got = ask("mop.mop.node.hyper.rpc.anton", send)
        c.check("584 legacy human rpc send keeps lease and identity, no resolver",
                holder("pu-mop-1") == "anton" and not resolves()
                and bool(delivered) and delivered[0]["ident"] == profiles["anton"],
                (holder("pu-mop-1"), resolves(), delivered))
        reset()
        got = ask("mop.mop.node.hyper.msg", send)
        c.check("584 legacy public msg send: no owner, no resolver",
                holder("pu-mop-1") is None and not resolves()
                and bool(got and got.get("msg_id")), (holder("pu-mop-1"), got))
        # Узловые глаголы -- никогда актёру, что бы ни ответил резолвер.
        reset()
        got = ask(rpc(A), {"verb": "disk"})
        c.check("584 node-level verbs are never given to an actor",
                bool(got and got.get("error")), got)

    # Чистый вердикт: только явное allow, иное -- отказ, не «пропустить».
    ok = {"allow": True, "principal": A, "project": "mop", "owner": "anton"}
    for label, got in (("no allow", {k: v for k, v in ok.items() if k != "allow"}),
                       ("truthy allow", dict(ok, allow="yes")),
                       ("not a mapping", ["allow"]),
                       ("owner not a string", dict(ok, owner=["anton"]))):
        c.check(f"584 actor_verdict fails closed: {label}",
                bool(agent.actor_verdict(got, A, "mop", "send")), got)
    c.check("584 actor_verdict passes an explicit allow about the asked principal",
            agent.actor_verdict(ok, A, "mop", "send") is None)
    c.check("584 dispatch subject token must be a principal id",
            agent.actor_of("mop.mop.node.hyper.dispatch.anton") == ("dispatch", None)
            and agent.actor_of("mop.mop.node.hyper.rpc.anton") == ("rpc", None)
            and agent.actor_of(rpc(W)) == ("rpc", W)
            and agent.actor_of(dsp(A)) == ("dispatch", A))

    subs = asyncio.run(_subjects_584())
    c.check("584 the agent listens on its dispatch subject",
            "mop.*.node.hyper.dispatch.*" in subs, subs)


async def _subjects_584():
    """Субъекты attach без шины: conn -- записывающая заглушка."""
    got = []

    class Conn:
        async def subscribe(self, subj, cb=None):
            got.append(subj)

        async def publish(self, *a, **k):
            pass
    with restored(agent, "_event"):
        async def quiet(*a, **k):
            return None
        agent._event = quiet
        await agent.attach(Conn(), "hyper")
    return got


def main():
    c = Checks()
    for check in (check_sets, check_decisions, check_tmux, check_quiet,
                  check_write_home_279, check_addressed_write_312, check_write_fields_366,
                  check_clone_probe_fail_closed_564, check_clone_probe_347, check_timeouts_171, check_unclaim_181, check_intake,
                  check_main_169, check_subject_173, check_unclaim_race_189,
                  check_same_owner_generation_573,
                  check_gates_40, check_caller_207, check_git_identity_167,
                  check_identity_before_delivery_574,
                  check_state_fact_224, check_no_screen_fact_236,
                  check_usage_by_login_244, check_owner_gate_267,
                  check_junk_without_templates_276, check_owner_hook_313,
                  check_sweep_report_358, check_sweep_bodies_363,
                  check_hung_sweep_373, check_role_node_gates_584):
        try:
            check(c)
        except Exception as e:
            c.fail(f"{check.__name__} raised", f"{type(e).__name__}: {e}")
    return c.report("agent")


if __name__ == "__main__":
    sys.exit(main())
