"""mop driver run <name>: raise the body and run the puppet inside it

What the job spec calls (the outer wrapper). Runs on the node that owns the
puppet: ensure the body, bootstrap the sandbox through the server,
push the inner wrapper in and hold its session until it ends.
"""
import asyncio
import base64
import binascii
import json
import os
import shlex
import signal
import subprocess
import sys
import time

from mop.cli import lib
from mop.common import bus, busnames, config
from mop.common import soul
from mop.common.domain import Body, JobMeta
from mop import driver

# Куда внешний врапер кладёт внутренний внутри тела. В $HOME, а не в /tmp:
# /tmp в теле бывает общим или вычищаемым, а этот файл обязан прожить ровно
# столько, сколько живёт папет.
INNER = ".cache/mop-wrapper.sh"

# Проба ssh перед врапером (#73): около минуты на то, чтобы сеть свежего тела
# догнала, дольше -- уже не гонка, а поломка, и отказ должен быть громким.
SSH_PROBE_TRIES = 12
SSH_PROBE_PAUSE = 5
SSH_PROBE_TIMEOUT = 20

# Что внешний врапер переливает внутрь тела. Список закрыт: открытый означал бы
# дыру, через которую в тело уехало бы окружение узла целиком — вместе с тем,
# чего папету видеть не положено.
#
# Закрыт он самой спекой (#155): job_spec кладёт в PU_CARRY ключи своего же
# окружения. Раньше список здесь перепечатывался руками и отставал от спеки —
# новая переменная доезжала до задачи и молча не доезжала до тела. Импортировать
# спеку отсюда нельзя: mop.spec тянет python-nomad, а на узле его нет.
#
# LEGACY_CARRY — список до #155, для спек, зарегистрированных раньше: их
# папет обязан подняться после раскатки и до перерегистрации.
LEGACY_CARRY = ("PU_NAME", "PU_ORIGIN", "PU_PROJECT", "PU_SEED", "PU_CONTINUE",
                "PU_LLM", "PU_LLM_ENV", "PU_LLM_KEY_VAR", "PU_LLM_AUTH_VAR",
                "HOME", "PATH")


def carry(env):
    """Ключи окружения задачи, которые едут в тело: список самой спеки."""
    keys = env.get("PU_CARRY") or ""
    return keys.split(",") if keys else list(LEGACY_CARRY)


def _inner_script():
    """Внутренний врапер с прелюдией из окружения задачи.

    Прелюдия обязательна: переменные спеки живут в процессе задачи на узле, а
    врапер исполняется в теле — без переливки он не узнал бы ни имени папета,
    ни origin, ни имени ключа LLM-профиля."""
    blob = os.environ.get("PU_WRAPPER") or ""
    if not blob:
        sys.exit("no PU_WRAPPER in the task environment — this job spec predates "
                 "the wrapper split; re-register the puppet: mop recycle <name>")
    return _prelude(os.environ) + base64.b64decode(blob).decode()


def _prelude(environ):
    """Экспорт окружения задачи для скрипта в теле: переменные спеки живут
    в процессе задачи на узле, а скрипт исполняется в теле."""
    return "".join(f"export {k}={shlex.quote(environ.get(k, ''))}\n"
                   for k in carry(environ))


def clone_script(environ):
    """Стадия клона (#247): что узел исполняет в теле ДО bootstrap. Чистая
    функция. Прелюдия из окружения задачи, отказ на первой ошибке, охрана
    пустого PU_CLONE (rm -rf в сниппете над пустой строкой снёс бы тело),
    затем driver.CLONE_SH -- тот же текст, что стоит во врапере спеки."""
    head = (_prelude(environ) + "set -e\n"
            ': "${PU_CLONE:?no PU_CLONE in the task environment}"\n'
            'd="$PU_CLONE"\n')
    # Ветка мастера (#256) -- из меты джоба: Nomad отдаёт Meta задаче как
    # NOMAD_META_<ключ>. Checkout только у свежего клона (не было .git или
    # origin сменился и сниппет снесёт каталог): существующий клон не
    # переключается, там может быть работа. Нет такой ветки в origin --
    # завести локально: мастер создаст её на origin первым landing.
    #
    # Дом клона (#272) -- на каждом старте, не только у свежего клона:
    # существующий клон узнаёт его тоже. Ветка мастера, а без неё ветка по
    # умолчанию из origin/HEAD; не вычислить -- записи нет, и дом тогда
    # решает агент по той же ветке по умолчанию. Живёт здесь, а не в
    # driver.CLONE_SH: тот едет во врапере, и правка его -- перерегистрация
    # всего пула.
    branch = (environ.get("NOMAD_META_branch") or "").strip()
    if not branch:
        return (head + driver.CLONE_SH
                + 'home=$(git -C "$d" rev-parse --abbrev-ref origin/HEAD 2>/dev/null || true)\n'
                'if [ -n "$home" ]; then git -C "$d" config mop.home "${home#origin/}"\n'
                'else git -C "$d" config --unset mop.home || true; fi\n')
    return (head + f"export PU_BRANCH={shlex.quote(branch)}\n"
            'fresh=1\n'
            'if [ -d "$d/.git" ] && [ "$(git -C "$d" remote get-url origin)" = "$PU_ORIGIN" ]; then fresh=0; fi\n'
            + driver.CLONE_SH +
            'if [ "$fresh" = 1 ]; then\n'
            '    git -C "$d" checkout -q "$PU_BRANCH" 2>/dev/null || git -C "$d" checkout -q -b "$PU_BRANCH"\n'
            'fi\n'
            'git -C "$d" config mop.home "$PU_BRANCH"\n')


