#!/usr/bin/env python3
"""Лаунчер тела `mop driver run` без пула: python3 tests/run.py

Поведение main() целиком: драйвер, push, клон, bootstrap и запуск
врапера подменены, проверяется, ЧТО и В КАКОМ ПОРЯДКЕ лаунчер кладёт в
тело и запускает ли модель. Живое тело (ssh, pct, tmux) -- на пуле.

#590: доставка бандла роли и кредов принципала в тело (docs/SOUL.md).

HYPOTHESIS (#590): лаунчер знает только легаси-путь: берёт b['bus'] --
проектный кред -- кладёт его в общий bus-<проект>.json и стартует модель.
Ролевой джоб (тройка soul/soul_digest/soul_inc в Meta) поднимается с
проектным кредом вместо своего принципала, без бандла, и расхождение
роли/инкарнации/digest не останавливает старт.
SOLUTION: Meta джоба -- доверенное ожидание; ролевой ответ проверяется
soul.from_manifest, сверяется с Meta и кладётся в каталог инкарнации
(driver.soul_dir) до запуска; любой отказ -- выход до модели. Порядок
старта роли: файлы -> identity хозяина глаголом живого агента (шов
initial_identity, #584/#603) -> метка готовности последней; спрос
bootstrap ролевого джоба идёт именованным каналом узла (шов
role_bootstrap_ask), не анонимным server.rpc.
"""
import asyncio
import base64
import io
import json
import os
import re
import shlex
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import yaml  # noqa: E402

from mop import driver  # noqa: E402
from mop.cli.driver import run  # noqa: E402
from mop.common import bus, busnames, soul  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
NAME, OTHER = "pu-mop-3", "pu-mop-4"
INC, INC2 = "1b4e28ba-2fa1-4d2b-9a5e-3c1f0a7b8c9d", "6f1c2a3b-4d5e-4f60-8a71-92b3c4d5e6f7"
GRANT, GRANT2 = "grant-" + "a1" * 16, "grant-" + "b2" * 16
SECRET, SECRET2 = "s3cr3t-pass-word-0001", "s3cr3t-pass-word-0002"
PROJECT_SECRET = "legacy-project-pass-9"
WRAPPER = base64.b64encode(b"echo inner\n").decode()


def role(name="puppet"):
    """Поставляемая роль: манифест словарём (как его хранит сервер), сырой
    промпт, digest -- одним правилом soul.digest."""
    d = os.path.join(REPO, "mop", "skills", name)
    with open(os.path.join(d, "soul.yaml")) as f:
        manifest = yaml.safe_load(f)
    with open(os.path.join(d, manifest["prompt"]), "rb") as f:
        prompt = f.read()
    return manifest, prompt, soul.digest(manifest, prompt)


def meta_env(role_name, digest, inc):
    """Тройка роли так, как её отдаёт Nomad задаче: NOMAD_META_<ключ>."""
    return {"NOMAD_META_soul": role_name, "NOMAD_META_soul_digest": digest,
            "NOMAD_META_soul_inc": inc}


def registered_env(role_name, digest, inc, origin="git@host:group/repo.git"):
    """Окружение НАСТОЯЩЕГО ролевого джоба -- ТОЛЬКО из живой spec.task_env
    #582: тройка роли -- явные PU_* в task_env, НЕ NOMAD_META_автоключи
    (сервер договорился не полагаться на регистр автореки Nomad). Живость
    обязательна: спека, переставшая нести тройку (потерялись поля), или
    старая сигнатура -- ГРОМКИЙ отказ провала, не тихий проход по записи;
    иначе проводной тест зелёнел бы против устаревшего слепка (случай
    уже был). Обрезаем только ключи тройки/origin: остальное окружение
    задачи лаунчеру не принадлежит."""
    from mop.server import spec
    try:
        env = spec.task_env("pu-x-1", origin, soul=role_name,
                            soul_digest=digest, soul_inc=inc)
    except TypeError as e:
        raise ValueError(f"spec.task_env no longer accepts the role triple "
                         f"-- the wire test is stale: {e}") from None
    missing = [k for k in ("PU_SOUL", "PU_SOUL_DIGEST", "PU_SOUL_INC")
               if not env.get(k)]
    if missing:
        raise ValueError(f"spec.task_env came without the role triple "
                         f"({', '.join(missing)}) -- the wire test is stale")
    out = {"PU_ORIGIN": env.get("PU_ORIGIN")}
    for k in ("PU_SOUL", "PU_SOUL_DIGEST", "PU_SOUL_INC", "PU_SOUL_DIR"):
        out[k] = env.get(k)
    return {k: v for k, v in out.items() if v is not None}


