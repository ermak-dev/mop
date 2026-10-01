#!/usr/bin/env python3
"""Слои пакета по машине без пула: python3 tests/layers.py

Четыре слоя, и это стороны пула, а не вкус (#258): общее -- то, что нужно
всем (шина, настройки, контекст, пути); клиент -- машина оператора и
мастер-шелл; сервер -- контроллер с инвентарём, Nomad и файлами /etc/nats;
узел -- агент и драйверы. Правило одно: импорт идёт только вниз. Общее ни
от кого не зависит, клиент и узел видят общее и себя, сервер -- общее и
себя. Так «клиент не читает .env», «nomad.py не бывает у оператора» (#81)
и «узел не знает токена» становятся проверкой, а не памятью.

Сканер читает и ленивые импорты внутри функций -- именно ими границы и
протекают, -- а рёбра, которых в тексте нет (реестр драйверов, профили LLM,
диспетчер командлетов), названы здесь явно. Модуль без слоя -- отказ:
новый файл кладут в слой сознательно.
"""
import ast
import os
import sys

from _lib import Checks  # noqa: E402 -- без hermetic: проверка лишь читает исходники

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PKG = os.path.join(ROOT, "mop")

COMMON, CLIENT, SERVER, NODE = "common", "client", "server", "node"
SEES = {COMMON: {COMMON}, CLIENT: {COMMON, CLIENT}, NODE: {COMMON, NODE},
        SERVER: {COMMON, SERVER}}

# Слой -- каталог (#260): mop/common, mop/client, mop/server, mop/node, и
# модуль mop.<слой>.* лежит в своём слое без записи здесь. Карта -- только
# для того, что из каталога не выводится:
#   корень пакета, session и usage -- общее, лежат в корне: едут в тело
#   файлом по путям, которые знают спека, хуки и агент;
#   mop.driver -- общий пакет путей и реестра, а сами драйверы -- узел;
#   mop.cli -- по командам: команда лежит в слое той машины, где её зовут
#   (пространства имён cli своё место называют и так). Группы -- их
#   __init__ и общее (_common) тоже.
LAYER_DIRS = (COMMON, CLIENT, SERVER, NODE)
LAYERS = {
    "mop": COMMON, "mop.session": COMMON, "mop.usage": COMMON,
    "mop.driver": COMMON, "mop.driver.host": NODE, "mop.driver.pve": NODE,
    # cli: общее
    "mop.cli": COMMON, "mop.cli.lib": COMMON, "mop.cli.term": COMMON,
    "mop.cli.__main__": COMMON,
    "mop.cli.driver": COMMON,
    # cli: клиент
    "mop.cli.core": CLIENT, "mop.cli.project": CLIENT, "mop.cli.secret": CLIENT,
    "mop.cli.node": CLIENT, "mop.cli.dev": CLIENT, "mop.cli.service.mcp": CLIENT,
    "mop.cli.pool.join": CLIENT, "mop.cli.pool.doctor": CLIENT,
    "mop.cli.pool.login": CLIENT, "mop.cli.pool.llm": CLIENT,
    "mop.cli.pool.disk": CLIENT, "mop.cli.pool.gc": CLIENT,
    "mop.cli.pool.sweep": CLIENT,
    "mop.cli.pool": CLIENT, "mop.cli.service": CLIENT,
    # cli: сервер. Образ печёт контроллер: ansible по гипервизорам, как deploy.
    "mop.cli.driver.build": SERVER,
    # Пространство контроллера (#259): deploy, config, setup, user,
    # cluster, bootstrap, web, callout, pve-facts и _play -- одним ключом.
    "mop.cli.server": SERVER,
    # mop setup наверху -- машина оператора, но играет self.yml с настройками
    # установки (playvars) -- как и до #259, когда оба варианта жили в нём.
    "mop.cli.pool.setup": SERVER, "mop.cli.pool._self": SERVER,
    # cli: узел
    "mop.cli.service.agent": NODE, "mop.cli.driver.run": NODE,
    "mop.cli.driver.list": NODE, "mop.cli.driver.sweep": NODE,
    "mop.cli.driver.clone-work": NODE,
}
# Что вправе лежать в корне пакета, кроме каталогов слоёв.
ROOT_PARTS = ("session", "usage", "driver", "cli")

# Рёбра, которых в тексте нет: importlib по имени.
# Реестр драйверов (mop.driver -> host, pve) сюда не входит намеренно: он
# читается только на узле (driver.current), а пути и разбор имён из того же
# модуля нужны всем. Драйверы проверяются как модули узла сами по себе.
DYNAMIC = {
    "mop.common.llm": {"mop.common.llm.claude", "mop.common.llm.glm"},   # профили LLM
}


