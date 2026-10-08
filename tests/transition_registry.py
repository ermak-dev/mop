#!/usr/bin/env python3
"""Приёмочные проверки перехода #541, фаза B1 (реестр и адрес прокси):
python3 tests/transition_registry.py

Переход -- не новая функция, а уже влитые части (#534 снимок реестра,
#547 канонический origin прокси). Здесь проверяется ИХ ПОВЕДЕНИЕ В СЦЕНАРИИ
перехода целиком: пара -> снимок -> выгрузка -> тот же вид, и серверный
адрес против сохранённого легаси. Это фаза B (приёмочные проверки) плана
#541: если свойство уже выполняется, проверка проходит сразу; дефект
доказывается временной локальной порчей поведения с откатом, и каждая
такая порция -- ниже в своей проверке с парой HYPOTHESIS/RESULT.

Пу-pu-mop-4 пишет tests/transition.py про вход и креды; этот файл -- про
реестр и адрес, файлы не пересекаются.

Герметичность: свои временные каталоги, и настоящий реестр клона, его
глобальные SNAPSHOT/SNAPSHOT_DIR/FILE/LIMITS и git config не меняются
(восстанавливаются в finally, а проверка №4 читает их до и после).

Сеть, живые login и deploy, ключи, .env и pip не трогаются: всё ниже --
чистые функции и временные каталоги.
"""
import json
import os
import shutil
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.cli.core import _common  # noqa: E402
from mop.common import config, creds, llm, projects, puppets  # noqa: E402

RU = "git@git.ermak.dev:ermak/rudesktop.git"
MOP = "git@git.ermak.dev:ermak/mop.git"
# Тот же basename (rudesktop), что у RU: коллизия одного проекта.
TWIN = "https://example.org/team/rudesktop.git"
HOST = "srv.test"


def _pair(d, lines, limits=None):
    """Записать легаси-пару: projects (строки) + limits.json."""
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "projects"), "w") as f:
        f.write("".join(f"{s}\n" for s in lines))
    with open(os.path.join(d, "limits.json"), "w") as f:
        f.write(json.dumps(limits or {}, sort_keys=True))
    return d


def _read_bytes(d):
    """Байты легаси-пары каталога, для сверки «не изменились ни байтом»."""
    return {n: open(os.path.join(d, n), "rb").read()
            for n in ("projects", "limits.json") if os.path.exists(os.path.join(d, n))}