# ─── узел ────────────────────────────────────────────────────────────────
class BootstrapFailed(RuntimeError):
    """Прогон bootstrap'а упал (#333). Текст -- прежний: код выхода ansible
    и хвост; поверх -- упавшая задача и её сообщение, если сервер их назвал
    (сервер до #333 их не шлёт)."""

    def __init__(self, reply):
        self.task, self.message = reply.get("task"), reply.get("message")
        # Сервер сдался (#345): столько провалов того же workspace подряд.
        self.gave_up, self.failures = bool(reply.get("gave_up")), reply.get("failures")
        self.tail = reply.get("tail", "")
        super().__init__(f"bootstrap failed (ansible exit {reply.get('rc')}):\n{self.tail}")


def refusal(name, error):
    """Строка отказа врапера на stderr. Чистая. Задача названа --
    «failed at task «…»: сообщение», затем хвост, как всегда; её читает
    state.failure_reason для FAILED в `mop list`."""
    if isinstance(error, BootstrapFailed) and error.gave_up:
        # Сдался (#345): джоб сейчас остановит сервер, и строка говорит, что
        # делать -- рестарт тут не поможет.
        where = f" at task «{error.task}»" if error.task else ""
        why = f": {error.message}" if error.message else ""
        return (f"bootstrap of {name} gave up after {error.failures} attempts{where}{why}"
                f" — fix .mop/bootstrap.yaml, then mop update\n{error.tail}")
    if isinstance(error, BootstrapFailed) and error.task:
        return (f"bootstrap of {name} failed at task «{error.task}»: {error.message}\n"
                f"{error.tail}")
    return f"bootstrap of {name} failed: {error}"


def initial_identity(name, soul_inc):
    """Начальная identity хозяина в клоне -- глагол живого агента узла
    (#584/#603), не локальная запись: замок #573/#574 живёт в агенте, и
    лаунчер -- другой процесс. Канал startup -- только своему узлу
    (bus.request строит субъект сам и откажет чужому имени). Тело запроса
    несёт только цель: verb, имя и инкарнацию; хозяина агент решает сам по
    доверенному биндингу, профиль из ответа -- не разрешение. -> расписка
    {identity_ready, role, soul_inc, soul_digest} | {error}."""
    project = driver.project_of_name(name)
    node = node_of_credentials()
    return bus.request(node, "initial_identity", timeout=busnames.BOOTSTRAP_TIMEOUT,
                       channel=busnames.STARTUP_CHANNEL, project=project,
                       name=name, soul_inc=soul_inc)


def role_bootstrap_ask(project, name, address):
    """Спрос bootstrap ролевого джоба: квалифицированный server.rpc узла
    (#603/#581), не анонимный (его зовут и люди проекта, а личный кред
    принципала выдаёт только хозяину узла). -> ответ сервера."""
    return bus.ask_server("bootstrap", timeout=busnames.BOOTSTRAP_TIMEOUT,
                          project=project, node_auth=True,
                          name=name, address=address)


def use_node_credentials():
    """Креды узла -- креды этого процесса (#603): config() без аргумента
    читает bus.FILE, а node_name() (ask_server node_auth, канал startup)
    зовёт именно его. До правки #603 (node_name не принимает файл) процесс
    лаунчера сам называет свой файл -- это его единственные креды, не
    подмена: файл настоящий, читает его сам bus."""
    bus.FILE = bus.NODE_FILE
    bus.connect(bus.NODE_FILE)


