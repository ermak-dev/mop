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


# Порча-доказательства приёмочных проверок (#541 B1): свойство уже
# выполнялось на дереве эпика, поэтому приёмочные зелёные сразу -- а чтобы
# они не были «проверкой пустоты», здесь каждая порча поведения (#541
# ревью: вариант (а)) запускает ПОДМЕНЁННОЕ поведение против НАСТОЯЩЕЙ
# приёмочной проверки этого файла: под порчей её прогон обязан краснеть,
# после отката -- снова зелёная. Так доказательство живёт в CI, а не в
# комментариe: «tamper caught by ...» -- это срабатывание приёмочной,
# а не осмотр самой порчи. Патч -- monkey-атрибут модуля, поэтому вызов
# порчи обязан идти через projects.snapshot_of/llm.origin (глобальную
# ссылку модуля), и прогон в одном процессе: дочерний процесс импортирует
# mop заново и патча не видит.
# Матрица порч (каждая проверена красным прогоном здесь же):
#   1. _snapshot_of_pair без orphan/legacy-проверки -- ловится
#      check_registry_roundtrip_541 (отказов writer'а нет);
#   2. llm.origin без проверки издателя -- ловится check_proxy_origin_541
#      и check_session_env_541 (чужой порт/хост обрезаны);
#   3. write_snapshot-«нормализатор» (пишет view, молча выбросив legacy) --
#      не ловится этим файлом: сценарий writer'а с legacy-view не кормит
#      (этот случай держит tests/projects.py #534), в матрицу НЕ включена;
#   4. export_pair с перезаписью занятого dest -- ловится
#      check_reentry_541 (отказ «must refuse» и нетронутый каталог);
#   5. canonical «снимает всегда» сквозь session_env -- ловится
#      check_proxy_origin_541 и check_session_env_541, как порча 2:
#      session_env держится контрактом llm.
# HYPOTHESIS: если порча не ловится -- проверка слаба и её надо усилить.
# RESULT: порчи 1, 2, 4, 5 ловятся своими приёмочными в CI; настоящих
# дефектов на 834889f не найдено.
def _runs_red_541(what, fn):
    """Прогон приёмочной fn со своим Checks() и заглушённым выводом --
    глушить умеет общий run_command из _lib (#269: свой диалект запрещён).
    -> (красный?, строки провалов). Приёмочная обязана печатать свои
    провалы; двойной отчёт не нужен, поэтому вывод заглушен, а красный
    читается по cc.failed, не по потоку."""
    from _lib import run_command
    cc = Checks()
    _, _, _ = run_command(lambda _argv: fn(cc))
    return cc.failed, cc.failures


def _tamper_541(c, what, patch, check):
    """Одна порча: под patch приёмочная check обязана краснеть, без порчи --
    снова зелёная. patch -- контекстный менеджер (patched из _lib)."""
    with patch:
        red, lines = _runs_red_541(what, check)
    c.check(f"#541 {what}: the acceptance catches the tamper red", red > 0,
            "; ".join(lines)[:300])
    red2, lines2 = _runs_red_541(what, check)
    c.check(f"#541 {what}: the acceptance is green again once restored", red2 == 0,
            "; ".join(lines2)[:300])


