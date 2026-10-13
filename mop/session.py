"""Локальная IPC-поверхность сессии claude code: файлы сессий и канал сообщений.

Один модуль на два знания, потому что меняются они вместе — это один и тот же
контракт claude, описанный в docs/CHANNEL.md: где лежит файл сессии, что в нём, где
её сокет и как в этот сокет говорить.

Важно: модуль намеренно standalone — только стандартная библиотека и ни одного
импорта из пакета mop. Он работает в двух режимах:

  * импортом на управляющей машине (командлеты bin/, MCP-сервер);
  * исходником, уехавшим внутрь аллокации Nomad: сокет папета host-local, с
    управляющей машины к нему не подключиться, поэтому отправитель и пробник
    едут на узел и исполняются там как `python3 - probe|send …`.

Второй режим и есть причина запрета на импорты: на узел уезжает один файл.
"""
import base64
import glob
import hashlib
import json
import re
import stat
import struct
import unicodedata
import os
import socket
import sys
import threading
import time
import uuid

SESSIONS = os.path.expanduser("~/.claude/sessions")
# Каталог состояния тела. Пакет зовёт его paths.STATE, но отсюда paths не
# импортировать: этот файл едет в тело исходником, только stdlib. Что оба
# называют одно, держит tests/session.py (#322).
STATE = os.path.expanduser("~/.local/state/mop")
# Записи исхода хода (#222): их пишет хук claude, читает `state`.
TURNS = os.path.join(STATE, "turns")
# Записи модели сессии (#453): их пишут statusline и PostModelSwitch, читает
# `state`. Только {model, at} -- сырой JSON statusline несут cost и контекст,
# это расход и внутренности сессии, а не состояние папета.
MODELS = os.path.join(STATE, "models")
CONNECT_TIMEOUT = 5
PRIORITIES = ("now", "next", "later")
MODES = ("bypass", "prompting")


class Ambiguous(LookupError):
    """Под целью подходит несколько сессий. Отдельный тип, потому что «здесь
    такой нет» и «их тут несколько» ведут в разные стороны: первое разрешает
    искать адресата дальше (на шине), второе обязано остановить — иначе
    однажды напишем не тому."""


