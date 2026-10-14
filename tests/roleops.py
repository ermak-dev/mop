#!/usr/bin/env python3
"""Фронтенды управления ролями #618: python3 tests/roleops.py

HYPOTHESIS (#618): командлеты add/update/recycle и их MCP-объявления ролей
не знают. MCP add принимает только origin, CLI add не принимает роль, а
грант-мастер (#582) обязан звать add с ролью и БЕЗ клиентского workspace --
сегодня ops.add всегда собирает политику рабочей копии, сервер отказывает, и
грант не может поднять ребёнка. Старому серверу такой запрос завёл бы
безролевого папета молча.
SOLUTION: тонкий клей командлетов против уже собранного сервера 582:
`mop add --role R` и MCP-аргумент role, ping-предзапрос свода повторов
(client.roles.protocol), эхо тройки (client.roles.echo), request_id на
каждое создание; грант зовёт ops.add вовсе без политики (API #617:
workspace=None -- ни одного поля на провод), update/recycle гранта -- без
чтения политики (ops.update(preserve_workspace=True), recycle
workspace_of=None, контракт уже есть). Сервер не правится: проверки гоняют
РЕАЛЬНЫЕ обработчики cluster.answer с FakeNomad и реальным реестром grants.
STATUS: FIXED — see #618
"""
import os
import sys
import uuid

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, FakeNomad, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
os.environ.setdefault("MOP_SERVER_LAN", "127.0.0.1")

from mop.cli.core import add as cli_add  # noqa: E402
from mop.cli.core import recycle as cli_recycle  # noqa: E402
from mop.cli.core import update as cli_update  # noqa: E402
from mop.client import roles  # noqa: E402
from mop.common import bus, context, ops, soul  # noqa: E402

# Среда сервера 582 без пула: общие швы soul_env/soul_parent/soul_answer
# (#617 вынес их в _lib) -- те же реальные обработчики, что проверяет #582.
from _lib import soul_answer, soul_env, soul_parent  # noqa: E402

ALPHA = "git@h:g/alpha.git"
ORIGIN = ALPHA
# Согласованная политика родителя: наследуемый снапшот (PLAY из #582).
POLICY = "- vars:\n    answer: 42\n  tasks:\n  - name: greet\n    shell: echo hi\n"
# Что отдаёт подмена workspace_text: валидный один-плей YAML, сервер его
# принимает (проверка не о тексте политики, а о том, ЕДЕТ ли она).
PLAY = "- name: greet\n  tasks: []\n"


class Wire:
    """Провод командлета -> РЕАЛЬНЫЙ cluster.answer.

    call_cluster командлета (verb, **fields) оборачивается в req с
    _caller/_subject, как их кладёт каркас сервиса по субъекту провода:
    человек -- легаси-rpc, грант -- role-канал. fields едут на сервер как
    есть: sanitize здесь не место, лишнее поле обязан увидеть сервер (и
    отказать) -- иначе проверка фронтенда проверяла бы саму себя."""

    def __init__(self, api, who, grant=False, project="alpha"):
        self.api, self.project, self.who = api, project, who
        self.subject = (f"mop.{project}.cluster.role.{who}" if grant
                        else f"mop.{project}.cluster.rpc")
        self.seen = []
        self.marker = None

    def __call__(self, verb, timeout=None, project=None, **fields):
        self.seen.append((verb, fields))
        if verb == "bootstrap_result":
            # Прогон мгновенно сыгран: проверка фронтенда и сервера, не
            # узла; метку отдаёт регистрация, провод её помнит.
            return {"ok": True, "name": fields.get("name"),
                    "result": {"sent": self.marker, "played": True, "ok": True,
                               "seconds": 0}}
        if verb == "alloc":
            # Аллокация фейкового Nomad'а: джоб зарегистрирован -- он и
            # «поднят» (проверка ждёт провода и сервера, не планировщика).
            return {"alloc": {"ID": "a-1", "JobID": fields.get("name"),
                              "NodeName": "n1", "ClientStatus": "running",
                              "TaskStates": {"claude": {"State": "running"}}}}
        req = {"verb": verb, "_caller": self.who, "_subject": self.subject,
               **fields}
        got = soul_answer(self.api, self.project, req)
        if got.get("error"):
            raise bus.Refused(got["error"])
        if verb in ("add", "update") and got.get("bootstrap_marker"):
            # Метку возвращает и регистрация, и перерегистрация рецикла.
            self.marker = got["bootstrap_marker"]
        return got

    def sent(self, verb):
        """Поля последнего (или единственного) вызова глагола."""
        got = [f for v, f in self.seen if v == verb]
        return got[-1] if got else {}


