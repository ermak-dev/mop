"""Общее у команд мастер-шелла (#258): профиль LLM и его ключ, ключи на
узлы, workspace папета. Здесь, а не в lib: это клиентская сторона, а lib
импортирует каждый командлет на каждой машине, и ключи с профилями ей
тянуть за собой незачем."""
import os
import time

from mop.cli import lib
from mop.common import bus, busnames, config, creds, llm, manifest, puppets


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
# Сколько ждать итога bootstrap'а (#334): прогон на сервере плюс подъём
# тела до вызова -- тот же запас, что у `mop add` на подъём.
BOOTSTRAP_WAIT = busnames.BOOTSTRAP_TIMEOUT + 120
NOT_REPLACED = ("bootstrap not played: the spec is unchanged, "
                "Nomad kept the running allocation")


def outcome_line(record, waited):
    """Итог прогона -> строка либо None (#334). Чистая функция. Прогона не
    было (нечего играть) -- ничего сверх; итога нет -- так и сказать."""
    if record is None:
        return f"bootstrap result not seen in {waited}s — mop list"
    if not record.get("played"):
        return None
    if record.get("ok"):
        return f"bootstrap ok in {round(record.get('seconds') or 0)}s"
    if record.get("task"):
        line = f"bootstrap failed at task «{record['task']}»"
        return line + (f": {record['message']}" if record.get("message") else "")
    line = "bootstrap failed" + (f" (rc {record['rc']})" if record.get("rc") is not None else "")
    return line + (f": {record['last']}" if record.get("last") else "")


def wait_bootstrap(name, marker, p, timeout, sleep=time.sleep, clock=time.time):
    """Итог прогона с меткой своей регистрации -> запись либо None по
    таймауту. Итог с другой меткой -- прежний прогон, а не наш: ждём дальше."""
    p.step(f"{name}: bootstrap playing")
    deadline = clock() + timeout
    while True:
        got = (bus.call_cluster("bootstrap_result", name=name) or {}).get("result")
        if got and got.get("sent") == marker:
            return got
        if clock() >= deadline:
            return None
        sleep(2)


def report_bootstrap(name, got, p):
    """Ответ регистрации -> строка итога (#334) и код выхода. Без метки
    (старый сервер, клиент без происхождения) -- тишина, как было.

    Провал прогона -- провал команды: с этим bootstrap'ом папет работать не
    будет, так же `mop add` падает, когда папет не встаёт (stderr, 1).
    Итог не увиден -- не известный провал, а неизвестность: stdout, 0."""
    marker = got.get("bootstrap_marker")
    if not marker:
        return 0
    if got.get("replaced") is False:
        print(NOT_REPLACED, flush=True)
        return 0
    record = wait_bootstrap(name, marker, p, BOOTSTRAP_WAIT)
    p.clear()
    line = outcome_line(record, BOOTSTRAP_WAIT)
    if record and record.get("played") and not record.get("ok"):
        lib.fail(line)
        return 1
    if line:
        print(line, flush=True)
    return 0


def session_env():
    """Окружение claude: модельная карта общая, URL и ключ -- joined-сервера."""
    host = config.get("MOP_SERVER_LAN")
    record = creds.client(creds.server_dir(host))
    if record is None:
        raise RuntimeError(f"no joined proxy configuration for {host}: run mop join --server {host}")
    env = dict(llm.env())
    env["ANTHROPIC_BASE_URL"] = record["proxy_url"]
    env[llm.AUTH_VAR] = record["proxy_key"]
    return env
