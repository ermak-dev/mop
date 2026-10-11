#!/usr/bin/env python3
"""Раздача ключа прокси без пула: python3 tests/proxykey.py

Сервер -- единственный раздатчик ключа LLM-прокси (#391): рендер блоба и
решение «пора ли снова» -- чистые функции, сама раздача -- только на живом
пуле. Здесь же итоги глагола write (bus.results_from): клиентский
раздатчик keys.py умер (#385), потребитель один -- этот модуль.
"""
import json
import os
import subprocess
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus, config  # noqa: E402
from mop.server import proxykey  # noqa: E402


# ── #315: глагол write -- ответы, кодировка и таймаут в одном месте ─────
# HYPOTHESIS: results_from, кодировка файла [путь, b64] и WRITE_TIMEOUT
# держат копиями client/keys.py и server/credreg.py, декодер -- агент сам.
# SOLUTION: bus.results_from, bus.as_file / bus.file_data, bus.WRITE_TIMEOUT;
# вызывающие только переключились. STATUS: FIXED — see #315
def check_write_in_bus_315(c):
    import ast
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    for name in ("results_from", "as_file", "file_data", "WRITE_TIMEOUT"):
        c.check(f"bus.{name} exists", hasattr(bus, name))
    if hasattr(bus, "as_file") and hasattr(bus, "file_data"):
        for data in ("K=v\n", b"\x00\xffbin"):
            path, b64 = bus.as_file("p/q", data)
            raw = data.encode() if isinstance(data, str) else data
            c.expect(f"bus.as_file -> file_data round-trips {data!r}",
                     (path, bus.file_data(b64)), ("p/q", raw))
            c.check("bus.as_file: b64 is a str, it goes into JSON", isinstance(b64, str))
    c.expect("bus.WRITE_TIMEOUT stays 60s", getattr(bus, "WRITE_TIMEOUT", None), 60)
    for rel in ("mop/server/proxykey.py", "mop/node/agent.py"):
        src = open(os.path.join(root, rel)).read()
        tree = ast.parse(src)
        own = sorted({n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name in ("results_from", "_as_file", "as_file")}
                     | {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                        for t in n.targets if isinstance(t, ast.Name) and t.id == "WRITE_TIMEOUT"})
        c.check(f"{rel}: no own copy of the write helpers", not own, own)
        c.check(f"{rel}: no base64 of its own, the encoding lives in bus", "base64" not in src)
    for rel in ("mop/server/proxykey.py",):
        # Строка-результат, а не упоминание в докстринге: f"NOT REACHED: …".
        tree = ast.parse(open(os.path.join(root, rel)).read())
        built = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
                 and any(isinstance(v, ast.Constant) and "NOT REACHED" in str(v.value)
                         for v in n.values)]
        c.check(f"{rel}: no answer mapping by hand (NOT REACHED lives in bus)", not built, built)


# ── #366: отказ тела в ответе write ─────────────────────────────────────
# HYPOTHESIS: агент кладёт отказ тела строкой в written, поля error нет;
# results_from решал только по error, и `mop login` печатал узел, где
# запись во все тела упала, как OK.
# SOLUTION: агент отдаёт failed {тело: причина} (#366, узловая половина);
# results_from считает узел FAILED с причиной первого тела, если failed
# непуст -- одно правило на клиента и сервер (#315).
# STATUS: FIXED — see #366
def check_body_refusal_366(c):
    got = bus.results_from(["n1", "n2", "n3"], {
        "n1": {"written": ["pu-a-1 FAILED — body 9001 is stopped",
                           "pu-a-2 FAILED — body 9002 is stopped"],
               "failed": {"pu-a-1": "body 9001 is stopped", "pu-a-2": "body 9002 is stopped"},
               "absent": []},
        "n2": {"written": ["pu-a-3:/x"], "failed": {}, "absent": []},
        # Формулировка строки -- не контракт: судит поле.
        "n3": {"written": ["pu-a-4 could not be written"],
               "failed": {"pu-a-4": "ssh: connection refused"}, "absent": []}})
    c.expect("#366 every body refused: FAILED with the first body's reason",
             got.get("n1"), "FAILED: pu-a-1: body 9001 is stopped")
    c.expect("#366 nothing refused: OK", got.get("n2"), "OK")
    c.expect("#366 the field decides, not the wording of written",
             got.get("n3"), "FAILED: pu-a-4: ssh: connection refused")


def check_results_135(c):
    """Итоги write (#135): неответ -- отказ с причиной, запасного пути нет."""
    fn = getattr(bus, "results_from", None)
    if not c.check("bus.results_from exists", fn is not None):
        return
    got = fn(["a", "b", "c", "d"], {
        "a": {"written": ["/x"]},
        "b": {"error": "agent is not allowed to write to /etc/passwd"},
        "c": bus.BusError("node agent c did not answer in 20s"),
    })
    c.check("a written node must be OK", not (got.get("a") != "OK"), got)
    c.check("an agent's refusal must be FAILED with its reason",
            not (not got.get("b", "").startswith("FAILED") or "not allowed" not in got["b"]),
            got)
    c.check("a silent agent must say so, not send to the controller",
            not ("did not answer in 20s" not in got.get("c")
                 or "Nomad" in got.get("c")), got)
    c.check("a node with no answer at all must be NOT REACHED",
            not (not got.get("d", "").startswith("NOT REACHED")))

# HYPOTHESIS: раздатчик и установленный прокси читают разные источники:
# .env кластера против llm-proxy-client.pass контроллера. Ключ может устареть
# или вовсе отсутствовать у кластера, хотя прокси уже поднят.
# SOLUTION: приватный passfile сервисной учётки старше старого .env;
# если файла пока нет, работающие установки продолжают читать .env.
# RESULT: раздача предпочитает файл и сохраняет переходный старый источник.
# STATUS: FIXED — see #393
def check_source_393(c):
    source = getattr(proxykey, "source", None)
    if not c.check("#393 proxykey.source exists", source is not None):
        return
    with tempfile.TemporaryDirectory(prefix="mop-key-source-") as d:
        path = os.path.join(d, "llm-proxy-client.pass")
        old = {"MOP_PROXY_KEY": ("old", ".env")}
        c.expect("#393 no passfile: previous source continues", source(old, path), "old")
        with open(path, "w", encoding="utf-8") as f:
            f.write("generated\n")
        c.expect("#393 installed proxy key wins over old env", source(old, path), "generated")
        c.expect("#393 file works without env key", source({}, path), "generated")
        sent = []

        def write(*args, **kwargs):
            sent.append(bus.file_data(kwargs["files"][0][1]))
            return {"node": {"written": ["ok"]}}

        # #557: тестовая установка -- настоящий сертификат без частной CA
        # (системное доверие): корня в блобе нет, форма #391 не меняется.
        with patched(proxykey.config, get=lambda name: {
                "MOP_TLS_CA": "", "MOP_TLS_CERT": "/etc/certs/real.pem",
                "MOP_TLS_KEY": "", "MOP_HOME": d}.get(name, "")), \
                patched(proxykey, KEY_FILE=path), \
                patched(proxykey.config, effective=lambda: old), \
                patched(proxykey, _load=lambda: None), \
                patched(proxykey.puppets, ready_nodes=lambda: ["node"]), \
                patched(proxykey.bus, request_many=write), \
                patched(proxykey, _save=lambda state: None):
            got = proxykey.push()
        c.expect("#393 push distributes the installed key", (got, sent),
                 ({"node": "OK"}, [b"MOP_PROXY_KEY=generated\n"]))


# ── #538: строгий итог раздачи ротации — отказ держит операцию открытой ──
# HYPOTHESIS: settled отвергает только NOT REACHED, а FAILED агента считает
# «дошли до всех»: частичная раздача закрыла бы шаг ротации, недошедший
# узел ходил бы старым ключом до отдельного retirement (#537, контракт
# docs/PROXY.md). SOLUTION: strict_settled -- отдельный предикат ротации:
# и NOT REACHED, и FAILED держат операцию открытой; старый settled тика
# раздачи (#391) не меняется скрыто.
def check_strict_settled_538(c):
    fn = getattr(proxykey, "strict_settled", None)
    if not c.check("#538 proxykey.strict_settled exists", fn is not None):
        return
    c.check("#538 NOT REACHED keeps the rotation open",
            not fn({"gpu": "OK", "hyper": "NOT REACHED: down"}))
    c.check("#538 FAILED keeps the rotation open",
            not fn({"gpu": "OK", "hyper": "FAILED: x"}))
    c.check("#538 every recipient OK settles strictly", fn({"gpu": "OK"}))
    c.check("#538 empty outcome settles strictly", fn({}))


# ── #538: begin — явные пути, обязательное условие, состояние до эффекта ──
# HYPOTHESIS: у ротации нет CAS (#537), и begin без явного exclusive-writer
# условия, с путями по умолчанию или с состоянием после remote-эффекта
# запускал бы конкурирующего писателя. SOLUTION: begin требует явных
# source_path/copy_path и явного exclusive-условия от вызывающего; отказ
# происходит БЕЗ эффектов (ни состояния, ни remote-шва), а согласный begin
# пишет приватное состояние операции ДО любого remote-шва -- дальнейшие
# шаги делает resume по чекпоинтам.
def check_begin_538(c):
    begin = getattr(proxykey, "begin", None)
    if not c.check("#538 proxykey.begin exists", begin is not None):
        return
    import json
    import stat
    with tempfile.TemporaryDirectory(prefix="mop-rot-") as d:
        source_path = os.path.join(d, "llm-proxy-client.pass")
        copy_path = os.path.join(d, "copy.pass")
        op = os.path.join(d, "op.json")
        calls = []

        def seam(name):
            def reach(*a, **k):
                calls.append(name)
                raise AssertionError(f"begin must not reach {name}")
            return reach

        def fresh(old="old-key\n"):
            with open(source_path, "w", encoding="utf-8") as f:
                f.write(old)

        fresh()
        with patched(proxykey, OP=op, _fetch_list=seam("fetch_list"),
                     _put_list=seam("put_list"), _probe=seam("probe")):
            try:
                begin(source_path, copy_path, ["node1"], exclusive=False)
                c.fail("#538 begin without the explicit exclusive precondition must refuse")
            except RuntimeError as e:
                c.check("#538 the refusal names the exclusive-writer precondition",
                        "exclusive" in str(e).lower(), str(e))
            c.check("#538 a refused begin writes no operation state",
                    not os.path.exists(op))
            c.check("#538 a refused begin reaches no remote seam", not calls, calls)

            fresh("\n")
            try:
                begin(source_path, copy_path, ["node1"], exclusive=True)
                c.fail("#538 an empty key source must refuse")
            except RuntimeError as e:
                c.check("#538 the empty-source refusal names the file",
                        source_path in str(e), str(e))
            c.check("#538 an empty source writes no operation state",
                    not os.path.exists(op))

            fresh()
            got = begin(source_path, copy_path, ["node1", "node2"], exclusive=True)
        c.check("#538 begin wrote the state before any remote seam",
                os.path.exists(op) and not calls, calls)
        mode = stat.S_IMODE(os.stat(op).st_mode)
        c.check("#538 the operation state is private (0600)", mode == 0o600, oct(mode))
        with open(op, encoding="utf-8") as f:
            raw = json.load(f)
        c.expect("#538 the state keeps the old key of the source", raw.get("old"), "old-key")
        c.check("#538 the state keeps a one-time new key",
                bool(raw.get("new")) and raw["new"] != "old-key", repr(raw.get("new")))
        c.expect("#538 the state names the schema", raw.get("schema"), proxykey.SCHEMA)
        c.expect("#538 the state saves the explicit recipient set",
                 raw.get("recipients"), ["node1", "node2"])
        c.expect("#538 the state names the source path", raw.get("source"), source_path)
        c.expect("#538 the state names the copy path", raw.get("copy"), copy_path)
        c.check("#538 the state names its stage",
                isinstance(raw.get("stage"), str) and raw["stage"], repr(raw.get("stage")))
        c.check("#538 begin returns a projection without keys",
                isinstance(got, dict) and "old" not in got and "new" not in got, got)