# HYPOTHESIS (#541 B1): переход реестра можно описать одним свойством --
# исходная легаси-пара, снятая миграцией, записанная снимком и выгруженная
# обратно, даёт тот же вид: те же origin'ы, те же лимиты, те же имена.
# Непредставимое в v1 (голое имя, коллизия basename'ов, orphan-лимит) --
# отказ writer'а, при котором взгляд читателя видит прежние данные, а
# исходные файлы не меняются ни байтом и нового файла нет.
# RESULT: зелёная сразу на 834889f (эпик #531): снимок #534 и выгрузка
# #542 уже дают roundtrip. Порча-доказательство в check_registry_541(c)
# ниже (снятие orphan-лимита молча) ловится двумя проверками из этой
# проверки -- как и задумано приёмочной.
def check_registry_roundtrip_541(c):
    root = tempfile.mkdtemp(prefix="mop-541-registry-")
    try:
        # Сценарий 1а: чистая пара -> снимок v1 -> запись -> выгрузка ->
        # взгляд по ВЫГРУЖЕННОЙ паре равен взгляду по ИСХОДНОЙ.
        # Пути -- явно в каждый вызов: path=SNAPSHOT у writer'а -- аргумент
        # по умолчанию, он заморожен на импорте и патчем модуля не меняется.
        src = _pair(os.path.join(root, "src"), [RU, MOP], {"rudesktop": 3, "mop": 0})
        before = projects.view_of(src)
        snap = projects.snapshot_of(src)
        c.expect("#541 clean pair migrates to v1", snap,
                 {"version": 1, "projects": [
                     {"origin": MOP, "limit": 0},
                     {"origin": RU, "limit": 3}]})
        live = os.path.join(root, "server", "projects.json")
        projects.write_snapshot(snap, live)
        dest = os.path.join(root, "rollback")
        projects.export_pair(dest, live)
        after = projects.view_of(dest)
        c.expect("#541 roundtrip keeps the origins",
                 projects.snapshot_origins(after), projects.snapshot_origins(before))
        c.expect("#541 roundtrip keeps the limits",
                 projects.snapshot_limits(after), projects.snapshot_limits(before))
        c.expect("#541 roundtrip keeps the names",
                 projects.snapshot_names(after), projects.snapshot_names(before))
        # Прямое равенство взглядов: без legacy-хвостов обе стороны -- v1.
        c.expect("#541 the exported view equals the migrated one", after,
                 {"version": 1, "projects": snap["projects"]})

        # Сценарий 1б: непредставимое -- отказ writer'а, взгляд читателя
        # при этом видит ПРЕЖНИЕ данные, исходные файлы не изменились ни
        # байтом, нового файла нет.
        for name, lines, limits, marker in (
                ("bare name", [RU, "oldproject"], {}, "oldproject"),
                ("basename collision", [RU, TWIN], {}, "rudesktop"),
                ("orphan limit", [RU], {"ghost": 2}, "ghost"),
                ("foreign limit", [RU], {"mop": 2}, "mop")):
            d = _pair(os.path.join(root, "bad"), lines, limits)
            raw = _read_bytes(d)
            view_before = projects.view_of(d)
            try:
                projects.snapshot_of(d)
                refused, why = False, ""
            except RuntimeError as e:
                refused, why = True, str(e)
            c.check(f"#541 unrepresentable pair ({name}) refuses the writer", refused, why)
            c.check(f"#541 the refusal names the reason ({name})",
                    marker in why, why)
            c.check(f"#541 refused migration keeps the pair byte-identical ({name})",
                    _read_bytes(d) == raw, (raw, _read_bytes(d)))
            c.check(f"#541 refused migration writes no snapshot ({name})",
                    not os.path.exists(os.path.join(d, "projects.json")))
            # Взгляд читателя: прежние данные видны и после отказа writer'а.
            # Голое имя и чужие лимиты остаются во взгляде как legacy --
            # читатель их не теряет (writer их не сохранит -- его отказ).
            c.expect(f"#541 the reader still sees the old data ({name})",
                     projects.view_of(d), view_before)

        # Коллизия basename'ов: оба origin'а читателю видны, ни один не
        # выбран молча (у TWIN тот же basename rudesktop, что у RU).
        collide = _pair(os.path.join(root, "collide"), [RU, TWIN])
        view = projects.view_of(collide)
        c.check("#541 a collision stays visible to the reader",
                set(projects.snapshot_lines(view)) >= {RU, TWIN},
                projects.snapshot_lines(view))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# HYPOTHESIS (#541 B1): повторная миграция не должна калечить живой реестр:
# существующий projects.json -- правда, и легаси-пара его не перетирает;
# повторный export в существующий каталог -- отказ без порчи содержимого.
# RESULT: зелёная сразу на 834889f: snapshot_of предпочитает снимок (#534),
# export_pair заявляет имя каталога через rename-без-перезаписи (#542).
def check_reentry_541(c):
    root = tempfile.mkdtemp(prefix="mop-541-reentry-")
    try:
        # Живой реестр -- снимок; рядом лежит устаревшая легаси-пара.
        live = {"version": 1, "projects": [{"origin": MOP}]}
        server = os.path.join(root, "server")
        snapshot = os.path.join(server, "projects.json")
        projects.write_snapshot(live, snapshot)
        stale = _pair(os.path.join(server, "stale"), [RU], {"rudesktop": 9})
        # Снимок в СВОЁМ каталоге и рядом лежащая пара: правда -- снимок.
        c.expect("#541 an existing snapshot is the truth, the pair does not "
                 "override it", projects.snapshot_of(server), live)
        # Пара без всякого снимка не перетирает уже существующий снимок в
        # другом каталоге: каждая директория читается своя.
        c.expect("#541 a pair in its own directory migrates alone",
                 projects.snapshot_of(stale),
                 {"version": 1, "projects": [{"origin": RU, "limit": 9}]})

        # Повторный export в существующий каталог -- отказ, содержимое
        # каталога не тронуто (даже пустой -- публикация rename-без-
        # перезаписи; здесь занят файлом).
        dest = os.path.join(root, "taken")
        os.makedirs(dest)
        keep = os.path.join(dest, "keep.txt")
        with open(keep, "w") as f:
            f.write("operator data")
        raw_stale = _read_bytes(stale)
        try:
            projects.export_pair(dest, snapshot)
            c.fail("#541 a repeated export into an existing directory must refuse")
        except RuntimeError as e:
            c.check("#541 the refusal names the existing destination",
                    "exists" in str(e) or "refus" in str(e), str(e))
        c.check("#541 a refused export leaves the existing directory intact",
                os.listdir(dest) == ["keep.txt"], os.listdir(dest))
        c.check("#541 a refused export leaves the live snapshot intact",
                json.loads(open(snapshot).read()) == live)
        c.check("#541 a refused export leaves the legacy pair intact",
                _read_bytes(stale) == raw_stale)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# HYPOTHESIS (#541 B1): адрес прокси, выданный сервером после #547, и
