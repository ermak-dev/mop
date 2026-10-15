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
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time

from ..common import (bus, busnames, config, creds, fsutil, manifest, paths, project_secrets,
                      service)
from ..common import soul as souls
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


def _sent_path(root, name):
    return os.path.join(root, f"{name}-sent.json")


def _approval_path(root, name):
    return os.path.join(root, f"{name}-approved.json")


def _result_path(root, name):
    return os.path.join(root, f"{name}-result.json")


def _read_json(path, why=None):
    """JSON по пути либо None, если файла нет. Битый/нечитаемый -- не
    «пусто», а громкий отказ (#582): проглоченная причина метки
    согласования читалась бы как «политика не согласована» -- и молча
    меняла решение о наследовании."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise OSError(f"{why or path}: unreadable: {e}") from e
    except ValueError as e:
        raise ValueError(f"{why or path}: corrupted JSON: {e}") from e


def read_sent(root, name):
    """Что уехало последней регистрацией папета (#334) либо None."""
    return _read_json(_sent_path(root, name))


def read_result(root, name):
    """Итог последнего прогона папета (#334) либо None."""
    return _read_json(_result_path(root, name))


def note_sent(root, name, prov, now=None):
    """Положить происхождение workspace рядом с ним (#334). -> метка.

    Метка -- регистрации, по часам сервера, а не клиента: итог прогона
    несёт её, и клиент ждёт итог именно своей регистрации. Строго больше
    прежней: две регистрации в одну наносекунду не сливаются."""
    before = (read_sent(root, name) or {}).get("marker") or 0
    marker = max(time.time_ns(), before + 1)
    fsutil.make_private_dir(root)
    fsutil.write_private(_sent_path(root, name), json.dumps(
        {**(prov or {}), "marker": marker, "at": int(now or time.time())}))
    return marker


# Сдаться (#345): столько подряд провалов прогона одного и того же
# workspace -- и джоб останавливается, а не перезапускается вечно. Провал
# прогона детерминирован (задача проекта упала), в отличие от лежащего
# сервера или ssh: те не считаются и живут прежним delay.
GIVE_UP_AFTER = 3
# Через сколько после ответа узлу просить сервис кластера остановить джоб:
# узел сперва закрывает дверь в тело (admit False), и стоп посреди bootstrap
# оставил бы в теле ключ сервера. Рестарт Nomad -- через 15 с, стоп успевает.
GIVE_UP_GRACE = 5
# ansible-playbook: 2 -- упала задача на хосте (3 -- хост недоступен).
TASK_FAILED_RC = 2


def note_approval(root, name):
    """Метка согласования политики папета-роли (#582): человек ЗАЯВИЛ
    политику полем workspace -- текст любой, включая пустой («пусто
    согласовано» -- законная политика, mop сам без .mop/bootstrap.yaml).
    Метка держит хеш снапшота на момент согласования: копия ребёнку
    сверяется с ней -- файлы, сменившиеся мимо человеческого пути,
    согласованием не считаются. Кладётся только человеческим
    add/update роли; грант метку не пишет."""
    fsutil.make_private_dir(root)
    fsutil.write_private(_approval_path(root, name), json.dumps(
        {"at": int(time.time()), "key": _snapshot_key(root, name)}))
    return True


def _snapshot_files(root, name):
    """(путь, байты) снапшота ОДНИМ чтением либо ValueError/OSError
    (#582, блокатор ревью): дальше эти же байты и хешируются, и пишутся
    -- хеш одного чтения, копия другого, разрешали подтасовку файлами,
    сменившимися между двумя ходами. Отсутствующий файл -- законное
    «пусто» (согласованный пустой снапшот); нечитаемый -- отказ."""
    out = []
    for path in files_of(root, name):
        if path is None:
            out.append((None, b""))
            continue
        try:
            with open(path, "rb") as f:
                out.append((path, f.read()))
        except OSError as e:
            raise OSError(f"{os.path.basename(path)}: unreadable: {e}") from e
    return out


def _snapshot_key(root, name):
    """Хеш хранимого снапшота политики: те же байты, что workspace_key,
    без метки регистрации -- согласование о файлах, не о прогонах."""
    h = hashlib.sha256()
    for _path, data in _snapshot_files(root, name):
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()


def approval_of(root, name):
    """Согласована ли политика папета-роли (#582): метка либо None."""
    return _read_json(_approval_path(root, name))


def drop_approval(root, name):
    """Снять метку согласования (#582): папета нет -- политики нет."""
    try:
        os.unlink(_approval_path(root, name))
    except FileNotFoundError:
        pass


def copy_approval(root, parent, child):
    """Наследование согласованной политики (#582): снапшот родителя под
    именем ребёнка, одним ходом и до Nomad -- ретрай того же request_id
    не может молча получить политику новее той, что закреплена резервом.
    Без метки согласования -- отказ: унаследовать несогласованное значило
    бы обойти границу, которую держит человек. Снапшот, не сходящийся с
    меткой (файлы сменились мимо человеческого пути), -- тот же отказ:
    согласование -- факт человека о конкретных байтах. -> отказ | None.

    Одно чтение -- одна запись (#582, блокатор ревью): байты читаются
    один раз, хеш сверяется с НИМИ, и пишутся именно они -- атомарно и
    приватно (fsutil.write_private): никакого re-read/copy между хешем
    и записью, файлы, сменившиеся в этом зазоре, до ребёнка не доезжают."""
    try:
        mark = approval_of(root, parent)
    except (OSError, ValueError) as e:
        return (f"{parent}: the approval marker is unreadable: {e} (#582)")
    if mark is None:
        return (f"{parent}: no approved bootstrap to inherit: its human "
                f"owner has not registered a policy (#582)")
    try:
        snapshot = _snapshot_files(root, parent)
    except OSError as e:
        return f"{parent}: the approved bootstrap is unreadable: {e} (#582)"
    h = hashlib.sha256()
    for _path, data in snapshot:
        h.update(data)
        h.update(b"\0")
    if h.hexdigest() != mark.get("key"):
        return (f"{parent}: the approved bootstrap changed outside the "
                f"human path: the stored snapshot does not match its "
                f"approval (#582)")
    fsutil.make_private_dir(root)
    for what, (path, data) in zip(("tasks", "vars"), snapshot):
        dst = os.path.join(root, f"{child}-{what}.yml")
        if path is None:
            # Файла родителя нет -- и у ребёнка его быть не должно:
            # пустая согласованная политика наследуется как пустота, а
            # не как пустой файл (его уже считал бы проигрыватель).
            try:
                os.unlink(dst)
            except FileNotFoundError:
                pass
            continue
        fsutil.write_private(dst, data)
    fsutil.write_private(
        _approval_path(root, child),
        json.dumps({"at": mark.get("at"), "key": mark.get("key"),
                    "inherited_from": parent}))
    return None


def workspace_key(root, name):
    """Ключ «того же workspace» (#345): метка регистрации (#334) и sha
    хранимых файлов -- новая регистрация или другой файл начинают счёт
    заново, в том числе у клиента без происхождения."""
    h = hashlib.sha256()
    for path in files_of(root, name):
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except (OSError, TypeError):
            pass
        h.update(b"\0")
    return [(read_sent(root, name) or {}).get("marker"), h.hexdigest()]


def counts_as_failure(out):
    """Прогон дошёл до конца и назвал упавшую задачу -- провал, который
    повторится (#345). Недоступный хост, таймаут, отказы до и после прогона
    -- нет: они проходят сами."""
    return bool(out.get("played") and not out.get("ok")
                and out.get("rc") == TASK_FAILED_RC and out.get("task"))


def gave_up_of(root, name):
    """Строка «сдался» для ростера и `alloc` (#345) либо None: запись итога
    говорит gave_up, и ключ -- текущий (update/recycle его сменили бы)."""
    rec = read_result(root, name) or {}
    if not rec.get("gave_up") or rec.get("key") != workspace_key(root, name):
        return None
    where = f" at task «{rec['task']}»" if rec.get("task") else ""
    why = f": {rec['message']}" if rec.get("message") else ""
    return (f"bootstrap gave up after {rec.get('failures')} attempts{where}{why}"
            f" — fix {FILE}, then mop update")


def note_result(root, name, out, now=None):
    """Итог прогона папета -- рядом с отправленным (#334): метка и коммит
    того, что уехало, плюс task/message, если прогон их назвал (#333).
    failures -- подряд идущие провалы этого ключа, gave_up -- их хватило
    (#345): удачный прогон обнуляет, преходящий провал счёт не трогает."""
    sent = read_sent(root, name) or {}
    key = workspace_key(root, name)
    prev = read_result(root, name) or {}
    before = (prev.get("failures") or 0) if prev.get("key") == key else 0
    if out.get("ok"):
        failures = 0
    elif counts_as_failure(out):
        failures = before + 1
    else:
        failures = before
    lines = [l for l in (out.get("tail") or "").splitlines() if l.strip()]
    record = {"ok": bool(out.get("ok")), "played": bool(out.get("played")),
              "seconds": out.get("seconds"), "rc": out.get("rc"),
              "task": out.get("task"), "message": out.get("message"),
              "last": lines[-1] if lines else None, "at": int(now or time.time()),
              "sent": sent.get("marker"), "commit": sent.get("commit"),
              "key": key, "failures": failures, "gave_up": failures >= GIVE_UP_AFTER}
    fsutil.make_private_dir(root)
    fsutil.write_private(_result_path(root, name), json.dumps(record))
    return record


def store(root, project, text):
    """Положить bootstrap проекта: text — содержимое .mop/bootstrap.yaml, пусто
    — снять (проект убрал файл, сервер не должен играть вчерашний).
    -> {tasks, vars, alien}; кривая форма — ValueError с именем проекта.

    Просьбы о размерах (PROJECT_SCOPED) здесь не на месте — они дело
    песочницы — и идут в alien по имени, а не глотаются."""
    tasks_path = os.path.join(root, f"{project}-tasks.yml")
    vars_path = os.path.join(root, f"{project}-vars.yml")
    if not text.strip():
        # С файлом уходят и отправленное с итогом (#334): их папета больше нет
        # на сервере, и чужой старый итог не должен прочитаться своим.
        for p in (tasks_path, vars_path, _sent_path(root, project),
                  _result_path(root, project), *_progress_paths(root, project)):
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


# ─── выдача кредов роли телу (#590/#603) ─────────────────────────────────
def _node_of(caller):
    """Имя узла из его машинного имени, либо None."""
    if isinstance(caller, str) and caller.startswith(busnames.NODE_PREFIX):
        return caller[len(busnames.NODE_PREFIX):] or None
    return None


def _soul_root():
    """Корень хранилища душ: шов проверок (патчится на временный каталог)."""
    from . import grants
    return grants._root_dir(None)


def _job_meta(name):
    """Мета джоба (#256/#265) как JobMeta: шов проверок. Читают её только
    через JobMeta -- сырые .get по ключам расходились умолчаниями. Отказ
    НЕ глотается (#581): пустая мета от молчащего Nomad значила бы
    «не роль» -- и ролевое имя уезжало на легаси-путь с проектным кредом.
    Громкая ошибка едет наружу и читается отказом (fail closed); нет
    джоба -- пустая мета, как у cluster._meta: «ничей» -- законный ответ
    Nomad, не сбой."""
    from ..common.domain import JobMeta
    from . import nomad
    job = nomad.get_job(name)
    if not job:
        return JobMeta(None, None)
    return JobMeta.from_job(job)


def _assigned_node(name):
    """Узел последней аллокации джоба, либо None: шов проверок.

    Отсутствие (нет аллокации, нет узла в сводке) -- None: молчание
    Nomad законно, калитка resolve_soul читает его как «не найдено».
    Сбой НЕ глотается (#581): причина едет наверх, и калитка возвращает
    found:False с ней -- общий текст скрыл бы, ЧТО именно отказало."""
    from . import nomad
    alloc = nomad.latest_alloc(name)
    if not alloc:
        return None
    node_id = alloc.get("NodeID")
    if not node_id:
        return None
    for summary in nomad.get_nodes():
        if summary.get("ID") == node_id:
            return summary.get("Name")
    return None


def _owner_profile(login):
    """Профиль владельца роли для ответа (#590): шов проверок."""
    return identity.profile(identity.server_provider(), login)


def role_gates(name, caller, reply, project, root=None, jobs=None,
               assigned=None):
    """Ворота выдачи кредов роли (#581/#603/#590) -> запись | {"error"}.

    Четыре проверки, все до прогона:
      1. вызывающий -- узел (субъект #603): анонимный легаси server.rpc
         зовут и люди проекта, ролевые факты им не положены. caller --
         полное машинное имя (node-<узел>): им же проверяется приватный
         префикс ответа, голое имя узла -- Nomad-сверка назначения;
      2. ответ -- приватный префикс ИМЕННО этого узла (valid_private_reply,
         #603): кред роли не едет в глобально читаемый _INBOX и не в
         чужой префикс;
      3. джоб назначен Nomad ИМЕННО этому узлу (свежая аллокация);
      4. тройка Meta джоба (#581) -- цельком и ровно привязке: роль,
         дайджест и инкарнация совпадают все три; переиспользованное
         имя (#579) держит новый грант, и by_job без инкарнации выбрал
         бы любую живую привязку мимо факта Nomad.
    by_job с инкарнацией приносит запись С её состоянием -- решение о
    неживой (отозвана, не подтверждена) выносит выдающий: кред не выдают.

    root -- корень хранилища душ (шов проверок); jobs(name) -> Meta и
    assigned(name) -> имя узла аллокации (швы проверок, по умолчанию --
    Nomad).

    Тройка Meta -- требование целиком (#581): (soul, soul_digest,
    soul_inc), и привязка обязана совпасть по ВСЕЙ тройке и проекту.
    by_job(None) выбрал бы любую живую привязку имени -- молча, мимо
    факта, который пишет в Nomad сам сервер; пустая Meta (Nomad молчит
    или битый джоб) -- громкий отказ, не «не роль». Проект -- из origin
    ФАКТИЧЕСКОЙ Meta: та же тройка на чужом origin -- запись чужого
    проекта, и имени джоба мало: тройка без origin -- сломанная запись
    новой роли. Легаси-мета (без тройки) живёт прежним путём."""
    from . import grants
    node = _node_of(caller)
    if node is None:
        return {"error": "role credentials are issued to the node the job "
                         "is assigned to: the anonymous legacy server.rpc "
                         "gets no role facts (#603)"}
    if not busnames.valid_private_reply(reply or "", caller):
        return {"error": f"role credentials ride the caller's own private "
                         f"reply subject, not {reply!r} (#603)"}
    meta = (jobs or _job_meta)(name)
    if not (meta.soul and meta.soul_digest and meta.soul_inc):
        raise ValueError(
            f"{name}: job Meta carries no whole role triple (soul, "
            f"soul_digest, soul_inc) -- the server writes it at "
            f"registration, and its absence is a broken job, not a "
            f"legacy one")
    if not meta.origin or meta.project != project:
        raise ValueError(
            f"{name}: the role triple rides the origin {meta.origin!r} "
            f"of project {meta.project!r}, not {project!r} -- the server "
            f"writes the origin at registration, and a triple without "
            f"its project's origin is a broken job")
    rec = grants.by_job(project, name, soul_inc=meta.soul_inc, root=root)
    if rec is None or not rec.get("active") or rec.get("revoked_at") \
            or rec.get("role") != meta.soul \
            or rec.get("soul_digest") != meta.soul_digest:
        raise ValueError(
            f"{name}: no live binding in project {project} for the Meta "
            f"triple (role {meta.soul}, digest {meta.soul_digest[:8]}, "
            f"incarnation {meta.soul_inc})")
    where = (assigned or _assigned_node)(name)
    if where != node:
        return {"error": f"{name} is assigned to {where or 'no node'}, "
                         f"not {node} (#603)"}
    return rec


def role_reply(rec, project, root=None):
    """Ролевая надстройка ответа bootstrap'а (#590): индивидуальный кред
    принципала и полезная нагрузка души. Прогон уже сыгран -- здесь
    только выдача; пароль едет одним полем bus.password, не в реестр
    и не в журнал."""
    import base64
    from . import grants
    principal = rec.get("principal")
    password = grants.password_of(principal, root=root)
    if not password:
        return {"error": f"{rec.get('job')}: the principal's password is "
                         f"cut (transport revocation, #579)"}
    got = grants.bundle(rec.get("soul_digest"), root=root)
    if got is None:
        return {"error": f"{rec.get('job')}: the pinned bundle "
                         f"{rec.get('soul_digest') and rec.get('soul_digest')[:8]}"
                         f" is missing from the store -- the role cannot "
                         f"start with an empty soul"}
    return {
        "bus": creds.bus_config(config.get("MOP_SERVER_LAN"),
                                config.get("MOP_NATS_PORT"), project,
                                password, user=principal),
        "soul": {"role": rec.get("role"), "soul_inc": rec.get("soul_inc"),
                 "soul_digest": rec.get("soul_digest"),
                 "manifest": got["manifest"],
                 "prompt": base64.b64encode(got["prompt"]).decode(),
                 "principal": principal,
                 "owner": _owner_profile(rec.get("owner"))}}


# ─── селекция секретов по роли (#592) ────────────────────────────────────
# Allowlist роли (soul.yaml, блок secrets) приходит из доверенного бандла
# роли, не из тела запроса: локально отредактированный YAML прав не даёт
# (SOUL.md, «секреты»). None -- легаси-джоб без роли: каталог целиком,
# как сегодня. Пустой список -- тоже приговор: роль без блока секретов
# не получает ничего сверх печённого в образ, но прогон всё равно
# играется -- плейбук вычищает прежний staging тела, иначе удалённый из
# роли секрет оставался бы жить в ~/.config/mop/project-secrets.
_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _allow_of(role):
    """Роль на входе селекции -> словарь allowlist {"files", "vars"}.

    Два входа, два уровня доверия, один выход. soul.Bundle -- бандл из
    доверенного хранения сервера (#581: хранилище бандлов по дайджесту):
    allowlist -- блок secrets разобранного манифеста, второго разбора
    soul.yaml на сервере нет. Словарь -- переходный внутренний вход шва
    (пока резолвер #581 не кладёт бандл в каждое обращение): его грамматию
    проверяет secrets_of, но сам он не «роль», а её тень -- по шине так
    приезжать не должно (#592: allowlist из тела запроса -- самовыданная
    роль)."""
    if isinstance(role, souls.Bundle):
        return {"files": list(role.manifest.secrets.files),
                "vars": list(role.manifest.secrets.vars)}
    return role


def _canonical_file_name(name):
    """Имя секретного файла, каноническое, без масок. Нормализация --
    валидатор, а не пропуск: 'a/../b' и './b' остаются собой и
    отказываются до хранилища; '*' -- маска, а не имя. Канонично и то,
    чего хранилище не меняет: оно само стрипит и нормализует (valid_name),
    и написание, отличающееся от его канонической формы (' b' против 'b'),
    выбрало бы чужой секрет -- молча другое имя не выбирается никогда."""
    if not isinstance(name, str) or any(w in name for w in ("*", "?", "[")):
        raise ValueError(f"{name!r}: a secret file is named exactly, in canonical form")
    try:
        stored = project_secrets.valid_name(name)
    except ValueError:
        stored = None
    if stored != name:
        raise ValueError(f"{name!r}: a secret file is named exactly, in canonical form")
    return stored


def secrets_of(root, project, allow):
    """Имена секрета, которые роли можно выдать -> (files, vars).

    Точные канонические имена, без масок: селекция не подбирает ни по
    префиксу, ни по нормализации. Побег из клона и чужая грамматика --
    ValueError до хранилища. Отсутствие запрошенного файла -- тоже
    ValueError: отказ слышит узел до прогона. Переменные здесь не
    читаются вовсе: их единственный слепок читает role_staging --
    валидация одним чтением, материализация тем же (рассинхрон двух
    чтений -- тихая пустая выдача)."""
    files, names = [], []
    for kind, sink, listed in (("file", files, allow.get("files") or []),
                               ("var", names, allow.get("vars") or [])):
        for name in listed:
            if kind == "file":
                sink.append(_canonical_file_name(name))
            elif not isinstance(name, str) or not _VAR_NAME.match(name):
                raise ValueError(f"{name!r}: a secret variable is VAR=VALUE's name")
            else:
                sink.append(name)
        if kind == "file":
            listed = [f["name"] for f in project_secrets.list_files(root, project)]
            missing = [n for n in sink if n not in listed]
            if missing:
                raise ValueError(f"no file secret {missing[0]!r} in project {project}")
    return files, names


def _no_links(base, rel, what):
    """Ни одной символьной ссылки по пути base/rel, base включительно.

    Листовая проверка пропускала ссылку-каталог проекта: хранилище
    читалось целиком по ссылке наружу, и «секрет» приходил из чужого
    файла. Проверяется каждая ступень: ссылка где угодно на пути --
    отказ до чтения значения."""
    probe = base
    if os.path.islink(probe):
        raise ValueError(f"{what}: the secret store path must not contain symlinks")
    for piece in (rel.split(os.sep) if rel else []):
        probe = os.path.join(probe, piece)
        if os.path.islink(probe):
            raise ValueError(f"{what}: the secret store path must not contain symlinks")


@contextlib.contextmanager
def role_staging(root, project, allow):
    """Приватный временный каталог секрета роли в формате хранилища
    (files/ + vars.env) -> путь. Существующему плейбуку всё равно, откуда
    каталог: mop_secrets_dir копирует его целиком (deploy/bootstrap.yml).

    Ссылки не переходят нигде: от корня хранилища до vars.env включительно
    -- иначе значением секрета становился бы файл по ссылке, которого
    оператор не клал. Переменные читаются одним слепком и материализуются
    из него: рассинхрон двух чтений не выдаёт пустой успех вместо
    запрошенного. Каталог создаётся заново на каждый старт (mkdtemp),
    одновременные старты ролей друг друга не видят; хранилище не
    мутируется. Убирается на каждом выходе, включая исключение, и сбой
    уборки -- слышен, а не проглочен: за каталогом секретов нельзя
    молча оставить след."""
    files, names = secrets_of(root, project, _allow_of(allow))
    base = project_secrets.project_dir(root, project)
    _no_links(base, "", f"store of {project}")
    stage = tempfile.mkdtemp(prefix=f"mop-secrets-{project}-")
    os.chmod(stage, 0o700)
    try:
        for name in files:
            src = os.path.join(base, project_secrets.FILES, name)
            _no_links(base, os.path.join(project_secrets.FILES, name), name)
            if not stat.S_ISREG(os.stat(src).st_mode):
                raise ValueError(f"{name}: a secret file is a regular file, not a link")
            dst = os.path.join(stage, project_secrets.FILES, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o600)
        if names:
            _no_links(base, project_secrets.VARS, project_secrets.VARS)
            # Один слепок на прогон: рассинхрон двух чтений молча выдал бы
            # пустой успех вместо запрошенного -- это утечка права, а не
            # «секрета нет».
            snapshot = project_secrets._vars(root, project)
            values = {k: snapshot.get(k) for k in names}
            missing = [k for k in names if k not in snapshot]
            if missing:
                raise ValueError(f"no var secret {missing[0]!r} in project {project}")
            fsutil.write_private(os.path.join(stage, project_secrets.VARS),
                                 fsutil.write_kv(values))
        yield stage
    finally:
        shutil.rmtree(stage)


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


PROGRESS_BYTES = 65536
PROGRESS_LINES = 500
_PROGRESS = re.compile(r"^(?:PLAY|TASK) \[(.*?)\](?: \*+)?$|^((?:ok|changed|skipping|failed|fatal): \[[^\]\r\n]+\])")


def _progress_paths(root, name):
    return (os.path.join(root, f"{name}-progress.log"),
            os.path.join(root, f"{name}-progress.json"))


def read_progress(root, name, alloc):
    """Ход текущей аллокации: только задачи и их статусы, не command/stdout.
    Вывод доступен во время прогона; прежняя аллокация не выдаётся за новую."""
    log, meta = _progress_paths(root, name)
    empty = {"lines": [], "running": False, "alloc": None, "truncated": False}
    try:
        with open(meta, encoding="utf-8") as stream:
            record = json.load(stream)
    except FileNotFoundError:
        return empty
    if record.get("alloc") != alloc:
        return empty
    try:
        with open(log, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - PROGRESS_BYTES))
            text = stream.read(PROGRESS_BYTES).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return empty
    lines = []
    for line in text.splitlines():
        matched = _PROGRESS.match(line)
        if matched:
            lines.append((matched.group(2) or line.rstrip(" *"))[:1024])
    return {"lines": lines[-PROGRESS_LINES:], "running": record.get("running") is True,
            "alloc": alloc, "at": record.get("at"),
            "truncated": size > PROGRESS_BYTES or len(lines) > PROGRESS_LINES}


