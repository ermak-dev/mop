#!/usr/bin/env python3
"""Проверка настроек без пула: python3 tests/config.py

Здесь только то, что считается, а не лежит литералом: у вычисленного дефолта
есть шанс оказаться неверным, и проявляется это далеко от config.py.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле.
"""
import os
import subprocess
import tempfile
import pwd
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import config, manifest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


# HYPOTHESIS: config.effective() передавал MOP_PROXY_KEY и в таблицу,
# и в JSON для Ansible; unit с режимом 0644 содержал тот же секрет.
# SOLUTION: печатный вывод маскирует значение, playbook_vars исключает ключ
# целиком, а SERVER_SCOPED не включает его в юнит. Раздатчик читает файл.
# RESULT: оба вида вывода и unit не содержат секретного значения.
# STATUS: FIXED — see #394, #402
def check_proxy_key_redaction_394(c):
    from mop.cli.server import config as server_config
    from mop.server import playvars
    with patched_env(MOP_PROXY_KEY="sentinel-do-not-print"):
        config.forget()
        table, _, _ = run_command(server_config.main, [])
        machine, _, _ = run_command(server_config.main, ["--json"])
        c.check("#394 settings table never prints the proxy key",
                "sentinel-do-not-print" not in table and "MOP_PROXY_KEY" in table)
        c.check("#394 settings JSON does not carry the proxy key",
                "MOP_PROXY_KEY" not in playvars.playbook_vars()
                and "sentinel-do-not-print" not in machine)
        c.check("#402 cluster's public unit gets no proxy key",
                "MOP_PROXY_KEY" not in config.SERVER_SCOPED["mop-cluster"])
    config.forget()


