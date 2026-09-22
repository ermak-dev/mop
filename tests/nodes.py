#!/usr/bin/env python3
"""Проверка строк узлов без кластера: python3 tests/nodes.py

Строка узла собирается из трёх ответов Nomad — сводки узла, его meta и
ёмкости — и её читают два фронтенда, `mop node` и инструмент nodes в MCP.
Состояние планирования здесь единственное место с логикой: draining
старше closed, и узел без драйвера в meta — host, а не пусто (#49).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import nodes  # noqa: E402

CASES = [
    # (сводка, meta, ёмкость, ожидаемая строка)
    ({"Name": "gpu", "Status": "ready"}, {}, {"free_mb": 40960, "total_mb": 40960, "slots": 5},
     {"name": "gpu", "driver": "host", "serves": "-", "state": "ready",
      "free_mb": 40960, "total_mb": 40960, "slots": 5}),
    ({"Name": "hyper", "Status": "ready", "Drain": True, "SchedulingEligibility": "ineligible"},
     {"mop_driver": "pve", "mop_shards": "mop,rugent"}, {},
     {"name": "hyper", "driver": "pve", "serves": "mop,rugent", "state": "ready, draining",
      "free_mb": None, "total_mb": None, "slots": None}),
    ({"Name": "mate", "Status": "ready", "SchedulingEligibility": "ineligible"}, {}, {},
     {"name": "mate", "driver": "host", "serves": "-", "state": "ready, closed",
      "free_mb": None, "total_mb": None, "slots": None}),
]


def main():
    failed = 0
    for summary, meta, cap, want in CASES:
        got = nodes.row(summary, meta, cap)
        if got != want:
            failed += 1
            print(f"FAIL row({summary['Name']}): {got} != {want}")
    print("nodes: FAILED" if failed else "nodes: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