def modules():
    """{имя модуля: путь} по дереву пакета."""
    out = {}
    for d, _, files in os.walk(PKG):
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(d, f)
                rel = os.path.relpath(p, ROOT)[:-3].replace(os.sep, ".")
                out[rel[:-9] if rel.endswith(".__init__") else rel] = p
    return out


def layer_of(name):
    """Слой модуля: каталог слоя (mop.<слой>.*), иначе сам или ближайший
    родитель в карте. Модуль корня вне карты слоя не получает: иначе новый
    mop/x.py молча унаследовал бы слой корня пакета."""
    parts = name.split(".")
    if len(parts) > 1 and parts[1] in LAYER_DIRS:
        return parts[1]
    if len(parts) > 1 and parts[1] not in ROOT_PARTS:
        return None
    while parts:
        n = ".".join(parts)
        if n in LAYERS:
            return LAYERS[n]
        parts.pop()
    return None


def imports_of(path, name, known):
    """Модули пакета, которые импортирует файл, где угодно в тексте."""
    tree = ast.parse(open(path).read())
    pkg = name if os.path.basename(path) == "__init__.py" else name.rsplit(".", 1)[0]
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("mop"):
                    out.add(a.name)
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                base = ".".join(pkg.split(".")[:len(pkg.split(".")) - n.level + 1])
                base = base + ("." + n.module if n.module else "")
            elif n.module and n.module.startswith("mop"):
                base = n.module
            else:
                continue
            for a in n.names:
                out.add(f"{base}.{a.name}" if f"{base}.{a.name}" in known else base)
    return {m for m in out if m in known and m != name}


def violations(c, mods):
    for name, path in sorted(mods.items()):
        parts = name.split(".")
        # Слой -- каталог (#260): модуль или пакет в корне mop/ слоя не
        # называет. Корень держит только session и usage (их пути знают
        # спека, хуки и агент), реестр драйверов и командлеты.
        if not c.check(f"{name}: not in a layer directory",
                       not (len(parts) > 1 and parts[1] not in LAYER_DIRS + ROOT_PARTS)):
            continue
        me = layer_of(name)
        if not c.check(f"{name}: no layer in tests/layers.py", not (me is None)):
            continue
        for dep in sorted(imports_of(path, name, mods) | DYNAMIC.get(name, set())):
            if dep.startswith("mop.cli") and name in ("mop.cli", "mop.cli.__main__"):
                continue
            it = layer_of(dep)
            if c.check(f"{dep}: no layer in tests/layers.py", not (it is None)):
                c.check(f"{name} ({me}) imports {dep} ({it})", not (it not in SEES[me]))


def bound_names(path, name, known):
    """{локальное имя: модуль пакета} по импортам файла, с псевдонимами
    `import ... as`: по ним код и обращается к атрибутам."""
    tree = ast.parse(open(path).read())
    pkg = name if os.path.basename(path) == "__init__.py" else name.rsplit(".", 1)[0]
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.level:
                base = ".".join(pkg.split(".")[:len(pkg.split(".")) - n.level + 1])
                base = base + ("." + n.module if n.module else "")
            elif n.module and n.module.startswith("mop"):
                base = n.module
            else:
                continue
            for a in n.names:
                full = f"{base}.{a.name}"
                if full in known:
                    out[a.asname or a.name] = full
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name in known:
                    out[a.asname or a.name.split(".")[0]] = a.name
    return out