def node_of_credentials():
    """Имя узла из его собственного кред-файла (NODE_FILE), не из
    bus.node_name(): тот читает дефолтный config(), а процесс задачи
    подключается узловым файлом (#603, дефект у pu8). -> имя | None."""
    try:
        user = bus.config(bus.NODE_FILE).get("user") or ""
    except Exception:
        return None
    if user.startswith(busnames.NODE_PREFIX):
        return user[len(busnames.NODE_PREFIX):]
    return None


def bootstrap_sandbox(d, name, project, node_qualified=False):
    """Bootstrap песочницы папета с этого узла: впустить сервер, позвать,
    дождаться, выпустить. -> ответ сервера; отказ — RuntimeError/BusError.

    node_qualified -- спросить именованным каналом узла (ролевой джоб,
    #603): личный кред принципала выдаёт только хозяину узла, анонимный
    server.rpc зовут и люди проекта. Легаси -- прежний анонимный вопрос.

    Зовётся из `mop driver run` до внутреннего врапера. Дверь закрывается в
    любом исходе: ключ сервера в теле живёт ровно столько, сколько идёт
    bootstrap. Где сервер найдёт тело и надо ли его впускать, решает драйвер
    (#151): у host тело — сам узел, и дорога туда есть всегда."""
    address = d.address(name)

    # Стоп джоба посреди разговора (#345: сервер сдался и просит стоп) --
    # SIGTERM. По умолчанию он убил бы процесс мимо finally, и ключ сервера
    # остался бы в теле; исключение же закрывает дверь, как любой исход.
    def stopped(_sig, _frm):
        raise SystemExit(f"{name}: stopped during bootstrap")
    before = signal.signal(signal.SIGTERM, stopped)
    try:
        r = asyncio.run(d.admit(name, True))
        if r.get("error"):
            raise RuntimeError(f"cannot let the server into the body: {r['error']}")
        bus.connect(bus.NODE_FILE)
        if node_qualified:
            use_node_credentials()
            out = role_bootstrap_ask(project, name, address)
        else:
            out = bus.ask_server("bootstrap", timeout=busnames.BOOTSTRAP_TIMEOUT,
                                 project=project, name=name, address=address)
    finally:
        # Закрыть -- и после неудачного впуска: стоп мог прийти посреди него.
        asyncio.run(d.admit(name, False))
        signal.signal(signal.SIGTERM, before)
    if out.get("error"):
        raise RuntimeError(out["error"])
    if not out.get("ok"):
        raise BootstrapFailed(out)
    return out


def ensure_params(name, environ):
    """Что ensure узнаёт о папете из окружения задачи. Чистая функция.

    mem -- потолок памяти из спеки (PU_MEM_MB, #197); спека до #197 его не
    несёт, и тогда память тела не трогается."""
    params = {"project": driver.project_of_name(name)}
    if environ.get("PU_MEM_MB"):
        params["mem"] = environ["PU_MEM_MB"]
    return params


# ─── роль (#590) ─────────────────────────────────────────────────────────
# Раскладка в каталоге инкарнации (driver.soul_dir): бандл -- подкаталогом
# ровно из soul.yaml и промпта, как его читает soul.load; рядом кред
# принципала и метка готовности. Имена -- константы узла и имя промпта из
# проверенного бандла, не поля ответа: тело запроса путей не выбирает.
# Метка -- последней и отдельной записью: обёртка (#583) стартует только
# над ней.
BUS_FILE, READY = "bus.json", "ready.json"


def expected_role(environ):
    """Тройка роли из Meta джоба -> JobMeta | None (легаси-джоб). Чистая.

    Nomad отдаёт Meta задаче как NOMAD_META_<ключ>, а Meta пишет сервер при
    регистрации (#582) -- это доверенное ожидание, с которым сверяется
    ответ. Половина тройки или битое значение -- ValueError тем же
    договором JobMeta: битый джоб не поднимается ни ролью, ни легаси."""
    triple = {k: environ.get(f"NOMAD_META_{k}") or None
              for k in ("soul", "soul_digest", "soul_inc")}
    if not any(triple.values()):
        return None
    return JobMeta(environ.get("NOMAD_META_origin") or None, **triple)


