"""Bootstrap песочницы: .mop/bootstrap.yaml проекта играет СЕРВЕР при каждом
старте песочницы (#62).

До этого манифест при подъёме не играл никто: единственным механизмом было
копирование env-файлов врапером по PU_SEED, и оно глотало отказ. Решение
оператора 22.09 — играть сервером, ценой времени холодного старта ради
гибкости настройки окружения. Четыре решения внутри, каждое названо:

  ansible на сервере   ставит роль deploy/roles/bootstrap; сервер — та
                       машина, которая включена всегда, и у неё есть
                       дорога к телам (#59);
  ключ в тело          у сервера свой ключ (~/.ssh/mop-bootstrap), и в
                       образе его НЕТ: узел впускает его в тело на время
                       bootstrap'а (admit) и выпускает после — постоянный
                       ключ был бы второй дорогой к телу мимо агента, то
                       есть мимо единственной проверки проектирования. На
                       узле, где тело равно узлу, ключ лежит в
                       authorized_keys пользователя пула постоянно: у
                       сервера-контроллера дорога на узел и так есть;
  хранение файла       ~/.config/mop/bootstrap/<папет>-{tasks,vars}.yml на
                       сервере (#133): workspace -- файл папета, sandbox --
                       проекта. Кладут его только глаголы жизненного цикла
                       папета (`mop add`, `mop update`, `mop recycle`) из
                       рабочей копии мастера, пустой текст снимает.
                       Слать в момент старта нельзя: Nomad перезапускает
                       аллокации сам, когда мастера может не быть вовсе;
  триггер              узел на старте зовёт сервер (mop.<проект>.server.rpc,
                       глагол bootstrap) и ЖДЁТ ответа, прежде чем открыть
                       tmux. Отказ — это отказ: врапер выходит ненулём,
                       Nomad перезапускает, ростер показывает падение.
                       Папет не поднимается «наполовину».

Что теряется, названо: файл из рабочей копии мастера — не из дефолтной
ветки origin. Решает мастер, и это приемлемо; из клона папета файл не едет
никогда — на host-узле это было бы исполнение с рабочей ветки в общем теле.

Потолок времени: bootstrap идёт и при лечении залипшего папета (doctor
--fix, restart). Держать в секундах; тяжёлое — в sandbox.yaml.

Обе половины в одном файле, как у драйвера: узел (run) и сервер (serve)
— одно решение. Узловая половина живёт stdlib'ом плюс шиной.
"""
import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import time

from ..common import (bus, busnames, config, creds, fsutil, manifest, paths, project_secrets,
                      service)
from .. import driver
from . import identity, playvars

# На сервере: файлы проектов и ключ к телам.
ROOT = paths.local(paths.BOOTSTRAP)
# На сервере: пароли папетов, по одному на проект. Их заводит сам сервер
# (`mop server cluster users`, #116) и раздаёт в ответе на bootstrap: узел
# перестаёт хранить кред проекта, у которого на нём сейчас никто не живёт
# (#83). Каталог отдельный от servers/<адрес>/ намеренно — тот про то, что
# держит ОПЕРАТОР, и пароль папета оператору не положен (creds.pick).
PUPPET_CREDS = paths.local("puppets")
KEY = os.path.expanduser("~/.ssh/mop-bootstrap")
PLAYBOOK = os.path.join(config.PROJECT, "deploy", "bootstrap.yml")
# Файл рабочей копии, из которого `mop add|update|recycle` шлют workspace.
FILE = manifest.BOOTSTRAP_FILE
# Сколько узел ждёт сервер. Больше «секунд», чтобы прогон с загрузкой не
# срывался на ровном месте; меньше — чтобы висящий сервер читался отказом,
# а не молчащим папетом.
TIMEOUT = busnames.BOOTSTRAP_TIMEOUT


# ─── хранение на сервере ─────────────────────────────────────────────────
def files_of(root, project):
    """(задачи, конфигурация) проекта — пути или None, если нет."""
    tasks = os.path.join(root, f"{project}-tasks.yml")
    of_vars = os.path.join(root, f"{project}-vars.yml")
    return (tasks if os.path.exists(tasks) else None,
            of_vars if os.path.exists(of_vars) else None)