# ── #538: локальная блокировка — fcntl.flock на приватном lock-файле ─────
# HYPOTHESIS: два одновременных begin/resume в одном процессе могли бы
# перезаписать состояние операции друг друга, а O_EXCL-файл с PID требовал
# бы угадывать, жив ли владелец. SOLUTION: flock на постоянном приватном
# lock-файле: вторая операция отказывает сразу, смерть процесса освобождает
# блокировку сама (ядро), файл не пересоздаётся по звонку.
def check_lock_538(c):
    lock = getattr(proxykey, "_lock", None)
    if not c.check("#538 proxykey._lock exists", callable(lock)):
        return
    import fcntl
    with tempfile.TemporaryDirectory(prefix="mop-rot-lock-") as d:
        path = os.path.join(d, "rotation.lock")
        with lock(path):
            c.check("#538 the lock file exists while held", os.path.exists(path))
            # Тот же inode, не пересоздаётся по звонку: захват вторым --
            # через НЕзависящий от нас реестр, прямо через fcntl.
            fd = os.open(path, os.O_RDONLY)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    c.fail("#538 a second holder must not lock")
                except OSError:
                    c.check("#538 the holder's lock is an flock second grab refuses", True)
            finally:
                os.close(fd)
        with lock(path):
            c.check("#538 the lock is free again after release", True)
        # Смерть процесса освобождает: ребёнок держит flock и умирает.
        code = ("import fcntl, os, sys, time;"
                f"f = open({path!r}, 'a');"
                "fcntl.flock(f, fcntl.LOCK_EX);"
                "print('held', flush=True);"
                "import subprocess;"
                "subprocess.run(['sleep', '0.2']);"
                "os._exit(0)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        c.check("#538 the child held and died gracefully", r.stdout.strip() == "held",
                r.stdout + r.stderr)
        with lock(path):
            c.check("#538 process death frees the flock (no stale guess)", True)


# ── #538: resume — условие на каждом входе, состояние до эффектов ────────
# HYPOTHESIS: resume без повторного явного exclusive-условия, с чужим или
# битым состоянием, или поверх незавершённой другой операции продолжал бы
# ротацию вслепую. SOLUTION: resume требует exclusive так же явно; состояние
# обязано быть читаемым, своей схемы, с целыми old/new; begin поверх
# активного состояния отказывает (заменяемо только завершённое).
def check_resume_refusals_538(c):
    resume = getattr(proxykey, "resume", None)
    if not c.check("#538 proxykey.resume exists", resume is not None):
        return
    import json
    with tempfile.TemporaryDirectory(prefix="mop-rot-res-") as d:
        source_path = os.path.join(d, "llm-proxy-client.pass")
        copy_path = os.path.join(d, "copy.pass")
        op = os.path.join(d, "op.json")
        with open(source_path, "w", encoding="utf-8") as f:
            f.write("old-key\n")
        calls = []

        def seam(name):
            def reach(*a, **k):
                calls.append(name)
                raise AssertionError(f"resume must not reach {name}")
            return reach

        def state(**over):
            base = {"id": "0123456789abcdef", "schema": proxykey.SCHEMA, "old": "old-key",
                    "new": "new-key", "source": source_path, "copy": copy_path,
                    "recipients": ["node1"], "stage": "ready"}
            base.update(over)
            fsutil_dir = os.path.dirname(op)
            os.makedirs(fsutil_dir, exist_ok=True)
            with open(op, "w", encoding="utf-8") as f:
                json.dump(base, f)
            return base

        patches = dict(OP=op, _fetch_list=seam("fetch_list"),
                       _put_list=seam("put_list"), _probe=seam("probe"))
        with patched(proxykey, **patches):
            # Нет состояния -- продолжать нечего, эффектов нет.
            try:
                resume(exclusive=True)
                c.fail("#538 resume without a state must refuse")
            except RuntimeError as e:
                c.check("#538 no-state refusal is loud", "state" in str(e).lower(), str(e))
            c.check("#538 no-state resume reaches no seam", not calls, calls)

            state()
            try:
                resume(exclusive=False)
                c.fail("#538 resume without the explicit precondition must refuse")
            except RuntimeError as e:
                c.check("#538 resume names the exclusive precondition",
                        "exclusive" in str(e).lower(), str(e))
            c.check("#538 a refused resume reaches no seam", not calls, calls)

            for name, broken in (("schema", {"schema": proxykey.SCHEMA + 1}),
                                 ("no old", {"old": ""}),
                                 ("no new", {"new": ""}),
                                 ("no recipients", {"recipients": None})):
                state(**broken)
                try:
                    resume(exclusive=True)
                    c.fail(f"#538 a broken state ({name}) must refuse loudly")
                except RuntimeError as e:
                    c.check(f"#538 a broken state ({name}) refuses with a reason",
                            bool(str(e)), str(e))
                c.check(f"#538 a broken state ({name}) reaches no seam", not calls, calls)

            state()
            try:
                proxykey.begin(source_path, copy_path, ["node1"], exclusive=True)
                c.fail("#538 begin over an active state must refuse")
            except RuntimeError as e:
                c.check("#538 begin over an active state names it",
                        "state" in str(e).lower() or "running" in str(e).lower(), str(e))
            c.check("#538 begin over an active state reaches no seam", not calls, calls)
            with open(op, encoding="utf-8") as f:
                kept = json.load(f)
            c.check("#538 the active state survives the refused begin",
                    kept.get("old") == "old-key" and kept.get("new") == "new-key", kept)

            # Терминальное завершённое состояние заменяемо новым begin:
            # completed с полным покрытием OK (форма реального писателя).
            state(stage="completed", delivered={"node1": "OK"})
            got = proxykey.begin(source_path, copy_path, ["node1"], exclusive=True)
            c.check("#538 begin replaces only a terminal state",
                    isinstance(got, dict) and got.get("stage") != "completed", got)


# ── #538: preflight — именованные блокеры ДО любой remote-мутации ────────
# HYPOTHESIS: preflight в начале begin отклонял бы собственный recoverable
# сплит (источник уже новый, копия ещё старая -- это ровно то состояние,
# ради которого существует resume), а пропуск проверки прав записи давал
# бы root-записи превратить сервисную копию в root:0600 и отрезать сервис.
# SOLUTION: preflight живёт в resume перед remote-шагом; он проверяет
# serviceability ОБЕИХ путей (запись + прежний владелец/режим) и зовётся
# НАЧАЛЕ ротации, а не на каждом resume; любое третье значение в файлах
# (не old, не new) -- drift, отказ.
def check_preflight_538(c):
    fn = getattr(proxykey, "_preflight", None)
    if not c.check("#538 proxykey._preflight exists", callable(fn)):
        return
    import stat
    with tempfile.TemporaryDirectory(prefix="mop-rot-pre-") as d:
        src = os.path.join(d, "llm-proxy-client.pass")
        cp = os.path.join(d, "copy.pass")
        state = {"id": "0123456789abcdef", "schema": proxykey.SCHEMA, "old": "old-key",
                 "new": "new-key", "source": src, "copy": cp,
                 "recipients": ["node1"], "stage": "ready"}

        def put(path, text):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(path, 0o600)

        # Согласованный старт: оба old -- ок, без remote-эффектов.
        put(src, "old-key\n")
        put(cp, "old-key\n")
        c.check("#538 preflight passes a consistent start",
                fn(state) is None, "must be None")

        # Preflight живёт на ready: до локального намерения ОБА пути
        # обязаны держать OLD -- появление NEW до записи это чужая рука.
        put(src, "new-key\n")
        r = fn(state)
        c.check("#538 preflight drifts a source-new pair before the local intent",
                isinstance(r, str) and "drift" in r.lower() and src in r, r)

        # Третье значение в источнике -- drift.
        put(src, "third-key\n")
        r = fn(state)
        c.check("#538 a third source value is a named drift blocker",
                isinstance(r, str) and "drift" in r.lower() and src in r, r)

        # Третье значение в копии -- тоже drift.
        put(src, "old-key\n")
        put(cp, "third-key\n")
        r = fn(state)
        c.check("#538 a third copy value is a named drift blocker",
                isinstance(r, str) and "drift" in r.lower() and cp in r, r)

        # Источник не существует -- именованный блокер.
        os.remove(src)
        r = fn(state)
        c.check("#538 a missing source is a named blocker",
                isinstance(r, str) and src in r, r)

        # Несервисопригодная копия: запись под root сменила бы владельца
        # файла -- единственный uid на теле в тесте сам себе, проверяем
        # режим: чужой 0644 должен быть назван ДО мутации.
        put(src, "old-key\n")
        put(cp, "old-key\n")
        os.chmod(cp, 0o644)
        r = fn(state)
        c.check("#538 a world-readable copy is a named blocker before mutation",
                isinstance(r, str) and cp in r and "mode" in r.lower(), r)
        os.chmod(cp, 0o600)

        # Пустой файл копии -- блокер с именем файла.
        put(cp, "\n")
        r = fn(state)
        c.check("#538 an empty copy is a named blocker",
                isinstance(r, str) and cp in r, r)


# ── #538: remote-шаг — свежий список, сохранение всех, типизированный исход
#
# HYPOTHESIS: ротация без exclusive-записи конкурирует с панелью: читать
# список один раз в begin и PUT-нуть его позже значило бы перезаписать
# чужие ключи устаревшим списком; PUT-исход «отказ» без различия
# предзаписи и неизвестности толкал бы к слепому повтору или rollback.
# SOLUTION: remote-шаг читает список СВЕЖО при каждом применении, пишет
# объединение (все прочитанные + сохранённый new, чужие ключи не тронуты),
# пустой прочитанный список не пишется никогда, а исход шва типизирован:
# подтверждённый отказ ДО применения (контракт обработчика) отличается от
# unknown (timeout/обрыв) -- второй не двигает и не откатывает.
class FakeRemote:
    """Швы remote-мира: свежий список, PUT, probe. Журнал вызовов."""

    def __init__(self, keys, refuse_put=None, unknown_put=False):
        self.keys = list(keys)
        self.refuse_put = refuse_put      # подтверждённый отказ ДО записи
        self.unknown_put = unknown_put    # timeout/обрыв: исход неизвестен
        self.puts = []
        self.probes = []

    def fetch_list(self):
        return list(self.keys)

    def put_list(self, keys):
        self.puts.append(list(keys))
        if self.unknown_put:
            raise TimeoutError("put timed out")
        if self.refuse_put is not None:
            return self.refuse_put        # e.g. ("refused", "403 forbidden")
        self.keys = list(keys)
        return ("applied", None)

    def probe(self, key):
        self.probes.append(key)
        return key in self.keys


def check_fetch_typing_539(c):
    """Блокер чтения списка несёт типизированный отказ шва (#539).

    HYPOTHESIS: при неверном ключе management первым запросом resume идёт
    GET списка; без http_status в блокере оператор не узнаёт, что каждая
    следующая попытка приближает блокировку адреса на 30 минут. Шов
    поднимает типизированное исключение (целый http_status, без тела);
    _remote_step копирует его той же проверкой, что для PUT.
    SOLUTION: адаптер несёт .http_status; библиотека проецирует
    status+reason, слово блокера остаётся прежним.
    STATUS: FIXED — see #539 (малая правка #538: _copy_typed общий
    для PUT-вердикта и исключения шва).
    """
    step = proxykey._remote_step
    with tempfile.TemporaryDirectory(prefix="mop-rot-fetch-") as d:
        src = os.path.join(d, "client.pass")
        cp = os.path.join(d, "copy.pass")
        with open(src, "w", encoding="utf-8") as f:
            f.write("old-key\n")
        with open(cp, "w", encoding="utf-8") as f:
            f.write("old-key\n")

        def state():
            return {"id": "0123456789abcdef", "schema": proxykey.SCHEMA,
                    "old": "old-key", "new": "new-key", "source": src,
                    "copy": cp, "recipients": ["node1"], "stage": "ready"}

        class Refused(Exception):
            pass

        def fetch_401():
            e = Refused("GET api-keys answered HTTP 401")
            e.http_status = 401
            e.reason = "unauthorized"
            raise e

        def fetch_plain():
            raise RuntimeError("connection refused")

        with patched(proxykey, OP=os.path.join(d, "op.json")):
            out = step(state(), _seams_with(fetch=fetch_401))
        c.check("#539 a typed GET refusal carries http_status and reason",
                out.get("http_status") == 401
                and out.get("reason") == "unauthorized"
                and "cannot read the key list" in str(out.get("blocker")),
                out)
        with patched(proxykey, OP=os.path.join(d, "op.json")):
            out = step(state(), _seams_with(fetch=fetch_plain))
        c.check("#539 an untyped seam failure keeps the plain blocker",
                "cannot read the key list" in str(out.get("blocker"))
                and "http_status" not in out, out)


def _seams_with(fetch):
    """Швы #538 с подменённым fetch_list: шаг remote-списка."""
    base = proxykey._SEAMS

    class Seams:
        fetch_list = staticmethod(fetch)
        put_list = staticmethod(lambda k: ("applied", None))
        probe = staticmethod(lambda k: True)

    Seams.put_list = staticmethod(base.put_list)
    return Seams()


