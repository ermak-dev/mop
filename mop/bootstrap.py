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
import shlex
import socket
import subprocess
import sys
import time

from . import bus, busnames, config, creds, driver, playvars, project_secrets, service

# На сервере: файлы проектов и ключ к телам.
ROOT = os.path.expanduser("~/.config/mop/bootstrap")
# На сервере: пароли папетов, по одному на проект. Их заводит сам сервер
# (`mop cluster users`, #116) и раздаёт в ответе на bootstrap: узел
# перестаёт хранить кред проекта, у которого на нём сейчас никто не живёт
# (#83). Каталог отдельный от servers/<адрес>/ намеренно — тот про то, что
# держит ОПЕРАТОР, и пароль папета оператору не положен (creds.pick).
PUPPET_CREDS = os.path.expanduser("~/.config/mop/puppets")
KEY = os.path.expanduser("~/.ssh/mop-bootstrap")
PLAYBOOK = os.path.join(config.PROJECT, "deploy", "bootstrap.yml")
# На узле: публичная часть ключа сервера, её кладёт `mop deploy`.
PUB_ON_NODE = f"{driver.HOME}/.config/mop/bootstrap.pub"
# Файл рабочей копии, из которого `mop add|update|recycle` шлют workspace.
FILE = ".mop/bootstrap.yaml"
# Сколько узел ждёт сервер. Больше «секунд», чтобы прогон с загрузкой не
# срывался на ровном месте; меньше — чтобы висящий сервер читался отказом,
# а не молчащим папетом.
TIMEOUT = 300


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
    from . import manifest
    try:
        mvars, tasks = manifest.play(text)
    except ValueError as e:
        raise ValueError(f"{project}/{FILE}: {e}")
    asks, mine, alien = manifest.parts(mvars)
    alien = sorted(set(alien) | set(asks))
    import yaml
    os.makedirs(root, mode=0o700, exist_ok=True)
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


def argv(playbook, address, user, key, settings, project, name, clone, tasks, of_vars,
         secrets=None):
    """Аргументы прогона: одна машина по адресу, пользователь пула, ключ
    сервера. Ключ хоста не спрашивается и не помнится: тело пересоздаётся и
    приезжает с новым, а известного заранее у сервера нет — цена названа
    (сеть тел — локальная за NAT узла, и дорога в неё только с сервера)."""
    # Всё одним JSON'ом, и это не вкус: голое `-e k=v` со значением в
    # несколько слов ansible режет по пробелам на несколько пар, и до ssh
    # доезжало одно `-o` («no argument after keyword -o», первый живой прогон).
    extra = {"mop_project": project, "mop_puppet": name, "mop_clone": clone,
             "ansible_user": user,
             "ansible_ssh_private_key_file": key,
             "ansible_ssh_common_args": "-o StrictHostKeyChecking=no "
                                        "-o UserKnownHostsFile=/dev/null "
                                        "-o IdentitiesOnly=yes -o ConnectTimeout=10 "
                                        "-o LogLevel=ERROR"}
    if tasks:
        extra["mop_bootstrap_tasks"] = tasks
    if of_vars:
        extra["mop_bootstrap_vars"] = of_vars
    # Секреты проекта (#127) -- путём к каталогу, не значениями: argv
    # прогона виден в ps любому на сервере.
    if secrets:
        extra["mop_secrets_dir"] = secrets
    return ["ansible-playbook", "-i", f"{address},", playbook,
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
    tail = "\n".join((r.stdout + r.stderr).splitlines()[-25:])
    return {"ok": r.returncode == 0, "played": True, "rc": r.returncode,
            "seconds": round(time.time() - t0, 1), "tail": tail}


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
        # `put` снят (#133): workspace кладут глаголы жизненного цикла
        # папета у сервиса кластера, а в этот субъект пишут и узлы.
        return {"error": f"no such verb {verb}; available: ping, bootstrap"}
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
    """Подписчик сервера. Креды — оператора (admin): сервер слушает все
    проекты, и своего файла кредов у него нет — тот же каталог, что у
    дашборда (mop/creds.py)."""
    subj = bus.server_subject(busnames.ANY)
    await service.serve("mop-bootstrap", subj, answer, log, journal,
                        lambda: banner(subj, ROOT, _puppets_here()))


# ─── узел ────────────────────────────────────────────────────────────────
def toward_server():
    """Адрес этого узла со стороны сервера — тот, с которого узел сам ходит
    на сервер. Нужен узлу, где тело равно узлу: серверу надо куда-то
    прийти. Без сети: соединение UDP ничего не шлёт, только выбирает
    маршрут."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((config.get("MOP_SERVER_LAN"), 1))
        return s.getsockname()[0]
    finally:
        s.close()


def run(d, name, project):
    """Bootstrap песочницы папета с этого узла: впустить сервер, позвать,
    дождаться, выпустить. -> ответ сервера; отказ — RuntimeError/BusError.

    Зовётся из `mop driver run` до внутреннего врапера. Дверь закрывается в
    любом исходе: ключ сервера в теле живёт ровно столько, сколько идёт
    bootstrap."""
    if d.BODY_IS_NODE:
        address, pub = toward_server(), None
    else:
        address = d.address_of(name)
        try:
            with open(PUB_ON_NODE) as f:
                pub = f.read().strip()
        except FileNotFoundError:
            raise RuntimeError(f"no server key on this node ({PUB_ON_NODE}) — "
                               f"run mop deploy")
        r = asyncio.run(d.admit(name, pub))
        if r.get("error"):
            raise RuntimeError(f"cannot let the server into the body: {r['error']}")
    try:
        bus.connect(bus.NODE_FILE)
        out = bus.ask_server("bootstrap", timeout=TIMEOUT, project=project,
                             name=name, address=address)
    finally:
        if pub is not None:
            asyncio.run(d.admit(name, None))
    if out.get("error"):
        raise RuntimeError(out["error"])
    if not out.get("ok"):
        raise RuntimeError(f"bootstrap failed (ansible exit {out.get('rc')}):\n"
                           f"{out.get('tail', '')}")
    return out


# ─── мастер ──────────────────────────────────────────────────────────────
