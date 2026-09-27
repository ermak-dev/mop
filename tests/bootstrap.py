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


def main():
    c = Checks()
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
    return c.report("bootstrap")


if __name__ == "__main__":
    sys.exit(main())