class Quiet:
    """Прогресс без терминала: тихий, как у командлетов вне TTY."""

    def __init__(self, *a):
        pass

    def step(self, text):
        pass

    def clear(self):
        pass


def drive(module, argv, api, who, grant=False):
    """Командлет с проводом на реальный сервер -> провод (code в w.code).

    bus.call_cluster ведёт в cluster.answer (FakeNomad и реестр grants --
    уже подняты soul_env'ом вызывающего), login/principal -- человек или
    принципал, Progress молчит, guard выключен (команда вне проекта),
    workspace_text виден: если фронтенд гранта его прочитал -- провод это
    увидит вызовом, а проверка скажет «не должен был»."""
    w = Wire(api, who, grant)
    reads = []
    wipes = []
    _note_wipe = wipes.append
    with patched(bus, call_cluster=w, login=lambda: None if grant else who,
                 principal=lambda: who if grant else None), \
            patched(cli_add.lib, project_ready=lambda p: True,
                    origin=lambda *a: ORIGIN, Progress=Quiet), \
            patched(cli_update.lib, Progress=Quiet, guard=lambda n: None), \
            patched(cli_recycle.lib, Progress=Quiet, guard=lambda n: None), \
            patched(cli_add._common, workspace_text=lambda o: reads.append(("add", o)) or ("- name: greet\n  tasks: []\n", {"source": "origin", "present": True, "tasks": 1})), \
            patched(cli_update._common, workspace_text=lambda o: reads.append(("update", o)) or ("- name: greet\n  tasks: []\n", {"source": "origin", "present": True, "tasks": 1})), \
            patched(cli_recycle._common, workspace_text=lambda o: reads.append(("recycle", o)) or ("- name: greet\n  tasks: []\n", {"source": "origin", "present": True, "tasks": 1})), \
            patched(cli_recycle.puppets, _wait_stopped=lambda n: None,
                    wipe=lambda *a, **k: _note_wipe((a, k))), \
            patched(bus, request=lambda node, verb, **kw:
                    # Факты клона по проводу (#266): ключи cur/def, числа
                    # парами -- чистый клон, ни работы, ни владельца.
                    {"clone": {"cur": "dev", "def": "dev", "origin": ORIGIN,
                               "dirty": 0, "ahead": 0, "owner": None}}), \
            context.isolated(context.Context()):
        out, err, code = run_command(module.main, argv, via_cli=True)
    w.code, w.out, w.err = code, out, err
    w.reads = reads
    w.wipes = wipes
    return w


def grant_env():
    """Живой грант-мастер на реальном сервере -> (api, ctx, principal, name).

    Родителя поднимает ЧЕЛОВЕК с согласованной политикой (текст человека и
    есть согласование, ревью #582): без неё делегированный add отказан
    «нечего наследовать» (#582). Дальше командлеты действуют от лица его
    принципала -- тем же путём, каким живой грант-мастер зовёт сервис."""
    api = FakeNomad()
    g = soul_env(api)
    root = g.__enter__()
    principal, parent, _got = soul_parent(api, root, workspace=POLICY)
    return api, g, principal, parent


def child_of(api, parent):
    """Джобы-дети родителя (все, кроме него)."""
    return [n for n in api.jobs if n != parent]


