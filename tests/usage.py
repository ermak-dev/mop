#!/usr/bin/env python3
"""Проверка разбора транскриптов без пула: python3 tests/usage.py

Ошибка здесь незаметна на глаз: завышенный вдвое расход — просто большая
цифра. Ответ API с несколькими блоками пишется в jsonl несколькими строками
с одним message.id и одним usage, и считать его надо один раз.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import usage  # noqa: E402

NOW = datetime(2026, 9, 8, 12, 0)


def rec(msg_id, ts, out=10, kind="assistant", block=0):
    return json.dumps({
        "type": kind, "timestamp": ts, "apiBlockIndex": block,
        "message": {"id": msg_id, "usage": {
            "input_tokens": 1, "output_tokens": out,
            "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}}})


def write(root, rel, lines):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    return p


# ── #244: расход по логину мастера ──────────────────────────────────────────
# HYPOTHESIS: usage.py суммирует расход по папету и дню, но не говорит, чья
# это работа: оператор не видит, кто сколько тратит. Каждое сообщение
# мастера лежит в транскрипте конвертом `<cross-session-message ...
# from-name="<логин>.<хост>-<pid>">` (логин — первый токен адреса, #213).
# SOLUTION: ответ приписывается логину последнего конверта, увиденного до него
# в том же транскрипте; до первого конверта и у адреса без логина — «-».
# Субагент наследует логин хода родителя, который его запустил: toolUseId из
# <agent>.meta.json — это id вызова инструмента в транскрипте родителя.
# Формы записей сняты с живого транскрипта папета (Claude Code 2.1, 24.09).
# STATUS: FIXED — see #244
def env_user(name, ts, head="Another Claude session sent a message:\n"):
    """Доставленное сообщение: user, isMeta, строка с конвертом и origin."""
    body = f'<cross-session-message from-name="{name}" from-mode="bypass">\nhi\n</cross-session-message>'
    return json.dumps({"type": "user", "isMeta": True, "timestamp": ts,
                       "origin": {"kind": "peer", "from": "unknown", "name": name},
                       "message": {"role": "user", "content": head + body}})


def env_attachment(name, ts):
    """Сообщение, пришедшее посреди хода: attachment queued_command."""
    return json.dumps({"type": "attachment", "timestamp": ts, "attachment": {
        "type": "queued_command",
        "prompt": f'<cross-session-message from-name="{name}" from-mode="bypass">\nmid\n'
                  f'</cross-session-message>'}})


def env_queued(name, ts):
    """Постановка в очередь — ещё не доставка."""
    return json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": ts,
                       "content": f'<cross-session-message from-name="{name}" '
                                  f'from-mode="bypass">\nq\n</cross-session-message>'})


def tool_text(ts):
    """Конверт внутри результата инструмента — папет читал исходник."""
    return json.dumps({"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t0",
         "content": '<cross-session-message from-name="eve.h-1" from-mode="bypass">'}]}})


def spawn(msg_id, ts, tool_id, out=10):
    """Ответ с вызовом инструмента, который запускает субагента."""
    d = json.loads(rec(msg_id, ts, out))
    d["message"]["content"] = [{"type": "tool_use", "id": tool_id, "name": "Agent", "input": {}}]
    return json.dumps(d)


def check_by_login():
    out = []
    fn = getattr(usage, "scan_all", None)
    if fn is None:
        return ["usage.scan_all is missing"]
    day = NOW.date().isoformat()
    t = NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def row(o):
        return {"input": 1, "output": o, "cache_write": 100, "cache_read": 1000}

    def add(*rows):
        return {k: sum(r[k] for r in rows) for k in usage.KINDS}
    with tempfile.TemporaryDirectory() as d:
        write(d, "s1.jsonl", [
            rec("m0", t, out=1),                                   # до конверта
            env_user("anton.mate-1", t),
            rec("m1", t, out=10, block=0), rec("m1", t, out=10, block=1),
            env_queued("olga.mate-2", t),                          # не доставка
            tool_text(t),                                          # не доставка
            rec("m2", t, out=20),
            env_user("olga.mate-2", t),
            spawn("m3", t, "toolu_A", out=30),
            env_attachment("anton.mate-1", t),                     # посреди хода
            rec("m4", t, out=40),
            env_user("mate-722140", t),                            # адрес до #213
            rec("m5", t, out=50),
            env_user("mop", t),
            rec("m6", t, out=60),
        ])
        # Субагент, запущенный ходом olga, и субагент без meta.
        write(d, "s1/subagents/agent-a.jsonl", [rec("a1", t, out=7)])
        write(d, "s1/subagents/agent-a.meta.json", ['{"toolUseId": "toolu_A"}'])
        write(d, "s1/subagents/agent-b.jsonl", [rec("b1", t, out=8)])
        # Форк: история скопирована в новый файл вместе со своими конвертами,
        # m1 считается один раз и остаётся за anton при любом порядке файлов.
        write(d, "s2.jsonl", [env_user("anton.mate-1", t), rec("m1", t, out=10),
                              env_user("carol.x-3", t), rec("c1", t, out=9)])
        total, by = fn(d, 14, now=NOW)
    want = {"-": {day: add(row(1), row(50), row(60), row(8))},
            "anton": {day: add(row(10), row(20), row(40))},
            "olga": {day: add(row(30), row(7))},
            "carol": {day: row(9)}}
    if by != want:
        out.append(f"by_login:\n  got  {by}\n  want {want}")
    # Инвариант: сумма по логинам за папет и день — прежняя строка usage.
    summed = {}
    for rows in by.values():
        usage.merge(summed, rows)
    if summed != total:
        out.append(f"sum(by_login) {summed} != usage {total}")
    with tempfile.TemporaryDirectory() as d:
        write(d, "s1.jsonl", [env_user("anton.mate-1", t), rec("m1", t)])
        if usage.scan(d, 14, now=NOW) != fn(d, 14, now=NOW)[0]:
            out.append("scan must equal scan_all's first half")
    for addr, want in (("anton.mate-722140", "anton"), ("mate-722140", "-"),
                       ("mop", "-"), ("", "-"), (".x-1", "-")):
        got = usage.login_of(addr) if hasattr(usage, "login_of") else None
        if got != want:
            out.append(f"login_of({addr!r}) -> {got!r}, want {want!r}")
    return out


def main():
    failed = 0
    for line in check_by_login():
        failed += 1
        print(f"FAIL {line}")
    with tempfile.TemporaryDirectory() as d:
        today = NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        old = (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        write(d, "s1.jsonl", [
            rec("m1", today, block=0), rec("m1", today, block=1), rec("m1", today, block=2),
            rec("m2", today, out=5),
            rec("m3", old),                          # за окном
            rec("m4", today, kind="user"),           # не ответ
            "not json at all",
        ])
        # Субагент — этажом ниже, и его ответы считаются. Плюс форк сессии:
        # тот же m2 скопирован в новый файл.
        write(d, "s1/subagents/agent-1.jsonl", [rec("a1", today, out=7)])
        write(d, "s2.jsonl", [rec("m2", today, out=5)])
        got = usage.scan(d, 14, now=NOW)
        day = NOW.date().isoformat()
        want = {"input": 3, "output": 22, "cache_write": 300, "cache_read": 3000}
        if got != {day: want}:
            failed += 1
            print(f"FAIL scan: {got} != {{{day!r}: {want}}}")
    if usage.slug("/home/u/puppets/pu-a.b_1") != "-home-u-puppets-pu-a-b-1":
        failed += 1
        print("FAIL slug")
    axis = usage.days_back(3, now=NOW)
    if axis != ["2026-09-06", "2026-09-07", "2026-09-08"]:
        failed += 1
        print(f"FAIL days_back: {axis}")
    print("usage: FAILED" if failed else "usage: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