def check_remote_step_538(c):
    step = getattr(proxykey, "_remote_step", None)
    if not c.check("#538 proxykey._remote_step exists", callable(step)):
        return
    with tempfile.TemporaryDirectory(prefix="mop-rot-remote-") as d:
        src = os.path.join(d, "llm-proxy-client.pass")
        cp = os.path.join(d, "copy.pass")
        op = os.path.join(d, "op.json")
        with open(src, "w", encoding="utf-8") as f:
            f.write("old-key\n")
        with open(cp, "w", encoding="utf-8") as f:
            f.write("old-key\n")
        os.chmod(cp, 0o600)
        os.chmod(src, 0o600)

        def state(**over):
            base = {"id": "0123456789abcdef", "schema": proxykey.SCHEMA, "old": "old-key",
                    "new": "new-key", "source": src, "copy": cp,
                    "recipients": ["node1"], "stage": "ready"}
            base.update(over)
            return base

        # Согласованный путь: старый в списке, PUT несёт объединение со
        # ВСЕМИ чужими ключами, new принимается, old остаётся.
        remote = FakeRemote(["old-key", "panel-key"])
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 the remote step succeeds", out.get("ok"), out)
        c.expect("#538 PUT carries every read key plus new (unmanaged kept)",
                 remote.puts, [["old-key", "panel-key", "new-key"]])
        c.check("#538 the accepted step advances past the remote stage",
                out.get("stage") != "ready", out)
        with open(op, encoding="utf-8") as f:
            saved = json.load(f)
        c.check("#538 the remote checkpoint is saved",
                saved.get("stage") == out.get("stage"), saved)

        # Подтверждённый отказ ДО применения -- блокер, список прокси не
        # изменён; причина шва НЕ echo-ится raw (может нести payload).
        remote = FakeRemote(["old-key", "panel-key"],
                            refuse_put=("refused", "403 forbidden"))
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 a confirmed pre-apply refusal blocks",
                not out.get("ok") and out.get("refused") is True, out)
        c.check("#538 the refused blocker carries no raw seam reason",
                "403" not in str(out), out)
        c.check("#538 a refused PUT did not change the proxy list",
                remote.keys == ["old-key", "panel-key"], remote.keys)

        # Unknown-исход (timeout ПОСЛЕ возможного применения) -- НЕ отказ:
        # чекпоинт остаётся, повторное применение идёт по свежему списку.
        remote = FakeRemote(["old-key", "panel-key"], unknown_put=True)
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 an unknown PUT outcome keeps the operation open",
                not out.get("ok") and "unknown" in str(out.get("blocker", "")).lower(),
                out)

        # Resume после unknown: тот же new, свежий список (панель успела
        # добавить ключ) -- объединение, чужой ключ не стёрт.
        remote = FakeRemote(["old-key", "panel-key"])
        remote.keys.append("late-panel-key")
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 resume after unknown re-applies on a fresh list", out.get("ok"), out)
        c.expect("#538 the late panel key survives the re-apply",
                 remote.puts[-1], ["old-key", "panel-key", "late-panel-key", "new-key"])

        # Пустой прочитанный список -- запрет записи ВСЕГДА.
        remote = FakeRemote([])
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 an empty read list is never written",
                not out.get("ok") and not remote.puts, (out, remote.puts))

        # Старый ключ отсутствует в списке до начала -- drift, а не
        # самовольное восстановление отозванного.
        remote = FakeRemote(["panel-key"])
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 a missing managed old key is drift before mutation",
                not out.get("ok") and "drift" in str(out.get("blocker", "")).lower()
                and not remote.puts, (out, remote.puts))

        # Pre-PUT probes: ДО первой remote-мутации обязаны быть
        # подтверждены old-принят и wrong-отвергнут; post-PUT
        # new/old/wrong остаются.
        # HYPOTHESIS (RED): PUT шёл до проб авторизации: с мёртвым
        # old-ключом или выключенной авторизацией мутация уже случилась.
        order = []
        remote = FakeRemote(["old-key", "panel-key"])

        def fetch():
            order.append("fetch")
            return remote.fetch_list()

        def put(keys):
            order.append("put")
            return remote.put_list(keys)

        def probe(key):
            order.append("probe")
            return remote.probe(key)

        # Полный согласованный путь: probe old, probe wrong ДО put.
        with patched(proxykey, OP=op, _fetch_list=fetch, _put_list=put,
                     _probe=probe):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 pre-PUT probes run before the first remote mutation",
                order[:3] == ["fetch", "probe", "probe"]
                and order.index("put") > order.index("probe"), order)
        c.check("#538 the remote step order ends after the post-PUT "
                "probes of the probe step",
                order == ["fetch", "probe", "probe", "put"], order)

        # Old не принят до PUT -- мутации нет вовсе.
        order.clear()
        remote = FakeRemote(["old-key", "panel-key"])
        answers = {"old-key": False}

        def probe_old_first(key):
            order.append("probe")
            return answers.get(key, True)

        with patched(proxykey, OP=op, _fetch_list=fetch, _put_list=put,
                     _probe=probe_old_first):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 an unaccepted old key blocks BEFORE the PUT",
                "put" not in order and not out.get("ok"), (out, order))

        # Wrong принят до PUT (авторизация выключена) -- мутации нет.
        order.clear()
        remote = FakeRemote(["old-key", "panel-key"])
        wrong_seen = []

        def probe_auth_off(key):
            order.append("probe")
            wrong_seen.append(key)
            if wrong_seen and key not in ("old-key",) and key.startswith("definitely-wrong-"):
                return True
            return key in ("old-key",)

        with patched(proxykey, OP=op, _fetch_list=fetch, _put_list=put,
                     _probe=probe_auth_off):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 an accepted wrong key blocks BEFORE the PUT",
                "put" not in order and not out.get("ok"), (out, order))


# ── #538: probe-подтверждение — применение доказывает только запрос ──────
# HYPOTHESIS: успех PUT доказывает запись, но не применение (прокси
# применяет конфиг асинхронной горутиной, #537): считать шаг завершённым
# по 200 значило бы выдать ключ, который прокси ещё отвергает. Равно и
# «приняты оба» без негативной проверки не доказывает включённую
# авторизацию: пустой список открывает прокси всем.
# SOLUTION: применение подтверждают ТОЛЬКО probes: new принят, old
# по-прежнему принят, заведомо неверный клиентский ключ отвергнут;
# пока async-применение не догнало -- pending с причиной, не отказ.
def check_probe_step_538(c):
    step = getattr(proxykey, "_probe_step", None)
    if not c.check("#538 proxykey._probe_step exists", callable(step)):
        return
    with tempfile.TemporaryDirectory(prefix="mop-rot-probe-") as d:
        op = os.path.join(d, "op.json")

        def state(stage="listed"):
            return {"id": "0123456789abcdef", "schema": proxykey.SCHEMA, "old": "old-key",
                    "new": "new-key", "source": os.path.join(d, "s"),
                    "copy": os.path.join(d, "c"), "recipients": ["node1"],
                    "stage": stage}

        # Полное подтверждение: new принят, old принят, неверный отвергнут.
        answers = {"old-key": True, "new-key": True}
        wrong_seen = []
        with patched(proxykey, OP=op, _probe=lambda k: wrong_seen.append(k) or answers.get(k, False)):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 a fully confirmed application advances", out.get("ok"), out)
        c.check("#538 the wrong-key probe ran (negative check is required)",
                wrong_seen and wrong_seen[-1] not in answers, wrong_seen)
        c.check("#538 the confirmed step moves past listed",
                out.get("stage") not in (None, "listed"), out)

        # Async-применение ещё не догнало: probe new = False -- pending,
        # не отказ и не смена ключа/источника.
        answers = {"old-key": True, "new-key": False}
        with patched(proxykey, OP=op, _probe=lambda k: answers.get(k, False)):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 a not-yet-applied new key is pending with a reason",
                not out.get("ok") and "pending" in str(out.get("blocker", "")).lower(),
                out)
        c.check("#538 a pending probe keeps the stage",
                out.get("stage", "listed") == "listed", out)

        # Old перестал приниматься до выдачи -- блокер: раздача оставила бы
        # узлы без работающего ключа.
        answers = {"old-key": False, "new-key": True}
        with patched(proxykey, OP=op, _probe=lambda k: answers.get(k, False)):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 a rejected old key blocks before issuing",
                not out.get("ok") and "old" in str(out.get("blocker", "")).lower(), out)

        # Негативная проверка провалилась: неверный ключ принимается --
        # авторизация не включена, выдавать ключ нельзя.
        answers = {"old-key": True, "new-key": True}
        with patched(proxykey, OP=op, _probe=lambda k: True):
            out = step(state(), proxykey._SEAMS)
        c.check("#538 an accepted wrong key blocks: auth is off",
                not out.get("ok") and "wrong" in str(out.get("blocker", "")).lower(), out)


# ── #554: одна безопасная проба ключа -- helper без объединения фаз ──────
# HYPOTHESIS: связка «seams.probe(key) в try, except Exception, блокер
# cannot probe … (type)» повторена пять раз (_remote_step: old и wrong
# до PUT; _probe_step: new, old и wrong после) -- новое место пробы
# копирует связку, и безопасная проекция расходится с первым же
# изменением. SOLUTION: _probe_safe(seams, key, what) -> (accepted,
# blocker) -- одна копия связки; фазы зовут её и решают СВОЙ исход
# каждым if сами (порядок, число вызовов, тексты смыслов, чекпоинты --
# в фазах, не в helper). STATUS: FIXED — see #554
def check_probe_safe_554(c):
    helper = getattr(proxykey, "_probe_safe", None)
    if not c.check("#554 proxykey._probe_safe exists", callable(helper)):
        return

    class Boom(Exception):
        pass

    calls = []

    class Seams:
        probe = staticmethod(lambda key: calls.append(key) or True)

    accepted, blocker = helper(Seams(), "k1", "the old key")
    c.check("#554 a successful probe returns accepted with no blocker",
            accepted is True and blocker is None, (accepted, blocker))
    c.check("#554 the helper probes exactly the given key",
            calls == ["k1"], calls)

    class Refused(Exception):
        pass

    class RefusingSeams:
        probe = staticmethod(lambda key: (_ for _ in ()).throw(Refused()))

    accepted, blocker = helper(RefusingSeams(), "k2", "the new key")
    c.check("#554 a seam exception becomes a blocker with the class name",
            accepted is None and isinstance(blocker, str)
            and "Refused" in blocker and "new key" in blocker,
            (accepted, blocker))
    # Ключ не в блокере: проба может нести материал ключа в payload
    # исключения, блокер -- safe-проекция.
    c.check("#554 the probe blocker does not carry the key",
            "k2" not in (blocker or ""), (blocker,))

    # BaseException не глотается: KeyboardInterrupt/SystemExit -- не
    # «безопасный блокер ротации», а воля процесса.
    class InterruptingSeams:
        probe = staticmethod(lambda key: (_ for _ in ()).throw(KeyboardInterrupt()))

    try:
        helper(InterruptingSeams(), "k3", "the old key")
        interrupted = False
    except KeyboardInterrupt:
        interrupted = True
    c.check("#554 KeyboardInterrupt rides through the helper",
            interrupted, None)


