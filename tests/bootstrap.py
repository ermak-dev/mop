#!/usr/bin/env python3
"""Проверка bootstrap'а песочницы без пула: python3 tests/bootstrap.py

Bootstrap (#62): .mop/bootstrap.yaml проекта играет СЕРВЕР при каждом старте
песочницы, узел зовёт его с шины и ждёт. Чистое здесь — хранение файла на
сервере, аргументы прогона и проверка, что узел просит про папета своего
проекта. Сам прогон, ключ в тело и ожидание врапера — только на живом пуле.
"""
import json
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import bootstrap  # noqa: E402

TEXT = """
- name: bootstrap of proj
  vars:
    ENV_FILE: .env
    MOP_MEM_MB: 4096
  tasks:
    - name: env file
      ansible.builtin.copy: {src: ~/proj/.env, dest: "{{ mop_clone }}/.env"}
"""


def check_identity_167(c):
    """HYPOTHESIS (#167): коммит папета подписан тем, кого назвал мастер
    разовым `git -c`, а не человеком, которого проверила шина. Агент узла
    знает логин владельца из субъекта (#207), но профиля (имя, почта) у него
    нет: провайдер личностей (#205) живёт на сервере.
    SOLUTION: глагол `identity` у сервиса сервера (server.rpc -- в него узлу
    писать уже можно, в cluster.rpc нельзя и не надо): логин -> имя и почта
    из провайдера сервера, и ничего больше -- ни роли, ни проектов, ни хеша.
    Профиль без имени или без почты -- не профиль: половина identity коммит
    всё равно не спасает. Имя и почта из тела запроса не читаются.

    Сервис работает под пользователем пула, а файл операторов лежит в
    secrets/ контроллера, куда ему хода нет (#206): провайдер тот же, что у
    callout (identity.server_provider), и читает копию deploy'я в
    natsconf.IDENTITY_DIR, а не MOP_OPERATORS_FILE.
    STATUS: FIXED — see #167"""
    from mop.server import identity, natsconf
    tmp = tempfile.mkdtemp(prefix="mop-test-identity-")
    path = os.path.join(tmp, "operators")
    # Файл контроллера, который сервису не виден: его olga -- другая.
    elsewhere = os.path.join(tempfile.mkdtemp(prefix="mop-test-identity-"), "operators")
    with open(elsewhere, "w") as f:
        f.write(identity.format_line(identity.Identity("olga", "user", ("mop",), "Not Olga",
                                                       "no@example.dev"),
                                     identity.hash_password("pw")) + "\n")
    rows = [identity.Identity("olga", "user", ("mop",), "Ольга Петрова", "olga@example.dev"),
            identity.Identity("ivan", "user", ("mop",), "Иван", ""),
            identity.Identity("anna.k", "admin", (), "", "anna@example.dev")]
    with open(path, "w") as f:
        f.write("".join(identity.format_line(w, identity.hash_password("pw")) + "\n"
                        for w in rows))
    with patched_env(MOP_OPERATORS_FILE=elsewhere, MOP_OPERATORS="boss:admin"), \
            patched(natsconf, IDENTITY_DIR=tmp):
        for req, want in (
                ({"login": "olga"}, {"login": "olga", "name": "Ольга Петрова",
                                     "email": "olga@example.dev"}),
                # Имя и почта из тела -- не источник: только провайдер.
                ({"login": "olga", "name": "Mallory", "email": "m@evil"},
                 {"login": "olga", "name": "Ольга Петрова", "email": "olga@example.dev"}),
                ({"login": "ivan"}, {"login": "ivan"}),
                ({"login": "anna.k"}, {"login": "anna.k"}),
                ({"login": "boss"}, {"login": "boss"}),
                ({"login": "nobody"}, {"login": "nobody"})):
            got = bootstrap.answer("mop", {"verb": "identity", **req})
            c.expect(f"identity {req}", got, want)
        for bad in (None, "", "a\nb", 7):
            got = bootstrap.answer("mop", {"verb": "identity", "login": bad})
            c.check(f"identity of {bad!r} must be refused", not (not got.get("error")), got)
        c.check("the unknown-verb refusal must list identity",
                not ("identity" not in bootstrap.answer("mop", {"verb": "nope"}).get("error", "")))


