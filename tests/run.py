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
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import yaml  # noqa: E402

from mop import driver  # noqa: E402
from mop.cli.driver import run  # noqa: E402
from mop.common import soul  # noqa: E402

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


def fake_identity(body):
    """Шов initial_identity: что сделает живой агент (#584), по режиму
    тела -- успех с распиской, отказ с причиной, транспортный отказ,
    расписка без identity_ready, расписка с чужой тройкой."""
    async def identity(name, inc, login, profile):
        body.log.append(("identity", (name, inc, login, profile)))
        if body.identity == "raise":
            raise RuntimeError("the agent did not answer")
        if body.identity == "error":
            return {"error": "the clone belongs to another owner"}
        if body.identity == "no-receipt":
            return {"ok": True}
        role_name, digest, _ = body.triple
        if body.identity == "wrong-triple":
            role_name = "builder"
        return {"identity_ready": True, "role": role_name,
                "soul_inc": inc, "soul_digest": digest}
    return identity


def launch(body, reply, env, name=NAME):
    """main() с подменённым миром. -> (код|SystemExit-текст, stdout, лог).
    Лог -- push'и, шеллы в теле и запуск врапера в порядке исполнения."""
    body.triple = (env.get("NOMAD_META_soul"), env.get("NOMAD_META_soul_digest"),
                   env.get("NOMAD_META_soul_inc"))
    async def role_ask(project, name, address):
        body.log.append(("bootstrap-ask", ("role", project, name, address)))
        return reply

    def legacy_ask(*a, **k):
        body.log.append(("bootstrap-ask", ("legacy",)))
        return reply
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
                patched(run, role_bootstrap_ask=role_ask,
                        initial_identity=fake_identity(body)), \
                patched(run.bus, connect=lambda *a, **k: None,
                        ask_server=legacy_ask), \
                patched(run.subprocess, run=sh, Popen=Proc):
            out, err, code = run_command(run.main, [name])
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    return (str(code) if isinstance(code, str) else code,
            out + err, body.log)


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


