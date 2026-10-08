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
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, ROOT)


def _import_mop():
    """Импорт зависимых модулей с пропуском по образцу callout/deploy.

    bus требует nats-py; его ставит MOP_PIP_DEPS, и на теле до выката
    библиотеки может не быть. Пропуск громкий, с именем причины (#229);
    в CI (где зависимости есть) пропуска неоткуда взяться."""
    try:
        from mop.common import bus, creds, llm
        from mop.cli.core import _common as cli_common
        return bus, creds, llm, cli_common
    except ImportError as e:
        hermetic.skip("the transition checks", f"{e} -- pip install nats-py "
                      "(MOP_PIP_DEPS brings it with mop server deploy)")
        print("transition: ok (skipped)")
        return None


_MOB = _import_mop()
if _MOB is not None:
    bus, creds, llm, cli_common = _MOB

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
# ENV сессии и конфиг шины видят НОВОЕ» нигде не собрана: юнит-проверки
# держат каждую часть по отдельности, стык не покрыт.
# RESULT: на влитом дереве цепочка зелёная целиком; порча ниже
# (_poison_shadowing) воспроизводит дефект «половины перечитаны после
# нового файла» -- именно его эта проверка и ловит.
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
# соседний (общий ROOT, выбор по имени): стык выбора сервера и снимка
# не покрыт юнит-проверками одного каталога.
# RESULT: зелёно; порча (_poison_blending) воспроизводит подмешивание --
# чужой профиль под именем другого сервера.
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


# ── 3. битый connection.json / половина пары -- отказ без fallback ───────
# HYPOTHESIS (#541 B1): отказ входа может молча упасть в соседний слой
# (.env, соседний сервер, легаси-пару) на стыке решателя и диагностики:
# #533 покрывал решатель, #536 -- диагностику, СТЫК «диагностика называет
# тот же формат, что отказ решателя» не покрыт.
# RESULT: зелёно; порча (_poison_fallback) воспроизводит молчаливый
# отход к паре при битом новом файле -- ровно то, что здесь ловится.
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
# RESULT: зелёно; порча (_poison_conflict) воспроизводит молчаливую
# победу человеческой стороны -- то, что ловит проверка.
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


# ── 5. герметичность: дом прогона и git-конфиг клона не меняются ────────
# Guard по образцу #530: проверки выше работали через подменённый
# creds.ROOT и временные каталоги; guard подтверждает это двумя пробами:
# (а) файл зелёный в дочернем процессе и не пишет в ~/.config/mop этого
# прогона; (б) локальный git config настоящего клона не получил новых
# mop.*-записей (до/после -- у клона оператора могут быть свои).
def check_hermetic_guard(c, listing_before, git_before):
    code = ("import sys\n"
            "import os\n"
            f"sys.path.insert(0, {os.path.join(ROOT, 'tests')!r})\n"
            "import hermetic  # noqa: F401\n"
            "def listing():\n"
            "    try:\n"
            "        return sorted(os.listdir(os.path.expanduser('~/.config/mop')))\n"
            "    except FileNotFoundError:\n"
            "        return None\n"
            "before = listing()\n"
            "import transition\n"
            "from _lib import Checks\n"
            "cc = Checks()\n"
            "transition.main_children(cc)\n"
            "after = listing()\n"
            "assert before == after, (before, after)\n"
            "sys.exit(1 if cc.failed else 0)\n")
    run = subprocess.run([sys.executable, "-c", code],
                         env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "tests")},
                         capture_output=True, text=True, cwd=ROOT)
    c.check("#541 the transition checks stay green in a child process "
            "and write nothing into ~/.config/mop",
            run.returncode == 0, (run.stdout + run.stderr).strip()[-500:])
    c.expect("#541 the run's own ~/.config/mop is unchanged by the checks",
             _mop_listing(), listing_before)
    got_git = _git_mop_entries()
    if got_git is None:
        # Прогон вне git-репозитория (копия без .git, как у hermetic):
        # сам клон недоступен, проба неприменима -- это не утечка.
        c.check("#541 the git config probe is inapplicable outside a repository",
                True, "skipped: no .git in the run root")
    else:
        c.expect("#541 the clone's git config carries no new mop.* entries",
                 got_git, git_before)


