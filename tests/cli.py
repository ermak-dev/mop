#!/usr/bin/env python3
"""Проверка диспетчера командлетов без пула: python3 tests/cli.py

Командлеты живут модулями в mop/cli (#75): секции — плоские имена, группы —
подпакеты с глаголами. Чистое здесь — каталог имён с отказом на дубль,
разбор argv в (модуль, аргументы) и описание команды из докстринга без
импорта модуля. Сам запуск команд — только руками.
"""
import json
import os
import uuid
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import (Checks, NetworkGuard, no_network, offline, patched,  # noqa: E402,F401
                  patched_env, restored, run_command)
sys.path.insert(0, ROOT)

from mop import cli  # noqa: E402
from mop.cli import lib  # noqa: E402
from mop.cli.core import _common  # noqa: E402
from mop.cli.server import _play  # noqa: E402
from mop.common import domain  # noqa: E402

# (каталог, имя, подпакет?) — то, что находит обход mop/cli.
FOUND = [
    ("core", "add", False), ("core", "list", False), ("pool", "deploy", False),
    ("service", "mcp", False), ("driver", "", True), ("bug", "", True),
    ("dev", "", True),
]
# Дерево глаголов (#253): модуль -- {}, подгруппа -- её дерево. Множество
# прежнего контракта сюда не годится: у подгруппы есть свои глаголы.
VERBS = {"driver": {"build": {}, "run": {}}, "bug": {},
         "dev": {"bug": {"new": {}, "list": {}}, "ci": {"list": {}}}}


# HYPOTHESIS: общий help пересказывает устройство и при опечатке печатает
# весь каталог вместо одного способа исправить вызов.
# SOLUTION: короткий заголовок, каталог действий и одна подсказка на опечатку.
# RESULT: проверяется на живом каталоге без входа в пул.
# STATUS: FIXED — see #430

def check_concise_global_help_430(c):
    help_text = cli.usage()
    c.check("#430 help keeps command form", "mop [--server" in help_text)
    c.check("#430 help retains daily commands", all(
        f"    {name}" in help_text for name in ("add", "list", "login", "master")))
    for internal in ("Nomad", "NATS", "git config", "MOP_BUS_USER", "Integration branch"):
        c.check(f"#430 help omits {internal}", internal not in help_text)
    c.check("#430 help gives a focused route to more detail", "mop help" in help_text)

    out, err, code = run_command(cli.main, ["not-a-command"])
    c.expect("#430 unknown command leaves stdout empty", out, "")
    # #642: неизвестное имя также может быть ролью. Пока role query/launch не
    # опубликованы, отказ capability честнее старого lookup через NATS/SSH.
    c.expect("#642 unresolved role launch exits unsupported", code, 2)
    c.check("#642 unresolved role launch names missing capability",
            "unsupported" in err and "role" in err, repr(err))
    c.check("#430 unknown command does not dump catalog", "    add " not in err, repr(err))

    out, err, code = run_command(cli.main, ["help"])
    c.expect("#430 explicit help uses concise catalog", (out, err, code),
             (help_text + "\n", "", 0))


# ── #334: mop add/update/recycle говорят, какой bootstrap отправлен ────
# HYPOTHESIS: workspace_text возвращает голый текст -- откуда он (рабочая
# копия или origin, какой коммит, правлен ли), теряется, и `mop update`
# молчит сразу после регистрации: оператор не знает, какой файл уехал.
# SOLUTION: workspace_text -> (текст, происхождение {source, commit, dirty,
# tasks, present}); происхождение едет полем bootstrap_sent рядом с
# workspace, строку «bootstrap sent: …» печатает командлет.
# STATUS: FIXED — see #334
NOFILE_334 = {"source": "working copy", "commit": "5b92b440c952fad2", "dirty": False,
              "tasks": 0, "present": False}
BOOT_334 = """
- name: bootstrap
  tasks:
    - name: one
      ansible.builtin.command: "true"
    - name: two
      ansible.builtin.command: "true"
"""


def check_bootstrap_sent_334(c):
    import subprocess
    sent_line = getattr(_common, "sent_line", None)
    if not c.check("#334 _common.sent_line exists", sent_line is not None):
        return
    sha = "5b92b440c952fad2fcded7a39d6e9f82b9d982a0"
    cases = [
        ({"source": "working copy", "commit": sha, "dirty": False, "tasks": 4, "present": True},
         "bootstrap sent: .mop/bootstrap.yaml from the working copy at 5b92b440c952, 4 tasks"),
        ({"source": "working copy", "commit": sha, "dirty": True, "tasks": 4, "present": True},
         "bootstrap sent: .mop/bootstrap.yaml from the working copy at 5b92b440c952 "
         "(+ uncommitted), 4 tasks"),
        ({"source": "origin", "commit": sha, "dirty": False, "tasks": 1, "present": True},
         "bootstrap sent: .mop/bootstrap.yaml from origin at 5b92b440c952, 1 task"),
        ({"source": "working copy", "commit": sha, "dirty": True, "tasks": 0, "present": False},
         "bootstrap not sent: no .mop/bootstrap.yaml, the server copy is removed"),
        ({"source": "origin", "commit": sha, "dirty": False, "tasks": 0, "present": False},
         "bootstrap not sent: no .mop/bootstrap.yaml, the server copy is removed"),
        # Не разобрался -- без счёта: отказ по форме скажет сервер.
        ({"source": "working copy", "commit": sha, "dirty": False, "tasks": None,
          "present": True},
         "bootstrap sent: .mop/bootstrap.yaml from the working copy at 5b92b440c952"),
    ]
    for prov, want in cases:
        c.expect(f"#334 sent_line {prov['source']}, dirty {prov['dirty']}, "
                 f"present {prov['present']}", sent_line(prov), want)

    # Рабочая копия: коммит HEAD, правка считается только у самого файла.
    d = tempfile.mkdtemp(prefix="mop-test-334-")

    def git(*a):
        return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True,
                              check=True).stdout.strip()
    git("init", "-q")
    os.makedirs(os.path.join(d, ".mop"))
    boot = os.path.join(d, ".mop", "bootstrap.yaml")
    with open(boot, "w") as f:
        f.write(BOOT_334)
    with open(os.path.join(d, "other"), "w") as f:
        f.write("x")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
    head = git("rev-parse", "HEAD")
    here = os.getcwd()
    origin = "git@example.dev:g/proj.git"
    try:
        os.chdir(d)
        with patched(lib, cwd_origin=lambda: origin):
            with open(os.path.join(d, "other"), "w") as f:
                f.write("changed")                    # правка не bootstrap'а
            got = _common.workspace_text(origin)
            c.expect("#334 working copy, clean file: text and provenance", got,
                     (BOOT_334, {"source": "working copy", "commit": head, "dirty": False,
                                 "tasks": 2, "present": True}))
            with open(boot, "a") as f:
                f.write("    - name: three\n      ansible.builtin.command: \"true\"\n")
            text, prov = _common.workspace_text(origin)
            c.expect("#334 working copy, edited file: dirty, the edit counted",
                     (prov["dirty"], prov["tasks"]), (True, 3))
            os.unlink(boot)
            c.expect("#334 working copy, no file: empty text, not present",
                     _common.workspace_text(origin),
                     ("", {"source": "working copy", "commit": head, "dirty": True,
                           "tasks": 0, "present": False}))
        # origin: коммит -- HEAD того же клона, что читает манифест.
        from mop.common import manifest
        with patched(lib, cwd_origin=lambda: "git@example.dev:g/other.git"), \
                patched(manifest, fetch=lambda o: {"bootstrap_text": BOOT_334, "commit": head}):
            c.expect("#334 origin: text and provenance", _common.workspace_text(origin),
                     (BOOT_334, {"source": "origin", "commit": head, "dirty": False,
                                 "tasks": 2, "present": True}))
        with patched(lib, cwd_origin=lambda: None), \
                patched(manifest, fetch=lambda o: {"bootstrap_text": None, "commit": head}):
            c.expect("#334 origin without the file: empty text, not present",
                     _common.workspace_text(origin),
                     ("", {"source": "origin", "commit": head, "dirty": False,
                           "tasks": 0, "present": False}))
    finally:
        os.chdir(here)


# ── #334: add/update ждут итог своей регистрации и называют его ──────────
# HYPOTHESIS: чем кончился прогон bootstrap'а после `mop update`, видно
# только в `mop list`, журнале сервиса или stderr аллокации.
# SOLUTION: ответ регистрации несёт метку (bootstrap_marker); клиент ждёт
# итог с этой меткой глаголом bootstrap_result и печатает строку; спека не
# изменилась (replaced False) -- говорит, что прогона не будет, и не ждёт.
# STATUS: FIXED — see #334
def check_bootstrap_outcome_334(c):
    from mop.common import bus
    line = getattr(_common, "outcome_line", None)
    if not c.check("#334 _common.outcome_line exists", line is not None):
        return
    rec = {"ok": True, "played": True, "seconds": 41.6, "rc": 0, "task": None,
           "message": None, "last": "PLAY RECAP", "sent": 7, "commit": "abc"}
    cases = [
        (rec, "bootstrap ok in 42s"),
        ({**rec, "ok": False, "rc": 2, "task": "env file",
          "message": "Could not find ~/proj/.env"},
         "bootstrap failed at task «env file»: Could not find ~/proj/.env"),
        ({**rec, "ok": False, "rc": 2, "last": "fatal: [host]: UNREACHABLE!"},
         "bootstrap failed (rc 2): fatal: [host]: UNREACHABLE!"),
        ({**rec, "ok": False, "rc": None, "seconds": 290.0,
          "last": "bootstrap of pu-mop-1 did not finish in 290s"},
         "bootstrap failed: bootstrap of pu-mop-1 did not finish in 290s"),
        (None, "bootstrap result not seen in 420s — mop list"),
        ({**rec, "played": False, "seconds": None, "rc": None, "last": None}, None),
    ]
    for record, want in cases:
        c.expect(f"#334 outcome_line {record and (record['ok'], record['played'], record['task'])}",
                 line(record, 420), want)

    # Ожидание: чужая (старая) метка и пустота -- ждать дальше; своя -- итог.
    answers = [{"ok": True, "result": {**rec, "sent": 6}}, {"ok": True, "result": None},
               {"ok": True, "result": rec}]
    asked, now = [], [0.0]

    def call_cluster(verb, **kw):
        asked.append((verb, kw.get("name")))
        return answers[min(len(asked) - 1, len(answers) - 1)]

    def sleep(sec):
        now[0] += sec
    with patched(bus, call_cluster=call_cluster):
        got = _common.wait_bootstrap("pu-mop-1", 7, lib.Progress("t"), 60,
                                     sleep=sleep, clock=lambda: now[0])
    c.expect("#334 wait_bootstrap returns the record of its own marker",
             (got, asked[-1]), (rec, ("bootstrap_result", "pu-mop-1")))
    answers[:] = [{"ok": True, "result": {**rec, "sent": 6}}]
    asked.clear(), now.__setitem__(0, 0.0)
    with patched(bus, call_cluster=call_cluster):
        got = _common.wait_bootstrap("pu-mop-1", 7, lib.Progress("t"), 60,
                                     sleep=sleep, clock=lambda: now[0])
    c.check("#334 wait_bootstrap gives up after its timeout with None",
            got is None and now[0] >= 60 and len(asked) > 1, (got, now, len(asked)))

    # update целиком: метка в ответе -- ждёт и печатает итог; replaced False --
    # говорит, что прогона не будет, и итог не спрашивает; без метки (старый
    # сервер) -- ничего сверх строки «что уехало».
    from mop.cli.core import update
    # Поток и код выхода по исходу (решение мастера 27.09): провал прогона --
    # провал команды (stderr, 1), как не вставший папет у `mop add`; итог не
    # увиден и прогона не будет -- stdout, 0.
    failed = {**rec, "ok": False, "rc": 2, "task": "env file", "message": "no .env"}
    sent = _common.sent_line(NOFILE_334)
    for reply, result, want_out, want_err, want_code, want_asked in (
            ({"ok": True, "bootstrap_marker": 7, "replaced": True},
             {"ok": True, "result": rec}, [sent, "bootstrap ok in 42s"], [], 0, True),
            ({"ok": True, "bootstrap_marker": 7, "replaced": True},
             {"ok": True, "result": failed}, [sent],
             ["bootstrap failed at task «env file»: no .env"], 1, True),
            ({"ok": True, "bootstrap_marker": 7, "replaced": True},
             {"ok": True, "result": None},
             [sent, "bootstrap result not seen in 0s — mop list"], [], 0, True),
            ({"ok": True, "bootstrap_marker": 7, "replaced": True},
             {"ok": True, "result": {**rec, "played": False}}, [sent], [], 0, True),
            ({"ok": True, "bootstrap_marker": 7, "replaced": False}, None,
             [sent, "bootstrap not played: the spec is unchanged, "
                    "Nomad kept the running allocation"], [], 0, False),
            ({"ok": True}, None, [sent], [], 0, False)):
        seen = []

        def cc(verb, **kw):
            seen.append(verb)
            return reply if verb == "update" else result
        with patched_env(MOP_SERVER_LAN="192.0.2.1"), \
                patched(lib, guard=lambda name: {"ok": True, "meta": {"origin": "git@h:g/mop.git"}}), \
                patched(bus, call_cluster=cc, login=lambda: "anton"), \
                patched(_common, workspace_text=lambda origin: ("", NOFILE_334),
                        BOOTSTRAP_WAIT=0):
            out, err, code = run_command(update.main, ["pu-mop-1"])
        c.expect(f"#334 update, reply {reply}, result {result and result.get('result')}: "
                 f"stdout, stderr, exit code, asked",
                 (out.splitlines(), [lib.plain(l) for l in err.splitlines()], code,
                  "bootstrap_result" in seen),
                 (want_out, want_err, want_code, want_asked))

# ── #503: mop update без origin — TypeError в workspace_text ───────────
# HYPOTHESIS: рефакторинг #459 (00b888f) уронил подстановку прежнего
# репозитория из спеки: командлет зовёт workspace_text(None), cwd_origin()
# == None ложно, выполнение проваливается в manifest.fetch(None) и падает
# TypeError в project_of -- папета нельзя перерегистрировать на прежнем
# репозитории без явного origin.
# SOLUTION: командлет восстанавливает контракт до 00b888f — origin из
# meta.origin прочитанной перилами спеки (lib.guard уже отдал её); в
# workspace_text уезжает фактическая строка, не None. ops.update и
# web-воркер origin=None понимают и не задеты; recycle свой origin берёт
# сам внутри общей функции.
def check_update_origin_503(c):
    from mop.cli.core import update
    from mop.common import bus
    spec = {"ok": True, "meta": {"origin": "git@h:g/mop.git", "branch": "master"}}
    seen_wt, seen_rpc = [], []

    def wt(origin):
        seen_wt.append(origin)
        return "", {"source": "origin", "commit": "abc", "dirty": False,
                    "tasks": 0, "present": False}

    def cc(verb, **kw):
        seen_rpc.append((verb, kw))
        # Спека не изменилась: no-op -- accepted, прогона не будет.
        return {"ok": True, "replaced": False, "owner_note": None}
    with patched_env(MOP_SERVER_LAN="192.0.2.1"), \
            patched(lib, guard=lambda name: spec), \
            patched(bus, call_cluster=cc, login=lambda: "anton"), \
            patched(_common, workspace_text=wt, BOOTSTRAP_WAIT=0):
        out, err, code = run_command(update.main, ["pu-mop-1"])
    c.check("#503 update without origin passes the spec's origin to workspace_text",
            seen_wt == ["git@h:g/mop.git"], seen_wt)
    c.check("#503 update without origin sends the same origin to the service",
            seen_rpc and seen_rpc[0][1].get("origin") == "git@h:g/mop.git",
            seen_rpc)
    c.check("#503 the command no longer crashes (exit 0, clean stderr)",
            code == 0 and not err.strip(), (code, err))
    # Явный origin по-прежнему едет как есть — подстановка только для
    # пропущенного аргумента.
    seen_wt.clear(), seen_rpc.clear()
    with patched_env(MOP_SERVER_LAN="192.0.2.1"), \
            patched(lib, guard=lambda name: spec), \
            patched(bus, call_cluster=cc, login=lambda: "anton"), \
            patched(_common, workspace_text=wt, BOOTSTRAP_WAIT=0):
        run_command(update.main, ["pu-mop-1", "git@h:g/other.git"])
    c.check("#503 an explicit origin overrides the spec's",
            seen_wt == ["git@h:g/other.git"]
            and seen_rpc[0][1].get("origin") == "git@h:g/other.git",
            (seen_wt, seen_rpc))
    # Спека без origin — не папет: прежний отказ командлета (usage), не
    # TypeError в глубине manifest.
    seen_wt.clear(), seen_rpc.clear()
    bare = {"ok": True, "meta": {"branch": "master"}}
    with patched_env(MOP_SERVER_LAN="192.0.2.1"), \
            patched(lib, guard=lambda name: bare), \
            patched(bus, call_cluster=cc, login=lambda: "anton"), \
            patched(_common, workspace_text=wt, BOOTSTRAP_WAIT=0):
        out, err, code = run_command(update.main, ["pu-mop-1"])
    # usage -- SystemExit(текст): в процессе кодом выхода становится сам
    # текст, его печатает интерпретатор (как у #263). Существенно: отказ
    # ДО workspace_text -- None туда больше не уезжает.
    c.check("#503 a spec without origin refuses before workspace_text",
            not seen_wt and bool(code)
            and "no origin in the spec" in str(code), (seen_wt, code, out, err))
# STATUS: FIXED — see #503


# ── #345: сдавшийся bootstrap в `mop list` и `mop tail` ────────────────
# HYPOTHESIS: у остановленного джоба нет работающей аллокации, а после GC
# Nomad -- никакой: failing_row по stderr аллокации ничего не скажет, и
# папет читался бы «dead» без причины.
# SOLUTION: сервис кластера кладёт gave_up (строку из записи итога) в
# элемент ростера и в ответ alloc; строка ростера -- FAILED с ней, без
# хвоста «(N restarts, …)» Nomad; `mop tail`/attach говорят то же.
# STATUS: FIXED — see #345
def check_gave_up_rows_345(c):
    from mop.common import bus, puppets
    from mop.common.state import State
    text = ("bootstrap gave up after 3 attempts at task «bootstrap : sync»: "
            "Group `cloud` is not defined — fix .mop/bootstrap.yaml, then mop update")
    job = {"ID": "pu-rudesktop-8", "Type": "service", "Status": "dead",
           "Meta": {"origin": "git@h:g/rudesktop.git", "llm": "claude"}}
    stopped = {"ID": "a1", "JobID": job["ID"], "NodeName": "agent1",
               "ClientStatus": "complete", "DesiredStatus": "stop"}
    for alloc in (None, stopped):
        item = {"job": job, "alloc": alloc, "error": None, "task": None, "reason": None,
                "state": None, "kind": None, "gave_up": text}
        row = puppets.rows_from([item])[0]
        c.expect(f"#345 a gave-up puppet's row (alloc {alloc and alloc['ClientStatus']})",
                 (row.alloc_status, row.state, row.kind),
                 ("failed", str(State("failing", text)), "failing"))
        c.check("#345 the row reads FAILED: bootstrap gave up …",
                row.state.startswith("FAILED: bootstrap gave up after 3 attempts"), row.state)
        with patched(bus, call_cluster=lambda verb, **kw: {"ok": True, "alloc": alloc,
                                                            "driver": None, "gave_up": text}):
            try:
                puppets.running(job["ID"])
                c.fail("#345 running() of a gave-up puppet must refuse")
            except LookupError as e:
                c.expect("#345 mop tail/attach name the give-up",
                         str(e), f"{job['ID']}: {State('failing', text)}")
        # doctor: диагноз без действия -- ни stop, ни restart аллокации.
        with patched(puppets, roster=lambda stale=False: [dict(item)]):
            got = [i for i in puppets.diagnose() if i["name"] == job["ID"]]
        c.expect(f"#345 doctor names the give-up with no action (alloc {alloc and 'stopped'})",
                 [(i["diagnosis"], i["action"]) for i in got],
                 [(str(State("failing", text)), None)])


# ── #350: справочник CLI из докстрингов командлетов ───────────────────
# HYPOTHESIS: полного справочника подкоманд нет нигде: README держит десять
# устаревших строк, `mop` без аргументов -- однострочники. Второй текст
# рядом с кодом разошёлся бы с ним так же, как README.
# SOLUTION: cli.render_reference -- чистая функция «[(каталог, слова,
# докстринг)] -> Markdown» с частями клиент/сервер/служебное; cli.reference()
# собирает её вход AST-разбором (docstring), без импорта командлетов;
# `mop dev docs` пишет docs/CLI.md, проверка сверяет его с регенерацией.
# Пустой докстринг -- отказ: новая команда без доки красит CI, а не молчит.
# STATUS: FIXED — see #350
ENTRIES_350 = [
    ("core", ["add"], "create a puppet: mop add [origin]\n\n  --llm P   the profile"),
    ("pool", ["doctor"], "pool diagnostics: mop doctor [--fix]"),
    ("cred", ["cred"], "credentials of LLM providers"),
    ("cred", ["cred", "add"], "add a key: mop cred add <name>"),
    ("server", ["server"], "the installation's server"),
    ("server", ["server", "deploy"], "deploy: mop server deploy [--check]\n\nRolls ```x``` out."),
    ("service", ["agent"], "node agent: answers the bus"),
]


def check_reference_render_350(c):
    render = getattr(cli, "render_reference", None)
    if not c.check("#350 cli.render_reference exists", render is not None):
        return
    md = render(ENTRIES_350)
    heads = [l for l in md.splitlines() if l.startswith("## ")]
    c.expect("#350 parts: client, server, service, in this order",
             heads, ["## Client", "## Server", "## Service"])
    client, server, service = (md.split("\n## ")[i] for i in (1, 2, 3))
    for part, words in [(client, "mop add"), (client, "mop doctor"),
                        (client, "mop cred"), (client, "mop cred add"),
                        (server, "mop server"), (server, "mop server deploy"),
                        (service, "mop agent")]:
        c.check(f"#350 {words} in its part", f"\n#### {words}\n" in part)
    c.check("#350 the server group is not client", "mop server" not in client.replace(
        "mop server deploy [--check]", ""))
    for _, _, doc in ENTRIES_350:
        c.check(f"#350 the whole docstring, verbatim: {doc[:30]!r}", doc in md)
    # Три обратные кавычки внутри докстринга не закрывают блок раньше времени.
    c.check("#350 a fence longer than any backtick run inside",
            "\n````text\ndeploy: mop server deploy" in md)
    c.expect("#350 the render is deterministic", render(ENTRIES_350), md)


def check_reference_empty_350(c):
    render = getattr(cli, "render_reference", None)
    if not c.check("#350 cli.render_reference exists", render is not None):
        return
    try:
        render(ENTRIES_350 + [("core", ["mute"], ""), ("dev", ["dev", "x"], "  ")])
        c.fail("#350 an empty docstring must refuse, not render")
    except ValueError as e:
        c.check(f"#350 the refusal names every command: {e}",
                "mop mute" in str(e) and "mop dev x" in str(e))


def check_reference_committed_350(c):
    reference = getattr(cli, "reference", None)
    if not c.check("#350 cli.reference exists", reference is not None):
        return
    try:
        md = reference()
    except ValueError as e:
        c.fail(f"#350 every commandlet needs a docstring: {e}")
        return
    path = os.path.join(ROOT, "docs", "CLI.md")
    try:
        with open(path) as f:
            committed = f.read()
    except OSError:
        committed = None
    c.check("#350 docs/CLI.md matches its regeneration: run `mop dev docs` and commit it",
            committed == md)
    # Реальный каталог -- целиком: каждая команда и глагол, и ни одного
    # переходного имени из LEGACY.
    for words in (["add"], ["tail"], ["server", "deploy"], ["server", "user", "add"],
                  ["dev", "docs"], ["mcp"]):
        c.check(f"#350 the reference holds mop {' '.join(words)}",
                f"\n#### mop {' '.join(words)}\n" in md)
    for key in cli.LEGACY:
        words = " ".join(key) if isinstance(key, tuple) else key
        c.check(f"#350 no legacy name mop {words}", f"\n#### mop {words}\n" not in md)


# ── #432: справка командлетов и групп driver/server/service — суть, не история ──
# HYPOTHESIS: докстринги этих командлетов выросли в рассказы (deploy 302
# слова, sweep 211, web 198, doctor 194, группы 114-160) и уезжают целиком
# в docs/CLI.md и в ответ usage; редкая деталь принадлежит docs/, а не help.
# Отказы в тех же файлах повторяли имя команды внутри её собственного
# сообщения.
# SOLUTION: заголовок с синтаксисом и один-два абзаца сути — что делает,
# где запускать, что ломает; группы перечисляют глаголы без пересказа их
# докстрингов. Отказ — одна строка: причина и лечение. Ключи и
# предупреждения о потере работы сохранены дословно.
# STATUS: FIXED — see #432
import ast
import os
import uuid


def _owned_help():
    """{(путь: докстринг)} командлетов и групп #432: driver, server,
    service, включая __init__ групп (server/cluster, server/user,
    server/bootstrap), без mcp.py (проза мастера) и скрытых _*.py.
    Разбор AST, без импорта: как сам справочник (#350)."""
    from mop import cli
    root = os.path.dirname(cli.__file__)
    out = {}
    for group in ("driver", "server", "service"):
        base = os.path.join(root, group)
        out[os.path.join(base, "__init__.py")] = cli.docstring(os.path.join(base, "__init__.py")) or ""
        for dirpath, _, names in sorted(os.walk(base)):
            if "__pycache__" in dirpath:
                continue
            for n in sorted(names):
                if not n.endswith(".py") or n == "mcp.py":
                    continue
                # __init__.py секции уже взят выше; скрытые _*.py -- не
                # команды. Вложенный __init__.py -- help группы
                # (server/cluster, server/user, server/bootstrap).
                if n.startswith("_") and n != "__init__.py":
                    continue
                if n == "__init__.py" and dirpath == base:
                    continue
                path = os.path.join(dirpath, n)
                out[path] = cli.docstring(path) or ""
    return out