def main():
    c = Checks()
    manifest, prompt, digest = role("puppet")
    bmanifest, bprompt, bdigest = role("builder")
    project_path = driver.project_creds("mop")

    # Легаси (контроль): джоб без тройки -- прежний путь байт в байт.
    body = Body()
    code, out, log = launch(body, legacy_reply(), {})
    c.expect("#590 legacy: a job without a role launches", code, 0)
    c.expect("#590 legacy: the project credential lands where it always did",
             creds_in(body, project_path), legacy_reply()["bus"])
    c.check("#590 legacy: no role directory appears",
            not any("/souls/" in p for p in body.files), sorted(body.files))
    c.check("#590 legacy: the password is not printed", PROJECT_SECRET not in out, out)
    c.expect("#590 legacy: the bootstrap ask rides the anonymous server.rpc",
             [a for k, a in log if k == "bootstrap-ask"], [("legacy",)])

    # Легаси-джоб, которому сервер прислал роль, -- рассогласование: отказ.
    body = Body()
    code, out, log = launch(body, {**legacy_reply(), "soul": payload(
        "puppet", digest, INC, GRANT, manifest, prompt)}, {})
    c.check("#590 a legacy job refuses a role payload", refused(code), code)
    c.check("#590 a legacy job with a role payload never launches", not launched(log), brief(log))

    env = meta_env("puppet", digest, INC)
    good = reply_for(payload("puppet", digest, INC, GRANT, manifest, prompt),
                     creds(GRANT, SECRET))

    # Ролевой джоб, ответ легаси (только проектный кред): отказ до модели,
    # проектный кред в тело не едет.
    body = Body()
    code, out, log = launch(body, legacy_reply(), env)
    c.check("#590 a role job refuses a legacy-only reply", refused(code), code)
    c.check("#590 a role job without its payload never launches", not launched(log), brief(log))
    c.check("#590 a role job never receives the project credential",
            project_path not in body.files, sorted(body.files))
    c.check("#590 the refusal does not print the project password",
            PROJECT_SECRET not in str(code) + out, (code, out))

    # Ролевой джоб, ролевой ответ, но кред -- проектный: легаси-кред в
    # ролевое тело не едет никак.
    body = Body()
    code, out, log = launch(body, {**good, "bus": legacy_reply()["bus"]}, env)
    c.check("#590 a role job refuses the project credential beside its payload",
            refused(code) and "credential is not" in code and not launched(log)
            and project_path not in body.files, (code, brief(log)))
    c.check("#590 that refusal hides the project password",
            PROJECT_SECRET not in str(code) + out, (code, out))
    # Второй кред внутри роли -- дубль, не «запасной»: отказ.
    body = Body()
    code, out, log = launch(body, {**good, "soul": {**good["soul"], "bus": creds(GRANT, SECRET)}},
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
        code, out, log = launch(body, bad, menv)
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
    code, out, log = launch(body, good, {"NOMAD_META_soul": "puppet"})
    c.check("#590 half a role triple in Meta refuses before the body",
            refused(code) and not log, (code, brief(log)))

    # Удачная доставка: бандл и кред -- в каталоге инкарнации, метка
    # готовности последней, проектного креда нет, модель -- после всего.
    body = Body()
    code, out, log = launch(body, good, env)
    d = driver.soul_dir(NAME, INC) if hasattr(driver, "soul_dir") else None
    c.expect("#590 a valid role payload launches", code, 0)
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
        c.expect("#590 the identity verb hears the job and the owner",
                 log[ident[0]][1] if ident else None,
                 (NAME, INC, OWNER["login"],
                  {"name": OWNER["name"], "email": OWNER["email"]}))
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

    # Identity хозяина: отказ глагола -- отказ старта, без метки.
    for mode, why in (("error", "another owner"), ("raise", "did not answer")):
        body = Body(identity=mode)
        code, out, log = launch(body, good, env)
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
        code, out, log = launch(body, good, env)
        c.check(f"#590 a bad identity receipt ({mode}) stops before the model",
                refused(code) and why in code and not launched(log), (code, brief(log)))
        c.check(f"#590 a bad identity receipt ({mode}) leaves no ready mark",
                not any(p.endswith("ready.json") for p in body.files), sorted(body.files))

    # Хозяин-человек обязателен: без логина и без профиля -- отказ до модели.
    for what, bad_soul in (
            ("no owner", {k: v for k, v in good["soul"].items() if k != "owner"}),
            ("no owner email", {**good["soul"], "owner": {"login": "ermak", "name": "A"}})):
        body = Body()
        code, out, log = launch(body, reply_for(bad_soul, creds(GRANT, SECRET)), env)
        c.check(f"#590 a payload with {what} refuses",
                refused(code) and "owner" in code and not launched(log)
                and not any(p.endswith("ready.json") for p in body.files),
                (code, brief(log)))

    # Швы до появления транспорта (#603/#584): громкий отказ без тикета в тексте.
    for name in ("role_bootstrap_ask", "initial_identity"):
        seam = getattr(run, name, None)
        if seam is None:
            c.fail(f"#590 the seam {name} must exist", "no attribute")
            continue
        calls = {"role_bootstrap_ask": (run.role_bootstrap_ask,
                                        ("mop", NAME, "10.0.0.5")),
                 "initial_identity": (run.initial_identity,
                                      (NAME, INC, OWNER["login"],
                                       {"name": OWNER["name"],
                                        "email": OWNER["email"]}))}
        fn, args = calls[name]
        try:
            asyncio.run(fn(*args))
            c.fail(f"#590 the unwired {name} must refuse", "no refusal")
        except RuntimeError as e:
            c.check(f"#590 the unwired {name} refuses with a ticket-free reason",
                    bool(str(e)) and "#" not in str(e), str(e))

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
    d2, asked = D2(), []
    ok_reply = {"ok": True, "played": False}

    async def role_ask(project, name, address):
        asked.append(("role", project, name, address))
        return dict(ok_reply)

    def legacy_ask(*a, **k):
        asked.append("legacy")
        return dict(ok_reply)
    with patched(run.bus, connect=lambda *a, **k: None, ask_server=legacy_ask), \
            patched(run, role_bootstrap_ask=role_ask):
        run.bootstrap_sandbox(d2, NAME, "mop", node_qualified=True)
        c.expect("#590 a role ask goes to the node-qualified channel by name",
                 asked, [("role", "mop", NAME, "10.0.0.9")])
        c.expect("#590 the door still closes for a role ask", d2.doors, [True, False])
        asked.clear()
        run.bootstrap_sandbox(d2, NAME, "mop")
        c.expect("#590 a legacy job asks the anonymous server.rpc as before",
                 asked, ["legacy"])
        c.expect("#590 the door still closes for the legacy ask", d2.doors[-2:], [True, False])

    # Вторая роль того же проекта на том же доме (host): свои файлы, без
    # коллизии с первой.
    both = Body()
    launch(both, good, env)
    second = reply_for(payload("builder", bdigest, INC2, GRANT2, bmanifest, bprompt),
                       creds(GRANT2, SECRET2))
    code, out, log = launch(both, second, meta_env("builder", bdigest, INC2), name=OTHER)
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
        code, out, log = launch(body, good, env)
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
    code, out, log = launch(body, good, env)
    c.expect("#590 a retry after a partial write launches", code, 0)
    if hasattr(driver, "soul_dir"):
        c.check("#590 the retry writes the ready mark",
                f"{driver.soul_dir(NAME, INC)}/ready.json" in body.files, sorted(body.files))
    return c.report("run")


if __name__ == "__main__":
    sys.exit(main())