# ─── сервер ──────────────────────────────────────────────────────────────
def play(req, project, role=None, bundle=None):
    """Сыграть bootstrap проекта в песочницу папета. -> {ok, played, ...}.

    Нет файла — ok без прогона: большинству проектов хватает общего.
    bundle — soul.Bundle из доверенного хранения сервера (#592, #580):
    allowlist секретов — блок secrets его манифеста, не тело запроса и
    не локальный YAML. role — переходный внутренний вход того же шва
    (голый allowlist-словарь): его переводит _allow_of, наружу он не
    виден; оба заданы -- роль у бандла. None и тот и другой -- легаси-джоб
    без роли, каталог секретов целиком, как сегодня. Пустой allowlist
    тоже играет: прогон вычищает прежний staging тела, «нечего играть»
    оставило бы старое жить в ~/.config/mop/project-secrets."""
    # workspace -- файл папета (#133): копия того, кто стартует, а не проекта.
    tasks, of_vars = files_of(ROOT, req["name"])
    store_dir = project_secrets.project_dir(project_secrets.ROOT, project)
    store_dir = store_dir if os.path.isdir(store_dir) else None
    picked = bundle if bundle is not None else role
    if picked is None:
        if not needs_play(tasks, store_dir):
            return {"ok": True, "played": False, "text": f"no bootstrap for {project}"}
        return _run(req, project, tasks, of_vars, store_dir)
    # Роль: прогон обязателен всегда, даже с пустым allowlist и без
    # хранилища. «На сервере нет» не доказывает «в теле чисто»: прежний
    # staging тела вычищает только прогон, молчаливый ok оставил бы
    # удалённый из роли секрет жить в ~/.config/mop/project-secrets.
    try:
        with role_staging(project_secrets.ROOT, project, picked) as staged:
            return _run(req, project, tasks, of_vars, staged)
    except ValueError as e:
        # Отказ до прогона: узел читает его как отказ старта, и модель не
        # поднимается под видом «секреты приехали» (readiness #592).
        return {"ok": False, "played": False, "error": f"secrets of the role: {e}"}


