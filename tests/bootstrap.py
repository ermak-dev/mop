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


def role_bundle():
    """Минимальный живой бандл роли: (манифест-словарь, промпт, digest).
    Душа обязательна у каждого джоба (#662), и проверкам ответа
    bootstrap'а нужен прошедший ворота Meta-тройку и реестр."""
    from mop.common import soul
    manifest = {"soul": "puppet", "version": 1,
                "runtime": {"driver": "any", "receives_dispatch": False},
                "capabilities": {"node_verbs": [], "cluster_verbs": [],
                                 "dispatch": False, "spawn": []},
                "prompt": "soul.md"}
    prompt = b"# prompt\n"
    return manifest, prompt, soul.digest(manifest, prompt)


def role_meta_job():
    """JobMeta ролевого джоба проекта proj (git@example:team/proj.git):
    источник -- тот же манифест, что и бандл роли."""
    from mop.common.domain import JobMeta
    manifest, _prompt, digest = role_bundle()
    return JobMeta("git@example:team/proj.git", None, None,
                   manifest["soul"], digest,
                   "00000000-0000-0000-0000-000000000345")


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

    # Прогон по запросу узла пишет итог сам: ролевой джоб, ворота роли
    # проходит (свой узел, приватный ответ).
    from _lib import patched as _patched
    from mop.common import busnames
    from mop.server import grants
    grant_root = tempfile.mkdtemp(prefix="mop-test-bootstrap-334g-")
    manifest, prompt, digest = role_bundle()
    meta = role_meta_job()
    rec334 = grants.prepare("proj", manifest["soul"], digest, manifest, prompt,
                            "anton", "req-334", name, meta.soul_inc, "fixture",
                            root=grant_root)
    grants.activate(rec334["principal"], root=grant_root)

    def play(req, project, **kw):
        return dict(failed)
    ask334 = {"verb": "bootstrap", "name": name,
              "_caller": "node-wate", "_reply": "_MOP_REPLY.node-wate.a"}
    with patched(bootstrap, ROOT=root, play=play,
                 _soul_root=lambda: grant_root,
                 _assigned_node=lambda n: "wate",
                 _owner_profile=lambda login: {"login": login, "name": "A",
                                               "email": "a@example.dev"},
                 _job_meta=lambda n: meta):
        bootstrap.answer("proj", dict(ask334))
    c.expect("#334 answer(bootstrap) writes the result of its play",
             (bootstrap.read_result(root, name) or {}).get("task"), "env file")
    with patched(bootstrap, ROOT=root, play=play,
                 _soul_root=lambda: grant_root,
                 _assigned_node=lambda n: "wate",
                 _owner_profile=lambda login: {"login": login, "name": "A",
                                               "email": "a@example.dev"},
                 _job_meta=lambda n: meta):
        bootstrap.answer("other", dict(ask334))
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

    def play(req, project, **kwargs):
        plays.append(req["name"])
        return dict(failed)
    from mop.common.domain import JobMeta
    from mop.server import grants
    grant_root = tempfile.mkdtemp(prefix="mop-test-bootstrap-581-")
    manifest, prompt, digest = role_bundle()
    inc = "00000000-0000-0000-0000-000000000345"
    rec = grants.prepare("proj", manifest["soul"], digest, manifest, prompt,
                         "anton", "req-345", name, inc, "fixture",
                         root=grant_root)
    grants.activate(rec["principal"], root=grant_root)
    meta = JobMeta("git@example:team/proj.git", None, None,
                   manifest["soul"], digest, inc)
    with patched(bootstrap, ROOT=root, play=play,
                 _later=lambda delay, fn: later.append((delay, fn)),
                 _soul_root=lambda: grant_root,
                 _assigned_node=lambda name: "wate",
                 _job_meta=lambda n: meta):
        asks = [{"verb": "bootstrap", "name": name,
                 "_caller": "node-wate", "_reply": "_MOP_REPLY.node-wate.a"}
                for _ in range(4)]
        replies = [bootstrap.answer("proj", dict(r)) for r in asks]
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