def check_help_content_432(c):
    """Ужатая справка сохраняет суть: глаголы групп перечислены, ключи и
    предупреждения о потере работы — дословно. Конкретные утверждения
    вместо потолков: будущая необходимая деталь не обязана влезать."""
    failed_before = c.failed
    docs = _owned_help()

    # Группа перечисляет свои глаголы — весь список, до последнего.
    verbs_of = {
        "mop/cli/driver/__init__.py": ("list", "run", "sweep", "clone-work", "build"),
        "mop/cli/server/cluster/__init__.py": ("serve", "check", "users", "reload", "builder", "export"),
        "mop/cli/server/user/__init__.py": ("add", "passwd", "delete", "import"),
        "mop/cli/server/bootstrap/__init__.py": ("serve", "check"),
    }
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    for rel, verbs in verbs_of.items():
        doc = docs.get(os.path.join(root, rel))
        if not c.check(f"#432 the group help exists: {rel}", doc is not None, rel):
            continue
        for v in verbs:
            c.check(f"#432 {rel} lists its verb {v}", v in doc, doc.splitlines()[0][:60])

    # Ключи и предупреждения, которые нельзя было потерять при отжиме.
    must = {
        "mop/cli/server/deploy.py": ("--check", "--skip-pipeline", "--from-ci",
                                     "MOP_DEPLOY_NEEDS_GREEN"),
        "mop/cli/server/doctor.py": ("--fix", "--safe", "silent"),
        "mop/cli/server/sweep.py": ("--dry", "build bodies", "orphan"),
        "mop/cli/server/gc.py": ("--dry", "MOP_GC_FREE_MIN_GB"),
        "mop/cli/driver/build.py": ("--fresh", "--force", "--node"),
        "mop/cli/driver/sweep.py": ("--dry",),
        "mop/cli/driver/clone-work.py": ("exit 0 yes, 3 no", "cannot be read is kept"),
        "mop/cli/server/user/add.py": ("--role", "--projects", "--stdin"),
        "mop/cli/server/user/passwd.py": ("--stdin",),
        "mop/cli/server/pve-facts.py": ("--base", "--project", "--listing"),
        "mop/cli/server/web.py": ("--port", "--bind", "/api/journal"),
        "mop/cli/server/config.py": ("--json",),
        "mop/cli/server/cluster/users.py": ("--reload",),
        "mop/cli/service/agent.py": ("--check",),
    }
    for rel, keys in must.items():
        doc = docs.get(os.path.join(root, rel))
        if not c.check(f"#432 {rel} is in the audit", doc is not None, rel):
            continue
        for k in keys:
            c.check(f"#432 {rel} keeps {k!r}", k in doc, doc.splitlines()[0][:60])

    # Группы и служебные командлеты: без истории и внутренностей.
    # driver: где живёт тело и кто его поднимает -- суть; «Each verb is a
    # module» и job spec/Nomad -- устройство, не помощь.
    drv = docs[os.path.join(root, "mop/cli/driver/__init__.py")]
    c.check("#432 the driver group keeps the node-side note",
            "node" in drv, drv.splitlines()[0][:60])
    for gone in ("module of this package", "job spec", "Nomad picks"):
        c.check(f"#432 the driver group names no {gone!r}",
                gone not in drv, [l for l in drv.splitlines() if gone in l])
    # server: что нужно контроллеру; «old one-word names» -- неверная уже
    # история (имена с тех пор иные).
    srvgrp = docs[os.path.join(root, "mop/cli/server/__init__.py")]
    c.check("#432 the server group keeps what only the server has",
            "inventory" in srvgrp, srvgrp.splitlines()[0][:60])
    for gone in ("old one-word names", "still work for one release"):
        c.check(f"#432 the server group names no {gone!r}",
                gone not in srvgrp, [l for l in srvgrp.splitlines() if gone in l])
    # agent: по одному на узел, вне джобы, --check спрашивает шину;
    # переходные юниты и внутренние импорты -- не помощь.
    ag = docs[os.path.join(root, "mop/cli/service/agent.py")]
    for kept in ("one per node", "--check", "systemd"):
        c.check(f"#432 the agent help keeps {kept!r}",
                kept.lower() in ag.lower(), ag.splitlines()[0][:60])
    for gone in ("python3 -m mop.agent", "mop.cli.lib", "python-nomad"):
        c.check(f"#432 the agent help names no {gone!r}",
                gone not in ag, [l for l in ag.splitlines() if gone in l])
    # bootstrap serve: что делает (при старте песочницы), без subject.
    bs = docs[os.path.join(root, "mop/cli/server/bootstrap/serve.py")]
    c.check("#432 the bootstrap help keeps the at-start note",
            "start" in bs, bs.splitlines()[0][:60])
    c.check("#432 the bootstrap help names no bus subject",
            "server.rpc" not in bs and "mop.*" not in bs,
            [l for l in bs.splitlines() if "rpc" in l])
    # pve-facts: контроллерные факты, без путей плейбуков и модулей.
    pve = docs[os.path.join(root, "mop/cli/server/pve-facts.py")]
    c.check("#432 the pve-facts help keeps the controller note",
            "controller" in pve.lower(), pve.splitlines()[0][:60])
    for gone in ("deploy/pve-build.yml", "mop/driver/pve.py", "pve role"):
        c.check(f"#432 the pve-facts help names no {gone!r}",
                gone not in pve, [l for l in pve.splitlines() if gone in l])
    # callout: кто и как проверяется; без пересказа auth_users и файла.
    co = docs[os.path.join(root, "mop/cli/server/callout.py")]
    c.check("#432 the callout help keeps who is checked",
            "people" in co and "puppets" in co, co.splitlines()[0][:60])
    for gone in ("auth_users", "users.conf"):
        c.check(f"#432 the callout help names no {gone!r}",
                gone not in co, [l for l in co.splitlines() if gone in l])
    # callout: без частоты «at every connect» (спрашивает не каждого) —
    # ровно то, что делает: проверяет людей и папетов.
    c.check("#432 the callout help claims no every-connect frequency",
            "every connect" not in co and "at every connect" not in co,
            [l for l in co.splitlines() if "connect" in l.lower()])
    # agent: никаких утверждений о том, что узел «ничего не берёт» с
    # контроллера — node.env и креды приезжают именно оттуда.
    c.check("#432 the agent help claims no controller-independence",
            "nothing from the controller" not in ag.lower()
            and "takes nothing" not in ag.lower(),
            [l for l in ag.splitlines() if "controller" in l.lower()])

    # cluster: роль и гарантия токена, без subject и Nomad API.
    cl = docs[os.path.join(root, "mop/cli/server/cluster/__init__.py")]
    c.check("#432 the cluster group keeps the token guarantee",
            "token" in cl and "server" in cl, cl.splitlines()[0][:60])
    for gone in ("cluster.rpc", "mop.<project>", "calls the API"):
        c.check(f"#432 the cluster group names no {gone!r}",
                gone not in cl, [l for l in cl.splitlines() if gone in l])
    # web: справка описывает вход как он есть (#416): публичный обзор,
    # логин открывает admin-части (журнал, панель прокси). «No login for
    # the page» — опровергнуто самим маршрутом /api/login.
    webdoc = docs[os.path.join(root, "mop/cli/server/web.py")]
    c.check("#432 the web help names the login (it exists, #416)",
            "/api/login" in webdoc and "login" in webdoc.lower(), webdoc[:60])
    c.check("#432 the web help says the admin gate",
            "admin" in webdoc, webdoc.splitlines()[0][:60])
    c.check("#432 the web help claims no network boundary",
            "LAN" not in webdoc and "without login" in webdoc,
            [l for l in webdoc.splitlines() if "login" in l.lower()][:2])
    c.check("#432 the web help claims no login",
            "No login" not in webdoc, [l for l in webdoc.splitlines() if "login" in l.lower()][:2])
    # deploy: справка говорит о команде, не об устройстве прогона: имя
    # плейбука, заголовок ansible, «реестр» и внутренности --from-ci —
    # не помощь вызывающему. Поведение флагов и последствия — остаются.
    dep = docs[os.path.join(root, "mop/cli/server/deploy.py")]
    # deploy: условие мягкого выхода --from-ci -- ожидание гейта, а не
    # безусловное обещание; и новый job «deploys it».
    c.check("#432 the deploy help ties the soft exit to the gate's wait",
            'answer would be "wait"' in dep and "exits 0" in dep,
            [l for l in dep.splitlines() if "wait" in l or "exits 0" in l])
    c.check("#432 the deploy help keeps no redundant rollout wording",
            "rolls the rollout" not in dep, dep.splitlines()[-1])
    for gone in ("site.yml", "PLAY RECAP", "registry"):
        c.check(f"#432 the deploy help names no {gone!r}",
                gone not in dep, [l for l in dep.splitlines() if gone in l])
    for kept in ("--check", "--diff", "--skip-pipeline", "--from-ci",
                 "MOP_DEPLOY_NEEDS_GREEN", "default branch"):
        c.check(f"#432 the deploy help keeps {kept!r}", kept in dep,
                dep.splitlines()[0][:60])

    # Серверная группа всё ещё говорит, где живут её команды.
    srv = docs[os.path.join(root, "mop/cli/server/__init__.py")]
    c.check("#432 the server group keeps the controller-only note",
            "controller" in srv, srv.splitlines()[0][:60])
    # Справка говорит о команде, не о внутренностях пакета: пути модулей
    # и Nomad-внутренности — не помощь вызывающему.
    for rel, doc in docs.items():
        if not doc:
            continue     # service/__init__.py — технический, не команда
        rel_docs = os.path.relpath(rel, root)
        # Модульные пути — нигде; docs/*.md и web/dist — только не в
        # пересказе usage самих web и deploy (рассказ о справке — в docs/).
        no_doc_ref = rel_docs in ("mop/cli/server/web.py", "mop/cli/server/deploy.py")
        c.check(f"#432 {rel_docs} names no internal path",
                "mop/server/" not in doc and "mop/cli/" not in doc
                and not (no_doc_ref and ("docs/" in doc or "web/dist" in doc)),
                doc.splitlines()[0][:60])
    # Капс — не акцент (CLAUDE.md): справка — тоже вывод программы. Речь
    # об эмфазе («NOT treated»), а не о статус-токенах и именах настроек:
    # PLAY RECAP, MOP_*, VMID, LAN — легальные капс по смыслу.
    EMPHASIS = ("NOT", "ALWAYS", "NEVER", "MUST", "ONLY", "WARNING", "DANGER")
    for rel, doc in docs.items():
        if not doc:
            continue
        caps = [w.strip(".,:;()[]") for w in doc.split()
                if w.strip(".,:;()[]") in EMPHASIS]
        c.check(f"#432 {os.path.relpath(rel, root)}: no caps emphasis",
                not caps, caps)
    return c.failed == failed_before