# ── #334: итог прогона -- рядом с отправленным ──────────────────────────
# HYPOTHESIS: play() отдаёт {ok, played, rc, seconds, tail} узлу и ничего не
# хранит, journal() только пишет в журнал: чем кончился прогон после
# `mop update`, клиенту узнать неоткуда. Что уехало, сервер тоже не помнит.
# SOLUTION: сервис кластера кладёт происхождение рядом с workspace
# (<папет>-sent.json, с меткой регистрации по часам сервера), bootstrap
# после каждого прогона -- итог (<папет>-result.json) с меткой и коммитом
# отправленного; store("") снимает оба. STATUS: FIXED — see #334
def check_result_334(c):
    root = tempfile.mkdtemp(prefix="mop-test-bootstrap-334-")
    name = "pu-proj-1"
    for fn in ("note_sent", "note_result", "read_result", "read_sent"):
        if not c.check(f"#334 bootstrap.{fn} exists", hasattr(bootstrap, fn)):
            return
    prov = {"source": "working copy", "commit": "5b92b440c952fad2", "dirty": True,
            "tasks": 1, "present": True}
    bootstrap.store(root, name, TEXT)
    marker = bootstrap.note_sent(root, name, prov, now=1_790_500_000.5)
    c.check("#334 note_sent: the marker is the server's, not empty", marker)
    sent = bootstrap.read_sent(root, name)
    c.expect("#334 sent record: provenance plus the marker",
             sent, {**prov, "marker": marker, "at": 1_790_500_000})
    path = os.path.join(root, f"{name}-sent.json")
    c.expect("#334 sent file is 0600", oct(os.stat(path).st_mode & 0o777), oct(0o600))
    again = bootstrap.note_sent(root, name, prov, now=1_790_500_000.5)
    c.check("#334 two registrations in the same second get different markers",
            again != marker, (marker, again))

    ok = {"ok": True, "played": True, "rc": 0, "seconds": 41.6, "tail": "a\nPLAY RECAP ok=3\n"}
    bootstrap.note_result(root, name, ok, now=1_790_500_060)
    # С #345 в записи ещё key/failures/gave_up (их проверяет check_give_up_345).
    got = bootstrap.read_result(root, name)
    c.expect("#334 result after an ok play: fields, marker and commit of what was sent",
             {k: v for k, v in got.items() if k not in ("key", "failures", "gave_up")},
             {"ok": True, "played": True, "seconds": 41.6, "rc": 0, "task": None,
              "message": None, "last": "PLAY RECAP ok=3", "at": 1_790_500_060,
              "sent": again, "commit": "5b92b440c952fad2"})
    c.expect("#345 an ok play: no failures, not given up",
             (got.get("failures"), got.get("gave_up")), (0, False))
    failed = {"ok": False, "played": True, "rc": 2, "seconds": 3.0, "tail": "x\nfatal",
              "task": "env file", "message": "Could not find ~/proj/.env"}
    bootstrap.note_result(root, name, failed, now=1_790_500_070)
    got = bootstrap.read_result(root, name)
    c.expect("#334 result after a failed play carries #333's task and message",
             (got["ok"], got["rc"], got["task"], got["message"], got["last"]),
             (False, 2, "env file", "Could not find ~/proj/.env", "fatal"))
    bootstrap.note_result(root, name, {"ok": True, "played": False, "text": "no bootstrap"},
                          now=1_790_500_080)
    got = bootstrap.read_result(root, name)
    c.expect("#334 result without a play: played False, nothing else invented",
             (got["ok"], got["played"], got["rc"], got["seconds"], got["last"]),
             (True, False, None, None, None))

    # Прогон по запросу узла пишет итог сам -- и когда кред не нашёлся:
    # прогон уже был.
    with patched(bootstrap, ROOT=root, play=lambda req, project: dict(failed),
                 puppet_creds=lambda project: None):
        bootstrap.answer("proj", {"verb": "bootstrap", "name": name})
    c.expect("#334 answer(bootstrap) writes the result of its play",
             (bootstrap.read_result(root, name) or {}).get("task"), "env file")
    with patched(bootstrap, ROOT=root, play=lambda req, project: dict(ok),
                 puppet_creds=lambda project: None):
        bootstrap.answer("other", {"verb": "bootstrap", "name": name})
    c.expect("#334 a refused request (foreign project) plays nothing and writes nothing",
             (bootstrap.read_result(root, name) or {}).get("task"), "env file")

    # Нет отправленного (старый клиент) -- итог без метки и коммита.
    bootstrap.store(root, name, "")
    c.expect("#334 store('') removes the sent and result files",
             sorted(f for f in os.listdir(root) if f.startswith(name)), [])
    bootstrap.note_result(root, name, ok, now=1_790_500_090)
    got = bootstrap.read_result(root, name)
    c.expect("#334 result with nothing sent: no marker, no commit",
             (got["sent"], got["commit"]), (None, None))
    c.expect("#334 read_result of an unknown puppet is None",
             bootstrap.read_result(root, "pu-proj-9"), None)