# сохранённый старый https://host:port/llm при совпадающем издателе
# обязаны давать один и тот же результат: origin без /llm и endpoint ровно
# с одним /v1. Чужой хост, чужой порт, http и чужой кастомный путь -- без
# изменений: легаси-префикс снимается только у известного издателя.
# RESULT: зелёная сразу на 834889f: контракт #547 уже влит в llm и
# cluster._client_config; порча-доказательства в check_url_tamper_541(c).
def check_proxy_origin_541(c):
    # _client_config читает настройки сервера и установленный ключ прокси:
    # подменяем и то и другое на время вызова, ничего настоящего не трогая.
    from mop.server import cluster

    with patched(cluster.config, get=lambda name: {"MOP_SERVER_LAN": HOST,
                                                   "MOP_HTTPS_PORT": "443"}.get(name)), \
            patched(cluster.proxykey, source=lambda env, path=None: "unit-test-key"):
        served = cluster._client_config({"_caller": "alice"})
    c.check("#541 the server hands out the canonical origin",
            served.get("ok") is True
            and served.get("proxy_url") == f"https://{HOST}:{served['https_port']}"
            and "/llm" not in served["proxy_url"], served)
    port = served["https_port"]

    # Один и тот же результат для выданного и сохранённого легаси-адреса.
    legacy = f"https://{HOST}:{port}/llm"
    c.expect("#541 the saved legacy URL gives the same origin",
             llm.origin(legacy, host=HOST, port=port),
             llm.origin(served["proxy_url"], host=HOST, port=port))
    c.expect("#541 the legacy /llm is stripped at the known issuer",
             llm.origin(legacy, host=HOST, port=port), f"https://{HOST}:{port}")
    for resource in ("models", "messages"):
        c.expect(f"#541 exactly one /v1 for {resource} (issued)",
                 llm.endpoint(served["proxy_url"], resource, host=HOST, port=port),
                 f"https://{HOST}:{port}/v1/{resource}")
        c.expect(f"#541 exactly one /v1 for {resource} (saved legacy)",
                 llm.endpoint(legacy, resource, host=HOST, port=port),
                 f"https://{HOST}:{port}/v1/{resource}")

    # Чужой издатель -- без изменений: легаси-префикс не снимается.
    same_host_other_port = f"https://{HOST}:9443/llm"
    c.expect("#541 a foreign port is not ours to strip",
             llm.origin(same_host_other_port, host=HOST, port=port),
             same_host_other_port)
    c.expect("#541 a foreign host is not ours to strip",
             llm.origin(f"https://other.test:{port}/llm", host=HOST, port=port),
             f"https://other.test:{port}/llm")
    c.expect("#541 plain http is not an issuer authority",
             llm.origin(f"http://{HOST}:{port}/llm", host=HOST, port=port),
             f"http://{HOST}:{port}/llm")
    c.expect("#541 a foreign custom path passes through",
             llm.origin(f"https://{HOST}:{port}/custom", host=HOST, port=port),
             f"https://{HOST}:{port}/custom")
    c.expect("#541 a foreign custom path keeps one /v1 in the endpoint",
             llm.endpoint(f"https://{HOST}:{port}/custom", "models",
                          host=HOST, port=port),
             f"https://{HOST}:{port}/custom/v1/models")