def check_role_secrets_592(c):
    """HYPOTHESIS (#592): bootstrap раздаёт каталог секретов проекта целиком
    всем ролям (bootstrap.play -> mop_secrets_dir = project_dir), и роль
    без права на секрет всё равно его получает: allowlist схемы soul
    (SOUL.md, «секреты») -- объявление без принуждения. SOLUTION: селекция
    на сервере -- по allowlist из доверенного бандла роли собирается
    приватный временный каталог в том же формате files/+vars.env и подаётся
    существующему плейбуку как mop_secrets_dir; пустой allowlist --
    пустой каталог (прогон всё равно играется, вычищая старое в теле);
    allow None -- легаси-джоб без роли, каталог целиком, как сегодня;
    запрошенного секрета нет в хранилище -- отказ до прогона, модель не
    стартует. Статус: STATUS: RED — see #592"""
    fn = getattr(bootstrap, "secrets_of", None)
    if fn is None:
        c.fail("#592 no bootstrap.secrets_of: the role allowlist selects nothing")
        return
    root = tempfile.mkdtemp(prefix="mop-test-secrets-592-")
    from mop.common import project_secrets
    # Хранилище проекта: два файла (один в подкаталоге) и две переменные.
    project_secrets.put_file(root, "proj", "gitlab-token", b"TOKEN")
    project_secrets.put_file(root, "proj", "ci/runner.key", b"RUN")
    project_secrets.set_var(root, "proj", "CI_JOB_TOKEN", "ci-tok")
    project_secrets.set_var(root, "proj", "DEPLOY_KEY", "dep")

    # (1) Точный выбор: в staging попадает ровно allowlist, формат тот же.
    try:
        with bootstrap.role_staging(root, "proj", {"files": ["gitlab-token"],
                                                   "vars": ["CI_JOB_TOKEN"]}) as stage:
            with open(os.path.join(stage, "files", "gitlab-token"), "rb") as f:
                got = f.read()
            c.expect("#592 the allowed file is staged", got, b"TOKEN")
            c.check("#592 the sibling file is not staged",
                    not os.path.exists(os.path.join(stage, "files", "ci"))), None
            with open(os.path.join(stage, "vars.env")) as f:
                c.expect("#592 the allowed var is staged, byte for byte",
                         f.read(), "CI_JOB_TOKEN=ci-tok\n")
            c.check("#592 the staging dir is 0700",
                    not (oct(os.stat(stage).st_mode & 0o777) != oct(0o700)),
                    oct(os.stat(stage).st_mode & 0o777))
        c.check("#592 the staging dir is removed after the play",
                not os.path.exists(stage), stage)
    except AttributeError as e:
        c.fail("#592 no role_staging context manager", e)

    # (2) Пустой allowlist -- пустой каталог, не None: прогон обязан
    # сыграть и вычистить прежнее в теле, а не оставить старый staging.
    try:
        with bootstrap.role_staging(root, "proj", {"files": [], "vars": []}) as stage:
            c.check("#592 an empty allowlist stages an empty dir, not None",
                    not (stage is None or os.path.exists(os.path.join(stage, "files"))
                         or os.path.exists(os.path.join(stage, "vars.env"))), stage)
    except AttributeError as e:
        c.fail("#592 role_staging of an empty allowlist", e)

    # (3) Запрошенного нет в хранилище -- отказ до прогона: имя названо,
    #    значение не приезжает, модель не стартует.
    for allow in ({"files": ["no-such-token"], "vars": []},
                  {"files": [], "vars": ["NO_SUCH_VAR"]}):
        try:
            with bootstrap.role_staging(root, "proj", allow):
                c.fail("#592 a missing secret must fail before the play", allow)
        except ValueError as e:
            c.check("#592 the refusal names the missing name, not a value",
                    not ((allow["files"] or allow["vars"])[0] not in str(e)), e)

    # (4) Побег из клона и маски -- отказ селекции ещё до хранилища.
    for bad in ("../escape", "/etc/passwd", "a/../b"):
        try:
            with bootstrap.role_staging(root, "proj", {"files": [bad], "vars": []}):
                c.fail("#592 a path escape must be refused", bad)
        except ValueError:
            pass
    for bad in ("not a var", "1BAD", ""):
        try:
            with bootstrap.role_staging(root, "proj", {"files": [], "vars": [bad]}):
                c.fail("#592 a wild var name must be refused", bad)
        except ValueError:
            pass

    # (5) Разные роли -- разные каталоги: одновременные старты не делят
    #     один staging и не мутируют хранилище-источник.
    before = sorted(os.listdir(project_secrets.project_dir(root, "proj")))
    try:
        with bootstrap.role_staging(root, "proj", {"files": ["gitlab-token"], "vars": []}) as a, \
                bootstrap.role_staging(root, "proj", {"files": ["gitlab-token"], "vars": []}) as b:
            c.check("#592 concurrent roles stage in separate dirs", a != b, (a, b))
    except AttributeError as e:
        c.fail("#592 concurrent role staging", e)
    c.check("#592 the source store is not mutated",
            not (sorted(os.listdir(project_secrets.project_dir(root, "proj"))) != before),
            sorted(os.listdir(project_secrets.project_dir(root, "proj"))))

    # (6) play() с ролью подменяет mop_secrets_dir staged-каталогом:
    #     argv прогона несёт staging, а не каталог хранилища.
    played = {}
    def fake_run(cmd, **kw):
        played["argv"] = cmd
        class R:
            returncode = 0
            stdout = "PLAY RECAP ***\nh : ok=1 failed=0\n"
            stderr = ""
        return R()
    import subprocess as _sp
    from _lib import patched as _patched
    try:
        with _patched(bootstrap, ROOT=os.path.join(root, "no-workspace")), \
                _patched(project_secrets, ROOT=root), \
                _patched(bootstrap, TIMEOUT=120), \
                _patched(_sp, run=fake_run):
            got = bootstrap.play({"name": "pu-proj-1", "address": ""}, "proj",
                                 role={"files": ["gitlab-token"], "vars": []})
        extra = json.loads(played["argv"][played["argv"].index("-e",
                          played["argv"].index("-e") + 1) + 1])
        staged = extra.get("mop_secrets_dir")
        c.check("#592 play must feed the staging dir, not the store",
                not (not staged or staged == project_secrets.project_dir(root, "proj")), extra)
        c.check("#592 the play of a role with an empty allowlist still runs",
                not (not got.get("played") or not got.get("ok")), got)
    except (AttributeError, TypeError) as e:
        c.fail("#592 play with a role allowlist", e)

    # (7) Легаси-джоб без роли -- каталог целиком, как сегодня.
    played.clear()
    try:
        with _patched(bootstrap, ROOT=os.path.join(root, "no-workspace")), \
                _patched(project_secrets, ROOT=root), \
                _patched(bootstrap, TIMEOUT=120), \
                _patched(_sp, run=fake_run):
            got = bootstrap.play({"name": "pu-proj-1", "address": ""}, "proj", role=None)
        extra = json.loads(played["argv"][played["argv"].index("-e",
                          played["argv"].index("-e") + 1) + 1])
        c.expect("#592 a legacy job gets the whole store dir",
                 extra.get("mop_secrets_dir"), project_secrets.project_dir(root, "proj"))
    except (AttributeError, TypeError) as e:
        c.fail("#592 legacy play must stay today's", e)