# ── #333: упавшая задача bootstrap -- по имени, с сообщением ────────────
# HYPOTHESIS: ответ bootstrap -- {ok, played, rc, seconds, tail} с 25
# строками хвоста; какая задача упала, не говорит никто, и FAILED в
# `mop list` называет лишь первую строку [ERROR] без задачи.
# SOLUTION: чистая failed_task(вывод) -> (задача, сообщение): последний
# заголовок `TASK [...]` перед первой строкой `fatal:`/`failed:`, сообщение
# -- msg из `FAILED! => {...}`, иначе сама строка ошибки, до 200 знаков.
# Ответ (outcome) несёт task и message рядом с tail -- поля стабильны, их
# читает #334. STATUS: FIXED — see #333
PLAYED = """
PLAY [bootstrap of pu-rugent-3] ************************************************

TASK [Gathering Facts] *********************************************************
ok: [10.77.38.103]

TASK [bootstrap : project secrets] *********************************************
changed: [10.77.38.103] => (item=.env)

TASK [bootstrap : env file] ****************************************************
fatal: [10.77.38.103]: FAILED! => {"changed": false, "msg": "Could not find or access '~/rugent/.env-prod' on the Ansible Controller.\\nIf you are using a module and expect the file to exist on the remote, see the remote_src option"}

PLAY RECAP *********************************************************************
10.77.38.103               : ok=2    changed=1    unreachable=0    failed=1    skipped=0    rescued=0    ignored=0
"""


def check_failed_task_333(c):
    fn = getattr(bootstrap, "failed_task", None)
    if fn is None:
        c.fail("#333 no bootstrap.failed_task: the failed task is not named")
        return
    c.expect("#333 fatal: the task and the msg",
             fn(PLAYED), ("bootstrap : env file",
                          ("Could not find or access '~/rugent/.env-prod' on the Ansible "
                           "Controller. If you are using a module and expect the file to "
                           "exist on the remote, see the remote_src option")[:200]))
    c.check("#333 the message is one line: it goes into a line of stderr",
            "\n" not in (fn(PLAYED)[1] or "\n"))
    loop = ("TASK [bootstrap : packages] ***\n"
            "ok: [h] => (item=git)\n"
            'failed: [h] (item=nope) => {"ansible_loop_var": "item", "item": "nope", '
            '"msg": "No package matching \'nope\' is available"}\n'
            "PLAY RECAP ***\nh : ok=1 failed=1\n")
    c.expect("#333 a loop item failed: the task and its msg", fn(loop),
             ("bootstrap : packages", "No package matching 'nope' is available"))
    garbled = "TASK [x : y] ***\nfatal: [h]: FAILED! => {not json at all\n"
    c.expect("#333 an unparsable result: the error line as the message", fn(garbled),
             ("x : y", "fatal: [h]: FAILED! => {not json at all"))
    long = "TASK [x : y] ***\nfatal: [h]: FAILED! => " + json.dumps({"msg": "z" * 500}) + "\n"
    c.expect("#333 the message is cut", len(fn(long)[1]), 200)
    c.expect("#333 a clean run: nothing failed",
             fn("TASK [a : b] ***\nok: [h]\nPLAY RECAP ***\nh : ok=1 failed=0\n"), (None, None))
    c.expect("#333 an error before any task: no task named",
             fn("[ERROR]: couldn't resolve module/action 'nope'\n"), (None, None))
    c.expect("#333 the recap's failed=1 is not a failure line",
             fn("TASK [a : b] ***\nok: [h]\nPLAY RECAP ***\nh : ok=1 failed=1\n"),
             (None, None))
    out = getattr(bootstrap, "outcome", None)
    if out is None:
        c.fail("#333 no bootstrap.outcome: the reply is built inline")
        return
    got = out(2, PLAYED, 3.21)
    c.expect("#333 the failed reply: task and message next to tail",
             {k: got.get(k) for k in ("ok", "played", "rc", "seconds", "task")},
             {"ok": False, "played": True, "rc": 2, "seconds": 3.2,
              "task": "bootstrap : env file"})
    c.check("#333 the failed reply keeps its tail of 25 lines",
            got.get("tail") == "\n".join(PLAYED.splitlines()[-25:])
            and got.get("message", "").startswith("Could not find"), got)
    ok = out(0, "TASK [a : b] ***\nok: [h]\n", 1.0)
    c.expect("#333 a good run: task and message are None, the keys are there",
             (ok["ok"], "task" in ok and ok["task"], "message" in ok and ok["message"]),
             (True, None, None))
    # #334: итог, который хранит сервер, несёт task/message из этого ответа.
    root = tempfile.mkdtemp(prefix="mop-test-bootstrap-333-334-")
    rec = bootstrap.note_result(root, "pu-proj-1", got, now=1)
    c.expect("#334 the stored result carries #333's task and message",
             (rec["task"], rec["message"]), (got["task"], got["message"]))
    c.expect("#334 the stored result of a good run: task and message None",
             {k: bootstrap.note_result(root, "pu-proj-1", ok, now=2)[k]
              for k in ("ok", "task", "message")},
             {"ok": True, "task": None, "message": None})


