"""Раздача ключа LLM-прокси узлам (#391, эпик #379).

Клиентские пути раздачи умерли: из мастер-шелла копию узла не пишут -- это
by design (#312, чужой проект не сеет свои ключи будущим телам), а mop
login -- путь кредов (#385). Владелец ключа -- сервер: ключ лежит в его
.env (MOP_PROXY_KEY), и сервис кластера раздаёт его глаголом write от
своего субъекта -- админа: копия каждого узла и живые тела без аренды.
Свежие тела сеются с копии узла, живые получают письмо напрямую.

Только stdlib: рендер и решение чистые (tests/proxykey.py), раздача -- на
живом пуле, потоком сервиса.
"""
import hashlib
import json
import os
import threading
import time

from ..common import bus, config, fsutil, paths, puppets

TICK = 300                   # как цикл кредитов (#284): редко и предсказуемо
STATE = paths.local("proxy-key.json")


def blob(env):
    """Что везти узлам в secrets.env: ключ прокси и переходные, что есть.
    -> содержимое | None, когда раздавать нечего (ключа нет -- установка
    без прокси, тишина правильнее отказа)."""
    key = env.get("MOP_PROXY_KEY")
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
    content = blob(env)
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
    циклом кредитов до его снятия (#386)."""
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
