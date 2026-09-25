"""Раздача файлов на узлы пула: креды claude.ai и ключи LLM-провайдеров.

Путь один — глагол `write` агенту узла, всем узлам разом: агент есть на
каждом узле независимо от того, живёт там папет или нет, пишет файл на узел
и в каждое живое тело. Запасного пути через sysbatch-джоб Nomad больше нет
(#135): ему нужен токен, которого вне сервера нет, в тела pve-узла он не
доставал, а довод «раздача не смеет зависеть от шины — она везёт креды самой
шины» устарел: креды шины узлу кладёт `mop server deploy`. Молчащий агент — отказ с
причиной; лечится он юнитом на узле, а не раздачей в обход.
"""
import base64
import json
import os
import time

from ..common import bus, config, fsutil, llm, puppets

# Логин claude.ai управляющей машины — то, что раздаётся на узлы.
CREDENTIALS = os.path.expanduser("~/.claude/.credentials.json")


def results_from(nodes, answers):
    """Ответы агентов -> {узел: "OK" | "FAILED: …" | "NOT REACHED: …"}.
    Чистая функция (#135): молчание агента называется молчанием, без
    отсылки к токену Nomad и контроллеру."""
    out = {}
    for node in nodes:
        got = bus.verdict(answers.get(node))
        if got is None:
            out[node] = "OK"
        elif got[0] == bus.UNREACHED:
            out[node] = f"NOT REACHED: {got[1] or 'no answer'}"
        else:
            out[node] = f"FAILED: {got[1][:120]}"
    return out


# Запись в тела pve-узла идёт секундами на тело (#136, #137): таймаут --
# с запасом, иначе живой агент читался бы молчащим.
WRITE_TIMEOUT = 60


def distribute(files):
    """Разложить [(путь, b64)] по всем ready-узлам пула -> {узел: результат}.

    Всем узлам разом -- глаголом `write` их агентам. Агент пишет только в
    свой белый список путей; попытка привезти что-то ещё вернётся отказом,
    а не тихо запишется."""
    # Состав пула -- через шину, как и всё остальное (#81).
    nodes = sorted(puppets.ready_nodes())
    if not nodes:
        raise RuntimeError("no ready nodes in the pool")
    try:
        answers = bus.request_many("write", nodes, timeout=WRITE_TIMEOUT,
                                   files=[list(f) for f in files])
    except bus.BusError as e:
        answers = {n: e for n in nodes}
    return results_from(nodes, answers)


def llm_keys_blob():
    """Что везти на узлы в secrets.env: только названное явно — ключи, которые
    просит хоть один LLM-профиль.

    Фильтр здесь не гигиена, а условие, на котором источником может быть общий
    .env проекта: там же лежат креды GitLab, и на узлах пула им делать нечего.
    Едет ровно перечисленное.
    -> (содержимое|None, замечание|None)"""
    wanted = {p["key"] for p in llm.profiles().values() if p.get("key")}
    if not wanted:
        return None, None
    if not os.path.exists(puppets.LOCAL_KEYS_FILE):
        return None, f"no {puppets.LOCAL_KEYS_FILE} — profiles needing a key won't start"
    # Тот же разбор, что у настроек: это и есть .env, ключи в нём — строки
    # того же примитивного формата.
    found = {k: v for k, v in config.read_env(puppets.LOCAL_KEYS_FILE).items()
             if k in wanted and v}
    missing = sorted(wanted - set(found))
    note = f"{puppets.LOCAL_KEYS_FILE} is missing: {', '.join(missing)}" if missing else None
    if not found:
        return None, note
    return fsutil.write_kv(found), note


def _as_file(path, text_or_bytes):
    raw = text_or_bytes.encode() if isinstance(text_or_bytes, str) else text_or_bytes
    return (path, base64.b64encode(raw).decode())


def push_llm_keys(profile):
    """Ключ профиля обязан лежать на узле раньше папета: без него врапер
    валится, а Nomad уводит папет в restart-backoff. Узел заранее неизвестен
    (место выбирает планировщик), поэтому раздаём на весь пул.
    -> {узел: результат} либо None, если профилю ключ не нужен.

    Чужому имени — тихий None, а не отказ: валидация имени — дело вызывающих
    (parse_llm, job_spec), а отказ здесь читался бы как «ключа нет» и уводил
    бы разбор не туда."""
    key = (llm.get(profile) or {}).get("key")
    if not key:
        return None
    blob, note = llm_keys_blob()
    if not blob or key not in blob:
        raise RuntimeError(f"profile {profile}: {note}")
    return distribute([_as_file(puppets.SECRETS_FILE, blob)])


def credentials():
    """Локальные креды claude.ai, годные к раздаче."""
    try:
        with open(CREDENTIALS, "rb") as f:
            raw = f.read()
        json.loads(raw)
    except FileNotFoundError:
        raise RuntimeError(f"no {CREDENTIALS} — log in to claude on this machine first")
    except ValueError:
        raise RuntimeError(f"{CREDENTIALS}: not valid JSON, nothing to distribute")
    return raw


def credentials_fresh():
    """Годятся ли локальные креды: валидный JSON, expiresAt в будущем."""
    try:
        with open(CREDENTIALS) as f:
            c = json.load(f)
        return ((c.get("claudeAiOauth") or {}).get("expiresAt") or 0) / 1000 > time.time()
    except Exception:
        return False


def push_login():
    """Раздать креды claude.ai и ключи LLM. -> (результаты, что везли, замечание)

    Токен Nomad отсюда убран, и это не забывчивость. Его возили на узлы, чтобы
    узловой mop mcp дотягивался до соседей и до инбокса мастера, — и цена была
    названа прямо: папет, добравшийся до файла, мог снести чужие джобы. Шина
    даёт ту же связь правами по субъектам, поэтому полномочия узлам больше не
    нужны. Старую копию файла с узлов сносит плейбук nats: перестать раздавать
    значит оставить лежать."""
    files = [_as_file(f"{puppets.HOME}/.claude/.credentials.json", credentials())]
    what = ["claude.ai credentials"]
    blob, note = llm_keys_blob()
    if blob:
        files.append(_as_file(puppets.SECRETS_FILE, blob))
        what.append("node secrets (" + ", ".join(
            l.split("=")[0] for l in blob.splitlines()) + ")")
    return distribute(files), what, note