def store(root, project, text):
    """Положить bootstrap проекта: text — содержимое .mop/bootstrap.yaml, пусто
    — снять (проект убрал файл, сервер не должен играть вчерашний).
    -> {tasks, vars, alien}; кривая форма — ValueError с именем проекта.

    Просьбы о размерах (PROJECT_SCOPED) здесь не на месте — они дело
    песочницы — и идут в alien по имени, а не глотаются."""
    tasks_path = os.path.join(root, f"{project}-tasks.yml")
    vars_path = os.path.join(root, f"{project}-vars.yml")
    if not text.strip():
        for p in (tasks_path, vars_path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        return {"tasks": 0, "vars": 0, "alien": []}
    # Лениво: manifest тянет puppets, а bootstrap ему нужен ради одного разбора.
    from ..common import manifest
    try:
        mvars, tasks = manifest.play(text)
    except ValueError as e:
        raise ValueError(f"{project}/{FILE}: {e}")
    asks, mine, alien = manifest.parts(mvars)
    alien = sorted(set(alien) | set(asks))
    import yaml
    # 0700 и лежащему (#170): makedirs с mode не трогает уже существующий.
    fsutil.make_private_dir(root)
    for path, what in ((tasks_path, tasks), (vars_path, mine)):
        if what:
            with open(path, "w") as f:
                yaml.safe_dump(what, f, allow_unicode=True, default_flow_style=False)
        else:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    return {"tasks": len(tasks), "vars": len(mine), "alien": alien}


def refusal(req, project):
    """Почему запрос узла не годится, либо None. Проект — из субъекта (его
    держат права NATS), имя — из тела: узел, представившийся своим
    субъектом, не может попросить сыграть чужой bootstrap в своё тело."""
    name = req.get("name") or ""
    if not driver.valid_name(name):
        return driver.bad_name(name)
    if driver.project_of_name(name) != project:
        return f"puppet {name} is not in project {project}"
    return None


def needs_play(tasks, secrets):
    """Играть ли рамку: у проекта есть задачи или секреты (#128). Ни того ни
    другого -- ответ без прогона: большинству проектов хватает общего."""
    return bool(tasks or secrets)


def split_address(address):
    """`адрес[:порт]` от узла -> (адрес, порт или None). Чистая функция.

    Порт шлёт host-узел со sshd не на 22 (#201); тело pve и узел на 22 шлют
    голый адрес. Строка пришла с шины, поэтому порт — число в пределах, а
    не что угодно в -e прогона: иное — ValueError, и answer отдаёт отказ."""
    host, sep, port = address.rpartition(":")
    if not sep:
        return address, None
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"bad node address {address!r}: wanted address[:port]")
    return host, int(port)


def argv(playbook, address, user, key, settings, project, name, clone, tasks, of_vars,
         secrets=None):
    """Аргументы прогона: одна машина по адресу, пользователь пула, ключ
    сервера. Ключ хоста не спрашивается и не помнится: тело пересоздаётся и
    приезжает с новым, а известного заранее у сервера нет — цена названа
    (сеть тел — локальная за NAT узла, и дорога в неё только с сервера)."""
    # Всё одним JSON'ом, и это не вкус: голое `-e k=v` со значением в
    # несколько слов ansible режет по пробелам на несколько пар, и до ssh
    # доезжало одно `-o` («no argument after keyword -o», первый живой прогон).
    host, port = split_address(address)
    extra = {"mop_project": project, "mop_puppet": name, "mop_clone": clone,
             "ansible_user": user,
             "ansible_ssh_private_key_file": key,
             "ansible_ssh_common_args": "-o StrictHostKeyChecking=no "
                                        "-o UserKnownHostsFile=/dev/null "
                                        "-o IdentitiesOnly=yes -o ConnectTimeout=10 "
                                        "-o LogLevel=ERROR"}
    if port is not None:
        extra["ansible_port"] = port
    if tasks:
        extra["mop_bootstrap_tasks"] = tasks
    if of_vars:
        extra["mop_bootstrap_vars"] = of_vars
    # Секреты проекта (#127) -- путём к каталогу, не значениями: argv
    # прогона виден в ps любому на сервере.
    if secrets:
        extra["mop_secrets_dir"] = secrets
    return ["ansible-playbook", "-i", f"{host},", playbook,
            "-e", json.dumps(settings, ensure_ascii=False),
            "-e", json.dumps(extra, ensure_ascii=False)]


