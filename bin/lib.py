#!/usr/bin/env python3
"""Общая часть питоновских командлетов.

Живёт в bin/, а не в пакете, намеренно: здесь ПЕЧАТАЮТ. Библиотека `mop`
возвращает данные и молчит — иначе MCP-сервер начал бы разбирать текст,
свёрстанный для терминала.

Командлет пользуется этим так:

    import lib
    from mop import slaves

    def main(argv):
        ...
    lib.run(main)
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import bus, keys, nomad, slaves  # noqa: E402


def run(fn, argv=None):
    """Запустить командлет, переведя ожидаемые отказы в понятную строку.

    Трассировка питона в ответ на «нет связи с Nomad» — это шум, за которым
    теряется единственное, что оператору нужно знать."""
    try:
        sys.exit(fn(sys.argv[1:] if argv is None else argv) or 0)
    except (ConnectionError, RuntimeError, LookupError, bus.BusError) as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit(130)


def usage(doc):
    sys.exit(doc.strip())


def cwd_origin(required=True):
    """origin текущей рабочей копии. required=False -> None вместо отказа."""
    try:
        return subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        if required:
            sys.exit("не рабочая копия git и origin не указан: mop add <git-origin>")
        return None


def project_of(origin):
    return os.path.basename(origin).removesuffix(".git")


def parse_llm(args):
    """Выкусить --llm PROFILE (или --llm=PROFILE) откуда угодно в аргументах.
    -> (профиль | None, остальные аргументы)."""
    llm, rest, it = None, [], iter(args)
    for a in it:
        if a == "--llm":
            llm = next(it, "")
        elif a.startswith("--llm="):
            llm = a.split("=", 1)[1]
        else:
            rest.append(a)
    if llm is not None and llm not in slaves.LLM_PROFILES:
        sys.exit(f"нет LLM-профиля {llm or '(пусто)'}; есть: "
                 f"{', '.join(slaves.LLM_PROFILES)} (mop llm)")
    return llm, rest


def require_job(name):
    try:
        return nomad.get_job(name)
    except nomad.NotFound:
        sys.exit(f"нет такого слейва: {name}")


def running_alloc(name):
    a = nomad.latest_alloc(name)
    if not a or a["ClientStatus"] != "running":
        sys.exit(f"{name} не running")
    return a


def running_node(name):
    """Узел слейва — адрес для шины. Аллокация адресом быть перестала вместе
    с alloc exec; агент подписан на субъект узла."""
    return running_alloc(name)["NodeName"]


def push_llm_keys(llm):
    """Ключи профиля на узлы, с отчётом. Молчит для профилей без ключа."""
    results = keys.push_llm_keys(llm)
    if results is None:
        return
    print(f"раздаю ключи LLM на узлы пула ({slaves.LLM_KEYS_FILE})...")
    bad = [f"{n}: {r}" for n, r in sorted(results.items()) if r != "OK"]
    if bad:
        print("  не всем узлам: " + "; ".join(bad))


def pool_lines():
    try:
        out = []
        for n in slaves.pool():
            if n["status"] != "ready":
                out.append(f"  {n['name']}: {n['status']}")
            elif "error" in n:
                out.append(f"  {n['name']}: {n['error']}")
            else:
                out.append(f"  {n['name']}: свободно {n['free_mb'] / 1024:.0f}/"
                           f"{n['total_mb'] / 1024:.0f} ГБ ({n['slots']} слотов)")
        return out
    except Exception as e:
        return [f"  {nomad.describe_error(e)}"]
