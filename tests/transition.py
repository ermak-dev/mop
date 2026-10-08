#!/usr/bin/env python3
"""Сквозные проверки перехода конфигурации: python3 tests/transition.py

Приёмочные проверки влитых частей эпика #531 (фаза B1, #541): не новые
свойства, а СКВОЗНЫЕ цепочки, которые юнит-проверки каждой части видят
по отдельности и теряют на стыках. До этого файла переход проверялся
по кускам: snapshot в connection.py, диагностика в diagnose.py -- а
цепочка «легаси-пара -> вход -> ENV сессии и конфиг шины» нигде не
собиралась целиком. Каждая проверка гоняет ЦЕЛУЮ цепочку на одном
временном доме. Реестр и URL-контракт -- отдельная половина фазы
(tests/transition_registry.py, другой исполнитель): здесь их нет.

Найденный дефект -- отдельная задача: файл свойство фиксирует, не чинит.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, ROOT)

# Зависимые модули -- атрибуты модуля, заполняются load(): импорт со
# пропуском живёт В main (по образцу callout/deploy), не на уровне
# импорта файла, иначе дочерний прогон guard'а упал бы на неподменённых
# именах.
bus = creds = llm = cli_common = None


def load():
    """Импорт зависимых модулей с пропуском по образцу callout/deploy.

    bus требует nats-py; его ставит MOP_PIP_DEPS, и на теле до выката
    библиотеки может не быть. Пропуск громкий, с именем причины (#229);
    в CI (где зависимости есть) пропуску неоткуда взяться.
    -> True, либо None -- пропуск (уже напечатан)."""
    global bus, creds, llm, cli_common
    try:
        from mop.common import bus as bus_mod, creds as creds_mod, llm as llm_mod
        from mop.cli.core import _common as cli_mod
    except ImportError as e:
        hermetic.skip("the transition checks", f"{e} -- pip install nats-py "
                      "(MOP_PIP_DEPS brings it with mop server deploy)")
        return None
    bus, creds, llm, cli_common = bus_mod, creds_mod, llm_mod, cli_mod
    return True


# Два целых входа: прежняя пара и то, что пишет новый login. Порт в URL
# BOB совпадает с портом профиля -- издатель известен, /llm снимается.
ALICE = ("alice", "bus-alice", "443", "https://srv.test/llm", "key-alice")
BOB = ("bob", "bus-bob", "8443", "https://srv.test:8443/llm", "key-bob")
SERVICE_OP = ("service", "bus-service")


def put_legacy(directory, row):
    """Прежняя пара: operator.json + client.json, без connection.json."""
    creds.write_operator(directory, row[0], row[1])
    creds.write_client(directory, row[2], row[3], row[4])


def profile_of(row):
    return {"user": row[0], "password": row[1], "https_port": row[2],
            "proxy_url": row[3], "proxy_key": row[4]}


def _mop_listing():
    """Что лежит в ~/.config/mop этого прогона (дом hermetic изолирован)."""
    home = os.path.expanduser("~/.config/mop")
    try:
        return sorted(os.listdir(home))
    except FileNotFoundError:
        return None


# ── 1. легаси-пара -> решатель -> новый файл затеняет пару ───────────────
# HYPOTHESIS (#541 B1): решатель и его потребители расщепляют профиль на
# половины, и цепочка «пара читается целиком, новый вход затеняет пару,
# ENV сессии и конфигины шины видят НОВОЕ» нигде не собрана: юнит-проверки
# держат каждую часть по отдельности, стык не покрыт.
# RESULT: на влитом дереве цепочка зелёная целиком; порча ниже
# (_poisoned_profile под check_poisons_reproduce_defects) роняет именно
# этот сценарий -- доказательство ловли живёт в CI.
def check_pair_to_connection(c):
    with tempfile.TemporaryDirectory(prefix="mop-tr-pair-") as root:
        dest = os.path.join(root, "srv.test")
        put_legacy(dest, ALICE)
        kind, got = creds.snapshot(dest)
        c.expect("#541 a legacy pair still reads as one whole profile",
                 (kind, got), ("human", profile_of(ALICE)))
        # Новый вход одним файлом: та же пара половин больше не источник.
        creds.write_connection(dest, *BOB)
        kind, got = creds.snapshot(dest)
        c.expect("#541 connection.json shadows the pair",
                 (kind, got), ("human", profile_of(BOB)))
        # Потребители видят НОВОЕ, каждый через тот же снимок.
        with patched(creds, ROOT=root), patched_env(MOP_SERVER_LAN="srv.test"):
            env = cli_common.session_env()
            c.expect("#541 session_env carries the new profile's key",
                     env.get(llm.AUTH_VAR), BOB[4])
            c.expect("#541 session_env builds the origin via the URL contract",
                     env.get("ANTHROPIC_BASE_URL"), "https://srv.test:8443")
            cfg = bus.server_config("srv.test")
            c.expect("#541 the bus config carries the new profile's identity",
                     (cfg["user"], cfg["password"]), (BOB[0], BOB[1]))


# ── 2. два каталога серверов не подмешивают друг друга ──────────────────
# HYPOTHESIS (#541 B1): решатель по одному каталогу может зацепить
# соседний (общий ROOT, выбор по имени): стык выбора сервера ПО ИМЕНИ и
# снимка не покрыт юнит-проверками одного каталога.
# RESULT: зелёно; порча-подмешивание ниже роняет именно этот сценарий.
def check_two_servers(c):
    with tempfile.TemporaryDirectory(prefix="mop-tr-two-") as root:
        old = os.path.join(root, "old.test")
        new = os.path.join(root, "new.test")
        put_legacy(old, ALICE)
        creds.write_connection(new, *BOB)
        with patched(creds, ROOT=root):
            c.expect("#541 the legacy server still resolves on its own",
                     creds.snapshot(old), ("human", profile_of(ALICE)))
            c.expect("#541 the connection server still resolves on its own",
                     creds.snapshot(new), ("human", profile_of(BOB)))
            c.expect("#541 joined_dirs sees both, no cross-blending",
                     creds.joined_dirs(), ["new.test", "old.test"])
            # Потребители выбирают сервер именем, не полным путём: каждый
            # получает свою учётку и свой ключ, чужого -- нет. URL здесь
            # не сверяется: у ALICE адрес чужого authority (srv.test при
            # сервере old.test), и контракт #547 честно пропускает его
            # как есть -- свойство этого сценария -- раздельность учёток.
            for host, row in (("old.test", ALICE), ("new.test", BOB)):
                with patched_env(MOP_SERVER_LAN=host):
                    env = cli_common.session_env()
                    c.expect(f"#541 session_env on {host} carries that server's key",
                             env.get(llm.AUTH_VAR), row[4])
                    cfg = bus.server_config(host)
                    c.expect(f"#541 the bus config on {host} carries that "
                             f"server's identity",
                             (cfg["user"], cfg["password"]), (row[0], row[1]))


# ── 3. битый connection.json / половина пары -- отказ без fallback ───────
# HYPOTHESIS (#541 B1): отказ входа может молча упасть в соседний слой
# (.env, соседний сервер, легаси-пару) на стыке решателя и диагностики:
# #533 покрывал решатель, #536 -- диагностику, СТЫК «диагностика называет
# тот же формат, что отказ решателя» не покрыт.
# RESULT: зелёно; порча-fallback ниже роняет именно этот сценарий.
def check_refusals_no_fallback(c):
    from mop.client import diagnose
    with tempfile.TemporaryDirectory(prefix="mop-tr-broken-") as root:
        broken = os.path.join(root, "srv.test")
        put_legacy(broken, ALICE)
        with open(os.path.join(broken, creds.CONNECTION_FILE), "w") as f:
            f.write('{"version": 1, "bus": {}')          # битый JSON
        with patched(creds, ROOT=root), patched_env(MOP_SERVER_LAN="srv.test"):
            try:
                creds.snapshot(broken)
                c.fail("#541 a broken connection.json refuses in the resolver",
                       "no refusal")
            except creds.ProfileError as e:
                c.check("#541 the refusal names the connection profile",
                        "connection" in str(e), str(e))
                c.check("#541 the refusal suggests no fallback (.env or a next server)",
                        ".env" not in str(e), str(e))
            # Диагностика называет ТОТ ЖЕ формат по ТОМУ ЖЕ каталогу.
            try:
                diagnose.report()
                c.fail("#541 diagnose refuses on a broken connection.json", "no refusal")
            except RuntimeError as e:
                c.check("#541 diagnose names the same broken connection format",
                        "connection" in str(e), str(e))
        # Половина пары -- не вход: потребитель отказывает, не доискивается.
        half = os.path.join(root, "half.test")
        creds.write_client(half, *ALICE[2:])
        with patched(creds, ROOT=root), patched_env(MOP_SERVER_LAN="half.test"):
            try:
                cli_common.session_env()
                c.fail("#541 a lone client half refuses in session_env", "no refusal")
            except RuntimeError as e:
                c.check("#541 the half refusal names the incomplete login",
                        "incomplete" in str(e), str(e))


# ── 4. служебный operator.json рядом с человеческим входом -- конфликт ──
# HYPOTHESIS (#541 B1): конфликт machine/human (#533) может раствориться
# в потребителе шины: решатель отказал, а bus.server_config перечитал
# каталог поштучно и молча взял одну сторону. Стык не покрыт.
# RESULT: зелёно; порча «человек молча побеждает» ниже роняет именно
# этот сценарий.
def check_service_conflict(c):
    with tempfile.TemporaryDirectory(prefix="mop-tr-conflict-") as root:
        dest = os.path.join(root, "srv.test")
        creds.write_connection(dest, *BOB)
        creds.write_operator(dest, *SERVICE_OP)          # машинная запись рядом
        with patched(creds, ROOT=root):
            try:
                creds.snapshot(dest)
                c.fail("#541 a service operator next to a human profile conflicts",
                       "no refusal")
            except creds.ProfileError as e:
                c.check("#541 the conflict names both sides, none wins silently",
                        "conflict" in str(e), str(e))
            try:
                bus.server_config("srv.test")
                c.fail("#541 the conflict reaches the bus consumer", "no refusal")
            except RuntimeError as e:
                c.check("#541 the bus consumer refuses the same conflict",
                        "conflict" in str(e), str(e))


# ── 7. герметичность: дом прогона и git-конфиг чужого репозитория ────────
# Guard по образцу #530 (tests/autologin.py check_ensure_hermetic_530):
# сценарии выше работают через подменённый creds.ROOT и временные
# каталоги; guard подтверждает это (а) зелёностью в дочернем процессе,
# чей cwd -- СВЕЖИЙ временный git-репозиторий, а не настоящий клон
# (прогон в настоящем клоне и был бы той дырой, что закрыл #530), и (б)
# пробой mop.* локального конфига именно этого репозитория: git 1 --
# чисто, 0 -- утечка, прочее -- провал с причиной. Проба применима
# всегда, «неприменимо» у неё нет. Плюс (в): ~/.config/mop самого
# прогона не меняется.
def check_hermetic_guard(c, listing_before):
    tests = os.path.dirname(os.path.realpath(__file__))
    with tempfile.TemporaryDirectory(prefix="mop-tr-541-repo-") as repo:
        r = subprocess.run(["git", "init", "-q", repo], capture_output=True, text=True)
        if not c.check("#541 guard repository init", r.returncode == 0, r.stderr):
            return
        code = ("import sys\n"
                f"sys.path.insert(0, {tests!r})\n"
                "import hermetic  # noqa: F401 -- изоляция настроек (#209)\n"
                "import transition\n"
                "if transition.load() is None:\n"
                "    sys.exit(2)  # пропуск -- не доказательство\n"
                "from _lib import Checks\n"
                "cc = Checks()\n"
                "transition.main_children(cc)\n"
                "sys.exit(1 if cc.failed else 0)\n")
        run = subprocess.run([sys.executable, "-c", code], cwd=repo,
                             env={**os.environ, "PYTHONPATH": tests},
                             capture_output=True, text=True)
        c.check("#541 the scenarios stay green in a child process outside "
                "the real clone",
                run.returncode == 0, (run.stdout + run.stderr).strip()[-500:])
        leak = subprocess.run(["git", "config", "--local", "--get-regexp", "^mop\\."],
                              cwd=repo, capture_output=True, text=True)
        if leak.returncode == 0:
            c.fail("#541 the scenarios wrote mop.* into the cwd repository's config",
                   leak.stdout.strip())
        elif leak.returncode != 1:
            c.fail("#541 git config probe in the guard repository failed",
                   f"exit {leak.returncode}: {(leak.stdout + leak.stderr).strip()}")
        else:
            c.check("#541 the scenarios leave the cwd repository's config clean",
                    not leak.stdout.strip(), leak.stdout.strip())
    c.expect("#541 the run's own ~/.config/mop is unchanged by the checks",
             _mop_listing(), listing_before)


# ── доказательство ловли: порча -> парный СЦЕНАРИЙ проваливается ────────
# Порча -- временная и ЛОКАЛЬНАЯ прогону (patched откатывает её при
# выходе). Доказывается не «порча воспроизводит дефект» (это проверяло
# бы саму порчу), а «порча роняет СЦЕНАРИЙ»: под каждой порчей
# прогоняется парная check_* с отдельным счётчиком, её вывод глушится,
# и блок требует провала (cc.failed > 0). Порча бьёт по creds.snapshot --
# атрибуту модуля в момент вызова, -- а потребители (session_env,
# bus.server_config, diagnose.report) зовут именно его, так что порча
# доходит и до них. Если порча перестанет ронять сценарий -- доказательство
# провалено: у проверки не стало зубов.
def _poisoned_profile(directory):
    """Прочитать каталог ПОШТУЧНО из половин, игнорируя connection.json."""
    op = creds.operator(directory)
    joined = creds.client(directory)
    if op and joined:
        return "human", {"user": op["user"], "password": op["password"],
                         "https_port": joined["https_port"],
                         "proxy_url": joined["proxy_url"],
                         "proxy_key": joined["proxy_key"]}
    return None, None


def _runs_red(c, what, poison, check):
    """Порча snapshot + прогон парного сценария -> сценарий обязан провалиться.
    Вывод глушит общий run_command (#269: свой redirect_stdout -- диалект)."""
    cc = Checks()
    with patched(creds, snapshot=poison):
        run_command(lambda _argv: check(cc))
    c.check(what, cc.failed > 0,
            "" if cc.failed else
            "the paired check stayed green under the poison: no teeth")


def check_poisons_reproduce_defects(c):
    real = creds.snapshot
    with tempfile.TemporaryDirectory(prefix="mop-tr-poison-") as root:
        dest = os.path.join(root, "srv.test")

        # Порча 1 (сценарий 1): половины перечитаны ПОСЛЕ нового файла --
        # решатель держит старую пару при живом connection.json.
        put_legacy(dest, ALICE)
        creds.write_connection(dest, *BOB)
        _runs_red(c, "#541 poison: halves re-read past connection.json "
                     "fails scenario 1 (its teeth)",
                  lambda d: _poisoned_profile(d) if os.path.isfile(
                      os.path.join(d, creds.CONNECTION_FILE)) else real(d),
                  check_pair_to_connection)

        # Порча 2 (сценарий 3): при битом connection.json решатель молча
        # падает в пару.
        broken = os.path.join(root, "broken.test")
        put_legacy(broken, ALICE)
        with open(os.path.join(broken, creds.CONNECTION_FILE), "w") as f:
            f.write('{"version": 1, "bus": {}')

        def silent_fallback(d):
            if os.path.isfile(os.path.join(d, creds.CONNECTION_FILE)):
                try:
                    return real(d)
                except creds.ProfileError:
                    return _poisoned_profile(d)
            return real(d)

        _runs_red(c, "#541 poison: a broken file silently falls back to "
                     "the pair fails scenario 3 (its teeth)",
                  silent_fallback, check_refusals_no_fallback)

        # Порча 3 (сценарий 4): конфликт machine/human решается молча в
        # пользу человека -- машинную запись решатель не видит.
        conflict = os.path.join(root, "conflict.test")
        creds.write_connection(conflict, *BOB)
        creds.write_operator(conflict, *SERVICE_OP)

        def human_wins(d):
            if os.path.isfile(os.path.join(d, creds.CONNECTION_FILE)) and \
                    os.path.isfile(os.path.join(d, creds.OPERATOR_FILE)):
                with open(os.path.join(d, creds.CONNECTION_FILE),
                          encoding="utf-8") as f:
                    raw = json.load(f)
                return "human", {"user": raw["bus"]["user"],
                                 "password": raw["bus"]["password"],
                                 "https_port": raw["https_port"],
                                 "proxy_url": raw["proxy"]["url"],
                                 "proxy_key": raw["proxy"]["key"]}
            return real(d)

        _runs_red(c, "#541 poison: the conflict silently resolves to the "
                     "human side fails scenario 4 (its teeth)",
                  human_wins, check_service_conflict)

        # Порча 4 (сценарий 2): решатель одного каталога читает соседний --
        # каталог old.test отвечает профилем new.test.
        def blended(d):
            if os.path.basename(d) == "old.test":
                return real(os.path.join(os.path.dirname(d), "new.test"))
            return real(d)

        _runs_red(c, "#541 poison: one server's directory answers with "
                     "another's profile fails scenario 2 (its teeth)",
                  blended, check_two_servers)

        # Порчи откачены (patched вышел): решатель снова цел.
        kind, got = real(dest)
        c.expect("#541 poisons are rolled back: the resolver is whole again",
                 (kind, got.get("proxy_key")), ("human", BOB[4]))
        try:
            real(conflict)
            c.fail("#541 poisons are rolled back: the conflict refuses again",
                   "no refusal")
        except creds.ProfileError:
            c.check("#541 poisons are rolled back: the conflict refuses again",
                    True, "")


def main_children(c):
    """Прогон без guard-блока и без порчи: дочерний вызов из guard'а."""
    for check in (check_pair_to_connection, check_two_servers,
                  check_refusals_no_fallback, check_service_conflict):
        check(c)


def main():
    if load() is None:
        print("transition: ok (skipped)")
        return 0                      # пропуск уже напечатан с причиной
    listing_before = _mop_listing()
    c = Checks()
    main_children(c)
    check_hermetic_guard(c, listing_before)
    check_poisons_reproduce_defects(c)
    return c.report("transition")


if __name__ == "__main__":
    sys.exit(main())
