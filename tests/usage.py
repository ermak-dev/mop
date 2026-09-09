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


def main():
    failed = 0
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