def _short(value):
    """Значение ответа в тексте отказа: repr, обрезанный -- ответ сервера
    не печатается целиком."""
    r = repr(value)
    return r if len(r) <= 80 else r[:77] + "..."


def role_files(name, expected, reply):
    """Ролевой ответ bootstrap'а -> ([(путь, bytes)] файлов роли, (путь,
    bytes) метки готовности). Чистая; отказ -- ValueError с причиной, без
    пароля в тексте.

    Авторитет -- Meta джоба (expected) и неизменяемый бандл: роль,
    инкарнация и digest ответа обязаны совпасть с Meta, бандл собирается
    soul.from_manifest и обязан хешироваться в digest Meta. Кред -- прежнее
    поле b['bus'], но индивидуальный: его user -- принципал роли.
    Проектный кред на его месте -- отказ: легаси-кред в ролевое тело не
    едет никак, и роль без своего принципала не стартует на чужом. Второй
    кред внутри роли -- дубль, тоже отказ."""
    s = reply.get("soul")
    if not isinstance(s, dict):
        raise ValueError(f"bootstrap brought no role payload for {expected.soul} "
                         f"-- the server predates role delivery or the binding is gone; "
                         f"a role never starts on project credentials")
    if "bus" in s:
        raise ValueError("the role payload carries a second credential; "
                         "the role's credential travels only as the reply's bus")
    for key, want in (("role", expected.soul), ("soul_inc", expected.soul_inc),
                      ("soul_digest", expected.soul_digest)):
        if s.get(key) != want:
            raise ValueError(f"the payload's {key} {_short(s.get(key))} is not "
                             f"the job's {want!r}")
    if not isinstance(s.get("manifest"), dict):
        raise ValueError("the payload carries no manifest mapping")
    try:
        prompt = base64.b64decode(s.get("prompt") or "", validate=True)
    except (binascii.Error, TypeError, ValueError):
        raise ValueError("the payload's prompt is not base64") from None
    try:
        bundle = soul.from_manifest(s["manifest"], prompt)
    except (ValueError, TypeError) as e:
        raise ValueError(f"the bundle does not parse: {e}") from None
    if bundle.role != expected.soul:
        raise ValueError(f"the bundle is role {bundle.role!r}, the job's is "
                         f"{expected.soul!r}")
    if bundle.digest != expected.soul_digest:
        raise ValueError(f"the bundle hashes to {bundle.digest}, the job pins "
                         f"{expected.soul_digest}")
    principal = s.get("principal")
    if not soul.is_grant(principal):
        raise ValueError(f"the principal {_short(principal)} is not grant-<id>")
    creds = reply.get("bus")
    if not isinstance(creds, dict) or creds.get("user") != principal:
        raise ValueError(f"the bus credential is not {principal}'s")
    for key in ("url", "password"):
        if not (isinstance(creds.get(key), str) and creds[key]):
            raise ValueError(f"the credential of {principal} has no {key}")
    d = driver.soul_dir(name, expected.soul_inc)
    # soul.yaml -- JSON манифеста (JSON -- это YAML): soul.load обёртки
    # прочтёт тот же словарь и пересчитает тот же digest. ensure_ascii --
    # чтобы YAML-загрузчику не досталось ни одного не-ASCII знака.
    manifest = json.dumps(s["manifest"], sort_keys=True, ensure_ascii=True).encode()
    files = [(f"{d}/{driver.SOUL_BUNDLE}/{soul.MANIFEST}", manifest),
             (f"{d}/{driver.SOUL_BUNDLE}/{bundle.manifest.prompt}", prompt),
             (f"{d}/{BUS_FILE}", json.dumps(creds).encode() + b"\n")]
    ready = json.dumps({"ready": True, "role": bundle.role,
                        "soul_inc": expected.soul_inc,
                        "soul_digest": bundle.digest},
                       sort_keys=True).encode() + b"\n"
    return files, (f"{d}/{READY}", ready)