def check_refusals_432(c):
    """Отказы изменённых команд: одна мысль, лечение названо, без повтора
    собственного префикса команды. Только изменённые строки — конкретные
    утверждения."""
    failed_before = c.failed
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

    def refusals_of(path):
        """Текстовые части отказов (lib.fail/sys.exit) файла — по содержимому
        вызова, без привязки к номерам строк."""
        out = []
        for node in ast.walk(ast.parse(open(path).read())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in ("fail", "exit") or not node.args:
                continue
            a = node.args[0]
            if isinstance(a, ast.Constant):
                out.append(a.value)
            elif isinstance(a, ast.JoinedStr):
                out.append("".join(x.value for x in a.values
                                   if isinstance(x, ast.Constant)))
        return out

    def one(path, marker):
        """Отказ, содержащий marker; ровно один такой."""
        got = [t for t in refusals_of(os.path.join(root, path)) if marker in t]
        if not c.check(f"#432 {path} has the refusal with {marker!r}", len(got) == 1, got):
            return None
        return got[0]

    # deploy: лишний аргумент -- совет зарегистрировать проект, точной
    # командой, без повтора собственного имени.
    got = one("mop/cli/server/deploy.py", "takes no arguments")
    c.check("#432 the no-arguments refusal cures with the exact command",
            got is not None and "mop project add" in got
            and "mop server deploy takes" not in got, got)
    # deploy: точный синтаксис команды-зависимости.
    got = one("mop/cli/server/deploy.py", "no ansible-playbook")
    c.check("#432 the missing-ansible refusal names the exact command",
            got is not None and "mop server setup" in got, got)
    # deploy: недоступные машины -- одна строка с лечением, и лечение --
    # та же команда, а не «a deploy».
    got = one("mop/cli/server/deploy.py", "did not answer")
    c.check("#432 the unanswered-machines refusal stays one sentence and exact",
            got is not None and "\n" not in got and "mop server deploy again" in got, got)
    # deploy: подсказка входа называет точную команду, не «user verbs».
    got = one("mop/cli/server/deploy.py", "nobody on the bus")
    c.check("#432 the login-hint refusal names the exact command",
            got is not None and "mop server user" in got and "user` verbs" not in got, got)
    # cluster/users: лечение — точная команда, без пересказа «a deploy».
    got = one("mop/cli/server/cluster/users.py", "no base users")
    c.check("#432 the base-users refusal names the exact command",
            got is not None and "mop server deploy" in got, got)
    got = one("mop/cli/server/cluster/users.py", "changed: nats takes it")
    c.check("#432 the callout-restart refusal names the exact way",
            got is not None and "systemctl restart" in got, got)
    return c.failed == failed_before


# HYPOTHESIS: environment() исполняется до клиентского командлета и читает
# installation .env; дочерний mop mcp стартует тем же диспетчером.
# SOLUTION: диспетчер выбирает клиентский источник до environment и run,
# а dev/server сохраняют установочный файл.
# RESULT: list, join и mcp не открывают .env; dev и server сохраняют источник.
# STATUS: FIXED — see #396
def check_dispatch_sources_396(c):
    from mop.common import config
    opened = []
    def read_installation():
        opened.append(True)
        return {"MOP_LOCALE": "C.UTF-8"}
    def command(module):
        def main(argv):
            config.get("MOP_LOCALE")
            return 0
        return main
    from mop.cli import _control
    # #642: converted entrypoints обходят environment/context целиком. Здесь
    # проверяем выбор ветви dispatcher; реальный fake/TLS покрыт consumers.
    routed = []
    def invoke(words, argv, **kw):
        routed.append(words)
        return 0
    with patched(config, _load=read_installation), patched(cli, command=command), \
            patched(_control, invoke=invoke):
        for name, module, client in (("list", "mop.cli.core.list", True),
                                     ("login", "mop.cli.pool.login", True),
                                     ("mcp", "mop.cli.service.mcp", True),
                                     ("dev", "mop.cli.dev", False),
                                     ("server", "mop.cli.server", False)):
            with patched(cli, resolve=lambda *a: (module, [])):
                opened.clear()
                routed.clear()
                _, _, code = run_command(cli.main, [name])
                c.expect(f"#642 {name} converted dispatch selection", routed,
                         [name] if client else [])
                c.expect(f"#396 {name} dispatch succeeds", code, 0)
                c.check(f"#396 {name} installation source selection",
                        (not opened) if client else bool(opened), opened)


# HYPOTHESIS: старые команды и источник ключа остались в руководстве даже
# после миграции join. Справочник генерируется из докстрингов командлетов.
# SOLUTION: руководство и docstring описывают тот же рабочий клиентский путь.
# RESULT: действующие инструкции описывают join, сервер и закрытые источники.
# STATUS: FIXED — see #401
def check_current_docs_401(c):
    files = ("README.md", ".env.example", "CLAUDE.md", "docs/PROXY.md",
             "docs/DRIVER.md", "docs/MCP.md", "docs/BUS.md", "docs/CLI.md")
    removed = ("mop llm", "mop cred", "--llm", "--cred",
               "MOP_DEFAULT_LLM", "Z_AI_KEY", "ANTHROPIC_API_KEY")
    for name in files:
        text = open(os.path.join(ROOT, name), encoding="utf-8").read()
        for obsolete in removed:
            c.check(f"#401 {name} has no obsolete {obsolete}", obsolete not in text)
    # HYPOTHESIS: login после #533 пишет connection.json, а прежнее
    # общее условие «client.json или mop join» выполнялось и на
    # устаревшем тексте — оно не отличало новый контракт от старого
    # имени. SOLUTION: явное условие — новое имя профиля и для README,
    # и для docs/CLI.md (легаси-пара в README описана верно и остаётся).
    # RESULT: RED на 7259e30^ (README:107 и :180 ещё говорят
    # client.json), зелёная на 7259e30, где README называет
    # connection.json.
    for name in ("README.md", "docs/CLI.md"):
        text = open(os.path.join(ROOT, name), encoding="utf-8").read()
        c.check(f"#401 #533 {name} names the client profile connection.json",
                "connection.json" in text)
    # HYPOTHESIS: login после #533 пишет connection.json, а PROXY.md
    # переехал на новое имя — общее условие «client.json или mop join»
    # для него больше не может выполняться и краснит честный текст.
    # SOLUTION: для PROXY.md контракт — новое имя профиля; README и
    # docs/CLI.md остаются на прежнем условии до их переделки (pu-mop-1).
    # RESULT: RED на 0efd5c0 (PROXY.md ещё говорит client.json),
    # зелёная на 679ceec, где PROXY.md говорит connection.json.
    text = open(os.path.join(ROOT, "docs/PROXY.md"), encoding="utf-8").read()
    c.check("#401 #533 docs/PROXY.md names the client profile connection.json",
            "connection.json" in text)
    for name, stale in (("docs/CLI.md", "local .env is exactly"),
                        ("docs/PROXY.md", "мастерских копий"),
                        ("docs/BUS.md", "слоя — дефолт установки из `.env`"),
                        (".env.example", "MOP_PROXY_KEY=")):
        text = open(os.path.join(ROOT, name), encoding="utf-8").read()
        c.check(f"#401 {name} does not claim installation env is a client source",
                stale not in text)
    c.check("#401 mop help does not promise client fallback to .env",
            "with none of them, from .env" not in cli.usage())
    # STATUS: FIXED — see #405. Единый вход не просит секрет прокси отдельно.
    for name, obsolete in (("README.md", "ключ, полученный от администратора"),
                           ("docs/PROXY.md", "передают закрытым"),
                           (".env.example", "клиент\n# вводит"),
                           ("docs/CLI.md", "key are asked for separately")):
        text = open(os.path.join(ROOT, name), encoding="utf-8").read()
        c.check(f"#405 {name} does not require manual proxy key entry", obsolete not in text)


# HYPOTHESIS: вход остаётся только как join, а старые вызовы после rename
# потеряют команду. Псевдоним не должен попадать в каталог новых команд.
# RESULT: новая команда в каталоге, старое имя работает только как alias.
# STATUS: FIXED — see #407
def check_login_name_407(c):
    cat = cli.catalog(cli.scan())
    c.check("#407 login is the current command", "login" in cat and "join" not in cat)
    c.expect("#407 old join is a transitional alias",
             cli.unalias(["join", "--server", "srv.test"]),
             ["login", "--server", "srv.test"])
    c.check("#407 reference documents login rather than alias",
            "#### mop login" in cli.reference() and "#### mop join" not in cli.reference())


# HYPOTHESIS: общая уборка и doctor остаются доступными клиенту и мастеру.
# SOLUTION: серверные командлеты без клиентского каталога и MCP.
# RESULT: мастер не видит общие команды, клиент отказывает до шины;
# установленный контроллер проходит проверку, dry/fix проверены отдельно.
# STATUS: FIXED — see #409
def check_server_maintenance_409(c):
    cat = cli.catalog(cli.scan())
    verbs = cli.verbs().get("server", {})
    for name in ("gc", "doctor", "disk", "sweep"):
        c.check(f"#409 {name} is not a client command", name not in cat)
        c.check(f"#409 server {name} exists", name in verbs)
    tools = [name for name, _, path in cli.tool_commands(cli.scan(), cli.verbs())
             if cli.declared(path) is not None]
    for name in ("gc", "doctor", "disk", "sweep"):
        c.check(f"#409 server {name} is not a master MCP tool", f"server_{name}" not in tools)
    from mop.cli.server import _maintenance
    from mop.cli.server import disk, doctor, gc, sweep
    from _lib import patched
    for name, command in (("disk", disk), ("doctor", doctor), ("gc", gc), ("sweep", sweep)):
        with patched(_maintenance.os.path, isfile=lambda path: False):
            try:
                command.main([])
                c.fail(f"#409 client can run server {name}")
            except RuntimeError as e:
                c.check(f"#409 server {name} refuses before bus", "server" in str(e), str(e))
    called = []
    with patched(_maintenance.os.path, isfile=lambda path: True), \
            patched(_maintenance.os, access=lambda path, mode: True):
        _maintenance.only_server(lambda argv: called.append(argv))(["--dry"])
    c.expect("#409 controller enters maintenance command", called, [["--dry"]])
    with patched(_maintenance.os.path, isfile=lambda path: True), \
            patched(_maintenance.os, access=lambda path, mode: False):
        try:
            _maintenance.only_server(lambda argv: c.fail("#409 unreadable credentials run"))([])
            c.fail("#409 unreadable server credentials accepted")
        except RuntimeError:
            pass



# ── #431: разбор операторских команд ────────────────────────────────────
# HYPOTHESIS: mop tail -n x ронял командлет трассой ValueError: parse
# кормил int() сырой аргумент, когда соседний stat --days делает то же
# через usage. Оператор видит стек вместо подсказки.
# SOLUTION: разбор tail возвращает usage для -n без числа и для не-числа;
# это тот же контракт, что у stat --days.
# RESULT: пять плохих форм -- usage (SystemExit с докстрингом), здоровые
# -n 20 / -n20 / -f -- прежние значения; stat --days не изменился.
# STATUS: FIXED — see #431
def check_tail_parse_431(c):
    from mop.cli.core import stat, tail
    # Здоровые формы не меняются.
    c.expect("#431 tail parses its flags", tail.parse(["pu-mop-1", "-n", "20"]),
             ("pu-mop-1", 20, False))
    c.expect("#431 tail -n=N", tail.parse(["pu-mop-1", "-n20"]), ("pu-mop-1", 20, False))
    c.expect("#431 tail -f", tail.parse(["pu-mop-1", "-f"]), ("pu-mop-1", 10, True))
    for bad in (["pu-mop-1", "-n", "x"], ["pu-mop-1", "-n0x"], ["pu-mop-1", "-n", "-f"],
                ["pu-mop-1", "-n"]):
        try:
            got = tail.parse(bad)
            c.fail(f"#431 tail {bad} must be a usage refusal, parsed {got!r}")
        except ValueError as e:
            c.fail(f"#431 tail {bad}: a trace, not a usage refusal", str(e))
        except SystemExit:
            c.check(f"#431 tail {bad}: usage", True)
    # #642: новый stat отказывает bad input в command boundary до HTTPS.
    for bad in (["--days", "x"], ["--days"]):
        out, err, code = run_command(stat.main, bad)
        c.expect(f"#642 stat {bad} refuses without a trace or query",
                 (out, err, code), ('', 'invalid command arguments\n', 1))


CONCISE_431 = {
    "mop.cli.core.landing": (
        ("syntax", "mop landing [take <puppet>|give|show] [--force]"),
        ("keeps", "take it from a gone master"),
        ("keeps", "one puppet between merge and push"),
        ("gone", "the token is kept by the cluster service"),
    ),
    "mop.cli.core.master": (
        # #662: `mop master` -- только лаунчер мастер-роли; локальный
        # запуск claude снят (breaking change, установки пересоздаются).
        ("syntax", "mop master [git-origin]"),
        ("keeps", "a new puppet of the project is registered"),
        ("gone", "--local"),
        ("gone", "resumes the master's last session"),
        ("gone", "goes to claude as-is"),
        ("gone", "one master: a master sees"),
        ("gone", "mop/common/creds.py"),
        ("gone", "mop mcp, spawned"),
    ),
    "mop.cli.core.delete": (
        # #642: delete -- HTTP-командлет; host-клон остаётся, перехват не подключён.
        ("syntax", "mop delete <id>"),
        ("keeps", "a host body keeps its clone"),
        ("keeps", "--force takeover is explicitly unsupported"),
        ("gone", "a body nobody owns is a running container"),
    ),
    "mop.cli.pool.login": (
        ("syntax", "mop login [login] [--server host] [--https-port port]"),
        ("keeps", "asked without echo"),
        ("keeps", "fingerprint"),
        ("keeps", "MOP_HTTPS_PORT"),
        ("gone", "personal, authenticated"),
        ("gone", "no public HTTP endpoint"),
        ("gone", "/nats"),
        ("gone", "/v1/models"),
    ),
    "mop.cli.node": (
        ("syntax", "mop node [drain|up|forget <name>]"),
        ("keeps", "refuses while anything still"),
        ("gone", "never hears of it again"),
        ("gone", "on TERM"),
    ),
    "mop.cli.project": (
        ("syntax", "mop project [add <git-origin>|delete <name>|list]"),
        ("keeps", "puppet limit"),
        ("gone", "Registering used to be a side effect"),
        ("gone", "the registry lives on the server"),
        ("gone", "put it on the bus"),
        ("gone", "out of the registry"),
    ),
    "mop.cli.secret": (
        ("syntax", "mop secret file|var add|list|remove"),
        ("keeps", "kept on the server"),
    ),
    "mop.cli.project.delete": (
        ("keeps", "Refuses while the project still has puppets"),
        ("keeps", "The repository is not touched"),
        ("gone", "cluster service"),
        ("gone", "NATS user"),
        # Подсказка обязана называть папетов, а не «name» проекта.
        ("keeps", "delete its puppets first"),
    ),
    "mop.cli.project.list": (
        ("syntax", "mop project list [--cursor VALUE] [--page-size N]"),
        ("gone", "cluster service"),
        ("gone", "registry"),
    ),
    "mop.cli.core.add": (
        # #662: безролевой add снят (breaking change): только --role.
        ("syntax", "mop add [git-origin] --role <role>"),
        ("keeps", "the creation carries a request_id"),
        ("gone", "Nomad decides placement"),
        ("gone", "instead of a legacy puppet"),
    ),
    "mop.cli.core.tail": (
        ("syntax", "mop tail <name> [-n N] [-f]"),
        ("gone", "tmux"),
    ),
    "mop.cli.core.attach": (
        ("syntax", "mop attach <name>"),
        ("keeps", "over ssh"),
        ("gone", "the only subcommand"),
    ),
}


def check_concise_help_431(c):
    import importlib
    import re as _re
    for mod, rows in CONCISE_431.items():
        doc = (importlib.import_module(mod).__doc__ or "").strip()
        # Переносы строк и регистр не должны решать судьбу утверждения:
        # свернутые пробелы, нижний регистр -- и фраза ищется как фраза.
        flat = _re.sub(r"\s+", " ", doc).lower()
        for kind, text in rows:
            what = f"#431 {mod.rsplit('.', 1)[-1]} help {kind}: {text[:40]!r}"
            hit = _re.sub(r"\s+", " ", text).lower() in flat
            if kind in ("syntax", "keeps"):
                c.check(what, hit, doc[:120])
            else:
                c.check(what, not hit, "still present")



# HYPOTHESIS: отказ MCP-позиционного аргумента, похожего на флаг,
# включает repr самого значения; значение могло быть приватным.
# SOLUTION: отказ оставляет имя поля и причину, но не входное значение.
# RESULT: синтетический маркер не попал в отказ, поле и причина остались.
# STATUS: FIXED — see #438
def check_mcp_positional_refusal_438(c):
    marker = "-SYNTHETIC_PRIVATE_VALUE"
    args = [{"name": "origin", "type": "string", "required": True}]
    try:
        cli.tool_argv(args, {"origin": marker})
    except ValueError as e:
        message = str(e)
        c.check("#438 reject a flag-like positional without echoing its value",
                "origin" in message and "flag" in message and marker not in message,
                message)
    else:
        c.fail("#438 flag-like positional must still be rejected")



# HYPOTHESIS: отказ deploy с лишним аргументом выводит argv[0] дважды,
# включая URL с секретом; команда исправления не требует значения в отказе.
# SOLUTION: отказ называет синтаксис и плейсхолдер вместо сырых данных.
# RESULT: отказ не содержит маркер, имя исправляющей команды и код сохранились.
# STATUS: FIXED — see #439
def check_deploy_argument_refusal_439(c):
    import shutil
    from mop.common import config
    from mop.cli.server import deploy

    marker = "-SYNTHETIC_PRIVATE_VALUE"
    with patched(shutil, which=lambda name: "/usr/bin/ansible-playbook"), \
            patched(config, require=lambda: None):
        out, err, code = run_command(deploy.main, [marker])
    c.expect("#439 extra argument exits with a refusal", code, 1)
    c.check("#439 extra argument is not echoed in either stream",
            marker not in out and marker not in err, repr((out, err)))
    c.check("#439 refusal shows the correcting command with a placeholder",
            "mop project add <git-origin>" in err and "takes no arguments" in err,
            repr(err))


# ─── #475: смешанный провал не прячется за недоступными узлами ───────────
# ansible-core (2.21, plugins/strategy/__init__.py) выбирает код игры
# цепочкой: не RUN_OK -- вернуть его; ДАЛЕЕ недоступные -- 4; ДАЛЕЕ
# провалившиеся -- 2. Недоступные стоят РАНЬШЕ провалившихся, и
# _tqm._unreachable_hosts копится по играм, поэтому прогон с failed=1 и
# unreachable=1 в recap приходит кодом 4 -- тем же, что и чистый #87.
# Равенство `rc != _play.UNREACHABLE` в deploy.main читала его как «узлы
# молчат»: печатала «everything that did is configured», собирала креды,
# показывала ростер и выходила нулём -- CI-джоба deploy:mop была зелёной
# при красном localhost в логе (проверено живым прогоном ansible-core
# 2.21.4: чистый unreachable -- 4, чистый failed -- 2, смешанный в одной
# игре и разнесённый по играм -- 4).
# Код возврата один говорит лишь об недоступных; провал виден только в
# PLAY RECAP, строкой `host ... failed=N ... N>0`. Паттерн -- #333
# (bootstrap.failed_task).
RECAP_MIXED = ("PLAY RECAP *********************************************************************\n"
               "localhost                  : ok=41   changed=0    unreachable=0    failed=1   "
               "skipped=3   rescued=0    ignored=0\n"
               "wate-wsl                   : ok=0    changed=0    unreachable=1    failed=0   "
               "skipped=0   rescued=0    ignored=0\n")
RECAP_UNREACHABLE = ("PLAY RECAP *********************************************************************\n"
                     "wate-wsl                   : ok=0    changed=0    unreachable=1    failed=0   "
                     "skipped=0   rescued=0    ignored=0\n")
RECAP_OK = ("PLAY RECAP *********************************************************************\n"
            "localhost                  : ok=41   changed=0    unreachable=0    failed=0   "
            "skipped=3   rescued=0    ignored=0\n")


def check_deploy_mixed_outcome_475(c):
    """HYPOTHESIS: deploy различал коды ansible равенством с UNREACHABLE,
    и смешанный прогон (failed=1 + unreachable=1) приходил кодом 4 -- как
    чистый #87. SOLUTION: вердикт по итогам прогона, а не по одному коду:
    провал в PLAY RECAP делает прогон красным при любом коде, чистый
    unreachable остаётся веткой #87. STATUS: FIXED — see #475"""
    from mop.cli.server import _play

    # Чистая классификация: (код ansible, recap) -> вердикт.
    for what, rc, recap, want in (
            # Смешанный случай ansible маскирует кодом 4 -- как в #475.
            ("a mixed run, unreachable first in the playbook", 4, RECAP_MIXED, "failed"),
            ("the both-bits code", 6, RECAP_MIXED, "failed"),
            ("the failed code", 2, RECAP_MIXED, "failed"),
            # Ветку #87 не ослабляем: чистый unreachable -- не красный прогон.
            ("an unreachable-only run, the #87 case", 4, RECAP_UNREACHABLE, "unreachable"),
            # Сухой прогон recap не печатает; код 4 без провала в логе --
            # тоже #87 (вывод могли и не собрать).
            ("no recap in hand", 4, "", "unreachable"),
            ("a clean run", 0, RECAP_OK, "ok"),
            ("a recap without failures says ok whatever the code", 0, RECAP_OK, "ok")):
        got = _play.verdict(rc, recap)
        c.expect(f"#475 verdict of rc={rc} with {what}", got, want)

    # deploy.main обязана читать вердикт, не код: смешанный прогон --
    # красный отказ, без кредов и ростера за ним. Подмена _play.play --
    # шов, каким его видит deploy: (код, вывод прогона).
    import shutil
    import subprocess
    from mop.common import bus, config, creds, projects
    from mop.cli.server import deploy
    from mop.server import identity
    d = tempfile.mkdtemp(prefix="mop-test-deploy-475-")
    inventory, key = os.path.join(d, "inventory.yaml"), os.path.join(d, "id")
    for f in (inventory, key, key + ".pub"):
        open(f, "w").close()
    people = os.path.join(d, "operators")
    with open(people, "w") as f:
        f.write(identity.format_line(identity.Identity("anton", "admin", ("*",)),
                                     identity.hash_password("x")) + "\n")
    runs = {"recap": RECAP_MIXED, "rc": 4}
    collected, checked = [], []
    listing = [INVENTORY_CLEAN]
    env = dict(os.environ)
    try:
        with offline(), \
                patched(shutil, which=lambda cmd: f"/usr/bin/{cmd}"), \
                patched(config, require=lambda *a: None), \
                patched(subprocess, run=lambda argv, **kw: subprocess.CompletedProcess(
                    argv, 0, json.dumps(listing[0]), "")), \
                patched(_play, play=lambda *a, **kw: (runs["rc"], runs["recap"])), \
                patched(deploy, missing_extras=lambda setting, root: [],
                        manifests=lambda origins: {},
                        check=lambda: checked.append(1)), \
                patched(bus, ask_cluster=lambda *a, **kw: {"ok": True, "projects": []}), \
                patched(projects, for_deploy=lambda answer, local: ([], None),
                        read=lambda: []), \
                patched(creds, collect=lambda *a, **kw: collected.append(1) or [],
                        operator=lambda dest: "anton",
                        snapshot=lambda dest: ("human", {"user": "anton",
                                                         "password": "x"})):
            os.environ.update({"INVENTORY": inventory, "MOP_GIT_KEY": key,
                               "MOP_OPERATORS_FILE": people})
            out, err, code = run_command(deploy.main, [])
            # Красный -- не ноль, и отказ называет провал; «configured» из
            # прежнего поведения не должно быть.
            c.check("#475 a mixed run is a red refusal, not \"configured\"",
                    code != 0 and "failed" in err.lower()
                    and "did not answer" not in err, (code, repr(err)))
            c.check("#475 no credentials collected after a mixed run", not collected,
                    collected)
            c.check("#475 no roster check after a mixed run", not checked, checked)
            c.check("#475 the refusal names the failure, not silence",
                    "failed" in err.lower() and "did not answer" not in err, repr(err))
            # А чистый #87 остаётся прежним: не красный, креды и ростер собраны.
            collected.clear(), checked.clear()
            runs["recap"], runs["rc"] = RECAP_UNREACHABLE, 4
            out, err, code = run_command(deploy.main, [])
            c.expect("#475 an unreachable-only run stays the #87 way: not red", code, 0)
            c.check("#475 #87 still collects credentials", not (not collected), collected)
            c.check("#475 #87 still shows the roster", not (not checked), checked)
    finally:
        os.environ.clear()
        os.environ.update(env)


def check_deploy_staging_error_548(c):
    """HYPOTHESIS (#548): вокруг _staged_registry_seed ловился только
    RuntimeError; OSError из mkdtemp/chmod/write_snapshot уходил трейсбеком
    и ронял deploy даже серверу, у которого реестр есть и посев был бы
    пропущен. SOLUTION: ошибка подготовки любого вида -- тот же отказ:
    одна строка в stderr, плейбук идёт с mop_registry_seed_refused, deploy
    не падает. STATUS: FIXED — see #548"""
    import shutil
    import subprocess
    from mop.common import bus, config, creds, projects
    from mop.cli.server import deploy
    from mop.server import identity
    d = tempfile.mkdtemp(prefix="mop-test-deploy-548-")
    inventory, key = os.path.join(d, "inventory.yaml"), os.path.join(d, "id")
    for f in (inventory, key, key + ".pub"):
        open(f, "w").close()
    people = os.path.join(d, "operators")
    with open(people, "w") as f:
        f.write(identity.format_line(identity.Identity("anton", "admin", ("*",)),
                                     identity.hash_password("x")) + "\n")
    plays = []
    listing = [INVENTORY_CLEAN]
    env = dict(os.environ)

    def run_mock(argv):
        """Игра (#475): ansible-inventory отвечает листингом, плейбук -- нулём."""
        text = json.dumps(listing[0]) if argv and argv[0] == "ansible-inventory" else ""
        return subprocess.CompletedProcess(argv, 0, text, "")

    try:
        with offline(), \
                patched(shutil, which=lambda cmd: f"/usr/bin/{cmd}"), \
                patched(config, require=lambda *a: None), \
                patched(subprocess, run=lambda argv, **kw: run_mock(argv)), \
                patched(_play, play=lambda *a, **kw: plays.append(kw) or (0, "")), \
                patched(deploy, missing_extras=lambda setting, root: [],
                        manifests=lambda origins: {},
                        check=lambda: None, _staged_registry_seed=lambda: (
                            (_ for _ in ()).throw(OSError(28, "No space left on device")))), \
                patched(bus, ask_cluster=lambda *a, **kw: {"ok": True, "projects": []}), \
                patched(projects, for_deploy=lambda answer, local: ([], None), read=lambda: []), \
                patched(creds, collect=lambda *a, **kw: [], operator=lambda dest: "anton",
                        snapshot=lambda dest: ("human", {"user": "anton", "password": "x"})):
            os.environ.update({"INVENTORY": inventory, "MOP_GIT_KEY": key,
                               "MOP_OPERATORS_FILE": people})
            try:
                out, err, code = run_command(deploy.main, [])
            except OSError as e:
                # Сам дефект #548: OSError подготовки уходит из main
                # наружу трейсбеком, унося весь файл проверок.
                c.check("#548 an OSError in staging does not kill deploy with a traceback",
                        False, f"main raised {e!r}")
                out, err, code, plays[:] = "", "", 1, []
            sent = plays[-1] if plays else {}
            c.check("#548 an OSError in staging does not kill deploy with a traceback",
                    code == 0 and plays and "Traceback" not in err, (code, repr(err)[:200]))
            c.check("#548 the refusal is one stderr line naming the failure",
                    len([l for l in err.strip().splitlines() if l.strip()]) == 1
                    and "cannot stage the registry seed" in err and "No space left" in err,
                    repr(err)[:200])
            c.check("#548 the play still runs, with the refused flag and no seed",
                    sent.get("registry_seed") is None
                    and sent.get("registry_seed_refused") is True, sent)
    finally:
        os.environ.clear()
        os.environ.update(env)


def main():
    c = Checks()
    check_deploy_staging_error_548(c)
    check_server_maintenance_409(c)
    check_login_name_407(c)
    check_current_docs_401(c)
    check_dispatch_sources_396(c)
    check_concise_global_help_430(c)
    check_help_content_432(c)
    check_deploy_argument_refusal_439(c)
    check_deploy_mixed_outcome_475(c)
    check_refusals_432(c)
    check_reference_render_350(c)
    check_reference_empty_350(c)
    check_reference_committed_350(c)
    check_gave_up_rows_345(c)
    check_bootstrap_outcome_334(c)
    check_update_origin_503(c)
    check_bootstrap_sent_334(c)
    # HYPOTHESIS: каталога нет — диспетчер bash ищет файл по имени в bin/.
    # SOLUTION: catalog() из обхода пакета, resolve() по нему.
    # STATUS: FIXED — see #75
    cat = cli.catalog(FOUND)
    want = {"add": "mop.cli.core.add", "list": "mop.cli.core.list",
            "deploy": "mop.cli.pool.deploy", "mcp": "mop.cli.service.mcp",
            "driver": "mop.cli.driver", "bug": "mop.cli.bug", "dev": "mop.cli.dev"}
    c.expect("catalog", cat, want)
    # Одно имя в двух секциях — отказ, а не «кто первый».
    try:
        cli.catalog(FOUND + [("pool", "add", False)])
        c.fail("catalog: a duplicate name must refuse")
    except RuntimeError as e:
        c.check(f"catalog: the refusal must name the duplicate: {e}", not ("add" not in str(e)))

    # Плоская команда: модуль и остаток argv.
    for argv, want in [
        (["list"], ("mop.cli.core.list", [])),
        (["add", "--llm", "x"], ("mop.cli.core.add", ["--llm", "x"])),
        # Группа с глаголом-модулем: модуль глагола, остаток без глагола.
        (["driver", "build", "mop"], ("mop.cli.driver.build", ["mop"])),
        # Группа без такого модуля-глагола: сама группа, argv целиком —
        # так переезжают группы, пока их не разрезали (#77).
        (["bug", "new", "t"], ("mop.cli.bug", ["new", "t"])),
        (["driver"], ("mop.cli.driver", [])),
        # Вложенные группы (#253): спуск по дереву, пока следующий argv --
        # глагол; подгруппа без глагола отвечает сама, чужое слово -- ей же.
        (["dev", "bug", "new", "t"], ("mop.cli.dev.bug.new", ["t"])),
        (["dev", "bug"], ("mop.cli.dev.bug", [])),
        (["dev", "bug", "nope"], ("mop.cli.dev.bug", ["nope"])),
        (["dev", "x"], ("mop.cli.dev", ["x"])),
    ]:
        got = cli.resolve(argv, cat, VERBS)
        c.expect(f"resolve({argv})", got, want)
    c.check("resolve of an unknown name must be None", not (cli.resolve(["nope"], cat, VERBS) is not None))
    c.check("resolve of nothing must be None", not (cli.resolve([], cat, VERBS) is not None))

    # HYPOTHESIS (#140): строка хода одна, и строкам вывода сборки на
    # терминале негде жить. SOLUTION: frame() -- кадр из нескольких строк,
    # перерисовываемый на месте: вверх на прежнюю высоту, стереть до конца
    # экрана, новые строки, обрезанные по ширине, чтобы перенос не сбил счёт.
    E = "\033"
    got = lib.frame(0, ["one", "two", "step"], 80)
    c.expect("frame from nothing", got, f"\r{E}[Jone\ntwo\nstep")
    got = lib.frame(3, ["step"], 80)
    c.expect("frame over three rows", got, f"\r{E}[2A{E}[Jstep")
    c.expect("frame to nothing erases", lib.frame(1, [], 80), f"\r{E}[J")
    # Цвета и управляющие символы строки ansible не должны ни красить
    # кадр, ни сдвигать курсор; ширина -- по видимым символам.
    got = lib.frame(0, [f"{E}[0;32mok: [hyper]{E}[0m\tx\r", "abcdefghij"], 6)
    c.expect("frame must strip escapes and cut to width", got, f"\r{E}[Jok: [\nabcde")
    # STATUS: FIXED — see #140

    # Описание — первая строка докстринга, прочитанная без импорта: импорт
    # тянул бы шину и падал бы на машине без nats-py ради строки help.
    d = tempfile.mkdtemp(prefix="mop-test-cli-")
    p = os.path.join(d, "x.py")
    with open(p, "w") as f:
        f.write('"""do a thing: mop x [--flag]\n\nmore text\n"""\nimport nothing_such\n')
    c.expect("describe", cli.describe(p), "do a thing: mop x [--flag]")
    with open(p, "w") as f:
        f.write("x = 1\n")
    c.expect("describe of a module without a docstring must be empty", cli.describe(p), "")

    # deploy на python (#76): чистое — отвергнутые старые цели, недостающие
    # файлы MOP_BODY_EXTRA, имена проектов из origin'ов и легаси, --extra-vars.
    # HYPOTHESIS: deploy на bash, зовёт mop server config/projects/list подпроцессом.
    # SOLUTION: mop.cli.pool.deploy поверх библиотеки. STATUS: FIXED — see #76
    try:
        from mop.cli.server import deploy
        from mop.common import projects
    except ImportError as e:
        c.fail(f"{e}")
        return c.report("cli")
    # Деплой не трогает ~/etc (#311): ссылки ~/etc/nomad и ~/etc/nats были
    # стыком с личным инструментом автора (net setup) и никем больше не
    # читаются; старые цели запуска отвергает общий отказ «takes no
    # arguments» (#79). HYPOTHESIS: deploy.link заводит ссылки каждым
    # прогоном, RUN_TARGETS дублирует общий отказ. SOLUTION: оба удалены.
    # STATUS: FIXED — see #311
    src = open(deploy.__file__, encoding="utf-8").read()
    c.check("#311 deploy knows nothing of ~/etc", "~/etc" not in src and not hasattr(deploy, "link"))
    c.check("#311 no run-target table besides the generic refusal",
            not hasattr(deploy, "RUN_TARGETS") and not hasattr(deploy, "refused_target"))
    root = tempfile.mkdtemp(prefix="mop-test-deploy-")
    open(os.path.join(root, "sandbox.yaml"), "w").close()
    c.expect("missing_extras", deploy.missing_extras("sandbox.yaml, other.yaml,", root), ["other.yaml"])
    c.expect("missing_extras of an empty setting must be empty", deploy.missing_extras("", root), [])
    c.expect("projects.names", projects.names({"git@h:g/proj.git", "git@h:g/mop.git"}, {"legacy"}),
             ["legacy", "mop", "proj"])
    # Списком, а не строкой: `--extra-vars mop_projects=[...]` ansible берёт как
    # строку и проходит по её символам, порождая пользователей `master-[`.
    ev = _play.play_vars(["mop", "proj"], {"proj": {"asks": {}}})
    c.check(f"play_vars: {ev}", not (json.loads(ev[0]) != {"mop_projects": ["mop", "proj"]}
                                     or json.loads(ev[1]) != {"mop_manifests": {"proj": {"asks": {}}}}))
    # Узкий прогон проектов (#79) идёт без манифестов: их читают слои узла и
    # тела, а не роль шины. Лишний --extra-vars пустым словарём стирал бы
    # манифесты, уже разложенные полной игрой.
    c.expect("play_vars without manifests", _play.play_vars(["mop"]), [json.dumps({"mop_projects": ["mop"]})])
    # Лимиты папетов (#107) едут рядом с проектами, и пустые тоже: пустой
    # словарь -- правда контроллера «лимитов нет», и сервер обязан её
    # получить, иначе снятый лимит жил бы там дальше. Сама функция чистая:
    # файл читает play(), а не она.
    got = _play.play_vars(["mop"], limits={})
    c.expect("play_vars with empty limits", got, [json.dumps({"mop_projects": ["mop"], "mop_limits": {}})])
    got = _play.play_vars(["mop"], limits={"mop": 2})
    c.expect(f"play_vars with limits: {got}", json.loads(got[0]).get("mop_limits"), {"mop": 2})
    # STATUS: FIXED — see #107
    # Хосты форжей (#121) едут полной игре списком: роль узла доверяет ключу
    # каждого. Без них -- ключа нет вовсе, узкий прогон проектов их не
    # передаёт и роль узла не играет.
    got = _play.play_vars(["mop"], git_hosts=["dev.corp", "git.ermak.dev"])
    c.expect(f"play_vars with git hosts: {got}", json.loads(got[0]).get("mop_git_hosts"),
             ["dev.corp", "git.ermak.dev"])
    # #178: хосты инвентаря -- списком, только когда их дали.
    got = _play.play_vars(["mop"], inventory_hosts=["a", "b"])
    c.expect(f"play_vars with inventory hosts: {got}",
             json.loads(got[0]).get("mop_inventory_hosts"), ["a", "b"])
    c.check("play_vars without inventory hosts must not send an empty list",
            not ("mop_inventory_hosts" in json.loads(_play.play_vars(["mop"])[0])))
    c.check("play_vars without git hosts must not send an empty list",
            not ("mop_git_hosts" in json.loads(_play.play_vars(["mop"])[0])))
    # STATUS: FIXED — see #121

    # Локаль прогонов (#92): ansible требует UTF-8 и берёт её из окружения, а
    # свежая машина несёт LANG=C. Ставит её диспетчер, потому что зовут
    # прогон и `mop setup`, и `mop server deploy`, и сборка образа.
    # Локаль прогона считается от локали УСТАНОВКИ, а не от унаследованного
    # окружения. Окружение как раз и бывает сломано: ssh привозит LC_* с
    # машины оператора, и на сервере такой локали нет — ansible тогда не
    # стартует, хотя LANG и LC_CTYPE выглядят исправными.
    have = {"C.UTF-8", "ru_RU.UTF-8"}
    for wanted, want in [("ru_RU.UTF-8", "ru_RU.UTF-8"),   # есть на машине
                         ("de_DE.UTF-8", "C.UTF-8"),       # нет — запасная
                         ("C", "C.UTF-8"),                 # не UTF-8 — запасная
                         ("", "C.UTF-8")]:
        got = cli.run_locale(wanted, usable=have.__contains__)
        c.expect(f"run_locale({wanted!r})", got, want)
    # Запасная берётся, даже если и её на машине нет: сказать нечего, а
    # C.UTF-8 встроена в glibc и есть везде, где есть сам glibc.
    c.expect("run_locale must fall back to C.UTF-8",
             cli.run_locale("ru_RU.UTF-8", usable=lambda _: False), "C.UTF-8")

    # Чем запускать команду, которой нужны права root (#91). Под root —
    # ничем: повышать нечего, а на выделенном сервере ещё и нечем, там
    # `sudo` попросту не стоит, и команда падала трассировкой на первом же
    # шаге установки.
    from mop.cli.pool import _self as pool_setup   # общее двух setup (#259)
    c.expect("elevate as root", pool_setup.elevate(uid=0), [])
    c.expect("elevate as a user", pool_setup.elevate(uid=1000), ["sudo"])

    # Группы разрезаны по глаголам (#77): каждый глагол из usage группы
    # (`  mop <группа> <глагол>`) — свой модуль, и диспетчер находит его по
    # имени; таблиц VERBS в группах нет.
    # HYPOTHESIS: группа — один модуль с VERBS. SOLUTION: подпакет, глагол —
    # модуль, __init__ отвечает без глагола. STATUS: FIXED — see #77
    import re
    have = cli.verbs()
    for group in sorted(g for _, g, _ in [(0, s, p) for s, _, p in cli.scan() if p]):
        path = os.path.join(cli.PACKAGE, group, "__init__.py")
        with open(path) as f:
            doc = f.read()
        # Глагол бывает и через дефис: `mop server pve-facts` (#158).
        listed = set(re.findall(rf"^  mop {group} ([a-z][a-z-]*)", doc, re.M))
        missing = listed - set(have.get(group) or {})   # дерево (#253): глаголы -- ключи
        c.check(f"group {group}: verbs without a module: {sorted(missing)}", not (missing))
        c.check(f"group {group}: its docstring lists no verbs", not (not listed))

    # Каждый модуль команды определяет `main` ровно один раз, и никакое имя
    # верхнего уровня не определяется дважды: второе определение молча
    # затирает первое, и py_compile этого не видит. Поймано на живом узле —
    # `_inner_script` переименовали в `main` вместе с `v_run`, и врапер
    # падал NameError после ensure.
    import ast as _ast
    for root, _, files in os.walk(cli.PACKAGE):
        for f in files:
            if not f.endswith(".py") or f.startswith("_") and f != "__init__.py":
                continue
            path = os.path.join(root, f)
            if root == cli.PACKAGE:
                continue
            with open(path) as fh:
                tree = _ast.parse(fh.read())
            defs = [n.name for n in tree.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))]
            dup = sorted({d for d in defs if defs.count(d) > 1})
            c.check(f"{os.path.relpath(path)}: defined twice: {dup}", not (dup))
            is_section_init = f == "__init__.py" and os.path.basename(root) in cli.SECTIONS
            c.check(f"{os.path.relpath(path)}: no main(argv)",
                    not (not is_section_init and "main" not in defs and not any(
                        isinstance(n, _ast.Assign) and any(getattr(t, "id", "") == "main" for t in n.targets)
                        for n in tree.body)))

    # ── #111: origin из рабочей копии, одним механизмом ─────────────────
    # HYPOTHESIS: `mop project add` требовал origin всегда, а команды, что
    # умели брать его из рабочей копии, делали это каждая по-своему: своя
    # проверка на вид origin'а у master, свой запасной путь у driver build,
    # отказ cwd_origin всегда называл `mop add`.
    # SOLUTION: lib.pick_origin -- чистое решение, lib.origin -- git и отказ.
    from mop.common import puppets
    for good in ("git@git.ermak.dev:ermak/mop.git", "https://h/g/mop.git",
                 "/srv/git/mop.git"):
        c.check(f"looks_like_origin({good!r}) must be true", not (not puppets.looks_like_origin(good)))
    # Голое имя и значение чужого флага, приехавшее позиционно, -- не origin.
    for bad in ("mop", "opus", "", "  "):
        c.check(f"looks_like_origin({bad!r}) must be false", not (puppets.looks_like_origin(bad)))

    here = "git@git.ermak.dev:ermak/mop.git"
    other = "git@git.ermak.dev:rugent/rugent.git"
    # Явный аргумент старше рабочей копии.
    c.expect("pick_origin: an explicit origin must win over the working copy",
             lib.pick_origin(other, here), other)
    # Нет аргумента -- рабочая копия.
    c.expect("pick_origin: without an argument the working copy's origin", lib.pick_origin(None, here), here)
    # Отказы -- ValueError с причиной, а не None: None дальше читался бы как
    # проект с пустым именем.
    for arg, cwd, why in (("opus", here, "doesn't look like a git-origin"),
                          (None, None, "no origin given")):
        try:
            got = lib.pick_origin(arg, cwd)
        except ValueError as e:
            c.check(f"pick_origin({arg!r}, {cwd!r}) refused without the reason: {e}", not (why not in str(e)))
            continue
        c.fail(f"pick_origin({arg!r}, {cwd!r}) must refuse, got {got!r}")
    # Неверный явный аргумент не подменяется рабочей копией: опечатка иначе
    # молча завела бы не тот проект.
    # STATUS: FIXED — see #111

    check_mcp_declarations(c)
    check_dev_mcp_515(c)
    check_mcp_positional_refusal_438(c)
    check_refusals(c)
    check_refusals_163(c)
    check_output_rules(c)
    check_empty_value(c)
    check_node_flag_320(c)
    check_image_branch_cli_593(c)
    check_one_parser_320(c)
    check_node_flag_340(c)
    check_deploy_check(c)
    check_inventory_drivers(c)
    check_loopback_switch_558(c)
    check_pool_uniform(c)
    check_node_memory_197(c)
    check_fallback_model_183(c)
    check_agent_on_node_172(c)
    check_role_declarations_585(c)
    check_bus_import_169(c)      # последними: перезагружают модули
    check_nomad_import_187(c)
    return c.report("cli")