def _snapshot_of(name):
    """Текст одобренного снапшота папета в хранилище сервера (#133):
    задачи и конфигурация вместе -- «политика целиком»."""
    from mop.server import bootstrap
    tasks, of_vars = bootstrap.files_of(bootstrap.ROOT, name)
    return (open(tasks).read() if tasks else "") + \
           (open(of_vars).read() if of_vars else "")


import contextlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from mop import driver  # noqa: E402
from mop.common import soul as souls  # noqa: E402
from mop.client import channel  # noqa: E402

BODY_HOME = {"dir": None}


@contextlib.contextmanager
def body(job, caps_over=None):
    """Честное тело роли на время блока: дом тел, бандл-пресет пакета,
    расписка готовности, MOP_SOUL_DIR/MOP_PUPPET в окружении. caps_over --
    чужие capabilities (реквизит инкарнации: файл правится, расписка и
    загрузка пересобираются -- это не подделка digest'а). Фикстура та же,
    что у #585: пресет под тестовым driver.HOME, own_bundle его принимает."""
    if BODY_HOME["dir"] is None:
        BODY_HOME["dir"] = tempfile.mkdtemp(prefix="mop-test-618-body-")
    presets = souls.catalogue([os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
        "mop", "skills")])
    src = presets["master"]
    # Раскладка тел -- под тестовым домом ВМЕСТЕ со сборкой: soul_dir зовут
    # и сборка фикстуры, и own_bundle проверяемого, и оба -- одним HOME.
    with patched(driver, HOME=BODY_HOME["dir"]):
        inc = str(uuid.uuid4())
        root = driver.soul_dir(job, inc)
        bundle = os.path.join(root, driver.SOUL_BUNDLE)
        os.makedirs(bundle)
        for f in os.listdir(src):
            shutil.copy(os.path.join(src, f), os.path.join(bundle, f))
        if caps_over is not None:
            text = open(os.path.join(bundle, souls.MANIFEST)).read()
            doc = _yaml_of(text, caps_over)
            with open(os.path.join(bundle, souls.MANIFEST), "w") as fh:
                fh.write(doc)
        loaded = souls.load(bundle)
        with open(os.path.join(root, driver.READY), "w") as fh:
            json.dump({"ready": True, "role": loaded.role, "soul_inc": inc,
                       "soul_digest": loaded.digest}, fh)
        with patched_env(MOP_SOUL_DIR=bundle, MOP_PUPPET=job):
            yield


def _yaml_of(text, caps_over):
    """Манифест с перекрытым блоком capabilities: строка за строкой,
    без сторонних зависимостей -- пресет читаем, а не пересобираем."""
    out, in_caps = [], False
    for line in text.splitlines():
        if line.startswith("capabilities:"):
            in_caps = True
            out.append(line)
            out.append("  node_verbs: [" + ", ".join(
                caps_over.get("node_verbs", [])) + "]")
            out.append("  cluster_verbs: [" + ", ".join(
                caps_over.get("cluster_verbs", [])) + "]")
            out.append(f"  dispatch: {str(caps_over.get('dispatch', True)).lower()}")
            out.append("  spawn: [" + ", ".join(
                caps_over.get("spawn", ["puppet"])) + "]")
            continue
        if in_caps and line.startswith("  "):
            continue
        if in_caps:
            in_caps = False
        out.append(line)
    return "\n".join(out) + "\n"