def deliver_legacy(d, name, b):
    """Легаси-джоб (без роли в Meta): проектный кред, как до #590."""
    # Ролевой ответ легаси-джобу -- рассогласование сервера и Meta: роль
    # без своей тройки в Meta не проверить, и класть её некуда.
    if b.get("soul"):
        sys.exit(f"role delivery of {name} refused: the server sent a role "
                 f"payload for a job without a role in its Meta")
    # Кред папета приезжает тем же ответом (#83), и только им (#114): узел
    # его не хранит, файл появляется в теле на подъёме папета и для его
    # проекта. Ответ без кредов сервер отдаёт отказом, так что здесь его нет
    # -- но старый сервер ещё может так ответить, и тогда падаем громко:
    # папет без шины мастеру читается живым, но молчащим.
    if not b.get("bus"):
        sys.exit(f"bootstrap of {name} brought no bus credentials -- "
                 f"is the project registered? mop project add <origin>")
    project = driver.project_of_name(name)
    path = driver.project_creds(project)
    w = asyncio.run(d.push(name, path, json.dumps(b["bus"]).encode() + b"\n"))
    if w.get("error"):
        sys.exit(f"bus credentials did not reach the body of {name}: {w['error']}")
    print(f"{name}: bus credentials in {path}", flush=True)


def deliver_role(d, name, role, b, inside):
    """Ролевой джоб: бандл и кред принципала в каталог инкарнации, затем
    identity хозяина глаголем агента, затем метка -- всё до модели (#590).
    Любой отказ -- выход: обёртка не стартует.

    Метка готовности прошлого старта той же инкарнации снимается ДО записи:
    оборванная запись не оставит «готово» над половиной файлов, и повтор
    после обрыва дописывает всё заново."""
    try:
        files, (ready, mark) = role_files(name, role, b)
    except ValueError as e:
        sys.exit(f"role delivery of {name} refused: {e}")
    r = subprocess.run(inside(f"rm -f {shlex.quote(ready)}"),
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"role delivery of {name} refused: the previous ready mark "
                 f"did not go: {driver.why(r.stderr or r.stdout, r.returncode)}")
    w = asyncio.run(d.push_many(name, files))
    if w.get("error"):
        sys.exit(f"role delivery of {name} refused: the role files did not "
                 f"reach the body: {w['error']}")
    # Identity хозяина -- глагол живого агента (шов): чужой процесс не
    # владеет замком #573/#574, и старт роли не пишет аренду сам. Отказ
    # глагола -- отказ старта: метка готовности не ставится, обёртка не
    # стартует.
    owner = (b.get("soul") or {}).get("owner")
    if not (isinstance(owner, dict) and busnames.valid_login(owner.get("login"))
            and isinstance(owner.get("name"), str) and owner["name"]
            and isinstance(owner.get("email"), str) and owner["email"]):
        sys.exit(f"role delivery of {name} refused: the payload has no owner "
                 f"with a login, name and email")
    use_node_credentials()
    try:
        r = initial_identity(name, role.soul_inc)
    except (RuntimeError, bus.BusError, ValueError) as e:
        sys.exit(f"role delivery of {name} refused: the owner identity did "
                 f"not settle: {e}")
    # Расписка -- доказательство глагола, а не «ok»: identity_ready и вся
    # тройка роли в ней обязаны совпасть с Meta джоба (#603). Расписка без
    # тройки -- не расписка этой роли.
    why = r.get("error") if isinstance(r, dict) else _short(r)
    if why is None and isinstance(r, dict):
        why = "no identity_ready in the receipt"
        if r.get("identity_ready"):
            for key, want in (("role", role.soul), ("soul_inc", role.soul_inc),
                              ("soul_digest", role.soul_digest)):
                if r.get(key) != want:
                    why = (f"the receipt's {key} {_short(r.get(key))} is not "
                           f"the job's {want!r}")
                    break
            else:
                why = None
    if why is not None:
        sys.exit(f"role delivery of {name} refused: the owner identity did "
                 f"not settle: {why}")
    w = asyncio.run(d.push(name, ready, mark))
    if w.get("error"):
        sys.exit(f"role delivery of {name} refused: the ready mark did not "
                 f"reach the body: {w['error']}")
    print(f"{name}: role {role.soul} ({role.soul_digest[:12]}) in "
          f"{os.path.dirname(ready)}", flush=True)


