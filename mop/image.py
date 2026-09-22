"""Образ шарда: манифест -> прогон плейбука сборки -> объявление узлам.

Дорога у сборки одна, а фронтендов два — `mop driver build` в терминале и
инструмент `build` в MCP мастера (#49), — поэтому она живёт здесь.
Библиотека молчит: вывод ansible уходит туда, куда скажет вызывающий
(`out`: файл или None, то есть наследование), а итог возвращается данными.

Только для управляющей машины: здесь нужны токен Nomad (объявление) и
инвентарь (плейбук), и ни того ни другого на узлах нет.
"""
import json
import os
import subprocess

from . import config, driver, manifest, nomad, puppets

PLAYBOOK = os.path.join(config.PROJECT, "deploy", "pve-build.yml")


def prepare(origin, root=None):
    """Манифесты шарда: из рабочей копии root, если она названа, иначе из
    origin (дефолтная ветка). -> словарь manifest.fetch плюс 'origin'.
    Отказ — RuntimeError, как у manifest."""
    shard = puppets.shard_of(origin)
    got = manifest.fetch_tree(root, shard) if root else manifest.fetch(origin)
    got["origin"] = origin
    return got


def extra_vars(origin, got):
    """Что уезжает плейбуку. None-половины не передаём вообще, а не как
    null: `default('')` в условиях плейбука не подменяет определённый None,
    и len(None) ронял сборку шарда без vars (rugent: только задачи). Поймано
    живой сборкой (#26), держится tests/image.py."""
    extra = {"mop_shard": got["shard"], "mop_origin": origin,
             "mop_shard_asks": got["asks"]}
    if got["ws_vars"]:
        extra["mop_shard_vars"] = got["ws_vars"]
    if got["ws_tasks"]:
        extra["mop_shard_tasks"] = got["ws_tasks"]
    return extra


def bake(origin, got, out=None):
    """Прогнать плейбук сборки. -> код возврата ansible.

    out — куда писать вывод плейбука: файл (MCP пишет в журнал и присылает
    хвост вестью) либо None (терминал видит прогон живьём). Инвентарь — из
    окружения, его ставит диспетчер `mop` для всех командлетов."""
    settings = json.dumps({k: v for k, (v, _) in config.effective().items()},
                          ensure_ascii=False)
    return subprocess.call(
        ["ansible-playbook", "-i", os.environ["INVENTORY"], PLAYBOOK,
         "--extra-vars", settings,
         "--extra-vars", json.dumps(extra_vars(origin, got))],
        stdout=out, stderr=subprocess.STDOUT if out else None)


def announce(shard):
    """Сказать кластеру, что образ этого шарда на узлах собран.
    -> [(узел, 'announced'|'already announced'|'not a container node')].

    Без этого планировщик про образ не знает, и папет шарда на узел не
    сядет — ограничение в спеке смотрит именно на этот перечень. Отдельным
    шагом после плейбука, а не задачей в нём: токен Nomad есть только у
    управляющей машины, и тащить его в плейбук, который ходит на узлы,
    значит вернуть то, ради чего заводили шину.

    Только после успешной сборки: объявить образ, которого нет, — это
    папет, висящий pending, и ровно тот отказ, от которого ограничение и
    заводилось."""
    out = []
    for name, meta in sorted(nomad.nodes_meta().items()):
        if meta.get("mop_driver", driver.DEFAULT) == driver.DEFAULT:
            out.append((name, "not a container node"))  # объявляет any и так
            continue
        # Перечень берём с узла: серверная копия отстаёт на секунды, и
        # дописать к устаревшей значит стереть ранее объявленные образы.
        have = [s for s in (nomad.node_dynamic_meta(name).get("mop_shards") or "").split(",") if s]
        if shard in have:
            out.append((name, "already announced"))
            continue
        nomad.set_node_meta(name, {"mop_shards": ",".join(sorted(have + [shard]))})
        out.append((name, f"announced, serves {', '.join(sorted(have + [shard]))}"))
    return out