# Записанный выход task_env с принятого дерева 56033df (#582) — только
# диагностическая справка происхождения фикстуры; тест из него НЕ читает.
SPEC_582_TASK_ENV = {
    "PU_ORIGIN": "git@host:group/repo.git",
    "PU_SOUL": "puppet",
    "PU_SOUL_DIGEST": "d" * 64,
    "PU_SOUL_INC": "1b4e28ba-2fa1-4d2b-9a5e-3c1f0a7b8c9d",
    "PU_SOUL_DIR": "/home/ermak/.config/mop/souls/pu-x-1/"
                   "1b4e28ba-2fa1-4d2b-9a5e-3c1f0a7b8c9d",
}


OWNER = {"login": "ermak", "name": "Anton", "email": "anton@example.org"}


def payload(role_name, digest, inc, grant, manifest, prompt):
    """Ролевая часть ответа bootstrap'а (форма #590): без кредов -- кред
    едет прежним полем b['bus'], индивидуальным для роли."""
    return {"role": role_name, "soul_inc": inc, "soul_digest": digest,
            "manifest": manifest, "prompt": base64.b64encode(prompt).decode(),
            "principal": grant, "owner": dict(OWNER)}


def creds(user, password):
    return {"url": "nats://10.0.0.1:4222", "user": user, "password": password}


def reply_for(soul_part, bus):
    return {"ok": True, "played": False, "soul": soul_part, "bus": bus}


def legacy_reply():
    return {"ok": True, "played": False,
            "bus": {"url": "nats://10.0.0.1:4222", "user": "puppet-mop",
                    "password": PROJECT_SECRET}}


NODE_USER = "node-wate-1"


class FakeBus:
    """Настоящий транспорт #603 с фальшивой NATS-стороной: запросы
    раскладываются по субъектам, ответы -- из сценария. Кред узла --
    настоящий файл (bus.config читает его сам), соединения не выходит:
    arequest подменён. Субъекты проверяются настоящими busnames, так что
    маршрут и собственный узел ловятся здесь, а не в стабе."""

    def __init__(self, identity=None, bootstrap=None, project="mop",
                 node=NODE_USER):
        import tempfile
        self.log, self.project, self.sink = [], project, None
        self.replies = {"initial_identity": identity or {}, "bootstrap": bootstrap or {}}
        fd, self.creds_file = tempfile.mkstemp(prefix="mop-node-creds-")
        user = node  # имя пользователя -- как есть: и узел, и «не узел»
        with os.fdopen(fd, "w") as f:
            json.dump({"url": "nats://10.0.0.1:4222", "user": user,
                       "password": "node-pass"}, f)

    def reply(self, verb, out):
        self.replies[verb] = out

    def raises(self, exc):
        """Сценарий «глагол не ответил»: сам запрос падает, ответа нет."""
        self.replies["initial_identity"] = exc

    def asked(self, subj):
        return [a for a in self.log if a[0] == subj]

    def __enter__(self):
        import _lib
        import types
        async def fake_arequest(nc, subj, data, timeout):
            body = json.loads(data.decode())
            self.log.append((subj, body))
            if self.sink is not None:
                # Спрос в журнал драйвера в момент события, не постфактум:
                # порядок файлов -> identity -> метка проверяется честно.
                self.sink.append(
                    ("identity", (body["name"], body["soul_inc"]))
                    if body.get("verb") == "initial_identity" else
                    ("bootstrap-ask", body.get("verb", "bootstrap")))
            out = self.replies.get(
                "initial_identity" if "initial_identity" in data.decode() else
                "bootstrap")
            if isinstance(out, Exception):
                raise out
            out = dict(out)
            import types
            return types.SimpleNamespace(data=json.dumps(out).encode())
        # FILE тут не патчуем: патч скрывал бы глобальную подмену FILE
        # лаунчером (#603-обход) — чек «laунчер не трогает FILE» обязан
        # видеть настоящее значение процесса. connect подменён, файл в
        # фейковом пути никто не читает.
        # connect повторяет шов настоящего bus.connect (#603): объект
        # соединения несёт creds-снимок выбранного файла — его читает
        # node_name(); голый object() ломал бы узловой путь молча. Креды
        # для снимка читает config(file) ТОЛЬКО для явного file (узловой
        # путь лаунчера); без file — пустой снимок: легаси-справочник
        # сервера фейковому процессу не нужен, config() без аргумента
        # здесь поднимать нельзя.
        def fake_connect(file=None):
            creds = bus.config(file) if file else {}
            conn = types.SimpleNamespace(is_closed=False, creds=creds)
            bus._conn = conn
            return conn
        self._cm = _lib.patched(bus, arequest=fake_arequest,
                                connect=fake_connect)
        # Файл кредов узла -- туда, откуда его читает config(file): NODE_FILE
        # модуля run подменяем путём подмены busnames.NODE_FILE через env не
        # выйдет (expanduser на импорте), поэтому сам атрибут.
        self._file_cm = _lib.patched(bus, NODE_FILE=self.creds_file)
        self._file_cm.__enter__()
        return self._cm.__enter__()

    def __exit__(self, *exc):
        try:
            self._cm.__exit__(*exc)
            self._file_cm.__exit__(*exc)
        finally:
            bus._conn = None
            os.unlink(self.creds_file)
        return False