def _run(req, project, tasks, of_vars, secrets):
    """Один прогон плейбука с готовым каталогом секретов."""
    name = req["name"]
    cmd = argv(PLAYBOOK, req.get("address") or "", config.get("MOP_USER"), KEY,
               playvars.playbook_vars(), project, name, driver.clone_dir(name),
               tasks, of_vars, secrets)
    log, meta = _progress_paths(ROOT, name)
    # Роль играет и без workspace (#592), и ROOT до этого прогона мог никто
    # не создавать: прогресс (#612) пишется в существующий каталог, как
    # всякий другой писатель здесь.
    fsutil.make_private_dir(ROOT)
    fsutil.write_private(log, "")
    progress = {"alloc": req.get("alloc"), "running": True, "at": req.get("started_at")}
    fsutil.write_private(meta, json.dumps(progress))
    t0 = time.time()
    try:
        with open(log, "w+", encoding="utf-8") as output:
            r = subprocess.run(cmd, stdout=output, stderr=subprocess.STDOUT,
                               text=True, timeout=TIMEOUT - 10,
                               env={**os.environ, "PYTHONUNBUFFERED": "1", "ANSIBLE_FORCE_COLOR": "0",
                                    "ANSIBLE_CONFIG": os.path.join(config.PROJECT, "ansible.cfg")})
            output.seek(0)
            text = output.read()
    except subprocess.TimeoutExpired:
        return {"ok": False, "played": True, "rc": None,
                "seconds": round(time.time() - t0, 1),
                "tail": f"bootstrap of {name} did not finish in {TIMEOUT - 10}s — "
                        f"keep bootstrap.yaml in seconds, bake the heavy part into sandbox.yaml"}
    finally:
        fsutil.write_private(meta, json.dumps({**progress, "running": False}))
    return outcome(r.returncode, text, time.time() - t0)


