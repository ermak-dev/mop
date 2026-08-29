"""Раздача файлов на узлы пула: креды claude.ai и ключи LLM-провайдеров.

Основной путь — глагол `write` агенту узла: он есть на каждом узле независимо
от того, живёт там слейв или нет, и не требует свободной памяти. Раньше на его
месте был exec в аллокацию живого слейва — узлы пула забиты памятью под завязку
и sysbatch туда не садится (DimensionExhausted: memory).

Sysbatch остался запасным путём для узла, чей агент молчит. Он не exec, и
именно поэтому пережил переезд: раздача кредов не имеет права зависеть от
шины — на первый узел она везёт креды самой шины.
"""
import base64
import json
import os
import time

from . import bus, nomad, slaves

LOGIN_JOB = "sl-login"


def push_script(files):
    """files: [(абсолютный путь, base64 содержимого)] — атомарная запись, 600.

    b64-алфавит безопасен внутри одинарных кавычек, поэтому содержимое
    подставляется в скрипт как есть."""
    parts = ["set -e", "umask 077"]
    for path, b64 in files:
        parts.append(f'mkdir -p "{os.path.dirname(path)}"')
        parts.append(f"printf '%s' '{b64}' | base64 -d > \"{path}.tmp\"")
        parts.append(f'mv "{path}.tmp" "{path}"')
    return "; ".join(parts)


def push_spec(name, script, node_names):
    return {"Job": {
        "ID": name,
        "Name": name,
        "Datacenters": [nomad.POOL_DC],
        "Type": "sysbatch",
        "Constraints": [{
            "LTarget": "${node.unique.name}",
            "Operand": "regexp",
            "RTarget": "^(" + "|".join(node_names) + ")$",
        }],
        "TaskGroups": [{
            "Name": "login",
            "Count": 1,
            "Tasks": [{
                "Name": "login",
                "Driver": "raw_exec",
                "User": "ermak",
                "Config": {"command": "/bin/bash", "args": ["-c", script]},
                "Env": {"HOME": slaves.HOME},
                "Resources": {"CPU": 100, "MemoryMB": 64},
            }],
        }],
    }}


def distribute(files):
    """Разложить [(путь, b64)] по всем ready-узлам пула -> {узел: результат}."""
    script = push_script(files)
    nodes = nomad.ready_nodes()
    if not nodes:
        raise RuntimeError("в пуле нет ready-узлов")

    results = {}
    _push_via_agents(files, sorted(nodes), results)
    _push_via_sysbatch(script, sorted(nodes - set(results)), results)
    for node in nodes:
        results.setdefault(node, "НЕ ДОСТАЛСЯ — ни слейва, ни места под sysbatch")
    return results


def _push_via_agents(files, nodes, results):
    """Всем узлам разом — глаголом `write` их агентам.

    Агент пишет только в свой белый список путей; попытка привезти что-то ещё
    вернётся отказом, а не тихо запишется. Молчащий агент здесь не ошибка —
    узел просто уходит в запасной путь."""
    try:
        answers = bus.request_many(
            {n: {"verb": "write", "files": [list(f) for f in files]} for n in nodes})
    except bus.BusError:
        return
    for node, answer in answers.items():
        if isinstance(answer, Exception):
            continue
        results[node] = ("OK" if not answer.get("error")
                         else f"FAILED: {str(answer['error'])[:80]}")


def _push_via_sysbatch(script, nodes, results):
    """Пустые узлы — коротким sysbatch-джобом, после — purge, чтобы секреты не
    оставались в состоянии Nomad."""
    if not nodes:
        return
    try:
        nomad.deregister(LOGIN_JOB)
    except Exception:
        pass
    nomad.register(push_spec(LOGIN_JOB, script, nodes))
    try:
        for _ in range(60):
            time.sleep(1)
            try:
                allocs = nomad.client().job.get_allocations(LOGIN_JOB)
            except nomad.NotFound:
                continue
            pending = False
            for a in allocs:
                if a["ClientStatus"] in ("complete", "failed"):
                    results[a["NodeName"]] = ("OK" if a["ClientStatus"] == "complete"
                                              else "FAILED (sysbatch, смотри nomad UI)")
                else:
                    pending = True
            if allocs and not pending:
                break
    finally:
        try:
            nomad.deregister(LOGIN_JOB)
        except Exception:
            pass


