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
import threading
import time

from ..common import bus, config, paths, puppets

TICK = 300                   # редко и предсказуемо; sha не нов -- ни одного запроса
STATE = paths.local("proxy-key.json")
KEY_FILE = paths.local(paths.SECRETS, "llm-proxy-client.pass")


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