# Строка провала задачи ansible: `fatal: [хост]: FAILED! => {...}` или, у
# элемента цикла, `failed: [хост] (item=…) => {...}`. `failed=1` в PLAY
# RECAP -- не она: там нет ни двоеточия, ни хоста в скобках.
_FAILED_LINE = re.compile(r"^(?:fatal|failed): \[")
_TASK = re.compile(r"^TASK \[(.+?)\] \**$")
MESSAGE_MAX = 200


def failed_task(output):
    """Вывод ansible-playbook -> (задача, сообщение) | (None, None) (#333).

    Задача -- последний заголовок `TASK [...]` перед первой строкой провала;
    сообщение -- из результата `=> {...}` (_result_message: у команды -- её
    вывод, #344), если разбирается, иначе сама строка,
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
                message = _result_message(got) if isinstance(got, dict) else None
            except ValueError:
                pass
        message = message or " ".join(line.split())[:MESSAGE_MAX]
        return task, message
    return None, None


def _result_message(result):
    """Причина провала из результата задачи ansible -> строка | None (#344).

    У command/shell msg всегда «non-zero return code»: причина -- в выводе.
    Есть rc и вывод -- последняя непустая строка stderr (иначе stdout) и код
    выхода; вывода нет -- msg. Одна строка, до MESSAGE_MAX, код не срезается."""
    msg = result.get("msg")
    if result.get("rc") is not None:
        for lines in (result.get("stderr_lines"), result.get("stdout_lines")):
            said = [" ".join(str(l).split()) for l in lines or [] if str(l).strip()]
            if said:
                code = f" (rc {result['rc']})"
                return said[-1][:MESSAGE_MAX - len(code)] + code
    return " ".join(str(msg).split())[:MESSAGE_MAX] if msg else None


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
    главнее: кред к упавшему bootstrap'у не приклеиваем. Упавший прогон
    несёт ok False, а не error (#349): прежде кред клеился и к нему, и
    journal() печатал его вместе с ответом. Упавший старт шину не открывает."""
    if out.get("error") or not out.get("ok"):
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


def resolve_soul(project, req, root=None, assigned=None):
    """Глагол resolve (#579/#581/#584): единый wire-вердикт. Два режима:

    -- с принципалом -- делегированное действие {principal, service,
       verb_name, target?}: вердикт ИСТОЧНИКА и, при названной цели,
       вложенный блок target, один атомарный снимок реестра;
    -- без принципала -- режим только-цели (#584): узел читает
       состояние привязки ДО того, как процесс роли сможет действовать.
       Это data-only: found/job/project/owner/role/soul_inc/soul_digest/
       active/revoked_at/receives_dispatch, пароля нет. Отсутствие
       принципала НЕ становится молча анонимным привилегированным
       действием -- только этот явный режим и только узлу.

    Здесь, а не в cluster.rpc (#579): спросить может только узел, кластеру
    -- та же библиотека in-process. Проект -- из субъекта, не из тела:
    субъект проверен правами NATS. Проектный человек тоже публикует в
    легаси server.rpc (#603), поэтому анонимный вызов -- отказ без фактов:
    вердикт о принципале едет только узловому вызывающему, которого назвал
    субъект (_caller каркаса, не тело). root -- шов проверок: реальный
    вызов читает хранилище сервера. Отказ -- тоже ответ: fail closed.

    Режим только-цели сверяет НАЗНАЧЕНИЕ (#581): узел спрашивает о чужой
    цели -- сервер смотрит узел последней аллокации джоба (assigned --
    шов проверок) и не раскрывает состояние узлу-не-хозяину: авторитет
    старта -- Nomad, и чужое состояние соседнему узлу не нужно. Сверка
    обязана ОТВЕТИТЬ именем узла (fail closed): молчаливый Nomad и сбой
    сверки -- «не найдено», с причиной сбоя -- узел-хозяин переспросит,
    а соседний не прочитает. Гейт накрывает ВСЕ найденные состояния:
    отозванная или неподтверждённая цель соседнему узлу -- тоже факт
    проекта, не «мертва -- значит можно»."""
    from . import grants
    if not _node_caller(req):
        return {"allow": False,
                "reason": "resolve serves node callers only: the anonymous "
                          "legacy server.rpc carries no grant facts (#603)"}
    if not req.get("principal"):
        out = grants.describe_target(project, req.get("target") or "",
                                     soul_inc=req.get("soul_inc"), root=root)
        if out.get("found"):
            try:
                where = (assigned or _assigned_node)(out.get("job"))
            except Exception as e:
                return {"found": False,
                        "reason": f"{out.get('job')}: the assignment lookup "
                                  f"failed: {e}"}
            if not where:
                return {"found": False,
                        "reason": f"{out.get('job')}: Nomad names no node "
                                  f"for the job -- the assignment decides "
                                  f"who may read the state"}
            if _node_of(req.get("_caller") or "") != where:
                return {"found": False}
        return out
    return grants.resolve(req.get("principal"),
                          req.get("service") or "node",
                          req.get("verb_name") or "", target=req.get("target"),
                          root=root)


def _node_caller(req):
    """Узловой вызывающий из доверенного поля каркаса, либо None.

    _caller ставит каркас из субъекта (#207): тело его не подделывает.
    Узел -- имя машинного пользователя node-<имя>; человек и принципал
    сюда не попадают."""
    who = req.get("_caller") or ""
    return who if who.startswith(busnames.NODE_PREFIX) else None


def _later(delay, fn):
    """Отложенный вызов своим потоком; шов для проверок (#345)."""
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


def ask_give_up(name):
    """Попросить сервис кластера остановить сдавшийся джоб (#345). Глагол
    оператора: Nomad -- у него. Не вышло -- следующий
    подъём того же ключа ответит gave_up без прогона и попросит снова."""
    def ask():
        try:
            bus.ask_cluster("give_up", timeout=bus.TIMEOUT, project=bus.ADMIN, name=name)
        except Exception:
            pass
    _later(GIVE_UP_GRACE, ask)


def gave_up_reply(rec):
    """Ответ узлу по сдавшемуся ключу без прогона (#345): отказ с той же
    задачей и сообщением, что у последнего провала."""
    return {"ok": False, "played": False, "gave_up": True, "rc": rec.get("rc"),
            "task": rec.get("task"), "message": rec.get("message"),
            "failures": rec.get("failures"), "seconds": None,
            "tail": f"not replayed: bootstrap gave up after {rec.get('failures')} "
                    f"failed plays of this workspace"}


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
            # Ролевой джоб -- выдают индивидуальный кред принципала, и
            # ворота строже (#581/#603): анонимный легаси-путь ролевому
            # имени -- отказ без фактов, узловому -- проверка назначения
            # и приватного ответа, ДО прогона. Прогон тот же: секреты и
            # рамка sandbox едут и роли. Легаси-имя (привязки нет) живёт
            # прежним путём с проектным кредом (переход #603).
            from . import grants
            soul_root = _soul_root()
            # Классификация -- по ФАКТИЧЕСКОЙ Meta джоба (#581): тройка
            # роли в Nomad делает джоб ролевым, что бы ни держал реестр
            # (привязка стёрта/битая -- отказ в воротах, не легаси-путь);
            # Архив старой роли не назначает роль новому legacy-джобу:
            # исключение требует зарегистрированный origin того же проекта.
            meta = _job_meta(req.get("name") or "")
            legacy = (not meta.soul and bool(meta.origin)
                      and driver.project_of(meta.origin) == project)
            is_role = bool(meta.soul and meta.soul_digest and meta.soul_inc) \
                or grants.binding_named(project, req.get("name") or "",
                                        root=soul_root, include_revoked=not legacy)
            if is_role:
                bound = role_gates(req.get("name") or "",
                                   req.get("_caller") or "",
                                   req.get("_reply"), project, root=soul_root,
                                   jobs=lambda name: meta,
                                   assigned=_assigned_node)
                if bound.get("error"):
                    return bound
            # `put` снят (#133): workspace кладут глаголы жизненного цикла
            # папета у сервиса кластера, а в этот субъект пишут и узлы.
            # Сдавшийся ключ (#345) -- без прогона: ansible не переигрывается,
            # а стоп просится снова (прежний мог не дойти).
            prev = read_result(ROOT, req["name"]) or {}
            if prev.get("gave_up") and prev.get("key") == workspace_key(ROOT, req["name"]):
                ask_give_up(req["name"])
                return gave_up_reply(prev)
            # Роль играет с закреплённым бандлом (#592/#581): allowlist
            # секретов -- из доверенного хранения сервера, не из тела.
            pinned = None
            if is_role:
                pinned = grants.load_bundle(bound.get("soul_digest"),
                                            root=soul_root)
                if pinned is None:
                    return {"error": f"{req.get('name')}: the pinned bundle "
                                     f"is unreadable -- refusing to play "
                                     f"the role with the whole store"}
            out = play(req, project, bundle=pinned) if is_role \
                else play(req, project)
            # Итог -- рядом с отправленным (#334): `mop update` ждёт его. Не
            # записался -- подсказка пропала, а прогон и кред важнее.
            try:
                rec = note_result(ROOT, req["name"], out)
            except OSError:
                rec = {}
            if rec.get("gave_up"):
                ask_give_up(req["name"])
                return {**out, "gave_up": True, "failures": rec["failures"]}
            if is_role:
                # Кред роли едет тем же ответом, что и легаси-кред папета:
                # разговора два не нужно. Индивидуальный принципал вместо
                # проектного -- ролевая надстройка (#590), отказ -- отказ.
                # Только при успешном прогоне (#590): упавший ansible --
                # узел без рабочего workspace, и пароль принципала с душой
                # ему не выдаются; причина провала сохраняется как есть.
                if not out.get("ok"):
                    return out
                # Прогон идёт минуты (#581): привязку проверяет и ПОСЛЕ
                # него -- отзыв, приехавший за время ansible, не выдаёт
                # кредов уже поднятому телу.
                fresh = grants.binding(bound.get("principal"),
                                       root=soul_root)
                if not fresh or not fresh.get("active") \
                        or fresh.get("revoked_at"):
                    return {"error": f"{req.get('name')}: the binding was "
                                     f"revoked while the play ran -- no "
                                     f"credentials"}
                role = role_reply(bound, project, root=soul_root)
                if role.get("error"):
                    return role
                return {**out, **role}
            # Кред папета едет тем же ответом: узел уже позвал нас, и
            # второго разговора ради одного файла не нужно.
            return with_creds(out, puppet_creds(project), project)
        if verb == "identity":
            return owner_identity(req)
        if verb == "resolve":
            return resolve_soul(project, req)
        # `put` снят (#133): workspace кладут глаголы жизненного цикла
        # папета у сервиса кластера, а в этот субъект пишут и узлы.
        return {"error": f"no such verb {verb}; available: ping, bootstrap, "
                         f"identity, resolve"}
    except Exception as e:
        return {"error": f"{verb}: {e}"}


def journal(project, req, out):
    """Строки журнала на один ответ; у проваленного прогона -- ещё и его хвост.

    Только из известных полей (#349): словарь ответа сюда не форматируется
    никогда. Упавший прогон error не несёт, и прежде строка печатала ответ
    целиком -- вместе с `bus`, паролем шины проекта, в journald сервера."""
    if out.get("error"):
        what = out["error"]
    elif out.get("ok") is False:
        # Сообщение #344 уже кончается «(rc N)» -- код не повторяем.
        what = "failed" + (f" at task «{out['task']}»" if out.get("task") else "") \
            + (f": {out['message']}" if out.get("message") else "") \
            + (f" (rc {out['rc']})" if out.get("rc") is not None
               and f"(rc {out['rc']})" not in (out.get("message") or "") else "") \
            + (f"; gave up after {out.get('failures')} failures" if out.get("gave_up") else "")
    else:
        what = "ok"
    lines = [f"{project}.{req.get('verb')} {req.get('name', '')}: {what}"
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
    тот же каталог, что у дашборда (mop/common/creds.py).

    Семейство субъектов (#581/#603): анонимный легаси mop.*.server.rpc и
    квалифицированный хвост mop.*.server.rpc.node-<узел>, на котором узловой
    резолвер (#584) просит от своего имени. Одно определение с ACL
    (busnames.server_family): подписка мимо него ждала бы таймаут молчания."""
    subj = busnames.server_family()
    await service.serve("mop-bootstrap", subj, answer, log, journal,
                        lambda: banner(", ".join(subj), ROOT, _puppets_here()))


# ─── мастер ──────────────────────────────────────────────────────────────