def bus_timeout(module):
    from mop.common import busnames
    return busnames.BOOTSTRAP_TIMEOUT


def check_give_up_345(c):
    root = tempfile.mkdtemp(prefix="mop-test-bootstrap-345-")
    name = "pu-proj-1"
    if not c.check("#345 bootstrap.GIVE_UP_AFTER is 3", getattr(bootstrap, "GIVE_UP_AFTER", None) == 3):
        return
    prov = {"source": "origin", "commit": "abc", "dirty": False, "tasks": 1, "present": True}
    bootstrap.store(root, name, TEXT)
    bootstrap.note_sent(root, name, prov, now=1)
    failed = {"ok": False, "played": True, "rc": 2, "seconds": 3.0, "tail": "x\nfatal",
              "task": "bootstrap : env file", "message": "no group cloud"}
    unreachable = {"ok": False, "played": True, "rc": 3, "seconds": 9.0, "tail": "UNREACHABLE!",
                   "task": None, "message": None}
    timeout = {"ok": False, "played": True, "rc": None, "seconds": 290.0, "tail": "did not finish",
               "task": None, "message": None}
    ok = {"ok": True, "played": True, "rc": 0, "seconds": 4.0, "tail": "ok", "task": None,
          "message": None}

    def note(out):
        r = bootstrap.note_result(root, name, out, now=2)
        return (r["failures"], r["gave_up"])
    c.expect("#345 a failed task counts 1", note(failed), (1, False))
    c.expect("#345 unreachable does not count", note(unreachable), (1, False))
    c.expect("#345 a timeout does not count", note(timeout), (1, False))
    c.expect("#345 second failed task", note(failed), (2, False))
    c.expect("#345 rc 2 without a named task does not count", note({**failed, "task": None}), (2, False))
    c.expect("#345 third failed task: gave up", note(failed), (3, True))
    c.expect("#345 an ok play resets", note(ok), (0, False))
    note(failed), note(failed)
    bootstrap.note_sent(root, name, prov, now=3)          # mop update: новая метка
    c.expect("#345 a new marker (update/recycle) starts over", note(failed), (1, False))
    note(failed)
    bootstrap.store(root, name, TEXT.replace("env file", "env file 2"))   # другой файл
    c.expect("#345 a changed stored workspace starts over", note(failed), (1, False))

    # Через answer: третий провал -- gave_up в ответе и одна отложенная
    # просьба give_up; четвёртый запрос того же ключа -- без прогона.
    bootstrap.note_sent(root, name, prov, now=4)
    plays, later = [], []

    def play(req, project):
        plays.append(req["name"])
        return dict(failed)
    with patched(bootstrap, ROOT=root, play=play, puppet_creds=lambda project: None,
                 _later=lambda delay, fn: later.append((delay, fn))):
        replies = [bootstrap.answer("proj", {"verb": "bootstrap", "name": name}) for _ in range(4)]
    c.expect("#345 three plays, the fourth request plays nothing", len(plays), 3)
    c.expect("#345 replies: gave_up only from the third on",
             [bool(r.get("gave_up")) for r in replies], [False, False, True, True])
    c.check("#345 a gave-up reply is a failure the wrapper refuses on, naming the task",
            all(not r.get("ok") and r.get("task") == "bootstrap : env file"
                and r.get("failures") == 3 for r in replies[2:]), replies[2:])
    c.expect("#345 the stop is asked after the grace, once per gave-up reply",
             [d for d, _ in later], [bootstrap.GIVE_UP_GRACE, bootstrap.GIVE_UP_GRACE])
    asked = []
    with patched(bootstrap.bus, ask_cluster=lambda verb, **kw: asked.append((verb, kw)) or {"ok": True}):
        later[0][1]()
    c.expect("#345 the deferred ask is give_up on the operator's subject for this puppet",
             [(v, kw.get("project"), kw.get("name")) for v, kw in asked],
             [("give_up", bootstrap.bus.ADMIN, name)])
    with patched(bootstrap.bus, ask_cluster=lambda verb, **kw: (_ for _ in ()).throw(
            bootstrap.bus.BusError("no cluster"))):
        try:
            later[0][1]()
            c.check("#345 a failing give_up ask does not raise in the timer thread", True)
        except Exception as e:
            c.fail(f"#345 the deferred ask raised: {e}")


