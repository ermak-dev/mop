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
    old = _read_key(source_path)
    state = {"id": secrets.token_hex(8), "schema": SCHEMA,
             "old": old, "new": _new_key(),
             "source": source_path, "copy": copy_path,
             "recipients": list(recipients), "stage": "ready"}
    fsutil.make_private_dir(os.path.dirname(OP))
    fsutil.write_private(OP, json.dumps(state))
    return _project(state)


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