class Body:
    """Драйвер с телом-узлом (как host): запись -- в словарь и на диск
    временного дома (бандл читается soul.load, как его прочтёт обёртка),
    шелл -- в журнал. fail -- подстрока пути, на котором push откажет."""

    def __init__(self, fail=None, identity="ok"):
        self.files, self.log, self.fail = {}, [], fail
        # Поведение глагола identity: ok | error | raise.
        self.identity = identity

    async def ensure(self, name, params):
        return {"name": name, "body": None, "created": False}

    def address(self, name):
        return "10.0.0.5"

    async def admit(self, name, on):
        return {}

    def run_argv(self, name):
        return []

    def argv(self, name):
        return []

    async def push(self, name, path, data):
        if self.fail and self.fail in path:
            self.log.append(("push-failed", path))
            return {"error": f"{path}: disk full"}
        self.files[path] = data
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        self.log.append(("push", path))
        return {"written": path}

    async def push_many(self, name, files):
        for path, data in files:
            r = await self.push(name, path, data)
            if r.get("error"):
                return r
        return {"written": [p for p, _ in files]}


def launcher_role_triple(env):
    """Тройка роли так, как её видит лаунчер: PU_* первичен, авторека --
    переходный источник; расхождение здесь не проверяется (это дело
    самого лаунчера), читаем как прочитал бы он."""
    pu = [env.get(f"PU_{k.upper()}") for k in ("soul", "soul_digest", "soul_inc")]
    if any(k in env for k in ("PU_SOUL", "PU_SOUL_DIGEST", "PU_SOUL_INC")):
        keys = pu
    else:
        keys = [env.get(f"NOMAD_META_{k}") for k in ("soul", "soul_digest", "soul_inc")]
    return dict(zip(("soul", "soul_digest", "soul_inc"), [v or None for v in keys]))


def launch(body, reply, env, name=NAME, node=NODE_USER):
    """main() с подменённым миром. -> (код|SystemExit-текст, stdout, лог).
    Лог -- push'и, шеллы в теле и запуск врапера в порядке исполнения."""
    # Тройка -- из источника лаунчера (PU_* первичен, #590): expected_role
    # решает, а фикстура лишь повторяет его грамматику для расписки.
    meta = launcher_role_triple(env)
    body.triple = (meta.get("soul"), meta.get("soul_digest"), meta.get("soul_inc"))
    role_name, digest, inc = body.triple
    # Режим identity у Body -- сценарий ответа агента: гуд-расписка по умолчанию,
    # отказ -- свой ответ глагола (или его отсутствие).
    receipt = {"identity_ready": True, "role": role_name,
               "soul_inc": inc, "soul_digest": digest}
    if body.identity == "error":
        receipt = {"error": "the clone belongs to another owner"}
    elif body.identity == "no-receipt":
        receipt = {"ok": True}
    elif body.identity == "wrong-triple":
        receipt = {**receipt, "role": "builder"}
    transport = FakeBus(identity=receipt, bootstrap=reply, node=node)
    transport.sink = body.log
    if body.identity == "raise":
        transport.raises(bus.BusError("the agent did not answer"))
    class Proc:
        def __init__(self, argv, *a, **kw):
            body.log.append(("launch", argv))

        def wait(self):
            return 0

        def terminate(self):
            pass

    def sh(argv, *a, **kw):
        body.log.append(("sh", argv))

        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()

    base = {"PU_WRAPPER": WRAPPER, "PU_ORIGIN": "git@example:mop.git",
            "PU_CLONE": f"/home/u/puppets/{name}", "NOMAD_META_soul": None,
            "NOMAD_META_soul_digest": None, "NOMAD_META_soul_inc": None}
    import signal
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        with patched_env(**{**base, **env}), \
                patched(driver, current=lambda: body), \
                transport, \
                patched(run.subprocess, run=sh, Popen=Proc):
            out, err, code = run_command(run.main, [name])
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    return (str(code) if isinstance(code, str) else code,
            out + err, body.log, transport)


def brief(log):
    """Журнал для текста провала: шелл -- первыми словами, не сотней строк."""
    return [(k, " ".join(a)[:60] if isinstance(a, list) else a) for k, a in log]


def refused(code):
    """Отказ лаунчера роли -- своим текстом, а не попутным провалом
    легаси-пути (тот тоже ненулевой и тоже до модели)."""
    return isinstance(code, str) and code.startswith("role delivery of ")


def launched(log):
    return any(kind == "launch" for kind, _ in log)


def creds_in(body, path):
    return json.loads(body.files[path]) if path in body.files else None


def wrapper_script(body):
    """Текст внутреннего врапера, долетевший в тело (последняя стадия)."""
    return next((v.decode() for p, v in body.files.items()
                 if p.endswith(".cache/mop-wrapper.sh")), "")


