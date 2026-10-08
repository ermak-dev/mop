"""Общее у глаголов `mop server proxykey`: граница, эффекты, печать."""
import json
import os
import sys

from mop.cli import lib
from mop.server import proxyapi, proxykey


def refusal():
    """Граница контроллера и пригодность эффектов ДО всего. -> str | None."""
    why = proxyapi.refusal_if_not_controller()
    if why:
        return why
    why = proxyapi.port_refusal()
    if why:
        return why
    path = proxyapi.mgmt_key_path()
    if not os.path.isfile(path):
        return f"no management key at {path} (the controller's secrets)"
    return None


def resolve_refusal():
    """Пригодность путей ДО begin: неабсолютный или пустой MOP_HOME --
    отказ, не путь от CWD. -> str | None."""
    return proxyapi.resolve_refusal()


def effects():
    """Адаптер #539 для оркестратора #538: единственная точка, где
    появляются настоящие транспорт-швы."""
    return proxyapi.Effects()


def recipients(argv_opts):
    """Явный набор получателей из опций start. -> [имя] | str-отказ.

    --nodes n1,n2 -- явный список (повторяемый флаг суммируется), каждое
    имя -- чистый токен субъекта и известный узел (отказ до begin);
    --all-registered -- полный известный состав БЕЗ ready-фильтра, ОДИН
    защищённый вызов; --no-recipients -- осознанный пустой набор.
    Конфликт флагов -- отказ, не молчаливый выбор одного. Недоступный
    состав -- блокер ДО эффекта, не пустой набор."""
    given = [flag for flag in ("--nodes", "--all-registered",
                               "--no-recipients") if flag in argv_opts
             and (flag != "--nodes" or argv_opts.get("--nodes"))]
    if len(given) > 1:
        return ("start needs one recipient set, got "
                + " and ".join(given))
    if "--no-recipients" in argv_opts:
        return []
    if "--all-registered" in argv_opts:
        got = proxyapi.all_nodes_guarded()
        if isinstance(got, str):
            return got
        return got
    raw = argv_opts.get("--nodes", "")
    if not raw:
        return ("start needs a recipient set: --nodes n1,n2, "
                "--all-registered, or --no-recipients")
    out = []
    for name in raw.split(","):
        name = name.strip()
        if name:
            out.append(name)
    why = proxyapi.node_refusals(out)
    if why:
        return why
    return out


def show(out, as_json, quiet_recorded=False):
    """Итог операции: тихий быстрый успех, явный JSON/текст при
    pending/отказе. Ключей и raw-причин здесь нет -- safe-проекция
    библиотеки. Слово блокера по типизированным полям: 401 --
    предупреждение о блокировке петли (5 неудач management запирают
    адрес на 30 минут, исключения для 127.0.0.1 нет), 403 -- «доступ
    запрещён», не «ключ неверен».

    quiet_recorded (start): операция записана -- rc 0, тихо или одной
    строкой JSON по явному запросу; скрипт обязан отличать удачный
    start от отказа. resume остаётся прежним: completed -- 0,
    pending/отказ -- 1. -> rc."""
    # as_json первым: quiet_recorded раньше него глотал и явный запрос
    # JSON (тихий rc 0 без строки). Отказ (ok=False) тихим не бывает ни
    # в одном режиме.
    if as_json:
        recorded = quiet_recorded and out.get("stage") \
            and out.get("ok") is not False
        print(json.dumps(out))
        # Проекция begin несёт только id/schema/stage/recipients --
        # «ok» в ней нет, это записанное состояние, не отказ.
        return 0 if out.get("ok") or recorded else 1
    if quiet_recorded and out.get("stage") and out.get("ok") is not False:
        return 0
    if not out.get("ok"):
        why = out.get("blocker") or out.get("note") or out.get("stage")
        extra = _wording(out)
        print(f"pending: {why}{extra}", file=sys.stderr)
    elif out.get("stage") == "completed":
        print("completed")
    return 0 if out.get("ok") else 1


def _wording(out):
    """Типизированное дополнение к блокеру по http_status/reason. -> str.

    Только перечислимые факты безопасной проекции, ничего из шва."""
    status = out.get("http_status")
    reason = out.get("reason")
    if status == 401 and reason == "unauthorized":
        return (" — the management key is wrong or missing; repeated "
                "attempts lock this address out for 30 minutes, with "
                "no exception for the loopback")
    if status == 403 and reason == "forbidden":
        return " — access denied; the address may be locked out after failed attempts"
    if reason == "invalid":
        return " — validation refused the write before applying"
    return ""
