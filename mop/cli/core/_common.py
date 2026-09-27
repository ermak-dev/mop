"""Общее у команд мастер-шелла (#258): профиль LLM и его ключ, ключи на
узлы, workspace папета. Здесь, а не в lib: это клиентская сторона, а lib
импортирует каждый командлет на каждой машине, и ключи с профилями ей
тянуть за собой незачем."""
import os

from mop.cli import lib
from mop.common import config, llm, manifest, paths, puppets
from mop.client import keys


def workspace_text(origin):
    """workspace папета (#133): .mop/bootstrap.yaml рабочей копии проекта,
    если команда идёт из неё, иначе из origin (ветка по умолчанию). Нет
    файла -- пустой текст: сервер снимает копию папета, и удаление доходит.

    -> (текст, происхождение) (#334): {source: "working copy" | "origin",
    commit, dirty -- правлен ли сам файл, tasks -- сколько задач (None --
    не разобрался, отказ скажет сервер), present -- есть ли файл}. Едет
    полем bootstrap_sent рядом с workspace и печатается sent_line."""
    if lib.cwd_origin() == origin:
        top = lib.git("rev-parse", "--show-toplevel")
        try:
            with open(os.path.join(top, manifest.BOOTSTRAP_FILE)) as f:
                text = f.read()
        except FileNotFoundError:
            text = ""
        # Правка -- только самого файла: чужие изменения рабочей копии на
        # то, что уехало, не влияют.
        dirty = bool(lib.git("status", "--porcelain", "--", manifest.BOOTSTRAP_FILE))
        return text, _provenance(text, "working copy", lib.git("rev-parse", "HEAD"), dirty)
    got = manifest.fetch(origin)
    text = got["bootstrap_text"] or ""
    return text, _provenance(text, "origin", got.get("commit"), False)


def _provenance(text, source, commit, dirty):
    present = bool(text.strip())
    tasks = 0
    if present:
        try:
            tasks = len(manifest.play(text)[1])
        except ValueError:
            tasks = None
    return {"source": source, "commit": commit, "dirty": dirty, "tasks": tasks,
            "present": present}


def sent_line(prov):
    """Происхождение workspace -> строка «что уехало» (#334). Чистая функция."""
    if not prov.get("present"):
        return f"bootstrap not sent: no {manifest.BOOTSTRAP_FILE}, the server copy is removed"
    where = "the working copy" if prov.get("source") == "working copy" else "origin"
    line = f"bootstrap sent: {manifest.BOOTSTRAP_FILE} from {where}"
    if prov.get("commit"):
        line += f" at {prov['commit'][:12]}"
    if prov.get("dirty"):
        line += " (+ uncommitted)"
    tasks = prov.get("tasks")
    if tasks is not None:
        line += f", {tasks} task" + ("" if tasks == 1 else "s")
    return line
def parse_value(args, flag):
    """Выкусить `flag VALUE` (или `flag=VALUE`) откуда угодно в аргументах
    -> (значение | None, остальные). Тот же разбор, что у --llm (#284)."""
    value, rest, it = None, [], iter(args)
    for a in it:
        if a == flag:
            value = next(it, "")
            if value.startswith("-"):
                rest.append(value)
                value = ""
        elif a.startswith(flag + "="):
            value = a.split("=", 1)[1]
        else:
            rest.append(a)
    return (value or None), rest


def parse_llm(args):
    """Выкусить --llm PROFILE (или --llm=PROFILE) откуда угодно в аргументах.
    -> (профиль | None, остальные аргументы)."""
    profile, rest, it = None, [], iter(args)
    for a in it:
        if a == "--llm":
            profile = next(it, "")
            # Следом флаг, а не имя: `--llm --fresh` съедал бы соседний флаг
            # как профиль и отказывал про профиль «--fresh».
            if profile.startswith("-"):
                rest.append(profile)
                profile = ""
        elif a.startswith("--llm="):
            profile = a.split("=", 1)[1]
        else:
            rest.append(a)
    if profile == "":
        # Забытое значение -- ошибка использования, а не «нет профиля ''»
        # (#164). RuntimeError: диспетчер делает из него одну строку в
        # stderr, как из любого отказа (#146).
        raise RuntimeError(f"--llm needs a profile name; available: "
                           f"{', '.join(llm.profiles())}")
    if profile is not None:
        llm.require(profile)
    return profile, rest
def session_env(profile):
    """Окружение сессии claude на профиле из mop/common/llm/: статическая часть
    профиля плюс ключ. -> (профиль, {переменные}).

    Источник ключа — местный .env, а не узловой secrets.env: на управляющей
    машине узел ничего не выдавал. Отказ, а не тишина: сессия без ключа
    отбивает каждый ход 401-й, а читается живой. Так поднимаются и мастер,
    и `mop code`."""
    prof = llm.require(profile)
    env = dict(prof["env"])
    if prof["key"]:
        key = config.get(prof["key"])
        if not key:
            lib.usage(f"profile {profile}: no {prof['key']} in "
                  f"{puppets.LOCAL_KEYS_FILE} — add it and retry")
        env[prof["auth_var"]] = key
    return prof, env
def push_llm_keys(llm):
    """Ключи профиля на узлы. При успехе молчит (#124); не дошедшие --
    ошибкой, с узлами."""
    results = keys.push_llm_keys(llm)
    if results is None:
        return
    bad = [f"{n}: {r}" for n, r in sorted(results.items()) if r != "OK"]
    if bad:
        lib.fail(f"{paths.NODE_SECRETS} did not reach every node: " + "; ".join(bad))
