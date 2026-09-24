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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import bootstrap  # noqa: E402

TEXT = """
- name: bootstrap of proj
  vars:
    ENV_FILE: .env
    MOP_MEM_MB: 4096
  tasks:
    - name: env file
      ansible.builtin.copy: {src: ~/proj/.env, dest: "{{ mop_clone }}/.env"}
"""


def main():
    failed = 0
    root = tempfile.mkdtemp(prefix="mop-test-bootstrap-")

    # HYPOTHESIS: механизма нет вовсе — ничто не играет манифест при старте.
    # SOLUTION: файл проекта хранится на сервере заранее (deploy/add кладут), а
    # играется по запросу узла. STATUS: FIXED — see #62
    try:
        got = bootstrap.store(root, "proj", TEXT)
    except AttributeError:
        print("FAIL bootstrap.store is missing")
        return 1
    tasks, of_vars = bootstrap.files_of(root, "proj")
    if not (tasks and os.path.exists(tasks) and of_vars and os.path.exists(of_vars)):
        failed += 1
        print(f"FAIL store: {tasks!r} {of_vars!r}")
    if got.get("tasks") != 1:
        failed += 1
        print(f"FAIL store must count the tasks: {got}")
    # Просьба о размерах в bootstrap не на месте — названа, не проглочена.
    if got.get("alien") != ["MOP_MEM_MB"]:
        failed += 1
        print(f"FAIL store must name a size ask as alien: {got}")
    # Пустой текст — снять файлы: проект убрал bootstrap, сервер не должен
    # играть вчерашний.
    bootstrap.store(root, "proj", "")
    if bootstrap.files_of(root, "proj") != (None, None):
        failed += 1
        print("FAIL an empty bootstrap must remove the project's files")
    # Кривая форма — громко, с именем проекта.
    try:
        bootstrap.store(root, "bad", "vars: {}\n")
        failed += 1
        print("FAIL malformed: no error")
    except ValueError as e:
        if "bad" not in str(e):
            failed += 1
            print(f"FAIL malformed message must name the project: {e}")

    # Узел просит за папета; проект — из субъекта (права NATS), имя — из тела
    # запроса. Расхождение — отказ: узел, представившийся своим субъектом,
    # не может попросить сыграть чужой bootstrap в своё тело.
    if bootstrap.refusal({"name": "pu-proj-1"}, "proj") is not None:
        failed += 1
        print("FAIL refusal: a puppet of the project must pass")
    if bootstrap.refusal({"name": "pu-other-1"}, "proj") is None:
        failed += 1
        print("FAIL refusal: a puppet of another project must be refused")
    if bootstrap.refusal({"name": "pu-proj-1; id"}, "proj") is None:
        failed += 1
        print("FAIL refusal: a name that is not a puppet name must be refused")

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
        if want not in joined:
            failed += 1
            print(f"FAIL argv lacks {want!r}: {argv}")

    # Значение с пробелами едет только JSON'ом: голое `-e k=v w=x` ansible
    # режет по пробелам на несколько пар, и до ssh доезжало одно `-o`
    # (поймано первым живым прогоном: «no argument after keyword -o»).
    for i, a in enumerate(argv):
        if a == "-e" and " " in argv[i + 1] and not argv[i + 1].lstrip().startswith("{"):
            failed += 1
            print(f"FAIL a bare -e value with spaces is split by ansible: {argv[i + 1]!r}")

    # HYPOTHESIS (#114): ответ bootstrap без кредов — не отказ, потому что
    # узел держал `bus-<проект>.json` от прогона; снимаем файл — и папет
    # поднимается без шины, читаясь мастером как живой, но молчащий.
    # SOLUTION: сервер отказывает в bootstrap громко, с именем проекта.
    # STATUS: FIXED — see #114
    creds = {"url": "nats://s:4222", "user": "puppet-proj", "password": "p"}
    try:
        got = bootstrap.with_creds({"ok": True}, creds, "proj")
        if got.get("bus") != creds or got.get("error"):
            failed += 1
            print(f"FAIL with_creds must carry the credentials: {got}")
        got = bootstrap.with_creds({"ok": True}, None, "proj")
        if not got.get("error") or "proj" not in got["error"]:
            failed += 1
            print(f"FAIL with_creds: no password for the project must be "
                  f"a refusal naming it, got {got}")
        # Отказ прогона главнее: кред к упавшему bootstrap не приклеиваем.
        got = bootstrap.with_creds({"error": "play failed"}, creds, "proj")
        if got.get("bus") or got.get("error") != "play failed":
            failed += 1
            print(f"FAIL with_creds must keep the play's error: {got}")
    except AttributeError:
        failed += 1
        print("FAIL bootstrap.with_creds is missing")

    # HYPOTHESIS (#128): bootstrap играется, только если у проекта есть
    # задачи, и секретов проекта не знает. SOLUTION: рамка играется, если
    # есть задачи или секреты; секреты едут путём к каталогу на сервере, а
    # не значениями -- argv виден в ps. STATUS: FIXED — see #128
    try:
        if bootstrap.needs_play(None, None) or not bootstrap.needs_play(None, "/s/sec") \
                or not bootstrap.needs_play("/s/t.yml", None):
            failed += 1
            print("FAIL needs_play: play when there are tasks or secrets, only then")
        a = bootstrap.argv("/x/deploy/bootstrap.yml", "10.0.0.2", "pool", "/k",
                           {}, "proj", "pu-proj-1", "/c", None, None, secrets="/s/sec")
        extra = json.loads(a[a.index("-e", a.index("-e") + 1) + 1])
        if extra.get("mop_secrets_dir") != "/s/sec":
            failed += 1
            print(f"FAIL argv must carry the secrets dir: {extra}")
        if "mop_bootstrap_tasks" in extra:
            failed += 1
            print("FAIL argv without project tasks must not name any")
    except (AttributeError, TypeError) as e:
        failed += 1
        print(f"FAIL secrets in bootstrap: {e}")

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
        if inv != "192.168.1.37," or extra.get("ansible_port") != 2222:
            failed += 1
            print(f"FAIL argv for address:port must play the address on ansible_port: "
                  f"-i {inv!r}, ansible_port={extra.get('ansible_port')!r}")
        inv, extra = inventory_and_extra("10.77.38.100")
        if inv != "10.77.38.100," or "ansible_port" in extra:
            failed += 1
            print(f"FAIL argv for a bare address must stay today's: -i {inv!r}, {extra}")
        for bad in ("192.168.1.37:22x", "192.168.1.37:0", "192.168.1.37:70000", ":2222"):
            try:
                inv, extra = inventory_and_extra(bad)
            except ValueError:
                continue
            failed += 1
            print(f"FAIL argv must refuse the address {bad!r}, played -i {inv!r} "
                  f"ansible_port={extra.get('ansible_port')!r}")
    except (AttributeError, TypeError) as e:
        failed += 1
        print(f"FAIL ssh port in bootstrap: {e}")

    # HYPOTHESIS (#133): workspace хранился на проект и писался четырьмя
    # дорогами; удаление из рабочей копии не доходило, и старт играл
    # проектную копию из origin. SOLUTION: workspace -- файл папета, старт
    # играет копию своего папета. STATUS: FIXED — see #133
    from mop import cluster, project_secrets
    root = tempfile.mkdtemp(prefix="mop-test-ws-")
    saved = (bootstrap.ROOT, project_secrets.ROOT)
    bootstrap.ROOT, project_secrets.ROOT = root, os.path.join(root, "no-secrets")
    try:
        bootstrap.store(root, "proj", TEXT)               # старая проектная копия
        got = bootstrap.play({"name": "pu-proj-1", "address": ""}, "proj")
        if got.get("played") is not False:
            failed += 1
            print(f"FAIL play must ignore a project-level copy: {got}")
        # add/update несут workspace: текст -- положить папету, пусто -- снять,
        # нет поля -- не трогать (restart без рабочей копии).
        cluster.store_workspace(root, "pu-proj-1", {"workspace": TEXT})
        if bootstrap.files_of(root, "pu-proj-1")[0] is None:
            failed += 1
            print("FAIL store_workspace must lay the puppet's own copy")
        cluster.store_workspace(root, "pu-proj-1", {})
        if bootstrap.files_of(root, "pu-proj-1")[0] is None:
            failed += 1
            print("FAIL a request without workspace must leave the copy alone")
        cluster.store_workspace(root, "pu-proj-1", {"workspace": ""})
        if bootstrap.files_of(root, "pu-proj-1") != (None, None):
            failed += 1
            print("FAIL an empty workspace must remove the puppet's copy")
    except AttributeError as e:
        failed += 1
        print(f"FAIL workspace per puppet: {e}")
    finally:
        bootstrap.ROOT, project_secrets.ROOT = saved

    print("bootstrap: FAILED" if failed else "bootstrap: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