def dangling(c, mods):
    """Обращения `m.attr` к модулю пакета, у которого такого атрибута нет
    (#261): после переноса функции между модулями поиск по тексту не видит
    псевдонима (`from . import nodes as pool_nodes`), а проверка слоёв --
    атрибутов. Модуль, который не импортируется без своей библиотеки,
    пропускается: его атрибуты проверит строгий прогон CI."""
    import importlib
    sys.path.insert(0, ROOT)
    loaded = {}
    for name in mods:
        if name.endswith("__main__"):     # исполняется при импорте
            loaded[name] = None
            continue
        try:
            loaded[name] = importlib.import_module(name)
        except Exception:
            loaded[name] = None
    seen = set()
    for name, path in sorted(mods.items()):
        names = bound_names(path, name, mods)
        if not names:
            continue
        tree = ast.parse(open(path).read())
        # Локальное имя, совпавшее с модулем (`spec = VERBS[verb]`,
        # `state` в цикле), -- не модуль: обращения в его области не считаются.
        for scope in [tree] + [n for n in ast.walk(tree)
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            local = set()
            if not isinstance(scope, ast.Module):
                a = scope.args
                local |= {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
                local |= {x.arg for x in (a.vararg, a.kwarg) if x}
            for n in ast.walk(scope):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    local.add(n.id)
            for n in ast.walk(scope):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                        and n.value.id in names and n.value.id not in local:
                    target = loaded.get(names[n.value.id])
                    msg = (f"{name}:{n.lineno}: {n.value.id}.{n.attr} -- "
                           f"{names[n.value.id]} has no such attribute")
                    if msg in seen:       # одно обращение -- одна строка, как прежний set
                        continue
                    seen.add(msg)
                    c.check(msg, not (target is not None and not hasattr(target, n.attr)))


# ── #268: одно написание на константу, мёртвого кода нет ─────────────────
# HYPOTHESIS: ~/.config/mop набран руками в двадцати местах, ключ меты узла
# mop_projects -- литералом рядом с spec.META_PROJECTS, канал сервиса
# кластера -- второй копией в cluster.CHANNEL, соглашение `tmux -L <имя>
# ... -t <имя>` -- в пяти местах мимо класса Tmux агента, таймаут вызова
# перепечатан в why(out, code, N); и мёртвые имена пережили #263-#267.
# SOLUTION: mop/common/paths.py (каталог и пути рядом с ним), META_PROJECTS
# и PROJECTS_VAR, busnames.CLUSTER_CHANNEL, driver.Tmux, таймауты -- именами
# у своего вызова; мёртвое удалено. Литералы врапера (spec.WRAPPER) и
# докстринги (текст помощи) остаются: врапер -- перерегистрация всего пула.
# STATUS: FIXED — see #268
CONFIG_HOME = ".config/mop"
DEAD = {"mop/common/puppets.py": ("facts", "project_of_name"),
        "mop/server/nomad.py": ("token_or_none", "ready_nodes"),
        "mop/server/spec.py": ("queued",), "mop/driver/pve.py": ("routes",),
        "mop/common/bus.py": ("publish",), "mop/server/cluster.py": ("CHANNEL",)}
# Где литерал -- определение, а не повтор: {файл: имя присваивания}.
OWNERS = {".config/mop": {"mop/common/paths.py": None},
          "mop_projects": {"mop/server/spec.py": "META_PROJECTS",
                           "mop/cli/server/_play.py": "PROJECTS_VAR"},
          "tmux -L": {"mop/driver/__init__.py": None}}


def _strings(tree):
    """(строка, узел-владелец присваивания или None, lineno) всех строковых
    литералов, кроме докстрингов. f-строки -- по их литеральным кускам."""
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and n.body and isinstance(n.body[0], ast.Expr) \
                and isinstance(n.body[0].value, ast.Constant) and isinstance(n.body[0].value.value, str):
            docs.add(id(n.body[0].value))
    owner = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            names = [t.id for t in n.targets if isinstance(t, ast.Name)]
            for sub in ast.walk(n):
                owner[id(sub)] = names[0] if names else None
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            yield n.value, owner.get(id(n)), n.lineno
    # Список аргументов ["tmux", "-L", ...] -- то же соглашение, что строка.
    for n in ast.walk(tree):
        if isinstance(n, ast.List):
            vals = [e.value for e in n.elts if isinstance(e, ast.Constant)]
            if vals[:2] == ["tmux", "-L"]:
                yield "tmux -L", owner.get(id(n)), n.lineno


def one_spelling(c):
    for dp, _, fs in os.walk(PKG):
        for f in fs:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dp, f)
            rel = os.path.relpath(path, ROOT)
            tree = ast.parse(open(path).read())
            for text, owner, line in _strings(tree):
                if rel == "mop/server/spec.py" and owner in ("WRAPPER", "OUTER"):
                    continue
                for needle, allowed in OWNERS.items():
                    if needle not in text:
                        continue
                    c.check(f"{rel}:{line}: {needle!r} spelled out -- use its constant",
                            rel in allowed and allowed[rel] in (None, owner))
            # Таймаут вызова -- именем: why(out, code, 600) рядом с timeout=600
            # однажды разойдутся.
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "why" \
                        and len(n.args) > 2 and isinstance(n.args[2], ast.Constant):
                    c.fail(f"{rel}:{n.lineno}: why(..., {n.args[2].value}) -- name the timeout")
            top = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
                   if isinstance(t, ast.Name)} | {n.name for n in tree.body
                                                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for name in DEAD.get(rel, ()):
                c.check(f"{rel}: {name} is dead code -- delete it", not (name in top))


def main():
    c = Checks()
    mods = modules()
    violations(c, mods)
    dangling(c, mods)
    one_spelling(c)
    print(f"layers: {len(mods)} modules, {c.failed} violations")
    return c.report("layers")


if __name__ == "__main__":
    sys.exit(main())