def llm_keys_blob():
    """Что везти на узлы в llm-keys.env: ТОЛЬКО те ключи из локального файла,
    которые называет хоть один LLM-профиль. Остальным секретам из того файла
    (юкасса, телеграм, прочие провайдеры) на узлах пула делать нечего.
    -> (содержимое|None, замечание|None)"""
    wanted = {p["key"] for p in slaves.LLM_PROFILES.values() if p.get("key")}
    if not wanted:
        return None, None
    found = {}
    try:
        with open(os.path.expanduser(slaves.LOCAL_KEYS_FILE)) as f:
            for line in f:
                k, sep, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if sep and k in wanted and v:
                    found[k] = v
    except OSError:
        return None, f"нет {slaves.LOCAL_KEYS_FILE} — профили с ключом не поднимутся"
    missing = sorted(wanted - set(found))
    note = f"в {slaves.LOCAL_KEYS_FILE} нет: {', '.join(missing)}" if missing else None
    if not found:
        return None, note
    return "".join(f"{k}={v}\n" for k, v in sorted(found.items())), note


def _as_file(path, text_or_bytes):
    raw = text_or_bytes.encode() if isinstance(text_or_bytes, str) else text_or_bytes
    return (path, base64.b64encode(raw).decode())


def push_llm_keys(llm):
    """Ключ профиля обязан лежать на узле РАНЬШЕ слейва: без него врапер
    валится, а Nomad уводит слейв в restart-backoff. Узел заранее неизвестен
    (место выбирает планировщик), поэтому раздаём на весь пул.
    -> {узел: результат} либо None, если профилю ключ не нужен."""
    key = slaves.LLM_PROFILES[llm].get("key")
    if not key:
        return None
    blob, note = llm_keys_blob()
    if not blob or key not in blob:
        raise RuntimeError(f"профиль {llm}: {note}")
    return distribute([_as_file(slaves.LLM_KEYS_FILE, blob)])


def credentials():
    """Локальные креды claude.ai, годные к раздаче."""
    src = os.path.expanduser("~/.claude/.credentials.json")
    try:
        with open(src, "rb") as f:
            raw = f.read()
        json.loads(raw)
    except FileNotFoundError:
        raise RuntimeError(f"нет {src} — сначала залогиньтесь в claude на этой машине")
    except ValueError:
        raise RuntimeError(f"{src}: не валидный JSON, раздавать нечего")
    return raw


def credentials_fresh():
    """Годятся ли локальные креды: валидный JSON, expiresAt в будущем."""
    try:
        with open(os.path.expanduser("~/.claude/.credentials.json")) as f:
            c = json.load(f)
        return ((c.get("claudeAiOauth") or {}).get("expiresAt") or 0) / 1000 > time.time()
    except Exception:
        return False


def push_login():
    """Раздать креды claude.ai и ключи LLM. -> (результаты, что везли, замечание)

    Токен Nomad отсюда убран, и это не забывчивость. Его возили на узлы, чтобы
    узловой mop mcp дотягивался до соседей и до инбокса мастера, — и цена была
    названа прямо: слейв, добравшийся до файла, мог снести чужие джобы. Шина
    даёт ту же связь правами по субъектам, поэтому полномочия узлам больше не
    нужны. Старую копию файла с узлов сносит плейбук nats: перестать раздавать
    значит оставить лежать."""
    files = [_as_file(f"{slaves.HOME}/.claude/.credentials.json", credentials())]
    what = ["креды claude.ai"]
    blob, note = llm_keys_blob()
    if blob:
        files.append(_as_file(slaves.LLM_KEYS_FILE, blob))
        what.append("ключи LLM (" + ", ".join(
            l.split("=")[0] for l in blob.splitlines()) + ")")
    return distribute(files), what, note