def check_role_declarations_585(c):
    """HYPOTHESIS (#585): каталог инструментов роли строится из
    capabilities её бандла и объявлений командлетов, но объявление не
    умеет сказать, какие глаголы командлету нужны: `MCP keys are
    annotations, args, background` -- и роль либо получает все командлеты
    (ложная стена наоборот), либо ни одного.
    SOLUTION: объявление называет node_verbs/cluster_verbs (словарь
    soul.NODE_VERBS/CLUSTER_VERBS) и dispatch (bool); чужой глагол или не
    тот тип -- громкий отказ на старте mop mcp; молчащее объявление роли
    не открывается (channel.tools_for). STATUS: FIXED — see #585"""
    d = tempfile.mkdtemp(prefix="mop-test-mcp-585-")
    p = os.path.join(d, "x.py")
    with open(p, "w") as f:
        f.write('MCP = {"annotations": "destructive", "node_verbs": ["wipe"], '
                '"cluster_verbs": ["alloc", "stop"], "dispatch": False}\n')
    try:
        got = cli.declared(p)
    except ValueError as e:
        got = e
    c.expect("#585 a declaration names the verbs it needs",
             got, {"annotations": "destructive", "node_verbs": ["wipe"],
                   "cluster_verbs": ["alloc", "stop"], "dispatch": False})
    for bad in ('MCP = {"node_verbs": ["disk"]}',          # админский, не роли
                'MCP = {"cluster_verbs": ["drain"]}',
                'MCP = {"cluster_verbs": "roster"}',       # не список
                'MCP = {"dispatch": "yes"}'):
        with open(p, "w") as f:
            f.write(bad + "\n")
        try:
            cli.declared(p)
        except ValueError:
            continue
        c.fail(f"#585 declared must refuse: {bad}")

    # HYPOTHESIS (#585, остаток): seventeen commandlets carry an MCP
    # declaration, but none names its verbs -- so a role sees no commandlet
    # tool at all, and the fail-closed catalogue is empty for every grant.
    # SOLUTION: annotate each declaration with the verbs the commandlet
    # really calls (the audit below follows the actual calls, not guessed
    # names); a commandlet whose true verbs are admin ones the role grammar
    # excludes (nodes/drain/up/project_add/project_limit) or that needs a
    # human login (models, diagnose, server diagnose) stays silent -- it is
    # human-only, and the grammar keeps it out on purpose.
    # Глаголы -- по живым вызовам каждого командлета, не по имени команды:
    # wipe зовёт cluster alloc (узел остановленного) и node wipe; recycle
    # добавляет spec/delete/update/bootstrap_result; stat -- cluster pool и
    # node usage; landing -- только landing.
    ANNOTATED = {
        # #642: wipe переехал на HTTP-командлет -- старых bus verbs не декларирует.
        "wipe": None,
        "restart": None,
        # #642: delete переехал на HTTP-командлет -- старых bus verbs не декларирует.
        "delete": None,
        "recycle": (["wipe"], ["spec", "alloc", "delete", "update",
                              "bootstrap_result"]),
        "update": ([], ["spec", "update", "bootstrap_result"]),
        # add --role (#618): ping -- предзапрос протокола ролей до создания.
        "add": ([], ["add", "alloc", "bootstrap_result", "ping"]),
        "landing": ([], ["landing"]),
        "stat": None,
        "profile": None,
        # admin verbs outside the role grammar: silent for grants on purpose
        # stop (#608) -- job_stop, операторский глагол вне грамматики ролей
        "stop": None,
        "node": None, "node_drain": None, "node_up": None,
        "project_add": None, "project_limit": None,
        "models": None, "diagnose": None, "server_diagnose": None,
        # #642: новые HTTP-командлеты не декларируют старые bus verbs.
        "health": None, "addresses": None, "operation": None, "project_list": None, "build": None,
        "list": None, "detail": None,
        # #594: приватная история требует человеческого логина, терминал —
        # admin-допуска. Исходящим полномочием роли они не становятся.
        "session_history": None, "session_terminal": None,
    }
    from mop.common import soul
    tools = {n: cli.declared(path) for n, _, path
             in cli.tool_commands(cli.scan(), cli.verbs())}
    declared_names = {n: d for n, d in tools.items() if d is not None}
    c.expect("#585 every declared commandlet is audited",
             sorted(declared_names), sorted(ANNOTATED))
    for name, audited in sorted(ANNOTATED.items()):
        decl = declared_names.get(name)
        if audited is None:
            c.check(f"#585 {name} stays silent: its verbs are outside the role grammar "
                    f"or need a human login",
                    not any(k in (decl or {}) for k in
                            ("node_verbs", "cluster_verbs", "dispatch")))
            continue
        node_verbs, cluster_verbs = audited
        c.expect(f"#585 {name} names the verbs it really calls",
                 (decl.get("node_verbs"), decl.get("cluster_verbs")),
                 (node_verbs, cluster_verbs))
        for verb in node_verbs:
            c.check(f"#585 {name}: {verb} is in the role grammar",
                    verb in soul.NODE_VERBS)
        for verb in cluster_verbs:
            c.check(f"#585 {name}: {verb} is in the role grammar",
                    verb in soul.CLUSTER_VERBS)
    # Аннотация -- не декорация: с ней рабочий пресет видит командлеты в
    # своих правах, а не пустой каталог. Пресет -- рабочий из docs/SOUL.md
    # плюс чтение (usage/roster/alloc/spec), выданное каналом #585: stat
    # требует pool, которого рабочему не дают, и остаётся невидимым.
    from mop.client import channel
    grant_caps = soul.from_manifest({
        "soul": "puppet", "version": 1,
        "runtime": {"driver": "any", "receives_dispatch": True},
        "capabilities": {"node_verbs": ["send", "tail", "state", "states",
                                        "ping", "local", "usage"],
                         "cluster_verbs": ["alloc", "spec", "roster"],
                         "dispatch": False, "spawn": []},
        "prompt": "soul.md"}, b"p").manifest.capabilities
    seen = channel.tools_for(channel.GRANT, grant_caps,
                             {n: d for n, d in declared_names.items()})
    c.check("#642 HTTP declarations never open from legacy verb capabilities",
            not ({'profile', 'health', 'addresses', 'operation', 'project_list', 'node_drain', 'node_up'} & seen), seen)
    c.check("#585 a role with no matching verbs gains nothing",
            not ({"wipe", "add", "stat"} & seen), seen)
    # STATUS: FIXED — see #585


def check_mcp_declarations(c):
    """#160: инструменты управления MCP -- объявления в самих командлетах.

    HYPOTHESIS: MCP держал вторую реализацию add/update/build руками, и она
    разошлась с командлетами (не слал workspace, собирал образ мимо сборщика).
    SOLUTION: командлет объявляет `MCP = {...}`; mop mcp находит объявления
    обходом пакета, читает их без импорта и зовёт сам командлет."""
    d = tempfile.mkdtemp(prefix="mop-test-mcp-")
    p = os.path.join(d, "x.py")

    # Объявление читается без импорта, как докстринг: модуль тянет шину.
    with open(p, "w") as f:
        f.write('"""x"""\nimport nothing_such\n'
                'MCP = {"annotations": "destructive", "args": ['
                '{"name": "name", "type": "string", "required": True}]}\n')
    got = cli.declared(p)
    c.check(f"declared: {got!r}", not ((got or {}).get("annotations") != "destructive" or
                                       [a["name"] for a in got.get("args", [])] != ["name"]))
    # Без объявления команда в MCP не видна: attach, master, code, service.
    with open(p, "w") as f:
        f.write('"""x"""\n')
    c.check("declared: a module without MCP must stay out of MCP", not (cli.declared(p) is not None))
    # Кривое объявление -- громкий отказ, а не молча пропавший инструмент.
    for bad in ('MCP = {"args": [{"name": "fresh", "type": "boolean"}]}',  # без флага
                'MCP = {"args": [{"name": "x", "type": "float", "flag": "--x"}]}',
                'MCP = {"annotations": "sometimes"}',
                'MCP = {"args": [{"name": "a", "type": "string"},'
                ' {"name": "b", "type": "string", "required": True}]}',  # необязательный позиционный раньше обязательного
                'MCP = {"colour": 1}',
                'MCP = dict(args=[])'):                                  # не литерал
        with open(p, "w") as f:
            f.write(bad + "\n")
        try:
            cli.declared(p)
        except ValueError:
            continue
        c.fail(f"declared must refuse: {bad}")

    # Описание инструмента -- докстринг командлета целиком: usage и смысл.
    with open(p, "w") as f:
        f.write('"""do x: mop x <name>\n\nmore text\n"""\nimport nothing_such\n')
    c.expect("docstring", cli.docstring(p), "do x: mop x <name>\n\nmore text")
    # Вывод командлета уходит модели без цветов терминала: lib.fail красит
    # всегда. Строки и табуляция остаются.
    got = lib.plain("\033[0;31mpu-mop-1: gone\033[0m\n\tnext\r")
    c.expect("plain", got, "pu-mop-1: gone\n\tnext")

    # Имя инструмента -- слова команды через подчёркивание.
    found = [("core", "add", False), ("node", "", True), ("service", "mcp", False)]
    group_verbs = {"node": {"drain": {}, "up": {}}}
    names = {n: words for n, words, _ in cli.tool_commands(found, group_verbs)}
    want = {"add": ["add"], "node": ["node"], "node_drain": ["node", "drain"],
            "node_up": ["node", "up"], "mcp": ["mcp"]}
    c.expect("tool_commands", names, want)

    # ── #253: вложенные группы -- дерево, имена по пути, приватное, прежние имена
    # HYPOTHESIS: диспетчер знает один уровень, `mop dev bug new` и
    # `mop server user add` некуда положить. SOLUTION: verbs() -- дерево,
    # tool_commands спускается по нему и именует инструмент словами через
    # подчёркивание; в PRIVATE видны только явно объявленные листья (#443);
    # unalias переписывает
    # прежнее имя по таблице LEGACY молча, на один релиз.
    # STATUS: FIXED — see #253
    nested = [("core", "add", False), ("server", "", True), ("dev", "", True)]
    tree = {"server": {"deploy": {}, "user": {"add": {}, "passwd": {}}},
            "dev": {"bug": {"new": {}}}}
    got = {n: (words, os.path.relpath(path, cli.PACKAGE))
           for n, words, path in cli.tool_commands(nested, tree, private=())}
    want = {"add": (["add"], "core/add.py"),
            "server": (["server"], "server/__init__.py"),
            "server_deploy": (["server", "deploy"], "server/deploy.py"),
            "server_user": (["server", "user"], "server/user/__init__.py"),
            "server_user_add": (["server", "user", "add"], "server/user/add.py"),
            "server_user_passwd": (["server", "user", "passwd"], "server/user/passwd.py"),
            "dev": (["dev"], "dev/__init__.py"),
            "dev_bug": (["dev", "bug"], "dev/bug/__init__.py"),
            "dev_bug_new": (["dev", "bug", "new"], "dev/bug/new.py")}
    c.expect("#253 nested tool_commands", got, want)
    names = {n for n, _, _ in cli.tool_commands(nested, tree)}
    c.check(f"#253 server tools remain visible: {sorted(names)}",
            "server_user_add" in names)
    c.check(f"#253 dev must be private: {cli.PRIVATE}", not ("dev" not in cli.PRIVATE))
    table = {"bug": ("dev", "bug"), "web": ("server", "web")}
    for argv, want in [(["bug", "new", "t"], ["dev", "bug", "new", "t"]),
                       (["web", "--port", "1"], ["server", "web", "--port", "1"]),
                       (["list"], ["list"]), ([], [])]:
        got = cli.unalias(argv, table)
        c.expect(f"#253 unalias({argv})", got, want)
    c.check("#253 LEGACY must be the alias table", not (not isinstance(cli.LEGACY, dict)))
    # verbs() читает дерево с диска: подпакет группы -- подгруппа.
    d = tempfile.mkdtemp(prefix="mop-test-tree-")
    for rel in ("g/__init__.py", "g/a.py", "g/s/__init__.py", "g/s/b.py", "g/_hidden.py"):
        os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
        open(os.path.join(d, rel), "w").write('"""x"""\n')
    c.expect("#253 verbs tree", cli.verbs(d), {"g": {"a": {}, "s": {"b": {}}}})

    # argv из значений инструмента: позиционные по порядку, флаги по имени.
    args = [{"name": "name", "type": "string", "required": True},
            {"name": "origin", "type": "string"},
            {"name": "llm", "type": "string", "flag": "--llm"},
            {"name": "days", "type": "integer", "flag": "--days"},
            {"name": "fresh", "type": "boolean", "flag": "--fresh"}]
    for values, want in (
            ({"name": "pu-mop-1"}, ["pu-mop-1"]),
            ({"name": "pu-mop-1", "origin": "git@h:g/mop.git", "llm": "opus",
              "fresh": True, "days": 3},
             ["pu-mop-1", "git@h:g/mop.git", "--llm", "opus", "--days", "3", "--fresh"]),
            ({"name": "pu-mop-1", "origin": "", "llm": "", "fresh": False,
              "days": None}, ["pu-mop-1"])):
        got = cli.tool_argv(args, values)
        c.expect(f"tool_argv({values})", got, want)
    # Отказы: нет обязательного; позиционное, похожее на флаг, -- иначе
    # модель одним значением включала бы чужой флаг командлета.
    for values in ({}, {"name": ""}, {"name": "--fresh"},
                   {"name": "pu-mop-1", "origin": "-x"}):
        try:
            got = cli.tool_argv(args, values)
        except ValueError:
            continue
        c.fail(f"tool_argv({values}) must refuse, got {got}")
    # Пропущенный необязательный позиционный, а за ним заданный -- сдвиг на
    # чужое место: отказ.
    two = [{"name": "a", "type": "string"}, {"name": "b", "type": "string"}]
    try:
        got = cli.tool_argv(two, {"b": "x"})
        c.fail(f"tool_argv must refuse a gap in positionals, got {got}")
    except ValueError:
        pass
    # STATUS: FIXED — see #160

    # Каждое объявление в дереве разбирается: кривое уронило бы mop mcp
    # целиком на старте мастера.
    for _, _, path in cli.tool_commands(cli.scan(), cli.verbs(), private=()):
        try:
            cli.declared(path)
        except ValueError as e:
            c.fail(f"{os.path.relpath(path)}: {e}")


# #444: ограниченное свидетельство из веток задачи, без shell/Git-инструмента.
# HYPOTHESIS: сортировка сотен refs исключает назначенную ветку интеграции.
# SOLUTION: приоритет ветки контекста вне общего лимита, неполноту видно.
# RESULT: проверяется ниже. STATUS: FIXED — see #444
# #444: доказательство интеграции требует ветки именно этой задачи и
# второго родителя merge, а не любого коммита со строкой #iid.
# HYPOTHESIS: поиск по тексту истории принимает упоминание #444 за merge.
# SOLUTION: отбор имён веток и распознавание точного имени ветки в merge.
# RESULT: проверяется ниже. STATUS: FIXED — see #444

def check_evidence_444(c):
    from mop.cli.dev.bug import evidence
    branch = getattr(evidence, "evidence_branch", None)
    source = getattr(evidence, "merge_source", None)
    c.check("#444 branch matcher exists", branch is not None)
    c.check("#444 merge source parser exists", source is not None)
    if not branch or not source:
        return
    for ref, expected in [
        ("origin/bug/444-git-shell", True), ("origin/feat/444", True),
        ("origin/refactor/444-x", True), ("origin/docs/444-x", True),
        ("origin/ci/444-x", True), ("origin/bug/4444-x", False),
        ("origin/bug/444extra", False), ("origin/epic/444-x", False),
        ("origin/bug/44-444", False), ("origin/bug/444-x\nother", False),
    ]:
        c.expect(f"#444 branch {ref!r}", branch(ref, "444"), expected)
    for subject, expected in [
        ("Merge branch 'bug/444-git-shell' into epic/441-master-mcp", "bug/444-git-shell"),
        ("Merge branch 'feat/444' into master", "feat/444"),
        ("fix: mention #444 only", None),
        ("Merge #444 branch 'bug/444-git-shell'", None),
        ("Merge branch 'bug/4444-x' into master", "bug/4444-x"),
    ]:
        c.expect(f"#444 merge {subject!r}", source(subject), expected)


def check_evidence_tool_444(c):
    from mop.cli.dev.bug import evidence
    from mop.common import context
    # #515: dev не экспонируется в MCP -- декларации evidence больше нет;
    # сам командлет и его осторожность с local refs остаются CLI мастера.
    path = evidence.__file__
    c.check("#444 evidence carries no MCP declaration after #515",
            cli.declared(path) is None)
    exposed = {n: words for n, words, _ in cli.tool_commands(cli.scan(), cli.verbs())
               if n.startswith("dev_")}
    c.expect("#515 evidence stays out of the MCP catalog", exposed, {})

    # Ложный merge: ветка уже в первом родителе, второй — чужая работа.
    # Заголовок второго с именем задачи не доказывает её интеграцию.
    base, tip, unrelated, merged = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    calls = []

    def pretend_git(*args):
        calls.append(args)
        if args[0] == "fetch":
            return ""
        if args[0] == "for-each-ref":
            return f"origin/bug/444-x\t{tip}\norigin/master\t{merged}"
        if args[0] == "log":
            return (f"{merged}\t{tip} {unrelated}\t"
                    "Merge branch 'bug/444-x' into master\n"
                    f"{base}\t{tip}\tfix: mention #444 only")
        raise AssertionError(args)

    def ancestor(a, b):
        return (a, b) in {(tip, unrelated), (tip, tip)}

    with patched(lib, git=pretend_git), patched(evidence, _ancestor=ancestor):
        output, err, code = run_command(evidence.main, ["444"], via_cli=True)
    c.expect("#444 reader runs", (err, code), ("", 0))
    c.check("#444 fetches before inspecting refs", calls[0] == ("fetch", "--prune", "origin"))
    c.check("#444 refuses a merge whose branch is already in first parent",
            "verified ancestry" not in output, output)
    c.check("#444 does not treat a plain issue mention as merge",
            "fix: mention" not in output, output)

    # Настоящий merge: второй родитель — ветка задачи, первого в ней нет.
    def real_ancestor(a, b):
        return a == b

    def real_git(*args):
        if args[0] == "log":
            return (f"{merged}\t{base} {tip}\t"
                    "Merge branch 'bug/444-x' into master")
        return pretend_git(*args)

    with patched(lib, git=real_git), patched(evidence, _ancestor=real_ancestor):
        output, err, code = run_command(evidence.main, ["444"], via_cli=True)
    c.check("#444 verifies actual second-parent merge",
            code == 0 and f"{merged} merged bug/444-x at {tip} (verified ancestry)" in output,
            (output, err, code))

    # Интеграция может идти из локальной review-ветки: её точный второй
    # родитель — опубликованный tip задачи, хотя имена refs различаются.
    def alias_git(*args):
        if args[0] == "log":
            return (f"{merged}\t{base} {tip}\t"
                    "Merge branch 'review/444-evidence' into epic/441-master-mcp")
        return pretend_git(*args)
    with patched(lib, git=alias_git), patched(evidence, _ancestor=real_ancestor):
        output, err, code = run_command(evidence.main, ["444"], via_cli=True)
    c.check("#444 verifies an exact published issue tip under a local alias",
            code == 0 and f"{merged} merged review/444-evidence at {tip}" in output,
            (output, err, code))

    # Даже за пределом общего перечня назначенная ветка обязана проверяться.
    current = "origin/integration/441-current"
    branch = "origin/bug/444-x"
    calls.clear()
    def crowded_git(*args):
        calls.append(args)
        if args[0] == "fetch":
            return ""
        if args[0] == "for-each-ref" and args[-1] == "refs/remotes/origin":
            return f"{branch}\t{tip}\n" + "\n".join(
                f"origin/epic/{n:03}\t{base}" for n in range(500))
        if args[0] == "for-each-ref" and args[-1] == "refs/remotes/" + current:
            return f"{current}\t{merged}"
        if args[0] == "log":
            return f"{merged}\t{base} {tip}\tMerge branch 'bug/444-x' into integration/441-current"
        raise AssertionError(args)
    with patched(lib, git=crowded_git), patched(evidence, _ancestor=real_ancestor), \
            context.use(context.Context(branch="integration/441-current")):
        output, err, code = run_command(evidence.main, ["444"], via_cli=True)
    c.check("#444 current integration inspected past remote ref limit",
            code == 0 and f"{current} {merged} merged {branch.removeprefix('origin/')}" in output,
            (output, err, code))
    c.check("#444 truncated refs explicitly marked incomplete",
            "results may be incomplete" in output, output)

    for iid in ("0", "-1", "444; echo unsafe", "9" * 5000):
        with patched(lib, git=lambda *args: c.fail("invalid iid reached git")):
            out, err, code = run_command(evidence.main, [iid])
        c.check(f"#444 invalid iid {iid[:15]!r} refused without git",
                code != 0 and not out and "iid must be" in err,
                (out, err[:100], code))

def check_dev_mcp_515(c):
    """#515: приватная группа dev не экспонируется в MCP вовсе.

    HYPOTHESIS: координаты трекера резолвятся из origin пакета mop, не из
    cwd сессии мастера -- мастер чужого проекта через dev_bug_*/dev_ci_*
    попадал в трекер mop (#515). SOLUTION: группа dev закрыта в MCP
    целиком: declared-фильтр в tool_commands упразднён, декларации из
    листьев удалены; трекер и CI остаются CLI мастера (исключение #450).
    RESULT: в экспозиции нет ни одного dev-инструмента, командлеты живы.
    STATUS: FIXED — see #515"""
    import io
    import inspect
    from mop.cli.dev.bug import new, edit
    from mop.cli.service import mcp
    from mop.common import gitlab

    # Экспозиция собирается обходом дерева: приватная группа не входит в
    # неё ни группой, ни листом -- независимо от деклараций (#515).
    tools = {n: words for n, words, path in
             cli.tool_commands(cli.scan(), cli.verbs())}
    c.check("#515 no developer tool is exposed", not (any(
        n == "dev" or n.startswith("dev_") for n in tools)),
        f"{sorted(n for n in tools if n == 'dev' or n.startswith('dev_'))}")
    # Дверь закрыта на уровне группы: даже объявление MCP в dev-листе не
    # пробивает её обратно (#515). Дверь «объявить = экспонировать» для
    # остальных групп остаётся единственным инвариантом.
    declared_leaf = os.path.join(cli.PACKAGE, "dev", "bug", "list.py")
    c.check("#515 no developer leaf carries an MCP declaration",
            cli.declared(declared_leaf) is None)
    calls = []
    with patched(mcp, MASTER=False, command_tool=lambda *args: calls.append(args)):
        mcp.command_tools()
    c.expect("#515 node profile has no developer tools", calls, [])
    # Профиль мастера собирает каталог тем же обходом: пока что не
    # смотря на PROFILE, проверяем состав выше -- повторять его здесь
    # значило бы вторую копию списка.
    # Командлеты остаются рабочим CLI: characterization ниже -- те же
    # вызовы, что были зелёными до #515 (#443), ничего не ослаблено.

    # ── перенесено из #443: поведение самого механизма объявлений ──────
    # repeatable остаётся механизмом адаптера; его последние живые
    # пользователи ушли вместе с dev-декларациями, поэтому контракт --
    # синтетическим объявлением.
    d = tempfile.mkdtemp(prefix="mop-test-mcp-515-")
    p = os.path.join(d, "x.py")
    with open(p, "w") as f:
        f.write('"""x"""\n'
                'MCP = {"args": [{"name": "label", "type": "string", '
                '"flag": "--label", "repeatable": True}]}\n')
    decl = cli.declared(p)
    c.check("#515 repeatable declaration reads", isinstance(decl, dict))
    if isinstance(decl, dict):
        values = {"label": ["sev::low", "component::cli"]}
        c.expect("#515 repeatable renders a flag per value",
                 cli.tool_argv(decl["args"], values),
                 ["--label", "sev::low", "--label", "component::cli"])
        for bad in ("sev::low", ["sev::low", 4]):
            try:
                cli.tool_argv(decl["args"], {"label": bad})
            except ValueError:
                pass
            else:
                c.fail(f"#515 repeatable accepted non-list label {bad!r}")
        for bad in (["-x", "ok"], ["ok", "--flag"]):
            try:
                cli.tool_argv(decl["args"], {"label": bad})
            except ValueError:
                pass
            else:
                c.fail(f"#515 repeatable accepted dash-prefixed {bad!r}")

    # ── characterization: командлеты dev живут как CLI и после #515 ────
    # Те же вызовы, что были зелёными в #443: тело из --body без stdin,
    # эпик сохраняется при правке, пустое тело отклоняет, второго
    # источника тела не бывает. Экспозиция ушла, поведение осталось.
    calls = []
    with patched(sys, stdin=io.StringIO("WRONG STDIN")), \
            patched(gitlab, create=lambda *a: calls.append(a) or {"iid": 443, "web_url": "url"}):
        run_command(new.main, ["Задача", "--body", "Строка\nтела", "--label", "sev::low"])
    c.expect("#443 new inline body, no stdin", calls,
             [("Задача", "Строка\nтела", ["sev::low", "status::live"])])
    calls.clear()
    with patched(sys, stdin=io.StringIO("WRONG STDIN")), \
            patched(gitlab, issue=lambda iid: {"description": "<!-- epic:12 -->\nold"}), \
            patched(gitlab, edit=lambda *a: calls.append(a)):
        run_command(edit.main, ["443", "--body", "Новый текст"])
    c.expect("#443 edit inline body keeps epic", calls,
             [("443", None, gitlab.replace_body("<!-- epic:12 -->\nold", "Новый текст", gitlab.KEEP))])
    # Пустое тело отклоняет сам командлет, до вызова трекера (#443).
    calls.clear()
    with patched(gitlab, create=lambda *a: calls.append(a) or {"iid": 443, "web_url": "url"}):
        _, _, code = run_command(new.main, ["Задача", "--body", "   \n"])
    c.check("#443 CLI rejects empty issue body", code != 0 and not calls,
            f"code={code}, calls={calls}")
    for fn, argv in ((new.main, ["Задача"]), (edit.main, ["443"])):
        _, _, code = run_command(fn, argv + ["--body", "text", "--body-file", "/missing"])
        c.check(f"#443 {fn.__module__} rejects two body sources", code != 0)


