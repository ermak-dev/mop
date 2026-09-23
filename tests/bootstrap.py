#!/usr/bin/env python3
"""Проверка bootstrap'а песочницы без пула: python3 tests/bootstrap.py

Bootstrap (#62): .mop/bootstrap.yaml проекта играет СЕРВЕР при каждом старте
песочницы, узел зовёт его с шины и ждёт. Чистое здесь — хранение файла на
сервере, аргументы прогона и проверка, что узел просит про папета своего
проекта. Сам прогон, ключ в тело и ожидание врапера — только на живом пуле.
"""
import os
import sys
import tempfile

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

    print("bootstrap: FAILED" if failed else "bootstrap: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
