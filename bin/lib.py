#!/usr/bin/env python3
"""Общая часть питоновских командлетов.

Живёт в bin/, а не в пакете, намеренно: здесь ПЕЧАТАЮТ. Библиотека `mop`
возвращает данные и молчит — иначе MCP-сервер начал бы разбирать текст,
свёрстанный для терминала.

Командлет пользуется этим так:

    import lib
    from mop import puppets

    def main(argv):
        ...
    lib.run(main)
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import bus, config, keys, llm, nomad, puppets  # noqa: E402


def run(fn, argv=None):
    """Запустить командлет, переведя ожидаемые отказы в понятную строку.

    Трассировка питона в ответ на «нет связи с Nomad» — это шум, за которым
    теряется единственное, что оператору нужно знать."""
    try:
        sys.exit(fn(sys.argv[1:] if argv is None else argv) or 0)
    except config.Missing as e:
        # Не трассировка и не «нет связи»: на новой машине это первое, обо что
        # спотыкаются, и отказ обязан читаться как инструкция.
        sys.exit(str(e))
    except (ConnectionError, RuntimeError, LookupError, bus.BusError) as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit(130)


def cluster(fn):
    """Командлет, которому нужен настроенный кластер. Проверка обязательных
    настроек — до первого сетевого вызова, чтобы отказ был про настройки, а не
    про таймаут к чужому адресу."""
    def wrap(argv):
        config.require()
        return fn(argv)
    return wrap


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
    """Проект он же шард. Определение одно на всю систему — в puppets."""
    return puppets.shard_of(origin)


def shard_creds(shard):
    return os.path.expanduser(f"~/.config/mop/bus-{shard}.json")


def in_shard():
    """Шард этого шелла, либо None у оператора вне `mop master`."""
    return None if bus.SHARD == bus.ADMIN else bus.SHARD


def guard(name):
    """Перила мастер-шелла: не трогать чужого папета.

    Это НЕ граница — MOP_SHARD оператор может и снять. Настоящая живёт в кредах
    NATS и в проверке агента. Здесь мы лишь не даём промахнуться вслепую."""
    shard = in_shard()
    if shard is None:
        return
    meta = require_job(name).get("Meta") or {}
    owner = puppets.shard_of(meta.get("origin", ""))
    if owner != shard:
        sys.exit(f"{name} — шард {owner}, а этот мастер ведёт {shard}. "
                 f"Выйди из мастер-шелла или запусти mop master для {owner}.")


def parse_llm(args):
    """Выкусить --llm PROFILE (или --llm=PROFILE) откуда угодно в аргументах.
    -> (профиль | None, остальные аргументы)."""
    profile, rest, it = None, [], iter(args)
    for a in it:
        if a == "--llm":
            profile = next(it, "")
        elif a.startswith("--llm="):
            profile = a.split("=", 1)[1]
        else:
            rest.append(a)
    if profile is not None:
        llm.require(profile)
    return profile, rest


def require_job(name):
    try:
        return nomad.get_job(name)
    except nomad.NotFound:
        sys.exit(f"нет такого папета: {name}")


def running_alloc(name):
    a = nomad.latest_alloc(name)
    if not a or a["ClientStatus"] != "running":
        sys.exit(f"{name} не running")
    return a


def running_node(name):
    """Узел папета — адрес для шины. Аллокация адресом быть перестала вместе
    с alloc exec; агент подписан на субъект узла."""
    return running_alloc(name)["NodeName"]


def push_llm_keys(llm):
    """Ключи профиля на узлы, с отчётом. Молчит для профилей без ключа."""
    results = keys.push_llm_keys(llm)
    if results is None:
        return
    print(f"раздаю секреты на узлы пула ({puppets.SECRETS_FILE})...")
    bad = [f"{n}: {r}" for n, r in sorted(results.items()) if r != "OK"]
    if bad:
        print("  не всем узлам: " + "; ".join(bad))


def pool_lines():
    try:
        out = []
        for n in puppets.pool():
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