def check_refusals_163(c):
    """#163: отказ сервиса кластера на pool и nodes читался пустым списком.

    HYPOTHESIS: puppets.pool() и nodes.rows() делали
    `bus.ask_cluster(...).get("nodes") or []`: нет прав или Nomad лежит --
    `mop node` рисовал пустую таблицу, ростер пула -- пул без узлов, а всё,
    что шло через ready_nodes (stat, disk, sweep, раздача ключей), работало
    по пустому множеству. stat отдавал отказ из main строкой.
    SOLUTION: оба читателя -- через bus.call_cluster (Refused с текстом
    сервиса), stat бросает тот же текст; диспетчер делает из этого stderr и
    ненулевой выход, stdout пуст. Узел с кривым драйвером (#175) -- строка
    с ошибкой, а не отказ списка: это другой случай.
    STATUS: FIXED — see #163"""
    import importlib
    from mop.common import bus, puppets

    reason = "no rights for nodes: project mop"

    def run(fn, argv=()):
        return run_command(fn, argv, via_cli=True)

    def refusal(what, got, text):
        out, err, code = got
        c.check(f"{what}: stdout {out!r}, stderr {err!r}, exit {code!r}",
                not (not isinstance(code, int) or code == 0 or out or err != text + "\n"))

    # Живой шине сюда хода нет: и соединение, и адрес -- заглушки.
    async def no_bus(*a, **k):
        raise AssertionError("a check reached the live bus")

    def no_connect(*a, **k):
        raise AssertionError("a check reached the live bus")
    real_ready_nodes = puppets.ready_nodes
    with patched(bus, connect=no_connect), patched(bus.nats, connect=no_bus), \
            restored(bus, "ask_cluster", "request_many"), restored(puppets, "ready_nodes"), \
            patched_env(MOP_SERVER_LAN="192.0.2.1"):
        node = importlib.import_module("mop.cli.node")
        stat = importlib.import_module("mop.cli.core.stat")

        bus.ask_cluster = lambda verb, **kw: {"error": reason}
        refusal("mop node on a refusal", run(node.main), reason)
        for fn in (puppets.pool, puppets.ready_nodes):
            try:
                got = fn()
                c.fail(f"{fn.__name__} read a refusal as {got!r}")
            except bus.Refused as e:
                c.expect(f"{fn.__name__} lost the reason: {e!r}", str(e), reason)
        # Подвал `mop list`: причина видна, а не пустой пул.
        c.expect("pool_lines on a refusal", lib.pool_lines(), [f"  {reason}"])

        # #642/#656: stat больше не зависит от pool/node/transcripts. Его
        # реальные HTTP refusals/nullable coverage проверены в control_consumers.

        # Обычный ответ -- вывод прежний, символ в символ.
        row = {"name": "hyper", "driver": "pve", "serves": "mop", "state": "ready",
               "free_mb": 40960, "total_mb": 65536, "slots": 5, "slots_total": 8}
        # #243: слоты -- свободно/всего; всего не знает сервис старше -- «-».
        old = {k: v for k, v in row.items() if k != "slots_total"}
        down = {"name": "mate", "driver": "host", "serves": "-", "state": "down",
                "free_mb": None, "total_mb": None, "slots": None, "slots_total": None}
        bus.ask_cluster = lambda verb, **kw: {"nodes": [row, dict(old, name="old"), down]}
        got = run(node.main)
        want = ("NODE   DRIVER  SERVES  STATE  FREE   TOTAL  SLOTS\n"
                "hyper  pve     mop     ready  40 GB  64 GB  5/8\n"
                "old    pve     mop     ready  40 GB  64 GB  5/-\n"
                "mate   host    -       down   -      -      -\n", "", 0)
        c.expect("mop node on a normal answer", got, want)
        pool = [{"name": "gpu", "status": "ready", "free_mb": 2048, "total_mb": 40960,
                 "slots": 0, "slots_total": 5},
                {"name": "old", "status": "ready", "free_mb": 2048, "total_mb": 40960,
                 "slots": 0},
                {"name": "off", "status": "down"}]
        bus.ask_cluster = lambda verb, **kw: {"nodes": pool}
        want = ["  gpu: free 2/40 GB, slots 0/5", "  old: free 2/40 GB, slots 0/-",
                "  off: down"]
        c.expect("pool_lines", lib.pool_lines(), want)
        bus.ask_cluster = lambda verb, **kw: {"nodes": [pool[0]]}

        # #656: shared proxy keys не доказывают user/puppet attribution. Даже
        # при доступных старых счётчиках новый stat не читает их как fallback.
        def no_transcripts(*args, **kwargs):
            raise AssertionError('stat attempted transcript/node usage fallback')
        bus.request_many = no_transcripts
        puppets.ready_nodes = no_transcripts
        for args in (["--users"], ["--puppets"], ["--puppets", "--users"]):
            out, err, code = run(stat.main, args)
            c.check(f"#642 {args} is explicitly unavailable before IO",
                    code == 2 and out == '' and 'unavailable for shared proxy keys' in err,
                    (out, err, code))
        puppets.ready_nodes = real_ready_nodes
        # #277: ответ глагола pool -- форма pool, а не строка nodes: у той нет
        # статуса, и PoolNode её не примет.
        c.expect("pool on a normal answer", puppets.pool(), [domain.PoolNode.from_pool(pool[0])])


def check_refusals(c):
    """#146: отказ сервиса кластера -- исключение, а не поле, которое каждый
    командлет проверял сам.

    HYPOTHESIS: около двадцати мест делали `got = bus.ask_cluster(...); if
    got.get("error")` и сообщали отказ четырьмя способами; lib.require_job,
    alloc_of, running_alloc выходили из процесса, и MCP держал свою копию
    «аллокация должна работать» -- mcp.puppet_alloc, которая отказ сервиса
    («не твой папет», «нет такого») читала как «не размещён» и теряла причину.
    SOLUTION: bus.Refused и строгий bus.call_cluster; puppets.running_alloc
    бросает LookupError с причиной; диспетчер и loud в MCP превращают
    исключение в одну строку.
    RESULT: командлеты зовут bus.call_cluster и puppets.running_alloc,
    lib.guard спрашивает шину один раз и отдаёт спеку.
    STATUS: FIXED — see #146"""
    from mop.common import bus, puppets

    reason = "pu-mop-9 belongs to project other, not to mop"

    # Строгий вызов: отказ -- Refused с причиной как есть, ответ -- как есть.
    with restored(bus, "ask_cluster"):
        bus.ask_cluster = lambda verb, **kw: {"error": reason}
        try:
            got = bus.call_cluster("alloc", name="pu-mop-9")
            c.fail(f"call_cluster must raise Refused on a refusal, got {got!r}")
        except bus.Refused as e:
            c.expect(f"call_cluster lost the reason: {e!r}", str(e), reason)
        c.check("Refused must be a RuntimeError: callers catch that already",
                not (not issubclass(bus.Refused, RuntimeError)))
        bus.ask_cluster = lambda verb, **kw: {"ok": True, "verb": verb, **kw}
        got = bus.call_cluster("spec", name="pu-mop-9")
        c.expect("call_cluster must pass the answer through", got,
                 {"ok": True, "verb": "spec", "name": "pu-mop-9",
                  "timeout": bus.TIMEOUT, "project": None})

        # running_alloc: отказ сервиса доходит причиной, а не «не размещён».
        bus.ask_cluster = lambda verb, **kw: {"error": reason}
        try:
            puppets.running_alloc("pu-mop-9")
            c.fail("running_alloc must raise on a refusal")
        except LookupError:
            c.fail("running_alloc read a refusal as a missing allocation")
        except bus.Refused as e:
            c.expect(f"running_alloc lost the reason: {e!r}", str(e), reason)
        # Не работает -- LookupError, у падающего -- с причиной падения.
        task = {"state": "pending", "failed": False, "restarts": 3, "exit": 1,
                "next_s": 20}
        for alloc, want in ((None, "pu-mop-9 not running"),
                            ({"ClientStatus": "pending", "NodeName": "n1", "task": task,
                              "reason": "no bus credentials"},
                             "pu-mop-9: FAILED: no bus credentials")):
            bus.ask_cluster = lambda verb, a=alloc, **kw: {"ok": True, "alloc": a}
            try:
                got = puppets.running_alloc("pu-mop-9")
                c.fail(f"running_alloc({alloc!r}) must raise, got {got!r}")
            except LookupError as e:
                c.check(f"running_alloc: {e!r}, want {want!r}...", not (not str(e).startswith(want)))
        live = {"ClientStatus": "running", "NodeName": "n1"}
        bus.ask_cluster = lambda verb, **kw: {"ok": True, "alloc": live}
        c.expect("running_alloc must return the running allocation",
                 puppets.running_alloc("pu-mop-9"), live)

        # guard: один вопрос шине, и его ответ -- вызывающему, а не второй
        # такой же запрос следом (update спрашивал spec дважды).
        asked = []
        spec = {"ok": True, "meta": {"origin": "git@h:g/mop.git"}}
        bus.ask_cluster = lambda verb, **kw: asked.append(verb) or spec
        with patched(bus, PROJECT="mop"):
            got = lib.guard("pu-mop-9")
        c.check(f"guard: asked {asked}, returned {got!r}", not (asked != ["spec"] or got != spec))

    # Диспетчер: отказ -- ровно одна строка в stderr с причиной, ненулевой
    # выход, в stdout ничего: stdout модель читает как ответ команды.
    for exc in (bus.Refused(reason), LookupError(reason)):
        def fn(_argv, exc=exc):
            raise exc
        out, err, code = run_command(fn, [], via_cli=True)
        name = type(exc).__name__
        c.check(f"run({name}): exit {code!r}, want a non-zero int",
                not (not isinstance(code, int) or code == 0))
        c.expect(f"run({name}): stderr must be one line", err, reason + "\n")
        c.check(f"run({name}): stdout {out!r}", not (out))

    # Своих копий больше нет: помощники, выходившие из процесса, и ручные
    # проверки поля error у сервиса кластера в командлетах.
    for gone in ("require_job", "alloc_of", "running_alloc", "running_node"):
        c.check(f"lib.{gone} must be gone: puppets.running_alloc / bus.call_cluster",
                not (hasattr(lib, gone)))
    # Два исключения: у ping в cluster check поле error рядом с ok -- это
    # «сервис жив, Nomad нет», а не отказ; deploy отдаёт ответ целиком
    # projects.for_deploy, и отказ там -- заметка, а не конец прогона.
    allowed = {os.path.join("server", "cluster", "check.py"), os.path.join("server", "deploy.py")}
    for root, _, files in os.walk(cli.PACKAGE):
        for f in files:
            path = os.path.join(root, f)
            rel = os.path.relpath(path, cli.PACKAGE)
            if not f.endswith(".py") or rel in allowed:
                continue
            with open(path) as fh:
                c.check(f"{rel}: bus.ask_cluster by hand, use bus.call_cluster",
                        not ("bus.ask_cluster(" in fh.read()))


# ── #159: правила вывода ──────────────────────────────────────────────────────
# HYPOTHESIS: капс для выразительности (THIS IS THE ONLY CHANNEL, ONLY, FREE,
# ORPHAN), эхо параметров («restarting X on Y...») и советы «run X» в успешном
# выводе. Вывод читает модель через MCP: крик и советы — шум, а успешная быстрая
# команда отвечает ей `done`.
# SOLUTION: слова переписаны, быстрые команды на успехе молчат, отказ — в stderr.
#
# Капс допустим только для того, что заглавное буквально: статусы, протоколы,
# идентификаторы, заголовки колонок, плейсхолдеры в usage. Список закрыт:
# новое слово в нём — осознанное решение, а не молчаливое разрешение.
CAPS_OK = {
    # статусы, которые читают глазами и модель
    "FAILED", "AGENT", "SILENT", "DELIVERED", "HUNG", "MUST", "SHOULD",
    # протоколы, сигналы, литералы чужих программ
    "JSON", "NATS", "LDAP", "HTTP", "HTTPS", "ASGI", "PATH", "PYTHONPATH", "HEAD", "TERM", "SIGHUP", "VMID",
    "PLAY", "RECAP", "EXIT",
    # идентификаторы в тексте
    "SECTIONS", "TESTS",
    # плейсхолдеры в строках usage
    "ADDRESS", "ADDR", "PROFILE", "TEXT", "VALUE", "LOGIN", "NAME",
    # заголовки колонок, названные в тексте
    "MASTER", "OWNER", "CACHE",
}
CAPS_WORD = r"(?<![A-Za-z0-9_$.{/-])[A-Z]{4,}(?![A-Za-z0-9_])(?!\.md)"


def caps_hits(files):
    """Слова капсом в пользовательских строках: литералы и докстринги модулей
    (usage и описания инструментов MCP). Докстринги функций — русские
    комментарии, строки без единой строчной буквы — заголовки таблиц и токены."""
    import ast
    import re
    hits = []
    for f in files:
        with open(f) as fh:
            tree = ast.parse(fh.read())
        skip = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body \
                    and isinstance(n.body[0], ast.Expr) \
                    and isinstance(n.body[0].value, ast.Constant):
                skip.add(id(n.body[0].value))
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str)) or id(n) in skip:
                continue
            if not re.search(r"[a-zа-я]", n.value):
                continue
            for m in re.finditer(CAPS_WORD, n.value):
                if m.group() not in CAPS_OK:
                    hits.append(f"{os.path.relpath(f, ROOT)}:{n.lineno}: {m.group()}")
    return hits


def run_server(main, argv):
    from mop.cli.server import _maintenance
    with patched(_maintenance, require=lambda: None):
        return run_command(main, argv)


def check_output_rules(c):
    """STATUS: FIXED — see #159"""
    import glob
    import re
    from mop.common import bus, config, llm, puppets
    files = sorted(glob.glob(os.path.join(ROOT, "mop", "cli", "**", "*.py"), recursive=True)) \
        + [os.path.join(ROOT, "mop", "client", "channel.py"), os.path.join(ROOT, "mop", "node", "agent.py")]
    for hit in caps_hits(files):
        c.fail(f"caps for emphasis: {hit}")

    os.environ.setdefault("MOP_SERVER_LAN", "10.0.0.1")
    with restored(bus, "call_cluster"), restored(puppets, "running_alloc", "diagnose"), \
            restored(lib, "guard"), restored(config, "read_env"), restored(llm, "profiles"):
        bus.call_cluster = lambda verb, **kw: {"ok": True}
        puppets.running_alloc = lambda name: {"ClientStatus": "running", "NodeName": "n1"}
        lib.guard = lambda name: {"ok": True, "meta": {"origin": "git@h:g/mop.git"}}
        # Быстрая команда на успехе молчит: MCP ответит модели `done`.
        from mop.cli.node import forget
        # #642 drain/restart печатают versioned admission/result, а не молчат
        # на accepted; HTTP/TLS и exit semantics — в control_consumers/control_tls.
        for what, main, argv in (("node forget", forget.main, ["n1"]),):
            out, err, code = run_command(main, argv)
            c.check(f"{what} must be silent on success: out {out!r} err {err!r} code {code!r}",
                    not (out or err or code))
        from mop.cli.server import doctor
        from mop.server.doctor import proxy as proxy_group
        # Здоровый пул — одна строка результата, её и показываем. Прокси
        # заглушен: его живость -- про контроллер, не про этот вывод.
        puppets.diagnose = lambda: []
        proxy_group.diagnose = lambda: []
        out, _, _ = run_server(doctor.main, [])
        c.expect("doctor on a healthy pool", out, "pool is healthy: nothing stuck\n")
        # Советов «run X» в успешном выводе нет: таблица уже говорит [restart].
        puppets.diagnose = lambda: [{"name": "pu-mop-1", "alloc": {"NodeName": "n1"},
                                     "diagnosis": "HUNG (not responding)", "action": "restart"}]
        out, _, _ = run_server(doctor.main, [])
        for what, text in (("doctor", out),):
            c.check(f"{what} advises another command on success: {text!r}",
                    not (re.search(r"\bmop [a-z]+", text)))
        del proxy_group.diagnose
    check_output_rest(c)


def check_output_rest(c):
    """Остаток #159: setup, sweep, driver build. STATUS: FIXED — see #159"""
    import shutil
    import subprocess
    from mop.common import bus, puppets
    from mop.server import image, playvars
    from mop.cli.driver import build
    from mop.cli.pool import setup
    from mop.cli.server import sweep

    # setup: шаг установки — строка хода на терминале, не на терминале
    # тишина; предупреждение про claude — в stderr, stdout пуст.
    ran = []
    with restored(shutil, "which"), restored(subprocess, "run"), restored(playvars, "playbook_vars"):
        shutil.which = lambda cmd: "/usr/bin/sudo" if cmd == "sudo" else None
        subprocess.run = lambda args, **kw: ran.append(args) or subprocess.CompletedProcess(args, 0)
        playvars.playbook_vars = lambda: {}
        out, err, code = run_command(setup.main, [])
        # #259: mop setup -- машина оператора, флаг ушёл; контроллер --
        # mop server setup, с ролью controller и без слова про claude.
        _, _, refused = run_command(setup.main, ["--operator"])
        from mop.cli.server import setup as server_setup
        ran_before = len(ran)
        s_out, s_err, s_code = run_command(server_setup.main, [])
        server_role = [a for a in ran[ran_before:] if "ansible-playbook" in a]
    c.check(f"setup: stdout {out!r}, stderr {err!r}, code {code!r}, ran {len(ran)} commands",
            not (out or code or "claude is not in PATH" not in err or len(ran) - 3 != 3))
    c.check("#259 mop setup --operator must be a usage refusal: the flag is gone", not (not refused))
    c.check(f"#259 mop server setup: code {s_code!r}, stderr {s_err!r}, ran {server_role!r}",
            not (s_code or s_out or "claude" in s_err or not server_role
                 or '{"mop_role": "controller"}' not in server_role[0]))

    # sweep: нечего убирать — успех, и он молчит.
    with patched(puppets, ready_nodes=lambda: {"n1"}, jobs=lambda *a, **kw: [{"ID": "pu-a-1"}],
                 classify_junk=lambda answers, known: []), \
            patched(bus, request_many=lambda verb, nodes, **kw: {"n1": {"bodies": [], "templates": []}}):
        out, err, code = run_server(sweep.main, [])
    c.check(f"sweep with nothing to sweep must be silent: {out!r} {err!r} {code!r}", not (out or err or code))

    # driver build: без эха того, откуда прочитан .mop.
    with patched(image, prepare=lambda origin, root: {"project": "p", "asks": {}, "alien": [], "legacy": []},
                 build=lambda origin, got, **kw: {"gone": [], "announced": [], "rc": 0}):
        out, err, code = run_command(build.main, ["git@h:g/p.git"])
    c.check(f"driver build echoes on success: {out!r} code {code!r}", not (out or code))
    check_output_179(c)
    check_output_182(c)
    check_restore_all_188(c)


def check_output_179(c):
    """HYPOTHESIS (#179): после #159 остались отказы `mop sweep` в stdout (а
    «no ready nodes» ещё и с кодом 0 -- отказ читался успехом),
    предупреждения `mop driver build` в stdout и эхо `mop update` на успехе.
    SOLUTION: отказы и предупреждения -- в stderr теми же словами, «no ready
    nodes» -- код 1, update на успехе молчит (MCP ответит done, как restart).
    STATUS: FIXED — see #179"""
    from mop.common import bus, llm, puppets
    from mop.server import image
    from mop.cli.core import update
    from mop.cli.driver import build
    from mop.cli.server import sweep

    def in_stderr(got, text, nonzero):
        """Отказ только в stderr, с text и нужным кодом. -> (ok, подробность)."""
        out, err, code = got
        return (not (out or text not in err or (bool(code) != nonzero)),
                f"stdout {out!r}, stderr {err!r}, code {code!r}")

    with restored(puppets, "ready_nodes", "jobs", "classify_junk"), restored(bus, "request_many"):
        puppets.ready_nodes = lambda: set()
        c.check("sweep with no ready nodes",
                *in_stderr(run_server(sweep.main, []), "no ready nodes", True))
        puppets.ready_nodes = lambda: {"n1"}
        bus.request_many = lambda verb, nodes, **kw: {"n1": None}
        c.check("sweep where no node answered",
                *in_stderr(run_server(sweep.main, []), "no node answered", True))
        bus.request_many = lambda verb, nodes, **kw: {"n1": {"bodies": [], "templates": []}}
        puppets.jobs = lambda *a, **kw: []
        c.check("sweep where Nomad lists no puppets",
                *in_stderr(run_server(sweep.main, []), "Nomad lists no puppets", True))
        # Промолчавший узел при ответившем соседе: отказ по нему -- тоже в
        # stderr, а код выхода за него отвечает.
        bus.request_many = lambda verb, nodes, **kw: {"n1": {"bodies": [], "templates": []},
                                               "n2": None}
        puppets.ready_nodes = lambda: {"n1", "n2"}
        puppets.jobs = lambda *a, **kw: [{"ID": "pu-a-1"}]
        puppets.classify_junk = lambda answers, known: []
        c.check("sweep with one silent node",
                *in_stderr(run_server(sweep.main, []), "n2: no response", True))

    with patched(image, prepare=lambda origin, root: {"project": "p", "asks": {},
                                                      "alien": ["MOP_X"], "legacy": [".mop.yaml"]},
                 build=lambda origin, got, **kw: {"gone": [], "announced": [], "rc": 0}):
        out, err, code = run_command(build.main, ["git@h:g/p.git"])
    c.check(f"driver build warnings must go to stderr: {out!r} {err!r} {code!r}",
            not (out or code or "MOP_X is not a project's to set — ignored" not in err
                 or "read as .mop/sandbox.yaml for the transition" not in err))

    calls = []
    with patched(lib, guard=lambda name: {"ok": True, "meta": {"origin": "git@h:g/mop.git"}}), \
            patched(bus, call_cluster=lambda verb, **kw: calls.append(verb) or {"ok": True}), \
            patched(_common, workspace_text=lambda origin: ("", NOFILE_334)):
        # Эха параметров нет; единственная строка -- что уехало в bootstrap
        # (#334): её оператор сам не набирал.
        for argv in (["pu-mop-1"], ["pu-mop-1", "--fresh"], ["pu-mop-1", "git@h:g/other.git"]):
            calls.clear()
            out, err, code = run_command(update.main, argv)
            c.check(f"update {argv} must print only what bootstrap was sent: out {out!r} "
                    f"err {err!r} code {code!r} calls {calls}",
                    not (out != _common.sent_line(NOFILE_334) + "\n" or err or code
                         or calls != ["update"]))


