#!/usr/bin/env python3
"""Проверка реестра проектов без кластера: python3 tests/projects.py

Реестр — единственный ответ на вопрос «какие проекты заведены»: по нему
плейбук рендерит пользователей NATS. Пока его не было, список собирался
объединением ростера, памяти контроллера и аргументов `mop server deploy`, и завод
проекта был побочным эффектом прогона (#79).

Здесь проверяется чистая часть: что реестр принимает, что отдаёт и что
теряет. Всё остальное — чтение файла и прогон ansible.

HYPOTHESIS (#79): списка проектов как сущности нет — он собирается при каждом
прогоне из ростера, памяти контроллера и аргументов, поэтому завод проекта
есть побочный эффект `mop server deploy <origin>`, а снятия нет вовсе.
SOLUTION: реестр ~/.config/mop/projects, группа `mop project add|delete|list`
и узкий прогон deploy/projects.yml; deploy реестр только читает.
STATUS: FIXED — see #79
"""
import contextlib
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import projects  # noqa: E402

RU = "git@git.ermak.dev:ermak/rudesktop.git"
MOP = "git@git.ermak.dev:ermak/mop.git"
OTHER = "https://example.org/team/rudesktop.git"


def check_add(c):
    """with_origin: что реестр принимает."""
    got, added = projects.with_origin(MOP, {RU})
    c.check(f"new origin not added: {got}, added={added}", not (got != {RU, MOP} or not added))

    # Повтор — не ошибка и не изменение: `mop project add` идемпотентен,
    # иначе повторный завод проекта читался бы как отказ.
    got, added = projects.with_origin(RU, {RU})
    c.check(f"duplicate origin changed the registry: {got}, added={added}",
            not (got != {RU} or added))

    # Голое имя отвергается: origin — единственная правда о проекте, из
    # имени его не развернуть, а deploy режет манифест именно из origin.
    # Легаси-строки в реестре живут, но завести новую такую нельзя.
    for bad in ("rudesktop", "", "   "):
        try:
            projects.with_origin(bad, set())
            refused = False
        except RuntimeError:
            refused = True
        c.check(f"bare name {bad!r} accepted as an origin", refused)


def check_delete(c):
    """without_project: что реестр теряет."""
    got, dropped = projects.without_project("rudesktop", {RU, MOP})
    c.check(f"delete by project name: {got}, dropped={dropped}",
            not (got != {MOP} or dropped != [RU]))

    # Имя проекта — basename origin'а, и оно же имя пользователя NATS.
    # Два origin'а с одним basename — один проект: снимать надо оба, иначе
    # реестр помнит проект, которого на шине уже нет.
    got, dropped = projects.without_project("rudesktop", {RU, OTHER, MOP})
    c.check(f"two origins of one project: {got}, dropped={dropped}",
            not (got != {MOP} or dropped != sorted([RU, OTHER])))

    # Легаси-строка — имя без origin'а: снимается по себе самой.
    got, dropped = projects.without_project("oldproject", {"oldproject", MOP})
    c.check(f"legacy name not dropped: {got}, dropped={dropped}",
            not (got != {MOP} or dropped != ["oldproject"]))

    # Неизвестный проект — пустой вердикт, а не молчаливое «сделано»:
    # опечатка в имени иначе читалась бы как успешное снятие.
    got, dropped = projects.without_project("nosuch", {RU})
    c.check(f"unknown project reported as dropped: {got}, dropped={dropped}",
            not (got != {RU} or dropped != []))


def check_names(c):
    """names: имена проектов для плейбука — basename'ы плюс легаси."""
    got = projects.names({RU, MOP}, {"oldproject"})
    c.check(f"names: {got}", not (got != ["mop", "oldproject", "rudesktop"]))
    # Два origin'а одного проекта дают ОДНО имя: пользователь в конфиге
    # NATS заводится по имени, и дубль в цикле плейбука — второй lookup
    # того же пароля.
    got = projects.names({RU, OTHER}, set())
    c.check(f"duplicate basenames: {got}", not (got != ["rudesktop"]))


def check_limits(c):
    """Потолок папетов проекта (#107): что принимается и как снимается.

    HYPOTHESIS: лимитов нет, проект занимает пул, пока не кончатся слоты.
    SOLUTION: лимит -- политика проекта в своём файле рядом с реестром;
    сервис кластера сверяет с ним `add`.
    STATUS: FIXED — see #107
    """
    # Число и снятие. 0 законен: «новых папетов не заводить» -- заморозка.
    for text, want in (("3", 3), ("0", 0), ("none", None)):
        try:
            got = projects.parse_limit(text)
        except ValueError as e:
            c.fail(f"parse_limit({text!r}) refused: {e}")
            continue
        c.check(f"parse_limit({text!r}) -> {got!r}, wanted {want!r}", not (got != want))
    # Мусор -- отказ, а не «без лимита»: опечатка снимала бы потолок молча.
    for bad in ("", "-1", "three", "2.5"):
        try:
            projects.parse_limit(bad)
            refused = False
        except ValueError:
            refused = True
        c.check(f"parse_limit({bad!r}) must be refused", refused)

    got = projects.with_limit({"mop": 2}, "rugent", 5)
    c.check(f"with_limit set -> {got}", not (got != {"mop": 2, "rugent": 5}))
    got = projects.with_limit({"mop": 2, "rugent": 5}, "rugent", None)
    c.check(f"with_limit clear -> {got}", not (got != {"mop": 2}))
    # Исходный словарь не трогаем: чистая функция.
    src = {"mop": 2}
    projects.with_limit(src, "mop", None)
    c.check("with_limit must not change its argument", not (src != {"mop": 2}))


def check_git_hosts(c):
    """HYPOTHESIS (#121): узел доверял ключу хоста одного MOP_GIT_HOST, и
    папет проекта с другого форжа падал на клоне: Host key verification
    failed. SOLUTION: хосты для known_hosts -- MOP_GIT_HOST плюс ssh-хосты
    origin'ов реестра. STATUS: FIXED — see #121"""
    try:
        fn = projects.git_hosts
    except AttributeError:
        c.fail("projects.git_hosts is missing")
        return
    cases = [
        # Проект на форже установки -- один хост, без дубля.
        (["git@dev.corp:rud/app.git"], "dev.corp", ["dev.corp"]),
        # Проект с чужого форжа -- его хост добавляется.
        (["git@git.ermak.dev:ermak/mop.git", "git@dev.corp:rud/app.git"],
         "dev.corp", ["dev.corp", "git.ermak.dev"]),
        # ssh:// с портом: known_hosts пишет такой хост как [host]:port.
        (["ssh://git@forge.example:2222/team/x.git"], "dev.corp",
         ["dev.corp", "[forge.example]:2222"]),
        (["ssh://git@forge.example/team/x.git"], "dev.corp",
         ["dev.corp", "forge.example"]),
        # https и локальный путь ключа хоста не требуют.
        (["https://github.com/a/b.git", "/srv/git/c.git"], "dev.corp",
         ["dev.corp"]),
        # Пустой реестр -- хост установки всё равно нужен: им клонирует сборка.
        ([], "dev.corp", ["dev.corp"]),
        # HYPOTHESIS (#166): git клонирует git+ssh:// и ssh+git:// по ssh, а
        # git_hosts их хостов не давал -- узел не знал ключа форжа, и папет
        # падал на клоне «Host key verification failed» (как #121/#141).
        # SOLUTION: схемы ssh, git+ssh, ssh+git -- все ssh. STATUS: FIXED — see #166
        (["git+ssh://git@forge.example/team/x.git"], "dev.corp",
         ["dev.corp", "forge.example"]),
        (["git+ssh://git@forge.example:2222/team/x.git"], "dev.corp",
         ["dev.corp", "[forge.example]:2222"]),
        (["ssh+git://forge.example/team/x.git"], "dev.corp",
         ["dev.corp", "forge.example"]),
        (["ssh+git://git@forge.example:22/team/x.git"], "dev.corp",
         ["dev.corp", "forge.example"]),
        (["ssh+git://git@forge.example:2222/team/x"], "dev.corp",
         ["dev.corp", "[forge.example]:2222"]),
        # https по-прежнему не ssh.
        (["https://forge.example:8443/team/x.git"], "dev.corp", ["dev.corp"]),
    ]
    for origins, default, want in cases:
        got = fn(origins, default)
        c.check(f"git_hosts({origins!r}, {default!r}) -> {got!r}, wanted {want!r}",
                not (got != want))


