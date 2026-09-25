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

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PKG = os.path.join(ROOT, "mop")

COMMON, CLIENT, SERVER, NODE = "common", "client", "server", "node"
SEES = {COMMON: {COMMON}, CLIENT: {COMMON, CLIENT}, NODE: {COMMON, NODE},
        SERVER: {COMMON, SERVER}}

# Карта слоёв. Пакет `mop.cli` -- по командам: команда лежит в слое той
# машины, где её зовут. Группы -- их __init__ и общее (_common) тоже.
LAYERS = {
    # общее
    "mop": COMMON, "mop.bus": COMMON, "mop.busnames": COMMON, "mop.config": COMMON,
    "mop.context": COMMON, "mop.creds": COMMON, "mop.fsutil": COMMON,
    "mop.render": COMMON, "mop.domain": COMMON, "mop.driver": COMMON,
    "mop.puppets": COMMON, "mop.session": COMMON, "mop.deps": COMMON,
    "mop.cli": COMMON, "mop.cli.lib": COMMON, "mop.cli.__main__": COMMON,
    # Чистые функции и то, что читают обе стороны шины: вердикты, аренда,
    # профили LLM и их реестр плагинов, манифест проекта, реестр проектов,
    # секреты (хранение на сервере, разбор у всех), GitLab, учёт токенов,
    # подписчик сервиса, токен landing.
    "mop.state": COMMON, "mop.lease": COMMON, "mop.llm": COMMON,
    "mop.llm.claude": COMMON, "mop.llm.glm": COMMON, "mop.plugins": COMMON,
    "mop.manifest": COMMON, "mop.projects": COMMON, "mop.project_secrets": COMMON,
    "mop.gitlab": COMMON, "mop.usage": COMMON, "mop.service": COMMON,
    "mop.landing": COMMON, "mop.cli.driver": COMMON,
    # клиент
    "mop.channel": CLIENT, "mop.keys": CLIENT,
    "mop.cli.core": CLIENT, "mop.cli.project": CLIENT, "mop.cli.secret": CLIENT,
    "mop.cli.node": CLIENT, "mop.cli.dev": CLIENT, "mop.cli.service.mcp": CLIENT,
    "mop.cli.pool.join": CLIENT, "mop.cli.pool.doctor": CLIENT,
    "mop.cli.pool.login": CLIENT, "mop.cli.pool.llm": CLIENT,
    "mop.cli.pool.disk": CLIENT, "mop.cli.pool.gc": CLIENT,
    "mop.cli.pool.sweep": CLIENT,
    "mop.cli.pool": CLIENT, "mop.cli.service": CLIENT,
    # сервер
    "mop.cluster": SERVER, "mop.spec": SERVER, "mop.nomad": SERVER,
    "mop.callout": SERVER, "mop.identity": SERVER, "mop.ldapauth": SERVER,
    "mop.operators": SERVER, "mop.nkjwt": SERVER, "mop.natsconf": SERVER,
    "mop.playvars": SERVER, "mop.builder": SERVER, "mop.image": SERVER,
    "mop.web": SERVER, "mop.nodes": SERVER, "mop.bootstrap": SERVER,
    # Образ печёт контроллер: ansible по гипервизорам, как deploy.
    "mop.cli.driver.build": SERVER,
    "mop.cli.pool.deploy": SERVER, "mop.cli.pool.config": SERVER,
    "mop.cli.pool.setup": SERVER, "mop.cli.user": SERVER,
    "mop.cli.pool._play": SERVER,
    "mop.cli.cluster": SERVER, "mop.cli.bootstrap": SERVER,
    "mop.cli.service.callout": SERVER, "mop.cli.service.web": SERVER,
    "mop.cli.driver.pve-facts": SERVER,
    # узел
    "mop.agent": NODE, "mop.driver.host": NODE, "mop.driver.pve": NODE,
    "mop.cli.service.agent": NODE, "mop.cli.driver.run": NODE,
    "mop.cli.driver.list": NODE, "mop.cli.driver.sweep": NODE,
}

# Рёбра, которых в тексте нет: importlib по имени.
# Реестр драйверов (mop.driver -> host, pve) сюда не входит намеренно: он
# читается только на узле (driver.current), а пути и разбор имён из того же
# модуля нужны всем. Драйверы проверяются как модули узла сами по себе.
DYNAMIC = {
    "mop.llm": {"mop.llm.claude", "mop.llm.glm"},             # профили LLM
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
    """Слой модуля: сам, иначе ближайший родитель в карте."""
    parts = name.split(".")
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


def violations(mods):
    out = []
    for name, path in sorted(mods.items()):
        me = layer_of(name)
        if me is None:
            out.append(f"{name}: no layer in tests/layers.py")
            continue
        for dep in sorted(imports_of(path, name, mods) | DYNAMIC.get(name, set())):
            if dep.startswith("mop.cli") and name in ("mop.cli", "mop.cli.__main__"):
                continue
            it = layer_of(dep)
            if it is None:
                out.append(f"{dep}: no layer in tests/layers.py")
            elif it not in SEES[me]:
                out.append(f"{name} ({me}) imports {dep} ({it})")
    return out


def main():
    mods = modules()
    bad = violations(mods)
    print("\n".join(f"FAIL {b}" for b in bad))
    print(f"layers: {len(mods)} modules, {len(bad)} violations" + (" FAILED" if bad else " ok"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