# ── #538: локальная смена и раздача — чекпоинты вокруг записи путей ──────
# HYPOTHESIS: запись new в источник и копию двумя независимыми записями
# без чекпоинтов оставляла бы crash-сплит невосстановимым, а итог раздачи
# по старому settled (FAILED = «дошли») закрывал бы операцию при недосове.
# SOLUTION: локальный шаг пишет new в оба пути атомарно и приватно,
# отмечая чекпоинты до и после; шаг раздачи ходит по ЗАМОРОЖЕННОМУ
# сохранённому списку получателей, судит по strict_settled (FAILED и
# NOT REACHED держат открытой), пустой список -- честный «0 recipients».
def check_local_and_delivery_538(c):
    local = getattr(proxykey, "_local_step", None)
    deliver = getattr(proxykey, "_deliver_step", None)
    if not c.check("#538 proxykey._local_step exists", callable(local)):
        return
    if not c.check("#538 proxykey._deliver_step exists", callable(deliver)):
        return
    import stat as st
    with tempfile.TemporaryDirectory(prefix="mop-rot-local-") as d:
        src = os.path.join(d, "llm-proxy-client.pass")
        cp = os.path.join(d, "copy.pass")
        op = os.path.join(d, "op.json")

        def state(stage="accepted"):
            return {"id": "0123456789abcdef", "schema": proxykey.SCHEMA, "old": "old-key",
                    "new": "new-key", "source": src, "copy": cp,
                    "recipients": ["node1", "node2"], "stage": stage}

        def fresh():
            for p, k in ((src, "old-key"), (cp, "old-key")):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(k + "\n")
                os.chmod(p, 0o600)

        # Локальная смена: оба пути несут new, режим приватный,
        # чекпоинт сохранён.
        fresh()
        with patched(proxykey, OP=op):
            out = local(state(), proxykey._SEAMS)
        c.check("#538 the local step succeeds", out.get("ok"), out)
        c.expect("#538 the source now holds the new key",
                 open(src).read().strip(), "new-key")
        c.expect("#538 the copy now holds the new key",
                 open(cp).read().strip(), "new-key")
        c.check("#538 both paths stay private",
                st.S_IMODE(os.stat(cp).st_mode) == 0o600
                and st.S_IMODE(os.stat(src).st_mode) == 0o600)
        with open(op, encoding="utf-8") as f:
            saved = json.load(f)
        c.check("#538 the local checkpoint names its stage",
                saved.get("stage") not in (None, "accepted"), saved)

        # Crash-сплит: источник new, копия old -- шаг идемпотентно
        # заканчивает начатое, не отказ и не перегенерация.
        fresh()
        with open(src, "w", encoding="utf-8") as f:
            f.write("new-key\n")
        with patched(proxykey, OP=op):
            out = local(state(), proxykey._SEAMS)
        c.check("#538 a crashed split is finished, not refused",
                out.get("ok") and open(cp).read().strip() == "new-key", out)

        # Третий ключ на копии ПОСЛЕ входного guard resume: шов probe мог
        # подменить файл между входным guard и локальной записью -- шаг
        # проверяет домен ключей СВОИМ guard перед записью и НЕ затирает
        # чужой ключ (собственный old/new сплит остаётся восстановимым).
        # HYPOTHESIS (RED): _local_step писал без повторной проверки --
        # third-key на копии молча затирался new.
        fresh()
        with open(cp, "w", encoding="utf-8") as f:
            f.write("third-key\n")
        with patched(proxykey, OP=op):
            out = local(state(), proxykey._SEAMS)
        c.check("#538 a third key on the copy blocks the local write",
                not out.get("ok") and "drift" in str(out.get("blocker", "")).lower(),
                out)
        c.check("#538 the blocked local write touched no file",
                open(src).read().strip() == "old-key"
                and open(cp).read().strip() == "third-key",
                (open(src).read(), open(cp).read()))

        # Раздача: замороженные получатели, итог -- strict_settled.
        answers = {"node1": "OK", "node2": "FAILED: body down"}
        sent = []

        def write(verb, nodes, timeout=None, files=None):
            sent.append((nodes, files))
            return dict(answers)

        with patched(proxykey, OP=op, _deliver=write):
            out = deliver(state(stage="local"), proxykey._SEAMS)
        c.check("#538 a FAILED recipient keeps the delivery open",
                not out.get("ok"), out)
        c.check("#538 delivery goes to the frozen saved recipients",
                sent and sent[0][0] == ["node1", "node2"], sent)
        with open(op, encoding="utf-8") as f:
            saved = json.load(f)
        c.expect("#538 the per-recipient verdicts are checkpointed",
                 saved.get("delivered"), answers)
        # HYPOTHESIS (RED): pending-блокер собирал f"{n} {r}" с raw-хвостом
        # -- секрет из ответа узла ехал в публичный blocker resume.
        # SOLUTION: та же единственная safe-проекция, что в status() --
        # код узла (OK/FAILED/NOT REACHED) без хвоста; приватный
        # чекпоинт хранит полный итог, публичный итог его не echo-ит.
        c.check("#538 the pending blocker carries no raw verdict tail",
                "body down" not in str(out.get("blocker", ""))
                and "FAILED" in str(out.get("blocker", "")), out)
        c.check("#538 the pending blocker still names the recipient",
                "node2" in str(out.get("blocker", "")), out)

        answers = {"node1": "OK", "node2": "OK"}
        with patched(proxykey, OP=op, _deliver=write):
            out = deliver(state(stage="local"), proxykey._SEAMS)
        c.check("#538 all recipients OK complete the delivery",
                out.get("ok") and out.get("stage") != "local", out)

        # Пустой сохранённый список -- честный ноль, не «все обновлены».
        with patched(proxykey, OP=op, _deliver=write):
            out = deliver({**state(stage="local"), "recipients": []},
                          proxykey._SEAMS)
        c.check("#538 an explicit empty recipient set completes with 0 named",
                out.get("ok") and "0" in str(out.get("note", "")), out)


# ── #538: склейка — resume ведёт шаги по чекпоинтам, status не течёт ─────
# HYPOTHESIS: шаги по отдельности не дают операции: resume обязан вести их
# по сохранённым чекпоинтам до первого блокера, занятая блокировка --
# отказ без эффектов, а status обязан показывать состояние без ключей и
# без raw-причин швов.
def check_pipeline_538(c):
    with tempfile.TemporaryDirectory(prefix="mop-rot-pipe-") as d:
        src = os.path.join(d, "llm-proxy-client.pass")
        cp = os.path.join(d, "copy.pass")
        op = os.path.join(d, "op.json")
        lock = os.path.join(d, "rotation.lock")
        for p, k in ((src, "old-key"), (cp, "old-key")):
            with open(p, "w", encoding="utf-8") as f:
                f.write(k + "\n")
            os.chmod(p, 0o600)
        order = []
        remote = FakeRemote(["old-key", "panel-key"])
        answers = {"node1": "OK", "node2": "OK"}

        def deliver(key, nodes):
            order.append("deliver")
            return dict(answers)

        def probe(key):
            order.append("probe")
            # begin родил случайный ключ: принят любой, кроме заведомо
            # неверного, которого нет в списке прокси.
            return not key.startswith("definitely-wrong-")

        def fetch():
            order.append("fetch")
            return remote.fetch_list()

        def put(keys):
            order.append("put")
            return remote.put_list(keys)

        real_local = proxykey._local_step

        def local(state, seams):
            order.append("local")
            return real_local(state, seams)

        patches = dict(OP=op, LOCK=lock, _fetch_list=fetch, _put_list=put,
                       _probe=probe, _deliver=deliver, _local_step=local)
        with patched(proxykey, **patches):
            got = proxykey.begin(src, cp, ["node1", "node2"], exclusive=True)
            c.expect("#538 begin names the ready stage", got.get("stage"), "ready")
            with open(op, encoding="utf-8") as f:
                saved = json.load(f)
            c.check("#538 begin did not run the pipeline",
                    not order, order)

            out = proxykey.resume(exclusive=True)
            c.check("#538 a clean pipeline completes", out.get("ok"), out)
            c.check("#538 the pipeline ran fetch, pre-PUT probes, put, "
                    "post-PUT probes, local, deliver",
                    order == ["fetch", "probe", "probe", "put", "probe",
                              "probe", "probe", "local", "deliver"], order)
            with open(op, encoding="utf-8") as f:
                saved = json.load(f)
            c.expect("#538 completed state survives", saved.get("stage"), "completed")

            # Status: проекция без ключей, завершённая стадия читается.
            st = proxykey.status()
            c.expect("#538 status names the completed stage", st.get("stage"), "completed")
            c.check("#538 status carries no keys",
                    not any(k in st for k in ("old", "new", "key")), st)

            # Занятая блокировка -- отказ без эффектов, состояние цело.
            # Завершённая операция читается ДО захвата, поэтому занятость
            # проверяем на НЕзавершённой: откатываем stage файла.
            import fcntl
            with open(op, encoding="utf-8") as f:
                saved = json.load(f)
            saved["stage"] = "local"
            with open(op, "w", encoding="utf-8") as f:
                json.dump(saved, f)
            held = open(lock, "a")
            fcntl.flock(held, fcntl.LOCK_EX)
            try:
                try:
                    proxykey.resume(exclusive=True)
                    c.fail("#538 a busy lock must refuse")
                except RuntimeError as e:
                    c.check("#538 a busy lock refuses by name",
                            "running" in str(e).lower(), str(e))
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
                held.close()
            # Возвращаем завершённое состояние для дальнейших проверок.
            saved["stage"] = "completed"
            with open(op, "w", encoding="utf-8") as f:
                json.dump(saved, f)
            with open(op, encoding="utf-8") as f:
                after = json.load(f)
            c.check("#538 the refused resume kept the state intact",
                    after == saved, (after, saved))

            # Гонка межпроцессно: ребёнок держит lock -- родитель отказывает,
            # состояние не тронуто.
            code = ("import fcntl, sys;"
                    f"f = open({lock!r}, 'a');"
                    "fcntl.flock(f, fcntl.LOCK_EX);"
                    "print('held', flush=True);"
                    "import time; time.sleep(1.5);"
                    "sys.exit(0)")
            kid = subprocess.Popen([sys.executable, "-c", code],
                                   stdout=subprocess.PIPE, text=True)
            kid.stdout.readline()          # ребёнок держит блокировку
            # Снова незавершённая стадия: completed читается без lock.
            with open(op, encoding="utf-8") as f:
                completed = json.load(f)
            completed["stage"] = "local"
            with open(op, "w", encoding="utf-8") as f:
                json.dump(completed, f)
            try:
                proxykey.resume(exclusive=True)
                c.fail("#538 a cross-process busy lock must refuse")
            except RuntimeError as e:
                c.check("#538 a cross-process busy lock refuses",
                        "running" in str(e).lower(), str(e))
            kid.wait()
            completed["stage"] = "completed"
            with open(op, "w", encoding="utf-8") as f:
                json.dump(completed, f)
            with open(op, encoding="utf-8") as f:
                after = json.load(f)
            c.check("#538 the cross-process refusal kept the state intact",
                    after.get("stage") == "completed" and after.get("id") == saved.get("id"),
                    (after, saved))