def check_for_deploy(c):
    """HYPOTHESIS (#117): `mop server deploy` читал реестр контроллера, а правда о
    проектах теперь -- реестр сервера, который правят глаголы сервиса.
    SOLUTION: deploy спрашивает сервер; нет ответа (чистая установка: шины
    ещё нет) -- берёт копию этой машины и говорит об этом.
    STATUS: FIXED — see #117; локальная копия -- взгляд #534, а не сырой
    легаси-файл: после миграции файлы пары на диске устаревают, и fallback
    должен видеть актуальный снимок, а не остывший projects."""
    try:
        fn = projects.for_deploy
    except AttributeError:
        c.fail("projects.for_deploy is missing")
        return
    lines, note = fn({"ok": True, "lines": ["git@h:g/mop.git", "legacy"]}, {"git@h:g/old.git"})
    c.check(f"a server answer must win silently: {lines} {note!r}",
            not (lines != {"git@h:g/mop.git", "legacy"} or note))
    lines, note = fn({"error": "no cluster service"}, {"git@h:g/old.git"})
    c.check(f"without the server the local copy is used, and said so: {lines} {note!r}",
            not (lines != {"git@h:g/old.git"} or not note or "no cluster service" not in note))
    # Пустой ответ сервера -- правда (проектов нет), а не повод взять копию.
    lines, note = fn({"ok": True, "lines": []}, {"git@h:g/old.git"})
    c.check(f"an empty server registry is the truth: {lines}", not (lines != set()))


def check_local_view_534(c):
    """Локальный fallback deploy и команда users читают взгляд, не легаси-файл.

    HYPOTHESIS (#534): после миграции реестр на диске -- projects.json, а
    легаси-файлы пары остаются лежать устаревшими. Читатели, которым
    говорено «нет ответа сервера -- возьми локальную копию» (deploy) или
    «заведи пользователей шины по реестру этой машины» (mop cluster
    users), продолжали читать projects.read() -- устаревший файл: новые
    проекты, заведённые глаголом, не получали пользователей шины, и
    прогон/команда молча откатывали реестр к до-миграционному.
    SOLUTION: оба читают projects.view_lines(view_of(SNAPSHOT_DIR)) --
    тот же взгляд, что и глагол-список сервиса: старые данные видимы,
    актуальный снимок -- источник.
    STATUS: #534 (красный прогон впереди реализации)."""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="mop-test-534l-")
    try:
        # После миграции на диске только снимок: взгляд читает его, а не
        # отсутствующий легаси-файл.
        import json as _json
        with open(os.path.join(d, "projects.json"), "w") as f:
            f.write(_json.dumps({"version": 1, "projects": [
                {"origin": MOP, "limit": 2}, {"origin": RU}]}))
        c.expect("#534 the local view of a migrated registry sees the snapshot",
                 sorted(projects.view_lines(projects.view_of(d))),
                 sorted([MOP, RU]))
        # Форма списка -- прежняя (строки для плейбука, без структур).
        c.check("#534 the local view yields plain lines",
                all(isinstance(l, str) for l in projects.view_lines(projects.view_of(d))),
                "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_read_limits_337(c):
    """HYPOTHESIS (#337): read_limits зовёт int() на каждом значении и ловит
    OSError, ValueError, AttributeError, но не TypeError: `null`, список или
    объект в файле лимитов роняют глагол сервиса (add, снимок, `mop project
    limit`) исключением -- одна плохая строка кладёт add всего пула.
    SOLUTION: по записи, не по файлу -- значение, которое не целое (null,
    список, объект, bool, нечисловая строка), пропускается, потолки
    остальных проектов действуют: потолок защищает, и одна строка не должна
    снимать все. STATUS: FIXED — see #337"""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="mop-test-limits-337-")
    path = os.path.join(d, "limits.json")
    try:
        c.expect("#337 a missing limits file reads as {}", projects.read_limits(path), {})
        for text, want in (('{"a": null, "b": 3}', {"b": 3}),
                           ('{"a": [], "b": {}}', {}),
                           ('{"a": true}', {}),
                           ('{"a": "x", "b": 2}', {"b": 2}),
                           ('{"a": "5"}', {"a": 5}),
                           ('{"a": 5}', {"a": 5}),
                           ('{"a": Infinity, "b": 1}', {"b": 1}),
                           ('[1, 2]', {}),
                           ('{broken', {})):
            with open(path, "w") as f:
                f.write(text)
            try:
                got = projects.read_limits(path)
            except Exception as e:
                got = f"raised {type(e).__name__}: {e}"
            c.expect(f"#337 read_limits({text})", got, want)
    finally:
        shutil.rmtree(d)


# ─── атомарный реестр #534 ───────────────────────────────────────────────
def _refused534(c, what, fn, *needles):
    """Утверждение: fn() отказывает RuntimeError с причиной (без потерь).

    Локальный диалект: в _lib.Checks нет refused, а инфраструктура проверок
    -- вне границ #534."""
    import traceback
    try:
        fn()
        got = None
    except RuntimeError as e:
        got = str(e)
        traceback.clear_frames(e.__traceback__)
        del e
    except Exception as e:  # noqa: BLE001 -- ровно этот провал и ловим
        got = f"WRONG EXC {type(e).__name__}: {e}"
    ok = got is not None and all(n in got for n in needles) if needles else got is not None
    c.check(what, bool(ok), f"refusal was {got!r}")


# HYPOTHESIS (#534): origin'ы (projects) и лимиты (limits.json) лежат в двух
# файлах, и каждый мутирующий глагол сервиса пишет их двумя отдельными
# атомарными записями: обрыв или падение между ними разводит данные --
# проект снят, а потолок остался (или наоборот), и повторный завод читался
# бы чужим прошлым. Ниже -- проверка чистой половины атомарного реестра
# ~/.config/mop/projects.json {version:1, projects:[{origin, limit?}]}:
# миграция пары legacy-файлов в один снимок, разбор/отказы схемы, снимок
# одного поколения и обратное преобразование для даунгрейда. Глагольная
# половина (один writer, один снимок на операцию) -- в tests/cluster.py.
def _reg53x(d):
    """Каталог с legacy-парой реестра во временном доме проверки."""
    import json as _json
    os.makedirs(d, exist_ok=True)
    return d


def _legacy_pair(d, lines, limits=None):
    """Записать legacy-пару: projects (строки) + limits.json."""
    import json as _json
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "projects"), "w") as f:
        f.write("".join(f"{s}\n" for s in lines))
    _write_limits53x(d, limits or {})


def _write_limits53x(d, limits):
    import json as _json
    with open(os.path.join(d, "limits.json"), "w") as f:
        f.write(_json.dumps(limits, sort_keys=True))