# ── #344: провал команды -- причина из её вывода ──────────────────────
# HYPOTHESIS: failed_task (#333) берёт msg из `FAILED! => {...}`, а у
# command/shell-задачи msg всегда «non-zero return code»: FAILED, строка
# врапера и итог `mop update` (#334) теряют настоящую причину (rumop,
# pu-rudesktop-8: ошибка uv в stderr_lines).
# SOLUTION: у результата с rc и выводом сообщение -- последняя непустая
# строка stderr (иначе stdout) и код выхода; msg -- только когда вывода нет.
# Одна строка, не длиннее 200 знаков, код не срезается. STATUS: FIXED — see #344
UV_FAILED = (
    "TASK [bootstrap : uv sync] *****************************************************\n"
    'fatal: [10.77.35.145]: FAILED! => {"changed": true, "cmd": ["uv", "sync", "--group", '
    '"cloud"], "delta": "0:00:00.021530", "end": "2026-09-28 11:02:14.402317", "msg": '
    '"non-zero return code", "rc": 2, "start": "2026-09-28 11:02:14.380787", "stderr": '
    '"Resolved 198 packages in 2ms\\nerror: Group `cloud` is not defined in the project\'s '
    '`dependency-groups` table", "stderr_lines": ["Resolved 198 packages in 2ms", "error: '
    'Group `cloud` is not defined in the project\'s `dependency-groups` table"], "stdout": '
    '"", "stdout_lines": []}\n'
    "PLAY RECAP *********************************************************************\n"
    "10.77.35.145               : ok=4    changed=1    unreachable=0    failed=1\n")


def _command(result):
    return "TASK [bootstrap : run] ***\nfatal: [h]: FAILED! => " + json.dumps(result) + "\n"


def check_command_message_344(c):
    fn = bootstrap.failed_task
    c.expect("#344 a command with stderr: its last line and the exit code",
             fn(UV_FAILED), ("bootstrap : uv sync",
                             "error: Group `cloud` is not defined in the project's "
                             "`dependency-groups` table (rc 2)"))
    c.expect("#344 a command with only stdout: stdout's last line",
             fn(_command({"msg": "non-zero return code", "rc": 1, "stderr_lines": [],
                          "stdout_lines": ["building", "FAIL: tests broke", ""]})),
             ("bootstrap : run", "FAIL: tests broke (rc 1)"))
    c.expect("#344 a command with no output: msg, as before",
             fn(_command({"msg": "non-zero return code", "rc": 3, "stderr_lines": [],
                          "stdout_lines": []})),
             ("bootstrap : run", "non-zero return code"))
    c.expect("#344 a module without rc: msg, as before",
             fn(_command({"changed": False, "msg": "Could not find '.env-prod'"})),
             ("bootstrap : run", "Could not find '.env-prod'"))
    got = fn(_command({"msg": "non-zero return code", "rc": 7, "stderr_lines": ["x" * 400]}))[1]
    c.check("#344 a long line is cut, the exit code kept, one line, 200 at most",
            len(got) <= 200 and got.endswith(" (rc 7)") and "\n" not in got, got)
    c.expect("#344 a result that is not an object: the line, as before",
             fn("TASK [a : b] ***\nfatal: [h]: FAILED! => [1, 2]\n"),
             ("a : b", "fatal: [h]: FAILED! => [1, 2]"))


# Кред проекта с метками: ни одна не должна попасть в журнал (#349).
SENTINEL_BUS = {"url": "nats://sentinel-host-349:4222", "user": "sentinel-user-349",
                "password": "sentinel-password-349"}
UV_TASK = "Python environment of the site (uv sync)"
UV_MESSAGE = "error: Group `cloud` is not defined (rc 2)"


