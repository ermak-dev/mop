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
    raise NotImplementedError("the proxy key list seam belongs to the orchestration verb")


def _put_list(keys):
    """Шов записи списка: PUT management api-keys. (исход, причина);
    'applied' -- записано (применение подтверждает только probe)."""
    raise NotImplementedError("the proxy key list seam belongs to the orchestration verb")


def _probe(key):
    """Шов проверки клиентским API: принят ли ключ прокси сейчас."""
    raise NotImplementedError("the proxy probe seam belongs to the orchestration verb")


@contextmanager
def _lock(path=None):
    """flock на постоянном приватном lock-файле: параллельная операция того
    же процесса отказывает сразу, смерть процесса освобождает блокировку
    сама -- угадывать по PID, жив ли владелец, не нужно. Файл не
    пересоздаётся по звонку: inode живёт, пока жив каталог. Путь читается
    на каждом входе (не дефолтом аргумента), чтобы подмена LOCK в проверке
    доходила до настоящего файла."""
    if path is None:
        path = LOCK
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
        if prior is not None:
            why = _validate(prior)
            if why:
                raise RuntimeError(f"refusing to overwrite the existing "
                                   f"state: {why}")
            if prior.get("stage") != "completed":
                raise RuntimeError("an unfinished rotation state exists; resume it, "
                                   "do not begin over it")
        old = _read_key(source_path)
        state = {"id": secrets.token_hex(8), "schema": SCHEMA,
                 "old": old, "new": _new_key(),
                 "source": source_path, "copy": copy_path,
                 "recipients": list(recipients), "stage": "ready"}
        why = _validate(state)
        if why:  # свой же state не годен -- не сохранять его вовсе
            raise RuntimeError(f"the new state is not valid: {why}")
        fsutil.write_private(OP, json.dumps(state))
        return _project(state)


def resume(*, exclusive):
    """Продолжить незавершённую ротацию по её приватному состоянию.

    exclusive -- то же обязательное явное условие на КАЖДОМ входе. Ведёт
    шаги по сохранённым чекпоинтам до первого блокера: preflight (на старте
    операции, не на каждом входе) → remote-список → probe-подтверждение →
    локальная смена → раздача. Завершённая операция читается как завершённая.
    -> {"ok": bool, ...} без ключей."""
    if not exclusive:
        raise RuntimeError("rotation requires the exclusive-writer precondition")
    with _lock():
        state = _checked_state()
        if state.get("stage") == "completed":
            return {"ok": True, "stage": "completed"}
        # Guard на каждом входе перед mutating-шагом: пауза на любой
        # стадии не открывает окно третьей стороне.
        blocker = _guard(state)
        if blocker:
            return {"ok": False, "blocker": blocker}
        steps = {"ready": _remote_step, "listed": _probe_step,
                 "accepted": _local_step, "local": _deliver_step}
        while state["stage"] in steps:
            out = steps[state["stage"]](state)
            if not out.get("ok"):
                return out
        return {"ok": True, "stage": state["stage"]}


def status():
    """Состояние операции для вызывающего: без ключей и без raw-причин.
    Нет операции -- честный None."""
    state = _load_op()
    if state is None:
        return None
    why = _validate(state)
    if why:
        return {"stage": "invalid", "reason": why}
    out = _project(state)
    delivered = state.get("delivered")
    if delivered:
        out["delivered"] = dict(delivered)
    return out