def check_migration_534(c):
    """Миграция legacy-пары в атомарный снимок. Чистая функция от пары.

    Отказ миграции -- громкий, с конкретной причиной и без потерь: старые
    файлы остаются как были, нового файла нет, ничего не выбрасывается.
    Голое имя, коллизия basename'ов и orphan/битый лимит непредставимы в
    v1 без выдумывания origin'а -- это отказ оператору, а не молчаливая
    потеря строки. STATUS: #534 (реализация следует за красным прогоном)"""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="mop-test-534-")
    try:
        # Чистая пара: снимок v1 с лимитами по месту, один проект на запись.
        _legacy_pair(d, [MOP, RU], {"mop": 3, "rudesktop": 0})
        got = projects.snapshot_of(d)
        c.expect("#534 migrate a clean legacy pair", got,
                 {"version": 1, "projects": [
                     {"origin": MOP, "limit": 3},
                     {"origin": RU, "limit": 0}]})

        # Нет лимита -- ключа нет (не None: None и 0 -- разные вещи).
        _legacy_pair(d, [MOP])
        got = projects.snapshot_of(d)
        c.expect("#534 no limit means no key", got,
                 {"version": 1, "projects": [{"origin": MOP}]})

        # Пустой реестр -- валидный пустой снимок, не отказ.
        _legacy_pair(d, [])
        c.expect("#534 an empty legacy pair migrates to an empty v1 snapshot",
                 projects.snapshot_of(d), {"version": 1, "projects": []})

        # Нет ни пары, ни снимка -- свежая установка: реестр честно пуст
        # (это поддерживаемое состояние, а не «неизвестно»), и читатель
        # ничего не создаёт. Первый успешный add создаст projects.json.
        shutil.rmtree(d)
        os.makedirs(d)
        c.expect("#534 a fresh install reads as an empty registry",
                 projects.snapshot_of(d), {"version": 1, "projects": []})
        c.check("#534 reading a fresh install writes nothing",
                not os.path.exists(os.path.join(d, "projects.json")))

        # Голое имя непредставимо в v1 (origin выдумывать нельзя): отказ
        # с именем, legacy-файл не трогаем, нового файла нет.
        shutil.rmtree(d); d = tempfile.mkdtemp(prefix="mop-test-534-")
        _legacy_pair(d, [MOP, "oldproject"])
        _refused534(c, "#534 a bare legacy name must refuse with the name",
                  lambda: projects.snapshot_of(d), "oldproject")
        c.check("#534 a refused migration leaves the legacy registry untouched",
                projects.read(os.path.join(d, "projects")) == {MOP, "oldproject"})
        c.check("#534 a refused migration writes no new file",
                not os.path.exists(os.path.join(d, "projects.json")))

        # Два origin'а одного проекта (один basename): в v1 -- по одной
        # записи на проект, задваивать лимит некуда; без консенсуса,
        # какой origin правда, мигрировать нельзя -- отказ. Origin в
        # реестре тоже может нести креды (userinfo, token): отказ называет
        # имя проекта и число конфликтов и НЕ печатает сырые origin'ы --
        # ни из запроса, ни с диска.
        shutil.rmtree(d); d = tempfile.mkdtemp(prefix="mop-test-534-")
        _legacy_pair(d, [RU, OTHER])  # оба -- rudesktop
        try:
            projects.snapshot_of(d)
            got = None
        except RuntimeError as e:
            got = str(e)
        c.check("#534 a colliding pair refuses with the project name",
                bool(got and "rudesktop" in got), repr(got))
        c.check("#534 the collision refusal counts the conflicts",
                bool(got and "2" in got), repr(got))
        c.check("#534 the collision refusal leaks no raw origin",
                bool(got and RU not in got and OTHER not in got), repr(got))
        # Тот же запрет для origin'а с кредами в записи реестра: голого
        # имени достаточно, чтобы найти проблему; секрет не перепечатывается.
        credentialed = "https://user:secret-token@forge.example/team/rudesktop.git"
        shutil.rmtree(d); d = tempfile.mkdtemp(prefix="mop-test-534-")
        _legacy_pair(d, [RU, credentialed])
        try:
            projects.snapshot_of(d)
            got = None
        except RuntimeError as e:
            got = str(e)
        c.check("#534 a credential-bearing origin must not leak into the refusal",
                bool(got and "secret-token" not in got and "user:" not in got
                     and RU not in got and credentialed not in got), repr(got))

        # Orphan-лимит (имени нет в реестре) и нецелое значение: отказ с
        # именем, а не молчаливая утрата потолка. Непустые limits без
        # projects вовсе -- тот же отказ: потолки без реестра не
        # обнуляются молча.
        shutil.rmtree(d); d = tempfile.mkdtemp(prefix="mop-test-534-")
        _legacy_pair(d, [MOP], {"ghost": 2})
        _refused534(c, "#534 an orphan limit must refuse with the name",
                  lambda: projects.snapshot_of(d), "ghost")
        shutil.rmtree(d); d = tempfile.mkdtemp(prefix="mop-test-534-")
        _legacy_pair(d, [], {"ghost": 2})
        _refused534(c, "#534 non-empty limits without projects must refuse",
                  lambda: projects.snapshot_of(d), "ghost")
        shutil.rmtree(d); d = tempfile.mkdtemp(prefix="mop-test-534-")
        _legacy_pair(d, [MOP], {"mop": 2, "rudesktop": 1})
        _refused534(c, "#534 a limit of an unknown project must refuse with the name",
                  lambda: projects.snapshot_of(d), "rudesktop")

        # Strict-миграция не читает пару прощающим читателем (#534):
        # read_limits молча отбрасывает null/bool/дробные/битые значения
        # и приводит через int() -- strict потерял бы потолок ДО
        # валидации, и мутация сохранила бы усечённое состояние. Невалидная
        # запись -- отказ с именем и значением, файлы не тронуты.
        for name, value in (("null", "null"), ("true", "true"), ("float", "1.5"),
                            ("string", '"junk"'), ("negative", "-1")):
            shutil.rmtree(d); d = tempfile.mkdtemp(prefix="mop-test-534-")
            _legacy_pair(d, [MOP], {"mop": 2})
            with open(os.path.join(d, "limits.json"), "w") as f:
                f.write('{"mop": 2, "p": %s}' % value)
            _refused534(c, f"#534 a non-integer limit ({name}) must refuse the migration",
                      lambda: projects.snapshot_of(d), "p")
            c.check(f"#534 a refused non-integer limit ({name}) keeps the file bytes",
                    open(os.path.join(d, "limits.json")).read()
                    == '{"mop": 2, "p": %s}' % value)

        # IO-отказ не становится пустым реестром: read() глотает OSError,
        # и strict-миграция недоступного файла увидела бы «нет проектов».
        # Для writer'а это громкий отказ с причиной от системы.
        shutil.rmtree(d); d = tempfile.mkdtemp(prefix="mop-test-534-")
        _legacy_pair(d, [MOP], {"mop": 2})
        os.chmod(os.path.join(d, "projects"), 0)
        _refused534(c, "#534 an unreadable registry file must refuse loudly",
                  lambda: projects.snapshot_of(d))
        os.chmod(os.path.join(d, "projects"), 0o644)

        # Уже мигрированное: повторная миграция того же снимка -- тот же
        # результат, файлов-легаси больше не читаем.
        shutil.rmtree(d); d = tempfile.mkdtemp(prefix="mop-test-534-")
        _legacy_pair(d, [MOP], {"mop": 3})
        first = projects.snapshot_of(d)
        os.remove(os.path.join(d, "projects"))
        os.remove(os.path.join(d, "limits.json"))
        import json as _json
        with open(os.path.join(d, "projects.json"), "w") as f:
            f.write(_json.dumps(first))
        c.expect("#534 an existing v1 snapshot is read back as itself",
                 projects.snapshot_of(d), first)

        # Битый/чужой projects.json: громкий отказ без пустого реестра и
        # без fallback на legacy-файлы (fallback маскировал бы поломку).
        for name, text in (("broken json", "{broken"),
                           ("a newer schema", '{"version": 2, "projects": []}'),
                           ("not an object", "[1]")):
            with open(os.path.join(d, "projects.json"), "w") as f:
                f.write(text)
            _refused534(c, f"#534 a malformed snapshot ({name}) must refuse loudly",
                      lambda: projects.snapshot_of(d))
        os.remove(os.path.join(d, "projects.json"))

        # Отказ миграции не создаёт файл: пара с голым именем осталась
        # парой, файла нет (проверено выше), и повтор -- тот же отказ.
        d2 = tempfile.mkdtemp(prefix="mop-test-534-")
        try:
            _legacy_pair(d2, [RU, "oldproject"])
            for _ in range(2):
                _refused534(c, "#534 repeat migration of a bad pair refuses the same way",
                          lambda: projects.snapshot_of(d2))
                c.check("#534 no snapshot appears after a refusal",
                        not os.path.exists(os.path.join(d2, "projects.json")))
        finally:
            shutil.rmtree(d2)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_snapshot_534(c):
    """Снимок v1: разбор, ограничения схемы и одно поколение на операцию.

    STATUS: #534"""
    import json
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="mop-test-534-")
    try:
        path = os.path.join(d, "projects.json")
        # Полный снимок читается целиком: origins и limits из одного
        # поколения -- раздельное чтение двух файлов и разводило данные.
        with open(path, "w") as f:
            f.write(json.dumps({"version": 1, "projects": [
                {"origin": MOP, "limit": 3}, {"origin": RU}]}))
        snap = projects.snapshot_of(d)
        c.expect("#534 origins and limits of one snapshot", projects.snapshot_origins(snap), [MOP, RU])
        c.expect("#534 limits of one snapshot", projects.snapshot_limits(snap),
                 {"mop": 3})

        # Мутация над снимком: добавление/снятие/лимит не трогают исходник.
        got = projects.snapshot_with_origin(snap, "git@h:team/new.git")
        c.expect("#534 snapshot_with_origin adds one project",
                 projects.snapshot_origins(got), [MOP, RU, "git@h:team/new.git"])
        c.check("#534 snapshot_with_origin leaves the source untouched",
                projects.snapshot_origins(snap) == [MOP, RU])

        got = projects.snapshot_without(snap, "mop")
        c.expect("#534 snapshot_without drops the project and its limit",
                 (projects.snapshot_origins(got), projects.snapshot_limits(got)),
                 ([RU], {}))

        got = projects.snapshot_with_limit(snap, "rudesktop", 0)
        c.expect("#534 snapshot_with_limit sets a zero freeze",
                 projects.snapshot_limits(got), {"mop": 3, "rudesktop": 0})
        got = projects.snapshot_with_limit(snap, "mop", None)
        c.expect("#534 snapshot_with_limit lifts a limit",
                 projects.snapshot_limits(got), {})

        # Чистые функции снимка не пишут на диск и не падают на битом
        # аргументе -- запись ведёт writer сервиса, и только он.
        _refused534(c, "#534 a bare name is not an origin in v1 either",
                  lambda: projects.snapshot_with_origin(snap, "bare"))
        c.check("#534 no file is written by pure snapshot functions",
                not os.path.exists(os.path.join(d, "new.json")))

        # Обратное преобразование актуального v1 снимка в legacy-пару
        # (#534, даунгрейд после остановки writers): чистая функция, без
        # записи на диск, roundtrip-стабильна после новых мутаций.
        v1 = {"version": 1, "projects": [
            {"origin": MOP, "limit": 3}, {"origin": RU}]}
        lines, limits = projects.legacy_pair_of(v1)
        c.expect("#534 export to a legacy registry file", lines, [MOP, RU])
        c.expect("#534 export to legacy limits", limits, {"mop": 3})
        # Roundtrip: выгрузил -> смигрировал обратно -- тот же снимок.
        c.expect("#534 legacy export roundtrips back to the same v1 snapshot",
                 projects.snapshot_of(_pair_dir(d, lines, limits)), v1)
        # После новых мутаций (добавили проект, сняли другой) экспорт --
        # по-прежнему пара без потерь.
        v1b = projects.snapshot_with_origin(
            projects.snapshot_without(v1, "mop"), "git@h:team/new.git")
        lines, limits = projects.legacy_pair_of(v1b)
        c.expect("#534 export after new mutations roundtrips",
                 projects.snapshot_of(_pair_dir(d, lines, limits)), v1b)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _pair_dir(d, lines, limits):
    """Каталог с записанной legacy-парой (для roundtrip)."""
    _legacy_pair(os.path.join(d, "pair"), sorted(lines), limits)
    return os.path.join(d, "pair")