# ─── сервер ──────────────────────────────────────────────────────────────
def play(req, project):
    """Сыграть bootstrap проекта в песочницу папета. -> {ok, played, ...}.
    Нет файла — ok без прогона: большинству проектов хватает общего."""
    # workspace -- файл папета (#133): копия того, кто стартует, а не проекта.
    tasks, of_vars = files_of(ROOT, req["name"])
    secrets = project_secrets.project_dir(project_secrets.ROOT, project)
    secrets = secrets if os.path.isdir(secrets) else None
    if not needs_play(tasks, secrets):
        return {"ok": True, "played": False, "text": f"no bootstrap for {project}"}
    name = req["name"]
    cmd = argv(PLAYBOOK, req.get("address") or "", config.get("MOP_USER"), KEY,
               playvars.playbook_vars(), project, name, driver.clone_dir(name),
               tasks, of_vars, secrets)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT - 10,
                           env={**os.environ, "ANSIBLE_CONFIG": os.path.join(config.PROJECT, "ansible.cfg")})
    except subprocess.TimeoutExpired:
        return {"ok": False, "played": True, "rc": None,
                "seconds": round(time.time() - t0, 1),
                "tail": f"bootstrap of {name} did not finish in {TIMEOUT - 10}s — "
                        f"keep bootstrap.yaml in seconds, bake the heavy part into sandbox.yaml"}
    return outcome(r.returncode, r.stdout + r.stderr, time.time() - t0)


# Строка провала задачи ansible: `fatal: [хост]: FAILED! => {...}` или, у
# элемента цикла, `failed: [хост] (item=…) => {...}`. `failed=1` в PLAY
# RECAP -- не она: там нет ни двоеточия, ни хоста в скобках.
_FAILED_LINE = re.compile(r"^(?:fatal|failed): \[")
_TASK = re.compile(r"^TASK \[(.+?)\] \**$")
MESSAGE_MAX = 200


def failed_task(output):
    """Вывод ansible-playbook -> (задача, сообщение) | (None, None) (#333).

    Задача -- последний заголовок `TASK [...]` перед первой строкой провала;
    сообщение -- msg из `=> {...}`, если разбирается, иначе сама строка,
    одной строкой (она уходит в строку stderr врапера) и до MESSAGE_MAX.
    Провала нет или он до первой задачи (синтаксис, модуль) -- (None, None):
    тогда говорит хвост."""
    task = None
    for line in (output or "").splitlines():
        line = line.strip()
        header = _TASK.match(line)
        if header:
            task = header.group(1)
            continue
        if not _FAILED_LINE.match(line):
            continue
        if task is None:
            return None, None
        _, sep, result = line.partition("=> ")
        message = None
        if sep:
            try:
                got = json.loads(result)
                message = got.get("msg") if isinstance(got, dict) else None
            except ValueError:
                pass
        message = " ".join(str(message or line).split())
        return task, message[:MESSAGE_MAX]
    return None, None


def outcome(rc, output, seconds):
    """Итог сыгранного прогона -> ответ bootstrap'а. Чистая. task и message
    -- у каждого ответа (None, если провала нет): их читает `mop update`
    (#334), поля стабильны."""
    task, message = failed_task(output) if rc else (None, None)
    return {"ok": rc == 0, "played": True, "rc": rc, "seconds": round(seconds, 1),
            "tail": "\n".join((output or "").splitlines()[-25:]),
            "task": task, "message": message}