def check_role_secrets_hard_592(c):
    """HYPOTHESIS (#592, ревью 84a1fae): четыре поведенческих провала,
    воспроизведённых независимо на живом коде. (1) Пустой allowlist без
    хранилища и задач не играет прогон -- прежний staging тела живёт:
    «на сервере нет» не доказывает «в теле чисто». (2) Симлинк: vars.env
    читается по ссылке, каталог проекта -- ссылка наружу -- и листовая
    проверка её не видит: проверять надо каждую ступень от корня
    хранилища. (3) 'a/../b' нормализуется valid_name и проходит при
    существующем b: каноничность -- до поиска. (4) Гонка чтения:
    list_vars проверяет одним чтением, _vars перечитывает другим --
    рассинхрон отдаёт пустой успех: один слепок на прогон. (5) rmtree(
    ignore_errors=True) глотал сбой уборки вопреки контракту ошибок.
    SOLUTION: режим роли играет прогон всегда; имя канонично и без
    масок до хранилища; ни одной ссылки по пути; vars читаются одним
    слепком и материализуются из него; уборка строгая -- сбой виден.
    STATUS: RED — see #592"""
    from mop.common import project_secrets
    import subprocess as _sp
    import shutil as _sh
    from _lib import patched as _patched

    def fake_run(cmd, **kw):
        played = fake_run.played = getattr(fake_run, "played", {})
        played["argv"] = cmd

        class R:
            returncode = 0
            stdout = "PLAY RECAP ***\nh : ok=1 failed=0\n"
            stderr = ""
        return R()

    # (1) Нет хранилища и задач, allowlist пуст: прогон всё равно нужен --
    #     чистить прежнее тело; «no bootstrap» в режиме роли -- ложный.
    bare = tempfile.mkdtemp(prefix="mop-test-bare-592-")
    try:
        with _patched(bootstrap, ROOT=os.path.join(bare, "no-ws")), \
                _patched(project_secrets, ROOT=bare), \
                _patched(bootstrap, TIMEOUT=120), _patched(_sp, run=fake_run):
            got = bootstrap.play({"name": "pu-proj-1", "address": ""}, "proj",
                                 role={"files": [], "vars": []})
        c.check("#592 an empty allowlist with no store must still play to clear the body",
                not (not got.get("played") or not got.get("ok")), got)
        argv = getattr(fake_run, "played", {}).get("argv") or []
        extra = json.loads(argv[argv.index("-e", argv.index("-e") + 1) + 1]) if argv else {}
        c.check("#592 ... and the empty play must carry a staging dir",
                not (not extra.get("mop_secrets_dir")), extra)
    except (AttributeError, TypeError) as e:
        c.fail("#592 the empty role play with no store", e)

    # (2а) vars.env -- симлинк наружу: значения по ссылке не выдаются.
    sroot = tempfile.mkdtemp(prefix="mop-test-link-592-")
    pdir = os.path.join(sroot, "proj")
    os.makedirs(os.path.join(pdir, "files"))
    outside = os.path.join(sroot, "outside.env")
    with open(outside, "w") as f:
        f.write("KEY=fixture\n")
    os.symlink(outside, os.path.join(pdir, "vars.env"))
    try:
        with bootstrap.role_staging(sroot, "proj", {"files": [], "vars": ["KEY"]}):
            c.fail("#592 a symlinked vars.env must be refused", None)
    except ValueError:
        pass

    # (2б) Каталог проекта -- симлинк наружу: хранилище читается по ссылке
    #      целиком, листовая проверка файла её не видит. Проверяется каждая
    #      ступень от корня хранилища.
    r2 = tempfile.mkdtemp(prefix="mop-test-link2-592-")
    real = os.path.join(r2, "real")
    os.makedirs(os.path.join(real, "files"))
    with open(os.path.join(real, "files", "b"), "w") as f:
        f.write("THROUGH-LINK")
    os.symlink(real, os.path.join(r2, "proj"))
    try:
        with bootstrap.role_staging(r2, "proj", {"files": ["b"], "vars": []}):
            c.fail("#592 a symlinked project dir must be refused", None)
    except ValueError:
        pass

    # (2в) Предок выбранного файла -- симлинк-каталог: путь к листу тоже
    #      не должен идти по ссылкам. ci здесь -- ссылка на соседний
    #      каталог с настоящим файлом: листовая проверка прошла бы.
    r3 = tempfile.mkdtemp(prefix="mop-test-link3-592-")
    p3 = os.path.join(r3, "proj", "files")
    os.makedirs(p3)
    realci = os.path.join(r3, "elsewhere")
    os.makedirs(realci)
    with open(os.path.join(realci, "runner.key"), "w") as f:
        f.write("REAL")
    os.symlink(realci, os.path.join(p3, "ci"))
    try:
        with bootstrap.role_staging(r3, "proj",
                                    {"files": ["ci/runner.key"], "vars": []}):
            c.check("#592 a file through an ancestor symlink must not stage", False, None)
    except ValueError:
        pass

    # (3) Неканоническое имя при существующей цели: отказ до хранилища.
    nroot = tempfile.mkdtemp(prefix="mop-test-canon-592-")
    project_secrets.put_file(nroot, "proj", "b", b"REAL")
    for bad in ("a/../b", "./b", "b/", "*"):
        try:
            with bootstrap.role_staging(nroot, "proj", {"files": [bad], "vars": []}):
                c.fail(f"#592 a noncanonical name {bad!r} must be refused", bad)
        except ValueError:
            pass

    # (3б) Ревью ac0fe15: хранилище само стрипит и нормализует имя
    #      (valid_name), и написание с пробелом (' b') выбирало чужой
    #      секрет ('b'): валидатор имена лишь проверял, а разницу между
    #      «названо» и «лежит» не ловил. Любое написание, которое
    #      хранилище канонизирует в другое, -- отказ: селекция выбирает
    #      ровно названное, никогда молча другое имя.
    for bad in (" b", "b ", " b ", "\tb", "b\n"):
        try:
            with bootstrap.role_staging(nroot, "proj", {"files": [bad], "vars": []}):
                c.fail(f"#592 a storage-canonicalized name {bad!r} must be refused",
                       bad)
        except ValueError:
            pass

    # (4) Гонка чтения vars: первое чтение видит ключ, второе -- нет.
    #     Либо отказ, либо материализовано запрошенное: пустой успех --
    #     тихая утечка права.
    root4 = tempfile.mkdtemp(prefix="mop-test-race-592-")
    project_secrets.set_var(root4, "proj", "KEY", "fixture")
    calls = {"n": 0}

    def racing(root, project):
        calls["n"] += 1
        return {"KEY": "fixture"} if calls["n"] == 1 else {}

    with _patched(project_secrets, _vars=racing):
        try:
            with bootstrap.role_staging(root4, "proj", {"files": [], "vars": ["KEY"]}) as stage:
                with open(os.path.join(stage, "vars.env")) as f:
                    text = f.read()
                c.check("#592 the staged snapshot must carry the requested var",
                        "KEY=fixture" in text, text)
        except ValueError:
            c.check("#592 a racy var read must refuse rather than deliver empty",
                    False, "delivered an empty staging")

    # (5) Сбой уборки не глотается: ignore_errors -- не способ убрать
    #     каталог секретов, отказ уборки обязан быть слышен (без значений).
    root5 = tempfile.mkdtemp(prefix="mop-test-clean-592-")

    def picky_rmtree(path, ignore_errors=False, *a, **kw):
        if not ignore_errors:
            raise OSError("staging cleanup refused")

    try:
        with _patched(_sh, rmtree=picky_rmtree):
            with bootstrap.role_staging(root5, "proj", {"files": [], "vars": []}):
                pass
        c.fail("#592 a cleanup failure must surface, not be swallowed", None)
    except OSError:
        pass

    # (6) Исключение в теле: staging убирается и там (закрепление
    #     заявления отчёта, которого прежде не было в проверках).
    stage_of = {}
    try:
        with bootstrap.role_staging(root5, "proj", {"files": [], "vars": []}) as st:
            stage_of["p"] = st
            raise RuntimeError("body boom")
    except RuntimeError:
        pass
    c.check("#592 an exception in the body must still clean the staging",
            not (stage_of.get("p") and os.path.exists(stage_of["p"])), stage_of)