def _load_op():
    """Состояние операции. Только действительное отсутствие файла -- None:
    существующее битое или нечитаемое -- громкий отказ, чтобы begin не
    затирал его молча. -> dict | None | RuntimeError."""
    try:
        with open(OP, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise RuntimeError(f"the rotation state {OP} is not valid JSON: "
                           f"{type(e).__name__}") from e
    except OSError as e:
        raise RuntimeError(f"cannot read the rotation state {OP}: "
                           f"{e.strerror}") from e


def _validate(state):
    """Одна строгая форма состояния на всех входах (load/prior/resume/
    status). -> None | строка-причина. schema -- int ровно SCHEMA (bool и
    float -- не схема), ключи и пути -- непустые строки, stage -- из
    допустимых, recipients -- список уникальных непустых строк. Unknown
    stage не «ok»: цикл шагов его не покрывает."""
    if not isinstance(state, dict):
        return "the rotation state is not an object"
    if type(state.get("schema")) is not int or state.get("schema") != SCHEMA:
        return f"rotation state schema {state.get('schema')!r} is not {SCHEMA}"
    for field in ("id", "old", "new", "source", "copy"):
        if not isinstance(state.get(field), str) or not state.get(field):
            return f"rotation state has no {field} (a non-empty string)"
    if state.get("stage") not in STAGES:
        return f"rotation state stage {state.get('stage')!r} is not one of {sorted(STAGES)}"
    rs = state.get("recipients")
    if not isinstance(rs, list) or not all(isinstance(r, str) and r for r in rs):
        return "rotation state recipients must be a list of non-empty strings"
    if len(set(rs)) != len(rs):
        return "rotation state recipients carry duplicates"
    return None


STAGES = ("ready", "listed", "accepted", "local", "completed")


def _checked_state():
    """Состояние, годное для продолжения, либо громкий отказ."""
    state = _load_op()
    if state is None:
        raise RuntimeError("no rotation state to resume")
    why = _validate(state)
    if why:
        raise RuntimeError(f"refusing to continue: {why}")
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


def _read_file_key(path):
    """Ключ из файла с различённой причиной. -> (причина, ключ | None):
    "ok" | "missing" | "empty" | "permission" | "io". Различение не для
    красоты: «нет файла» и «нет прав» -- разные решения оператора."""
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().rstrip("\r\n")
    except FileNotFoundError:
        return "missing", None
    except IsADirectoryError:
        return "io", None
    except PermissionError:
        return "permission", None
    except OSError:
        return "io", None
    if not key:
        return "empty", None
    return "ok", key


def _serviceable(state):
    """Сервисопригодность ОБЕИХ путей до remote: режим приватный, владелец
    -- сам процесс. Владелец нужен ДО первой записи: атомарный replace
    без owner под root отдаёт inode процессу, и root:0600 отрезает сервис
    от его копии. -> None | строка-блокер."""
    for what, path in (("source", state["source"]), ("copy", state["copy"])):
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError as e:
            return f"blocker: {what} file {path} is not serviceable: {e.strerror}"
        if mode & 0o077:
            return (f"blocker: {what} file {path} mode {oct(mode)} is not "
                    f"private; rotation must preserve a serviceable copy")
        owner = os.stat(path).st_uid
        if owner != os.getuid():
            return (f"blocker: {what} file {path} is owned by uid {owner}, "
                    f"not this process; an atomic write would strand the service")
    return None


def _guard(state):
    """Guard КАЖДОГО resume перед mutating-шагом: пауза на поздней стадии
    не открывает окно третьей стороне. Собственный сплит (old/new) --
    восстанавливаем; третье значение, смена владельца или режима --
    блокер. -> None | строка-блокер."""
    blocker = _serviceable(state)
    if blocker:
        return blocker
    for what, path in (("source", state["source"]), ("copy", state["copy"])):
        why, key = _read_file_key(path)
        if why != "ok":
            return f"blocker: {what} file {path} is {why}"
        if key not in (state["old"], state["new"]):
            return (f"drift: {what} file {path} holds neither the old nor "
                    f"the new key of the operation")
    return None


def _preflight(state):
    """Локальные именованные блокеры ДО любой remote-мутации: guard
    состояния на старте операции (ready). -> None | строка-блокер."""
    return _guard(state)


PUT_TAGS = ("applied", "refused")
# допустимые теги исхода шва PUT: "applied" -- записано (применение
# доказывает только probe), "refused" -- подтверждённый отказ ДО
# применения. Всё прочее из шва -- неизвестный исход, не отказ.


def _remote_step(state):
    """Шаг remote-списка: свежее чтение, объединение, типизированный исход.

    Читает список СВЕЖО (панель могла писать между шагами), пишет
    объединение прочитанного с сохранённым new -- чужие ключи не тронуты.
    Пустой прочитанный список не пишется никогда: он открывает прокси (#537,
    upstream provider.go:20-24). Управляемый old обязан быть в списке ДО
    изменения -- иначе drift, и решает оператор. -> {"ok": bool, ...} без
    ключей."""
    out = {"ok": False}
    try:
        keys = _fetch_list()
    except Exception as e:  # noqa: BLE001 -- причина шва, не его payload
        out["blocker"] = f"cannot read the key list ({type(e).__name__})"
        return out
    if not keys:
        out["blocker"] = "the read key list is empty; an empty list is never written"
        return out
    if state["old"] not in keys:
        out["blocker"] = (f"drift: the managed old key is not in the proxy list; "
                          f"restoring a revoked key is an operator decision")
        return out
    want = keys + [state["new"]] if state["new"] not in keys else keys
    try:
        verdict = _put_list(want)
    except (OSError, TimeoutError) as e:
        # Unknown-исход: PUT мог примениться. Не откатываем, не меняем
        # ключ: reconcile на resume перечитает свежий список.
        out["blocker"] = f"unknown PUT outcome ({type(e).__name__}): reconcile on resume"
        out["unknown"] = True
        return out
    tag = verdict[0] if isinstance(verdict, tuple) else None
    if tag not in PUT_TAGS:
        # Неизвестный тег шва -- исход неизвестен, не отказ ДО применения.
        out["blocker"] = (f"unknown PUT outcome (seam tag {tag!r}): "
                          f"reconcile on resume")
        out["unknown"] = True
        return out
    if tag == "refused":
        out["blocker"] = "PUT refused before applying"
        out["refused"] = True
        return out
    state["stage"] = "listed"
    _save_op(state)
    out.update({"ok": True, "stage": state["stage"]})
    return out


def _probe_step(state):
    """Шаг подтверждения применения: только probes доказывают, что прокси
    принимает ключи. Успех PUT -- запись, не применение (#537: конфиг
    применяет асинхронная горутина). Нужно всё сразу: new принят, old
    по-прежнему принят, заведомо неверный клиентский ключ отвергнут --
    «оба приняты» без негативной проверки не доказывает включённую
    авторизацию (пустой список открывает прокси). -> {"ok": bool, ...} без
    ключей."""
    try:
        accepted_new = _probe(state["new"])
    except Exception as e:  # noqa: BLE001 -- причина шва, не его payload
        return {"ok": False,
                "blocker": f"cannot probe the new key ({type(e).__name__})"}
    if not accepted_new:
        # Not-yet, не отказ: список мог сохраниться, применение ещё идёт.
        return {"ok": False,
                "blocker": "pending: the proxy does not accept the new key yet "
                           "(async apply); bounded rechecks, then pending stands"}
    try:
        accepted_old = _probe(state["old"])
    except Exception as e:  # noqa: BLE001
        return {"ok": False,
                "blocker": f"cannot probe the old key ({type(e).__name__})"}
    if not accepted_old:
        return {"ok": False,
                "blocker": "the proxy no longer accepts the old key before the "
                           "operation issued anything; distribution would strand nodes"}
    wrong = "definitely-wrong-" + state["id"]
    try:
        wrong_accepted = _probe(wrong)
    except Exception as e:  # noqa: BLE001
        return {"ok": False,
                "blocker": f"cannot probe with a wrong key ({type(e).__name__})"}
    if wrong_accepted:
        return {"ok": False,
                "blocker": "the wrong client key is accepted: client auth is off; "
                           "issuing is refused"}
    state["stage"] = "accepted"
    _save_op(state)
    return {"ok": True, "stage": state["stage"]}


def _local_step(state):
    """Локальная смена: new в источник и копию, атомарно и приватно.
    Идемпотентна: crash между двумя записями оставляет сплит «источник
    новый, копия старая» -- повтор шага заканчивает начатое, ключ тот же
    сохранённый. Владелец каждого пути берётся С ПРЕЖНЕГО файла и едет в
    write_private owner=: атомарный replace под root иначе отдал бы inode
    процессу (root:0600) и отрезал бы сервис от его копии. Режим -- 0600."""
    for path in (state["source"], state["copy"]):
        st = os.stat(path)
        fsutil.write_private(path, state["new"] + "\n", owner=(st.st_uid, st.st_gid))
    state["stage"] = "local"
    _save_op(state)
    return {"ok": True, "stage": state["stage"]}


def _deliver_step(state):
    """Раздача blob по ЗАМОРОЖЕННОМУ сохранённому списку получателей:
    ready_nodes здесь не спрашивают -- offline-узел выпал бы из списка, и
    частичная раздача закрылась бы как полная. Итог судит strict_settled:
    FAILED и NOT REACHED держат операцию открытой. Пустой сохранённый
    список -- честный «0 recipients». -> {"ok": bool, ...} без ключей."""
    nodes = list(state.get("recipients") or [])
    if not nodes:
        state["stage"] = "completed"
        _save_op(state)
        return {"ok": True, "stage": "completed", "note": "0 recipients: "
                "the saved recipient set is empty"}
    try:
        answers = _deliver(state["new"], nodes)
    except Exception as e:  # noqa: BLE001 -- причина шва, не его payload
        return {"ok": False,
                "blocker": f"delivery failed ({type(e).__name__}); "
                           f"the operation stays open"}
    # Покрытие сверяется с СОХРАНЁННЫМ списком: пустые или частичные
    # ответы не завершают раздачу, неожидаемый получатель не считается.
    # Сведение сырых ответов агентов одно: bus.results_from (#315).
    if answers and all(isinstance(v, str) for v in answers.values()):
        out = {n: answers[n] for n in nodes if n in answers}
    elif answers:
        out = bus.results_from(nodes, answers)
    else:
        out = {}
    out.update({n: "NOT REACHED: no answer" for n in nodes if n not in out})
    stray = sorted(set(answers or {}) - set(nodes))
    state["delivered"] = out
    if not strict_settled(out) or stray:
        _save_op(state)
        why = [f"{n} {r}" for n, r in sorted(out.items()) if r != "OK"]
        if stray:
            why.append("unexpected recipients in answers: " + ", ".join(stray))
        return {"ok": False, "blocker": "delivery is open: " + "; ".join(why)}
    state["stage"] = "completed"
    _save_op(state)
    return {"ok": True, "stage": "completed"}


def _deliver(key, nodes):
    """Шов раздачи: глагол write узлам (blob secrets.env). Реализация --
    оркестрация #539; библиотека шва не имеет."""
    raise NotImplementedError("the delivery seam belongs to the orchestration verb")


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