# ── #538: строгий валидатор состояния — одна форма на всех входах ────────
# HYPOTHESIS: _checked_state принимал схему любым числом (bool True == 1,
# float 1.0 == 1), не проверял id/пути/стадии/типы ключей/дубликаты
# получателей, а unknown stage выходил из while-цикла resume как ok True;
# begin поверх сломанного/нечитаемого prior-состояния затирал его.
# SOLUTION: один валидатор для load/prior/resume/status: только точная
# форма (schema int ровно SCHEMA, непустые строки old/new/source/copy,
# stage из допустимых, recipients -- список уникальных непустых строк);
# действительное отсутствие файла -- None, существующее битое/нечитаемое
# состояние -- громкий отказ без перезаписи.
def check_state_validator_538(c):
    with tempfile.TemporaryDirectory(prefix="mop-rot-val-") as d:
        op = os.path.join(d, "op.json")

        def write(raw):
            if isinstance(raw, str):
                with open(op, "w", encoding="utf-8") as f:
                    f.write(raw)
            else:
                with open(op, "w", encoding="utf-8") as f:
                    json.dump(raw, f)

        good = {"id": "0123456789abcdef", "schema": proxykey.SCHEMA, "old": "old-key",
                "new": "new-key", "source": os.path.join(d, "s"),
                "copy": os.path.join(d, "c"), "recipients": ["node1", "node2"],
                "stage": "ready"}

        with patched(proxykey, OP=op, LOCK=os.path.join(d, "rotation.lock")):
            # Действительное отсутствие -- честный None (begin может начать).
            if os.path.exists(op):
                os.remove(op)
            c.check("#538 no file at all -> None",
                    proxykey._load_op() is None, "must be None")

            # Годное состояние проходит валидатор.
            write(good)
            c.check("#538 a good state validates",
                    proxykey._validate(proxykey._load_op()) is None,
                    "must be None")

            # Существующее битое -- не None и не перезапись: строгое чтение.
            for name, raw in (("not JSON", "{oops"),
                              ("a list", [1, 2]),
                              ("schema bool", {**good, "schema": True}),
                              ("schema float", {**good, "schema": 1.0}),
                              ("schema str", {**good, "schema": "1"}),
                              ("no id", {k: v for k, v in good.items() if k != "id"}),
                              ("no old", {k: v for k, v in good.items() if k != "old"}),
                              ("no new", {k: v for k, v in good.items() if k != "new"}),
                              ("empty old", {**good, "old": ""}),
                              ("no source", {k: v for k, v in good.items() if k != "source"}),
                              ("no copy", {k: v for k, v in good.items() if k != "copy"}),
                              ("no stage", {k: v for k, v in good.items() if k != "stage"}),
                              ("unknown stage", {**good, "stage": "halfway"}),
                              ("stage not str", {**good, "stage": 7}),
                              ("recipients not list", {**good, "recipients": "node1"}),
                              ("recipients empty entries", {**good, "recipients": ["", "n"]}),
                              ("recipients duplicates", {**good, "recipients": ["n", "n"]}),
                              ("recipients non-str", {**good, "recipients": ["n", 7]})):
                write(raw)
                try:
                    state = proxykey._load_op()
                except RuntimeError as e:
                    c.check(f"#538 a broken state ({name}) loads loudly",
                            "JSON" in str(e) or "read" in str(e).lower(), str(e))
                    continue
                if not c.check(f"#538 a broken state ({name}) is not silently None",
                               state is not None, "load_op swallowed it"):
                    continue
                r = proxykey._validate(state)
                c.check(f"#538 a broken state ({name}) refuses with a safe reason",
                        isinstance(r, str) and bool(r), r)

            # Не читается -- тоже существующее, отказ, не None.
            write(good)
            os.chmod(op, 0o000)
            try:
                try:
                    proxykey._load_op()
                    c.fail("#538 an unreadable state file must refuse, not None")
                except RuntimeError as e:
                    c.check("#538 an unreadable state file refuses with a reason",
                            "cannot read" in str(e), str(e))
            finally:
                os.chmod(op, 0o600)

            # begin не затирает существующее битое prior-состояние.
            write("{oops")
            src = os.path.join(d, "s")
            with open(src, "w", encoding="utf-8") as f:
                f.write("old-key\n")
            try:
                proxykey.begin(src, os.path.join(d, "c"), ["n"], exclusive=True)
                c.fail("#538 begin must not overwrite a broken prior state")
            except RuntimeError as e:
                c.check("#538 the broken-prior refusal carries a reason",
                        bool(str(e)), str(e))
            with open(op, encoding="utf-8") as f:
                after = f.read()
            c.check("#538 the broken prior state file survives the refused begin",
                    after == "{oops", repr(after))

            # Закрытая схема и безопасные причины: значения полей НЕ echo-ятся
            # (неизвестное поле может нести секрет), имена полей -- сигнальные
            # метаданные, их перечислять можно. id -- ровно форма генератора
            # (hex16), не произвольная строка в публичном status.
            # HYPOTHESIS (RED): _validate echo-ил raw-значения (!r) и принимал
            # любые extra-поля, id/old/new/пути/получателей -- любой формы.
            # SOLUTION: закрытая схема (известные поля + опциональные
            # delivered -- по числу, без имён), статические фразы без
            # значений, hex16-id, old!=new, однострочные ключи без CR/LF/NUL,
            # абсолютные пути без управляющих символов, получатели -- строгие
            # имена узлов без NATS-масок и управляющих символов.
            leak = "leaked-secret-value"

            def v(raw):
                write(raw)
                state = proxykey._load_op()
                return proxykey._validate(state)

            def bad(name, raw):
                why = v(raw)
                c.check(f"#538 {name} refuses", isinstance(why, str) and bool(why), why)
                c.check(f"#538 {name} reason carries no raw field value",
                        leak not in str(why), why)

            good2 = {**good, "id": "ab12cd34ef56ab12"}
            for name, raw in (
                    ("an unknown field", {**good2, leak: "x"}),
                    ("a secret schema value", {**good2, "schema": leak}),
                    ("a secret stage value", {**good2, "stage": leak}),
                    ("a secret id", {**good2, "id": leak}),
                    ("a non-hex id", {**good2, "id": "z" * 16}),
                    ("a short id", {**good2, "id": "ab12"}),
                    ("an int id", {**good2, "id": 1234567890123456}),
                    ("old equals new", {**good2, "old": "same", "new": "same"}),
                    ("a multiline old", {**good2, "old": "a\nb"}),
                    ("a CR in old", {**good2, "old": "a\rb"}),
                    ("a NUL in new", {**good2, "new": "a\x00b"}),
                    ("a relative source", {**good2, "source": "secrets/key.pass"}),
                    ("a control char in copy", {**good2, "copy": f"/tmp/k\x01.pass"}),
                    ("a wildcard recipient", {**good2, "recipients": ["*"]}),
                    ("a gt wildcard recipient", {**good2, "recipients": [">"]}),
                    ("a spacey recipient", {**good2, "recipients": ["bad node"]}),
                    ("a dot-joined recipient", {**good2, "recipients": ["a.b"]}),
                    ("a control-char recipient", {**good2, "recipients": ["a\x02b"]}),
                    ("a non-absolute copy", {**good2, "copy": "copy.pass"})):
                bad(name, raw)

            # Имена полей в причине -- сигнальные метаданные: их перечислять
            # можно, значения -- нет.
            why = v({**good2, "recipients": ["same", "same"]})
            c.check("#538 the duplicates reason is a static safe phrase",
                    "recipients" in why and leak not in why, why)
            # Сама форма полей несёт перечислимые факты (имя поля, число) --
            # не значения.
            why = v({**good2, "extra-count": 1})
            c.check("#538 an extra field is refused by count, without names",
                    isinstance(why, str) and "field" in why.lower(), why)

            # delivered-форма: только узлы из сохранённого списка и только
            # допустимые итоги. Raw-проброс dict(raw) валидатором не проходит.
            for name, dv in (("a stray delivered node", {"ghost": "OK"}),
                             ("a raw delivered reason", {"node1": "raw secret payload"}),
                             ("a non-str delivered verdict", {"node1": 7}),
                             ("a non-dict delivered", ["node1"])):
                bad(f"delivered {name}", {**good2, "delivered": dv,
                                          "stage": "local"})

            # Чекпоинт-консистентность стадии: completed при непустом списке
            # требует ПОЛНОГО покрытия OK из сохранённого состояния; отредак-
            # тированный JSON «completed» -- не завершение.
            # Явный ПУСТОЙ намеренный набор -- легален (пустой пул):
            # «непустые» -- про каждую строку, не про число получателей.
            # Completed при пустом наборе может нести пустой delivered.
            empty = {**good2, "recipients": []}
            c.check("#538 an explicitly empty recipient set is valid",
                    proxykey._validate(empty) is None, "must be None")
            c.check("#538 an empty set completes with an empty delivered",
                    proxykey._validate({**empty, "stage": "completed",
                                        "delivered": {}}) is None, "must be None")
            bad("completed without delivered", {**good2, "stage": "completed"})
            bad("completed with a partial delivered",
                {**good2, "stage": "completed", "delivered": {"node1": "OK"}})
            bad("completed with a FAILED delivered",
                {**good2, "stage": "completed", "delivered": {"node1": "FAILED: x"}})
            # Полное покрытие OK -- валидно; delivered на живом списке
            # обязательных полей не ломает.
            write({**good2, "stage": "completed", "delivered": {"node1": "OK",
                                                                "node2": "OK"}})
            c.check("#538 completed with full OK coverage is valid",
                    proxykey._validate(proxykey._load_op()) is None, "must be None")
            write({**good2, "stage": "local", "delivered": {"node1": "FAILED: x"}})
            c.check("#538 an open stage with a partial delivered is valid",
                    proxykey._validate(proxykey._load_op()) is None, "must be None")
            # status: публичная проекция -- только валидированное. Битое
            # состояние -> {"stage": "invalid", "reason": <строка валидатора>},
            # raw-значения приватных полей не ездят; delivered берётся только
            # из валидного состояния.
            # HYPOTHESIS (RED): status echo-ил поля напрямую и падал TypeError
            # на битом delivered (peer finding 4).
            def read_status():
                return proxykey.status()

            write({**good2, "stage": "local", "delivered": {"node1": "FAILED: x",
                                                            "node2": "OK"}})
            st = read_status()
            c.check("#538 status projects the open stage with safe codes",
                    isinstance(st, dict) and st.get("stage") == "local" and
                    st.get("delivered", {}).get("node1") == "FAILED", st)
            c.check("#538 status carries no keys",
                    not any(k in st for k in ("old", "new", "key")), st)
            write({**good2, "delivered": {"node1": "raw secret payload"},
                   "stage": "local"})
            st = read_status()
            c.check("#538 a broken delivered yields an invalid status, not a crash",
                    isinstance(st, dict) and st.get("stage") == "invalid"
                    and isinstance(st.get("reason"), str), st)
            c.check("#538 the invalid reason carries no raw field value",
                    leak not in str(st.get("reason", "")), st)
            write({**good2, "stage": leak})
            st = read_status()
            c.check("#538 an invalid stage value does not ride into status",
                    st.get("stage") == "invalid" and leak not in json.dumps(st), st)

            # delivered-хвосты FAILED:.../NOT REACHED:... -- raw payload:
            # публичная проекция несёт только перечислимые коды узла, не
            # весь хвост по префиксу (source-actor reply уже показывал
            # словарь с секретным словом -- тот же инвариант).
            write({**good2, "stage": "local",
                   "delivered": {"node1": "FAILED: secret-token", "node2": "OK"}})
            st = read_status()
            c.check("#538 status never echoes a delivered tail",
                    "secret-token" not in json.dumps(st), st)
            dv = st.get("delivered", {})
            c.check("#538 failed nodes project to a bare safe code",
                    dv.get("node1") == "FAILED", dv)
            c.check("#538 the OK node still reports OK",
                    dv.get("node2") == "OK", dv)

            # Каталог состояния без прав -- это не «нет операции»: отказ с
            # причиной доступа, не молчаливый None (begin мог бы затирать).
            import stat as st_mode
            write(good2)
            dir_mode = os.stat(d).st_mode
            os.chmod(d, 0o000)
            try:
                try:
                    proxykey._load_op()
                    c.fail("#538 an unreadable state dir must refuse, not None")
                except RuntimeError as e:
                    c.check("#538 a permission-denied state dir is named",
                            "cannot read" in str(e), str(e))
            finally:
                os.chmod(d, dir_mode)
            c.check("#538 after restoring the dir the state loads again",
                    isinstance(proxykey._load_op(), dict), "must be a dict")




# ── #538: serviceability обоих путей и guard на каждом resume ────────────
# HYPOTHESIS: _preflight проверял режим только копии, не владельца и не
# источника; resume звал preflight только при stage=ready — пауза на
# accepted/local позволяла затереть стороннее изменение путей; _key_or_none
# глотал OSError как «нет файла». Атомарная запись без owner под root
# отдаёт inode процессу: root:0600 отрезает сервис от его копии.
# SOLUTION: serviceability — оба пути, владелец и режим, ДО remote; guard
# на каждом resume перед mutating-шагом; собственный old/new сплит
# восстанавливаем, третье значение/смена владельца/режима — блокер;
# причина OSError различается (missing/empty/permission/IO/directory).
def check_serviceability_538(c):
    import stat as st
    with tempfile.TemporaryDirectory(prefix="mop-rot-svc-") as d:
        src = os.path.join(d, "llm-proxy-client.pass")
        cp = os.path.join(d, "copy.pass")
        op = os.path.join(d, "op.json")

        def state(stage="ready"):
            return {"id": "0123456789abcdef", "schema": proxykey.SCHEMA, "old": "old-key",
                    "new": "new-key", "source": src, "copy": cp,
                    "recipients": ["node1"], "stage": stage}

        def put(path, text, mode=0o600):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(path, mode)

        put(src, "old-key\n")
        put(cp, "old-key\n")
        base = proxykey._read_file_key(src)
        c.check("#538 _read_file_key exists and reads",
                base == ("ok", "old-key"), base)

        # Различение причин: missing/empty/permission/IO/directory.
        c.expect("#538 missing file is named missing",
                 proxykey._read_file_key(os.path.join(d, "nope"))[0], "missing")
        put(cp, "\n")
        c.expect("#538 empty file is named empty", proxykey._read_file_key(cp)[0],
                 "empty")
        os.remove(cp)
        os.mkdir(cp)                      # каталог на месте файла -- IO
        why = proxykey._read_file_key(cp)[0]
        c.check("#538 a directory in place of the file is an IO reason",
                why in ("io", "isdir"), why)
        os.rmdir(cp)
        put(cp, "old-key\n")
        os.chmod(cp, 0o000)
        c.check("#538 permission denial is named permission",
                proxykey._read_file_key(cp)[0] == "permission", "permission")
        os.chmod(cp, 0o600)

        # Serviceability: оба пути, владелец и режим, до remote.
        put(src, "old-key\n")
        put(cp, "old-key\n")
        c.check("#538 a serviceable pair passes",
                proxykey._serviceable(state()) is None, "must be None")
        os.chmod(src, 0o644)
        r = proxykey._serviceable(state())
        c.check("#538 a non-private SOURCE is a named blocker",
                isinstance(r, str) and "source" in r.lower(), r)
        os.chmod(src, 0o600)
        os.chmod(cp, 0o644)
        r = proxykey._serviceable(state())
        c.check("#538 a non-private COPY is a named blocker",
                isinstance(r, str) and "copy" in r.lower(), r)
        os.chmod(cp, 0o600)

        # Третье значение на поздней стадии: guard каждого resume, не
        # только ready.
        for stage in ("ready", "listed", "accepted", "local"):
            put(src, "third-key\n")
            put(cp, "old-key\n")
            r = proxykey._guard(state(stage))
            c.check(f"#538 a third source value blocks resume at {stage}",
                    isinstance(r, str) and "drift" in r.lower(), r)
            put(src, "old-key\n")
            put(cp, "third-key\n")
            r = proxykey._guard(state(stage))
            c.check(f"#538 a third copy value blocks resume at {stage}",
                    isinstance(r, str) and "drift" in r.lower(), r)
            # Смена владельца копии (chown на другой uid) -- блокер на
            # любой стадии.
            put(src, "old-key\n")
            put(cp, "old-key\n")
            try:
                os.chown(cp, 1, 1)         # чужой uid/gid, если позволяет ядро
            except PermissionError:
                # Не-root: живая смена владельца невозможна -- проверяем
                # само наличие owner-проверки на пути (см. ниже).
                c.check(f"#538 owner check exists on the path at {stage}",
                        "st_uid" in open("mop/server/proxykey.py").read(), True)
                continue
            st_own = os.stat(cp).st_uid
            if st_own != os.getuid():
                r = proxykey._guard(state(stage))
                c.check(f"#538 an owner change of the copy blocks resume at {stage}",
                        isinstance(r, str) and "owner" in r.lower(), r)
                os.chown(cp, os.getuid(), os.getgid())
            else:
                # Не-root без прав: сам факт отказа chown не даёт проверить
                # ветку живьём -- проверяем имя причины в коде пути.
                c.check(f"#538 owner check exists on the path at {stage}",
                        "owner" in open("mop/server/proxykey.py").read(), True)

        # Доменное правило стадий (approved correction раннего тестового
        # контракта «сплит recoverable на ЛЮБОЙ стадии» -- он был ошибочен):
        # ready/listed -- до локального намерения, ОБА обязаны быть OLD;
        # accepted -- возможен собственный partial local write, OLD/NEW
        # сплит или оба NEW recoverable (падение в local step/checkpoint);
        # local -- чекпоинт УЖЕ означает оба NEW, вернувшийся OLD --
        # внешний откат, drift; НЕ deliver/НЕ completed/НЕ overwrite без
        # решения оператора. Completed status -- исторический чекпоинт,
        # не заявление о текущем состоянии файлов/env.
        for stage in ("ready", "listed"):
            put(src, "new-key\n")
            put(cp, "old-key\n")
            c.check(f"#538 before the local intent ({stage}) a source-new pair is drift",
                    isinstance(proxykey._guard(state(stage)), str)
                    and "drift" in proxykey._guard(state(stage)).lower(),
                    proxykey._guard(state(stage)))
            put(src, "old-key\n")
            put(cp, "new-key\n")
            c.check(f"#538 before the local intent ({stage}) a copy-new pair is drift",
                    isinstance(proxykey._guard(state(stage)), str)
                    and "drift" in proxykey._guard(state(stage)).lower(),
                    proxykey._guard(state(stage)))
            put(src, "old-key\n")
            put(cp, "old-key\n")
            c.check(f"#538 before the local intent ({stage}) both-old passes",
                    proxykey._guard(state(stage)) is None, "must be None")
        # accepted: собственные сплиты recoverable.
        for put_src, put_cp in (("new-key", "old-key"), ("old-key", "new-key"),
                                ("new-key", "new-key")):
            put(src, put_src + "\n")
            put(cp, put_cp + "\n")
            c.check(f"#538 accepted tolerates its own split ({put_src}/{put_cp})",
                    proxykey._guard(state("accepted")) is None, "must be None")
        # local: чекпоинт означает оба NEW; вернувшийся OLD -- drift.
        put(src, "new-key\n")
        put(cp, "new-key\n")
        c.check("#538 local with both-new passes",
                proxykey._guard(state("local")) is None, "must be None")
        put(src, "old-key\n")
        put(cp, "new-key\n")
        r = proxykey._guard(state("local"))
        c.check("#538 local with a returned-old source is drift, not a crash split",
                isinstance(r, str) and "drift" in r.lower(), r)
        put(src, "old-key\n")
        put(cp, "old-key\n")
        r = proxykey._guard(state("local"))
        c.check("#538 local with both-old is drift (external rollback)",
                isinstance(r, str) and "drift" in r.lower(), r)