def check_output_182(c):
    """HYPOTHESIS (#182): после #179 `mop driver build` эхом печатал
    содержимое манифеста («asks for k=v»), отчёт о пересозданных телах и
    объявлении образа, а `mop server cluster users` -- «changed/unchanged».
    SOLUTION: сборка показывает шаги (lib.Progress: строка на терминале,
    тишина не на терминале), факты -- шагами, а не отчётом; отказ подъёма
    папета и объявления образа -- stderr с именем; users на успехе молчит.
    STATUS: FIXED — see #182"""
    from mop.server import bootstrap, image, natsconf, nomad, spec
    from mop.common import projects
    from mop.cli.server.cluster import users
    from mop.cli.driver import build
    def through(main, argv):
        return run_command(main, argv, via_cli=True)
    with offline(), restored(image, "prepare", "build", "clear", "bake", "announce"), \
            restored(nomad, "register"), restored(spec, "job_spec"), restored(projects, "read"), \
            restored(natsconf, "apply", "reload", "apply_callout"):
        got = {"project": "p", "asks": {"MOP_CORES": "8"}, "alien": [], "legacy": []}
        image.prepare = lambda origin, root: got
        gone = [{"name": "pu-p-1", "origin": "git@h:g/p.git", "llm": "claude", "node": "hyper"}]
        # account (#570): заглушка clear заполняет список build'а, как
        # настоящая, -- иначе restore не видит снятых и отказ register
        # не проверяется вовсе.
        def fake_clear(project, force=False, api=None, node=None, account=None):
            out = [] if account is None else account
            out.extend(gone)
            return out
        image.clear = fake_clear
        image.bake = lambda *a, **kw: 0
        image.announce = lambda project, api=None, node=None, nodes=None: [("hyper", "announced, serves p"),
                                          ("gpu", "not a container node"),
                                          ("old", "already announced")]
        spec.job_spec = lambda *a, **kw: {}
        nomad.register = lambda job: None

        # Успех не на терминале -- тишина: ни эха манифеста, ни отчёта.
        out, err, code = through(build.main, ["git@h:g/p.git"])
        c.check(f"driver build must be silent on success off a TTY: "
                f"out {out!r} err {err!r} code {code!r}", not (out or err or code))

        # Папет не поднялся -- громко, с именем и причиной.
        def refuse(job):
            raise RuntimeError("Nomad said no")
        nomad.register = refuse
        out, err, code = through(build.main, ["git@h:g/p.git"])
        c.check(f"a failed re-raise must reach stderr with the puppet: "
                f"out {out!r} err {err!r} code {code!r}",
                not (out or not code or "pu-p-1" not in err or "Nomad said no" not in err))
        nomad.register = lambda job: None

        # Узел, которому образ объявить не вышло (#175), -- stderr.
        image.announce = lambda project, api=None, node=None, nodes=None: [("hyper", "announced, serves p"),
                                          ("bad", "bad: unknown driver 'bogus'")]
        out, err, code = through(build.main, ["git@h:g/p.git"])
        c.check(f"a failed announcement must reach stderr alone: "
                f"out {out!r} err {err!r} code {code!r}",
                not (out or "bad: unknown driver 'bogus'" not in err or "hyper" in err))

        # cluster users на успехе молчит -- и с --reload.
        projects.read = lambda: ["git@h:g/p.git"]
        natsconf.apply = lambda names, creds: (True, None)
        natsconf.reload = lambda: None
        # callout.conf (#206) -- тот же /etc/nats: здесь не пишется.
        natsconf.apply_callout = lambda: False
        for argv in ([], ["--reload"]):
            out, err, code = through(users.main, argv)
            c.check(f"cluster users {argv} must be silent on success: "
                    f"out {out!r} err {err!r} code {code!r}", not (out or err or code))

        # #534: users читает взгляд реестра, а не устаревший легаси-файл.
        # После миграции правда на диске -- projects.json; projects.read()
        # видит только остывшую пару, и команда заводила бы пользователей
        # без новых проектов.
        seen = []
        natsconf.apply = lambda names, creds: (seen.append(sorted(names)), (True, None))[1]
        with tempfile.TemporaryDirectory(prefix="mop-test-534u-") as d:
            with open(os.path.join(d, "projects.json"), "w") as f:
                f.write(json.dumps({"version": 1, "projects": [
                    {"origin": "git@h:g/p.git"}, {"origin": "git@h:team/new.git"}]}))
            with restored(projects, "SNAPSHOT", "SNAPSHOT_DIR"), \
                    restored(projects, "read", "view_of", "view_lines"):
                projects.SNAPSHOT = os.path.join(d, "projects.json")
                projects.SNAPSHOT_DIR = d
                projects.read = lambda: ["git@h:g/p.git"]  # остывшая пара
                out, err, code = through(users.main, [])
        c.expect("#534 cluster users reads the current registry view",
                 seen, [["new", "p"]])

        # #542: cluster export -- офлайн-выгрузка пары из живого снимка.
        # Один позиционный аргумент; usage на прочем; обычный успех ТИХИЙ
        # (сводка -- в машинном маркере export.json и в возврате библиотеки,
        # не в обязательной строке stdout, ревью 2); отказы кодом без эха
        # origin'ов (креды живут в закрытых файлах пары).
        from mop.cli.server.cluster import export as export_cmd
        with tempfile.TemporaryDirectory(prefix="mop-test-542x-") as d:
            secret = "https://user:secret-token@forge.example/team/x.git"
            with open(os.path.join(d, "projects.json"), "w") as f:
                f.write(json.dumps({"version": 1, "projects": [
                    {"origin": secret, "limit": 2}]}))
            dest = os.path.join(d, "rollback")
            with restored(projects, "SNAPSHOT", "SNAPSHOT_DIR"):
                projects.SNAPSHOT = os.path.join(d, "projects.json")
                projects.SNAPSHOT_DIR = d
                out, err, code = through(export_cmd.main, [dest])
                c.check("#542 cluster export stays quiet on an ordinary success",
                        code == 0 and not err and not out, (out, err, code))
                out, err, code = through(export_cmd.main, [dest, "--json"])
                c.check("#542 export --json is a usage refusal: one directory",
                        code != 0 and not out, (out, err, code))
                marker = os.path.join(dest, "export.json")
                with open(marker) as f:
                    marker_json = json.load(f)
                c.check("#542 the completed marker stays machine-readable",
                        marker_json.get("schema") == "legacy-pair"
                        and marker_json.get("projects") == 1
                        and "secret-token" not in open(marker, errors="ignore").read(),
                        marker_json)
                c.check("#542 the exported directory carries the private pair",
                        os.path.exists(os.path.join(dest, "projects"))
                        and os.path.exists(os.path.join(dest, "limits.json"))
                        and os.path.exists(os.path.join(dest, "export.json")), dest)
                out, err, code = through(export_cmd.main, [dest])
                c.check("#542 a repeat export into the same dir refuses",
                        code != 0 and not out and "already exists" in str(code),
                        (out, err, code))
                out, err, code = through(export_cmd.main, [])
                c.check("#542 export without a directory is a usage refusal",
                        code != 0 and not out, (out, err, code))
                out, err, code = through(export_cmd.main, ["--flag", dest])
                c.check("#542 a flag-shaped argument is a usage refusal",
                        code != 0 and not out, (out, err, code))
                other = os.path.join(d, "other")
                os.makedirs(other)
                out, err, code = through(export_cmd.main, [other])
                c.check("#542 an existing directory destination refuses over the CLI",
                        code != 0 and not out and "already exists" in str(code),
                        (out, err, code))
                c.check("#542 the refused CLI export left the directory empty",
                        not os.listdir(other), os.listdir(other))


def check_restore_all_188(c):
    """HYPOTHESIS (#188): image.restore поднимал папетов до первого отказа
    Nomad; остальные снятые в `gone` не пробовались вовсе, и об этом не
    говорил никто -- пересборка оставляла их лежать.
    SOLUTION: пробовать каждого, отказы собрать и бросить одним исключением,
    по строке на папета («<папет> on <узел>: not raised again: <причина>»).
    STATUS: FIXED — see #188"""
    from mop.server import image, nomad, spec
    gone = [{"name": f"pu-p-{i}", "origin": "git@h:g/p.git",
             "node": "hyper"} for i in (1, 2, 3)]
    with offline(), restored(nomad, "register"), restored(spec, "job_spec"):
        spec.job_spec = lambda name, origin, **kw: {"ID": name}
        for refused in ({"pu-p-1"}, {"pu-p-1", "pu-p-3"}, set()):
            registered = []

            def register(job, refused=refused):
                if job["ID"] in refused:
                    raise RuntimeError(f"Nomad refused {job['ID']}")
                registered.append(job["ID"])
            nomad.register = register
            try:
                image.restore(gone)
                err = None
            except RuntimeError as e:
                err = str(e)
            want = [p["name"] for p in gone if p["name"] not in refused]
            c.expect(f"restore with {sorted(refused)} refused: registered", registered, want)
            if not refused:
                c.check(f"restore with nothing refused raised: {err!r}", not (err is not None))
                continue
            lines = (err or "").splitlines()
            want_lines = [f"{n} on hyper: not raised again: Nomad refused {n}"
                          for n in sorted(refused)]
            c.expect(f"restore with {sorted(refused)} refused: {err!r}", lines, want_lines)



def check_empty_value(c):
    """HYPOTHESIS (#327): parse_value сворачивал пустое значение в None --
    тот же ответ, что «флага нет»: `mop add --cred` без имени молча
    заводил папета на первой активной аренде профиля. SOLUTION: пустое
    значение -- ошибка использования, как у --llm (#164).
    RESULT: четыре пустых формы отказывают одной строкой через диспетчер.
    STATUS: FIXED — see #327"""

    def through_dispatcher(argv):
        return run_command(lambda x: lib.parse_value(x, "--cred") and None,
                           argv, via_cli=True)
    for argv in (["pu-mop-1", "--cred"], ["--cred="], ["--cred", ""],
                 ["--cred", "--fresh"]):
        out, err, code = through_dispatcher(argv)
        lines = err.strip().splitlines()
        c.check(f"parse_value({argv}): out {out!r} err {err!r} code {code!r}",
                not (out or not code or len(lines) != 1 or "Traceback" in err
                     or lines[0] != "--cred needs a value"))
    c.expect("parse_value with a value",
             lib.parse_value(["pu-mop-1", "--cred", "alice", "--fresh"], "--cred"),
             ("alice", ["pu-mop-1", "--fresh"]))
    c.expect("parse_value with flag=value",
             lib.parse_value(["--cred=alice", "pu-mop-1"], "--cred"),
             ("alice", ["pu-mop-1"]))
    c.expect("parse_value without the flag must leave the value unset",
             lib.parse_value(["pu-mop-1", "--fresh"], "--cred"),
             (None, ["pu-mop-1", "--fresh"]))


# HYPOTHESIS: CLI и запрос сборщику не имеют общего явного выбора ветки.
# SOLUTION: явная remote-ветка и SHA проходят одним источником до зеркала.
# RESULT: default HEAD не подменяет выбранные исходники прогрева.
# STATUS: FIXED — see #593
def check_image_branch_cli_593(c):
    from mop.cli.driver import build
    from mop.cli.project import add
    from mop.common import bus, manifest
    from mop.server import image
    got = {"project": "p", "asks": {}, "alien": [], "legacy": []}
    seen = []
    def prepare(origin, root=None, **kw):
        seen.append((origin, root, kw))
        return got
    with patched(lib, origin=lambda *a: "git@h:g/p.git", git=lambda *a: "/local"), \
            patched(image, prepare=prepare,
                    build=lambda *a, **kw: {"announced": [], "rc": 0}):
        out, err, code = run_command(build.main, ["--branch", "swarm", "--node", "n1"])
    c.expect("#593 driver build accepts --branch", (out, err, code), ("", "", 0))
    c.expect("#593 driver branch reaches prepare, not local tree",
             seen, [("git@h:g/p.git", None, {"branch": "swarm"})])
    seen.clear()
    def fetch(origin, **kw):
        seen.append(("fetch", kw))
        return got
    def stream(*a, **kw):
        seen.append(("request", kw))
        return iter([{"done": True, "ok": True}])
    with patched(lib, cluster_check=lambda: None, origin=lambda *a: "git@h:g/p.git"), \
            patched(manifest, fetch=fetch), \
            patched(bus, call_cluster=lambda *a, **kw: {"ok": True},
                    ask_stream=stream, build_subject=lambda: "build"):
        out, err, code = run_command(add.main, ["--branch=swarm", "--node", "n1", "git@h:g/p.git"])
    c.expect("#593 project add accepts --branch", (out, err, code), ("", "", 0))
    c.expect("#593 project preflight selects the branch", seen[:1], [("fetch", {"branch": "swarm"})])
    requests = [value for kind, value in seen if kind == "request"]
    c.check("#593 build request retains branch and node", len(requests) == 1
            and requests[0].get("branch") == "swarm" and requests[0].get("node") == "n1", requests)
    flags = {arg["name"]: arg.get("flag") for arg in add.MCP["args"]}
    c.expect("#593 MCP exposes the same branch option", flags.get("branch"), "--branch")
    for command in (build.main, add.main):
        for argv in (["--branch"], ["--branch="], ["--branch", ""],
                     ["--branch", "--node", "n1"], ["--branch", "a", "--branch", "b"]):
            with patched(build, origin_of=lambda a: "git@h:g/p.git"), \
                    patched(lib, origin=lambda *a: "git@h:g/p.git", git=lambda *a: "/local"), \
                    patched(image, prepare=prepare, build=lambda *a, **kw: {"announced": [], "rc": 0}), \
                    patched(add, _add=lambda *a, **kw: 0):
                out, err, code = run_command(command, argv)
            c.check(f"#593 empty/repeated branch refuses {argv}", bool(code), (out, err, code))


def node_flag_sites():
    """Два командлета с --node: argv -> то, что дошло до работы, либо текст
    usage. Сеть и сборка подменены: проверяется только разбор."""
    from mop.cli.driver import build
    from mop.cli.project import add as project_add
    from mop.server import image

    def run_build(argv):
        seen = {}
        got = {"project": "p", "asks": {}, "alien": [], "legacy": []}

        def fake_build(origin, g, fresh, force, on_step, node):
            seen.update(origin=origin, fresh=fresh, force=force, node=node)
            return {"announced": [], "rc": 0}
        with patched(build, origin_of=lambda a: f"origin:{a}"), \
                patched(lib, origin=lambda a, doc: "origin:here", git=lambda *a: "/top"), \
                patched(image, prepare=lambda origin, root: got, build=fake_build):
            _, _, code = run_command(build.main, argv)
        return seen if not code else code

    def run_project(argv):
        seen = {}

        def fake_add(origin, name, mode, p, node=None):
            seen.update(origin=origin, mode=mode, node=node)
        with patched(project_add, _add=fake_add), \
                patched(lib, origin=lambda a, doc: f"origin:{a}"):
            _, _, code = run_command(project_add.main, argv)
        return seen if not code else code
    return (("driver build", run_build, build.__doc__.strip()),
            ("project add", run_project, project_add.__doc__.strip()))


def check_node_flag_320(c):
    """Характеристика --node до и после общего разборщика (#320): значение,
    флаг в середине, флага нет -- как было; забытое значение и повтор --
    usage, как было."""
    (b_name, run_build, b_doc), (p_name, run_project, p_doc) = node_flag_sites()
    c.expect(f"{b_name} --node n1", run_build(["--node", "n1"]),
             {"origin": "origin:here", "fresh": False, "force": False, "node": "n1"})
    c.expect(f"{b_name} --node in the middle", run_build(["--fresh", "--node", "n1", "proj"]),
             {"origin": "origin:proj", "fresh": True, "force": False, "node": "n1"})
    c.expect(f"{b_name} without --node", run_build(["proj", "--force"]),
             {"origin": "origin:proj", "fresh": False, "force": True, "node": None})
    c.expect(f"{p_name} --node n1", run_project(["--node", "n1"]),
             {"origin": "origin:None", "mode": "missing", "node": "n1"})
    c.expect(f"{p_name} --node in the middle",
             run_project(["-v", "--node", "n1", "--update", "git@h:g/p.git"]),
             {"origin": "origin:git@h:g/p.git", "mode": "update", "node": "n1"})
    c.expect(f"{p_name} without --node", run_project(["--rebuild", "git@h:g/p.git"]),
             {"origin": "origin:git@h:g/p.git", "mode": "rebuild", "node": None})
    for name, run, doc in node_flag_sites():
        for argv in (["--node"], ["proj", "--node"],
                     ["--node", "a", "--node", "b"], ["--node", "a", "proj", "--node", "b"]):
            c.expect(f"{name} {argv} answers with usage", run(argv), doc)


def check_one_parser_320(c):
    """#320: разбор «флаг со значением» и красная строка отказа -- по одному
    разу. Вывод -- характеристика (зелёная и до переноса); тождество
    функций -- сам перенос."""
    from mop.cli.dev.web import build as web_build
    E = "\033"
    for name, fn in (("lib.fail", lib.fail), ("web build fail", web_build.fail)):
        out, err, code = run_command(lambda a, fn=fn: fn("broken"), [])
        c.expect(f"{name} output", (out, err, code), ("", f"{E}[0;31mbroken{E}[0m\n", 0))
    out, _, _ = run_command(lambda a: lib.ok("fine"), [])
    c.expect("lib.ok output", out, f"{E}[0;32mfine{E}[0m\n")
    out, _, _ = run_command(lambda a: lib.section("Head"), [])
    c.expect("lib.section output", out, f"\n{E}[1mHead{E}[0m\n")
    _, _, code = run_command(lambda a: lib.usage("  doc text\n"), [])
    c.expect("lib.usage exits with the stripped doc", code, "doc text")
    _, _, code = run_command(web_build.main, ["--nope"])
    c.expect("web build usage", code, web_build.__doc__.strip())
    # Перенос: одно определение.
    c.check("web build fail is lib's fail", web_build.fail is lib.fail)
    c.check("parse_value lives in lib only",
            hasattr(lib, "parse_value") and not hasattr(_common, "parse_value"))
    # Повтор: без once -- последнее значение (--cred, --llm как были), с
    # once -- отказ; флаг в середине и `=` -- одинаково.
    c.expect("parse_value repeated: the last one wins",
             lib.parse_value(["--cred", "a", "x", "--cred=b"], "--cred"), ("b", ["x"]))
    for argv in (["--node", "a", "--node", "b"], ["--node=a", "x", "--node", "b"]):
        try:
            got = lib.parse_value(argv, "--node", once=True)
        except RuntimeError as e:
            got = str(e)
        c.expect(f"parse_value once {argv}", got, "--node given twice")
    c.expect("parse_value once, a single flag in the middle",
             lib.parse_value(["x", "--node", "a", "y"], "--node", once=True), ("a", ["x", "y"]))


def check_node_flag_340(c):
    """HYPOTHESIS (#340): --node разбирался руками: `--node ""` и `--node -x`
    брали пустое или флаг за имя узла -- `mop project add --node ""` молча
    собирал на всех узлах, как --cred до #327; `--node=NAME` не понимался.
    SOLUTION: оба места -- lib.parse_value(once=True), отказ -- usage.
    RESULT: пустое и флаг вместо имени -- usage, `--node=NAME` понят.
    STATUS: FIXED — see #340"""
    for name, run, doc in node_flag_sites():
        for argv in (["--node", ""], ["--node", "", "proj"], ["--node", "-x", "proj"],
                     ["--node="], ["--node=", "proj"]):
            c.expect(f"{name} {argv} answers with usage", run(argv), doc)
        got = run(["--node=n1", "proj"])
        c.check(f"{name} --node=n1 reaches the work with n1: {got!r}",
                isinstance(got, dict) and got.get("node") == "n1")

# ── #186: драйвер узла из инвентаря — до плейбука ────────────────────────────
# Вывод настоящего `ansible-inventory --list` (ansible-core 2.21) на инвентаре:
#   puppet: plain (без переменной), explicit (mop_driver: host), hyper,
#           typo (mop_driver: pvee); proxmox: hyper, vars mop_driver: pve;
#   server: localhost.
# Хост без своих переменных есть только в списке группы, в _meta.hostvars
# его нет, — и драйвер у него MOP_DRIVER, как у `mop_driver | default(MOP_DRIVER)`
# в шаблонах.
INVENTORY_TYPO = {
    "_meta": {"hostvars": {
        "explicit": {"mop_driver": "host"},
        "hyper": {"mop_driver": "pve", "mop_pve_vmid_base": "9000"},
        "localhost": {"ansible_connection": "local"},
        "typo": {"mop_driver": "pvee"}}},
    "all": {"children": ["ungrouped", "server", "puppet", "proxmox"]},
    "proxmox": {"hosts": ["hyper"]},
    "puppet": {"hosts": ["plain", "explicit", "hyper", "typo"]},
    "server": {"hosts": ["localhost"]},
}
INVENTORY_CLEAN = {**INVENTORY_TYPO, "_meta": {"hostvars": {
    **INVENTORY_TYPO["_meta"]["hostvars"], "typo": {"mop_driver": "pve"}}}}


def check_inventory_drivers(c):
    """HYPOTHESIS (#186): опечатка в mop_driver инвентаря доезжала до meta
    Nomad, и ловили её только читатели (driver.of_node, #175). SOLUTION: deploy
    прогоняет драйвер каждого хоста через of_node до плейбука.
    STATUS: FIXED — see #186"""
    from mop.cli.server import deploy
    fn = getattr(deploy, "driver_refusals", None)
    got = fn(INVENTORY_TYPO, "host") if fn else None
    c.check(f"driver_refusals: {got!r}, wanted one refusal for typo/pvee",
            not (got is None or len(got) != 1 or not got[0].startswith("typo: unknown driver 'pvee'")))
    clean = fn(INVENTORY_CLEAN, "host") if fn else None
    c.check(f"a clean inventory refused: {clean!r}", not (fn and clean))
    # Пусто и нет ключа -- драйвер по умолчанию, как у of_node; а битый
    # MOP_DRIVER ловится на тех, кому он достаётся.
    if fn:
        bad = fn(INVENTORY_CLEAN, "pvee")
        c.expect(f"a broken MOP_DRIVER must name the hosts that inherit it: {bad!r}",
                 sorted(r.split(":")[0] for r in bad), ["localhost", "plain"])
        empty = {**INVENTORY_CLEAN, "_meta": {"hostvars": {"plain": {"mop_driver": ""}}}}
        c.check("an empty mop_driver is the default driver, not a refusal", not (fn(empty, "host")))


def check_loopback_switch_558(c):
    """HYPOTHESIS (#558): с флагом «закрыть порт» (MOP_LLM_PROXY_LAN=0) в
    .env может остаться MOP_PROXY_URL, смотрящий на LAN:8317, -- например
    с IP вместо имени: канонизация #557 узнаёт только собственный корень
    СВОЕГО authority по MOP_SERVER_LAN, и чужое написание того же адреса
    проходит насквозь. Спека папета собралась бы с адресом мимо TLS, а
    раскатка закрыла бы порт и убила бы всех папетов этой установки.
    SOLUTION: отказ префлайта `mop server deploy` -- адрес из llm.env() с
    настройками установки не должен смотреть на MOP_LLM_PROXY_PORT, когда
    переключатель в положении «петля». Что все ЗАПУЩЕННЫЕ папеты уже
    перерегистрированы, проверяет оператор -- это в docs/PROXY.md.
    STATUS: RED"""
    from mop.cli.server import deploy
    from mop.common import config, llm
    fn = getattr(deploy, "loopback_refusals", None)
    if not c.check("no deploy.loopback_refusals", fn is not None):
        return

    def settings(lan, https, llm_port, proxy_url):
        return patched(config, get=lambda name: {
            "MOP_SERVER_LAN": lan, "MOP_HTTPS_PORT": https,
            "MOP_LLM_PROXY_PORT": llm_port,
            "MOP_PROXY_URL": proxy_url}.get(name, ""))

    # Канонизируемое имя уходит на публичный origin -- отказа нет даже при
    # закрытом порту: nginx и петля кормят всех. IP-написание того же
    # адреса канонизация не узнаёт -- отказ: спека смотрела бы на порт,
    # который раскатка закрывает. Чужой адрес -- осознанный оверрайд.
    cases = [
        ("the flag off", "1", "srv.test", "8443", "8317", "", 0),
        ("own root is canonicalized", "0", "srv.test", "8443", "8317",
         "http://srv.test:8317", 0),
        ("the empty default rides the public origin", "0", "srv.test", "8443",
         "8317", "", 0),
        ("an IP spelling of the same port", "0", "srv.test", "8443", "8317",
         "http://10.0.0.1:8317", 1),
        ("a foreign host on the installation's port", "0", "srv.test", "8443",
         "8317", "http://foreign.test:8317", 1),
        ("a path on the same port is unknown ground", "0", "srv.test", "8443",
         "8317", "http://srv.test:8317/llm", 1),
        ("the public origin itself", "0", "srv.test", "8443", "8317",
         "https://srv.test:8443", 0),
        ("a broken flag value", "x", "srv.test", "8443", "8317", "", 1),
    ]
    for what, flag, lan, https, llm_port, url, want in cases:
        with settings(lan, https, llm_port, url):
            with patched(config, get=lambda name, _g=config.get: {
                "MOP_LLM_PROXY_LAN": flag}.get(name, _g(name))):
                got = fn(config.get)
                c.check(f"loopback_refusals, {what}: {got!r}",
                        want == (1 if got else 0), got)

    # Отказ обязан называть настройку и адрес, который он увидел: иначе
    # оператор не узнает, что править.
    with settings("srv.test", "8443", "8317", "http://10.0.0.1:8317"):
        with patched(config, get=lambda name, _g=config.get: {
                "MOP_LLM_PROXY_LAN": "0"}.get(name, _g(name))):
            text = " ".join(fn(config.get))
            c.check(f"loopback_refusals must name the setting and the address: {text!r}",
                    "MOP_PROXY_URL" in text and "10.0.0.1:8317" in text)

    # Сам адрес сравнивается с портом, а не со строкой литерала: env()
    # строит origin, refusal разбирает -- оба конца одного контракта.
    with settings("srv.test", "8443", "8317", "http://srv.test:8317/"):
        with patched(config, get=lambda name, _g=config.get: {
                "MOP_LLM_PROXY_LAN": "0"}.get(name, _g(name))):
            c.check("loopback_refusals rides the same origin contract as llm.env",
                    not fn(config.get))


