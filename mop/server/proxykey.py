"""Раздача ключа LLM-прокси узлам (#391, эпик #379).

Клиентские пути раздачи умерли: из мастер-шелла копию узла не пишут -- это
by design (#312, чужой проект не сеет свои ключи будущим телам), а mop
login -- путь кредов (#385). Владелец ключа -- сервер: ключ лежит в
закрытом файле установленного прокси; до раскатки файла работает старый
MOP_PROXY_KEY из окружения. Сервис раздаёт ключ глаголом write от своего
субъекта -- админа: копия каждого узла и живые тела без аренды.
Свежие тела сеются с копии узла, живые получают письмо напрямую.

Только stdlib: рендер и решение чистые (tests/proxykey.py), раздача -- на
живом пуле, потоком сервиса.
"""
import hashlib
import json
import os
import secrets
import stat
import threading
import time
from contextlib import contextmanager

from ..common import bus, config, fsutil, paths, puppets

TICK = 300                   # редко и предсказуемо; sha не нов -- ни одного запроса
STATE = paths.local("proxy-key.json")
KEY_FILE = paths.local(paths.SECRETS, "llm-proxy-client.pass")

# ── ротация клиентского ключа (#538, контракт #537) ──────────────────────
# Протокол без CAS: единственный писатель -- обязательное явное условие
# вызывающего, не молчаливое допущение. Состояние ОДНОЙ операции, приватное:
# старый и новый ключ для возобновления после сбоя (отпечатка недостаточно).
SCHEMA = 1
OP = paths.local("proxy-rotation.json")
LOCK = paths.local("proxy-rotation.lock")


def _fetch_list():
    """Шов remote-списка прокси: GET management api-keys. Реализация --
    глагол #539; библиотека шва не имеет (внеплановый вызов -- громкий
    отказ, не тихий None)."""
    raise NotImplementedError("the proxy key list seam is the #539 verb's")


def _put_list(keys):
    """Шов записи списка: PUT management api-keys. (исход, причина);
    'applied' -- записано (применение подтверждает только probe)."""
    raise NotImplementedError("the proxy key list seam is the #539 verb's")


def _probe(key):
    """Шов проверки клиентским API: принят ли ключ прокси сейчас."""
    raise NotImplementedError("the proxy probe seam is the #539 verb's")


@contextmanager
def _lock(path=LOCK):
    """flock на постоянном приватном lock-файле: параллельная операция того
    же процесса отказывает сразу, смерть процесса освобождает блокировку
    сама -- угадывать по PID, жив ли владелец, не нужно. Файл не
    пересоздаётся по звонку: inode живёт, пока жив каталог."""
    fsutil.make_private_dir(os.path.dirname(path))
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RuntimeError("another rotation is running in this home") from e
        try:
            yield
        finally:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def begin(source_path, copy_path, recipients, *, exclusive):
    """Начать ротацию: проверить условие, родить ключ, записать состояние.

    Никаких remote-швов: begin только создаёт приватное состояние ДО любого
    эффекта -- шаги к удалённому списку делает resume по чекпоинтам.
    exclusive -- явное условие единственного писателя от вызывающего: без
    него отказ без эффектов. recipients -- сохраняемый НАМЕРЕННЫЙ список
    получателей (ready_nodes его не фильтрует на resume). -> проекция без
    ключей."""
    if not exclusive:
        raise RuntimeError("rotation requires the exclusive-writer precondition")
    with _lock():
        prior = _load_op()
        if prior is not None and prior.get("stage") != "completed":
            raise RuntimeError("an unfinished rotation state exists; resume it, "
                               "do not begin over it")
        old = _read_key(source_path)
        state = {"id": secrets.token_hex(8), "schema": SCHEMA,
                 "old": old, "new": _new_key(),
                 "source": source_path, "copy": copy_path,
                 "recipients": list(recipients), "stage": "ready"}
        fsutil.write_private(OP, json.dumps(state))
        return _project(state)


def resume(*, exclusive):
    """Продолжить незавершённую ротацию по её приватному состоянию.

    exclusive -- то же обязательное явное условие на КАЖДОМ входе. Состояние
    читается целиком: битое, чужой схемы или без ключей -- громкий отказ без
    эффектов. -> проекция без ключей."""
    if not exclusive:
        raise RuntimeError("rotation requires the exclusive-writer precondition")
    with _lock():
        state = _checked_state()
        return _project(state)