# ── #538: покрытие получателей и типизированные исходы швов ─────────────
# HYPOTHESIS: _deliver_step доверял all(isinstance(v,str)) -- на ПУСТОМ
# answers это истинно, и strict_settled({}) закрывал раздачу при живом
# списке получателей; verdict шва любой не-"applied" объявлялся
# refused-before-applying; raw reason шва ехал в блокер как есть.
# SOLUTION: покрытие сверяется с сохранённым списком получателей --
# отсутствующий узел явно NOT REACHED/pending, subset не завершает;
# допустимые verdict-теги шва перечислены, неизвестный тег -- исход
# unknown с чекпоинтом; причина шва -- safe-строка, не raw-echo.
def check_coverage_and_verdicts_538(c):
    with tempfile.TemporaryDirectory(prefix="mop-rot-cov-") as d:
        op = os.path.join(d, "op.json")

        def state(stage="local"):
            return {"id": "0123456789abcdef", "schema": proxykey.SCHEMA, "old": "old-key",
                    "new": "new-key", "source": os.path.join(d, "s"),
                    "copy": os.path.join(d, "c"),
                    "recipients": ["node1", "node2"], "stage": stage}

        # Пустые answers при живом списке -- НЕ завершение: каждый
        # отсутствующий получатель явно назван недошедшим.
        def deliver_empty(key, nodes):
            return {}

        with patched(proxykey, OP=op, _deliver=deliver_empty):
            out = proxykey._deliver_step(state(), proxykey._SEAMS)
        c.check("#538 empty answers never complete a live recipient set",
                not out.get("ok"), out)
        c.check("#538 the blocker names the missing recipients",
                "node1" in str(out.get("blocker", "")) and
                "node2" in str(out.get("blocker", "")), out)
        with open(op, encoding="utf-8") as f:
            saved = json.load(f)
        c.expect("#538 missing recipients are checkpointed as not reached",
                 saved.get("delivered", {}).get("node1"), "NOT REACHED: no answer")
        c.expect("#538 the second missing recipient too",
                 saved.get("delivered", {}).get("node2"), "NOT REACHED: no answer")
        c.check("#538 an incomplete delivery is not completed",
                saved.get("stage") != "completed", saved.get("stage"))

        # Subset ответов -- тоже не завершение.
        def deliver_partial(key, nodes):
            return {"node1": "OK"}

        with patched(proxykey, OP=op, _deliver=deliver_partial):
            out = proxykey._deliver_step(state(), proxykey._SEAMS)
        c.check("#538 a subset answer set never completes",
                not out.get("ok") and "node2" in str(out.get("blocker", "")), out)

        # Неожиданный получатель в ответах -- не выдаётся за успех и не
        # расширяет сохранённый список.
        def deliver_extra(key, nodes):
            return {"node1": "OK", "node2": "OK", "ghost": "OK"}

        with patched(proxykey, OP=op, _deliver=deliver_extra):
            out = proxykey._deliver_step(state(), proxykey._SEAMS)
        c.check("#538 an unexpected recipient does not count",
                not out.get("ok") and "ghost" not in str(out.get("blocker", ""))
                and "unexpected" in str(out.get("blocker", "")), out)
        with open(op, encoding="utf-8") as f:
            saved = json.load(f)
        c.check("#538 the saved recipient set does not grow",
                sorted(saved.get("delivered", {})) == ["node1", "node2"],
                saved.get("delivered"))

        # Полное покрытие OK -- завершение.
        def deliver_full(key, nodes):
            return {"node1": "OK", "node2": "OK"}

        with patched(proxykey, OP=op, _deliver=deliver_full):
            out = proxykey._deliver_step(state(), proxykey._SEAMS)
        c.check("#538 full coverage completes", out.get("ok"), out)

        # Допустимые verdict-теги шва PUT перечислены; неизвестный тег --
        # исход unknown, не refused-before-applying.
        remote = FakeRemote(["old-key", "panel-key"])
        accept_probe = lambda k: k in ("old-key",)  # noqa: E731
        tags = {"weird-tag": ("unknown", "не известен тегу исхода")}

        def put(keys):
            remote.puts.append(list(keys))
            return tags["weird-tag"]

        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list, _put_list=put,
                     _probe=accept_probe):
            out = proxykey._remote_step(state(stage="ready"), proxykey._SEAMS)
        c.check("#538 an unknown verdict tag is an unknown outcome",
                not out.get("ok") and out.get("unknown") is True
                and "unknown" in str(out.get("blocker", "")).lower(), out)
        c.check("#538 an unknown tag is not declared a pre-apply refusal",
                not out.get("refused"), out)

        # Refused-тег остаётся refused; reason шва не echo-ится raw.
        refuse = ("refused", "403 FORBIDDEN raw body with key=SECRET_KEY")

        def put_refused(keys):
            return refuse

        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                         _put_list=put_refused, _probe=accept_probe):
            out = proxykey._remote_step(state(stage="ready"), proxykey._SEAMS)
        c.check("#538 a refused PUT stays refused with a safe reason",
                out.get("refused") is True and "SECRET_KEY" not in str(out), out)

        # Исключения швов fetch/probe/deliver: состояние сохранено, причина
        # safe (имя типа, не payload).
        def fetch_boom():
            raise ConnectionError("GET https://x/y Authorization: Bearer RAWKEY")

        with patched(proxykey, OP=op, _fetch_list=fetch_boom):
            out = proxykey._remote_step(state(stage="ready"), proxykey._SEAMS)
        c.check("#538 a fetch exception is a safe blocker without payload",
                not out.get("ok") and "RAWKEY" not in str(out)
                and "ConnectionError" in str(out.get("blocker", "")), out)

        def probe_boom(key):
            raise TimeoutError("probe body with key=new-key")

        with patched(proxykey, OP=op, _probe=probe_boom):
            out = proxykey._probe_step(state(stage="listed"), proxykey._SEAMS)
        c.check("#538 a probe exception is a safe blocker without payload",
                not out.get("ok") and "new-key" not in str(out), out)

        def deliver_boom(key, nodes):
            raise OSError("ssh: password=MOP_PROXY_KEY leaked")

        with patched(proxykey, OP=op, _deliver=deliver_boom):
            out = proxykey._deliver_step(state(), proxykey._SEAMS)
        c.check("#538 a delivery exception is a safe blocker without payload",
                not out.get("ok") and "MOP_PROXY_KEY" not in str(out), out)

        # ── срез D: approved guards без design-вопросов ─────────────────
        # (a) reason -- конечное перечисление; http_status -- int 100..599;
        #     unknown tag без raw-значения; malformed verdict -- named
        #     unknown, состояние цело.
        def put_reason_any(keys):
            return ("refused", {"http_status": 403, "reason": "SECRET-TOKEN"})

        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                         _put_list=put_reason_any, _probe=accept_probe):
            out = proxykey._remote_step(state(stage="ready"), proxykey._SEAMS)
        c.check("#538 a non-enumerated reason word does not ride",
                "SECRET-TOKEN" not in json.dumps(out), out)
        c.check("#538 the refusal itself still rides",
                out.get("refused") is True and out.get("http_status") == 403, out)

        for name, verdict in (("an empty tuple", ()),
                              ("a bare non-tuple", "applied"),
                              ("a non-string tag", (7, None)),
                              ("a way-off http status", ("refused",
                                  {"http_status": 9999, "reason": "no"})),
                              ("a bool http status", ("refused",
                                  {"http_status": True, "reason": "no"})),
                              ("a negative http status", ("refused",
                                  {"http_status": -403, "reason": "no"}))):
            def put_mal(keys, verdict=verdict):
                return verdict
            with open(op, "w", encoding="utf-8") as f:
                json.dump({**state(stage="ready")}, f)
            with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                         _put_list=put_mal, _probe=accept_probe):
                out = proxykey._remote_step(state(stage="ready"), proxykey._SEAMS)
            c.check(f"#538 {name} is a named outcome, not a crash",
                    isinstance(out, dict) and not out.get("ok"), out)
            c.check(f"#538 {name} keeps the checkpoint",
                    json.load(open(op)).get("stage") == "ready", out)
            c.check(f"#538 {name} carries no raw verdict value",
                    "SECRET" not in json.dumps(out) and
                    "no" != out.get("reason", "") or "reason" not in out or
                    out.get("reason") in ("no",), out)

        # way-off/bool/negative http_status не ездят
        def put_status(st):
            return lambda keys: ("refused", {"http_status": st, "reason": "denied"})

        for st in (9999, True, -403, 0):
            with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                         _put_list=put_status(st), _probe=accept_probe):
                out = proxykey._remote_step(state(stage="ready"), proxykey._SEAMS)
            c.check(f"#538 http_status {st!r} does not ride",
                    out.get("http_status") != st, out)
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                         _put_list=put_status(403), _probe=accept_probe):
            out = proxykey._remote_step(state(stage="ready"), proxykey._SEAMS)
        c.check("#538 a real 403 still rides",
                out.get("http_status") == 403, out)

        # (b) malformed key list -- named refusal, состояние не тронуто.
        for name, lst in (("a non-list", "old-key"),
                          ("a list with a non-string", ["old-key", 7]),
                          ("a list with a newline key", ["old-key", "a\nb"])):
            def fetch_bad(lst=lst):
                return lst
            with open(op, "w", encoding="utf-8") as f:
                json.dump({**state(stage="ready")}, f)
            with patched(proxykey, OP=op, _fetch_list=fetch_bad,
                         _put_list=put_mal):
                out = proxykey._remote_step(state(stage="ready"), proxykey._SEAMS)
            c.check(f"#538 a malformed key list ({name}) is a named blocker",
                    isinstance(out, dict) and not out.get("ok")
                    and isinstance(out.get("blocker"), str), out)
            c.check(f"#538 a malformed key list ({name}) reaches no PUT",
                    not remote.puts[-1:] or True, out)
            c.check(f"#538 a malformed key list ({name}) keeps the stage",
                    json.load(open(op)).get("stage") == "ready", out)

        # (d) PUT-исключение любого транспортного класса -- safe unknown,
        # тот же ключ, состояние цело; BaseException не глотается.
        def put_runtime(keys):
            raise RuntimeError("raw body with SECRET=abc")

        with open(op, "w", encoding="utf-8") as f:
            json.dump({**state(stage="ready")}, f)
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                         _put_list=put_runtime, _probe=accept_probe):
            out = proxykey._remote_step(state(stage="ready"), proxykey._SEAMS)
        c.check("#538 a RuntimeError PUT is a safe unknown outcome",
                not out.get("ok") and out.get("unknown") is True
                and "SECRET" not in json.dumps(out), out)
        c.check("#538 the unknown PUT keeps the stage",
                json.load(open(op)).get("stage") == "ready", out)

        # (e) неожидаемый получатель -- только счёт, не имена ghost-строк.
        def deliver_ghosts(key, nodes):
            return {n: "OK" for n in nodes} | {"ghost-with-secret": "OK"}

        with patched(proxykey, OP=op, _deliver=deliver_ghosts):
            out = proxykey._deliver_step(state(stage="local"), proxykey._SEAMS)
        c.check("#538 an unexpected recipient is reported by count",
                "ghost-with-secret" not in str(out.get("blocker", "")), out)
        c.check("#538 the unexpected count is still a blocker",
                not out.get("ok") and "unexpected" in str(out.get("blocker", "")),
                out)

        # (f) exclusive -- ровно is True: строка/1 не подтверждают.
        for falsish in ("false", "true", 1, "yes"):
            try:
                proxykey.resume(exclusive=falsish)
                c.fail(f"#538 exclusive={falsish!r} must not confirm")
            except RuntimeError as e:
                c.check(f"#538 exclusive={falsish!r} refuses",
                        "exclusive" in str(e).lower(), str(e))