def main():
    c = Checks()
    check_proxy_key_redaction_394(c)
    me = pwd.getpwuid(os.getuid()).pw_name

    # Защемлено живым отказом (2026-09-17, первый контейнерный папет).
    # Дефолт MOP_USER брался из $USER. Внешний врапер исполняется задачей
    # Nomad, а клиент Nomad ходит под root и свой $USER задаче отдаёт —
    # поэтому драйвер собрал `ssh root@<тело>` и получил «Permission denied»
    # при совершенно исправном ключе. uid процесса — факт, $USER — всего лишь
    # утверждение, и верить надо факту.
    with patched_env(USER="root-from-the-nomad-client"):
        c.check("pool_user() believed $USER over the real uid",
                not (config.pool_user() != me),
                f"{config.pool_user()!r}, wanted {me!r}")
        os.environ.pop("USER", None)
        c.check("pool_user() must work with no $USER at all — that is "
                "exactly a Nomad task's environment",
                not (config.pool_user() != me))

    # ── старшинство источников ───────────────────────────────────────────
    # Окружение > node.env > .env > дефолт. Ярус node.env появился потому, что
    # .env на узлы не едет (там креды GitLab), а тринадцать настроек читаются
    # именно на узле — все MOP_PVE_*, потолок памяти тела, пользователь и дом.
    # Пока яруса не было, вписанное в .env значение доезжало до мастера и не
    # доезжало до узла: сборка образа клала тело на одно хранилище, драйвер на
    # узле искал его на другом, и не жаловался никто.
    node = os.path.join(tempfile.mkdtemp(), "node.env")
    envf = os.path.join(tempfile.mkdtemp(), ".env")
    with open(envf, "w") as f:
        f.write("MOP_PVE_STORAGE=from-env-file\nMOP_CORES=2\n")
    try:
        with patched(config, NODE_ENV_FILE=node, ENV_FILE=envf):
            config.forget()
            c.check(".env must outrank the default when there is no node file",
                    not (config.get("MOP_PVE_STORAGE") != "from-env-file"))

            with open(node, "w") as f:
                f.write("# узловое, положено прогоном deploy\n"
                        "MOP_PVE_STORAGE=from-node\n")
            config.forget()
            c.check("node.env must outrank .env — that is the whole point",
                    not (config.get("MOP_PVE_STORAGE") != "from-node"))
            c.check("a setting absent from node.env must still come from .env",
                    not (config.num("MOP_CORES") != 2))

            os.environ["MOP_PVE_STORAGE"] = "from-environment"
            c.check("the environment must outrank node.env — a one-off run "
                    "has to keep working",
                    not (config.get("MOP_PVE_STORAGE") != "from-environment"))
            os.environ.pop("MOP_PVE_STORAGE", None)

            os.unlink(node)
            config.forget()
            c.check("a missing node.env is the normal case on the control "
                    "machine, not an error",
                    not (config.get("MOP_PVE_STORAGE") != "from-env-file"))
            c.check("effective() must name where a value came from",
                    not (config.effective()["MOP_CORES"][1] != ".env"))
            with open(node, "w") as f:
                f.write("MOP_CORES=8\n")
            config.forget()
            c.check("effective() must call the node file by its name",
                    not (config.effective()["MOP_CORES"] != ("8", "node")),
                    f"{config.effective()['MOP_CORES']!r}")
    finally:
        config.forget()

    # mop.yaml: манифест проекта (#26). Одна игра: vars — просьба и
    # конфигурация проекта, tasks — его окружение сверх общего. Форма строго
    # оговорена, и отход от неё — ошибка, а не «прочиталось как получилось»:
    # молчаливо потерянные tasks означают образ без окружения проекта.
    MANIFEST = [
        ("полный манифест", """- name: what a body is\n  vars:\n    MOP_CORES: "8"\n    postgres_major: 18\n  tasks:\n    - name: t\n""",
         {"MOP_CORES": "8", "postgres_major": 18}, [{"name": "t"}]),
        ("только vars — манифест mop", """- name: x\n  vars:\n    MOP_DISK_GB: "40"\n""",
         {"MOP_DISK_GB": "40"}, []),
        ("пустая игра допустима", "- name: nothing to ask\n", {}, []),
        ("пустой vars", """- name: x\n  vars: {}\n  tasks: []\n""", {}, []),
    ]
    for what, text, want_vars, want_tasks in MANIFEST:
        try:
            got_vars, got_tasks = manifest.play(text)
        except ValueError as e:
            c.fail(f"mop.yaml, {what}", f"поднялся ValueError {e}")
            continue
        c.check(f"mop.yaml, {what}",
                not (got_vars != want_vars or got_tasks != want_tasks),
                f"wanted: {want_vars!r} / {want_tasks!r}; got: {got_vars!r} / {got_tasks!r}")

    # HYPOTHESIS: отход от формы — не список игр, две игры, игра не словарь,
    # пустой файл — обязан валиться ValueError: у манифеста один хозяин и
    # одна форма, «почти правильный» файл это образ без половины замысла.
    for what, text in [
        ("не список", "vars: {}\n"),
        ("две игры", "- name: a\n- name: b\n"),
        ("игра не словарь", "- просто строка\n"),
        ("пустой файл", ""),
        ("vars не словарь", "- name: x\n  vars: 5\n"),
        ("tasks не список", "- name: x\n  tasks: {}\n"),
    ]:
        try:
            manifest.play(text)
            refused = False
        except ValueError:
            refused = True
        c.check(f"mop.yaml, {what}: должен был отказаться ValueError", refused)

    # Расщепление vars манифеста (#26): просьбы (PROJECT_SCOPED) идут в размеры
    # образа, остальное не-настройочное — конфигурация самого проекта и едет
    # его задачам. А вот имя, совпадающее с настоящей настройкой mop, — чужое:
    # в контексте задач оно затёрло бы правду машины (MOP_USER, MOP_HOME).
    PARTS = [
        ("полный расклад",
         {"MOP_CORES": 8, "postgres_major": 18, "MOP_USER": "root"},
         {"MOP_CORES": "8"}, {"postgres_major": 18}, ["MOP_USER"]),
        ("только просьбы", {"MOP_DISK_GB": "40"}, {"MOP_DISK_GB": "40"}, {}, []),
        ("только своё", {"toolchain": "rust"}, {}, {"toolchain": "rust"}, []),
        ("пусто", {}, {}, {}, []),
    ]
    for what, mvars, want_asks, want_mine, want_alien in PARTS:
        asks, mine, alien = manifest.parts(mvars)
        c.check(f"parts, {what}",
                not (asks != want_asks or mine != want_mine or sorted(alien) != want_alien),
                f"wanted: {want_asks!r}/{want_mine!r}/{want_alien!r}; "
                f"got: {asks!r}/{mine!r}/{alien!r}")

    # Всё, что проект вправе просить, обязано быть настройкой: иначе оно
    # никуда не доедет, а отказа не будет.
    unknown = [k for k in config.PROJECT_SCOPED if k not in config.SETTINGS]
    c.check("PROJECT_SCOPED names settings that do not exist", not (unknown), unknown)

    # Потолок — узловой: чужой проект просит, машина решает. Без потолка mop.yaml
    # это способ занять гипервизор, а не настройка.
    for cap in ("MOP_BODY_MEM_CAP_MB", "MOP_BODY_DISK_CAP_GB", "MOP_BODY_CORES_CAP"):
        c.check(f"{cap} must be node-scoped — the machine has the last word",
                not (cap not in config.NODE_SCOPED))

    # Список узловых настроек -- один: по нему deploy решает, что рендерить в
    # node.env. Разойдись он с тем, что читает узел, и настройка молча не
    # доедет -- ровно та беда, ради которой ярус и заводился.
    # Память -- свойство папета, не узла (#197): в node.env ей не место,
    # строка инвентаря mop_mem_mb -- отказ deploy. Потолок узла остаётся выше.
    c.check("MOP_MEM_MB is the puppet's, not the node's: out of NODE_SCOPED (#197)",
            not ("MOP_MEM_MB" in config.NODE_SCOPED))

    unknown = [k for k in config.NODE_SCOPED if k not in config.SETTINGS]
    c.check("NODE_SCOPED names settings that do not exist", not (unknown), unknown)

    # #358: подметание зовёт агент узла (глагол sweep), а агент читает
    # настройки из node.env -- .env туда не едет. Пороги ехали только в спеку
    # pu-cleanup через --extra-vars, и агент их не увидел бы: pu-sweep
    # молча мёл бы по своим зашитым дефолтам, а не по порогам установки.
    # STATUS: FIXED — see #358
    for knob in ("MOP_SWEEP_FREE_MIN_GB", "MOP_SWEEP_MAX_TARGET", "MOP_SWEEP_STALE_DAYS"):
        c.check(f"#358 {knob} reaches the node agent: in NODE_SCOPED",
                knob in config.NODE_SCOPED)

    # Настройка старше дефолта: установка, где пользователь пула не совпадает
    # с тем, под кем крутится mop, вписывает его в .env.
    with patched_env(MOP_USER="someone-else"):
        c.check("MOP_USER from the environment must outrank the default",
                not (config.get("MOP_USER") != "someone-else"))

    # Шлюз сети тел считается из подсети: два места для одного адреса разошлись
    # бы молча — мост встал бы, а тела просто не достучались.
    with patched_env(MOP_PVE_SUBNET="192.168.250.0/24"):
        c.check("gateway must be derived from the subnet",
                not (config.get("MOP_PVE_GATEWAY") != "192.168.250.1"),
                f"{config.get('MOP_PVE_GATEWAY')!r}")

    # Ключ пула к git: первый существующий из стандартных имён контроллера, а
    # не зашитый id_rsa. HYPOTHESIS (#66): на хосте с одним id_ed25519 deploy
    # падал посреди плейбука на «Could not find id_rsa on the Controller».
    keys = tempfile.mkdtemp()
    ed = os.path.join(keys, "id_ed25519")
    rsa = os.path.join(keys, "id_rsa")
    c.check("no key at all must be empty, so deploy can refuse up front",
            not (config.git_key([ed, rsa]) != ""))
    open(rsa, "w").close()
    c.check("the only key present must win", not (config.git_key([ed, rsa]) != rsa),
            f"{config.git_key([ed, rsa])!r}")
    open(ed, "w").close()
    c.check("with both present the first candidate wins, and it is ed25519",
            not (config.git_key([ed, rsa]) != ed))
    with patched_env(MOP_GIT_KEY="/elsewhere/pool-key"):
        c.check("MOP_GIT_KEY from the environment must outrank the derived default",
                not (config.get("MOP_GIT_KEY") != "/elsewhere/pool-key"))

    # HYPOTHESIS (#125): сервер -- один на машину (окружение или .env), и
    # второй сервер требовал MOP_SERVER_LAN=... в каждой команде.
    # SOLUTION: привязка клона; с #131 -- через контекст команды, который
    # старше файлов. STATUS: FIXED — see #125, #131
    from mop.common import context
    with patched_env(MOP_SERVER_LAN=None):
        with context.use(context.resolve({}, {}, {"server": "bound.example"})):
            c.check("the context's server must be MOP_SERVER_LAN",
                    not (config.get("MOP_SERVER_LAN") != "bound.example"))
            # Контекст задаёт только свои поля.
            c.check("a context must not set settings other than the server",
                    not (config.get("MOP_NATS_PORT") != config.SETTINGS["MOP_NATS_PORT"]
                         and not os.environ.get("MOP_NATS_PORT")))

    # HYPOTHESIS (#156): config -- нижний слой, а тянул вверх operators и
    # deps (playbook_vars), держал разбор манифеста и копию полей контекста;
    # три настройки процесса читались мимо config и в `mop server config` не видны.
    # SOLUTION: разбор манифеста -- в manifest, playbook_vars -- в playvars,
    # поля контекста объявляет context и сам вписывает их в config; три
    # переменные -- PROCESS_SCOPED: в SETTINGS, но только из окружения.
    # STATUS: FIXED — see #156
    probe = ("import sys; sys.path.insert(0, %r); import mop.common.config as c; "
             "c.get('MOP_SERVER_LAN'); c.effective(); "
             "print(sorted(m for m in ('mop.server.operators', 'mop.common.deps', 'mop.common.context') "
             "if m in sys.modules))" % ROOT)
    got = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True).stdout.strip()
    c.check("import mop.config loads modules from above", not (got != "[]"), got)

    c.check("CONTEXT_SCOPED",
            not (context.FIELDS and config.CONTEXT_SCOPED != {"MOP_SERVER_LAN": "server"}),
            f"{config.CONTEXT_SCOPED!r}")

    PROCESS = ("MOP_SERVER_DIR", "MOP_BUS_CONFIG", "MOP_PROJECT")
    c.check("PROCESS_SCOPED", not (getattr(config, "PROCESS_SCOPED", None) != PROCESS),
            f"{getattr(config, 'PROCESS_SCOPED', None)!r}")
    listed = config.effective()
    try:
        with patched_env(**{k: None for k in PROCESS}):
            # Файлы не переключают проект и каталог кредов: только окружение.
            config._cache[config.ENV_FILE] = {k: "from-env-file" for k in PROCESS}
            config._cache[config.NODE_ENV_FILE] = {k: "from-node-env" for k in PROCESS}
            for k in PROCESS:
                c.check(f"{k} must be a setting with default '' listed by mop server config",
                        not (k not in listed or config.SETTINGS.get(k) != ""))
                c.check(f"{k} read from a file", not (config.get(k) != ""),
                        f"{config.get(k)!r}")
                os.environ[k] = "set"
                c.check(f"{k} must come from the environment",
                        not (config.get(k) != "set" or config.effective()[k] != ("set", "env")))
            try:
                from mop.server import playvars
                pv = playvars.playbook_vars()
            except ImportError as e:
                pv = {"import": str(e)}
            want = (set(config.SETTINGS) - set(PROCESS) - {"MOP_PROXY_KEY"}) | {
                "MOP_NODE_SCOPED", "MOP_SERVER_SCOPED", "MOP_PIP_DEPS"}
            c.check("playbook_vars keys differ", not (set(pv) != want),
                    f"extra {sorted(set(pv) - want)}, missing {sorted(want - set(pv))}")
    finally:
        config.forget()

    return c.report("config")


if __name__ == "__main__":
    sys.exit(main())