def _load_op():
    """Состояние операции либо None: файла нет или не JSON."""
    try:
        with open(OP, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _checked_state():
    """Состояние, годное для продолжения, либо громкий отказ."""
    state = _load_op()
    if state is None:
        raise RuntimeError("no rotation state to resume")
    if state.get("schema") != SCHEMA:
        raise RuntimeError(f"rotation state schema {state.get('schema')!r} "
                           f"is not {SCHEMA}: not this library's state")
    for field in ("old", "new"):
        if not state.get(field):
            raise RuntimeError(f"rotation state has no {field} key: not resumable")
    if not isinstance(state.get("recipients"), list):
        raise RuntimeError("rotation state has no saved recipient set")
    return state


def _read_key(path):
    """Ключ из файла: пустой файл -- громкий отказ с именем файла."""
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().rstrip("\r\n")
    except OSError as e:
        raise RuntimeError(f"cannot read the key source {path}: {e.strerror}") from e
    if not key:
        raise RuntimeError(f"the key source {path} is empty")
    return key


def _new_key():
    """Одноразовая генерация нового клиентского ключа."""
    return secrets.token_urlsafe(32)


def _preflight(state):
    """Локальные именованные блокеры ДО любой remote-мутации. -> None |
    строка-блокер. Третье значение (не old, не new) -- drift: оператор
    решает ДО изменения. Сервисопригодность копии: права прежние, режим
    приватный -- атомарная запись под root сменила бы владельца и отрезала
    бы сервис от его копии."""
    for path, what in ((state["source"], "source"), (state["copy"], "copy")):
        key = _key_or_none(path)
        if key is None:
            return f"blocker: {what} file {path} is missing or empty"
        if key not in (state["old"], state["new"]):
            return (f"drift: {what} file {path} holds neither the old nor "
                    f"the new key of the operation")
    try:
        mode = stat.S_IMODE(os.stat(state["copy"]).st_mode)
    except OSError as e:
        return f"blocker: copy file {state['copy']} is not serviceable: {e.strerror}"
    if mode & 0o077:
        return (f"blocker: copy file {state['copy']} mode {oct(mode)} is not "
                f"private; rotation must preserve a serviceable copy")
    return None


def _key_or_none(path):
    """Ключ из файла либо None: нет файла, нечитаем или пуст."""
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().rstrip("\r\n")
    except OSError:
        return None
    return key or None


def _remote_step(state):
    """Шаг remote-списка: свежее чтение, объединение, типизированный исход.

    Читает список СВЕЖО (панель могла писать между шагами), пишет
    объединение прочитанного с сохранённым new -- чужие ключи не тронуты.
    Пустой прочитанный список не пишется никогда: он открывает прокси (#537,
    upstream provider.go:20-24). Управляемый old обязан быть в списке ДО
    изменения -- иначе drift, и решает оператор. -> {"ok": bool, ...} без
    ключей."""
    out = {"ok": False}
    keys = _fetch_list()
    if not keys:
        out["blocker"] = "the read key list is empty; an empty list is never written"
        return out
    if state["old"] not in keys:
        out["blocker"] = (f"drift: the managed old key is not in the proxy list; "
                          f"restoring a revoked key is an operator decision")
        return out
    want = keys + [state["new"]] if state["new"] not in keys else keys
    try:
        verdict, reason = _put_list(want)
    except (OSError, TimeoutError) as e:
        # Unknown-исход: PUT мог примениться. Не откатываем, не меняем
        # ключ: reconcile на resume перечитает свежий список.
        out["blocker"] = f"unknown PUT outcome ({type(e).__name__}): reconcile on resume"
        out["unknown"] = True
        return out
    if verdict != "applied":
        out["blocker"] = f"PUT refused before applying: {reason}"
        out["refused"] = True
        return out
    state["stage"] = "listed"
    _save_op(state)
    out.update({"ok": True, "stage": state["stage"]})
    return out


def _save_op(state):
    """Чекпоинт состояния операции: приватно, атомарно."""
    fsutil.write_private(OP, json.dumps(state))


def _project(state):
    """Состояние для вызывающего: без ключей и без raw-исключений."""
    return {"id": state.get("id"), "schema": state.get("schema"),
            "stage": state.get("stage"),
            "recipients": list(state.get("recipients") or [])}


def source(env, path=None):
    """Ключ установленного прокси; прежний env -- лишь до раскатки файла."""
    try:
        with open(path or KEY_FILE, encoding="utf-8") as f:
            key = f.read().rstrip("\r\n")
    except FileNotFoundError:
        old = env.get("MOP_PROXY_KEY", "")
        return old[0] if isinstance(old, tuple) else old
    if not key:
        raise ValueError("installed proxy key file is empty")
    return key


def blob(env):
    """Что везти узлам в secrets.env: ключ прокси и переходные, что есть.
    -> содержимое | None, когда раздавать нечего (ключа нет -- установка
    без прокси, тишина правильнее отказа)."""
    key = env.get("MOP_PROXY_KEY")
    # config.effective() несёт происхождение настройки кортежем (значение,
    # источник): в блоб едет значение, а не его repr -- 401 у всех тел
    # стоил именно этого.
    if isinstance(key, tuple):
        key = key[0]
    return f"MOP_PROXY_KEY={key}\n" if key else None


def sha(content):
    """Отпечаток блоба: что именно уехало, чтобы не гонять то же самое."""
    return hashlib.sha256((content or "").encode()).hexdigest()


def due(content, state):
    """Пора ли раздавать: блоба нет -- нет, отпечаток нов -- да."""
    if content is None:
        return False
    return not state or state.get("sha") != sha(content)


def settled(outcome):
    """Дошли ли до всех: недошедший узел держит раздачу открытой -- иначе
    поднявшийся узел жил бы без ключа до смены блоба. Отказ агента не
    держит: он виден в журнале каждого тика, а тишина тут прятала бы его."""
    return not any(str(r).startswith("NOT REACHED") for r in (outcome or {}).values())


def strict_settled(outcome):
    """Итог раздачи ШАГА ротации (#537, #538): держат открытым и недошедший
    узел (NOT REACHED), и отказ агента (FAILED) -- частичная раздача не
    «completed»: недошедшие так и ходят старым ключом до отдельного
    retirement. Старый settled тика (#391) этим не заменяется."""
    return not any(str(r) != "OK" for r in (outcome or {}).values())


def push(env=None, nodes=None, now=None):
    """Раздать блоб узлам и запомнить отпечаток. -> {узел: итог} | None.

    None -- раздавать нечего или отпечаток не нов. nodes -- готовый список
    (проба), иначе состав пула по шине. Пишет глагол write без адреса:
    агент для админа кладёт копию узла и живые тела без аренды."""
    env = config.effective() if env is None else env
    content = blob({"MOP_PROXY_KEY": source(env)})
    if not due(content, _load()):
        return None
    if nodes is None:
        nodes = sorted(puppets.ready_nodes())
    if not nodes:
        return {}
    try:
        answers = bus.request_many("write", nodes, timeout=bus.WRITE_TIMEOUT,
                                   files=[bus.as_file(paths.NODE_SECRETS, content)])
    except bus.BusError as e:
        answers = {n: e for n in nodes}
    out = bus.results_from(nodes, answers)
    if settled(out):
        _save({"sha": sha(content), "at": int(now or time.time()), "nodes": out})
    return out


def ticker(log, every=TICK):
    """Цикл раздачи: sha не нов -- ни одного запроса шине. Живёт рядом с
    никаким другим циклом."""
    while True:
        try:
            out = push()
            if out:
                log(", ".join(f"{n} {r}" for n, r in sorted(out.items())))
        except Exception as e:  # noqa: BLE001 -- цикл не имеет права умирать
            log(f"proxy key push failed: {type(e).__name__}: {e}")
        time.sleep(every)


def start(log):
    """Поток раздачи: сервис стартует -- ключ обязан быть на узлах до
    первого папета, ждать тика нельзя."""
    threading.Thread(target=ticker, args=(log,), daemon=True,
                     name="proxy-key").start()


def _load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(state, f)