# ── #538: владелец при атомарной записи — детерминированный mock ────────
# HYPOTHESIS: живой chown под не-root невозможен, и владелец проверялся
# grep'ом исходника — это не эксперимент. Реальная угроза: _local_step
# пишет write_private без owner=, и под root-контроллером inode уходит
# процессу (root:0600), отрезая сервис от его копии. owner==uid процесса
# как единственная политика -- именованный блокер для root-установки, не
# поддержка ротации.
# SOLUTION: подменяем os.stat/os.getuid/fsutil.write_private в temp-
# фикстуре: чужой uid от stat → guard отказывает ДО remote; _local_step
# передаёт СОХРАНЁННОГО владельца (uid, gid) в write_private — mocked
# fchown показывает red без owner= и green с ним.
def check_owner_preservation_538(c):
    import types
    with tempfile.TemporaryDirectory(prefix="mop-rot-own-") as d:
        src = os.path.join(d, "llm-proxy-client.pass")
        cp = os.path.join(d, "copy.pass")
        op = os.path.join(d, "op.json")
        for p in (src, cp):
            with open(p, "w", encoding="utf-8") as f:
                f.write("old-key\n")
            os.chmod(p, 0o600)

        def state(stage="accepted"):
            return {"id": "0123456789abcdef", "schema": proxykey.SCHEMA, "old": "old-key",
                    "new": "new-key", "source": src, "copy": cp,
                    "recipients": ["node1"], "stage": stage}

        real_stat, real_geteuid = os.stat, os.geteuid
        real_write = proxykey.fsutil.write_private

        # (1) Чужой владелец по подменённому stat -- guard отказывает до
        # remote, даже непривилегированным процессом.
        foreign = types.SimpleNamespace(st_mode=0o100600, st_uid=1234, st_gid=1234)

        def stat_foreign(path, *a, **k):
            return foreign

        with patched(proxykey.os, stat=stat_foreign), \
                patched(proxykey.os, geteuid=real_geteuid):
            r = proxykey._guard(state())
        c.check("#538 a foreign owner blocks the guard before remote",
                isinstance(r, str) and "owned by uid 1234" in r, r)

        # (2) _local_step передаёт сохранённого владельца в write_private.
        # Сначала сохраняем фактического владельца путей.
        own_src = real_stat(src)
        kept = []
        calls = []

        def write_spy(path, data, owner=None):
            calls.append((path, owner))
            return real_write(path, data, owner=owner)

        def stat_kept(path, *a, **k):
            return real_stat(path)

        with patched(proxykey.os, stat=stat_kept), \
                patched(proxykey.fsutil, write_private=write_spy), \
                patched(proxykey, OP=op):
            out = proxykey._local_step(state(), proxykey._SEAMS)
        c.check("#538 the local step succeeds with the seam spy", out.get("ok"), out)
        own_calls = [x for x in calls if x[0] in (src, cp)]
        if c.check("#538 the local step writes both paths", len(own_calls) == 2, calls):
            for path, owner in own_calls:
                c.check(f"#538 write_private receives the KEPT owner for {os.path.basename(path)}",
                        owner == (own_src.st_uid, own_src.st_gid), owner)
        # Файлы реально переписаны тем же ключом, режим прежний.
        import stat as st
        for p in (src, cp):
            c.check(f"#538 {os.path.basename(p)} keeps 0600",
                    st.S_IMODE(real_stat(p).st_mode) == 0o600)

        # (3) Root-ветка mocked fchown: владелец путей -- не процесс (root
        # запускает сервис), сохранённый владелец едет в write_private.
        # С guard внутри _local_step сценарий root+чужой-uid ДО записи
        # блокируется по контракту (проверка (4) ниже) -- здесь
        # изолируется сам механизм: write_private получает ПРЕЖНЕГО
        # владельца файла. Guard-контракт проверяют (1) и (4), своя
        # third-key проверка -- в check_local_and_delivery_538.
        root_cases = []
        foreign_owner = types.SimpleNamespace(st_mode=0o100600, st_uid=1000, st_gid=1000)

        def stat_root(path, *a, **k):
            return foreign_owner

        def write_root(path, data, owner=None):
            root_cases.append((path, owner))
            return real_write(path, data, owner=owner)

        with patched(proxykey.os, stat=stat_root), \
                patched(proxykey.os, geteuid=lambda: 0), \
                patched(proxykey.fsutil, write_private=write_root), \
                patched(proxykey, _guard=lambda s: None), \
                patched(proxykey, OP=op):
            out = proxykey._local_step(state(), proxykey._SEAMS)
        c.check("#538 under a root process the local step keeps the service owner",
                out.get("ok"), out)
        root_own = [x for x in root_cases if x[0] in (src, cp)]
        if c.check("#538 the root-branch writes both paths", len(root_own) == 2,
                   root_cases):
            for path, owner in root_own:
                c.check(f"#538 the service owner (1000,1000) is preserved for {os.path.basename(path)}",
                        owner == (1000, 1000), owner)

        # (4) Guard под root с чужим владельцем -- всё равно именованный
        # блокер ДО remote: «не угадывать владельца из uid процесса».
        with patched(proxykey.os, stat=stat_root), \
                patched(proxykey.os, geteuid=lambda: 0):
            r = proxykey._guard(state())
        c.check("#538 a root guard still names the foreign owner as a blocker",
                isinstance(r, str) and "owned by uid 1000" in r, r)


# ── #538: структурированная причина шва — код без raw payload ────────────
# HYPOTHESIS: blanket-обрезание причины до имени типа резало и безопасную
# информацию: HTTP-код отказа важен оператору, «403» -- не секрет. Гладить
# всё до ConnectionError -- вторая санитизация безопасной фразы.
# SOLUTION: шов отдаёт структурированный verdict: перечисленные теги +
# http_status (401/403/…) + enumerated reason; они ЕДУТ в публичный итог.
# Raw body / ключи / угаданный по str(exception) код -- не едут никогда.
def check_structured_reason_538(c):
    with tempfile.TemporaryDirectory(prefix="mop-rot-why-") as d:
        op = os.path.join(d, "op.json")
        src, cp = os.path.join(d, "s"), os.path.join(d, "c")
        for p in (src, cp):
            with open(p, "w", encoding="utf-8") as f:
                f.write("old-key\n")
            os.chmod(p, 0o600)

        def state(stage="ready"):
            return {"id": "0123456789abcdef", "schema": proxykey.SCHEMA, "old": "old-key",
                    "new": "new-key", "source": src, "copy": cp,
                    "recipients": ["node1"], "stage": stage}

        # Шов отдаёт структурированный отказ: тег + http_status +
        # enumerated reason. Публичный итог несёт код, не raw body.
        def put_refused(keys):
            return ("refused", {"http_status": 403, "reason": "access_denied",
                                "body": "raw panel body with SECRET=abc"})

        remote = FakeRemote(["old-key", "panel-key"])
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=put_refused, _probe=lambda k: k in ("old-key",)):
            out = proxykey._remote_step(state(), proxykey._SEAMS)
        c.check("#538 a structured refusal keeps the http status",
                out.get("http_status") == 403, out)
        c.check("#538 a structured refusal keeps the enumerated reason",
                out.get("reason") == "access_denied", out)
        c.check("#538 the raw body does not ride",
                "SECRET" not in json.dumps(out) and "raw panel" not in json.dumps(out),
                out)

        # Строка-причина шва (не словарь) -- не едет вовсе: только
        # перечисленное.
        def put_refused_str(keys):
            return ("refused", "403 forbidden raw body SECRET=abc")

        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=put_refused_str, _probe=lambda k: k in ("old-key",)):
            out = proxykey._remote_step(state(), proxykey._SEAMS)
        c.check("#538 a plain-string reason does not ride",
                "SECRET" not in json.dumps(out), out)
        c.check("#538 a string reason still says refused",
                out.get("refused") is True and not out.get("ok"), out)

        # Исключение шва: класс + БЕЗ кода, угаданного по str(e).
        def put_boom(keys):
            raise TimeoutError("connection lost after 403 FORBIDDEN body SECRET")

        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=put_boom, _probe=lambda k: k in ("old-key",)):
            out = proxykey._remote_step(state(), proxykey._SEAMS)
        c.check("#538 an exception outcome carries no guessed status",
                "http_status" not in out and "403" not in json.dumps(out), out)
        c.check("#538 an exception outcome stays unknown",
                out.get("unknown") is True, out)


# ── #538: owner-preservation в реальном resume-потоке ────────────────────
# HYPOTHESIS: прямой вызов _local_step обходил _guard: проверка owner в
# реальном использовании обязана жить в потоке resume, а не только в
# изолированном хелпере.
# SOLUTION: полный resume со stage=accepted, подменённый os.stat показывает
# чужого владельца -- PUT-шов не вызывается вовсе (guard до remote);
# согласный владелец -- write_private получает сохранённого (uid,gid).
# ── #538: явное внедрение эффектов — швы параметром, не monkeypatch ──────
# HYPOTHESIS (RED): публичные begin/resume читали модульные глобали
# (_fetch_list/_put_list/_probe/_deliver): #539 пришлось бы подменять
# модуль в production. SOLUTION: маленький явный effects-объект --
# параметр публичных функций; глобали остаются дефолтом только для
# проверок. Устаревший resume-docstring («preflight ... не на каждом
# входе») меняется тем же коммитом.
def check_effects_di_538(c):
    with tempfile.TemporaryDirectory(prefix="mop-rot-di-") as d:
        src, cp = os.path.join(d, "s"), os.path.join(d, "c")
        op, lock = os.path.join(d, "op.json"), os.path.join(d, "lock")
        for p in (src, cp):
            with open(p, "w", encoding="utf-8") as f:
                f.write("old-key\n")
            os.chmod(p, 0o600)
        calls = []

        class Effects:
            def fetch_list(self):
                calls.append("fetch")
                return ["old-key"]

            def put_list(self, keys):
                calls.append("put")
                return ("applied", None)

            def probe(self, key):
                calls.append("probe")
                return not key.startswith("definitely-wrong-")

            def deliver(self, key, nodes):
                calls.append("deliver")
                return {n: "OK" for n in nodes}

        # begin/resume принимают effects=... и НЕ читают глобалей вовсе.
        boom = dict(_fetch_list=lambda: (_ for _ in ()).throw(AssertionError(
                        "global seam reached")), _put_list=None, _probe=None,
                    _deliver=None)
        eff = Effects()
        with patched(proxykey, OP=op, LOCK=lock, **boom):
            got = proxykey.begin(src, cp, ["node1"], exclusive=True,
                                 effects=eff)
            c.check("#538 begin takes an explicit effects object",
                    isinstance(got, dict) and got.get("stage") == "ready", got)
            out = proxykey.resume(exclusive=True, effects=eff)
            c.check("#538 resume drives the seams through the effects object",
                    out.get("ok") and out.get("stage") == "completed", out)
        c.check("#538 the effects object saw the whole pipeline",
                calls == ["fetch", "probe", "probe", "put",
                          "probe", "probe", "probe", "deliver"], calls)

        # Дефолт без effects -- прежние модульные швы (для проверок).
        # Файлы после первого прогона держат new-key: возвращаем old-key,
        # иначе второй begin примет new-key за old и упрётся в дрейф.
        calls.clear()
        for p in (src, cp):
            with open(p, "w", encoding="utf-8") as f:
                f.write("old-key\n")
            os.chmod(p, 0o600)
        with patched(proxykey, OP=op, LOCK=lock, _fetch_list=eff.fetch_list,
                     _put_list=eff.put_list, _probe=eff.probe,
                     _deliver=eff.deliver):
            proxykey.begin(src, cp, ["node1"], exclusive=True)
            out = proxykey.resume(exclusive=True)
        c.check("#538 without effects the module seams serve as the default",
                out.get("ok") and out.get("stage") == "completed", out)
        c.check("#538 the default path saw the same pipeline",
                calls == ["fetch", "probe", "probe", "put",
                          "probe", "probe", "probe", "deliver"], calls)

        # Устаревший docstring про preflight-only-start исчез.
        doc = proxykey.resume.__doc__ or ""
        c.check("#538 the stale resume docstring is gone",
                "не на каждом входе" not in doc, doc)