def check_journal_no_secrets_349(c):
    """HYPOTHESIS (#349): journal() на проваленном прогоне пишет в строку весь
    ответ (`out.get('error') or ('ok' if ok else out)`: у упавшего прогона
    error нет), а with_creds к нему уже приклеил `bus` -- пароль шины
    проекта уходил в journald сервера на каждом упавшем старте.
    SOLUTION: строка собирается только из известных полей; словарь ответа
    не форматируется никогда.
    STATUS: FIXED — see #349"""
    req = {"verb": "bootstrap", "name": "pu-proj-1"}
    bus_ = {"bus": SENTINEL_BUS}
    outcomes = {
        "ok": {"ok": True, "played": True, "rc": 0, "seconds": 12.3, "tail": "done",
               "task": None, "message": None, **bus_},
        "failed play with a task": {
            "ok": False, "played": True, "rc": 2, "seconds": 40.1, "tail": "TAIL-349",
            "task": UV_TASK, "message": UV_MESSAGE, **bus_},
        "failed play without a task": {
            "ok": False, "played": True, "rc": 4, "seconds": 7.5, "tail": "TAIL-349",
            "task": None, "message": None, **bus_},
        "timed-out play": {
            "ok": False, "played": True, "rc": None, "seconds": 890.0,
            "tail": "bootstrap of pu-proj-1 did not finish in 890s", **bus_},
        "error": {"error": "no bus password for project proj on the server", **bus_},
        "gave up": {"ok": False, "played": False, "gave_up": True, "rc": 2, "task": UV_TASK,
                    "message": UV_MESSAGE, "failures": 3, "seconds": None,
                    "tail": "not replayed", **bus_},
        "not played": {"ok": True, "played": False, "text": "no bootstrap for proj", **bus_},
    }
    lines = {k: "\n".join(bootstrap.journal("proj", req, out)) for k, out in outcomes.items()}
    for k, text in lines.items():
        leaked = [v for v in SENTINEL_BUS.values() if v in text]
        c.check(f"#349 the journal of a {k} reply carries no bus credentials",
                not leaked and "'bus'" not in text, text)
    failed = lines["failed play with a task"]
    c.check("#349 a failed play's journal names its task, message, rc and seconds",
            all(s in failed for s in (UV_TASK, UV_MESSAGE, "rc 2", "40.1s", "TAIL-349")),
            failed)
    c.check("#349 the exit code is named once, not again after #344's message",
            failed.count("(rc 2)") == 1, failed)
    bare = lines["failed play without a task"]
    c.check("#349 a failed play without a task still reads as failed, with rc and seconds",
            all(s in bare for s in ("failed", "rc 4", "7.5s")), bare)
    c.check("#349 a timed-out play keeps its tail",
            "did not finish" in lines["timed-out play"], lines["timed-out play"])
    c.check("#349 an error reply names the error",
            "no bus password for project proj" in lines["error"], lines["error"])
    c.check("#349 a gave-up reply names the task and the failure count",
            all(s in lines["gave up"] for s in (UV_TASK, "gave up", "3")), lines["gave up"])
    c.check("#349 an ok reply reads ok with its seconds",
            all(s in lines["ok"] for s in ("ok", "12.3s")), lines["ok"])


