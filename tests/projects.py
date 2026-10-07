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
    STATUS: FIXED — see #117"""
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
        # какой origin правда, мигрировать нельзя -- отказ с обоими.
        shutil.rmtree(d); d = tempfile.mkdtemp(prefix="mop-test-534-")
        _legacy_pair(d, [RU, OTHER])  # оба -- rudesktop
        _refused534(c, "#534 two origins of one project must refuse with both",
                  lambda: projects.snapshot_of(d), RU, OTHER)

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


def main():
    c = Checks()
    for fn in (check_add, check_delete, check_names, check_limits, check_read_limits_337,
               check_git_hosts, check_for_deploy,
               check_migration_534, check_snapshot_534):
        fn(c)
    return c.report("projects")


if __name__ == "__main__":
    sys.exit(main())