def _migrate_refusal(dir_):
    """Текст отказа миграции пары в каталоге (или None, если не отказала)."""
    try:
        projects.snapshot_of(dir_)
        return None
    except RuntimeError as e:
        return str(e)


def _refusal_of(fn):
    """Текст отказа fn (или None, если не отказала; не-RuntimeError -- маркер)."""
    try:
        fn()
        return None
    except RuntimeError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001 -- ровно этот провал и ловим
        return f"WRONG EXC {type(e).__name__}: {e}"


def check_export_542(c):
    """Оффлайн-экспорт реестра для отката: проверяемая легаси-пара из v1.

    HYPOTHESIS (#542): после миграции старые projects/limits.json остаются
    на диске устаревшими, и git revert снимка не сохраняет add/delete/limit,
    сделанные после него. Обратного экспорта нет: снимать пару руками --
    небезопасно (origin'ы несут креды, порядок файлов не атомарен).
    SOLUTION: чистая функция export_pair -- читает projects.json ОДИН раз
    (байты -> digest -> валидатор #534), раскладывает легаси-пару в НОВЫЙ
    каталог с заявкой имени через mkdir (EEXIST -- отказ без overwrite),
    файлы 0600/каталог 0700, маркер export.json последним. Живой реестр
    не тронут; провал чистит только созданное этой попыткой.
    STATUS: #542 (красный прогон впереди реализации)."""
    import hashlib
    import json as _json
    import shutil
    import tempfile
    v1 = {"version": 1, "projects": [
        {"origin": MOP, "limit": 2}, {"origin": RU}]}

    def seed(root):
        projects.SNAPSHOT = os.path.join(root, "src", "projects.json")
        projects.SNAPSHOT_DIR = os.path.join(root, "src")
        projects.write_snapshot(v1, projects.SNAPSHOT)
        return projects.SNAPSHOT

    root = tempfile.mkdtemp(prefix="mop-test-542-")
    saved_snapshot, saved_dir = projects.SNAPSHOT, projects.SNAPSHOT_DIR
    try:
        src = seed(root)
        raw = open(src, "rb").read()
        digest = hashlib.sha256(raw).hexdigest()
        dest = os.path.join(root, "rollback")

        out = projects.export_pair(dest)
        c.check("#542 export answers with a summary, not a raw snapshot",
                isinstance(out, dict) and out.get("projects") == 2, out)
        c.check("#542 export names the source and its digest",
                out.get("source") == src and out.get("sha256") == digest, out)
        c.check("#542 the destination is a private directory",
                oct(os.stat(dest).st_mode & 0o777) == "0o700", oct(os.stat(dest).st_mode))
        for name in ("projects", "limits.json", "export.json"):
            c.check(f"#542 the exported {name} is 0600",
                    oct(os.stat(os.path.join(dest, name)).st_mode & 0o777) == "0o600", name)
        c.expect("#542 the exported lines are the legacy registry",
                 sorted(open(os.path.join(dest, "projects")).read().splitlines()),
                 sorted([MOP, RU]))
        c.expect("#542 the exported limits are the legacy limits",
                 _json.load(open(os.path.join(dest, "limits.json"))), {"mop": 2})
        marker = _json.load(open(os.path.join(dest, "export.json")))
        c.check("#542 the marker carries the digest and the schema, not origins",
                marker.get("snapshot_sha256") == digest
                and marker.get("schema") == "legacy-pair"
                and MOP not in _json.dumps(marker), marker)
        c.check("#542 the marker is newer than the pair it certifies",
                os.path.getmtime(os.path.join(dest, "export.json"))
                >= max(os.path.getmtime(os.path.join(dest, "projects")),
                       os.path.getmtime(os.path.join(dest, "limits.json"))))

        # Roundtrip: экспортированная пара мигрирует обратно в ТОТ ЖЕ v1.
        c.expect("#542 the exported pair migrates back to the same v1",
                 projects.snapshot_of(dest), v1)

        # Roundtrip после новых мутаций: export снимает актуальное
        # состояние, а не заголовок миграции.
        v1b = projects.snapshot_with_origin(
            projects.snapshot_without(v1, "mop"), "git@h:team/new.git")
        projects.write_snapshot(v1b, src)
        raw_b = open(src, "rb").read()
        dest2 = os.path.join(root, "rollback2")
        projects.export_pair(dest2)
        c.expect("#542 export after new mutations roundtrips",
                 projects.snapshot_of(dest2), v1b)

        # Живой реестр не тронут: байты source не изменились ЭТИМ экспортом
        # (слепок -- после перезаписи v1b, сравнение -- после export_pair).
        c.check("#542 export leaves the source bytes alone",
                open(src, "rb").read() == raw_b)

        # Отказы: dest уже есть (файл, каталог, symlink) -- без overwrite.
        busy = os.path.join(root, "busy")
        open(busy, "w").write("x")
        _refused542(c, "#542 an existing file destination refuses",
                 lambda: projects.export_pair(busy))
        c.check("#542 the refused file destination keeps its bytes",
                open(busy).read() == "x")
        made = os.path.join(root, "made")
        os.mkdir(made)
        _refused542(c, "#542 an existing directory destination refuses",
                 lambda: projects.export_pair(made))
        link = os.path.join(root, "link")
        os.symlink(root, link)
        _refused542(c, "#542 a symlink destination refuses",
                 lambda: projects.export_pair(link))
        c.check("#542 a refused symlink leaves its target alone",
                sorted(os.listdir(link)) == sorted(os.listdir(root)))

        # Race: параллельный mkdir успел раньше -- отказ, чужой каталог цел.
        raced = os.path.join(root, "raced")

        def racer():
            os.mkdir(raced, 0o700)
            with open(os.path.join(raced, "foreign"), "w") as f:
                f.write("not ours")

        import threading
        t = threading.Thread(target=racer)
        t.start()
        t.join()
        _refused542(c, "#542 a concurrently created destination refuses",
                 lambda: projects.export_pair(raced))
        c.check("#542 the concurrent directory is not cleaned up",
                os.path.exists(os.path.join(raced, "foreign")))

        # Нет v1 -- экспортировать нечего: пара НЕ fallback.
        empty_root = tempfile.mkdtemp(prefix="mop-test-542-e-")
        try:
            projects.SNAPSHOT = os.path.join(empty_root, "projects.json")
            projects.SNAPSHOT_DIR = empty_root
            _refused542(c, "#542 no snapshot to export refuses, no pair fallback",
                     lambda: projects.export_pair(os.path.join(root, "none")))
            c.check("#542 the refused no-snapshot export creates nothing",
                    not os.path.exists(os.path.join(root, "none")))
        finally:
            shutil.rmtree(empty_root)

        # Битый v1 (неизвестная версия, мусор) -- отказ валидатора #534.
        bad_root = tempfile.mkdtemp(prefix="mop-test-542-b-")
        try:
            projects.SNAPSHOT = os.path.join(bad_root, "projects.json")
            projects.SNAPSHOT_DIR = bad_root
            for text in ('{"version": 2, "projects": []}', "{broken"):
                with open(projects.SNAPSHOT, "w") as f:
                    f.write(text)
                _refused542(c, "#542 a bad snapshot refuses the export",
                         lambda: projects.export_pair(os.path.join(root, "bad")))
                c.check("#542 the refused bad-snapshot export creates nothing",
                        not os.path.exists(os.path.join(root, "bad")))
        finally:
            shutil.rmtree(bad_root)

        # Провал записи внутри экспорта: чистится ТОЛЬКО созданное этой
        # попыткой; parent и source живы.
        flaky_root = tempfile.mkdtemp(prefix="mop-test-542-f-")
        try:
            src2 = seed(flaky_root)
            from mop.common import fsutil
            real_wc = projects._write_claimed

            def dying_write(dirfd, name, data, *a, **k):
                if name == "limits.json":
                    raise OSError("disk full")
                return real_wc(dirfd, name, data, *a, **k)

            dest3 = os.path.join(flaky_root, "rollback")
            with _patched(projects, _write_claimed=dying_write):
                _refused542(c, "#542 a failed write refuses the export",
                         lambda: projects.export_pair(dest3))
            c.check("#542 the failed export cleans up its own directory",
                    not os.path.exists(dest3), dest3)
            c.check("#542 the failed export leaves the source alive",
                    open(src2, "rb").read()[:1] == b"{")
        finally:
            shutil.rmtree(flaky_root)

        # Inode-якорь: между провалом записи и cleanup кто-то ПОДМЕНИЛ наш
        # каталог (rmtree+smlink) -- cleanup обязан заметить и НЕ удалять
        # чужое; отказ называет, что каталог больше не наш.
        swap_root = tempfile.mkdtemp(prefix="mop-test-542-i-")
        try:
            src3 = seed(swap_root)
            real_wc = projects._write_claimed

            def swap_write(dirfd, name, data, *a, **k):
                if name == "limits.json":
                    # между записями каталог подменили (rmtree + symlink)
                    shutil.rmtree(victim_dir)
                    os.symlink(os.path.join(swap_root, "src"), victim_dir)
                    raise OSError("disk full")
                return real_wc(dirfd, name, data, *a, **k)

            victim_dir = os.path.join(swap_root, "rollback")
            with _patched(projects, _write_claimed=swap_write):
                got = None
                try:
                    projects.export_pair(victim_dir)
                except RuntimeError as e:
                    got = str(e)
                except OSError as e:
                    got = f"WRONG EXC OSError: {e}"
                c.check("#542 a replaced directory is not removed by cleanup",
                        bool(got and "ours" in got and os.path.islink(victim_dir)), got)
                c.check("#542 the replaced directory keeps its target alive",
                        os.path.exists(os.path.join(swap_root, "src", "projects.json")))
        finally:
            shutil.rmtree(swap_root, ignore_errors=True)

        # Фолты на каждом файле (projects, limits.json, export.json):
        # экспорт отказывает, своего каталога не остаётся (marker-провал
        # чистит созданное -- публикация не состоялась).
        for victim in ("projects", "limits.json", "export.json"):
            fault_root = tempfile.mkdtemp(prefix="mop-test-542-w-")
            try:
                seed(fault_root)
                real_wc = projects._write_claimed

                def dying_write(dirfd, name, data, *a, _v=victim, **k):
                    if name == _v:
                        raise OSError(f"disk died on {_v}")
                    return real_wc(dirfd, name, data, *a, **k)

                fdest = os.path.join(fault_root, "rollback")
                with _patched(projects, _write_claimed=dying_write):
                    _refused542(c, f"#542 a write fault on {victim} refuses the export",
                             lambda: projects.export_pair(fdest), "disk died")
                c.check(f"#542 the {victim} fault cleans its own directory",
                        not os.path.exists(fdest), fdest)
            finally:
                shutil.rmtree(fault_root, ignore_errors=True)

        # Маркер несёт дайджесты файлов и счётчик; сводка -- машиначитаемая,
        # без URL: перекладываем уже сделанные проверки сюда после зелёного.
        marker2 = _json.load(open(os.path.join(dest2, "export.json")))
        for name in ("projects", "limits.json"):
            body = open(os.path.join(dest2, name), "rb").read()
            c.check(f"#542 the marker carries the {name} digest",
                    marker2.get("files", {}).get(name)
                    == hashlib.sha256(body).hexdigest(), marker2.get("files"))
        c.check("#542 the marker counts the projects",
                marker2.get("projects") == 2, marker2)

        # Секрет в origin не раскрывается: экспорт пишет его в закрытый
        # файл пары (это его данные), но НИ в отказ, ни в маркер.
        secret = "https://user:secret-token@forge.example/team/x.git"
        secret_root = tempfile.mkdtemp(prefix="mop-test-542-s-")
        try:
            projects.SNAPSHOT = os.path.join(secret_root, "projects.json")
            projects.SNAPSHOT_DIR = secret_root
            projects.write_snapshot({"version": 1, "projects": [{"origin": secret}]},
                                    projects.SNAPSHOT)
            sdest = os.path.join(secret_root, "out")
            out = projects.export_pair(sdest)
            c.check("#542 the summary leaks no origin credentials",
                    "secret-token" not in _json.dumps(out), out)
            c.check("#542 the marker leaks no origin credentials",
                    "secret-token" not in open(os.path.join(sdest, "export.json")).read())
        finally:
            shutil.rmtree(secret_root)
    finally:
        projects.SNAPSHOT, projects.SNAPSHOT_DIR = saved_snapshot, saved_dir
        shutil.rmtree(root, ignore_errors=True)