# ─── файлы сессий ────────────────────────────────────────────────────────
def sessions():
    """Все читаемые файлы сессий, у которых есть адрес инбокса.

    Мёртвые тоже: живость решает сокет, а не файл — файл переживает грязную
    смерть процесса и остаётся лежать с протухшим status."""
    out = []
    for f in sorted(glob.glob(f"{SESSIONS}/*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        if d.get("messagingSocketPath"):
            out.append(d)
    return out


def socket_alive(path):
    """Единственная достоверная проба живости: слушает ли кто-то сокет."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _in_cwd(cwd):
    """Файлы сессий этого каталога, свежие первыми. Сравнение по realpath:
    сессия и спрашивающий могут называть один каталог через разные ссылки."""
    target = os.path.realpath(cwd)
    mine = [d for d in sessions() if os.path.realpath(d.get("cwd") or "") == target]
    return sorted(mine, key=lambda d: d.get("statusUpdatedAt", 0), reverse=True)


def session_for_cwd(cwd):
    """Сессия, работающая в этом каталоге; самая свежая, если их несколько."""
    mine = _in_cwd(cwd)
    return mine[0] if mine else None


def live_session_for_cwd(cwd):
    """Самая свежая живая сессия каталога.

    Файлов на один каталог накапливается много (у одного папета их было
    18): каждая умершая сессия оставляет свой. Брать просто самую свежую
    нельзя — она может быть трупом; спрашиваем сокет и спускаемся по времени,
    пока кто-нибудь не отзовётся."""
    for d in _in_cwd(cwd):
        if socket_alive(d["messagingSocketPath"]):
            return d
    return None


# ─── история: только файл текущей живой идентичности (#597) ──────────────
# 64 KiB исходника дают менее 400 KiB при худшем JSON escaping. Дополнительный
# предел сериализованной страницы оставляет запас под конверт шины (900 KB).
HISTORY_CHUNK = 64 * 1024
HISTORY_RECORD = 32 * 1024
HISTORY_WIRE = 256 * 1024
HISTORY_CURSOR = 2048
_HISTORY_COMPONENT = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_HISTORY_SID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _history_json(raw):
    """Строгий JSON: дубликаты ключей и нечисловые числа не являются объектом."""
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out

    def constant(_value):
        raise ValueError("non-finite JSON number")

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        out = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(out, dict):
            raise ValueError("JSON record is not an object")
        # Проверяет также бесконечность от 1e999 и одинокие UTF-16 surrogates.
        json.dumps(out, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return out
    except (ValueError, UnicodeError, RecursionError) as e:
        raise ValueError("incompatible history JSON object") from e


def _history_open(root, parts=(), directory=False):
    """Открыть от корня без единого symlink, включая промежуточные каталоги.

    dir_fd и O_NOFOLLOW не оставляют окна между realpath и open. Возвращённый
    fd закрывает вызывающий. Обычный файл обязателен: FIFO мог бы повесить RPC.
    """
    if not isinstance(root, str) or not os.path.isabs(root):
        raise ValueError("unsafe history config directory")
    components = [p for p in root.split(os.sep) if p] + list(parts)
    if any(not isinstance(p, str) or p in (".", "..") or not p
           or os.sep in p or "\x00" in p for p in components):
        raise ValueError("unsafe history path component")
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("history confinement is unavailable on this platform")
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, part in enumerate(components):
            is_dir = i < len(components) - 1 or directory
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if is_dir:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        st = os.fstat(fd)
        if not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)):
            raise ValueError("history path is not a regular file or directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _history_config(env):
    """Корень из окружения процесса, не из RPC и не из путей транскрипта."""
    if not isinstance(env, dict):
        raise LookupError("live session environment is unknown")
    root = env.get("CLAUDE_CONFIG_DIR")
    if not root:
        home = env.get("HOME")
        if not isinstance(home, str) or not os.path.isabs(home):
            raise LookupError("live session config home is unknown")
        root = os.path.join(home, ".claude")
    if not isinstance(root, str) or not os.path.isabs(root) or "\x00" in root:
        raise ValueError("unsafe live session config home")
    return unicodedata.normalize("NFC", root)


def _history_project(cwd, env):
    """Правило каталога Claude Code 2.1.296: override только с config home.

    Обычный slug заменяет каждый не-ASCII-alnum дефисом. Длинный slug требует
    версионного хеша Claude, пока он не принят живым контрактом — отказ, не
    попытка подобрать соседний файл. Невалидный override Claude игнорирует.
    """
    custom = env.get("CLAUDE_CODE_PROJECT_DIR_NAME") if env.get("CLAUDE_CONFIG_DIR") else None
    if (isinstance(custom, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", custom)
            and not re.fullmatch(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])", custom, re.I)):
        return custom
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    if len(slug) > 200:
        raise ValueError("long Claude project slug is not supported by history")
    return slug


def _history_process(pid):
    """Linux identity: boot id + start ticks; окружение и cwd именно PID.

    Чтения stat до и после не позволяют принять окружение переиспользованного
    PID. Нет /proc или доступа — нет доказанной живой идентичности.
    """
    if type(pid) is not int or pid <= 0:
        raise LookupError("live session PID is unknown")
    root = f"/proc/{pid}"

    def start():
        with open(root + "/stat") as f:
            text = f.read(8192)
        fields = text[text.rfind(")") + 2:].split()
        if len(fields) < 20 or fields[0] in ("Z", "X") or not fields[19].isdigit():
            raise LookupError("live session process start is unknown")
        return fields[19]

    try:
        before = start()
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot = f.read(128).strip()
        if not re.fullmatch(r"[0-9a-f-]{36}", boot):
            raise LookupError("live session boot identity is unknown")
        with open(root + "/environ", "rb") as f:
            raw = f.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or not raw.endswith(b"\x00"):
            raise LookupError("live session environment is unknown")
        env = {}
        for item in raw[:-1].split(b"\x00"):
            key, sep, value = item.partition(b"=")
            if not sep:
                raise LookupError("live session environment is unknown")
            key, value = key.decode("utf-8"), value.decode("utf-8")
            if key in env:
                raise LookupError("live session environment is ambiguous")
            env[key] = value
        cwd = os.readlink(root + "/cwd")
        uid = os.stat(root).st_uid
        if start() != before or not os.path.isdir(cwd):
            raise LookupError("live session process changed")
        return {"start": boot + ":" + before, "proc_start": before, "env": env,
                "cwd": os.path.realpath(cwd), "uid": uid}
    except (OSError, UnicodeError, IndexError) as e:
        raise LookupError("live session process identity is unavailable") from e


def _history_peer(path):
    """Проба без кадров, attach и inject: SO_PEERCRED связывает сокет с PID."""
    if not isinstance(path, str) or not os.path.isabs(path) or not hasattr(socket, "SO_PEERCRED"):
        raise LookupError("live session peer identity is unavailable")
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(before.st_mode):
        raise ValueError("unsafe live session socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.connect(path)
        except (ConnectionRefusedError, FileNotFoundError):
            return None
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", creds)
        after = os.lstat(path)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise LookupError("live session socket changed")
        return pid, uid, [after.st_dev, after.st_ino]


def _history_identity(cwd):
    """Ровно один живой кандидат, совпавшие cwd, config home и peer PID.

    Реестр discovery выбирается окружением тела. Отличающийся config home
    живого процесса не разрешает искать произвольный путь — это отказ.
    """
    target = os.path.realpath(cwd)
    config = _history_config(dict(os.environ))
    directory = _history_open(config, ("sessions",), directory=True)
    hits = []
    try:
        names = sorted(os.listdir(directory))
    finally:
        os.close(directory)
    for name in names:
        if not name.endswith(".json"):
            continue
        fd = _history_open(config, ("sessions", name))
        with os.fdopen(fd, "rb") as f:
            raw = f.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            continue
        try:
            d = _history_json(raw)
        except ValueError:
            # Discovery содержит остатки и незаконченные записи других сессий.
            # Это ещё не кандидат; fences живого endpoint ниже не ослабляются.
            continue
        if not isinstance(d.get("cwd"), str) or not os.path.isabs(d["cwd"]) or "\x00" in d["cwd"]:
            continue
        if os.path.realpath(d["cwd"]) != target:
            continue
        pid, sid, path = d.get("pid"), d.get("sessionId"), d.get("messagingSocketPath")
        if type(pid) is not int or pid <= 0 or not isinstance(sid, str) or not _HISTORY_SID.fullmatch(sid):
            continue
        if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
            continue
        peer = _history_peer(path)
        if peer is None:
            continue
        process = _history_process(pid)
        if (not isinstance(process, dict) or not isinstance(process.get("start"), str)
                or not process["start"] or not isinstance(process.get("env"), dict)
                or process.get("cwd") != target or type(process.get("uid")) is not int):
            raise LookupError("live session process identity is unknown")
        if peer[0] != pid or peer[1] != process["uid"] or peer[1] != os.getuid():
            raise LookupError("live session socket peer does not match registry")
        if _history_config(process["env"]) != config:
            raise LookupError("live session config home does not match discovery")
        # Опубликованный key связывает остаток реестра с process-start. Сам
        # peerToken не нужен чтению, не отправляется и не выходит в ответ.
        key_hash = _history_digest(os.path.realpath(path).encode())
        key_fd = _history_open(config, ("sessions", f"{pid}.{key_hash}.key"))
        with os.fdopen(key_fd, "rb") as f:
            key_raw = f.read(4097)
        if len(key_raw) > 4096:
            raise ValueError("session identity key is too large")
        key = _history_json(key_raw)
        proc_start = process.get("proc_start")
        if (not isinstance(proc_start, str) or not re.fullmatch(r"[0-9]{1,32}", proc_start)
                or key.get("procStart") != proc_start):
            raise LookupError("session process-start key is unknown or stale")
        hits.append({"session_id": sid, "pid": pid, "start": process["start"],
                     "config": config, "project_dir": _history_project(target, process["env"]),
                     "cwd": target, "socket": peer[2]})
    if not hits:
        raise LookupError("no proven live Claude session for history")
    if len(hits) != 1:
        raise Ambiguous("several live Claude session candidates for history")
    return hits[0]


def _history_digest(data):
    return hashlib.sha256(data).hexdigest()


def _history_cursor(cursor):
    """Курсор — позиция, не полномочие: ни одного пути, ограниченная схема."""
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not 0 < len(cursor) <= HISTORY_CURSOR or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        raise ValueError("invalid history cursor")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        d = _history_json(raw)
    except (ValueError, UnicodeError) as e:
        raise ValueError("invalid history cursor") from e
    keys = {"v", "epoch", "base", "file", "offset", "size", "anchor", "head", "head_bytes", "skip", "mtime", "ctime"}
    if set(d) != keys or type(d["v"]) is not int or d["v"] != 1 or type(d["skip"]) is not bool:
        raise ValueError("invalid history cursor schema")
    for key in ("epoch", "base", "anchor", "head"):
        if not isinstance(d[key], str) or not re.fullmatch(r"[0-9a-f]{64}", d[key]):
            raise ValueError("invalid history cursor identity")
    for key in ("offset", "size", "head_bytes", "mtime", "ctime"):
        if type(d[key]) is not int or not 0 <= d[key] <= 2**63 - 1:
            raise ValueError("invalid history cursor offset")
    if d["offset"] > d["size"] or d["head_bytes"] > min(64, d["size"]):
        raise ValueError("invalid history cursor bounds")
    if not isinstance(d["file"], list) or len(d["file"]) != 2 or any(type(n) is not int or n < 0 for n in d["file"]):
        raise ValueError("invalid history cursor file identity")
    return d


def _history_bytes(fd, offset, length):
    return os.pread(fd, length, offset)


def _history_anchor(fd, offset):
    return _history_digest(_history_bytes(fd, max(0, offset - 64), min(64, offset)))


def _history_page(fd, offset, size, skip):
    """Одна ограниченная byte-страница; незаконченная обычная строка остаётся.

    Слишком длинная строка пропускается частями с явным notice и продвижением.
    Не пытаемся разобрать её куски или приписать ей состояние инструмента.
    """
    raw = _history_bytes(fd, offset, min(HISTORY_CHUNK, size - offset))
    records, used, pos, partial = [], 0, 0, False
    while pos < len(raw):
        end = raw.find(b"\n", pos)
        complete = end != -1
        stop = end + 1 if complete else len(raw)
        line = raw[pos:stop]
        if not skip and not complete and len(line) <= HISTORY_RECORD:
            partial = True
            break
        if skip or len(line) > HISTORY_RECORD:
            record = {"type": "mop_history_notice", "reason": "record_too_large",
                      "offset": offset + pos, "skipped_bytes": len(line), "complete": complete}
        else:
            try:
                record = _history_json(line)
            except ValueError:
                # Завершённая запись отображения не исполняется: сообщаем пропуск,
                # не выдавая исходник или текст исключения. Cursor идёт дальше.
                record = {"type": "mop_notice", "reason": "invalid_record",
                          "offset": offset + pos, "skipped_bytes": len(line), "complete": True}
        length = len(json.dumps(record, ensure_ascii=True, allow_nan=False).encode())
        if length > HISTORY_WIRE - 4096:
            record = {"type": "mop_history_notice", "reason": "record_too_large",
                      "offset": offset + pos, "skipped_bytes": len(line), "complete": complete}
            length = len(json.dumps(record).encode())
        if used + length + 2 > HISTORY_WIRE - 4096:
            break
        records.append(record)
        used += length + 2
        pos = stop
        skip = not complete
        partial = skip
    new_offset = offset + pos
    more = offset + len(raw) < size or (new_offset < size and not (partial and not skip))
    return records, new_offset, skip, partial, more, raw


def history(cwd, cursor=None):
    """Транскрипт живой сессии: страницы объектов, без логов и публичных событий.

    Cursor позволяет повторить страницу и продолжить после reconnect без
    повторной выдачи уже прочитанных строк. Смена процесса/сессии/файла или
    нарушение byte fence начинают новую эпоху с reset. Смена во время чтения
    закрывает ответ отказом: старые records не должны выйти наружу.
    """
    previous = _history_cursor(cursor)
    identity = _history_identity(cwd)
    sid, project = identity.get("session_id"), identity.get("project_dir")
    if (not isinstance(sid, str) or not _HISTORY_SID.fullmatch(sid)
            or not isinstance(project, str) or not _HISTORY_COMPONENT.fullmatch(project)):
        raise ValueError("unsafe history session or project path")
    parts = ("projects", project, sid + ".jsonl")
    base = _history_digest(json.dumps(identity, sort_keys=True).encode())
    try:
        fd = _history_open(identity["config"], parts)
    except FileNotFoundError:
        # До первого transcript каталоги projects тоже могут отсутствовать.
        # Но недоступный/пропавший config home не является пустой историей.
        config_fd = _history_open(identity["config"], directory=True)
        os.close(config_fd)
        epoch = _history_digest(json.dumps([base, "pending"]).encode())
        empty = _history_digest(b"")
        token = {"v": 1, "epoch": epoch, "base": base, "file": [0, 0],
                 "offset": 0, "size": 0, "skip": False, "mtime": 0, "ctime": 0,
                 "anchor": empty, "head_bytes": 0, "head": empty}
        if _history_identity(cwd) != identity:
            raise LookupError("live session changed during history read")
        config_fd = _history_open(identity["config"], directory=True)
        os.close(config_fd)
        try:
            check_fd = _history_open(identity["config"], parts)
        except FileNotFoundError:
            pass
        else:
            os.close(check_fd)
            raise LookupError("transcript appeared during history read")
        encoded = base64.urlsafe_b64encode(json.dumps(token, separators=(",", ":")).encode()).decode().rstrip("=")
        return {"epoch": epoch, "session_id": sid, "cursor": encoded, "records": [],
                "reset": previous is not None and previous != token,
                "has_more": False, "partial": True}
    try:
        st = os.fstat(fd)
        file_id, size = [st.st_dev, st.st_ino], st.st_size
        tail_start = max(0, size - 64)
        tail = _history_bytes(fd, tail_start, size - tail_start)
        reset, offset, skip = False, 0, False
        epoch = _history_digest(json.dumps([base, file_id]).encode())
        if previous is not None:
            compatible = (previous["base"] == base and previous["file"] == file_id
                          and size >= previous["size"]
                          and (size > previous["size"] or (st.st_mtime_ns == previous["mtime"]
                                                           and st.st_ctime_ns == previous["ctime"]))
                          and _history_anchor(fd, previous["offset"]) == previous["anchor"]
                          and _history_digest(_history_bytes(fd, 0, previous["head_bytes"])) == previous["head"])
            if compatible:
                offset, skip, epoch = previous["offset"], previous["skip"], previous["epoch"]
                if offset and not skip and _history_bytes(fd, offset - 1, 1) != b"\n":
                    raise ValueError("history cursor is not on a record boundary")
            else:
                reset = True
                # inode может пережить truncate: новая эпоха нужна даже тогда.
                epoch = _history_digest(json.dumps([base, file_id, st.st_ctime_ns, previous["epoch"]]).encode())
        read_offset = offset
        records, offset, skip, partial, more, observed = _history_page(fd, offset, size, skip)
        head_bytes = min(64, size)
        token = {"v": 1, "epoch": epoch, "base": base, "file": file_id,
                 "offset": offset, "size": size, "skip": skip,
                 "mtime": st.st_mtime_ns, "ctime": st.st_ctime_ns,
                 "anchor": _history_anchor(fd, offset), "head_bytes": head_bytes,
                 "head": _history_digest(_history_bytes(fd, 0, head_bytes))}
        # Append разрешён, а замена/перезапись snapshot нет. Повторное discovery
        # идёт до последней проверки файла, чтобы замена в этом окне не уехала.
        if _history_identity(cwd) != identity:
            raise LookupError("live session changed during history read")
        check_fd = _history_open(identity["config"], parts)
        try:
            after = os.fstat(check_fd)
            if ([after.st_dev, after.st_ino] != file_id or after.st_size < size
                    or (after.st_size == size and (after.st_mtime_ns != st.st_mtime_ns
                                                   or after.st_ctime_ns != st.st_ctime_ns))
                    or _history_bytes(check_fd, read_offset, len(observed)) != observed
                    or _history_bytes(check_fd, tail_start, size - tail_start) != tail
                    or _history_anchor(check_fd, offset) != token["anchor"]
                    or _history_digest(_history_bytes(check_fd, 0, head_bytes)) != token["head"]):
                raise LookupError("transcript changed during history read")
        finally:
            os.close(check_fd)
        encoded = base64.urlsafe_b64encode(json.dumps(token, separators=(",", ":")).encode()).decode().rstrip("=")
        return {"epoch": epoch, "session_id": sid, "cursor": encoded, "records": records,
                "reset": reset, "has_more": more, "partial": partial}
    finally:
        os.close(fd)


def resolve(target):
    """Куда писать. Каталог -> живая сессия в нём (так адресуют папет пула),
    остальное -> строгий поиск по имени/pid; путь к сокету find принимает как
    есть."""
    if os.path.isdir(target):
        d = live_session_for_cwd(target)
        if d is None:
            raise LookupError(f"no live claude session in {target}")
        return d
    return find(target)


def find(target):
    """Цель -> файл сессии. Путь к сокету принимаем как есть: он может
    указывать на сессию, чей файл нам не читается."""
    if target.endswith(".sock"):
        return {"messagingSocketPath": target, "pid": None, "name": target}
    hits = [d for d in sessions()
            if str(d.get("pid")) == target
            or d.get("name") == target
            or d.get("cwd") == target]
    if not hits:
        hits = _in_cwd(target)
    if not hits:
        raise LookupError(f"session not found: {target}")
    if len(hits) > 1:
        # Имена выводятся из каталога и не уникальны; молча взять первую —
        # значит однажды написать не тому.
        names = ", ".join(f"{d.get('name')}[{d.get('pid')}]" for d in hits)
        raise Ambiguous(f"'{target}' matches several sessions: {names} — specify pid")
    return hits[0]


def peer_token(sock_path):
    """Ключ инбокса: ~/.claude/sessions/<pid>.<sha256(канонический путь)>.key

    Без него сообщение примут, но подписку notify_when_idle молча уронят."""
    import hashlib
    h = hashlib.sha256(os.path.realpath(sock_path).encode()).hexdigest()
    for f in glob.glob(f"{SESSIONS}/*.{h}.key"):
        try:
            with open(f) as fh:
                return json.load(fh)["peerToken"]
        except Exception:
            continue
    return None


def probe(cwd):
    """Достоверное состояние сессии, работающей в cwd: "<status> <alive> <listen>"
    либо "none". Формат строковый и плоский, потому что эта функция чаще всего
    исполняется на другом хосте, а вызывающий читает stdout."""
    d = session_for_cwd(cwd)
    if d is None:
        return "none"
    alive = False
    try:
        os.kill(int(d.get("pid")), 0)
        alive = True
    except Exception:
        alive = False
    listen = socket_alive(d["messagingSocketPath"]) if d.get("messagingSocketPath") else False
    return f"{d.get('status', '?')} {int(alive)} {int(listen)}"


# ─── исход хода: сток хуков claude (#222) ────────────────────────────────
# Состояние папета угадывалось по экрану tmux, и 24.09 два папета, умершие
# посреди хода на «Login expired», полтора часа читались как idle. Claude
# Code сообщает исход хода сам: вместо Stop приходит StopFailure с кодом
# ошибки (authentication_failed, rate_limit, billing_error, ...). Хук кладёт
# запись по session_id, `state` отдаёт её вместе с файлом сессии.
#
# Какие события: начало сессии, запрос, нормальный конец хода и смена модели
# снимают ошибку; StopFailure её ставит. PreToolUse/PostToolUse идут на
# каждый вызов инструмента, а busy и так есть в файле сессии; Notification
# не говорит, когда диалог закрылся, -- живой диалог в waitingFor файла.
CLEARING = ("SessionStart", "UserPromptSubmit", "Stop", "PostModelSwitch")
FAILURE = "StopFailure"
# Коды провала, на которых решают разборщики пакета (state, credreg): здесь,
# где словарь claude принимают, один раз. Что код значит -- у каждого своё.
AUTH_FAILED = "authentication_failed"
RATE_LIMIT = "rate_limit"
BILLING_ERROR = "billing_error"
DETAIL_MAX = 300
TURN_TTL = 7 * 86400


def _plain_id(sid):
    """session_id -- имя файла: только буквы, цифры, дефис и подчёркивание,
    иначе вход -- не наш (путь в имени писал бы мимо каталога)."""
    return isinstance(sid, str) and bool(sid) and \
        all(c.isalnum() or c in "-_" for c in sid) and sid.isascii()


def turn_record(payload, now):
    """Вход хука -> запись исхода хода или None (событие не наше, нет
    session_id). Форма ровно {event, at, error, detail}."""
    if not isinstance(payload, dict) or not _plain_id(payload.get("session_id")):
        return None
    event = payload.get("hook_event_name")
    if event in CLEARING:
        return {"event": event, "at": int(now), "error": None, "detail": None}
    if event == FAILURE:
        # Кода нет -- всё равно провал: «unknown» из словаря claude, а не
        # None, который читался бы снятой ошибкой.
        detail = payload.get("last_assistant_message")
        return {"event": event, "at": int(now), "error": payload.get("error") or "unknown",
                "detail": detail[:DETAIL_MAX] if isinstance(detail, str) and detail else None}
    return None


def _write_turn(root, sid, record, now):
    """Запись атомарно: временный файл рядом + os.replace. Попутно снимаются
    записи старше недели -- мёртвые сессии копятся."""
    os.makedirs(root, exist_ok=True)
    tmp = os.path.join(root, f".{sid}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(record, f)
        os.replace(tmp, os.path.join(root, f"{sid}.json"))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    for f in glob.glob(os.path.join(root, "*.json")):
        try:
            if os.path.getmtime(f) < now - TURN_TTL:
                os.remove(f)
        except OSError:
            pass


# ─── модель сессии (#453) ────────────────────────────────────────────────
# Дашборд показывает модель папета пусто: сессии claude не несут модель
# нигде, что читает ростер. Claude Code сам сообщает её дважды: JSON на stdin
# statusline-команды (при старте и далее периодически, поле model.id) и
# событием PostModelSwitch (to_model). Отмена и провал перехода события не
# дают -- проверено вживую на 2.1.288: провалившийся /model не оставляет
# записи, и статус-строка продолжает звать прежнюю модель.
#
# У statusLine нет времени наблюдения: в его JSON поля с моментом, когда
# модель была видна, нет, «сейчас» стока -- только момент прихода строки.
# Опрос, сгенерированный ДО перехода, может дойти и ПОСЛЕ -- с прежней
# моделью и «свежим» временем прихода. Голое сравнение at такую запись
# пропускало: она откатывала применённый переход (равный at и даже at+1,
# если опрос завис в канале на секунду дольше); память только о предыдущей
# модели (from_model) не спасала и при двух быстрых переходах -- запоздалый
# опрос с САМОЙ ПЕРВОЙ моделью проходил как «иная». Отсюда окно STALE_WINDOW
# без истории старых id: событие перехода ставит модель и свой момент;
# пока окно действует, опрос может лишь ПОДТВЕРЖДАТЬ модель события (at
# обновляется, окно переживает подтверждение), сменить её внутри окна
# способен только новый PostModelSwitch -- он всегда право. Вне окна опрос
# -- правда как есть: модель могла смениться и без события (ручной /model
# с «s: session only»). Граница гарантии: переход без события виден ростеру
# не раньше, чем опрос дойдёт после истечения окна, -- до 5 минут модель в
# записи остаётся прежней; и откат не ловится, если опрос завис дольше окна
# И модель успела смениться без события -- тогда «старое» наблюдение
# неотличимо от нового факта.
STALE_WINDOW = 300


def model_record(payload, now):
    """Вход statusline или PostModelSwitch -> {model, at} | None.

    Ровно эта форма и ничего из сырого JSON: ростеру нужен один id.
    PostModelSwitch распознаётся по to_model (в словаре statusline его нет),
    statusline -- по model.id; отсюда один вход у обоих стоков."""
    if not isinstance(payload, dict) or not _plain_id(payload.get("session_id")):
        return None
    if isinstance(payload.get("to_model"), str) and payload["to_model"]:
        model = payload["to_model"]
    else:
        model = (payload.get("model") or {}).get("id") \
            if isinstance(payload.get("model"), dict) else None
    # Ограничение сверху держит запись файлом с честным именем, а не потоком
    # чужих данных под именем session_id: словарь claude открытый, и поле
    # может оказаться чем угодно.
    if not isinstance(model, str) or not model or len(model) > 128:
        return None
    return {"model": model, "at": int(now)}


def _model_event(payload):
    """Событие перехода -> to_model | None. Проверенное поле, не сырой
    словарь: id модели той же нормы, что в model_record."""
    to_m = payload.get("to_model") if isinstance(payload, dict) else None
    if isinstance(to_m, str) and to_m and len(to_m) <= 128:
        return to_m
    return None


def read_model(sid, root=None):
    """Запись модели сессии или None (нет, не читается, чужой session_id)."""
    if not _plain_id(sid):
        return None
    try:
        with open(os.path.join(root or MODELS, f"{sid}.json")) as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def write_model(payload, now, root=None):
    """Вход statusline/PostModelSwitch -> запись модели по session_id.

    Событие перехода право всегда -- ставит модель и свой момент (event_at).
    Опрос внутри STALE_WINDOW от события лишь подтверждает модель события
    (at обновляется, event_at хранится -- окно переживает подтверждение);
    любая иная модель у опроса внутри окна -- запоздалое наблюдение ДО
    перехода и отбрасывается. Вне окна опрос пишется как есть: модель могла
    смениться и без события. TTL и атомарность -- как у записей хода
    (_write_turn)."""
    record = model_record(payload, now)
    if record is None:
        return
    sid = payload["session_id"]
    event = _model_event(payload)
    current = read_model(sid, root=root)
    if current and not event and current.get("event_at") \
            and record["at"] - current["event_at"] <= STALE_WINDOW:
        if record["model"] != current.get("model"):
            # Запоздалое наблюдение до перехода: модель внутри окна
            # меняет только новое событие.
            return
        if record["at"] > current.get("at", 0):
            # Подтверждение новее -- обновляем только at (дубль с более
            # ранним временем прихода ничего не меняет).
            out = {**current, "at": record["at"]}
            _write_turn(root or MODELS, sid, out, now)
        return
    out = dict(record)
    if event:
        out["event_at"] = record["at"]
    _write_turn(root or MODELS, sid, out, now)


def hook(text, now, root=None):
    """Вход хука (stdin) -> запись на диск, если событие наше."""
    payload = json.loads(text)
    record = turn_record(payload, now)
    if record is not None:
        _write_turn(root or TURNS, payload["session_id"], record, now)
    write_model(payload, now, root=root)


def read_turn(sid, root=None):
    """Запись исхода хода сессии или None."""
    if not _plain_id(sid):
        return None
    try:
        with open(os.path.join(root or TURNS, f"{sid}.json")) as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def state(cwd):
    """Состояние свежейшей сессии каталога одной записью: status, alive и
    listen -- как у probe, waitingFor -- из файла сессии (открытый диалог),
    turn -- запись исхода хода этой сессии, model -- id модели ЖИВОЙ сессии
    каталога по её session_id (мёртвая -- null: запись трупа ничего не
    говорит о работающей сессии). Привязка подтверждения (#468): sessionId
    -- session_id живой сессии (чья модель -- тот же id), model_at -- время
    записи модели (свежесть показа); записи нет -- model_at null вместе с
    моделью, sessionId остаётся. Сессии нет -- те же ключи, пустые:
    status и waitingFor null, alive и listen false, turn, sessionId,
    model_at и model null."""
    d = session_for_cwd(cwd)
    if d is None:
        return {"status": None, "waitingFor": None, "alive": False, "listen": False,
                "turn": None, "sessionId": None, "model_at": None, "model": None}
    try:
        os.kill(int(d.get("pid")), 0)
        alive = True
    except Exception:
        alive = False
    listen = socket_alive(d["messagingSocketPath"]) if d.get("messagingSocketPath") else False
    live = None
    if listen:
        # Модель -- утверждение о работающей сессии: свежайший файл каталога
        # может быть трупом. Диагностику умершей сессии (status/turn) это не
        # трогает: она и прежде была о свежайшем файле, а model -- о живых.
        live = live_session_for_cwd(cwd)
    got = read_model(live.get("sessionId")) if live is not None else None
    return {"status": d.get("status", "?"), "waitingFor": d.get("waitingFor"),
            "alive": alive, "listen": listen, "turn": read_turn(d.get("sessionId")),
            "sessionId": live.get("sessionId") if live is not None else None,
            "model_at": got.get("at") if got else None,
            "model": got.get("model") if got else None}


# ─── протокол канала ─────────────────────────────────────────────────────
# Подсказка едет в теле каждого сообщения, а не в системном промпте, потому
# что системную инструкцию съедает компактация, а тело — нет. Чинит ровно тот
# случай, на котором мы споткнулись: glm-папет получило сообщение от
# "mop", попыталось ответить встроенным SendMessage, получило "No agent
# named 'mop' is reachable" и отдало ответ случайному соседу по хосту.
#
# Обратный адрес называем прямо. Раньше здесь стояло «отправитель обратного
# адреса не даёт», и это была не осторожность, а дыра: папет, которого просили
# отчитаться, шёл в mcp__mop__send с любым именем, какое смог придумать, и
# получал глухое "Error executing tool send" — маршрута до мастера в send
# просто не было. Теперь у мастера есть адрес (его инбокс на шине), он едет в
# конверте полем from-name, и подсказка называет его тем же словом, каким его
# примет send.
#
# uds-адрес отправителя по-прежнему не обещаем: доставляет агент узла, а сокет
# его собственного инбокса живёт ровно столько, сколько ждёт доставка.
def reply_hint(from_name=None):
    """Чем закончить тело сообщения: куда адресату отвечать."""
    how = (f"Reply to the sender: mcp__mop__send(to=\"{from_name}\", …) — that "
           f"is its bus address, not a session name."
           if from_name and from_name != "mop" else
           "The sender did not give a reply address.")
    return ("[mop channel] Delivered over the pool's socket channel. " + how +
            " Who else you can write to — mcp__mop__agents: the pool's puppets and "
            "the project's masters. The built-in SendMessage won't work for this: it "
            "only reaches sessions on this same host and knows nothing about the "
            "rest of the pool.")


def envelope(body, from_addr=None, from_name=None, mode="bypass"):
    """Конверт заявки о режиме прав.

    Папет с --dangerously-skip-permissions не отдаёт модели сообщение от
    отправителя, который не заявил свой режим: оно кладёт его в held-очередь и
    ждёт живого человека. Для папета это неотличимо от потери сообщения.

    Порядок атрибутов фиксирован: приёмник разбирает конверт регуляркой и
    проверяет, что обратная сборка даёт исходную строку байт в байт —
    переставленные атрибуты просто не распознаются."""
    attrs = ""
    if from_addr:
        attrs += f' from="{from_addr}"'
    if from_name:
        attrs += f' from-name="{from_name}"'
    if mode:
        attrs += f' from-mode="{mode}"'
    return f"<cross-session-message{attrs}>\n{body}\n</cross-session-message>"


def user_frame(content, priority="next", from_addr=None):
    f = {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user",
         "message": {"role": "user", "content": content}, "priority": priority}
    if from_addr:
        f["from"] = from_addr
    return f


def control_frame(action, **fields):
    return {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "control",
            "action": action, **fields}


def write_frames(sock_path, frames, token=None):
    """Одно соединение, NDJSON, закрыть.

    Ответа в этом же соединении не бывает никогда: квитанции сессия шлёт
    отдельным коннектом на адрес из поля from."""
    line = ""
    if token:
        line += json.dumps({"type": "auth", "token": token}) + "\n"
    for f in frames:
        line += json.dumps(f, ensure_ascii=False) + "\n"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    try:
        s.connect(sock_path)
    except FileNotFoundError:
        raise ConnectionError(f"inbox not found: {sock_path} — session died or moved")
    except ConnectionRefusedError:
        raise ConnectionError(f"inbox is dead: {sock_path} — process is not listening")
    try:
        s.sendall(line.encode())
    finally:
        s.close()


class Inbox:
    """Свой инбокс для квитанций и peer_idle_notice.

    Имя и каталог не произвольные: приёмник отвечает, только если адрес лежит в
    том же каталоге сокетов и назван как <pid>.sock — иначе он отказывается
    писать в чужое пространство имён и молча роняет ответ."""

    def __init__(self, sockets_dir, tag=None):
        # tag нужен, когда один процесс ждёт простоя сразу нескольких сессий:
        # агент узла обслуживает всех папетов хоста, и без метки второй инбокс
        # отобрал бы путь <pid>.sock у первого. Форма <pid>-<hex>.sock приёмнику
        # тоже годится — других он молча не отвечает.
        leaf = f"{os.getpid()}-{tag}.sock" if tag else f"{os.getpid()}.sock"
        self.path = os.path.join(sockets_dir, leaf)
        self.frames = []
        self._event = threading.Event()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        os.chmod(self.path, 0o600)
        self.srv.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def address(self):
        return f"uds:{self.path}"

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            buf = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            conn.close()
            for ln in buf.decode(errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    self.frames.append(json.loads(ln))
                except ValueError:
                    continue
                self._event.set()

    def wait_for(self, action, orig_msg_id, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in self.frames:
                if f.get("action") == action and f.get("orig_msg_id") == orig_msg_id:
                    return f
            self._event.wait(1)
            self._event.clear()
        return None

    def close(self):
        try:
            self.srv.close()
        finally:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def send(sock_path, body, priority="next", mode="bypass", from_name="mop",
         wait_idle=0, hint=True):
    """Отправить сообщение сессии. -> {"msg_id":…, "idle":<кадр|None>}

    wait_idle > 0 поднимает свой инбокс, подписывается на notify_when_idle и
    ждёт до wait_idle секунд, пока сессия отчитается о простое."""
    token = peer_token(sock_path)
    inbox = Inbox(os.path.dirname(sock_path)) if wait_idle else None
    try:
        from_addr = inbox.address if inbox else None
        text = f"{body}\n\n{reply_hint(from_name)}" if hint else body
        frame = user_frame(envelope(text, from_addr, from_name, mode), priority, from_addr)
        write_frames(sock_path, [frame], token)
        if not inbox:
            return {"msg_id": frame["msg_id"], "idle": None}
        # Подписка отдельным кадром и со своим msg_id: notice приходит с
        # orig_msg_id именно подписки, а не сообщения.
        sub = control_frame("notify_when_idle", **{"from": from_addr, "from_mode": mode})
        write_frames(sock_path, [sub], token)
        return {"msg_id": frame["msg_id"],
                "idle": inbox.wait_for("peer_idle_notice", sub["msg_id"], wait_idle)}
    finally:
        if inbox:
            inbox.close()


def wait_idle(cwd, timeout, tag=None):
    """Дождаться простоя сессии в cwd, не занимая её ход. -> состояние | None.

    `send` всегда пишет пользовательский кадр первым, и прежний ожидатель этим
    и пользовался: сессия получала пустое тело с одной лишь подсказкой. Здесь
    нужен только control-кадр — спрашивать «ты освободился?», занимая ход,
    значит мешать ровно тому, чего ждёшь.

    Форма CLI обязательна, а не удобна: у контейнерного папета сокет сессии
    живёт внутри тела, и ждать его импортом с узла некому. Без этого глагола
    push-уведомления мастеру у таких папетов молча не приходят."""
    sock = resolve(cwd)["messagingSocketPath"]
    inbox = Inbox(os.path.dirname(sock), tag=tag)
    try:
        sub = control_frame("notify_when_idle",
                            **{"from": inbox.address, "from_mode": "bypass"})
        write_frames(sock, [sub], peer_token(sock))
        return (inbox.wait_for("peer_idle_notice", sub["msg_id"], timeout)
                or {}).get("state")
    finally:
        inbox.close()


def main(argv):
    if argv[:1] == ["hook"]:
        # Молча и всегда 0: stdout хуков SessionStart и UserPromptSubmit
        # уходит в контекст модели, а код 2 на Stop заставил бы claude
        # продолжить ход. Любой сбой стока -- не дело сессии.
        try:
            hook(sys.stdin.read(), time.time())
        except BaseException:
            pass
        return 0
    if argv[:1] == ["statusline"]:
        # Тот же контракт молчания: stdout statusline-команды уходит прямо в
        # терминал сессии, а её сбой не должен ронять показ строки.
        try:
            write_model(json.loads(sys.stdin.read()), time.time())
        except BaseException:
            pass
        return 0
    if argv[:1] == ["history"]:
        try:
            if len(argv) == 2:
                out = history(argv[1])
            elif len(argv) == 4 and argv[2] == "--cursor":
                out = history(argv[1], argv[3])
            else:
                raise ValueError("usage: session.py history <cwd> [--cursor CURSOR]")
        except Exception as e:
            out = {"error": str(e)}
        print(json.dumps(out, ensure_ascii=True, allow_nan=False))
        return 0 if "error" not in out else 1
    if len(argv) >= 2 and argv[0] == "state":
        try:
            out = state(argv[1])
        except Exception as e:
            out = {"error": str(e)}
        print(json.dumps(out, ensure_ascii=False))
        return 0 if "error" not in out else 1
    if len(argv) >= 2 and argv[0] == "probe":
        print(probe(argv[1]))
        return 0
    if len(argv) >= 3 and argv[0] == "wait-idle":
        # JSON и здесь, по той же причине, что у send: чаще всего эта ветка
        # исполняется в другой машине, и вызывающий читает один лишь stdout.
        try:
            out = {"state": wait_idle(argv[1], int(argv[2]),
                                      tag=(argv[3] if len(argv) > 3 else None))}
        except Exception as e:
            out = {"error": str(e)}
        print(json.dumps(out, ensure_ascii=False))
        return 0 if "error" not in out else 1
    if len(argv) >= 3 and argv[0] == "send":
        # Ответ всегда JSON, в том числе на ошибку: чаще всего эта ветка
        # исполняется на другом хосте, и вызывающий читает один лишь stdout.
        target, body = argv[1], argv[2]
        rest = argv[3:]
        opts = dict(zip(rest[::2], rest[1::2]))
        try:
            sock = resolve(target)["messagingSocketPath"]
            r = send(sock, body,
                     priority=opts.get("--priority", "next"),
                     mode=opts.get("--mode", "bypass"),
                     from_name=opts.get("--from-name", "mop"),
                     wait_idle=int(opts.get("--wait", 0)))
            out = {"msg_id": r["msg_id"], "idle": (r["idle"] or {}).get("state")}
        except Exception as e:
            out = {"error": str(e)}
        print(json.dumps(out, ensure_ascii=False))
        return 0 if "error" not in out else 1
    print("usage: session.py probe <cwd> | state <cwd> | "
          "history <cwd> [--cursor CURSOR] | hook (stdin) | "
          "statusline (stdin) | wait-idle <cwd> <timeout> [tag] | "
          "send <target> <text> [--priority P] [--mode M] [--from-name N] "
          "[--wait SEC]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