def main(argv):
    """Поднять тело и отработать в нём внутренний врапер. Зовётся из спеки.


    Смерть врапера обязана убивать сессию. На «живой папет = живая tmux-сессия
    с именем джоба» стоит и ростер, и сбор сирот: умри внешний врапер молча,
    Nomad считал бы папета остановленным, а в теле остался бы живой claude, с
    которым уже никто не разговаривает. Гасим внутри тела: pid сессии живёт в
    его namespace, и снаружи такого pid либо нет, либо это чужой процесс."""
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    if not driver.valid_name(name):
        sys.exit(f"{name!r} doesn't look like a puppet name")
    # Роль джоба -- до тела (#590): битая тройка в Meta не поднимает ни
    # роль, ни легаси-папета под ней.
    try:
        role = expected_role(os.environ)
    except ValueError as e:
        sys.exit(f"role delivery of {name} refused: {e}")
    d = driver.current()

    r = asyncio.run(d.ensure(name, ensure_params(name, os.environ)))
    body = Body.from_dict(r)
    if body is None:
        sys.exit(f"no body for {name}: {r.get('error')}")
    print(f"{name}: {body.describe()}", flush=True)

    # Врапер идёт своим соединением (run_argv), а гашение сессии — обычным
    # (argv): первое живёт столько же, сколько папет, и мультиплексировать его
    # нельзя, второе коротко и платить за него рукопожатием незачем.
    hold, probe = d.run_argv(name), d.argv(name)

    def inside(prefix, script):
        return prefix + [script] if prefix else ["bash", "-c", script]

    # Тело должно пустить врапер тем же соединением, которым он пойдёт (#73):
    # сеть свежего клона догоняет не сразу, и первый ssh уходил в Connection
    # timed out при исправном теле. Промах здесь -- пауза, а не падение
    # задачи и круг рестарта Nomad. У host префикса нет, и пробовать нечего.
    # Проба стоит до клона (#247): клон идёт в тело тем же ssh.
    if hold:
        def attempt():
            try:
                r = subprocess.run(hold + ["true"], capture_output=True,
                                   text=True, timeout=SSH_PROBE_TIMEOUT)
            except subprocess.TimeoutExpired:
                return False, f"no answer in {SSH_PROBE_TIMEOUT}s"
            return r.returncode == 0, (r.stderr.strip() or f"exit {r.returncode}")
        ok, why, n = driver.until_ok(attempt, SSH_PROBE_TRIES, SSH_PROBE_PAUSE,
                                     time.sleep)
        if not ok:
            sys.exit(f"the body of {name} did not let ssh in after {n} tries: {why}")
        if n > 1:
            print(f"{name}: ssh into the body passed on try {n}", flush=True)

    # Клон -- до bootstrap (#247): сервер играет манифест проекта в
    # песочницу, где клон уже стоит, и mop_clone означает то, что написано в
    # docs/BOOTSTRAP.md. Раньше клонировал внутренний врапер после ответа
    # сервера, и на первом старте контейнерного тела задача проекта с chdir
    # на клон падала «No such file or directory». Тем же путём в тело, что и
    # гашение сессии (probe): у host это сам узел, у pve -- ssh под ключом
    # узла. Отказ -- отказ, а не полуподнятый папет.
    r = subprocess.run(inside(probe, clone_script(os.environ)),
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"the clone of {name} did not come up: "
                 f"{driver.why(r.stderr or r.stdout, r.returncode)}")
    print(f"{name}: clone in {driver.clone_dir(name)}", flush=True)

    # Bootstrap песочницы (#62): сервер играет .mop/bootstrap.yaml проекта, а
    # узел ждёт ответа до того, как откроет tmux. Отказ — это отказ: выход
    # ненулём, Nomad перезапускает, ростер показывает падение. Папет не
    # поднимается «наполовину» с окружением, которого нет.
    try:
        b = bootstrap_sandbox(d, name, driver.project_of_name(name),
                              node_qualified=role is not None)
    except (RuntimeError, bus.BusError) as e:
        sys.exit(refusal(name, e))
    print(f"{name}: bootstrap "
          + (f"played in {b.get('seconds')}s" if b.get("played")
             else "none for the project"), flush=True)

    if role is not None:
        deliver_role(d, name, role, b, lambda script: inside(probe, script))
    else:
        deliver_legacy(d, name, b)

    inner = os.path.join(config.get("MOP_HOME"), INNER)
    w = asyncio.run(d.push(name, inner, _inner_script().encode()))
    if w.get("error"):
        sys.exit(f"the wrapper did not reach the body of {name}: {w['error']}")

    state = {"child": None, "asked": False}

    def stop(_sig, _frm):
        state["asked"] = True
        subprocess.run(inside(probe, driver.Tmux(name).kill()),
                       capture_output=True)
        if state["child"]:
            state["child"].terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state["child"] = subprocess.Popen(inside(hold, f"bash {shlex.quote(inner)}"))
    code = state["child"].wait()
    # Остановка по TERM — штатное завершение: так Nomad снимает задачу, и
    # ненулевой код здесь читался бы как падение папета.
    return 0 if state["asked"] else code
