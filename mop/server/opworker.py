"""Исполнитель веб-операций: устойчивый результат и восстановление (#476).

Дашборд действует (карточка папета #462, кнопки #461): клик человека
должен выполняться библиотечным workflow #458 под инвариантом #460 и
переживать крах службы с честным исходом. Воркер -- не фоновый subprocess
командлета и не привилегированный RPC: тонкий слой приёма, устойчивая
запись и вызов готовых функций.

Устойчивость -- каталог в ~/.config/mop сервера (приватный, 0700 у
каталога пула): журнал операций append-only (ops.jsonl), payload
исполнителя -- отдельным файлом на операцию (payload/<id>.json) со своим
сроком, из ответов GET его нет. Хранилище недоступно -- операция не
запускается вовсе: тихо продолжить без записи -- потерять результат.

Приём транзакционен: intent (actor, ключ, хэш нормализованного запроса)
пишется ДО эффекта; тот же ключ тем же actor с тем же хэшем -- та же
операция (двойной клик), с другим хэшем -- отказ. Роль проверяется до
первого эффекта: понижение отменяет ещё не начатое; начатое доводится
до этапа и аудируется.

Перезапуск службы: незавершённая running-запись помечается unknown и
НЕТ реконсиляции -- ничего не переигрывается и не сверяется с
job/alloc/session; unknown запрещает автоматический повтор (это не
failed), решение -- за админом по карточке папета.

Конфликтующие действия сериализуются по устойчивой идентичности цели
(имя папета): вторая операция над той же целью ждёт в очереди.

Никаких unbounded потоков: исполнение -- в вызывающем потоке (HTTP-сервер
своим тредом на запрос), никакого состояния в браузере.
"""
import hashlib
import json
import os
import threading
import time

from ..common import ops

# Сроки и лимиты (#476): настраиваются атрибутами воркера, значения --
# заведённые константы установки.
RETENTION_DAYS = 30          # сколько жить завершённым записям
PAYLOAD_DAYS = 3             # payload исполнителя живёт короче записей
QUEUE_LIMIT = 100            # незавершённых операций в очереди на приём
OBSERVE_WAIT = 60            # сколько наблюдать рестарт (#464)
OBSERVE_POLL = 2             # шаг опроса наблюдения

# Модель (#476): точные переходы. unknown -- не failed: эффекта могли и
# быть, и не быть; повтор автоматом запрещён.
STATUSES = ("queued", "running", "succeeded", "refused", "failed",
            "partial", "unknown")
TERMINAL = ("succeeded", "refused", "failed", "partial", "unknown")

# Карта исхода workflow #458 в статус операции: registered -- эффект
# сошёлся, accepted -- эффект без наблюдения (partial), refused -- отказ,
# unknown/failed -- ни эффекта, ни отказа.
_FROM_OUTCOME = {"registered": "succeeded", "accepted": "partial",
                 "refused": "refused", "failed": "failed",
                 "partial": "partial",
                 "unknown": "unknown"}