@contextlib.contextmanager
def _patched(obj, **kw):
    """Подмена атрибутов на время блока (местный patched: без _lib-зависимостей)."""
    import contextlib as _cl
    saved = {k: getattr(obj, k) for k in kw}
    for k, v in kw.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


def _refused542(c, what, fn, *needles):
    """Утверждение: fn() отказывает RuntimeError (текст ниже -- без сырья)."""
    try:
        fn()
        c.check(what, False, "refusal was None")
    except RuntimeError as e:
        c.check(what, True, str(e))
        for n in needles:
            c.check(f"{what}: names {n}", n in str(e), str(e))
    except Exception as e:  # noqa: BLE001 -- не тот класс -- сразу видно
        c.check(what, False, f"wrong exception {type(e).__name__}: {e}")


def check_export_safety_542(c):
    """Привязка выгрузки к созданному каталогу и тихий успех (#542, ревью 2).

    HYPOTHESIS (#542): выгрузка писала по ПУТИ. Подмена каталога между
    заявкой и записью направляла секреты пары в ЧУЖОЙ каталог, подмена
    между записью и очисткой давала cleanup удалить чужое по имени; байты
    фактической записи не сверялись до маркера -- усечённый файл
    публиковался готовым; успех командлета печатал строку всегда.
    SOLUTION: записи идут по fd созданного каталога (dir_fd), identity
    (dev, ino) фиксируется тем же fd; перед маркером фактические байты
    перечитываются и сверяются с ожидаемыми; удаление имени каталога --
    только после свежей сверки identity; успех команды молчит, сводка --
    в машинном маркере и возврате библиотеки.
    STATUS: #542 (красный прогон впереди реализации)."""
    import shutil
    import tempfile
    v1 = {"version": 1, "projects": [{"origin": MOP, "limit": 2}, {"origin": RU}]}

    def seeded(root):
        projects.SNAPSHOT = os.path.join(root, "src", "projects.json")
        projects.SNAPSHOT_DIR = os.path.join(root, "src")
        projects.write_snapshot(v1, projects.SNAPSHOT)

    def refusal_of(fn):
        try:
            fn()
            return None
        except RuntimeError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001 -- не тот класс -- сразу видно
            return f"WRONG EXC {type(e).__name__}: {e}"

    def leaked(dest):
        """Имена файлов в каталоге, куда попали наши origin'ы."""
        return [n for n in os.listdir(dest)
                if MOP in open(os.path.join(dest, n), errors="ignore").read()
                or RU in open(os.path.join(dest, n), errors="ignore").read()]

    saved_snapshot, saved_dir = projects.SNAPSHOT, projects.SNAPSHOT_DIR

    def swap(dest, _mkdir=os.mkdir, foreign_name="foreign"):
        """Подмена каталога по пути: наш снесён, на его месте -- чужой.

        _mkdir захвачен ДО патча (def-time binding) -- иначе подмена
        звала бы патченный mkdir и рекурсировала.
        """
        shutil.rmtree(dest, ignore_errors=True)
        _mkdir(dest)
        with open(os.path.join(dest, foreign_name), "w") as f:
            f.write("not ours")

    # 1. Подмена между заявкой (mkdir) и привязкой: чужой каталог не
    # получает ни секретов, ни удаления.
    root = tempfile.mkdtemp(prefix="mop-test-542-r2a-")
    try:
        seeded(root)
        dest = os.path.join(root, "rollback")
        journal = []
        real_mkdir = os.mkdir

        def swapping_mkdir(path, mode=0o777, *a, **k):
            real_mkdir(path, mode, *a, **k)
            if os.path.abspath(path or "") == os.path.abspath(dest):
                journal.append("swap")
                swap(dest)

        with _patched(os, mkdir=swapping_mkdir):
            got = refusal_of(lambda: projects.export_pair(dest))
        c.check("#542 a directory swapped before the bind is refused",
                bool(got) and journal == ["swap"], (got, journal))
        c.check("#542 the swapped directory keeps its foreign file",
                os.path.exists(os.path.join(dest, "foreign")))
        c.check("#542 the swapped directory receives no origins",
                not leaked(dest), leaked(dest))
    finally:
        projects.SNAPSHOT, projects.SNAPSHOT_DIR = saved_snapshot, saved_dir
        shutil.rmtree(root, ignore_errors=True)

    # 2. Подмена между записями пары: записи идут в СОЗДАННЫЙ каталог (по
    # fd созданного каталога), а не по пути -- следующая запись в отвязанный
    # каталог ломается (ENOENT), чужой каталог не получает секретов.
    root = tempfile.mkdtemp(prefix="mop-test-542-r2b-")
    try:
        seeded(root)
        dest = os.path.join(root, "rollback")
        journal = []
        real_wc = getattr(projects, '_write_claimed', None)

        def swapping_write(dirfd, name, data, *a, **k):
            real_wc(dirfd, name, data, *a, **k)
            if name == "projects":
                journal.append("swap")
                swap(dest)

        with _patched(projects, _write_claimed=swapping_write):
            got = refusal_of(lambda: projects.export_pair(dest))
        c.check("#542 a directory swapped between writes is refused",
                bool(got) and journal == ["swap"], (got, journal))
        c.check("#542 the mid-write swapped directory receives no origins",
                not leaked(dest), leaked(dest))
        c.check("#542 the mid-write swapped directory keeps its foreign file",
                os.path.exists(os.path.join(dest, "foreign")))
    finally:
        projects.SNAPSHOT, projects.SNAPSHOT_DIR = saved_snapshot, saved_dir
        shutil.rmtree(root, ignore_errors=True)

    # 3. Усечённая запись (диск соврал): фактические байты перечитываются
    # и сверяются ДО маркера -- готовой публикации нет.
    root = tempfile.mkdtemp(prefix="mop-test-542-r2c-")
    try:
        seeded(root)
        dest = os.path.join(root, "rollback")
        journal = []
        real_wc = getattr(projects, '_write_claimed', None)

        def truncating_write(dirfd, name, data, *a, **k):
            if name == "limits.json":
                journal.append("trunc")
                data = data[:len(data) // 2]
            return real_wc(dirfd, name, data, *a, **k)

        with _patched(projects, _write_claimed=truncating_write):
            got = refusal_of(lambda: projects.export_pair(dest))
        c.check("#542 a truncated write is refused, not published",
                bool(got) and journal == ["trunc"], (got, journal))
        c.check("#542 a truncated export publishes no marker",
                not os.path.exists(os.path.join(dest, "export.json")), dest)
    finally:
        projects.SNAPSHOT, projects.SNAPSHOT_DIR = saved_snapshot, saved_dir
        shutil.rmtree(root, ignore_errors=True)

    # 4. Подмена между ownership-сверкой и удалением имени каталога: cleanup
    # сначала АТОМАРНО пере-заявляет имя (rename dest -> соль-имя), сверяет
    # identity ПЕРЕД rmdir; подмена ПОСЛЕ rename ловится этой сверкой --
    # чужое возвращается на место, не удаляется. Инжекция -- на этой сверке:
    # подменяем os.stat(gone) (первый stat по пути gone в жизни попытки),
    # между rename и сверкой меняя содержимое соль-имени.
    root = tempfile.mkdtemp(prefix="mop-test-542-r2d-")
    try:
        seeded(root)
        dest = os.path.join(root, "rollback")
        journal = []
        real_wc = projects._write_claimed
        real_stat = os.stat

        def dying_write(dirfd, name, data, *a, **k):
            if name == "export.json":
                raise OSError("disk died on the marker")
            return real_wc(dirfd, name, data, *a, **k)

        def swapping_stat(path, *a, **k):
            if "mop-gone" in str(path or "") and not str(path).endswith(".moved"):
                journal.append("verify-swap")
                # Соль-имя занято нашим каталогом: подменяем ЕГО содержимое
                # -- чужой каталог по тому же имени, identity не совпадёт.
                gone = str(path)
                os.rename(gone, gone + ".moved")  # наш ушёл в сторону
                os.mkdir(gone)  # чужой на соль-имени
                with open(os.path.join(gone, "foreign"), "w") as f:
                    f.write("not ours")
                return real_stat(gone, *a, **k)
            return real_stat(path, *a, **k)

        with _patched(projects, _write_claimed=dying_write), \
                _patched(os, stat=swapping_stat):
            got = refusal_of(lambda: projects.export_pair(dest))
        c.check("#542 a swap at the cleanup verification is caught",
                bool(got) and journal == ["verify-swap"], (got, journal))
        c.check("#542 the cleanup does not remove the swapped-in directory",
                os.path.exists(os.path.join(dest, "foreign")), dest)
        c.check("#542 the swapped-in directory receives no origins",
                not leaked(dest), leaked(dest))
        c.check("#542 the moved-out original export survives the failed cleanup",
                not os.path.exists(dest + ".moved") or
                os.path.exists(os.path.join(dest + ".moved", "limits.json")),
                dest + ".moved")
    finally:
        projects.SNAPSHOT, projects.SNAPSHOT_DIR = saved_snapshot, saved_dir
        shutil.rmtree(root, ignore_errors=True)

    # 5. Не-UTF8 источник: отказ без эха содержимого файла.
    root = tempfile.mkdtemp(prefix="mop-test-542-r2e-")
    try:
        seeded(root)
        with open(projects.SNAPSHOT, "wb") as f:
            f.write(b'\xff\xfe{"version": 1}')
        got = refusal_of(lambda: projects.export_pair(os.path.join(root, "out")))
        c.check("#542 a non-UTF8 source refuses without echoing it",
                bool(got) and "projects.json" in got and b"\xff\xfe".decode("latin-1") not in got,
                got)
    finally:
        projects.SNAPSHOT, projects.SNAPSHOT_DIR = saved_snapshot, saved_dir
        shutil.rmtree(root, ignore_errors=True)


def check_schema_534(c):
    """Схема v1 одним валидатором: reader, writer и export сверяют одно.

    HYPOTHESIS (#534): _snapshot_from_file проверял только «это dict,
    version == 1, projects -- список» (== принимал True и 1.0) и записи не
    проверял вовсе: null/не-объекты, пустой и нестроковый origin, неизвестные
    поля, дубль basename'а, limit null/bool/float/отрицательный/строка
    читались как данные, а writer молча выбрасывал явный null. Отказы
    печатали сырые значения (version!r, value!r) -- содержимое испорченного
    файла, с кредами, уезжало в текст ошибки. И existence-проверка
    os.path.exists глотала ошибки: существующий-но-нечитаемый v1 (битый
    symlink, права) молча давал fallback на легаси-пару.
    SOLUTION: один чистый валидатор _checked -- точные ключи верхнего
    уровня, type(version) is int и == 1, запись {origin, limit?} с origin
    по правилам origin'ов и лимитом строго int >= 0 (null -- не отсутствие
    ключа), один origin на имя; в отказе -- путь/имя/тип, не сырое
    значение. Пользуют его reader (_snapshot_from_file), write_snapshot
    (до всякого mkdir) и legacy_pair_of; существование файла отличает
    «нет» от «есть, но не достать».
    STATUS: #534 (красный прогон впереди реализации)."""
    import json as _json
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="mop-test-534s-")
    try:
        snap = os.path.join(d, "projects.json")
        # Reader: каждое нарушение схемы -- отказ, не «прочиталось как есть».
        cases = [
            ("version true", '{"version": true, "projects": []}', "version"),
            ("version 1.0", '{"version": 1.0, "projects": []}', "version"),
            ("a null entry", '{"version": 1, "projects": [null]}', None),
            ("an entry not an object", '{"version": 1, "projects": [42]}', None),
            ("an entry without origin", '{"version": 1, "projects": [{"limit": 2}]}', "origin"),
            ("an unknown entry field", '{"version": 1, "projects": '
             '[{"origin": "%s", "name": "mop"}]}' % MOP, "unknown"),
            ("an empty origin", '{"version": 1, "projects": [{"origin": ""}]}', "origin"),
            ("a non-string origin", '{"version": 1, "projects": [{"origin": 7}]}', "origin"),
            ("a null limit", '{"version": 1, "projects": '
             '[{"origin": "%s", "limit": null}]}' % MOP, "limit"),
            ("a bool limit", '{"version": 1, "projects": '
             '[{"origin": "%s", "limit": true}]}' % MOP, "limit"),
            ("a float limit", '{"version": 1, "projects": '
             '[{"origin": "%s", "limit": 1.5}]}' % MOP, "limit"),
            ("a negative limit", '{"version": 1, "projects": '
             '[{"origin": "%s", "limit": -1}]}' % MOP, "limit"),
            ("a string limit", '{"version": 1, "projects": '
             '[{"origin": "%s", "limit": "2"}]}' % MOP, "limit"),
            ("an unknown top-level field", '{"version": 1, "projects": [], "note": "x"}', "unknown"),
        ]
        for label, text, needle in cases:
            with open(snap, "w") as f:
                f.write(text)
            _refused534(c, f"#534 the reader refuses {label}",
                      lambda: projects.snapshot_of(d), *( [needle] if needle else [] ))
        # Дубль basename'а в самом файле: в v1 один origin на проект.
        with open(snap, "w") as f:
            f.write(_json.dumps({"version": 1, "projects": [
                {"origin": RU}, {"origin": OTHER}]}))
        _refused534(c, "#534 the reader refuses 2 origins of one project in the file",
                  lambda: projects.snapshot_of(d), "rudesktop", "2")

        # Сырое значение не печатается: version -- строка с секретом.
        with open(snap, "w") as f:
            f.write('{"version": "hunter2-secret", "projects": []}')
        got = _refusal_of(lambda: projects.snapshot_of(d))
        c.check("#534 the version refusal names the type, not the raw value",
                bool(got and "hunter2-secret" not in got and "str" in got), repr(got))
        # Числовая чужая версия: число -- не секрет, отказ называет её;
        # wrongtype (строка в version) -- format refusal БЕЗ совета
        # «upgrade»: апгрейд строку в схему не превратит.
        with open(snap, "w") as f:
            f.write('{"version": 2, "projects": []}')
        got = _refusal_of(lambda: projects.snapshot_of(d))
        c.check("#534 a numeric foreign version names the number",
                bool(got and "version 2" in got), repr(got))
        with open(snap, "w") as f:
            f.write('{"version": "hunter2-secret", "projects": []}')
        got = _refusal_of(lambda: projects.snapshot_of(d))
        c.check("#534 a wrongtype version is a format refusal without upgrade advice",
                bool(got and "upgrade" not in got and "hunter2-secret" not in got
                     and "str" in got), repr(got))

        # Секрет в названии неизвестного поля (top-level и в записи): имя
        # чужого поля -- тоже содержимое испорченного файла, не печатаем.
        with open(snap, "w") as f:
            f.write('{"version": 1, "projects": [], "hunter2-secret-field": 1}')
        got = _refusal_of(lambda: projects.snapshot_of(d))
        c.check("#534 an unknown top-level key refuses without its raw name",
                bool(got and "hunter2-secret-field" not in got and "unknown" in got), repr(got))
        with open(snap, "w") as f:
            f.write('{"version": 1, "projects": [{"origin": "%s", "hunter2": 1}]}' % MOP)
        got = _refusal_of(lambda: projects.snapshot_of(d))
        c.check("#534 an unknown entry key refuses without its raw name",
                bool(got and "hunter2" not in got and "unknown" in got), repr(got))
        # Тот же класс в writer и export: непредставимое не печатается.
        wout = os.path.join(d, "wtest534.json")
        got = _refusal_of(lambda: projects.write_snapshot(
            {"version": 1, "projects": [{"origin": MOP, "hunter2": 1}]}, wout))
        c.check("#534 the writer refuses an unknown field without its raw name",
                bool(got and "hunter2" not in got and "WRONG EXC" not in got), repr(got))
        c.check("#534 the refused unknown-field write leaves no file",
                not os.path.exists(wout))
        got = _refusal_of(lambda: projects.legacy_pair_of(
            {"version": 1, "projects": [], "hunter2": 1}))
        c.check("#534 the export refuses an unknown top-level key without its raw name",
                bool(got and "hunter2" not in got), repr(got))

        # Null-лимит: отказ типа, без совета «remove the key» -- советы
        # ручного лечения для malformed не разрешены.
        with open(snap, "w") as f:
            f.write('{"version": 1, "projects": [{"origin": "%s", "limit": null}]}' % MOP)
        got = _refusal_of(lambda: projects.snapshot_of(d))
        c.check("#534 a null limit refuses without manual-repair advice",
                bool(got and "remove the key" not in got and "NoneType" in got), repr(got))

        # Writer: та же схема, ДО всякого mkdir, и явный null -- отказ.
        out = os.path.join(d, "out", "sub", "projects.json")
        _refused534(c, "#534 the writer refuses a snapshot of another version",
                  lambda: projects.write_snapshot({"version": 2, "projects": []}, out))
        _refused534(c, "#534 the writer refuses an explicit null limit",
                  lambda: projects.write_snapshot({"version": 1, "projects": [
                      {"origin": MOP, "limit": None}]}, out), "limit")
        _refused534(c, "#534 the writer refuses an unknown entry field",
                  lambda: projects.write_snapshot({"version": 1, "projects": [
                      {"origin": MOP, "name": "mop"}]}, out))
        c.check("#534 a refused writer creates no directories",
                not os.path.exists(os.path.join(d, "out")))
        # Представимый снимок пишется и читается обратно (регрессия).
        projects.write_snapshot({"version": 1, "projects": [{"origin": MOP, "limit": 2}]}, out)
        c.check("#534 a representable snapshot still writes", os.path.exists(out))
        c.expect("#534 the written snapshot reads back",
                 projects.snapshot_of(os.path.dirname(out)),
                 {"version": 1, "projects": [{"origin": MOP, "limit": 2}]})

        # Export: непредставимый view (голые имена, orphan-потолки) --
        # отказ, не потеря строк при выгрузке.
        pair = tempfile.mkdtemp(prefix="mop-test-534s-p-")
        _legacy_pair(pair, [MOP, "oldproject"], {"mop": 3, "ghost": 7})
        _refused534(c, "#534 the export refuses a view with legacy lines",
                  lambda: projects.legacy_pair_of(projects.view_of(pair)), "oldproject")
        _refused534(c, "#534 the export refuses a view with legacy limits",
                  lambda: projects.legacy_pair_of(projects.view_of(pair)), "ghost")
        # View коллизии (два origin одного имени, legacy-полей нет): writer
        # обязан отказаться -- в v1 один origin на проект.
        shutil.rmtree(pair)
        pair = tempfile.mkdtemp(prefix="mop-test-534s-p-")
        _legacy_pair(pair, [RU, OTHER])
        _refused534(c, "#534 the writer refuses a view with 2 origins of one project",
                  lambda: projects.write_snapshot(projects.view_of(pair),
                                                  os.path.join(d, "out2")), "rudesktop")
        c.check("#534 the refused collision write leaves no file",
                not os.path.exists(os.path.join(d, "out2")))

        # Строгие лимиты пары: отрицательное -- отказ; сырое значение и
        # буквальный {name!r} в отказе -- нет.
        shutil.rmtree(pair)
        pair = tempfile.mkdtemp(prefix="mop-test-534s-p-")
        _legacy_pair(pair, [MOP], {"mop": -1})
        _refused534(c, "#534 a negative limit must refuse the migration",
                  lambda: projects.snapshot_of(pair), "mop")
        shutil.rmtree(pair)
        pair = tempfile.mkdtemp(prefix="mop-test-534s-p-")
        _legacy_pair(pair, [MOP], {"mop": "hunter2"})
        got = _refusal_of(lambda: projects.snapshot_of(pair))
        c.check("#534 the limit refusal names the project and the type, not the raw value",
                bool(got and "hunter2" not in got and "mop" in got and "str" in got
                     and "{name" not in got), repr(got))

        # Существующий-но-нечитаемый v1: битый symlink и недоступный
        # каталог -- громкий отказ, fallback на легаси-пару нет.
        broken = tempfile.mkdtemp(prefix="mop-test-534s-b-")
        _legacy_pair(broken, [MOP])
        os.symlink("no-such-target", os.path.join(broken, "projects.json"))
        _refused534(c, "#534 a broken-symlink snapshot refuses, no fallback to the pair",
                  lambda: projects.snapshot_of(broken), "projects.json")
        os.remove(os.path.join(broken, "projects.json"))
        c.expect("#534 with the symlink gone the pair migrates",
                 projects.snapshot_of(broken), {"version": 1, "projects": [{"origin": MOP}]})
        shutil.rmtree(broken)
        # Тот же класс для читателя: битый symlink в взгляд читателя -- не
        # «пара с миграцией», а громкий отказ (fallback маскировал бы поломку).
        broken = tempfile.mkdtemp(prefix="mop-test-534s-b-")
        _legacy_pair(broken, [MOP])
        os.symlink("no-such-target", os.path.join(broken, "projects.json"))
        _refused534(c, "#534 a broken-symlink snapshot refuses for the reader too",
                  lambda: projects.view_of(broken), "projects.json")
        os.remove(os.path.join(broken, "projects.json"))
        c.expect("#534 with the symlink gone the reader migrates the pair",
                 projects.view_of(broken), {"version": 1, "projects": [{"origin": MOP}]})
        shutil.rmtree(broken)
        shut = tempfile.mkdtemp(prefix="mop-test-534s-d-")
        projects.write_snapshot({"version": 1, "projects": [{"origin": MOP}]},
                                os.path.join(shut, "projects.json"))
        os.chmod(shut, 0)
        try:
            _refused534(c, "#534 an unreachable directory refuses, not an empty registry",
                      lambda: projects.snapshot_of(shut))
        finally:
            os.chmod(shut, 0o700)
        shutil.rmtree(shut)
        # Пара (без снимка) в недоступном каталоге -- тот же класс: не
        # «свежая установка» с пустым реестром, а громкий отказ.
        paird = tempfile.mkdtemp(prefix="mop-test-534s-pd-")
        _legacy_pair(paird, [MOP])
        os.chmod(paird, 0)
        try:
            _refused534(c, "#534 an unreachable directory with a pair refuses too",
                      lambda: projects.snapshot_of(paird))
        finally:
            os.chmod(paird, 0o700)
        shutil.rmtree(paird)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_view_534(c):
    """Читающий взгляд #534: прежние видимые данные из проблемной пары.

    Читатель (глагол-список, лимит в add, users deploy-прогона) обязан
    показывать старые данные и без миграции -- refusing-миграция блокирует
    МУТАЦИЮ, а не чтение. Голое имя остаётся строкой lines, конфликт
    basename'ов -- оба origin'а, orphan-лимит -- в limits: взгляд
    воссоздаёт прежний ответ глагола `projects` по паре, ничего не пишет
    и не теряет. STATUS: #534"""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="mop-test-534v-")
    try:
        # Чистая пара: взгляд -- тот же снимок, что и миграция.
        _legacy_pair(d, [MOP, RU], {"mop": 3, "rudesktop": 0})
        c.expect("#534 the view of a clean pair is the migration itself",
                 projects.view_of(d), projects.snapshot_of(d))

        # Свежая установка: взгляд -- честный пустой реестр.
        shutil.rmtree(d)
        d = tempfile.mkdtemp(prefix="mop-test-534v-")
        c.expect("#534 the view of a fresh install is empty",
                 projects.view_of(d), {"version": 1, "projects": []})

        # Голое имя: взгляд сохраняет прежний ответ глагола `projects` --
        # имя в lines и names, лимит при нём, origin'ы как были.
        _legacy_pair(d, [MOP, "oldproject"], {"mop": 3})
        got = projects.view_of(d)
        c.expect("#534 a bare legacy name survives in the view's lines",
                 sorted(projects.snapshot_lines(got)), sorted([MOP, "oldproject"]))
        c.expect("#534 a bare legacy name survives in the view's names",
                 projects.snapshot_names(got), ["mop", "oldproject"])
        c.expect("#534 the view keeps the limit of a bare-named project",
                 projects.snapshot_limits(got), {"mop": 3})
        c.check("#534 the view writes nothing",
                not os.path.exists(os.path.join(d, "projects.json")))

        # Конфликт basename'ов: прежний ответ `projects` показывает оба
        # origin'а и оба имени -- без консенсуса, какой правда.
        _legacy_pair(d, [RU, OTHER], {"rudesktop": 1})
        got = projects.view_of(d)
        c.expect("#534 a colliding pair shows both origins in the view",
                 sorted(projects.snapshot_lines(got)), sorted([RU, OTHER]))
        c.expect("#534 a colliding pair shows the name in the view",
                 projects.snapshot_names(got), ["rudesktop"])
        c.expect("#534 a colliding pair keeps its limit in the view",
                 projects.snapshot_limits(got), {"rudesktop": 1})
        c.check("#534 the view of a collision writes nothing",
                not os.path.exists(os.path.join(d, "projects.json")))

        # Orphan-лимит: потолок чужого имени виден -- прежний ответ
        # `projects` печатал limits целиком, включая неизвестные имена.
        _legacy_pair(d, [MOP], {"mop": 3, "ghost": 7})
        got = projects.view_of(d)
        c.expect("#534 an orphan limit stays visible in the view",
                 projects.snapshot_limits(got), {"mop": 3, "ghost": 7})
        c.check("#534 the view of an orphan limit writes nothing",
                not os.path.exists(os.path.join(d, "projects.json")))

        # Writer не принимает непредставимый view: молча выбросить
        # legacy-поля и сохранить усечённое состояние -- та же потеря,
        # что и обрыв между двумя файлами; strict snapshot_of в мутаторах
        # -- необходимая, но не единственная гарантия сохранности.
        out = os.path.join(d, "out.json")
        _legacy_pair(d, [MOP, "oldproject"])
        _refused534(c, "#534 write_snapshot refuses a view with legacy lines",
                  lambda: projects.write_snapshot(projects.view_of(d), out), "oldproject")
        c.check("#534 a refused write leaves no file",
                not os.path.exists(out))
        _legacy_pair(d, [MOP], {"mop": 3, "ghost": 7})
        _refused534(c, "#534 write_snapshot refuses a view with legacy limits",
                  lambda: projects.write_snapshot(projects.view_of(d), out), "ghost")
        c.check("#534 a refused write still leaves no file",
                not os.path.exists(out))
        # Представимый снимок пишется как раньше -- гейт бьёт только view.
        projects.write_snapshot({"version": 1, "projects": [{"origin": MOP}]}, out)
        c.check("#534 a representable snapshot still writes",
                os.path.exists(out))
        os.remove(out)

        # Битый/чужой снимок -- как и в snapshot_of: громкий отказ без
        # пустого реестра и без fallback (взгляд не弱ее миграции).
        with open(os.path.join(d, "projects.json"), "w") as f:
            f.write('{"version": 2, "projects": []}')
        _refused534(c, "#534 the view refuses a newer schema loudly",
                  lambda: projects.view_of(d))
        os.remove(os.path.join(d, "projects.json"))

        # Вдогонку -- контракт ошибок миграции: отказ называет причину и
        # сохранность (файлы не тронуты), но НЕ советует несуществующего
        # исправления. Совет «заведи origin» или «удали руками» -- ловушка:
        # правка реестра идёт теми же глаголами, и миграция, которой совет
        # требует, тем же отказом и откажет. Решение конфликта -- отдельная
        # задача (resolve), не строка в отказе.
        _legacy_pair(d, [RU, OTHER])
        _refused534(c, "#534 the collision refusal names no fix that migration would refuse",
                  lambda: projects.snapshot_of(d), "rudesktop", "intact")
        c.check("#534 the collision refusal advises no hand-editing",
                " by hand" not in (str(_migrate_refusal(d)) or ""), "")
        _legacy_pair(d, [MOP, "oldproject"])
        _refused534(c, "#534 the bare-name refusal names no fix that migration would refuse",
                  lambda: projects.snapshot_of(d), "oldproject", "intact")
        c.check("#534 the bare-name refusal advises no hand-editing",
                " by hand" not in (str(_migrate_refusal(d)) or ""), "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    c = Checks()
    for fn in (check_add, check_delete, check_names, check_limits, check_read_limits_337,
               check_git_hosts, check_for_deploy, check_local_view_534,
               check_migration_534, check_schema_534, check_snapshot_534,
               check_view_534, check_export_542, check_export_safety_542):
        fn(c)
    return c.report("projects")


if __name__ == "__main__":
    sys.exit(main())