def _puppets_here():
    """Папеты, чей workspace лежит на сервере (#133)."""
    try:
        return sorted(n[:-len("-tasks.yml")] for n in os.listdir(ROOT)
                      if n.endswith("-tasks.yml"))
    except FileNotFoundError:
        return []


def puppet_creds(project, root=None):
    """Кред папета проекта для ответа узлу, либо None — пароля нет."""
    path = os.path.join(root or PUPPET_CREDS, creds.puppet_pass_file(project))
    try:
        with open(path) as f:
            password = f.read().strip()
    except OSError:
        return None
    if not password:
        return None
    return creds.bus_config(config.get("MOP_SERVER_LAN"),
                            config.get("MOP_NATS_PORT"), project, password,
                            user=creds.puppet_user(project))


def with_creds(out, got, project):
    """Ответ bootstrap'а с кредом папета. Чистая функция.

    Нет пароля проекта — отказ, а не ответ без кредов (#114): других дорог
    креду в тело больше нет, прогон не кладёт `bus-<проект>.json` на узлы, и
    папет без шины читался бы мастером как живой, но молчащий. Отказ прогона
    главнее: кред к упавшему bootstrap'у не приклеиваем."""
    if out.get("error"):
        return out
    if not got:
        return {"error": f"no bus password for project {project} on the server "
                         f"-- register it: mop project add <origin>"}
    return {**out, "bus": got}


def owner_identity(req):
    """Глагол identity (#167): логин -> имя и почта из провайдера личностей
    сервера, для git identity в клоне папета.

    Здесь, а не у сервиса кластера: спрашивает агент узла, а в server.rpc
    узлу писать уже можно; cluster.rpc узлам закрыт, и открывать его ради
    одного вопроса значило бы дать узлу глаголы над Nomad. Имя и почту из
    тела не берём: источник -- только провайдер."""
    login = req.get("login")
    if not busnames.valid_login(login):
        return {"error": f"{login!r} is not a login"}
    return identity.profile(identity.server_provider(), login)


def answer(project, req, _send=None):
    """Ответ на один запрос. Зовётся в отдельном потоке (service.serve):
    прогон идёт секунды, а петля обязана отвечать остальным."""
    verb = req.get("verb")
    try:
        if verb == "ping":
            return {"ok": True, "puppets": _puppets_here()}
        if verb == "bootstrap":
            why = refusal(req, project)
            if why:
                return {"error": why}
            # Кред папета едет тем же ответом: узел уже позвал нас, и
            # второго разговора ради одного файла не нужно.
            return with_creds(play(req, project), puppet_creds(project), project)
        if verb == "identity":
            return owner_identity(req)
        # `put` снят (#133): workspace кладут глаголы жизненного цикла
        # папета у сервиса кластера, а в этот субъект пишут и узлы.
        return {"error": f"no such verb {verb}; available: ping, bootstrap, identity"}
    except Exception as e:
        return {"error": f"{verb}: {e}"}


def journal(project, req, out):
    """Строки журнала на один ответ; у проваленного прогона -- ещё и его хвост."""
    lines = [f"{project}.{req.get('verb')} {req.get('name', '')}: "
             f"{out.get('error') or ('ok' if out.get('ok') else out)}"
             + (f" in {out['seconds']}s" if out.get("seconds") is not None else "")]
    if not out.get("ok", True):
        lines.append(f"{out.get('tail', '')}")
    return lines


def banner(subject, root, puppets):
    return (f"mop-bootstrap: subscribed to {subject}, "
            f"workspaces in {root}: {', '.join(puppets) or 'none'}")


async def serve(log):
    """Подписчик сервера. Креды — машинного пользователя service (#104), не
    оператора: сервер слушает все проекты, и своего файла кредов у него нет —
    тот же каталог, что у дашборда (mop/common/creds.py)."""
    subj = bus.server_subject(busnames.ANY)
    await service.serve("mop-bootstrap", subj, answer, log, journal,
                        lambda: banner(subj, ROOT, _puppets_here()))


# ─── мастер ──────────────────────────────────────────────────────────────