def main():
    c = Checks()
    check_journal_no_secrets_349(c)
    check_give_up_345(c)
    check_result_334(c)
    root = tempfile.mkdtemp(prefix="mop-test-bootstrap-")

    # HYPOTHESIS: механизма нет вовсе — ничто не играет манифест при старте.
    # SOLUTION: файл проекта хранится на сервере заранее (deploy/add кладут), а
    # играется по запросу узла. STATUS: FIXED — see #62
    try:
        got = bootstrap.store(root, "proj", TEXT)
    except AttributeError:
        c.fail("bootstrap.store is missing")
        return c.report("bootstrap")
    tasks, of_vars = bootstrap.files_of(root, "proj")
    c.check("store", not (not (tasks and os.path.exists(tasks) and of_vars
                               and os.path.exists(of_vars))), f"{tasks!r} {of_vars!r}")
    c.check("store must count the tasks", not (got.get("tasks") != 1), got)
    # Просьба о размерах в bootstrap не на месте — названа, не проглочена.
    c.check("store must name a size ask as alien", not (got.get("alien") != ["MOP_MEM_MB"]), got)
    # Пустой текст — снять файлы: проект убрал bootstrap, сервер не должен
    # играть вчерашний.
    bootstrap.store(root, "proj", "")
    c.check("an empty bootstrap must remove the project's files",
            not (bootstrap.files_of(root, "proj") != (None, None)))
    # Кривая форма — громко, с именем проекта.
    try:
        bootstrap.store(root, "bad", "vars: {}\n")
        c.fail("malformed: no error")
    except ValueError as e:
        c.check("malformed message must name the project", not ("bad" not in str(e)), e)

    # Узел просит за папета; проект — из субъекта (права NATS), имя — из тела
    # запроса. Расхождение — отказ: узел, представившийся своим субъектом,
    # не может попросить сыграть чужой bootstrap в своё тело.
    c.check("refusal: a puppet of the project must pass",
            not (bootstrap.refusal({"name": "pu-proj-1"}, "proj") is not None))
    c.check("refusal: a puppet of another project must be refused",
            not (bootstrap.refusal({"name": "pu-other-1"}, "proj") is None))
    c.check("refusal: a name that is not a puppet name must be refused",
            not (bootstrap.refusal({"name": "pu-proj-1; id"}, "proj") is None))

    # Аргументы прогона: одна машина по адресу, пользователь пула, ключ
    # сервера, без вопроса о ключе хоста (тело пересоздаётся и меняет его),
    # переменные прогона — проект, папет, клон, файлы.
    argv = bootstrap.argv("/x/deploy/bootstrap.yml", "10.77.38.100", "pool",
                          "/home/pool/.ssh/mop-bootstrap", {"MOP_HOME": "/home/pool"},
                          "proj", "pu-proj-1", "/home/pool/puppets/pu-proj-1",
                          "/s/proj-tasks.yml", "/s/proj-vars.yml")
    joined = " ".join(argv)
    for want in ("ansible-playbook", "10.77.38.100,", "\"ansible_user\": \"pool\"",
                 "mop-bootstrap", "StrictHostKeyChecking=no", "mop_puppet",
                 "pu-proj-1", "/s/proj-tasks.yml", "/s/proj-vars.yml",
                 "/x/deploy/bootstrap.yml"):
        c.check(f"argv lacks {want!r}", not (want not in joined), argv)

    # Значение с пробелами едет только JSON'ом: голое `-e k=v w=x` ansible
    # режет по пробелам на несколько пар, и до ssh доезжало одно `-o`
    # (поймано первым живым прогоном: «no argument after keyword -o»).
    for i, a in enumerate(argv):
        if a == "-e" and " " in argv[i + 1] and not argv[i + 1].lstrip().startswith("{"):
            c.fail("a bare -e value with spaces is split by ansible", repr(argv[i + 1]))

    # HYPOTHESIS (#114): ответ bootstrap без кредов — не отказ, потому что
    # узел держал `bus-<проект>.json` от прогона; снимаем файл — и папет
    # поднимается без шины, читаясь мастером как живой, но молчащий.
    # SOLUTION: сервер отказывает в bootstrap громко, с именем проекта.
    # STATUS: FIXED — see #114
    creds = {"url": "nats://s:4222", "user": "puppet-proj", "password": "p"}
    try:
        got = bootstrap.with_creds({"ok": True}, creds, "proj")
        c.check("with_creds must carry the credentials",
                not (got.get("bus") != creds or got.get("error")), got)
        got = bootstrap.with_creds({"ok": True}, None, "proj")
        c.check("with_creds: no password for the project must be a refusal naming it",
                not (not got.get("error") or "proj" not in got["error"]), got)
        # Отказ прогона главнее: кред к упавшему bootstrap не приклеиваем.
        got = bootstrap.with_creds({"error": "play failed"}, creds, "proj")
        c.check("with_creds must keep the play's error",
                not (got.get("bus") or got.get("error") != "play failed"), got)
        # HYPOTHESIS (#349): упавший прогон несёт ok False, а не error, и
        # with_creds клеил к нему кред вопреки своему docstring'у -- а
        # journal() печатал ответ целиком. SOLUTION: кред -- только ok True;
        # упавший старт шину не открывает (узел выходит на not ok раньше,
        # чем читает bus). STATUS: FIXED — see #349
        failed = {"ok": False, "played": True, "rc": 2, "tail": "fatal"}
        got = bootstrap.with_creds(failed, creds, "proj")
        c.expect("#349 with_creds: a failed play gets no credentials", got, failed)
        got = bootstrap.with_creds(failed, None, "proj")
        c.expect("#349 with_creds: a failed play without a project password stays the "
                 "play's failure", got, failed)
    except AttributeError:
        c.fail("bootstrap.with_creds is missing")

    # HYPOTHESIS (#128): bootstrap играется, только если у проекта есть
    # задачи, и секретов проекта не знает. SOLUTION: рамка играется, если
    # есть задачи или секреты; секреты едут путём к каталогу на сервере, а
    # не значениями -- argv виден в ps. STATUS: FIXED — see #128
    try:
        c.check("needs_play: play when there are tasks or secrets, only then",
                not (bootstrap.needs_play(None, None) or not bootstrap.needs_play(None, "/s/sec")
                     or not bootstrap.needs_play("/s/t.yml", None)))
        a = bootstrap.argv("/x/deploy/bootstrap.yml", "10.0.0.2", "pool", "/k",
                           {}, "proj", "pu-proj-1", "/c", None, None, secrets="/s/sec")
        extra = json.loads(a[a.index("-e", a.index("-e") + 1) + 1])
        c.check("argv must carry the secrets dir",
                not (extra.get("mop_secrets_dir") != "/s/sec"), extra)
        c.check("argv without project tasks must not name any",
                not ("mop_bootstrap_tasks" in extra))
    except (AttributeError, TypeError) as e:
        c.fail("secrets in bootstrap", e)

    # HYPOTHESIS (#201): прогон ходит на host-узел портом 22, а у узла на
    # WSL sshd слушает 2222 -- на 22 отвечает другой sshd, и вход отвергнут
    # (Permission denied (publickey)). Порт знал только ssh config root'а на
    # контроллере. SOLUTION: узел отдаёт адрес с портом (`адрес:порт`, если
    # не 22), прогон кладёт порт в ansible_port своего инвентаря; голый адрес
    # -- сегодняшний прогон без ansible_port. Порт -- число, пришедшее от
    # узла: не число -- отказ, а не строка в -e. STATUS: FIXED — see #201
    try:
        def inventory_and_extra(address):
            a = bootstrap.argv("/x/deploy/bootstrap.yml", address, "pool", "/k",
                               {}, "proj", "pu-proj-1", "/c", None, None)
            return a[a.index("-i") + 1], json.loads(a[a.index("-e", a.index("-e") + 1) + 1])
        inv, extra = inventory_and_extra("192.168.1.37:2222")
        c.check("argv for address:port must play the address on ansible_port",
                not (inv != "192.168.1.37," or extra.get("ansible_port") != 2222),
                f"-i {inv!r}, ansible_port={extra.get('ansible_port')!r}")
        inv, extra = inventory_and_extra("10.77.38.100")
        c.check("argv for a bare address must stay today's",
                not (inv != "10.77.38.100," or "ansible_port" in extra), f"-i {inv!r}, {extra}")
        for bad in ("192.168.1.37:22x", "192.168.1.37:0", "192.168.1.37:70000", ":2222"):
            try:
                inv, extra = inventory_and_extra(bad)
            except ValueError:
                continue
            c.fail(f"argv must refuse the address {bad!r}",
                   f"played -i {inv!r} ansible_port={extra.get('ansible_port')!r}")
    except (AttributeError, TypeError) as e:
        c.fail("ssh port in bootstrap", e)

    # HYPOTHESIS (#133): workspace хранился на проект и писался четырьмя
    # дорогами; удаление из рабочей копии не доходило, и старт играл
    # проектную копию из origin. SOLUTION: workspace -- файл папета, старт
    # играет копию своего папета. STATUS: FIXED — see #133
    from mop.server import cluster
    from mop.common import project_secrets
    root = tempfile.mkdtemp(prefix="mop-test-ws-")
    with patched(bootstrap, ROOT=root), \
            patched(project_secrets, ROOT=os.path.join(root, "no-secrets")):
        try:
            bootstrap.store(root, "proj", TEXT)               # старая проектная копия
            got = bootstrap.play({"name": "pu-proj-1", "address": ""}, "proj")
            c.check("play must ignore a project-level copy",
                    not (got.get("played") is not False), got)
            # add/update несут workspace: текст -- положить папету, пусто -- снять,
            # нет поля -- не трогать (restart без рабочей копии).
            cluster.store_workspace(root, "pu-proj-1", {"workspace": TEXT})
            c.check("store_workspace must lay the puppet's own copy",
                    not (bootstrap.files_of(root, "pu-proj-1")[0] is None))
            cluster.store_workspace(root, "pu-proj-1", {})
            c.check("a request without workspace must leave the copy alone",
                    not (bootstrap.files_of(root, "pu-proj-1")[0] is None))
            cluster.store_workspace(root, "pu-proj-1", {"workspace": ""})
            c.check("an empty workspace must remove the puppet's copy",
                    not (bootstrap.files_of(root, "pu-proj-1") != (None, None)))
        except AttributeError as e:
            c.fail("workspace per puppet", e)

    check_identity_167(c)
    check_failed_task_333(c)
    check_command_message_344(c)
    return c.report("bootstrap")


if __name__ == "__main__":
    sys.exit(main())