def check_bundle_seam_592(c):
    """HYPOTHESIS (#592, интеграция с #580): play() принимает роль голым
    словарём {"files","vars"} -- у сервера нет шва, куда пришить реальный
    бандл из общего API: allowlist нельзя получить из soul.Bundle, и
    селекцию секрeтов остаётся кормить самодельным словарём, каким его
    соберёт будущий резолвер #581 из мета джоба.
    SOLUTION: play()/role_staging() принимают бандл soul.Bundle из
    доверенного хранения (#581): allowlist -- bundle.manifest.secrets,
    никакого второго разбора soul.yaml на сервере; словарь остаётся
    внутренним переходным входом (его переведёт сам шов), а грамматика
    имён и весь харденинг d68117d (ссылки, канонические имена, один
    слепок, громкая уборка) -- без изменений.
    RESULT: STATUS: RED — see #592"""
    from mop.common import project_secrets, soul
    import subprocess as _sp
    from _lib import patched as _patched

    def fake_run(cmd, **kw):
        played = fake_run.played = getattr(fake_run, "played", {})
        played["argv"] = cmd

        class R:
            returncode = 0
            stdout = "PLAY RECAP ***\nh : ok=1 failed=0\n"
            stderr = ""
        return R()

    # Настоящий бандл из общего API #580: каталог роли с блоком secrets.
    base = tempfile.mkdtemp(prefix="mop-test-bundle-592-")
    role_dir = os.path.join(base, "deployer")
    os.makedirs(role_dir)
    manifest = """soul: deployer
version: 1
runtime: {driver: any, receives_dispatch: false}
capabilities:
  node_verbs: [ping]
  cluster_verbs: []
  dispatch: false
  spawn: []
secrets:
  files: [gitlab-token]
  vars: [DEPLOY_KEY]
prompt: soul.md
"""
    with open(os.path.join(role_dir, "soul.yaml"), "w") as f:
        f.write(manifest)
    with open(os.path.join(role_dir, "soul.md"), "wb") as f:
        f.write(b"You deploy.\n")
    bundle = soul.load(role_dir)
    c.check("#592 the fixture bundle carries the secret allowlist",
            (list(bundle.manifest.secrets.files),
             list(bundle.manifest.secrets.vars)) == (["gitlab-token"],
                                                     ["DEPLOY_KEY"]),
            bundle.manifest.secrets)

    # Хранилище с именованными секретами.
    root = tempfile.mkdtemp(prefix="mop-test-store-592-")
    project_secrets.put_file(root, "proj", "gitlab-token", b"TOKEN")
    project_secrets.set_var(root, "proj", "DEPLOY_KEY", "dep")
    project_secrets.set_var(root, "proj", "OTHER", "alien")

    # (1) role_staging принимает бандл: в staging -- ровно allowlist роли.
    try:
        with bootstrap.role_staging(root, "proj", bundle) as stage:
            with open(os.path.join(stage, "files", "gitlab-token"), "rb") as f:
                c.expect("#592 the bundle stages its allowed file",
                         f.read(), b"TOKEN")
            with open(os.path.join(stage, "vars.env")) as f:
                c.expect("#592 the bundle stages its allowed var",
                         f.read(), "DEPLOY_KEY=dep\n")
            c.check("#592 an unallowed var stays out of the staging",
                    not os.path.exists(os.path.join(stage, "files", "other")))
    except (AttributeError, TypeError) as e:
        c.fail("#592 role_staging must accept a soul.Bundle", e)

    # (2) play() с бандлом кормит плейбук staging'ом бандла; словарь из
    #     тела запроса путь не проходит: бандл -- доверенный вход, словарь
    #     остаётся внутренним переходом шва.
    try:
        with _patched(bootstrap, ROOT=os.path.join(root, "no-ws")), \
                _patched(project_secrets, ROOT=root), \
                _patched(bootstrap, TIMEOUT=120), \
                _patched(_sp, run=fake_run):
            got = bootstrap.play({"name": "pu-proj-1", "address": ""},
                                 "proj", bundle=bundle)
        c.check("#592 play with a bundle runs and succeeds",
                not (not got.get("played") or not got.get("ok")), got)
        argv = getattr(fake_run, "played", {}).get("argv") or []
        extra = json.loads(argv[argv.index("-e", argv.index("-e") + 1) + 1])
        staged = extra.get("mop_secrets_dir")
        c.check("#592 play feeds the bundle's staging, not the store",
                not (not staged
                     or staged == project_secrets.project_dir(root, "proj")),
                extra)
    except (AttributeError, TypeError) as e:
        c.fail("#592 play must accept a soul.Bundle", e)

    # (3) Бандл без блока secrets -- пустой allowlist: прогон всё равно
    #     играется и вычищает прежнее тело (d68117d #1).
    plain_dir = os.path.join(base, "plain")
    os.makedirs(plain_dir)
    with open(os.path.join(plain_dir, "soul.yaml"), "w") as f:
        f.write(manifest.replace(
            "secrets:\n  files: [gitlab-token]\n  vars: [DEPLOY_KEY]\n", ""))
    with open(os.path.join(plain_dir, "soul.md"), "wb") as f:
        f.write(b"Plain.\n")
    plain = soul.load(plain_dir)
    try:
        with _patched(bootstrap, ROOT=os.path.join(root, "no-ws")), \
                _patched(project_secrets, ROOT=root), \
                _patched(bootstrap, TIMEOUT=120), \
                _patched(_sp, run=fake_run):
            got = bootstrap.play({"name": "pu-proj-1", "address": ""},
                                 "proj", bundle=plain)
        c.check("#592 a secrets-less bundle still plays to clear the body",
                not (not got.get("played") or not got.get("ok")), got)
    except (AttributeError, TypeError) as e:
        c.fail("#592 a secrets-less bundle must still play", e)