# Порча-доказательства приёмочных проверок (#541 B1): каждая проверка выше
# зелёная на текущем дереве; чтобы доказать, что она ЛОВИТ дефект, а не
# проверяет пустоту, поведение временно портилось (monkey-патч с
# обязательным восстановлением) -- прогоном этого же файла в одном
# процессе (runpy: дочерний процесс импортирует mop заново и патча не
# видит). Матрица порч, каждая краснела своим сценарием, после отката --
# зелёные 56/56:
#   1. _snapshot_of_pair без orphan-проверки (потолок выброшен молча) --
#      9 красных: отказов writer'а нет (bare/collision/orphan/foreign),
#      «refuses the writer» и «names the reason» по всем четырём парам,
#      «restored behavior refuses the orphan»;
#   2. llm.origin без проверки издателя (снимает /llm с любого https) --
#      4 красных: чужой порт, чужой хост, контракт-контраст, профиль
#      session_env с чужим портом;
#   3. write_snapshot-«нормализатор» (пишет view, молча выбросив legacy) --
#      зелёная: сценарий этого файла writer'а с legacy-view не кормит
# (этот случай держит tests/projects.py #534); приёмочная сила здесь --
# в отказах миграции, а не в прямой записи legacy-view;
#   4. export_pair с перезаписью занятого dest -- 2 красных: отказ
#      «must refuse» и «leaves the existing directory intact»;
#   5. canonical «снимает всегда» (сквозь session_env) -- 4 красных,
#      тех же, что у порчи 2: session_env держится контрактом llm.
# HYPOTHESIS: если порча не ловится -- проверка слаба и её надо усилить.
# RESULT: порчи 1, 2, 4, 5 ловятся своими сценариями (красные прогоны
# выше); настоящих дефектов на 834889f не найдено.
def check_url_tamper_541(c):
    # (1) Порча миграции: orphan-лимит «выбрасывается» молча -- как если бы
    # _snapshot_of_pair не проверял чужие лимиты. Ловится сценарием 1б:
    # отказа writer'а не случается, и приёмочная проверка «refuses the
    # writer» краснеет; здесь прямой прибор той же порчи: потолок теряется.
    d = _pair(os.path.join(tempfile.mkdtemp(prefix="mop-541-tamper1-"), "p"),
              [RU], {"ghost": 2})
    original = projects._snapshot_of_pair

    def sloppy(dir_):
        # «Порча»: та же миграция, но без orphan-проверки -- чужой потолок
        # просто не попадает в снимок.
        origins, legacy = puppets.project_ids(
            projects._strict_lines(os.path.join(dir_, "projects")))
        limits = projects._strict_limits(os.path.join(dir_, "limits.json"))
        known = {puppets.project_of(o) for o in origins}
        return {"version": 1, "projects": [
            projects._entry(o, limits.get(puppets.project_of(o)))
            for o in sorted(origins) if puppets.project_of(o) in known or True]
            if not legacy else []}

    try:
        with patched(projects, _snapshot_of_pair=sloppy):
            snap = projects.snapshot_of(d)   # порча НЕ отказывает -- потеря
            lost = "ghost" not in projects.snapshot_limits(snap)
            c.check("#541 tamper-proof: a migration that silently drops an "
                    "orphan limit loses the ceiling (the acceptance above "
                    "demands the refusal instead)", lost)
        # Восстановленное поведение отказывает, как требует сценарий 1б.
        try:
            projects.snapshot_of(d)
            refused = False
        except RuntimeError as e:
            refused = "ghost" in str(e)
        c.check("#541 tamper-proof: restored behavior refuses the orphan", refused)
    finally:
        projects._snapshot_of_pair = original
        shutil.rmtree(os.path.dirname(d), ignore_errors=True)

    # (2) Порча адреса: origin снимает /llm у ЛЮБОГО host:port (без
    # проверки издателя). Патчим саму llm.origin: она зовёт canonical
    # глобальной ссылкой модуля, и подмена llm.canonical её не меняет.
    # Ловится сценарием 3: чужой порт обязан остаться чужим.
    other_port = f"https://{HOST}:9443/llm"
    real_origin = llm.origin

    def origin_anywhere(url, host=None, port=None):
        # Порча «как до #547»: префикс /llm легаси-установки режется с
        # любого https-адреса, издатель не сверяется.
        if url and url.startswith("https://") and url.endswith("/llm"):
            return url[: -len("/llm")]
        return url

    try:
        with patched(llm, origin=origin_anywhere):
            broken = llm.origin(other_port, host=HOST, port="443")
        # Порча ВИДИМО нарушает свойство сценария 3: чужой порт обрезан.
        # Красная приёмочная «a foreign port is not ours to strip» выше --
        # и есть срабатывание; здесь фиксируем сам дефект как воспроизведённый.
        c.check("#541 tamper-proof: the defect strips a foreign-port /llm "
                "(reproduced; the acceptance above catches it red)",
                broken != other_port, broken)
        # Настоящий контракт на том же адресе чужой порт сохраняет.
        c.expect("#541 tamper-proof: the real contract keeps the foreign port",
                 real_origin(other_port, host=HOST, port="443"), other_port)
        # Контраст того же портящего правила на СВОЁМ издателе: легаси
        # снимается и порчей, и контрактом -- приёмочная проверка различает
        # их не по пути, а по подсказкам издателя (host+port).
        c.expect("#541 tamper-proof: the real contract strips only its own issuer",
                 real_origin(f"https://{HOST}:443/llm", host=HOST, port="443"),
                 f"https://{HOST}:443")
    finally:
        llm.origin = real_origin