def _export(script, key):
    """Значение export <key> из прелюдии врапера: None -- нет такого экспорта."""
    m = re.search(rf"^export {key}=(.*)$", script, re.M)
    return shlex.split(m.group(1))[0] if m else None


def main():
    c = Checks()
    manifest, prompt, digest = role("puppet")
    bmanifest, bprompt, bdigest = role("builder")
    project_path = driver.project_creds("mop")

    # Окружение НАСТОЯЩЕГО ролевого джоба (#582): тройка -- явные PU_*,
    # а лаунчер 3cf3f7ad читал только NOMAD_META_автоключи и такой джоб
    # молча звал легаси-путь. PU_* -- первичный источник; NOMAD_META --
    # совместимость, без молчаливого отката: расхождение -- ValueError.
    env = registered_env("puppet", digest, INC)
    c.check("#590 the registered role env carries the explicit PU_SOUL triple",
            env.get("PU_SOUL") == "puppet" and env.get("PU_SOUL_DIGEST") == digest
            and env.get("PU_SOUL_INC") == INC and "NOMAD_META_soul" not in env, env)
    got = run.expected_role(env)
    c.check("#590 a registered role job (PU_* only, no NOMAD_META) is a role job",
            got is not None
            and (got.soul, got.soul_digest, got.soul_inc)
            == ("puppet", digest, INC), got)
    c.check("#590 the registered role job's origin rides PU_ORIGIN",
            got is not None and got.origin == "git@host:group/repo.git", got)
    # Явная тройка PU_* решает и при автореке Nomad рядом (реальный кейс
    # переходного периода): расхождение двух троек -- не откат к авто,
    # а громкий отказ.
    mixed = {**env, **meta_env("builder", bdigest, INC)}
    try:
        run.expected_role(mixed)
        c.check("#590 a PU_* job with a conflicting NOMAD_META triple refuses",
                False, mixed)
    except ValueError as e:
        c.check("#590 a PU_* job with a conflicting NOMAD_META triple refuses",
                "disagree" in str(e), str(e))
    # Целостность источника: тройка выбирается ЦЕЛИКОМ из одного места,
    # поля не сшиваются. Частичная PU_* + полная авторека -- не «дырки
    # доложить из META», а отказ; взаимодополняющие половины -- тоже;
    # явно-пустая PU-тройка (легаси-джоб новой спеки) при непустой автореке
    # -- противоречие окружений, а не тихий выбор.
    partial = {**env, "PU_SOUL_DIGEST": "", "PU_SOUL_INC": "",
               **meta_env("puppet", digest, INC)}
    try:
        run.expected_role(partial)
        c.check("#590 a partial PU_* triple is not filled from NOMAD_META",
                False, partial)
    except ValueError as e:
        c.check("#590 a partial PU_* triple is not filled from NOMAD_META",
                "whole" in str(e) or "PU_" in str(e), str(e))
    complementary = {"PU_SOUL": "puppet", "NOMAD_META_soul_digest": digest,
                     "NOMAD_META_soul_inc": INC}
    try:
        run.expected_role(complementary)
        c.check("#590 complementary PU_*/NOMAD_META halves do not merge",
                False, complementary)
    except ValueError as e:
        c.check("#590 complementary PU_*/NOMAD_META halves do not merge",
                "whole" in str(e) or "PU_" in str(e), str(e))
    empty_pu = {**meta_env("puppet", digest, INC),
                "PU_SOUL": "", "PU_SOUL_DIGEST": "", "PU_SOUL_INC": ""}
    try:
        run.expected_role(empty_pu)
        c.check("#590 explicitly empty PU_* with a NOMAD_META triple is a contradiction",
                False, empty_pu)
    except ValueError as e:
        c.check("#590 explicitly empty PU_* with a NOMAD_META triple is a contradiction",
                "PU_" in str(e) or "disagree" in str(e) or "contradict" in str(e), str(e))
    # Явно-пустая PU-тройка БЕЗ автореки -- легаси-джоб новой спеки: None.
    c.check("#590 explicitly empty PU_* without NOMAD_META stays legacy",
            run.expected_role({"PU_SOUL": "", "PU_SOUL_DIGEST": "",
                               "PU_SOUL_INC": ""}) is None, None)

    mixed_same = {**env, **meta_env("puppet", digest, INC)}
    c.expect("#590 a PU_* job with an agreeing NOMAD_META triple stays a role job",
             (lambda g: (g.soul, g.soul_digest, g.soul_inc))(run.expected_role(mixed_same)),
             ("puppet", digest, INC))

    # registered_env ходит ТОЛЬКО в живую спеку: если task_env перестала
    # нести тройку (правка потеряла поля) или сигнатура ещё старая --
    # громкий отказ, не тихий проход по записанному слепку. Иначе
    # критический проводной тест зелёнел бы против устаревшей записи.
    from mop.server import spec as live_spec
    real_task_env = live_spec.task_env
    def stripped_task_env(*a, **k):
        env = dict(real_task_env(*a, **k))
        for key in ("PU_SOUL", "PU_SOUL_DIGEST", "PU_SOUL_INC", "PU_SOUL_DIR"):
            env.pop(key, None)
        return env
    def legacy_task_env(name, origin, cont=False, mem=0):
        return {"PU_NAME": name}
    for label, stub, word in (
            ("without the triple", stripped_task_env, "role triple"),
            ("of the old signature", legacy_task_env, "task_env")):
        with patched(live_spec, task_env=stub):
            try:
                registered_env("puppet", digest, INC)
                c.check(f"#590 registered_env refuses a spec task_env {label}",
                        False, "fell through to the recorded snapshot")
            except ValueError as e:
                c.check(f"#590 registered_env refuses a spec task_env {label}",
                        word in str(e), str(e))

    before_file = bus.FILE  # до первого ролевого запуска: подмена видна

    # Легаси (контроль): джоб без тройки -- прежний путь байт в байт.
    body = Body()
    code, out, log, transport = launch(body, legacy_reply(), {})
    c.expect("#590 legacy: a job without a role launches", code, 0)
    c.expect("#590 legacy: the project credential lands where it always did",
             creds_in(body, project_path), legacy_reply()["bus"])
    c.check("#590 legacy: no role directory appears",
            not any("/souls/" in p for p in body.files), sorted(body.files))
    c.check("#590 legacy: the password is not printed", PROJECT_SECRET not in out, out)
    c.check("#590 legacy: the bootstrap ask rides the anonymous server.rpc",
            bool(transport.asked(busnames.server("mop")))
            and not transport.asked(busnames.server(
                "mop", caller=busnames.node_user(NODE_USER[5:]))),
            transport.log)

    # Легаси-джоб, которому сервер прислал роль, -- рассогласование: отказ.
    body = Body()
    code, out, log, transport = launch(body, {**legacy_reply(), "soul": payload(
        "puppet", digest, INC, GRANT, manifest, prompt)}, {})
    c.check("#590 a legacy job refuses a role payload", refused(code), code)
    c.check("#590 a legacy job with a role payload never launches", not launched(log), brief(log))

    env = meta_env("puppet", digest, INC)
    good = reply_for(payload("puppet", digest, INC, GRANT, manifest, prompt),
                     creds(GRANT, SECRET))
    # Обход bus.FILE (#603-до-снимка) снят: connect() штампует creds на
    # соединение, node_name() читает снимок первым -- глобальную подмену
    # FILE больше не делает никто, и лаунчер в том числе: config() без
    # аргумента продолжает видеть СВОЙ файл процесса, не узловой.
    body = Body()
    code, out, log, _t = launch(body, good, env)
    c.expect("#590 a role job launches over the 603 transport", code, 0)
    c.check("#590 the launcher no longer swaps bus.FILE to the node file",
            bus.FILE == before_file, (before_file, bus.FILE))

    # Ролевой джоб, ответ легаси (только проектный кред): отказ до модели,
    # проектный кред в тело не едет.
    body = Body()
    code, out, log, transport = launch(body, legacy_reply(), env)
    c.check("#590 a role job refuses a legacy-only reply", refused(code), code)
    c.check("#590 a role job without its payload never launches", not launched(log), brief(log))
    c.check("#590 a role job never receives the project credential",
            project_path not in body.files, sorted(body.files))
    c.check("#590 the refusal does not print the project password",
            PROJECT_SECRET not in str(code) + out, (code, out))

    # Ролевой джоб, ролевой ответ, но кред -- проектный: легаси-кред в
    # ролевое тело не едет никак.
    body = Body()
    code, out, log, transport = launch(body, {**good, "bus": legacy_reply()["bus"]}, env)
    c.check("#590 a role job refuses the project credential beside its payload",
            refused(code) and "credential is not" in code and not launched(log)
            and project_path not in body.files, (code, brief(log)))
    c.check("#590 that refusal hides the project password",
            PROJECT_SECRET not in str(code) + out, (code, out))
    # Второй кред внутри роли -- дубль, не «запасной»: отказ.
    body = Body()
    code, out, log, transport = launch(body, {**good, "soul": {**good["soul"], "bus": creds(GRANT, SECRET)}},
                            env)
    c.check("#590 a nested second credential in the role payload refuses",
            refused(code) and "second credential" in code and not launched(log),
            (code, brief(log)))

    # Рассогласования с Meta: инкарнация, digest, роль -- в ответе и в
    # бандле. Каждое -- отказ до модели и без записи кредов.
    mine = creds(GRANT, SECRET)
    other_inc = reply_for(payload("puppet", digest, INC2, GRANT, manifest, prompt), mine)
    tampered = reply_for(payload("puppet", digest, INC, GRANT, manifest, prompt + b"x"), mine)
    claims_builder = reply_for(payload("builder", digest, INC, GRANT, manifest, prompt), mine)
    builder_bundle = reply_for(payload("puppet", digest, INC, GRANT, bmanifest, bprompt), mine)
    alien_user = reply_for(good["soul"], creds(GRANT2, SECRET))
    not_grant = reply_for(payload("puppet", digest, INC, "puppet-mop", manifest, prompt),
                          creds("puppet-mop", SECRET))
    no_password = reply_for(good["soul"], {"url": "nats://10.0.0.1:4222", "user": GRANT})
    bad_manifest = reply_for(payload("puppet", digest, INC, GRANT,
                                     {**manifest, "version": 2}, prompt), mine)
    # Причина названа -- иначе отказ по попутной причине (битая Meta)
    # прошёл бы проверку любого рассогласования.
    for what, bad, menv, why in (
            ("a foreign incarnation", other_inc, env, "soul_inc"),
            ("a prompt that does not hash to the job's digest", tampered, env,
             "hashes to"),
            ("a payload naming another role", claims_builder, env, "role 'builder'"),
            ("another role's bundle", builder_bundle, env, "bundle is role 'builder'"),
            ("a credential of another user", alien_user, env, "credential is not"),
            ("a principal that is not grant-<id>", not_grant, env, "not grant-<id>"),
            ("a credential without a password", no_password, env, "has no password"),
            ("a manifest of an unknown schema", bad_manifest, env, "does not parse"),
            ("a job digest that is not the bundle's",
             good, meta_env("puppet", bdigest, INC), "soul_digest")):
        body = Body()
        code, out, log, transport = launch(body, bad, menv)
        c.check(f"#590 refused before the model: {what}",
                refused(code) and why in code and not launched(log),
                (code, brief(log)))
        c.check(f"#590 no credential written: {what}",
                not any("/souls/" in p for p in body.files)
                and project_path not in body.files, sorted(body.files))
        c.check(f"#590 the refusal hides the password: {what}",
                SECRET not in str(code) + out, (code, out))

    # Половина тройки в Meta -- битый джоб: отказ до тела.
    body = Body()
    code, out, log, transport = launch(body, good, {"NOMAD_META_soul": "puppet"})
    c.check("#590 half a role triple in Meta refuses before the body",
            refused(code) and not log, (code, brief(log)))

    # Удачная доставка: бандл и кред -- в каталоге инкарнации, метка
    # готовности последней, проектного креда нет, модель -- после всего.
    body = Body()
    code, out, log, transport = launch(body, good, env)
    d = driver.soul_dir(NAME, INC) if hasattr(driver, "soul_dir") else None
    c.expect("#590 a valid role payload launches", code, 0)
    subj = busnames.startup_subject("mop", NODE_USER[5:])
    asks = transport.asked(subj)
    c.check("#590 the identity ask rides the own-node startup channel",
            len(asks) == 1, transport.log)
    if asks:
        c.expect("#590 the startup body carries only verb, name, soul_inc",
                 asks[0][1], {"verb": "initial_identity", "name": NAME,
                              "soul_inc": INC})
    node_subj = busnames.server("mop", caller=busnames.node_user(NODE_USER[5:]))
    boots = transport.asked(node_subj)
    c.check("#590 the role bootstrap ask rides the node-qualified server.rpc",
            len(boots) == 1, transport.log)
    if boots:
        c.expect("#590 the bootstrap ask carries the body address",
                 {k: boots[0][1].get(k) for k in ("verb", "name", "address")},
                 {"verb": "bootstrap", "name": NAME, "address": "10.0.0.5"})
    c.check("#590 the role directory is per puppet and incarnation",
            d is not None and d.endswith(f"/souls/{NAME}/{INC}"), d)
    if d:
        c.expect("#590 the principal's credential lands in the incarnation",
                 creds_in(body, f"{d}/bus.json"), creds(GRANT, SECRET))
        bundle_dir = f"{d}/bundle"
        c.expect("#590 the bundle directory holds exactly soul.yaml and the prompt",
                 sorted(p for p in body.files if p.startswith(bundle_dir + "/")),
                 sorted([f"{bundle_dir}/soul.yaml", f"{bundle_dir}/{manifest['prompt']}"]))
        c.expect("#590 the prompt lands raw",
                 body.files.get(f"{bundle_dir}/{manifest['prompt']}"), prompt)
        try:
            loaded = soul.load(bundle_dir).digest
        except (OSError, ValueError) as e:
            loaded = f"{type(e).__name__}: {e}"
        c.expect("#590 the delivered bundle loads to the job's digest", loaded, digest)
        ready = json.loads(body.files.get(f"{d}/ready.json") or "null")
        c.expect("#590 the ready mark is the node facts shape, no principal",
                 ready, {"ready": True, "role": "puppet", "soul_inc": INC,
                         "soul_digest": digest})
        pushes = [p for kind, p in log if kind == "push"]
        order = [pushes.index(f"{d}/{f}") if f"{d}/{f}" in pushes else -1
                 for f in ("bus.json", f"bundle/{manifest['prompt']}", "bundle/soul.yaml",
                           "ready.json")]
        c.check("#590 the ready mark is the last role file written",
                min(order) >= 0 and order[-1] > max(order[:-1]), pushes)
        at_ready = [i for i, e in enumerate(log) if e == ("push", f"{d}/ready.json")]
        first_launch = [i for i, (k, _) in enumerate(log) if k == "launch"]
        c.check("#590 the model starts after the ready mark",
                at_ready and first_launch and first_launch[0] > at_ready[0], brief(log))
        ident = [i for i, (k, a) in enumerate(log) if k == "identity"]
        last_file = [i for i, (k, p) in enumerate(log)
                     if k == "push" and p.startswith(d + "/")
                     and p != f"{d}/ready.json"]
        c.check("#590 the owner identity settles after the files, before the mark",
                ident and last_file and max(last_file) < ident[0]
                and at_ready and ident[0] < at_ready[0], brief(log))
        c.expect("#590 the identity verb hears the job and the incarnation",
                 log[ident[0]][1] if ident else None, (NAME, INC))
        # Метка прошлого старта той же инкарнации снята ДО записи новых
        # файлов: оборванная запись не оставит «готово» над половиной.
        rm = [i for i, (k, a) in enumerate(log)
              if k == "sh" and "ready.json" in " ".join(a) and "rm" in " ".join(a)]
        bus_at = [i for i, e in enumerate(log) if e == ("push", f"{d}/bus.json")]
        c.check("#590 a stale ready mark is removed before the role files",
                rm and bus_at and rm[0] < bus_at[0], brief(log))
    c.check("#590 a role job has no project credential in its body",
            project_path not in body.files, sorted(body.files))
    c.check("#590 the password never reaches argv",
            not any(SECRET in " ".join(a) for k, a in log if k in ("sh", "launch")), brief(log))
    c.check("#590 the password is not printed", SECRET not in out, out)

    # #583-согласованные поля окружения тела (#590): явные значения роли --
    # в мету джоба их пишет сервер, и обёртка читает их из окружения, а не
    # из файла конфигурации. MOP_SOUL_DIR -- корень/бандл, не корень;
    # PU_NODE -- из авторизованного креда узла, без падения в hostname
    # контейнера. Легаси-джоб этих полей не получает.
    w = wrapper_script(body)
    c.expect("#590 the wrapper exports PU_SOUL", _export(w, "PU_SOUL"), "puppet")
    c.expect("#590 the wrapper exports PU_SOUL_DIGEST", _export(w, "PU_SOUL_DIGEST"), digest)
    c.expect("#590 the wrapper exports PU_SOUL_INC", _export(w, "PU_SOUL_INC"), INC)
    c.expect("#590 the wrapper exports PU_SOUL_DIR", _export(w, "PU_SOUL_DIR"), d)
    c.expect("#590 the wrapper exports MOP_SOUL_DIR", _export(w, "MOP_SOUL_DIR"),
             f"{d}/bundle" if d else None)
    c.expect("#590 the wrapper exports PU_NODE", _export(w, "PU_NODE"), NODE_USER[5:])
    # Кред без узлового имени -- не узел: PU_NODE не выдумывается из hostname
    # контейнера, старт роли отказывает. #583: имя узла -- только из креда.
    alien_body = Body()
    acode, aout, alog, _ = launch(alien_body, good, env, node="puppet-mop")
    c.check("#590 a non-node credential refuses the role start",
            refused(acode) and "node credential" in acode and not launched(alog),
            (acode, brief(alog)))

    legacy_body = Body()
    lcode, lout, llog, _ = launch(legacy_body, legacy_reply(), {})
    c.check("#590 legacy: no soul environment is carried",
            not any(_export(wrapper_script(legacy_body), k)
                    for k in ("PU_SOUL", "PU_SOUL_DIGEST", "PU_SOUL_INC",
                              "PU_SOUL_DIR", "MOP_SOUL_DIR", "PU_NODE")),
            wrapper_script(legacy_body))
    c.expect("#590 legacy still launches", lcode, 0)

    # Identity хозяина: отказ глагола -- отказ старта, без метки.
    for mode, why in (("error", "another owner"), ("raise", "did not answer")):
        body = Body(identity=mode)
        code, out, log, transport = launch(body, good, env)
        c.check(f"#590 a refused identity ({mode}) stops before the model",
                refused(code) and why in code and not launched(log), (code, brief(log)))
        c.check(f"#590 a refused identity ({mode}) leaves no ready mark",
                not any(p.endswith("ready.json") for p in body.files), sorted(body.files))
        c.check(f"#590 a refused identity ({mode}) hides the password",
                SECRET not in str(code) + out, (code, out))

    # Расписка identity -- доказательство, а не «ok»: без identity_ready
    # или с чужой тройкой старт роли отказывает, метки нет.
    for mode, why in (("no-receipt", "identity_ready"),
                      ("wrong-triple", "is not the job's")):
        body = Body(identity=mode)
        code, out, log, transport = launch(body, good, env)
        c.check(f"#590 a bad identity receipt ({mode}) stops before the model",
                refused(code) and why in code and not launched(log), (code, brief(log)))
        c.check(f"#590 a bad identity receipt ({mode}) leaves no ready mark",
                not any(p.endswith("ready.json") for p in body.files), sorted(body.files))

    # Хозяин-человек обязателен: без логина и без профиля -- отказ до модели.
    for what, bad_soul in (
            ("no owner", {k: v for k, v in good["soul"].items() if k != "owner"}),
            ("no owner email", {**good["soul"], "owner": {"login": "ermak", "name": "A"}})):
        body = Body()
        code, out, log, transport = launch(body, reply_for(bad_soul, creds(GRANT, SECRET)), env)
        c.check(f"#590 a payload with {what} refuses",
                refused(code) and "owner" in code and not launched(log)
                and not any(p.endswith("ready.json") for p in body.files),
                (code, brief(log)))

    # Маршрут спроса bootstrap: роль -- именованный канал узла, легаси --
    # прежний анонимный server.rpc; дверь в обоих исходах закрывается.
    class D2:
        def __init__(self):
            self.doors = []

        def address(self, name):
            return "10.0.0.9"

        async def admit(self, name, on):
            self.doors.append(on)
            return {}
    d2 = D2()
    ok_reply = {"ok": True, "played": False}
    transport = FakeBus(identity={}, bootstrap=dict(ok_reply))
    with transport:
        run.bootstrap_sandbox(d2, NAME, "mop", node_qualified=True)
        subj = busnames.server("mop", caller=busnames.node_user(NODE_USER[5:]))
        c.check("#590 a role ask goes to the node-qualified server.rpc",
                [(a["verb"], a["name"], a["address"])
                 for _, a in transport.asked(subj)]
                == [("bootstrap", NAME, "10.0.0.9")], transport.log)
        c.expect("#590 the door still closes for a role ask", d2.doors, [True, False])
        transport.log.clear()
        run.bootstrap_sandbox(d2, NAME, "mop")
        c.check("#590 a legacy job asks the anonymous server.rpc as before",
                bool(transport.asked(busnames.server("mop")))
                and not transport.asked(busnames.server(
                    "mop", caller=busnames.node_user(NODE_USER[5:]))),
                transport.log)
        c.expect("#590 the door still closes for the legacy ask", d2.doors[-2:], [True, False])

    # Вторая роль того же проекта на том же доме (host): свои файлы, без
    # коллизии с первой.
    both = Body()
    launch(both, good, env)
    second = reply_for(payload("builder", bdigest, INC2, GRANT2, bmanifest, bprompt),
                       creds(GRANT2, SECRET2))
    code, out, log, transport = launch(both, second, meta_env("builder", bdigest, INC2), name=OTHER)
    if hasattr(driver, "soul_dir"):
        c.expect("#590 the second role launches", code, 0)
        c.expect("#590 the first role keeps its credential",
                 (creds_in(both, f"{driver.soul_dir(NAME, INC)}/bus.json") or {}).get("password"),
                 SECRET)
        c.expect("#590 the second role has its own credential",
                 (creds_in(both, f"{driver.soul_dir(OTHER, INC2)}/bus.json") or {}).get("password"),
                 SECRET2)

    # Отказ записи на любом файле роли -- модель не стартует, метки нет.
    for f in ("bus.json", "soul.yaml", manifest["prompt"], "ready.json"):
        body = Body(fail=f"/{f}")
        code, out, log, transport = launch(body, good, env)
        c.check(f"#590 a failed push of {f} stops before the model",
                refused(code) and not launched(log), (code, brief(log)))
        c.check(f"#590 a failed push of {f} leaves no ready mark",
                not any(p.endswith("ready.json") for p in body.files), sorted(body.files))
        c.check(f"#590 a failed push of {f} hides the password",
                SECRET not in str(code) + out, (code, out))

    # Повтор после обрыва: тот же дом, вторая попытка дописывает всё.
    body = Body(fail="/ready.json")
    launch(body, good, env)
    body.fail = None
    code, out, log, transport = launch(body, good, env)
    c.expect("#590 a retry after a partial write launches", code, 0)
    if hasattr(driver, "soul_dir"):
        c.check("#590 the retry writes the ready mark",
                f"{driver.soul_dir(NAME, INC)}/ready.json" in body.files, sorted(body.files))
    return c.report("run")


if __name__ == "__main__":
    sys.exit(main())