def check_node_memory_197(c):
    """HYPOTHESIS (#197): строка mop_mem_mb в инвентаре не работает нигде:
    на host-узле спеку строит сервер своим MOP_MEM_MB (и #190 отказывал),
    на pve память тела шла из образа, то есть из `.mop`. SOLUTION: память --
    свойство папета (дефолт -> .env -> `.mop`), строку deploy отвергает на
    любом узле; потолок узла mop_body_mem_cap_mb остаётся узловым и едет в
    meta Nomad, где `>=` сравнивает численно только целые -- поэтому потолок
    не из одного целого числа тоже отказ.
    STATUS: FIXED — see #197"""
    from mop.cli.server import deploy
    with offline():
        fn = getattr(deploy, "memory_refusals", None)
        if not c.check("no deploy.memory_refusals", fn is not None):
            return

        def listing(hostvars, groups=None):
            out = {"_meta": {"hostvars": hostvars},
                   "puppet": {"hosts": ["plain", "hyper", "odd"]}}
            out.update(groups or {})
            return out
        cases = [
            ("clean", listing({"hyper": {"mop_driver": "pve"}}), "32768", []),
            ("mop_mem_mb on a host node", listing({"odd": {"mop_mem_mb": "12288"}}), "32768",
             ["odd: mop_mem_mb"]),
            ("mop_mem_mb on a pve node", listing({"hyper": {"mop_driver": "pve", "mop_mem_mb": 8192}}),
             "32768", ["hyper: mop_mem_mb"]),
            # Как на mop.corp.ermak.dev: строка в группе puppet. ansible-inventory
            # --list сводит её в hostvars каждого хоста; --export оставил бы в
            # vars группы -- отказ обязан видеть оба места.
            ("mop_mem_mb in a group's vars",
             listing({}, {"puppet": {"hosts": ["plain"], "vars": {"mop_mem_mb": "8192"}}}),
             "32768", ["puppet: mop_mem_mb"]),
            ("a node's cap is a node setting", listing({"hyper": {"mop_body_mem_cap_mb": "65536"}}),
             "32768", []),
            ("a node's cap from YAML as a number", listing({"hyper": {"mop_body_mem_cap_mb": 65536}}),
             "32768", []),
            ("a node's cap in gigabytes", listing({"hyper": {"mop_body_mem_cap_mb": "32G"}}),
             "32768", ["hyper: mop_body_mem_cap_mb"]),
            ("a node's cap with a space", listing({"hyper": {"mop_body_mem_cap_mb": " 32768"}}),
             "32768", ["hyper: mop_body_mem_cap_mb"]),
            ("a node's cap as a fraction", listing({"hyper": {"mop_body_mem_cap_mb": "1.5"}}),
             "32768", ["hyper: mop_body_mem_cap_mb"]),
            ("an empty cap", listing({"hyper": {"mop_body_mem_cap_mb": ""}}),
             "32768", ["hyper: mop_body_mem_cap_mb"]),
            # Потолок установки достаётся каждому хосту без своей строки.
            ("the installation's cap is broken", listing({"hyper": {"mop_body_mem_cap_mb": "4096"}}),
             "32G", ["MOP_BODY_MEM_CAP_MB"]),
        ]
        for what, lst, cap, want in cases:
            got = fn(lst, cap)
            heads = sorted(g.split("=")[0] for g in got)
            c.expect(f"memory_refusals, {what}: {got!r}", heads, sorted(want))
        got = fn(listing({"odd": {"mop_mem_mb": "12288"}}), "32768")
        text = " ".join(got)
        c.check(f"memory_refusals must say where memory comes from now: {got!r}",
                not (not ("not a node setting" in text and ".env" in text and ".mop" in text
                          and "remove the line" in text)))


def check_pool_uniform(c):
    """HYPOTHESIS (#190): спеку папета строит сервер своими MOP_HOME,
    MOP_USER, MOP_PUPPET_SEED, MOP_MEM_MB, а агент и `mop driver run` на узле
    читают их из node.env этого узла. Строка хоста в инвентаре, перекрывшая
    любую из них, молча разводит врапер и агента: клон ложится туда, куда
    агент не смотрит. SOLUTION: config.POOL_UNIFORM и отказ deploy до
    плейбука, если хост задаёт им значение, отличное от установки.
    STATUS: FIXED — see #190

    MOP_MEM_MB здесь больше нет (#197): память -- свойство папета, не узла,
    и строку mop_mem_mb в инвентаре отвергает check_node_memory_197 на любом
    узле, с любым значением."""
    from mop.common import config
    from mop.cli.server import deploy
    with offline():
        uniform = getattr(config, "POOL_UNIFORM", None)
        c.expect("config.POOL_UNIFORM: paths and user on every host",
                 set(uniform or ()), {"MOP_HOME", "MOP_USER", "MOP_PUPPET_SEED"})
        c.check("config.HOST_UNIFORM is gone with #197: MOP_MEM_MB is not a node setting",
                not (hasattr(config, "HOST_UNIFORM")))
        fn = getattr(deploy, "uniform_refusals", None)
        installed = {n: config.get(n) for n in (uniform or ())}

        def listing(hostvars):
            return {"_meta": {"hostvars": hostvars},
                    "puppet": {"hosts": ["plain", "hyper", "odd"]}}
        cases = [
            ("an override", {"odd": {"mop_home": "/srv/elsewhere"}}, ["odd: mop_home"]),
            ("two settings on one host", {"odd": {"mop_user": "someone",
                                                  "mop_home": "/srv/x"}},
             ["odd: mop_home", "odd: mop_user"]),
            ("the same value", {"odd": {"mop_home": installed.get("MOP_HOME")}}, []),
            ("no override", {"hyper": {"mop_driver": "pve"}}, []),
            # Память -- не этой проверки (#197): её отвергает своя.
            ("mop_mem_mb is not a uniform setting", {"odd": {"mop_mem_mb": "8192"}}, []),
            # Пути и пользователь -- общие и для контейнерного узла.
            ("mop_home on a pve node", {"hyper": {"mop_driver": "pve", "mop_home": "/srv/x"}},
             ["hyper: mop_home"]),
        ]
        for what, hv, want in cases:
            got = fn(listing(hv), installed, "host") if fn else None
            heads = sorted(g.split("=")[0] for g in got) if got is not None else None
            c.expect(f"uniform_refusals, {what}: {got!r}", heads, want)


def check_deploy_check(c):
    """HYPOTHESIS (#177): доказать, что правка deploy/ не меняет узлы, было
    нечем — `mop server deploy` аргументов не берёт, и #157 подкладывал на PATH
    обёртку ansible-playbook. А после плейбука deploy пишет файлы
    (creds.collect). SOLUTION: `mop server deploy --check` — --check --diff каждому
    ansible-playbook, и ничего пишущего после плейбука. STATUS: FIXED — see #177"""
    import shutil
    import subprocess
    from mop.common import bus, config, creds, projects
    from mop.server import playvars
    from mop.cli.server import deploy
    d = tempfile.mkdtemp(prefix="mop-test-deploy-check-")
    inventory, key = os.path.join(d, "inventory.yaml"), os.path.join(d, "id")
    for f in (inventory, key, key + ".pub"):
        open(f, "w").close()
    # Один человек в файле операторов: без людей deploy отказывает (#219).
    from mop.server import identity
    people = os.path.join(d, "operators")
    with open(people, "w") as f:
        f.write(identity.format_line(identity.Identity("anton", "admin", ("*",)),
                                     identity.hash_password("x")) + "\n")
    calls, collected, checked = [], [], []
    listing = [INVENTORY_CLEAN]

    def run_mock(argv):
        """Прогон в игре (#475): игра пишется в список, ansible-inventory
        отвечает листингом, плейбук -- пустым выводом и нулём."""
        calls.append(argv)
        text = json.dumps(listing[0]) if argv and argv[0] == "ansible-inventory" else ""
        return subprocess.CompletedProcess(argv, 0, text, "")

    # Окружение -- снимком до предохранителя: после проверки оно целиком
    # прежнее, без MOP_SERVER_LAN предохранителя и без INVENTORY здесь.
    env = dict(os.environ)
    try:
        # #475: play() собирает вывод прогона (subprocess.run) и возвращает
        # (код, вывод); игры пишет тот же mock, а ansible-inventory
        # отвечает листингом.
        with offline(), \
                patched(shutil, which=lambda cmd: f"/usr/bin/{cmd}"), \
                patched(config, require=lambda *a: None), \
                patched(subprocess, run=lambda argv, **kw: run_mock(argv)), \
                patched(playvars, playbook_vars=lambda: {}), \
                patched(deploy, missing_extras=lambda setting, root: [],
                        link=lambda name, target: None, manifests=lambda origins: {},
                        check=lambda: checked.append(1)), \
                patched(bus, ask_cluster=lambda *a, **kw: {"ok": True, "projects": []}), \
                patched(projects, for_deploy=lambda answer, local: ([], None), read=lambda: []), \
                patched(creds, collect=lambda *a, **kw: collected.append(1) or [],
                        operator=lambda dest: "anton",
                        snapshot=lambda dest: ("human", {"user": "anton",
                                                         "password": "x"})):
            os.environ.update({"INVENTORY": inventory, "MOP_GIT_KEY": key,
                               "MOP_OPERATORS_FILE": people})
            for argv, dry in (([], False), (["--check"], True)):
                calls.clear(), collected.clear(), checked.clear()
                out, err, code = run_command(deploy.main, argv)
                plays = [cmd for cmd in calls if cmd and cmd[0] == "ansible-playbook"]
                flags = {"--check", "--diff"} & {a for cmd in plays for a in cmd}
                c.check(f"deploy {argv}: code {code!r}, plays {plays!r}, err {err!r}",
                        not (code or not plays or flags != ({"--check", "--diff"} if dry else set())))
                # #178: хосты инвентаря едут плейбуку -- их файлом кладёт роль
                # cluster, и по нему forget отказывает узлу, который deploy
                # поставил бы снова.
                sent = [json.loads(cmd[i + 1]) for cmd in plays for i, a in enumerate(cmd)
                        if a == "--extra-vars"]
                hosts = [v["mop_inventory_hosts"] for v in sent if "mop_inventory_hosts" in v]
                c.expect(f"deploy {argv} must send the inventory's hosts to the playbook (#178)",
                         hosts, [["explicit", "hyper", "localhost", "plain", "typo"]])
                c.check("deploy --check must not collect server credentials (it writes)",
                        not (dry and collected))
                c.check("deploy without --check must still collect server credentials",
                        not (not dry and not collected))
            # #335: гейт спрашивает пайплайн коммита на ветке раскатки --
            # ветке origin по умолчанию, а не последний на любой ветке (там
            # 27.09 оказался идущий пайплайн эпика на том же коммите).
            from mop.common import gitlab
            asked, real_get = [], config.get

            def pipeline(sha, ref=None):
                asked.append((sha, ref))
                return {"id": 1, "status": "success", "web_url": "u"}
            with patched(config, get=lambda name, default=None: "1"
                         if name == "MOP_DEPLOY_NEEDS_GREEN" else real_get(name, default)), \
                    patched(gitlab, has_credentials=lambda: True, pipeline=pipeline,
                            jobs=lambda pid: []), \
                    patched(deploy, head_sha=lambda root: "5b92b440c952",
                            ci_state=lambda root: ("feature", "master", [])):
                calls.clear(), collected.clear()
                out, err, code = run_command(deploy.main, [])
                c.expect(f"#335 deploy gate asks the default branch's pipeline (err {err!r})",
                         (code, asked), (0, [("5b92b440c952", "master")]))
            # Прочие аргументы — по-прежнему отказ, и до плейбука.
            for argv in (["pool"], ["git@h:g/p.git"], ["--check", "extra"], ["--diff"]):
                calls.clear()
                out, err, code = run_command(deploy.main, argv)
                c.check(f"deploy {argv} must be refused before any playbook: code {code!r}",
                        not (not code or calls))
            # #190: перекрытая на хосте настройка, из которой сервер строит спеку.
            listing[0] = {**INVENTORY_CLEAN, "_meta": {"hostvars": {
                **INVENTORY_CLEAN["_meta"]["hostvars"], "plain": {"mop_home": "/srv/elsewhere"}}}}
            for argv in ([], ["--check"]):
                calls.clear(), collected.clear()
                out, err, code = run_command(deploy.main, argv)
                plays = [cmd for cmd in calls if cmd and cmd[0] == "ansible-playbook"]
                lines = [lib.plain(l) for l in err.strip().splitlines()]
                c.check(f"deploy {argv} over a host override of MOP_HOME: code {code!r}, "
                        f"plays {plays!r}, err {err!r}",
                        not (code != 1 or plays or len(lines) != 1
                             or not lines[0].startswith("plain: mop_home=")))
            # #186: опечатка в драйвере хоста — отказ до плейбука, строка на хост.
            listing[0] = INVENTORY_TYPO
            for argv in ([], ["--check"]):
                calls.clear(), collected.clear()
                out, err, code = run_command(deploy.main, argv)
                plays = [cmd for cmd in calls if cmd and cmd[0] == "ansible-playbook"]
                lines = [lib.plain(l) for l in err.strip().splitlines()]
                c.check(f"deploy {argv} over a driver typo: code {code!r}, "
                        f"plays {plays!r}, err {err!r}",
                        not (code != 1 or plays or collected or len(lines) != 1
                             or not lines[0].startswith("typo: unknown driver 'pvee'")))
    finally:
        os.environ.clear()
        os.environ.update(env)


def check_agent_on_node_172(c):
    """#172: юнит агента переезжает с `python3 -m mop.agent` на `mop agent`,
    а на узле .env нет -- только node.env. Считалось, что диспетчер откажет
    там на REQUIRED; не отказывает: require зовёт только lib.cluster, а
    командлет агента его не берёт. Проверка зелёная с первого прогона и
    закрепляет нынешнее поведение, а не чинит: отказ здесь уронил бы агента
    на каждом узле, как только юнит переедет. Живьём то же на hyper:
    `cd / && ~/mop/bin/mop agent --check` -- exit 0, subscribed.

    Узел здесь -- временный дом с одним node.env, .env нет, каталог `/`.
    Дальше отказа на настройках команда дойти обязана: до агента -- его
    отказ на кредах шины, либо, без nats-py, отказ командлета про библиотеку.
    STATUS: FIXED — see #172"""
    import subprocess
    import tempfile
    home = tempfile.mkdtemp(prefix="mop-node-172-")
    os.makedirs(os.path.join(home, ".config", "mop"))
    with open(os.path.join(home, ".config", "mop", "node.env"), "w") as f:
        f.write("MOP_DRIVER=host\nMOP_USER=mopuser\n")
    env = hermetic.child_env({"HOME": home,
                              "MOP_ENV_FILE": os.path.join(home, "no-such.env"),
                              "MOP_NODE": "hyper"})
    r = subprocess.run(["bash", os.path.join(ROOT, "bin", "mop"), "agent", "--check"],
                       cwd="/", env=env, capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    reached = "no bus credentials" in out or "bus library needed" in out
    c.check("mop agent --check on a node with node.env and no .env must reach the agent",
            not ("required settings not filled" in out or not reached),
            f"rc {r.returncode}, {out.strip()!r}")


def check_bus_import_169(c):
    """HYPOTHESIS (#169): mop/common/bus.py при импорте без nats-py зовёт sys.exit --
    библиотека кончает процесс сама, и тот, кто её импортировал, не может ни
    перехватить отказ, ни сказать его своими словами (#150: `-m mop.agent`
    печатал текст bus вместо своего).
    SOLUTION: bus бросает ImportError с тем же текстом; диспетчер импортирует
    командлет внутри cli.run, и run делает из ImportError одну строку в
    stderr и код 1.
    STATUS: FIXED — see #169"""
    missing_library(c, "nats", "mop.common.bus", "bus library required: pip install --user "
                           "--break-system-packages nats-py")


def check_nomad_import_187(c):
    """HYPOTHESIS (#187): mop/server/nomad.py при импорте без python-nomad зовёт
    sys.exit -- тот же дефект, что у bus в #169: библиотека кончает процесс
    сама.
    SOLUTION: как в #169 -- ImportError с тем же текстом, одну строку из него
    делает cli.run.
    STATUS: FIXED — see #187"""
    # Команда мастер-шелла python-nomad больше не импортирует (#81, слои
    # #258: nomad -- сервер), поэтому пробуем командой контроллера.
    missing_library(c, "nomad", "mop.server.nomad", "API library required: pip install --user "
                           "--break-system-packages python-nomad",
                           command="mop.cli.driver.build", argv=["--nope"])


def missing_library(c, lib, module, text, command="mop.cli.core.recycle", argv=()):
    """Импорт module без библиотеки lib -- ImportError с текстом text, а
    командлет command, которому module нужен, через cli.run -- одна строка в
    stderr и код 1. Модули перезагружаются: проверка идёт последней."""
    import importlib
    import importlib.abc

    class NoLib(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == lib or name.startswith(lib + "."):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return None

    def evict(lib_too):
        # Из sys.modules И из атрибутов пакета: `from mop import bus` берёт
        # атрибут пакета, и без этого вернул бы прежний bus с настоящим nats.
        for name in list(sys.modules):
            if (lib_too and (name == lib or name.startswith(lib + "."))) or \
                    (name.startswith("mop.") and name != "mop.cli"):
                parent, _, child = name.rpartition(".")
                if parent in sys.modules and getattr(sys.modules[parent], child, None) \
                        is sys.modules[name]:
                    delattr(sys.modules[parent], child)
                del sys.modules[name]

    # Предохранитель -- до всего: однажды неполное выселение модулей отправило
    # из этой проверки настоящий restart на живую шину (отбит правами).
    undo = no_network()
    try:
        import socket
        socket.create_connection(("192.0.2.1", 4222), timeout=1)
        c.fail("the network guard did not hold")
        undo()
        return
    except NetworkGuard:
        pass
    keep = dict(sys.modules)
    blocker = NoLib()
    sys.meta_path.insert(0, blocker)
    try:
        evict(True)
        try:
            importlib.import_module(module)
            c.fail(f"importing {module} without {lib} must raise ImportError")
        except ImportError as e:
            c.expect(f"{module} without {lib}: the ImportError text", str(e), text)
        except SystemExit as e:
            c.fail(f"{module} without {lib} ends the process: SystemExit({e.code!r})")
        evict(False)
        # Командлет, которому нужна шина, через диспетчер: одна строка, код 1.
        # Без аргументов: если шина вдруг импортируется, командлет остановится
        # на usage, а не пойдёт в сеть.
        try:
            _, err, code = run_command(cli.command(command), argv, via_cli=True)
        except BaseException as e:  # noqa: BLE001 -- ушедшее мимо cli.run и есть дефект
            err, code = "", f"escaped {type(e).__name__}"
        c.check(f"a commandlet without {lib} through cli.run: code {code!r}, stderr {err!r}",
                not (code != 1 or err != text + "\n"))
    finally:
        undo()
        sys.meta_path.remove(blocker)
        sys.modules.clear()
        sys.modules.update(keep)
        # Пакеты держат подмодули и атрибутом: вернуть и их, иначе следующий
        # `from mop import x` достал бы копию из этой проверки.
        for name, mod in keep.items():
            if "." in name:
                parent, child = name.rsplit(".", 1)
                if parent in keep:
                    setattr(keep[parent], child, mod)


def check_fallback_model_183(c):
    """HYPOTHESIS (#183): puppets.py читал MOP_FALLBACK_MODEL на уровне модуля,
    и всякий, кто импортирует puppets (сервис кластера), считался читающим
    эту настройку, хотя применяет её один treat() -- `mop doctor --fix` на
    машине оператора.
    SOLUTION: treat() читает её при вызове; на уровне модуля чтения нет.
    STATUS: FIXED — see #183"""
    import ast
    from mop.common import puppets
    tree = ast.parse(open(os.path.join(ROOT, "mop", "common", "puppets.py")).read())
    top = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                        ast.ClassDef))]
    c.check("puppets.py reads MOP_FALLBACK_MODEL at import, not in treat()",
            not (any("MOP_FALLBACK_MODEL" in ast.unparse(n) for n in top)))
    typed = []
    # force -- лечение проходит ворота владения (#40).
    with offline(), \
            patched(puppets, switch_model=lambda node, name, model, force=False: typed.append(model)), \
            patched_env(MOP_FALLBACK_MODEL="sonnet-for-183"):
        got = puppets.treat({"action": "model", "name": "pu-mop-1",
                             "alloc": {"NodeName": "n1"}})
    c.check(f"treat(model) must use MOP_FALLBACK_MODEL as it is at the call: "
            f"{got!r}, typed {typed}",
            not (got != "/model sonnet-for-183" or typed != ["sonnet-for-183"]))
    # ── #251: отвергнутый вход не оставляет мусора aiohttp ──────────────
    # HYPOTHESIS: bus.check / ask_once / connect зовут nats.connect(), который
    # при отказе (allow_reconnect=False) бросает до закрытия транспорта;
    # websocket-транспорт nats-py 2.15 держит aiohttp.ClientSession, и при
    # выходе процесса сборщик печатает «Unclosed client session» -- после
    # запроса пароля mop join успех неотличим от ошибки.
    # SOLUTION: одна точка соединения bus._open: клиент nats.NATS(), connect
    # в try, при отказе nc.close() (закрывает транспорт) и исключение дальше.
    # STATUS: FIXED — see #251
    from mop.common import bus as _bus
    closed = []

    class FakeNATS:
        def __init__(self):
            self.is_closed = False

        async def connect(self, **kw):
            raise OSError("refused by the check")

        async def close(self):
            closed.append(True)

    def refuse(*a, **k):
        raise OSError("nats.connect must not be used: the client must be closable")
    with patched(sys.modules.get("nats"), NATS=FakeNATS, connect=refuse):
        conn = {"url": "wss://192.0.2.1:443/nats", "user": "u", "password": "p"}
        for what, call in (("check", lambda: _bus.check(conn)),
                           ("ask_once", lambda: _bus.ask_once(conn, "s", "ping", timeout=1))):
            closed.clear()
            try:
                call()
                c.fail(f"#251 {what}: a refused connect must raise")
            except Exception as e:
                c.check(f"#251 {what}: must raise a RuntimeError, got {type(e).__name__}: {e}",
                        not (not isinstance(e, RuntimeError)))
            c.expect(f"#251 {what}: the client must be closed once on refusal", closed, [True])
    # ── #252: сообщения наружу без номеров тикетов ──────────────────────
    # HYPOTHESIS: строки, которые видит человек в терминале или модель через
    # MCP, несли «(#82)», «before #211», «(#61)»: довод, которому место в
    # комментарии рядом с кодом, а не в сообщении. SOLUTION: разбор AST всего
    # mop/ -- строковые константы в аргументах print / sys.exit / lib.fail /
    # lib.usage / конструкторов исключений не содержат `#<число>`.
    # STATUS: FIXED — see #252
    import ast as _ast2
    import re as _re
    ticket = _re.compile(r"#\d{2,}")
    SPEAK = {"print", "exit", "fail", "usage", "ok", "section"}

    def _consts(node):
        if isinstance(node, _ast2.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, _ast2.JoinedStr):
            for v in node.values:
                yield from _consts(v)
        elif isinstance(node, _ast2.BinOp):
            yield from _consts(node.left)
            yield from _consts(node.right)

    def _speaks(call):
        f = call.func
        name = f.id if isinstance(f, _ast2.Name) else f.attr if isinstance(f, _ast2.Attribute) else ""
        return name in SPEAK or name.endswith(("Error", "Exception", "Refused", "Missing"))

    root = os.path.dirname(cli.PACKAGE)
    spoken = []
    for d, _, files in os.walk(root):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(d, f)
            with open(path) as fh:
                tree = _ast2.parse(fh.read())
            for node in _ast2.walk(tree):
                if isinstance(node, _ast2.Call) and _speaks(node):
                    for a in node.args:
                        for text in _consts(a):
                            if ticket.search(text):
                                spoken.append(f"{os.path.relpath(path, root)}:{node.lineno}: {text.strip()[:60]!r}")
    c.check("#252 messages must not cite tickets", not (spoken), "\n  " + "\n  ".join(spoken))
    # #255: докстринг командлета -- usage, строка help и описание инструмента
    # MCP, то есть тоже сообщение наружу. `epic #12` в примере -- формат
    # номера, а не ссылка.
    # STATUS: FIXED — see #255
    told = []
    for d, _, files in os.walk(cli.PACKAGE):
        for f in files:
            if f.endswith(".py") and (not f.startswith("_") or f == "__init__.py"):
                path = os.path.join(d, f)
                for i, line in enumerate(cli.docstring(path).splitlines(), 1):
                    if ticket.search(_re.sub(r"epic #\d+", "", line)):
                        told.append(f"{os.path.relpath(path, cli.PACKAGE)}:{i}: {line.strip()[:60]!r}")
    c.check("#255 usage and help must not cite tickets", not (told), "\n  " + "\n  ".join(told))

    # ── #254: bug и ci -- в пространстве dev, без MCP ────────────────────
    # HYPOTHESIS: трекер и CI проекта -- команды разработчика, а лежат среди
    # команд пула и торчат в MCP инструментами ci_*. SOLUTION: mop/cli/dev/
    # {bug,ci}, группа dev в PRIVATE, прежние имена -- через LEGACY один релиз.
    # Теперь отдельные листья объявлены инструментами (#443), но группа
    # по-прежнему закрыта по умолчанию.
    # STATUS: FIXED — see #254
    tree = cli.verbs()
    # bug и ci -- в dev; сам dev растёт (#271 -- test), поэтому «содержит».
    c.check(f"#254 dev must hold bug and ci: {sorted(tree.get('dev') or {})}",
            not (not {"bug", "ci"} <= set(tree.get("dev") or {})))
    top = cli.catalog(cli.scan())
    c.check("#254 bug and ci must not stay top-level commands", not ("bug" in top or "ci" in top))
    c.check(f"#254 LEGACY must map bug and ci into dev: {cli.LEGACY}",
            not (cli.LEGACY.get("bug") != ("dev", "bug") or cli.LEGACY.get("ci") != ("dev", "ci")))
    c.check("#254 no legacy top-level ci tool may reach MCP",
            not (any(n.startswith("ci_") for n, _, _ in
                     cli.tool_commands(cli.scan(), tree))))
    got = cli.resolve(cli.unalias(["bug", "list"]), top, tree)
    c.expect("#254 `mop dev bug list` must still resolve through LEGACY", got,
             ("mop.cli.dev.bug.list", []))

    check_evidence_444(c)
    check_evidence_tool_444(c)
    check_server_namespace_259(c)
    check_named_263(c)
    check_body_file_270(c)
    check_tail_stderr_333(c)
    check_free_floor_329(c)
    check_add_failure_377(c)
    check_tail_parse_431(c)
    check_concise_help_431(c)
    check_add_argv_399(c)
    check_master_argv_400(c)
    check_dev_help_433(c)


# HYPOTHESIS: после смены интерфейса на main(argv) master всё ещё передавал
# старую переменную args парсеру, падая до определения проекта.
# SOLUTION: передавать argv напрямую в claude_args.
# RESULT: лишний origin отвергается без запуска claude или обращения к шине.
# STATUS: FIXED — see #400
def check_master_argv_400(c):
    from mop.cli.core import master
    try:
        out, err, code = run_command(master.main, ["git@h:g/a.git", "git@h:g/b.git"])
    except NameError as e:
        c.fail("#400 mop master must parse argv before starting a session", str(e))
        return
    c.check("#400 mop master refuses two origins with usage",
            code != 0 and "mop master" in str(code), (out, err, code))


