"""Общее у команд мастер-шелла (#258): профиль LLM и его ключ, ключи на
узлы, workspace папета. Здесь, а не в lib: это клиентская сторона, а lib
импортирует каждый командлет на каждой машине, и ключи с профилями ей
тянуть за собой незачем."""
import json
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
    directory = creds.server_dir(host)
    record = creds.client(directory)
    if record is None:
        raise RuntimeError(f"no joined proxy configuration for {host}: run mop login --server {host}")
    env = dict(llm.env())
    env["ANTHROPIC_BASE_URL"] = record["proxy_url"]
    env[llm.AUTH_VAR] = record["proxy_key"]
    # Шина и Python уже доверяют закреплённому сертификату (#412), но
    # Claude -- процесс Node.js со своим хранилищем доверия. Явный выбор
    # пользователя оставляем; без файла обычные публичные CA работают сами.
    cert = os.path.join(directory, creds.CERT_FILE)
    if record["proxy_url"].startswith("https://") and os.path.isfile(cert) \
            and not os.environ.get("NODE_EXTRA_CA_CERTS"):
        env["NODE_EXTRA_CA_CERTS"] = cert
    # Черновики отзыва и /feedback (#419) гасятся в обоих режимах запуска
    # claude: переменная -- команде, настройка -- инструменту SendFeedback
    # (значения и SemVer-контракт -- Claude Code 2.1.287: feedbackDrafts
    # notify|quiet|off, «off» выключает инструмент и очередь целиком).
    # Зонтик CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC не используем: он
    # резал бы и обновления плагинов, Remote Control -- тоже мимо.
    env["DISABLE_FEEDBACK_COMMAND"] = "1"
    return env


# Настройка одного сеанса (#419): черновики отзыва отключены, файлы настроек
# не трогаются. Идёт парой к session_env -- то, что гасится командой, а то,
# что настройкой, расходиться по местам не должно.
FEEDBACK_SETTINGS = ('--settings', '{"feedbackDrafts":"off"}')

# Ключ, который mop вписывает в пользовательские настройки (#419): пустой
# никогда -- «везде» означает и явное отсутствие ключа у пользователя.
FEEDBACK_KEY = "feedbackDrafts"
FEEDBACK_VALUE = "off"


def feedback_settings(passthru, read=None):
    """Итоговые настройки отзыва для запуска claude -> (значение, переписанный
    пасстру). Чистая функция (#419): read -- как прочитать файл-путь (для
    проверки подменяется), по умолчанию диск.

    Последнее пользовательское --settings в зоне флагов (до первого `--`;
    форма и через пробел, и через «=») несёт в себе и его настройки, и наш
    feedbackDrafts=off: claude повторных --settings не мержит (побеждает
    последний целиком), и вторым флагом его настройки терялись бы. Файл
    читается и возвращается значением -- commandlet отдаст его claude как
    memfd-путь (без диска и без argv: настройки бывают не для чужих глаз,
    а argv виден всей машине). Ключ ставится всегда -- «везде»; остальные
    ключи пользователя сохраняются.

    Файл недоступен или не-JSON/не-объект -- ValueError: fail closed,
    commandlet откажет явно, а не молча включит черновики. Ошибка значения
    не несёт. Своего --settings нет -- ('{"feedbackDrafts":"off"}', пасстру
    как был). За `--` флагов для claude нет -- та часть не рассматривается
    и не трогается."""
    read = read or _read_file
    dash = passthru.index("--") if "--" in passthru else len(passthru)
    flags, tail = list(passthru[:dash]), passthru[dash:]
    last = None
    for i, arg in enumerate(flags):
        value = None
        if arg == "--settings" and i + 1 < len(flags):
            value = flags[i + 1]
        elif arg.startswith("--settings="):
            value = arg[len("--settings="):]
        if value:
            last = (i, value)
    if last is None:
        return FEEDBACK_SETTINGS[1], passthru
    i, value = last
    if value.startswith("{"):
        text = value
    else:
        try:
            text = read(value)
        except OSError as e:
            raise ValueError(f"--settings file cannot be read ({e.strerror}): "
                             f"mop cannot add {FEEDBACK_KEY} to it") from None
    try:
        parsed = json.loads(text)
    except ValueError:
        raise ValueError("--settings is not valid JSON: mop cannot add "
                         f"{FEEDBACK_KEY} to it") from None
    if not isinstance(parsed, dict):
        raise ValueError("--settings is not a JSON object: mop cannot add "
                         f"{FEEDBACK_KEY} to it")
    parsed[FEEDBACK_KEY] = FEEDBACK_VALUE
    # Одна пара на месте прежней: файловый путь и inline-объект едут одним
    # значением дальше (commandlet решает, файл это или строка).
    if flags[i] == "--settings":
        del flags[i:i + 2]
    else:
        del flags[i]
    return json.dumps(parsed, ensure_ascii=False), flags + tail


def _read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def feedback_fd(value):
    """Значение настроек -> (memfd-путь, fd) для exec (#419). Настройки
    катаются файловым дескриптором: без диска, без argv-содержимого, fd
    наследуется в execvpe (pass_fds тут нет -- exec в своём процессе,
    дескриптор помечен наследуемым)."""
    fd = os.memfd_create("mop-claude-settings", flags=0)
    os.write(fd, value.encode())
    os.lseek(fd, 0, 0)
    os.set_inheritable(fd, True)
    return f"/proc/self/fd/{fd}", fd