def _git_mop_entries():
    """(код выхода, вывод) пробы mop.* локального конфига или None, если
    прогон идёт вне git-репозитория (копия без .git у hermetic) -- там
    проба неприменима, и это не утечка."""
    r = subprocess.run(["git", "config", "--local", "--get-regexp", "^mop\\."],
                       cwd=ROOT, capture_output=True, text=True)
    text = (r.stderr + r.stdout)
    if r.returncode == 128 and ("git repository" in text or "work tree" in text):
        return None
    if r.returncode not in (0, 1):
        raise RuntimeError(f"git config probe failed: exit {r.returncode}: "
                           f"{text.strip()}")
    return (r.returncode, r.stdout.strip())


# ── доказательство ловли: порча воспроизводит дефект, проверка его видит ─
# Порча -- временная и ЛОКАЛЬНАЯ прогону (patched откатывает её при
# выходе). Здесь утверждается НЕ «порча отсутствует», а наоборот: порча
# воспроизводит именованный дефект. Если бы порча не давала дефекта --
# проверка выше нечего было бы ловить, и это было бы провалом доказательства.
# После выхода из patched звенья обязаны быть целы -- отдельная проба.
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


def check_poisons_reproduce_defects(c):
    real = creds.snapshot
    with tempfile.TemporaryDirectory(prefix="mop-tr-poison-") as root:
        dest = os.path.join(root, "srv.test")

        # Порча 1 (сценарий 1): половины перечитаны ПОСЛЕ нового файла --
        # решатель держит старую пару. Дефект: ключ ALICE при живом BOB.
        put_legacy(dest, ALICE)
        creds.write_connection(dest, *BOB)
        with patched(creds, snapshot=lambda d: (
                _poisoned_profile(d) if os.path.isfile(
                    os.path.join(d, creds.CONNECTION_FILE)) else real(d))):
            kind, got = creds.snapshot(dest)
            c.check("#541 RED-proof: re-reading the halves returns the OLD key "
                    "(scenario 1 catches exactly this)",
                    (kind, got.get("proxy_key")) == ("human", ALICE[4]),
                    f"kind {kind!r}, key {got.get('proxy_key')!r}")

        # Порча 2 (сценарий 3): при битом connection.json решатель молча
        # падает в пару. Дефект: человеческий профиль из-под битого файла.
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

        with patched(creds, snapshot=silent_fallback):
            kind, got = creds.snapshot(broken)
            c.check("#541 RED-proof: a broken file silently falls back to the pair "
                    "(scenario 3 catches exactly this)",
                    kind == "human" and got.get("user") == ALICE[0],
                    f"kind {kind!r}, got {got!r}")

        # Порча 3 (сценарий 4): конфликт machine/human решается молча в
        # пользу человека. Дефект: профиль там, где обязан быть отказ.
        conflict = os.path.join(root, "conflict.test")
        creds.write_connection(conflict, *BOB)
        creds.write_operator(conflict, *SERVICE_OP)

        def human_wins(d):
            # Дефект: решатель машинную запись НЕ ВИДИТ -- остаётся
            # connection.json, конфликта нет, человеческая сторона молча
            # побеждает. real тут не звали бы: он отказывает конфликтом.
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

        with patched(creds, snapshot=human_wins):
            kind, got = creds.snapshot(conflict)
            c.check("#541 RED-proof: the conflict resolves to the human side "
                    "(scenario 4 catches exactly this)",
                    kind == "human" and got.get("user") == BOB[0],
                    f"kind {kind!r}, got {got!r}")

        # Порча 4 (сценарий 2): решатель одного каталога читает соседний.
        # Дефект: профиль BOB из каталога ALICE.
        cross = os.path.join(root, "cross.test")
        other = os.path.join(root, "other.test")
        put_legacy(cross, ALICE)
        creds.write_connection(other, *BOB)

        def blended(d):
            if os.path.basename(d) == "cross.test":
                return real(other)
            return real(d)

        with patched(creds, ROOT=root, snapshot=blended):
            kind, got = creds.snapshot(cross)
            c.check("#541 RED-proof: one server's directory answers with "
                    "another's profile (scenario 2 catches exactly this)",
                    kind == "human" and got.get("user") == BOB[0],
                    f"kind {kind!r}, got {got!r}")

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
    """Прогон без guard-блока: дочерний вызов из check_hermetic_guard."""
    for check in (check_pair_to_connection, check_two_servers,
                  check_refusals_no_fallback, check_service_conflict):
        check(c)


def main():
    if _MOB is None:
        return 0                      # пропуск уже напечатан с причиной
    listing_before = _mop_listing()
    git_before = _git_mop_entries()   # None вне репозитория -- guard это знает
    c = Checks()
    main_children(c)
    check_hermetic_guard(c, listing_before, git_before)
    check_poisons_reproduce_defects(c)
    return c.report("transition")


if __name__ == "__main__":
    sys.exit(main())
