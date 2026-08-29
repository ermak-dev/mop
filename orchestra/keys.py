"""Раздача файлов на узлы пула: креды claude.ai и ключи LLM-провайдеров.

Узлы с живым слейвом обычно забиты памятью под завязку и sysbatch туда не
сядет (DimensionExhausted: memory) — там файл пишется через exec в
существующую аллокацию. Sysbatch достаётся только пустым узлам.
"""
import base64
import json
import os
import time

from . import nomad, slaves

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
    _push_via_slaves(script, results)
    _push_via_sysbatch(script, sorted(nodes - set(results)), results)
    for node in nodes:
        results.setdefault(node, "НЕ ДОСТАЛСЯ — ни слейва, ни места под sysbatch")
    return results


def _push_via_slaves(script, results):
    """Узлы с живым слейвом — через exec в его аллокацию."""
    try:
        jobs = slaves.jobs()
    except Exception:
        return
    for j in jobs:
        a = nomad.latest_alloc(j["ID"])
        if not a or a["ClientStatus"] != "running" or a["NodeName"] in results:
            continue
        try:
            out, code = nomad.sh(a, script)
            results[a["NodeName"]] = (
                "OK" if code == 0 else f"FAILED: {(out.strip() or f'exit {code}')[:80]}")
        except Exception as e:
            results[a["NodeName"]] = f"FAILED: {str(e)[:80]}"


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


NOMAD_TOKEN_FILE = f"{slaves.HOME}/.config/nomad/bootstrap.json"


def push_nomad_token():
    """Токен Nomad на узлы пула.

    Решение оператора 2026-08-29: узлы имеют право управлять друг другом, то
    есть на них едет тот же management-токен, что у управляющей машины. Цена
    решения названа прямо: слейв, дотянувшееся до этого файла, может всё,
    включая снос чужих джобов. Без токена узловой orchestra-mcp видит только
    свой хост -- ни соседний узел, ни инбокс мастера ему недоступны."""
    src = os.path.expanduser("~/.config/nomad/bootstrap.json")
    with open(src, "rb") as f:
        raw = f.read()
    json.loads(raw)
    return distribute([_as_file(NOMAD_TOKEN_FILE, raw)])


def push_login():
    """Раздать креды claude.ai и ключи LLM. -> (результаты, что везли, замечание)"""
    files = [_as_file(f"{slaves.HOME}/.claude/.credentials.json", credentials())]
    what = ["креды claude.ai"]
    try:
        with open(os.path.expanduser("~/.config/nomad/bootstrap.json"), "rb") as f:
            files.append(_as_file(NOMAD_TOKEN_FILE, f.read()))
        what.append("токен Nomad")
    except OSError:
        pass
    blob, note = llm_keys_blob()
    if blob:
        files.append(_as_file(slaves.LLM_KEYS_FILE, blob))
        what.append("ключи LLM (" + ", ".join(
            l.split("=")[0] for l in blob.splitlines()) + ")")
    return distribute(files), what, note
