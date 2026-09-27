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
    c.expect("#334 result after an ok play: fields, marker and commit of what was sent",
             bootstrap.read_result(root, name),
             {"ok": True, "played": True, "seconds": 41.6, "rc": 0, "task": None,
              "message": None, "last": "PLAY RECAP ok=3", "at": 1_790_500_060,
              "sent": again, "commit": "5b92b440c952fad2"})
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


def check_lease_push_312(c):
    """HYPOTHESIS (#312): кредит в тело нового держателя не приезжал на
    подъёме -- только раздачей при смене кредита; тело жило на копии узла.
    SOLUTION: после прогона и до ответа bootstrap зовёт cred_push сервиса
    кластера (узел и аренда -- там, из Nomad). Отказ или молчание -- строка
    журнала и lease_note в ответе, подъём идёт дальше (на копии узла, как
    прежде). Упал прогон -- не зовёт: папет не поднимется. STATUS: FIXED — see #312"""
    from mop.common import bus
    asked = []

    def ask(reply):
        def ask_cluster(verb, timeout=None, project=None, **fields):
            asked.append((verb, project, fields.get("name"), timeout))
            if isinstance(reply, Exception):
                raise reply
            return reply
        return ask_cluster
    req = {"verb": "bootstrap", "name": "pu-mop-1", "address": ""}
    played = {"ok": True, "played": False, "text": "no bootstrap for mop"}
    with patched(bootstrap, refusal=lambda req, project: None, play=lambda req, project: dict(played),
                 puppet_creds=lambda project: {"url": "nats://x"}):
        for reply, note in (
                ({"ok": True, "lease": "anton", "node": "hyper", "result": "OK"}, None),
                ({"ok": True, "lease": None}, None),
                ({"ok": True, "lease": "anton", "node": "hyper",
                  "result": "NOT REACHED: node agent hyper did not answer in 60s"},
                 "lease anton not pushed: NOT REACHED: node agent hyper did not answer in 60s"),
                ({"error": "no such verb cred_push"},
                 "lease not pushed: no such verb cred_push"),
                (bus.BusError("cluster service did not answer in 45s"),
                 "lease not pushed: cluster service did not answer in 45s")):
            asked.clear()
            with patched(bus, ask_cluster=ask(reply)):
                got = bootstrap.answer("mop", dict(req))
            c.expect(f"#312 bootstrap after {reply!r}: ok, and the note",
                     (got.get("ok"), got.get("lease_note")), (True, note))
            c.expect("#312 bootstrap asks cred_push as the operator, by name, 45 s",
                     asked, [("cred_push", bus.ADMIN, "pu-mop-1", 45)])
            if note:
                c.check("#312 the journal names the lease note",
                        any(note in l for l in bootstrap.journal("mop", req, got)),
                        bootstrap.journal("mop", req, got))
    # Бюджет (#312): узел ждёт ответ bootstrap не дольше BOOTSTRAP_TIMEOUT
    # (300 с), прогон может занять до 290 с. Раздача аренды -- в остаток
    # минус запас на ответ; остатка нет -- не зовём, говорим.
    budget = getattr(bootstrap, "lease_budget", None)
    if budget is None:
        c.fail("#312 no bootstrap.lease_budget: the lease push can overrun the node's wait")
        return
    c.expect("#312 lease budget: 45 s when the play was quick", budget(3), 45)
    c.expect("#312 lease budget: the rest minus the reply's margin",
             budget(260), bus_timeout(bootstrap) - bootstrap.REPLY_MARGIN - 260)
    c.expect("#312 lease budget: none left near the limit",
             budget(bus_timeout(bootstrap) - bootstrap.REPLY_MARGIN - 0.5), None)
    for elapsed, want_asked, want_timeout, want_note in (
            (260, True, bus_timeout(bootstrap) - bootstrap.REPLY_MARGIN - 260, None),
            (296, False, None, "no time left for the lease push")):
        clock = iter([1000.0, 1000.0 + elapsed])
        asked.clear()
        with patched(bootstrap, refusal=lambda req, project: None,
                     play=lambda req, project: dict(played),
                     puppet_creds=lambda project: {"url": "nats://x"},
                     _clock=lambda: next(clock)), \
                patched(bus, ask_cluster=ask({"ok": True, "lease": "anton", "result": "OK"})):
            got = bootstrap.answer("mop", dict(req))
        c.expect(f"#312 after a {elapsed} s play: asked, timeout, note",
                 (bool(asked), asked[0][3] if asked else None, got.get("lease_note")),
                 (want_asked, want_timeout, want_note))
    # Отказ host-узла (#312): второй кредит профиля на общем $HOME -- подъём
    # отказывает с этим текстом, а не идёт на чужом аккаунте.
    why = "host node box already runs credential anton of profile claude for pu-mop-2"
    with patched(bootstrap, refusal=lambda req, project: None, play=lambda req, project: dict(played),
                 puppet_creds=lambda project: {"url": "nats://x"}), \
            patched(bus, ask_cluster=ask({"ok": True, "lease": "ermak", "node": "box",
                                          "refused": why})):
        got = bootstrap.answer("mop", dict(req))
    c.expect("#312 a host refusal from cred_push fails the start with its text",
             (got.get("ok"), got.get("error")), (None, why))
    failed = {"ok": False, "played": True, "rc": 2, "tail": "t", "task": None, "message": None}
    with patched(bootstrap, refusal=lambda req, project: None, play=lambda req, project: dict(failed),
                 puppet_creds=lambda project: {"url": "nats://x"}), \
            patched(bus, ask_cluster=ask({"ok": True, "lease": None})):
        asked.clear()
        bootstrap.answer("mop", dict(req))
        c.expect("#312 a failed play: no lease push", asked, [])


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


def main():
    c = Checks()
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
    check_lease_push_312(c)
    return c.report("bootstrap")


if __name__ == "__main__":
    sys.exit(main())
