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

from mop.common import bus  # noqa: E402
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

        with patched(proxykey, KEY_FILE=path), patched(proxykey.config,
                                                     effective=lambda: old), \
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
            base = {"id": "x" * 16, "schema": proxykey.SCHEMA, "old": "old-key",
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

            # Терминальное завершённое состояние заменяемо новым begin.
            state(stage="completed")
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
        state = {"id": "x" * 16, "schema": proxykey.SCHEMA, "old": "old-key",
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

        # Собственный recoverable сплит: источник уже new, копия ещё old --
        # это чекпоинт операции, не drift.
        put(src, "new-key\n")
        c.check("#538 preflight accepts its own source-ahead split",
                fn(state) is None, "must be None")

        # Третье значение в источнике -- drift.
        put(src, "third-key\n")
        r = fn(state)
        c.check("#538 a third source value is a named drift blocker",
                isinstance(r, str) and "drift" in r.lower() and src in r, r)

        # Третье значение в копии -- тоже drift.
        put(src, "new-key\n")
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
        put(src, "new-key\n")
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
            base = {"id": "x" * 16, "schema": proxykey.SCHEMA, "old": "old-key",
                    "new": "new-key", "source": src, "copy": cp,
                    "recipients": ["node1"], "stage": "ready"}
            base.update(over)
            return base

        # Согласованный путь: старый в списке, PUT несёт объединение со
        # ВСЕМИ чужими ключами, new принимается, old остаётся.
        remote = FakeRemote(["old-key", "panel-key"])
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state())
        c.check("#538 the remote step succeeds", out.get("ok"), out)
        c.expect("#538 PUT carries every read key plus new (unmanaged kept)",
                 remote.puts, [["old-key", "panel-key", "new-key"]])
        c.check("#538 the accepted step advances past the remote stage",
                out.get("stage") != "ready", out)
        with open(op, encoding="utf-8") as f:
            saved = json.load(f)
        c.check("#538 the remote checkpoint is saved",
                saved.get("stage") == out.get("stage"), saved)

        # Подтверждённый отказ ДО применения -- блокер с причиной, список
        # прокси не изменён, ключи не потеряны.
        remote = FakeRemote(["old-key", "panel-key"],
                            refuse_put=("refused", "403 forbidden"))
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state())
        c.check("#538 a confirmed pre-apply refusal blocks with the reason",
                not out.get("ok") and "403" in str(out.get("blocker")), out)
        c.check("#538 a refused PUT did not change the proxy list",
                remote.keys == ["old-key", "panel-key"], remote.keys)

        # Unknown-исход (timeout ПОСЛЕ возможного применения) -- НЕ отказ:
        # чекпоинт остаётся, повторное применение идёт по свежему списку.
        remote = FakeRemote(["old-key", "panel-key"], unknown_put=True)
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state())
        c.check("#538 an unknown PUT outcome keeps the operation open",
                not out.get("ok") and "unknown" in str(out.get("blocker", "")).lower(),
                out)

        # Resume после unknown: тот же new, свежий список (панель успела
        # добавить ключ) -- объединение, чужой ключ не стёрт.
        remote = FakeRemote(["old-key", "panel-key"])
        remote.keys.append("late-panel-key")
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state())
        c.check("#538 resume after unknown re-applies on a fresh list", out.get("ok"), out)
        c.expect("#538 the late panel key survives the re-apply",
                 remote.puts[-1], ["old-key", "panel-key", "late-panel-key", "new-key"])

        # Пустой прочитанный список -- запрет записи ВСЕГДА.
        remote = FakeRemote([])
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state())
        c.check("#538 an empty read list is never written",
                not out.get("ok") and not remote.puts, (out, remote.puts))

        # Старый ключ отсутствует в списке до начала -- drift, а не
        # самовольное восстановление отозванного.
        remote = FakeRemote(["panel-key"])
        with patched(proxykey, OP=op, _fetch_list=remote.fetch_list,
                     _put_list=remote.put_list, _probe=remote.probe):
            out = step(state())
        c.check("#538 a missing managed old key is drift before mutation",
                not out.get("ok") and "drift" in str(out.get("blocker", "")).lower()
                and not remote.puts, (out, remote.puts))


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
            return {"id": "x" * 16, "schema": proxykey.SCHEMA, "old": "old-key",
                    "new": "new-key", "source": os.path.join(d, "s"),
                    "copy": os.path.join(d, "c"), "recipients": ["node1"],
                    "stage": stage}

        # Полное подтверждение: new принят, old принят, неверный отвергнут.
        answers = {"old-key": True, "new-key": True}
        wrong_seen = []
        with patched(proxykey, OP=op, _probe=lambda k: wrong_seen.append(k) or answers.get(k, False)):
            out = step(state())
        c.check("#538 a fully confirmed application advances", out.get("ok"), out)
        c.check("#538 the wrong-key probe ran (negative check is required)",
                wrong_seen and wrong_seen[-1] not in answers, wrong_seen)
        c.check("#538 the confirmed step moves past listed",
                out.get("stage") not in (None, "listed"), out)

        # Async-применение ещё не догнало: probe new = False -- pending,
        # не отказ и не смена ключа/источника.
        answers = {"old-key": True, "new-key": False}
        with patched(proxykey, OP=op, _probe=lambda k: answers.get(k, False)):
            out = step(state())
        c.check("#538 a not-yet-applied new key is pending with a reason",
                not out.get("ok") and "pending" in str(out.get("blocker", "")).lower(),
                out)
        c.check("#538 a pending probe keeps the stage",
                out.get("stage", "listed") == "listed", out)

        # Old перестал приниматься до выдачи -- блокер: раздача оставила бы
        # узлы без работающего ключа.
        answers = {"old-key": False, "new-key": True}
        with patched(proxykey, OP=op, _probe=lambda k: answers.get(k, False)):
            out = step(state())
        c.check("#538 a rejected old key blocks before issuing",
                not out.get("ok") and "old" in str(out.get("blocker", "")).lower(), out)

        # Негативная проверка провалилась: неверный ключ принимается --
        # авторизация не включена, выдавать ключ нельзя.
        answers = {"old-key": True, "new-key": True}
        with patched(proxykey, OP=op, _probe=lambda k: True):
            out = step(state())
        c.check("#538 an accepted wrong key blocks: auth is off",
                not out.get("ok") and "wrong" in str(out.get("blocker", "")).lower(), out)


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
            return {"id": "x" * 16, "schema": proxykey.SCHEMA, "old": "old-key",
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
            out = local(state())
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
            out = local(state())
        c.check("#538 a crashed split is finished, not refused",
                out.get("ok") and open(cp).read().strip() == "new-key", out)

        # Раздача: замороженные получатели, итог -- strict_settled.
        answers = {"node1": "OK", "node2": "FAILED: body down"}
        sent = []

        def write(verb, nodes, timeout=None, files=None):
            sent.append((nodes, files))
            return dict(answers)

        with patched(proxykey, OP=op, _deliver=write):
            out = deliver(state(stage="local"))
        c.check("#538 a FAILED recipient keeps the delivery open",
                not out.get("ok"), out)
        c.check("#538 delivery goes to the frozen saved recipients",
                sent and sent[0][0] == ["node1", "node2"], sent)
        with open(op, encoding="utf-8") as f:
            saved = json.load(f)
        c.expect("#538 the per-recipient verdicts are checkpointed",
                 saved.get("delivered"), answers)

        answers = {"node1": "OK", "node2": "OK"}
        with patched(proxykey, OP=op, _deliver=write):
            out = deliver(state(stage="local"))
        c.check("#538 all recipients OK complete the delivery",
                out.get("ok") and out.get("stage") != "local", out)

        # Пустой сохранённый список -- честный ноль, не «все обновлены».
        with patched(proxykey, OP=op, _deliver=write):
            out = deliver({**state(stage="local"), "recipients": []})
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

        real_preflight = proxykey._preflight

        def preflight(state):
            order.append("preflight")
            return real_preflight(state)

        patches = dict(OP=op, LOCK=lock, _fetch_list=fetch, _put_list=put,
                       _probe=probe, _deliver=deliver, _preflight=preflight)
        with patched(proxykey, **patches):
            got = proxykey.begin(src, cp, ["node1", "node2"], exclusive=True)
            c.expect("#538 begin names the ready stage", got.get("stage"), "ready")
            with open(op, encoding="utf-8") as f:
                saved = json.load(f)
            c.check("#538 begin did not run the pipeline",
                    not order, order)

            out = proxykey.resume(exclusive=True)
            c.check("#538 a clean pipeline completes", out.get("ok"), out)
            c.check("#538 the pipeline ran preflight, remote, probes, local, deliver",
                    order == ["preflight", "fetch", "put", "probe", "probe",
                              "probe", "deliver"], order)
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

        good = {"id": "x" * 16, "schema": proxykey.SCHEMA, "old": "old-key",
                "new": "new-key", "source": os.path.join(d, "s"),
                "copy": os.path.join(d, "c"), "recipients": ["node1"],
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

def main():
    c = Checks()
    check_source_393(c)
    check_strict_settled_538(c)
    check_begin_538(c)
    check_lock_538(c)
    check_resume_refusals_538(c)
    check_preflight_538(c)
    check_remote_step_538(c)
    check_probe_step_538(c)
    check_local_and_delivery_538(c)
    check_pipeline_538(c)
    check_state_validator_538(c)
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