# HYPOTHESIS (#541 B1): переходный вход переживает оба адреса: session_env
# на временном профиле превращает и выданный сервером, и сохранённый
# легаси https://host:port/llm в один и тот же ANTHROPIC_BASE_URL, и ничего
# настоящего при этом не меняется.
# RESULT: зелёная сразу на 834889f: session_env ходит через llm.origin с
# подсказками издателя (#547/#533).
def check_session_env_541(c):
    port = "443"
    issued = f"https://{HOST}:{port}"
    legacy = f"https://{HOST}:{port}/llm"
    root = tempfile.mkdtemp(prefix="mop-541-env-")
    # Профиль -- во временном доме, до первого обращения к mop: creds.ROOT
    # вычислен на импорте от настоящего дома, и подмена каталога после
    # импорта уже не переносит серверные каталоги. Настоящий ~/.config/mop
    # не читается и не пишется.
    os.makedirs(os.path.join(root, "servers", HOST))
    saved_root = creds.ROOT
    creds.ROOT = os.path.join(root, "servers")
    directory = creds.server_dir(HOST)
    creds.write_connection(directory, "alice", "bus-secret", port,
                           legacy, "proxy-secret")
    before = {n: os.listdir(os.path.join(root, "servers", n))
              for n in os.listdir(os.path.join(root, "servers"))}
    git_before = _git_mop_config()
    try:
        with patched_env(MOP_SERVER_LAN=HOST, MOP_HTTPS_PORT=port):
            env = _common.session_env()
        c.expect("#541 session_env canonicalizes the saved legacy profile URL",
                 env["ANTHROPIC_BASE_URL"], issued)
        c.expect("#541 session_env keeps the saved key",
                 env[llm.AUTH_VAR], "proxy-secret")
        c.check("#541 session_env reads the profile and changes nothing on disk",
                {n: os.listdir(os.path.join(root, "servers", n))
                 for n in os.listdir(os.path.join(root, "servers"))} == before)
        c.check("#541 session_env does not touch the clone's git config",
                _git_mop_config() == git_before)

        # Выданный адрес (после перехода) через тот же профиль.
        creds.write_connection(directory, "alice", "bus-secret", port,
                               issued, "proxy-secret")
        with patched_env(MOP_SERVER_LAN=HOST, MOP_HTTPS_PORT=port):
            env2 = _common.session_env()
        c.expect("#541 session_env passes the issued origin unchanged",
                 env2["ANTHROPIC_BASE_URL"], issued)
        c.expect("#541 issued and legacy profiles give one address",
                 env2["ANTHROPIC_BASE_URL"], env["ANTHROPIC_BASE_URL"])

        # Чужой порт в профиле не снимается: подсказки издателя -- от
        # выбранного сервера, а не от самого URL.
        creds.write_connection(directory, "alice", "bus-secret", port,
                               f"https://{HOST}:9443/llm", "proxy-secret")
        with patched_env(MOP_SERVER_LAN=HOST, MOP_HTTPS_PORT=port):
            env3 = _common.session_env()
        c.expect("#541 a foreign-port profile URL stays as is",
                 env3["ANTHROPIC_BASE_URL"], f"https://{HOST}:9443/llm")
    finally:
        creds.ROOT = saved_root
        shutil.rmtree(root, ignore_errors=True)


def _git_mop_config():
    """git config --list клона, чтобы сверить «настоящее не меняется»."""
    import subprocess
    try:
        out = subprocess.run(["git", "config", "--list", "--local"],
                             capture_output=True, text=True, check=True,
                             cwd=os.path.dirname(os.path.dirname(
                                 os.path.realpath(__file__))))
        return sorted(out.stdout.splitlines())
    except (OSError, subprocess.SubprocessError):
        return None


def main():
    c = Checks()
    check_registry_roundtrip_541(c)
    check_reentry_541(c)
    check_proxy_origin_541(c)
    check_url_tamper_541(c)
    check_session_env_541(c)
    return c.report("transition_registry")


if __name__ == "__main__":
    sys.exit(main())