def check_tamper_matrix_541(c):
    """Матрица порч: каждая приёмочная ловит СВОЮ порчу красным."""
    # (1) Миграция без orphan/legacy-проверок: чужой потолок и голые имена
    # выпадают молча -- приёмочная 1б ждёт отказов writer'а.
    def sloppy_pair(dir_):
        origins, _legacy = puppets.project_ids(
            projects._strict_lines(os.path.join(dir_, "projects")))
        limits = projects._strict_limits(os.path.join(dir_, "limits.json"))
        return {"version": 1, "projects": [
            projects._entry(o, limits.get(puppets.project_of(o)))
            for o in sorted(origins)]}

    _tamper_541(c, "orphan-dropping migration",
                patched(projects, _snapshot_of_pair=sloppy_pair),
                check_registry_roundtrip_541)

    # (2) origin без проверки издателя: /llm режется с любого https-адреса.
    def origin_anywhere(url, host=None, port=None):
        if url and url.startswith("https://") and url.endswith("/llm"):
            return url[: -len("/llm")]
        return url

    for victim in (check_proxy_origin_541, check_session_env_541):
        _tamper_541(c, "issuer-blind /llm strip",
                    patched(llm, origin=origin_anywhere), victim)

    # (4) export_pair с перезаписью занятого dest: «порча» до публикации
    # расчищает имя -- приёмочная 2 ждёт отказа и нетронутого каталога.
    real_export = projects.export_pair

    def overwriting(dest, snapshot_path=None):
        try:
            return real_export(dest, snapshot_path)
        except RuntimeError:
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            return real_export(dest, snapshot_path)

    _tamper_541(c, "destination-overwriting export",
                patched(projects, export_pair=overwriting),
                check_reentry_541)


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
    # Профиль -- во временном доме; подмена каталога -- patched, как в
    # соседних файлах. Настоящий ~/.config/mop не читается и не пишется:
    # session_env ходит в каталог через creds.server_dir от creds.ROOT.
    os.makedirs(os.path.join(root, "servers", HOST))
    try:
        with patched(creds, ROOT=os.path.join(root, "servers")):
            _session_env_cases_541(c, root, port, issued, legacy)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _session_env_cases_541(c, root, port, issued, legacy):
    """Случаи session_env внутри подменённого creds.ROOT (см. выше)."""
    import subprocess

    # Проба git -- по ВРЕМЕННОМУ репозиторию, и session_env зовётся ИЗ
    # НЕГО (os.chdir в try/finally): запись в git config ушла бы в текущий
    # каталог процесса, и проба обязана смотреть туда, куда она ушла бы --
    # иначе «does not touch» проходил бы всегда (ревью #541). Клон прогона
    # не годится: копия репозитория враждебного прогона hermetic живёт без
    # .git, и проба падала бы сама по себе. Исходы различены как #530: сбой
    # пробы -- ПРОВАЛ с причиной, а не молчаливое None == None.
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    init = subprocess.run(["git", "init", "-q", repo], capture_output=True,
                          text=True)

    def git_mop_config():
        r = subprocess.run(["git", "config", "--list", "--local"],
                           capture_output=True, text=True, cwd=repo)
        if r.returncode != 0:
            return "probe-failed", (r.stdout + r.stderr).strip()[-300:]
        return "ok", sorted(r.stdout.splitlines())

    if not c.check("#541 the git probe repository initializes",
                   init.returncode == 0, init.stderr.strip()[-300:]):
        return
    keep_cwd = os.getcwd()
    os.chdir(repo)
    try:
        _session_env_runs_541(c, root, port, issued, legacy, git_mop_config)
    finally:
        os.chdir(keep_cwd)


def _session_env_runs_541(c, root, port, issued, legacy, git_mop_config):
    """Прогоны session_env из каталога временного репозитория (cwd уже там)."""
    directory = creds.server_dir(HOST)
    creds.write_connection(directory, "alice", "bus-secret", port,
                           legacy, "proxy-secret")
    before = {n: os.listdir(os.path.join(root, "servers", n))
              for n in os.listdir(os.path.join(root, "servers"))}
    kind, git_before = git_mop_config()
    if not c.check("#541 the git config probe of the scenario repository works",
                   kind == "ok", git_before):
        return
    with patched_env(MOP_SERVER_LAN=HOST, MOP_HTTPS_PORT=port):
        env = _common.session_env()
    c.expect("#541 session_env canonicalizes the saved legacy profile URL",
             env["ANTHROPIC_BASE_URL"], issued)
    c.expect("#541 session_env keeps the saved key",
             env[llm.AUTH_VAR], "proxy-secret")
    c.check("#541 session_env reads the profile and changes nothing on disk",
            {n: os.listdir(os.path.join(root, "servers", n))
             for n in os.listdir(os.path.join(root, "servers"))} == before)
    kind, git_after = git_mop_config()
    c.check("#541 session_env does not touch the repository's git config",
            kind == "ok" and git_after == git_before,
            f"probe {kind}: {git_after}")

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


def main():
    c = Checks()
    check_registry_roundtrip_541(c)
    check_reentry_541(c)
    check_proxy_origin_541(c)
    check_session_env_541(c)
    check_tamper_matrix_541(c)
    return c.report("transition_registry")


if __name__ == "__main__":
    sys.exit(main())
