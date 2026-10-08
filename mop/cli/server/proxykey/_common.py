"""Общее у глаголов `mop server proxykey`: граница, эффекты, печать."""
import json
import sys

from mop.cli import lib
from mop.server import proxyapi, proxykey


def refusal():
    """Граница контроллера и пригодность эффектов ДО всего. -> str | None."""
    why = proxyapi.refusal_if_not_controller()
    if why:
        return why
    return proxyapi.port_refusal()


def effects():
    """Адаптер #539 для оркестратора #538: единственная точка, где
    появляются настоящие транспорт-швы."""
    return proxyapi.Effects()


def recipients(argv_opts):
    """Явный набор получателей из опций start. -> [имя] | str-отказ.

    --nodes n1,n2 -- явный список (повторяемый флаг суммируется);
    --all-registered -- полный известный состав БЕЗ ready-фильтра;
    --no-recipients -- осознанный пустой набор (пустой пул). Недоступный
    состав -- блокер ДО эффекта, не пустой набор."""
    if "--no-recipients" in argv_opts:
        return []
    if "--all-registered" in argv_opts:
        why = proxyapi.nodes_refusal()
        if why:
            return why
        return proxyapi.all_nodes()
    raw = argv_opts.get("--nodes", "")
    if not raw:
        return ("start needs a recipient set: --nodes n1,n2, "
                "--all-registered, or --no-recipients")
    out = []
    for name in raw.split(","):
        name = name.strip()
        if name:
            out.append(name)
    return out or []


def show(out, as_json):
    """Итог операции: тихий быстрый успех, явный JSON/текст при
    pending/отказе. Ключей и raw-причин здесь нет -- safe-проекция
    библиотеки. -> rc."""
    if as_json:
        print(json.dumps(out))
    elif not out.get("ok"):
        why = out.get("blocker") or out.get("note") or out.get("stage")
        print(f"pending: {why}", file=sys.stderr)
    elif out.get("stage") == "completed":
        print("completed")
    return 0 if out.get("ok") else 1