# HYPOTHESIS: после переноса командлета на main(argv) тело mop add осталось
# читать прежнюю переменную args; даже --help падает до разбора аргументов.
# SOLUTION: читать argv из параметра main и не вызывать сеть для лишних аргументов.
# RESULT: два аргумента дают usage, а не NameError.
# STATUS: FIXED — see #399
def check_add_argv_399(c):
    from mop.cli.core import add
    from mop.common import config
    with patched(config, require=lambda *names: None):
        try:
            out, err, code = run_command(add.main, ["first", "second"])
        except NameError as e:
            c.fail("#399 mop add must parse argv before network", str(e))
            return
    c.check("#399 mop add refuses extra arguments with usage",
            code != 0 and "mop add" in str(code), (out, err, code))


# ── #377: путь отказа `mop add` -- текст, а не трасса ────────────────────
# HYPOTHESIS: с Alloc (#377) строка отказа add зовёт puppets.failing с уже
# собранным значением, а failing ждёт сырой словарь аллокации: папет,
# упавший на старте, ронял `mop add` AttributeError'ом вместо «<имя> on
# <узел>: failed to start». Путь отказа add проверками не покрыт.
# SOLUTION: failing получает сырой словарь, как в условии.
# STATUS: FIXED — see #377
def check_add_failure_377(c):
    from mop.cli.core import add
    from mop.common import bus, context, puppets
    task = {"state": "dead", "failed": True, "restarts": 3, "exit": 1, "next_s": None}
    cases = [
        ("the allocation failed", {"ID": "a-9", "NodeName": "n1", "ClientStatus": "failed"},
         "pu-mop-9 on n1: failed to start"),
        ("the task summary says failing", {"ID": "a-9", "NodeName": "n1",
                                           "ClientStatus": "pending", "task": task,
                                           "reason": "clone failed: no route"},
         "pu-mop-9 on n1: FAILED: clone failed: no route")]

    class P:
        def step(self, text):
            pass

        def clear(self):
            pass
    for what, alloc, want in cases:
        said = []

        def call_cluster(verb, alloc=alloc, **kw):
            if verb == "ping":
                return {"soul_protocol": {"version": 1, "request_id": True}}
            if verb == "add":
                return {"name": "pu-mop-9", "ok": True, "soul": "puppet",
                        "soul_digest": "a" * 64,
                        "soul_inc": "%s" % uuid.uuid5(uuid.NAMESPACE_DNS, "377")}
            return {"alloc": alloc}
        with patched(bus, call_cluster=call_cluster), \
                patched(_common, workspace_text=lambda o: ("", None),
                        sent_line=lambda s: ""), \
                patched(add.roles, protocol=lambda reply: True), \
                patched(lib, fail=said.append), \
                patched(_common.ops.time, sleep=lambda s: None), \
                patched(context, current=lambda: type("Ctx", (), {"branch": None})()):
            try:
                code = add._add("git@h:g/mop.git", "mop", False, P(), "puppet")
            except Exception as e:
                c.fail(f"#377 mop add, {what}: a refusal, not a trace",
                       f"{type(e).__name__}: {e}")
                continue
        c.expect(f"#377 mop add, {what}: exit 1", code, 1)
        c.check(f"#377 mop add, {what}: says why: {said}",
                len(said) == 1 and said[0].startswith(want), said)


def check_server_namespace_259(c):
    """HYPOTHESIS (#259): команды контроллера -- deploy, config, setup
    контроллера, user, cluster, bootstrap, web, callout, pve-facts -- лежат
    среди команд пула и оператора, и что из них делается только на сервере,
    по имени не видно.
    SOLUTION: пространство mop server; `mop setup` наверху -- только машина
    оператора. Прежние имена -- через LEGACY один релиз: юниты и роли
    зовут их до своего тикета. `mop driver pve-facts` -- двухсловный ключ
    LEGACY: driver как целое не псевдоним, run/list/sweep/build в нём.
    STATUS: FIXED — see #259"""
    want = {"deploy", "config", "setup", "user", "cluster", "bootstrap", "web",
            "callout", "pve-facts", "doctor", "gc", "disk", "sweep",
            "diagnose",   # #536: офлайн-сводка локальной установки
            "proxykey", "grant", "controller", "credentials", "install"}
    tree = cli.verbs()
    got = set(tree.get("server") or {})
    c.expect("#259 server must hold exactly its verbs", sorted(got), sorted(want))
    top = cli.catalog(cli.scan())
    # `diagnose` -- исключение двух половин #536: сводка ВЫБРАННОГО сервера
    # у оператора (mop diagnose, core) и сводка ЛОКАЛЬНОЙ установки
    # (mop server diagnose) -- одно слово, две машины, как `setup`.
    stay = sorted((want - {"setup", "diagnose"}) & set(top))
    c.check(f"#259 must not stay top-level: {stay}", not (stay))
    c.check("#259 mop setup stays top-level, for the operator's machine", not ("setup" not in top))
    c.check("#536 mop diagnose stays top-level, for the operator's machine",
            "diagnose" in top)
    # clone-work (#347) -- глагол узла для сторожа, тоже в driver.
    # #719: private OS fixture -- node diagnostic, не pool/model operation.
    c.expect("#646/#719 driver keeps operations and bounded non-model diagnostics",
             set(tree.get("driver") or {}),
             {"run", "list", "sweep", "build", "clone-work", "purge-clone", "exec", "scope-probe"})
    for old in ("deploy", "config", "user", "cluster", "bootstrap", "web", "callout"):
        c.expect(f"#259 LEGACY must map {old} into server", cli.LEGACY.get(old), ("server", old))
    for argv, want_argv in [(["driver", "pve-facts", "--base", "1"],
                             ["server", "pve-facts", "--base", "1"]),
                            (["driver", "run", "x"], ["driver", "run", "x"]),
                            (["cluster", "users"], ["server", "cluster", "users"])]:
        c.expect(f"#259 unalias({argv})", cli.unalias(argv), want_argv)
    for argv, mod in [(["deploy"], ("mop.cli.server.deploy", [])),
                      (["cluster", "users", "--reload"], ("mop.cli.server.cluster.users", ["--reload"])),
                      (["driver", "pve-facts", "--base", "9"], ("mop.cli.server.pve-facts", ["--base", "9"])),
                      (["server", "user", "add", "x"], ("mop.cli.server.user.add", ["x"]))]:
        got = cli.resolve(cli.unalias(argv), top, tree)
        c.expect(f"#259 {argv} must resolve to {mod}", got, mod)
    # ── #271: mop dev test -- прогон tests/ одной командой ──────────────
    # HYPOTHESIS: проверки гоняли шелл-циклом `for t in tests/*.py`, у
    # каждого свой (CI, папеты, мастер), с разным разбором итога. SOLUTION:
    # `mop dev test [имя ...] [--strict]`; план и итог -- чистые функции.
    # STATUS: FIXED — see #271
    try:
        from mop.cli.dev import test as dev_test
    except ImportError:
        dev_test = None
    if c.check("#271 mop.cli.dev.test with plan() and summary() is missing",
               not (dev_test is None or not hasattr(dev_test, "plan") or not hasattr(dev_test, "summary"))):
        listing = ["cli.py", "agent.py", "_lib.py", "README", "hermetic.py", "spec_snapshot.json"]
        for names, want in (([], ["agent.py", "cli.py", "hermetic.py"]),
                            (["cli"], ["cli.py"]),
                            (["tests/agent.py", "cli.py", "agent"], ["agent.py", "cli.py"])):
            got = dev_test.plan(names, listing)
            c.expect(f"#271 plan({names})", got, want)
        for bad in (["nope"], ["_lib"], ["spec_snapshot.json"]):
            try:
                dev_test.plan(bad, listing)
                c.fail(f"#271 plan({bad}) must refuse")
            except ValueError as e:
                c.check(f"#271 plan({bad}) refusal must name the file: {e}",
                        not ("tests/" not in str(e)))
        lines, ok = dev_test.summary([("agent.py", 0, "agent: ok\n", 1.0),
                                      ("cli.py", 1, "FAIL x\ncli: FAILED\n", 2.0)])
        c.check(f"#271 summary of a red run: {ok} {lines}",
                not (ok or lines[0] != "FAILED tests/cli.py (exit 1)" or "FAIL x" not in lines
                     or lines[-1] != "1/2 ok, failed: cli.py"))
        lines, ok = dev_test.summary([("agent.py", 0, "agent: ok\n", 1.0)])
        c.check(f"#271 summary of a green run: {ok} {lines}", not (not ok or lines != ["1/1 ok"]))
        c.check("#271 test must be a verb of the private dev namespace",
                not ("dev" not in cli.PRIVATE or "test" not in (cli.verbs().get("dev") or {})))


def check_named_263(c):
    """HYPOTHESIS (#263): delete, recycle, restart, wipe и update каждый
    повторяют разбор --force, проверку одного имени с usage, перила guard и
    печать owner_note; --dry -- в gc и двух sweep; фабрика argparse -- в
    bug и ci. Пять копий разойдутся на первой же правке одной.
    SOLUTION: lib.named / lib.parse_named, lib.note, lib.dry, одна фабрика
    parser в dev/_common. Вывод, usage и коды выхода -- те же: их держит
    характеризация ниже, снятая с кода до переделки.
    STATUS: FIXED — see #263"""
    import importlib
    from mop.cli import lib
    from mop.common import bus, puppets
    from mop.cli.core import _common as core_common

    # ── помощник: разбор имени и --force ──────────────────────────────────
    doc = "do a thing: mop thing <name> [--force]"
    fn = getattr(lib, "named", None)
    if not c.check("#263 lib.named is missing", fn is not None):
        return
    for argv, want in [(["pu-mop-1"], ("pu-mop-1", False)),
                       (["--force", "pu-mop-1"], ("pu-mop-1", True)),
                       (["pu-mop-1", "--force"], ("pu-mop-1", True))]:
        got = fn(argv, doc)
        c.expect(f"#263 named({argv})", got, want)
    for argv in ([], ["a", "b"], ["--force"]):
        try:
            fn(argv, doc)
            c.fail(f"#263 named({argv}) must refuse through usage")
        except SystemExit as e:
            c.expect(f"#263 named({argv}) must exit with the usage", str(e.code), doc)
    c.expect("#263 parse_named with most=2",
             lib.parse_named(["x", "git@h:o.git", "--force"], doc, most=2), (["x", "git@h:o.git"], True))
    for argv, want in (([], False), (["--dry"], True)):
        c.check(f"#263 dry({argv})", not (lib.dry(argv, doc) is not want))
    try:
        lib.dry(["--wet"], doc)
        c.fail("#263 dry(['--wet']) must refuse through usage")
    except SystemExit:
        pass
    from mop.cli.dev.bug import _common as bug_c
    from mop.cli.dev.ci import _common as ci_c
    c.check("#263 the argparse factory keeps prog and no built-in help",
            not (bug_c.parser("new").prog != "mop dev bug new" or ci_c.parser("log").prog != "mop dev ci log"
                 or bug_c.parser("new").add_help))

    # ── характеризация пяти команд: вывод тот же, что до переделки ────────
    calls = []
    note = {"owner_note": "was olga's: taken with --force"}

    def call_cluster(verb, **kw):
        calls.append((verb, kw.get("force")))
        if verb == "alloc":
            return {"alloc": {"NodeName": "hyper"}}
        if verb == "spec":
            return {"meta": {"origin": "git@h:g/mop.git"}}
        return dict(note)
    def run(module, argv):
        mod = importlib.import_module(f"mop.cli.core.{module}")
        return (*run_command(mod.main, argv, via_cli=True), mod.__doc__.strip())
    with patched_env(MOP_SERVER_LAN="192.0.2.1"), \
            patched(lib, in_project=lambda: None), \
            patched(bus, call_cluster=call_cluster, login=lambda: "anton"), \
            patched(puppets,
                    delete=lambda name, force=False: calls.append(("delete", force)) or
                    {**note, "body": "destroyed", "node": "hyper"},
                    recycle=lambda name, workspace_of=None, force=False, branch=None:
                    calls.append(("recycle", force)) or dict(note),
                    wipe=lambda node, name, force=False:
                    calls.append(("wipe", force)) or {**note, "target": "/t"},
                    running_alloc=lambda name: {"NodeName": "hyper"}), \
            patched(core_common, workspace_text=lambda o: ("", NOFILE_334)):
        line = "pu-mop-1: was olga's: taken with --force\n"
        want = {
            "recycle": line,
            "update": line + core_common.sent_line(NOFILE_334) + "\n",
        }
        for module, out_want in want.items():
            calls.clear()
            out, err, code, usage = run(module, ["pu-mop-1", "--force"])
            forced = [f for v, f in calls if v in ("delete", "recycle", "wipe", "restart", "update")]
            c.check(f"#263 mop {module} pu-mop-1 --force: {(out, err, code)!r}, force {forced}",
                    not ((out, err, code) != (out_want, "", 0) or forced != [True]))
            for bad in ([], ["a", "b", "c"]) if module == "update" else ([], ["a", "b"]):
                out, err, code, usage = run(module, bad)
                # usage -- SystemExit(докстринг): в процессе он -- код выхода,
                # печатает его интерпретатор на выходе (stderr, код 1).
                c.check(f"#263 mop {module} {bad}: must exit with its usage",
                        not ((out, err, code) != ("", "", usage)), repr((out, err, str(code)[:60])))
        # #642: human takeover не переехал в новые restart/wipe/stop как обычный флаг.
        for module in ('restart', 'wipe', 'stop', 'delete'):
            calls.clear()
            out, err, code, usage = run(module, ['pu-mop-1', '--force'])
            c.check(f'#642 {module} refuses takeover before any old RPC',
                    out == '' and code == 2 and 'unsupported' in err and calls == [], (out, err, code, calls))
            for bad in ([], ['a', 'b']):
                out, err, code, usage = run(module, bad)
                c.expect(f'#642 {module} invalid args are a safe typed boundary refusal',
                         (out, err, code), ('', 'invalid command arguments\n', 1))


def check_body_file_270(c):
    """HYPOTHESIS (#270): `mop dev bug new --body-file -` и любой
    несуществующий путь падают трассировкой FileNotFoundError в read_body:
    `-` читался как имя файла, а отсутствие файла не ловил никто.
    SOLUTION: `-` в --body-file -- stdin, как у текста комментария; файла нет
    или он не читается -- отказ одной строкой с путём.
    STATUS: FIXED — see #270"""
    import io
    import tempfile
    from mop.cli.dev.bug import _common as bug_common
    try:
        with patched(sys, stdin=io.StringIO("тело из stdin\n")):
            got = bug_common.read_body(None, "-")
        c.expect("#270 --body-file - must read stdin", got, "тело из stdin")
    except BaseException as e:  # noqa: BLE001 -- трассировка и есть дефект
        c.fail(f"#270 --body-file - must read stdin, raised {type(e).__name__}: {e}")
    missing = os.path.join(tempfile.mkdtemp(prefix="mop-test-270-"), "no-such.md")
    for path in (missing, tempfile.mkdtemp(prefix="mop-test-270-")):
        try:
            bug_common.read_body(None, path)
            c.fail(f"#270 --body-file {path} must be refused")
        except SystemExit as e:
            text = str(e.code)
            c.check(f"#270 the refusal must be one line naming the path: {text!r}",
                    not (path not in text or "\n" in text.strip() or "Traceback" in text))
        except BaseException as e:  # noqa: BLE001
            c.fail(f"#270 --body-file {path}: a traceback ({type(e).__name__}), "
                   f"not a one-line refusal")
    # Как было: файл -- его текст, аргумент -- он сам.
    ok = os.path.join(tempfile.mkdtemp(prefix="mop-test-270-"), "body.md")
    with open(ok, "w", encoding="utf-8") as f:
        f.write("  тело  \n")
    c.check("#270 a readable file and a text argument must work as before",
            not (bug_common.read_body(None, ok) != "тело" or bug_common.read_body(" x ", None) != "x"))


def check_free_floor_329(c):
    """HYPOTHESIS (#329): свободная память узла писалась по трём правилам:
    `mop node` делил нацело (1536 МБ -- «1 GB»), а строка пула
    (lib.pool_lines: `mop list`, проверка `mop server deploy`) и страница
    округляли (то же -- «2 GB»). Папет резервирует 8 ГБ, и оператор, решая,
    влезет ли ещё один, читал про один узел два разных числа.
    SOLUTION: вниз везде -- свободного не бывает больше, чем есть; всего --
    по тому же правилу.
    STATUS: FIXED — see #329"""
    from mop.cli import node
    from mop.common import bus
    nodes = [{"name": "hyper", "driver": "pve", "serves": "mop", "state": "ready",
              "free_mb": 1536, "total_mb": 64511, "slots": 0, "slots_total": 7}]
    pool = [{"name": "hyper", "status": "ready", "free_mb": 1536, "total_mb": 64511,
             "slots": 0, "slots_total": 7}]

    def no_connect(*a, **k):
        raise AssertionError("a check reached the live bus")
    with patched(bus, connect=no_connect), restored(bus, "ask_cluster"), \
            patched_env(MOP_SERVER_LAN="192.0.2.1"):
        bus.ask_cluster = lambda verb, **kw: {"nodes": nodes if verb == "nodes" else pool}
        c.expect("#329 mop node floors free and total (the reference rule)",
                 run_command(node.main, (), via_cli=True),
                 ("NODE   DRIVER  SERVES  STATE  FREE  TOTAL  SLOTS\n"
                  "hyper  pve     mop     ready  1 GB  62 GB  0/7\n", "", 0))
        c.expect("#329 the pool line floors like mop node", lib.pool_lines(),
                 ["  hyper: free 1/62 GB, slots 0/7"])


def check_tail_stderr_333(c):
    """HYPOTHESIS (#333): `mop tail` папета, у которого нет tmux (bootstrap
    падает до сессии), не показывает ничего полезного: running_alloc
    отказывает «not running», пейна нет, а причина живёт в stderr аллокации.
    SOLUTION: нет сессии (папет не running или пейн не читается) -- хвост
    stderr аллокации глаголом `stderr` сервиса кластера, под строкой, что это
    именно он (lib.stderr_text). Инструмент MCP `tail` -- тот же текст.
    STATUS: FIXED — see #333"""
    from mop.cli.core import tail
    from mop.cli.service import mcp
    from mop.common import puppets
    text = getattr(lib, "stderr_text", None)
    if text is None or not hasattr(puppets, "alloc_stderr"):
        c.fail("#333 no lib.stderr_text / puppets.alloc_stderr: no fallback to stderr")
        return
    why = "pu-mop-1 is not running: FAILED: bootstrap task «a : b» failed: no file\nmore"
    c.expect("#333 stderr text: a header naming the source, then the lines",
             text("pu-mop-1", why, ["l1", "l2"]),
             ["pu-mop-1: no tmux session (pu-mop-1 is not running: FAILED: bootstrap task "
              "«a : b» failed: no file); the allocation's stderr, last 2 lines:", "l1", "l2"])
    c.expect("#333 stderr text: nothing there is said",
             text("pu-mop-1", "why", []),
             ["pu-mop-1: no tmux session (why); the allocation's stderr is empty"])
    asked = []

    def stderr(name, lines):
        asked.append((name, lines))
        return ["l1", "l2"]

    def not_running(name):
        raise LookupError(why)

    def no_pane(node, name):
        raise RuntimeError("tmux in pu-mop-1: no server running")
    want = "\n".join(text("pu-mop-1", why, ["l1", "l2"])) + "\n"
    with patched(puppets, running_alloc=not_running, alloc_stderr=stderr):
        out, err, code = run_command(tail.main, ["pu-mop-1", "-n", "2"])
    c.expect("#333 mop tail, not running: the allocation's stderr", (out, code), (want, 0))
    c.expect("#333 mop tail asks for as many lines as -n", asked, [("pu-mop-1", 2)])
    with patched(puppets, running_alloc=lambda name: {"NodeName": "hyper"},
                 pane_lines=no_pane, alloc_stderr=stderr):
        out, err, code = run_command(tail.main, ["pu-mop-1", "-n", "2"])
    c.check("#333 mop tail, running without a pane: the allocation's stderr",
            code == 0 and out.startswith("pu-mop-1: no tmux session (tmux in pu-mop-1: "
                                         "no server running)") and out.endswith("l1\nl2\n"),
            (out, err, code))
    with patched(puppets, running_alloc=lambda name: {"NodeName": "hyper"},
                 pane_lines=lambda node, name: ["a", "b", "c"], alloc_stderr=stderr):
        asked.clear()
        out, err, code = run_command(tail.main, ["pu-mop-1", "-n", "2"])
    c.expect("#333 mop tail with a pane: as before, stderr not asked",
             (out, asked), ("b\nc\n", []))
    with patched(puppets, running_alloc=not_running, alloc_stderr=stderr), \
            patched(mcp, MASTER=True):
        got = mcp.tail("pu-mop-1", lines=2)
    c.expect("#333 MCP tail: the same text", got, want.rstrip("\n"))


# ── #433: справка и отказы mop/cli/dev короче, контракт на месте ───────
# HYPOTHESIS: докстринги dev-групп разрослись повторами («the same, N of
# them», пересказ аргументов в прозе) и туманными фразами («against the
# project», «as they would in a pipeline», «must be rebuilt»): читающий их
# модель вынуждена разбирать синтаксис из прозы, а человек -- листать.
# SOLUTION: у каждой команды -- аудит фразы, синтаксис вызова, действенные
# предупреждения и словарь статусов остаются, повторы и пересказ уходят.
# Проверка чистая: перечень глаголов из cli.verbs(), докстринги -- без
# импорта; границы -- «запрещённая фраза отсутствует» и «контрактное слово
# на месте», не числовые лимиты.
# STATUS: FIXED — see #433
GONE_433 = {
    # пересказ списка в прозе вместо одной строки синтаксиса
    "dev/ci/__init__.py": ("the same, N of them",),
    # «против проекта» без слова о том, что проверяется и где
    "dev/ci/lint.py": ("against the project", "as they would in a pipeline"),
    # многословие «должен быть перестроен» вместо действия
    "dev/web/build.py": ("must be rebuilt",),
}
KEPT_433 = {
    # словарь статусов -- контракт трекера, по группе на issue
    "dev/bug/relabel.py": ("live", "wip", "parked", "fixed", "noise", "dup"),
    # шёпот о молчаливом поведении GitLab -- это и есть предупреждение
    "dev/bug/labels.py": ("silently replaces",),
    # `-` и stdin -- путь тела, его потеря меняет поведение
    "dev/bug/comment.py": ("-",),
    "dev/bug/new.py": ("--body-file", "stdin"),
    "dev/bug/edit.py": ("--body-file",),
    # отказы с именованием состояния -- действенная диагностика
    "dev/ci/cancel.py": ("Refused",),
    "dev/ci/retry.py": ("Refused",),
    # разрушительное и молчаливое: предупреждение обязано остаться
    "dev/web/build.py": ("--check", "byte for byte"),
    # группы перечисляют свои глаголы: «нет аргументов» -- не сообщение,
    # а команда с аргументом обязана назвать их разбор
    "dev/test.py": ("--strict",),
}


def check_dev_help_433(c):
    # Каталог dev-подпространства из обхода пакета: группы, подгруппы,
    # глаголы -- всё, что справочник и usage показывают пользователю.
    import glob
    base = os.path.join(cli.PACKAGE, "dev")
    files = sorted(glob.glob(os.path.join(base, "**", "*.py"), recursive=True))
    docs = {os.path.relpath(f, cli.PACKAGE).replace(os.sep, "/"): cli.docstring(f)
            for f in files if "__pycache__" not in f}
    # #444 добавил ограниченный reader трекера после проверки #433: новый
    # глагол обязателен в каталоге, а не повод ослабить его полноту.
    # STATUS: FIXED — see #444
    # #484: проверки и локальный просмотр веба тоже идут через диспетчер.
    # #613: отдельный интерактивный probe добавляет ещё один обязательный глагол.
    # #615: локальный integration smoke -- самостоятельный обязательный глагол.
    # #696: регенерация публичного OpenAPI артефакта -- обязательный глагол.
    if not c.check("#433/#632 dev subtree: 4 groups, 28 verbs including controller fixture",
                   len([d for d in docs if d.endswith("__init__.py")]) == 4
                   and len([d for d in docs
                            if not d.endswith("__init__.py") and not d.endswith("_common.py")]) == 28
                   and docs.get("dev/controller.py", "").startswith("mop dev controller")
                   and docs.get("dev/chat-smoke.py", "").startswith("mop dev chat-smoke")
                   and docs.get("dev/chat-probe.py", "").startswith("mop dev chat-probe")
                   and docs.get("dev/bug/evidence.py", "").startswith("mop dev bug evidence")
                   and docs.get("dev/web/test.py", "").startswith("mop dev web test")
                   and docs.get("dev/web/preview.py", "").startswith("mop dev web preview"),
                   sorted(docs)):
        return
    for path, phrases in GONE_433.items():
        for phrase in phrases:
            c.check(f"#433 {path} no longer says «{phrase}»",
                    phrase not in docs.get(path, phrase), docs.get(path, "")[:120])
    for path, words in KEPT_433.items():
        for word in words:
            c.check(f"#433 {path} keeps «{word}»", word in docs.get(path, ""),
                    docs.get(path, "")[:120])
    # Ревью 897f80a: relabel не закрывает -- relabel_action даёт только
    # "reopen" либо "relabel", закрытие -- глагол close. Справка, обещающая
    # «closes», отправляет читателя мимо команды, которая это умеет;
    # назвать close как правильную команду -- можно и нужно.
    doc = docs["dev/bug/relabel.py"]
    lower = doc.lower()
    c.check("#433 relabel help may promise reopening, not closing",
            "closes it" not in lower and "or close" not in lower
            and "open or close" not in lower, doc)
    c.check("#433 relabel help names the statuses that reopen",
            all(w in lower for w in ("live", "wip", "parked", "reopen"))
            and "`mop dev bug close`" in doc, doc)
    c.check("#433 the bug index does not promise closing by relabel",
            "open or close" not in docs["dev/bug/__init__.py"].lower(),
            docs["dev/bug/__init__.py"][:200])
    # Синтаксис вызова остаётся первой строкой: «mop dev …» с глаголом.
    for path, doc in docs.items():
        if path.endswith("_common.py"):
            continue
        first = doc.splitlines()[0] if doc else ""
        c.check(f"#433 {path} first line names the command",
                first.startswith("mop ") or (path.endswith("__init__.py")
                                             and first and not first.startswith(" ")),
                first[:80])


if __name__ == "__main__":
    sys.exit(main())