def main():
    c = Checks()

    # ── 1. Грант поднимает ребёнка: `add --role` без клиентской политики ──
    # HYPOTHESIS: ops.add всегда везёт workspace, а делегированный add (#582)
    # клиентский текст отвергает -- грант-мастер не может создать ребёнка.
    api, g, principal, parent = grant_env()
    try:
        w = drive(cli_add, ["--role", "puppet"], api, principal, grant=True)
        add = w.sent("add")
        c.check("#618 grant add carries the role, a request_id and no policy",
                add.get("soul") == "puppet" and add.get("request_id")
                and "workspace" not in add and "bootstrap_sent" not in add,
                add)
        c.check("#618 grant add reads no client policy",
                not [r for r in w.reads if r[0] == "add"], w.reads)
        kids = child_of(api, parent)
        c.check("#618 the real server creates the child job with the triple",
                len(kids) == 1
                and api.jobs[kids[0]]["Meta"].get("soul") == "puppet",
                list(api.jobs))
        c.check("#618 the echo is validated and the commandlet succeeds",
                w.code == 0, (w.code, w.seen[:1]))
        c.check("#618 the success line names the created puppet and the role",
                kids[0] in w.out and "puppet" in w.out, (w.out, w.err))
        c.check("#618 the success is silent in stderr",
                w.err == "", w.err)
    finally:
        g.__exit__(None, None, None)

    # ── 2. Роль обязательна для гранта: отказ ДО аллокации ────────────────
    # HYPOTHESIS: `add` без роли от гранта сегодня уехал бы на сервер,
    # который отказал бы уже после резерва; SOLUTION: ранний отказ
    # командлета до первого сетевого вызова.
    api, g, principal, parent = grant_env()
    try:
        w = drive(cli_add, [], api, principal, grant=True)
        c.check("#618 grant add without a role refuses before any call",
                w.seen == [] and w.code not in (0, None), (w.code, w.seen))
    finally:
        g.__exit__(None, None, None)

    # ── 2b. Неизвестность регистрации: BusError без второго создания ────
    # Отзыв ревью 2: ответ регистрации не пришёл -- исход неизвестен; отказ
    # называет request_id (ручной повтор тем же идентификатором сведёт джобы
    # у сервера со сводом), второго слепого создания нет.
    api, g, principal, parent = grant_env()
    try:
        class Deaf(Wire):
            def __call__(self, verb, **fields):
                if verb == "add":
                    self.adds = getattr(self, "adds", 0) + 1
                    if self.adds == 1:
                        raise bus.BusError("cluster: no answer in 30s")
                return Wire.__call__(self, verb, **fields)

        w = Deaf(api, principal, grant=True)
        reads = []
        with patched(bus, call_cluster=w, login=lambda: None,
                     principal=lambda: principal), \
                patched(cli_add.lib, project_ready=lambda p: True,
                        origin=lambda *a: ORIGIN, Progress=Quiet), \
                patched(cli_add._common, workspace_text=lambda o: reads.append(("add", o)) or (PLAY, {"source": "origin", "present": True, "tasks": 1})), \
                context.isolated(context.Context()):
            out, err, code = run_command(cli_add.main, ["--role", "puppet"], via_cli=True)
        c.check("#618 an unanswered registration names the request_id and creates nothing blindly",
                code != 0 and w.adds == 1
                and w.sent("add").get("request_id", "") in err
                and "unknown" in err,
                (code, w.adds, err))
    finally:
        g.__exit__(None, None, None)

    # ── 3. Человек: явная роль и прежний путь без изменений ───────────────
    # HYPOTHESIS: у человека add без роли -- байт в байт прежний вызов
    # ops.add с политикой; с --role -- та же политика плюс поля роли.
    api, g, principal, parent = grant_env()
    try:
        w = drive(cli_add, [], api, "anton")
        add = w.sent("add")
        c.check("#618 human legacy add still sends the policy and no role",
                add.get("workspace") == PLAY
                and "soul" not in add and "request_id" not in add, add)
        w = drive(cli_add, ["--role", "master"], api, "anton")
        add = w.sent("add")
        c.check("#618 human add --role sends policy plus role fields",
                add.get("workspace") == PLAY and add.get("soul") == "master"
                and add.get("request_id"), add)
    finally:
        g.__exit__(None, None, None)

    # ── 4. Старый сервер (без протокола ролей) -- громкий отказ ───────────
    # HYPOTHESIS: `--role` против сервера без soul_protocol молча завёл бы
    # безролевого папета (роль уехала бы в никуда); SOLUTION: ping-запрос
    # (client.roles.protocol) и отказ до add.
    api = FakeNomad()
    g = soul_env(api, roles=False)
    try:
        root = g.__enter__()
        said = []
        with patched(cli_add.lib, fail=said.append):
            w = drive(cli_add, ["--role", "puppet"], api, "anton")
        c.check("#618 add --role on a legacy server refuses before add",
                not [f for v, f in w.seen if v == "add"] and w.code != 0,
                (w.code, w.seen))
    finally:
        g.__exit__(None, None, None)

    # ── 5. Запрещённая роль цели: отказ сервера доезжает до гранта ────────
    # HYPOTHESIS: spawn-allowlist проверяет сервер; SOLUTION: фронтенд не
    # дублирует список -- отказ сервера едет причиной отказа командлета.
    api, g, principal, parent = grant_env()
    try:
        w = drive(cli_add, ["--role", "master"], api, principal, grant=True)
        add = w.sent("add")
        c.check("#618 a forbidden target role is refused by the server",
                "soul" in add and len(api.jobs) == 1, (add, list(api.jobs)))
        c.check("#618 the refusal surfaces as the commandlet's failure",
                w.code != 0, w.code)
        # Отзыв ревью 1: причина ОТКАЗА сервера (spawn) обязана доехать до
        # stderr -- эхо не подменяет её «нет имени созданного».
        c.check("#618 the server's refusal reason reaches stderr, echo does not mask it",
                "spawn" in w.err, w.err)
    finally:
        g.__exit__(None, None, None)

    # ── 5b. Неполный ответ: созданный без тройки -- провал с именем ─────
    # Отзыв ревью 1: сервер создал джоб, но эха нет (легаси-режим, битый
    # ответ) -- это НЕ успех: отказ называет созданного и не создаёт второго.
    # HYPOTHESIS: echo зовётся после ops.add независимо от исхода и не
    # видит имени, когда verdict его потерял; SOLUTION: эхо сверяется с
    # ответом регистрации внутри транспорта, сразу после успеха.
    api, g, principal, parent = grant_env()
    try:
        stripped = {}
        orig = Wire.__call__

        def cutting(self, verb, **fields):
            got = orig(self, verb, **fields)
            if verb == "add" and got.get("name"):
                stripped.update(got)
                for k in ("soul", "soul_digest", "soul_inc"):
                    got.pop(k, None)
            return got

        with patched(Wire, __call__=cutting):
            w = drive(cli_add, ["--role", "puppet"], api, principal, grant=True)
        c.check("#618 an echo-less add answer fails naming the created puppet",
                w.code != 0 and stripped.get("name") in (w.err + w.out),
                (w.code, w.out, w.err))
        c.check("#618 no second creation follows a bad echo",
                len(child_of(api, parent)) == 1, list(api.jobs))
    finally:
        g.__exit__(None, None, None)

    # ── 5d. Нестрогое ok: подтверждение роли -- только ok=True ──────────
    # Отзыв ревью (финал), случай 1: транспорт звал эхо по ЛЮБОМУ truthy
    # ok -- ответ {name: ...} без ok, ok=1 или ok="x" обходил эхо, и
    # ops.add ждал аллокаций «успеха», которого не подтверждал никто.
    # Решение -- strict is True: не-true ok с битой тройкой (или без неё)
    # -- отказ «исход неизвестен» с request_id, БЕЗ alloc/wait и без
    # второго add; ok=True с честной тройкой -- прежний успех.
    for lie in (None, False, 1, "yes"):
        api, g, principal, parent = grant_env()
        try:
            patched_call = Wire.__call__

            def lying(self, verb, _lie=lie, **fields):
                got = patched_call(self, verb, **fields)
                if verb == "add":
                    if _lie is None:
                        got.pop("ok", None)
                    else:
                        got["ok"] = _lie
                    for k in ("soul", "soul_digest", "soul_inc"):
                        got.pop(k, None)
                return got

            with patched(Wire, __call__=lying):
                w = drive(cli_add, ["--role", "puppet"], api, principal,
                          grant=True)
            adds = [f for v, f in w.seen if v == "add"]
            c.check(f"#618 a non-true ok ({lie!r}) is no role confirmation",
                    w.code != 0 and len(adds) == 1
                    and adds[0].get("request_id", "") in w.err
                    and "unknown" in w.err
                    and not [v for v, _ in w.seen if v == "alloc"]
                    and len(child_of(api, parent)) == 1,
                    (lie, w.code, [v for v, _ in w.seen], w.err))
        finally:
            g.__exit__(None, None, None)

    # ── 5c. Смерть провода ПОСЛЕ ответа регистрации: имя + первопричина ──
    # Отзыв ревью 2, вторая половина: alloc/bootstrap_result молчат, когда
    # джоб УЖЕ создан -- это не «эха нет» и не «исход неизвестен»: отказ
    # зовет созданного по имени и везет первопричину; повтор слепым
    # вторым add не предлагается вовсе.
    api, g, principal, parent = grant_env()
    try:
        class Dead(Wire):
            def __call__(self, verb, **fields):
                if verb == "alloc":
                    raise bus.BusError("cluster: no answer in 30s")
                return Wire.__call__(self, verb, **fields)

        w = Dead(api, principal, grant=True)
        reads = []
        with patched(bus, call_cluster=w, login=lambda: None,
                     principal=lambda: principal), \
                patched(cli_add.lib, project_ready=lambda p: True,
                        origin=lambda *a: ORIGIN, Progress=Quiet), \
                patched(cli_add._common, workspace_text=lambda o: reads.append(("add", o)) or (PLAY, {"source": "origin", "present": True, "tasks": 1})), \
                context.isolated(context.Context()):
            out, err, code = run_command(cli_add.main, ["--role", "puppet"], via_cli=True)
        kids = child_of(api, parent)
        c.check("#618 a post-registration outage names the created job and its cause",
                code != 0 and len(kids) == 1 and kids[0] in err
                and "no answer in 30s" in err and "unknown" not in err,
                (code, kids, err))
    finally:
        g.__exit__(None, None, None)

    # ── 6. MCP-объявление add: аргумент role ─────────────────────────────
    # HYPOTHESIS: MCP add принимает только origin; SOLUTION: args role
    # (флаг --role), маршрут -- тот же командлет.
    import json
    decl = cli_add.MCP
    args = {a["name"]: a for a in decl.get("args", [])}
    c.check("#618 the MCP add declares the role argument",
            args.get("role", {}).get("flag") == "--role", decl)
    c.check("#618 the MCP add route keeps ping for the preflight",
            "ping" in decl.get("cluster_verbs", []), decl)

    # ── 7. Update гранта: политика не читается и не едет ──────────────────
    # HYPOTHESIS: update командлет всегда читает политику рабочей копии и
    # везёт её; SOLUTION: preserve_workspace=True (#617) -- ни чтения, ни
    # полей; сервер держит положенную политику сам.
    api, g, principal, parent = grant_env()
    try:
        api.allocs[parent] = {"ID": "a1", "NodeName": "n1",
                              "ClientStatus": "running"}
        w = drive(cli_update, [parent, "--fresh"], api, principal, grant=True)
        up = w.sent("update")
        c.check("#618 grant update sends no policy fields",
                "workspace" not in up and "bootstrap_sent" not in up, up)
        c.check("#618 grant update reads no client policy",
                not [r for r in w.reads if r[0] == "update"], w.reads)
        # Отзыв ревью 4: не только «полей нет» -- успех и ДОБРОСЛОВЛЕННАЯ
        # политика живого папета не тронута (снапшот сервера на месте).
        c.check("#618 grant update succeeds and keeps the approved snapshot",
                w.code == 0 and "answer" in _snapshot_of(parent),
                (w.code, w.err))
    finally:
        g.__exit__(None, None, None)

    # ── 8. Recycle гранта: рабочая копия не читается ──────────────────────
    # HYPOTHESIS: recycle всегда собирает workspace_of; SOLUTION: грант
    # зовёт puppets.recycle(workspace_of=None) -- контракт «рецикл без
    # рабочей копии», как у mop server gc.
    api, g, principal, parent = grant_env()
    try:
        api.allocs[parent] = {"ID": "a1", "NodeName": "n1",
                              "ClientStatus": "running"}
        # Полный маршрут роли (пресет мастера держит весь путь рецикла):
        # префлайт 9 -- не фильтр «только избранным», а честный коридор.
        with body(parent):
            w = drive(cli_recycle, [parent], api, principal, grant=True)
        up = w.sent("update")
        c.check("#618 grant recycle re-registers without policy fields",
                "workspace" not in up and "bootstrap_sent" not in up, up)
        c.check("#618 grant recycle reads no client policy",
                not [r for r in w.reads if r[0] == "recycle"], w.reads)
        # Отзыв ревью 3: не только поля -- код 0, джоб перерегистрирован
        # (Meta жива, ветка прежняя) и политика снапшота не тронута.
        c.check("#618 grant recycle succeeds, the job re-registered, snapshot kept",
                w.code == 0 and api.jobs.get(parent, {}).get("Meta", {}).get("origin") == ORIGIN
                and "answer" in _snapshot_of(parent),
                (w.code, list(api.jobs), w.err))
    finally:
        g.__exit__(None, None, None)

    # ── 10. --request-id: ручной повтор тем же идентификатором ──────────
    # Отзыв ревью (финал), случай 2: отказ неизвестности учит «повтори тем
    # же request_id», а флага не было -- командлет всегда заводил новый
    # UUID, и выполнить инструкцию было нельзя. Решение: --request-id
    # (только с --role, грамматика soul.HEX32 -- «Reuse RoleSource known
    # schema»), MCP-аргумент request_id; по умолчанию -- прежний свежий
    # UUID. Проверки -- против реального сервера 582: ретрай тем же id
    # поднимает СОХДАННЫЙ джоб, смена полезной нагрузки -- отказ до
    # эффекта, легаси-add с id без роли -- ранний usage.
    api, g, principal, parent = grant_env()
    try:
        rid = uuid.uuid4().hex
        w = drive(cli_add, ["--role", "puppet", "--request-id", rid], api,
                  principal, grant=True)
        add = w.sent("add")
        kids = child_of(api, parent)
        c.check("#618 --request-id rides the registration as given",
                add.get("request_id") == rid and len(kids) == 1
                and api.jobs[kids[0]]["Meta"].get("soul") == "puppet",
                (add, list(api.jobs)))

        # 10b. Ретрай тем же id после «неизвестности»: сервер сводит к
        # СОХДАННОМУ джобу (тот же принципал/имя), второго нет.
        w = drive(cli_add, ["--role", "puppet", "--request-id", rid], api,
                  principal, grant=True)
        c.check("#618 a retry with the same request_id reconciles to the same job",
                w.code == 0 and child_of(api, parent) == kids
                and len(child_of(api, parent)) == 1,
                (w.code, w.out, w.err, list(api.jobs)))

        # 10c. Тот же id с ДРУГОЙ полезной нагрузкой (другая роль) --
        # отказ сервера ДО эффекта: подготовленный резерв не даёт завести
        # вторую душу, джобы не прибавилось.
        w = drive(cli_add, ["--role", "builder", "--request-id", rid], api,
                  principal, grant=True)
        c.check("#618 a changed payload under the same request_id refuses",
                w.code != 0 and "request" in w.err
                and child_of(api, parent) == kids,
                (w.code, w.err, list(api.jobs)))

        # 10d. Мусорный id -- ранний отказ до первого вызова.
        w = drive(cli_add, ["--role", "puppet", "--request-id", "nope"], api,
                  principal, grant=True)
        c.check("#618 a malformed request_id refuses before any call",
                w.seen == [] and w.code not in (0, None)
                and "request-id" in w.err, (w.code, w.seen, w.err))

        # 10e. Легаси-add: id без роли -- ранний usage, на провод ничего.
        w = drive(cli_add, ["--request-id", rid], api, "anton")
        c.check("#618 a request_id without a role is an early usage refusal",
                w.seen == [] and w.code not in (0, None), (w.code, w.seen))

        # 10f. Без флага -- свежий UUID, как прежде.
        w = drive(cli_add, ["--role", "puppet"], api, principal, grant=True)
        got_id = w.sent("add").get("request_id")
        c.check("#618 without the flag the request_id is a fresh UUID",
                isinstance(got_id, str) and soul.HEX32.match(got_id)
                and got_id != rid, got_id)

        # 10g. MCP-объявление: аргумент request_id, только с role.
        decl = cli_add.MCP
        args = {a["name"]: a for a in decl.get("args", [])}
        c.check("#618 the MCP add declares the request_id argument",
                args.get("request_id", {}).get("flag") == "--request-id",
                decl)
    finally:
        g.__exit__(None, None, None)

    # ── 9. Recycle гранта: неполный маршрут -- отказ ДО сноса ────────────
    # Отзыв ревью 3: рецикл сносит джоб ДО перерегистрации: если бандл
    # роли держит delete, но не держит update/wipe -- останов доедет, а
    # поднять папета нечем, и пул потерян на полпути. SOLUTION: предзапрос
    # маршрута тем же инструментом, что режет инструменты модели
    # (channel.own_bundle + tools_for по ОБЪЯВЛЕНИЮ самого командлета):
    # новый API прав нет, сервер -- по-прежнему власть; человек пути не
    # касается вовсе.

    # 9a. Бандл с delete без update: отказ ДО первого разрушительного
    # глагола, джоб и его Meta живы.
    api, g, principal, parent = grant_env()
    try:
        with body("pu-alpha-9", {"node_verbs": ["send"], "cluster_verbs":
                                 ["ping", "roster", "alloc", "spec", "delete"],
                                 "spawn": []}):
            w = drive(cli_recycle, [parent], api, principal, grant=True)
        destructive = [v for v, _ in w.seen
                       if v in ("delete", "update", "stop", "restart")]
        c.check("#618 a grant lacking update on its route refuses before any damage",
                w.code != 0 and destructive == [] and w.wipes == []
                and api.jobs.get(parent, {}).get("Meta", {}).get("origin") == ORIGIN,
                (w.code, destructive, w.wipes, w.err))
        c.check("#618 the refusal explains the missing route verb",
                "update" in w.err, w.err)

        # 9b. Бандл недоступен -- тот же отказ до сноса, не молчание.
        with patched_env(MOP_SOUL_DIR=None, MOP_PUPPET=None):
            w = drive(cli_recycle, [parent], api, principal, grant=True)
        c.check("#618 an unavailable bundle refuses the grant recycle too",
                w.code != 0
                and not [v for v, _ in w.seen if v == "delete"]
                and api.jobs.get(parent, {}).get("Meta", {}).get("origin") == ORIGIN,
                (w.code, w.err))
    finally:
        g.__exit__(None, None, None)

    # 9c. Человек: префлайта нет, путь прежний (workspace читается).
    api, g, principal, parent = grant_env()
    try:
        with patched_env(MOP_SOUL_DIR=None, MOP_PUPPET=None):
            w = drive(cli_recycle, [parent], api, "anton")
        up = w.sent("update")
        c.check("#618 the human legacy recycle path is unchanged",
                w.code == 0 and up.get("workspace") == PLAY
                and not [r for r in w.reads if r[0] != "recycle"],
                (w.code, up.get("workspace"), w.reads))
    finally:
        g.__exit__(None, None, None)

    return c.report("roleops")


if __name__ == "__main__":
    sys.exit(main())