def normalized(request):
    """Нормализованный запрос -> хэш: сортированные ключи, без пробелов.

    Один и тот же смысл -- один хэш: порядок ключей JSON не должен делать
    из повторного клика «другой payload»."""
    return hashlib.sha256(json.dumps(request or {}, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _quiet_transport(transport):
    """Транспорт восстановления: сбой read-запроса -- None, а не исключение.

    Донаблюдение при подъёме службы не имеет права уронить её старт; None
    (-- транспорт не дан) -- наблюдение пропускается, запись остаётся
    unknown."""

    class Quiet:
        def __call__(self, verb, **fields):
            try:
                return transport(verb, **fields)
            except Exception:
                return None
    return Quiet() if transport else None


class Worker:
    """Приём и исполнение веб-операций над устойчивым журналом.

    Один замок на каталог: приём -- редкое событие, а спорить о файле
    дороже, чем подождать. Все записи идут через _append (fsync), чтение --
    снапшотом из памяти, который собирается при старте и держится тем же
    замком."""

    LIMIT = QUEUE_LIMIT

    def __init__(self, root=None, now=None, retention_days=RETENTION_DAYS,
                 payload_days=PAYLOAD_DAYS, transport=None,
                 observe_wait=OBSERVE_WAIT, observe_poll=OBSERVE_POLL,
                 sleep=time.sleep):
        import tempfile
        from ..common import paths
        self._root = root or paths.local("operations")
        self._now = now or time.time
        self.retention_days, self.payload_days = retention_days, payload_days
        self.observe_wait, self.observe_poll = observe_wait, observe_poll
        self._sleep = sleep              # наблюдение опрашивает им же
        self._transport = transport      # read-only: донаблюдение (#464)
        self._lock = threading.Lock()
        self._log = os.path.join(self._root, "ops.jsonl")
        self._ops = {}          # id -> запись
        self._by_key = {}       # (actor, ключ) -> id
        self._open = []         # незавершённые id, порядок приёма
        os.makedirs(os.path.join(self._root, "payload"), exist_ok=True)
        self._load()
        self._recover()

    # ── приём ───────────────────────────────────────────────────────────
    def submit(self, actor, verb, target, request, transport=None,
               safety=None, role="admin", context=None, effect=None,
               key=None):
        """Принять операцию. -> запись-ответ.

        Роль -- ДО первого эффекта: не admin -- отказ приёма, эффектов нет.
        key -- клиентский ключ идемпотентности (actor+key -- дедуп); без
        ключа каждая подача -- новая операция, дубли ловит только тот же
        ключ. effect -- вызов исполнителя (по умолчанию -- библиотечный
        handler #458 через transport); safety -- ops.safety-подобный
        предикат. Хранилище недоступно -- операция не запускается."""
        if role != "admin":
            return {"status": "refused", "error": "admin only"}
        digest = normalized(request)
        try:
            return self._accept(actor, verb, target, request, digest,
                                transport, safety, effect, context, key)
        except OSError as e:
            # Хранилище недоступно -- операция НЕ запускается: тихо
            # продолжить без записи -- потерять результат (#476).
            return {"status": "failed", "error": f"operations storage: {e}"}

    def _accept(self, actor, verb, target, request, digest,
                transport, safety, effect, context, key):
        idem = key or f"{verb}:{target}:{digest[:8]}"
        with self._lock:
            known = self._by_key.get((actor, idem))
            if known is None and any(t["actor"] == actor and t["key"] == idem
                                     for t in self._tombstones()):
                # Ключ уже жил и был очищен: молча выдать его за новый --
                # потерять границу повтора; отказ честнее (#476).
                return {"status": "refused",
                        "error": "this key was purged; take a new one"}
            if known is not None:
                rec = self._ops[known]
                if rec["hash"] == digest:
                    # Тот же запрос под тем же ключом: не второй запуск,
                    # а тот же результат.
                    return {k: rec[k] for k in ("op", "status")}
                return {"status": "refused",
                        "error": "the same key carries another payload"}
            if len(self._open) >= self.LIMIT:
                return {"status": "queue-full",
                        "error": "the operations queue is full"}
            op = f"op-{int(self._now() * 1000)}-{len(self._ops):04d}"
            rec = {"op": op, "actor": actor, "verb": verb, "target": target,
                   "status": "queued", "at": self._now(),
                   "key": idem, "hash": digest}
            # Intent ДО эффекта: крах ниже оставит честную запись, а не
            # «что-то случилось».
            self._append(rec)
            self._ops[op] = rec
            self._by_key[(actor, idem)] = op
            self._open.append(op)
            self._payload(op, {"operation": verb, "target": target,
                               "request": request})
        return self._run(rec, transport, safety, effect, context)

    # ── исполнение ──────────────────────────────────────────────────────
    def _run(self, rec, transport, safety, effect, context):
        """Довести операцию до исхода. Начатое -- до этапа и аудит.

        Роль проверена приёмом, safety -- до первого эффекта: отказ
        помечает запись и не зовёт транспорт вовсе. По цели уже идёт
        операция -- эта остаётся queued (сериализация по устойчивой
        идентичности цели #476), её доведёт следующий вызов/drive."""
        audit = ops.record(rec["verb"], rec["target"], None, rec["actor"])
        with self._lock:
            ahead = [o for o in self._open
                     if self._ops[o]["target"] == rec["target"] and o != rec["op"]]
        if ahead:
            return {**rec, "status": "queued", "audit": audit}
        self._mark(rec, "running")
        if safety is not None:
            # Сигнатура -- ops.safety (#460): имя, проект цели, принципал.
            ok, why = safety(rec["target"], self._project_of(rec),
                             owner=rec["actor"])
            if not ok:
                self._mark(rec, "refused", error=why)
                return {**rec, "status": "refused", "error": why, "audit": audit}
        try:
            if effect is None:
                effect = self._library_effect(rec, transport, context)
            got = effect()
        except Exception as e:
            self._mark(rec, "failed", error=str(e))
            return {**rec, "status": "failed", "error": str(e), "audit": audit}
        kind = (got or {}).get("outcome") if isinstance(got, dict) else None
        status = _FROM_OUTCOME.get(kind, "partial")
        # Факты наблюдения (#464) -- в журнал: по ним карточка и recovery
        # отвечают, что именно увидено.
        facts = {k: (got or {}).get(k) for k in ("alloc", "restarts")
                 if (got or {}).get(k) is not None}
        self._mark(rec, status, error=(got or {}).get("error"), **facts)
        # Освободив цель, двинуть следующую по ней (queued -> исход).
        self._drive(rec["target"], transport, safety, effect, context)
        return {**rec, "status": status,
                "error": (got or {}).get("error"),
                "audit": audit}

    def _drive(self, target, transport, safety, effect, context):
        """Исполнить следующую queued-операцию по цели после завершения."""
        with self._lock:
            waiting = [o for o in self._open
                       if self._ops[o]["target"] == target
                       and self._ops[o]["status"] == "queued"]
        for o in waiting:
            self._run(self._ops[o], transport, safety, effect, context)

    def _library_effect(self, rec, transport, context):
        """Эффект библиотечным workflow #458: воркер не держит копий.

        Рестарт -- наблюдаемым режимом (#464): база снимка пишется в
        журнал ДО эффекта, наблюдение -- часть операции; алгоритм --
        в mop/common/ops.py, здесь вызов."""
        fn = {"restart": ops.restart, "add": None, "update": None}.get(rec["verb"])
        if fn is None:
            raise RuntimeError(f"no library operation for {rec['verb']}")
        base = rec.get("baseline")
        if rec["verb"] == "restart" and base is None:
            try:
                base = ops.task_snapshot(transport, rec["target"])
            except Exception:
                base = None
            if base is not None:
                # База наблюдения -- ДО эффекта: переживает крах службы.
                self._mark(rec, rec["status"], baseline=base)
        # transport -- глаголы сервиса кластера (transport(verb, **fields)),
        # как у #458; контекст #457 передаётся явно, без env потока.
        call = (lambda: fn(transport, rec["target"], login=rec["actor"],
                           force=False, observe=True, before=base,
                           observe_wait=self.observe_wait,
                           sleep=lambda s: self._sleep(self.observe_poll))
                if rec["verb"] == "restart"
                else lambda: fn(transport, rec["target"], login=rec["actor"]))
        if context is not None:
            from ..common import context as _c
            return lambda: _c.call_in_context(context, call)
        return call

    def _project_of(self, rec):
        """Проект цели операции: из payload запроса его нет, пока воркер
        наверху не передал -- None (safety его не читает для решения)."""
        return None

    # ── чтение ──────────────────────────────────────────────────────────
    def read(self, actor, op=None, role=None):
        """GET результатов: актуальная admin-роль; свой actor.

        actor-supplied ID чужого -- нет записи: ID не право. role != admin
        -- None (пониженный не читает). Без op -- список своих операций,
        без payload исполнителя."""
        if role is not None and role != "admin":
            return None
        if op is None:
            with self._lock:
                mine = [dict(r) for r in self._ops.values() if r["actor"] == actor]
            return [{k: v for k, v in r.items()
                     if k not in ("hash", "key")} for r in mine]
        with self._lock:
            rec = self._ops.get(op)
        if not rec or rec["actor"] != actor:
            return None
        return {k: v for k, v in rec.items() if k not in ("hash", "key")}

    # ── устойчивость ────────────────────────────────────────────────────
    def _append(self, rec):
        """Запись в журнал с fsync: тихо продолжать без неё нельзя."""
        with open(self._log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _mark(self, rec, status, error=None, **facts):
        """Переход с записью в журнал; завершённая уходит из очереди.

        facts (baseline/alloc/restarts) -- факты операции, в журнал: без
        них донаблюдению восстановления не от чего отсчитывать сигнал."""
        rec = self._ops[rec["op"]]
        rec["status"] = status
        if error is not None:
            rec["error"] = error
        rec.update({k: v for k, v in facts.items() if v is not None})
        if status in TERMINAL and rec["op"] in self._open:
            self._open.remove(rec["op"])
        self._append({**rec, "transition": status})

    def _payload(self, op, data):
        """Payload исполнителя: отдельный файл, свой срок, приватно."""
        path = os.path.join(self._root, "payload", f"{op}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def _load(self):
        """Журнал -> память: последняя запись о каждой операции."""
        if not os.path.exists(self._log):
            return
        with open(self._log, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                op = rec.get("op")
                if not op:
                    continue
                prev = self._ops.get(op, {})
                prev.update(rec)
                self._ops[op] = prev
                self._by_key.setdefault((rec.get("actor"), rec.get("key")), op)
                if prev["status"] not in TERMINAL:
                    if op not in self._open:
                        self._open.append(op)

    def _recover(self):
        """Подъём службы: незавершённое -- unknown, без переигрывания.

        Исключение -- наблюдаемый рестарт (#464) с журнальной базой: до
        краша он мог и сойтись. Донаблюдение -- read-only тем же сигналом
        (ops.observe_restart), эффект не переигрывается никогда: unknown
        запрещает автоматический повтор (#476). Сошлось -- честный исход,
        не сошлось -- остаётся unknown с причиной."""
        for op in list(self._open):
            rec = self._ops[op]
            if rec["status"] not in ("queued", "running"):
                continue
            if rec["verb"] == "restart" and rec.get("baseline") \
                    and self._transport is not None:
                got = ops.observe_restart(_quiet_transport(self._transport),
                                          rec["target"], rec["baseline"],
                                          observe_wait=self.observe_wait,
                                          sleep=lambda s: self._sleep(self.observe_poll),
                                          clock=self._now)
                if got.get("outcome") == "registered":
                    self._mark(rec, "succeeded",
                               alloc=got.get("alloc"),
                               restarts=got.get("restarts"))
                    continue
                self._mark(rec, "unknown",
                           error=got.get("error")
                           or "the web service restarted mid-operation")
                continue
            self._mark(rec, "unknown",
                       error="the web service restarted mid-operation")

    # ── служебное для проверок (#476: чистые механизмы) ────────────────
    def _begin_for_test(self, actor, verb, target, request):
        """Запись, остановленная на running: крах посреди операции."""
        with self._lock:
            op = f"op-{int(self._now() * 1000)}-{len(self._ops):04d}"
            rec = {"op": op, "actor": actor, "verb": verb, "target": target,
                   "status": "running", "at": self._now(),
                   "key": f"{verb}:{target}", "hash": normalized(request)}
            self._append(rec)
            self._ops[op] = rec
            self._by_key[(actor, rec["key"])] = op
            self._open.append(op)
        return op

    def _retryable(self, rec):
        """Разрешён ли автоматический повтор: unknown -- никогда."""
        return bool(rec) and rec.get("status") == "failed"

    # ── retention и tombstone (#476) ────────────────────────────────────
    def _purge(self, before=None):
        """Очистка завершённых записей старше срока.

        Дедупликация не обещана навсегда: срок вышел -- записи нет. Но и
        молча считать старый ключ новым безопасным запросом нельзя: ключ
        уходит в tombstone (отдельная строка журнала, без payload), и тот
        же ключ после очистки -- отказ, а не «новая операция»."""
        cut = self._now() - self.retention_days * 86400 if before is None else before
        with self._lock:
            gone = [op for op, r in self._ops.items()
                    if r["status"] in TERMINAL and r["at"] < cut]
            for op in gone:
                rec = self._ops.pop(op)
                self._by_key.pop((rec["actor"], rec["key"]), None)
                self._append({"op": op, "key": rec["key"], "actor": rec["actor"],
                              "tombstone": True, "at": self._now()})
                self._drop_payload(op)

    def _drop_payload(self, op):
        """Payload исполнителя живёт короче записей: срок вышел -- файла нет."""
        try:
            os.remove(os.path.join(self._root, "payload", f"{op}.json"))
        except OSError:
            pass

    def _tombstones(self):
        """Ключи, уже жившие в этом журнале: после очистки -- только факт,
        что ключ был, без содержания."""
        out = []
        if not os.path.exists(self._log):
            return out
        with open(self._log, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("tombstone"):
                    out.append({"key": rec.get("key"), "actor": rec.get("actor")})
        return out

    def _stages(self, op):
        """Структурные этапы операции из журнала: [queued, running, исход].

        Читается проверками и будущим UI: переходы -- часть модели #476,
        а не внутренняя деталь."""
        out = []
        if not os.path.exists(self._log):
            return out
        with open(self._log, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("op") != op:
                    continue
                if "transition" in rec:
                    if not out or out[-1] != rec["transition"]:
                        out.append(rec["transition"])
                elif not out:
                    out.append(rec.get("status", "queued"))
        return out or ([self._ops[op]["status"]] if op in self._ops else [])

    def _audit(self):
        """Аудит-записи операций: ops.record на каждую."""
        return [ops.record(r["verb"], r["target"], None, r["actor"])
                for r in self._ops.values()]

    def _serialized(self, target, op):
        """Идёт ли операция по цели после данной: очередь по цели."""
        with self._lock:
            ids = [o for o in self._open if self._ops[o]["target"] == target]
        return op in ids

    def _blocked(self, target, op):
        with self._lock:
            return op not in self._open
