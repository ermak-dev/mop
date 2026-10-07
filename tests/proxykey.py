#!/usr/bin/env python3
"""Раздача ключа прокси без пула: python3 tests/proxykey.py

Сервер -- единственный раздатчик ключа LLM-прокси (#391): рендер блоба и
решение «пора ли снова» -- чистые функции, сама раздача -- только на живом
пуле. Здесь же итоги глагола write (bus.results_from): клиентский
раздатчик keys.py умер (#385), потребитель один -- этот модуль.
"""
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


def main():
    c = Checks()
    check_source_393(c)
    check_strict_settled_538(c)
    check_begin_538(c)
    check_lock_538(c)
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