def check_roleless_refused_662(c):
    """HYPOTHESIS (#662): ответ bootstrap'а без тройки в Meta шёл по
    легаси-пути: with_creds(puppet_creds(project)) выдавал кред проекта
    чужому имени. SOLUTION: роль обязательна у каждого джоба: Meta без
    тройки -- сломанный джоб, отказ fail-closed до прогона; путь
    проектных кредов из ответа умер вместе с with_creds/puppet_creds."""
    from mop.common.domain import JobMeta
    from mop.server import grants
    name = "pu-proj-1"
    root = tempfile.mkdtemp(prefix="mop-test-bootstrap-662-")
    manifest, prompt, digest = role_bundle()
    inc = "00000000-0000-0000-0000-000000000662"
    plays = []

    def play(req, project, **kw):
        plays.append(req["name"])
        return {"ok": True, "played": True, "rc": 0, "seconds": 1.0}

    grant_root = tempfile.mkdtemp(prefix="mop-test-bootstrap-662g-")
    rec = grants.prepare("proj", manifest["soul"], digest, manifest, prompt,
                         "anton", "req-662", name, inc, "fixture",
                         root=grant_root)
    grants.activate(rec["principal"], root=grant_root)
    meta = JobMeta("git@example:team/proj.git", None, None,
                   manifest["soul"], digest, inc)
    good = {"verb": "bootstrap", "name": name, "address": "192.0.2.10",
            "_caller": "node-wate", "_reply": "_MOP_REPLY.node-wate.a"}

    # Ролевой джоб: бандл в прогон, ответ несёт душу и кред принципала.
    with patched(bootstrap, ROOT=root, play=play, _soul_root=lambda: grant_root,
                 _assigned_node=lambda n: "wate",
                 _owner_profile=lambda login: {"login": login, "name": "A",
                                               "email": "a@example.dev"},
                 _job_meta=lambda n: meta):
        out = bootstrap.answer("proj", dict(good))
    c.check("#662 a role job still plays and gets its principal credential",
            out.get("ok") is True and plays == [name]
            and (out.get("bus") or {}).get("user") == rec["principal"]
            and (out.get("soul") or {}).get("role") == manifest["soul"], out)

    # Meta без тройки -- сломанный джоб, не легаси: отказ до прогона.
    for label, broken in (("an empty Meta", JobMeta(None, None)),
                          ("a legacy Meta with an origin",
                           JobMeta("git@example:team/proj.git", None))):
        plays.clear()
        with patched(bootstrap, ROOT=root, play=play, _soul_root=lambda: grant_root,
                     _job_meta=lambda n: broken):
            denied = bootstrap.answer("proj", dict(good))
        c.check(f"#662 {label} is refused loudly, never a legacy bootstrap",
                bool(denied.get("error")) and plays == []
                and "bus" not in denied and "soul" not in denied, denied)
    # Половина тройки -- тот же отказ (существующее fail-closed живо):
    # JobMeta.from_job сам отказывает сборке, и отказ едет наружу.
    plays.clear()

    def half_meta(name):
        raise ValueError("JobMeta.soul='puppet': the role triple comes "
                         "whole (soul, soul_digest, soul_inc) or not at all")
    with patched(bootstrap, ROOT=root, play=play, _soul_root=lambda: grant_root,
                 _job_meta=half_meta):
        denied = bootstrap.answer("proj", dict(good))
    c.check("#662 a half role triple is still a broken job",
            bool(denied.get("error")) and plays == [] and "bus" not in denied,
            denied)
    # Легаси-хвост умер: путь проектного креда из ответа не существует.
    c.check("#662 the legacy project-credential tail is gone from the module",
            not hasattr(bootstrap, "with_creds")
            and not hasattr(bootstrap, "puppet_creds"),
            [n for n in ("with_creds", "puppet_creds")
             if hasattr(bootstrap, n)])


def main():
    c = Checks()
    check_journal_no_secrets_349(c)
    check_give_up_345(c)
    check_result_334(c)
    check_role_secrets_592(c)
    check_role_secrets_hard_592(c)
    check_bundle_seam_592(c)
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

    # HYPOTHESIS (#662): Джоб без тройки роли в Meta bootstrap'а --
    # прежний легаси-путь с проектным кредом (with_creds/puppet_creds):
    # джоб без души получал шину проекта. SOLUTION: тройка обязательна:
    # такой Meta -- громкий отказ fail-closed, ДО прогона, без кредов;
    # with_creds и puppet_creds умерли вместе с легаси-путём. STATUS: RED
    # — see #662
    check_roleless_refused_662(c)

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