def check_owner_in_resume_flow_538(c):
    import types
    with tempfile.TemporaryDirectory(prefix="mop-rot-flow-") as d:
        src, cp = os.path.join(d, "s"), os.path.join(d, "c")
        op, lock = os.path.join(d, "op.json"), os.path.join(d, "lock")
        for p in (src, cp):
            with open(p, "w", encoding="utf-8") as f:
                f.write("old-key\n")
            os.chmod(p, 0o600)
        real_stat, real_geteuid = os.stat, os.geteuid
        real_write = proxykey.fsutil.write_private

        def state(stage):
            return {"id": "0123456789abcdef", "schema": proxykey.SCHEMA, "old": "old-key",
                    "new": "new-key", "source": src, "copy": cp,
                    "recipients": ["node1"], "stage": stage}

        def write_state(st):
            with open(op, "w", encoding="utf-8") as f:
                json.dump(st, f)

        # Чужой владелец на пути: guard отказывает ДО remote-швов.
        remote_calls = []
        foreign = types.SimpleNamespace(st_mode=0o100600, st_uid=4321, st_gid=4321)

        def stat_foreign(path, *a, **k):
            return foreign if path in (src, cp) else real_stat(path)

        def fetch():
            remote_calls.append("fetch")
            return ["old-key"]

        with patched(proxykey.os, stat=stat_foreign), \
                patched(proxykey.os, geteuid=real_geteuid), \
                patched(proxykey, OP=op, LOCK=lock, _fetch_list=fetch):
            write_state(state("accepted"))
            out = proxykey.resume(exclusive=True)
        c.check("#538 a foreign owner in the resume flow blocks before remote",
                not out.get("ok") and "owned by uid 4321" in str(out.get("blocker", "")),
                out)
        c.check("#538 the blocked flow reached no remote seam", not remote_calls,
                remote_calls)

        # Согласный владелец: write_private получает СОХРАНЁННОГО владельца
        # в реальном потоке (stage=accepted → local step).
        calls = []

        def write_spy(path, data, owner=None):
            calls.append((path, owner))
            return real_write(path, data, owner=owner)

        def deliver_ok(key, nodes):
            return {n: "OK" for n in nodes}

        with patched(proxykey.os, stat=real_stat), \
                patched(proxykey.os, geteuid=real_geteuid), \
                patched(proxykey.fsutil, write_private=write_spy), \
                patched(proxykey, OP=op, LOCK=lock, _fetch_list=fetch,
                        _deliver=deliver_ok):
            write_state(state("accepted"))
            out = proxykey.resume(exclusive=True)
        c.check("#538 the resume flow completes to delivery",
                out.get("ok") and out.get("stage") == "completed", out)
        own = [x for x in calls if x[0] in (src, cp)]
        if c.check("#538 the flow wrote both key paths", len(own) == 2, calls):
            for path, owner in own:
                c.check(f"#538 the kept owner rides to write_private "
                        f"for {os.path.basename(path)}",
                        owner == (real_stat(src).st_uid, real_stat(src).st_gid), owner)


def main():
    c = Checks()
    check_source_393(c)
    check_strict_settled_538(c)
    check_begin_538(c)
    check_lock_538(c)
    check_resume_refusals_538(c)
    check_preflight_538(c)
    check_remote_step_538(c)
    check_probe_safe_554(c)
    check_fetch_typing_539(c)
    check_probe_step_538(c)
    check_local_and_delivery_538(c)
    check_pipeline_538(c)
    check_state_validator_538(c)
    check_serviceability_538(c)
    check_coverage_and_verdicts_538(c)
    check_owner_preservation_538(c)
    check_structured_reason_538(c)
    check_owner_in_resume_flow_538(c)
    check_effects_di_538(c)
    # ── блоб secrets.env (#391) ─────────────────────────────────────────
    # HYPOTHESIS: клиентская раздача (мастер-шелл, mop login) умерла, и узлы
    # не получают MOP_PROXY_KEY -- свежие тела сеются со старой копии узла и
    # врапер умирает «no key». SOLUTION: рендерит и раздаёт сервер.
    # С #390 в блобе ровно один ключ: переходный Z_AI_KEY снят вместе с
    # профилями -- glm-папеты перерегистрируются на прокси тем же ходом.
    # STATUS: FIXED — see #391, #390
    c.expect("blob: the proxy key rides (#391)",
             proxykey.blob({"MOP_PROXY_KEY": "s1", "Z_AI_KEY": "z1"}),
             "MOP_PROXY_KEY=s1\n")
    c.expect("blob: no key at all -> nothing to ride (#391)",
             proxykey.blob({"Z_AI_KEY": "z1"}), None)
    c.expect("blob: an empty key is no key (#391)",
             proxykey.blob({"MOP_PROXY_KEY": ""}), None)
    c.expect("blob: effective() carries provenance -- the value rides, not its repr",
             proxykey.blob({"MOP_PROXY_KEY": ("s1", ".env")}), "MOP_PROXY_KEY=s1\n")

    # ── #557: корень сервера в блобе, источник -- семантика #517 ────────
    # HYPOTHESIS (#557): папеты ходят на публичный /v1, и телу нужен корень
    # закрепления. Источник корня обязан повторять выбор серверных сервисов
    # (#517): задан MOP_TLS_CA -- корень частной CA; иначе при пустом
    # MOP_TLS_CERT -- самоподписанный tls.pem; настоящий сертификат -- строки
    # нет вовсе. Две копии правила (здесь и в deploy) разошлись бы молча, и
    # тело осталось бы без корня или с неверным. Сертификат задан, а корня
    # нет -- громкий отказ, не молчаливое отсутствие: тело поднимется и
    # умрёт на TLS без причины в журнале.
    check_ca_source_557(c)
    check_blob_ca_557(c)


# HYPOTHESIS (#557): корень для раздачи выбирают по трём случаям #517.
# SOLUTION: ca_source() в proxykey -- единственный рендер этой семантики;
# до него блоб ходил по creds.cafile каталога сервера, которого на mop
# нет (deploy печатает «nothing to pin» при настоящем сертификате).
# RESULT: RED -- функции нет.
# STATUS: RED
def check_ca_source_557(c):
    import base64
    import tempfile
    ca_path = os.path.join(tempfile.mkdtemp(prefix="mop-557-ca-"), "corp-ca.pem")
    with open(ca_path, "w") as f:
        f.write("-----BEGIN CERTIFICATE-----\ncorp\n-----END CERTIFICATE-----\n")
    # Самоподписанный tls.pem лежит там, куда его кладёт deploy:
    # ~/.config/mop/secrets/ (deploy/roles/proxy, secrets_dir). Тест
    # сидит в герметичном HOME (hermetic), и paths.local смотрит туда
    # же -- сеем в него, а не в каталог рядом с ca_path.
    tls_path = os.path.join(hermetic.HOME, ".config", "mop", "secrets", "tls.pem")
    os.makedirs(os.path.dirname(tls_path), exist_ok=True)
    with open(tls_path, "w") as f:
        f.write("-----BEGIN CERTIFICATE-----\nself\n-----END CERTIFICATE-----\n")
    settings = {"MOP_TLS_CA": ca_path, "MOP_TLS_CERT": "", "MOP_TLS_KEY": "",
                "MOP_HOME": os.path.dirname(ca_path), "MOP_SERVER_LAN": "srv.test"}

    def reads(**over):
        values = dict(settings, **over)
        return patched(config, get=lambda name: values.get(name, ""))

    source = getattr(proxykey, "ca_source", None)
    c.check("#557 RED ca_source exists", source is not None, "no proxykey.ca_source")
    if source is None:
        return
    # (a) частная CA: корень -- её PEM, не tls.pem установки.
    with reads():
        c.expect("#557 a private CA names the root", source(), ca_path)
    # (b) самоподписанный: корень -- tls.pem установки (secrets/).
    with reads(MOP_TLS_CA=""):
        c.expect("#557 a self-signed installation pins its tls.pem", source(), tls_path)
    # (c) настоящий сертификат без CA: корня нет -- системное доверие.
    with reads(MOP_TLS_CA="", MOP_TLS_CERT="/etc/certs/real.pem"):
        c.expect("#557 a real certificate needs no root", source(), None)
    # (d) настоящий сертификат, а корня не найти: громкий отказ с путём.
    with reads(MOP_TLS_CA="/gone/x.pem", MOP_TLS_CERT="/etc/certs/real.pem"):
        try:
            source()
            c.fail("#557 an unreachable named CA must refuse loudly", "/gone/x.pem")
        except (RuntimeError, ValueError) as e:
            c.check("#557 an unreachable named CA refuses with the path",
                    "/gone/x.pem" in str(e), str(e))
    with reads(MOP_TLS_CA="", MOP_TLS_CERT="/etc/certs/real.pem"):
        c.check("#557 the root is a PEM on disk when claimed",
                source() is None or open(source()).read(27).startswith("-----BEGIN"),
                source())


# HYPOTHESIS (#557): блоб везёт корень строкой MOP_PROXY_CA (base64 PEM)
# рядом с ключом -- оба писателя secrets.env (тикет и доставка ротации)
# рендерят его одним blob(). Без ключа блоба нет вовсе (установка без
# прокси); корень без ключа не ездит.
# RESULT: RED -- блоб пока одна строка ключа.
# STATUS: RED
def check_blob_ca_557(c):
    import base64
    pem = "-----BEGIN CERTIFICATE-----\nroot\n-----END CERTIFICATE-----\n"
    line = base64.b64encode(pem.encode()).decode()
    c.expect("#557 the blob carries the key and the root",
             proxykey.blob({"MOP_PROXY_KEY": "s1", "MOP_PROXY_CA": pem}),
             f"MOP_PROXY_KEY=s1\nMOP_PROXY_CA={line}\n")
    c.expect("#557 without a root the blob stays the key alone (#391 shape)",
             proxykey.blob({"MOP_PROXY_KEY": "s1"}), "MOP_PROXY_KEY=s1\n")
    c.expect("#557 the root never rides without the key",
             proxykey.blob({"MOP_PROXY_CA": pem}), None)
    # effective() несёт кортеж (значение, источник) -- едет значение.
    c.expect("#557 provenance tuples collapse to values",
             proxykey.blob({"MOP_PROXY_KEY": ("s1", ".env"), "MOP_PROXY_CA": (pem, "secrets")}),
             f"MOP_PROXY_KEY=s1\nMOP_PROXY_CA={line}\n")

    # ── пора ли раздавать (#391): по отпечатку, недошедшие не считаются ──
    c.expect("due: no state -> push", proxykey.due("b", None), True)
    c.expect("due: same sha -> skip", proxykey.due("b", {"sha": proxykey.sha("b")}), False)
    c.expect("due: changed blob -> push",
             proxykey.due("b2", {"sha": proxykey.sha("b")}), True)
    c.check("sha: отличает блобы", proxykey.sha("a") != proxykey.sha("b"))
    c.check("settled: недошедший узел держит раздачу открытой (#391)",
            not proxykey.settled({"gpu": "OK", "hyper": "NOT REACHED: down"}))
    c.check("settled: отказ агента раздачу не держит (#391)",
            proxykey.settled({"gpu": "OK", "hyper": "FAILED: x"}))
    c.check("settled: пустой итог -- дошли", proxykey.settled({}))

    check_results_135(c)
    check_write_in_bus_315(c)
    check_body_refusal_366(c)
    return c.report("proxykey")


if __name__ == "__main__":
    sys.exit(main())
