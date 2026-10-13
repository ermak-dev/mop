#!/usr/bin/env python3
"""Жизненный цикл роли #582 без пула: python3 tests/soul_lifecycle.py

Принятая схема #579 (docs/SOUL.md, ревизия 99688a6): тройка роли
soul/soul_digest/soul_inc едет Meta джоба (#580 -- поля уже в JobMeta),
сервер закрепляет бандл дайджестом и выдаёт принципала grant-<32hex>
(#581 -- реестр уже в mop/server/grants.py), а ЗДЕСЬ -- жизненный цикл:
add с ролью (каталог сервера, а не текст клиента), идемпотентный
request_id до выбора имени, сохранение тройки на respec/restart/сборке
образа, смена роли -- новый папет, смена digest живой инкарнации --
отказ, удаление отзывает привязку, квота считает все роли, ping
объявляет протокол только включённым сервером, ростер проецирует
receives_dispatch из закреплённого бандла.

HYPOTHESIS: сегодня спека и глаголы кластера тройку не знают: add
теряет роль, update стёр бы её вместе с веткой (так терялась ветка
#256/#265), сборка образа подняла бы папета без души, повтор
неопределённого add завёл бы второго принципала на то же имя, а
делегированный add принял бы чужой workspace-текст на исполнение
сервером. SOLUTION: тройка течёт job_spec/respec из Meta, add резервирует
имя/инкарнацию/принципала ДО Nomad и активирует после, отношения
хранятся в реестре #581 (grants.prepare/activate/revoke/by_request/
by_job -- реальный модуль, не заглушка), роль администратора включается
настройкой сервера, выключенная -- молчит и в ping, и в add.
STATUS: RED -- ждёт реализации #582 (поля JobMeta #580 и реестр #581
на ветке уже есть; by_request/by_job -- запрос мастер-реляции к #581).
"""
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, FakeNomad, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import projects  # noqa: E402
from mop.common import soul as soulmod  # noqa: E402
from mop.common.domain import JobMeta  # noqa: E402
from mop.server import cluster, grants, image, spec  # noqa: E402

ALPHA = "git@h:g/alpha.git"
HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                  r"[0-9a-f]{12}$")
# Каталог ролей этого пакета: поставляемые пресеты #579.
PACKAGED = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                        "mop", "skills")


def role(name):
    """Бандл поставляемой роли по имени: (манифест-словарь, промпт,
    digest). Манифест -- разобранный soul.yaml тем же правилом, каким
    его закрепляет сервер: digest считается по сырым значениям (#580)."""
    import yaml
    path = os.path.join(PACKAGED, name)
    text = open(os.path.join(path, "soul.yaml"), encoding="utf-8").read()
    doc = yaml.safe_load(text)
    prompt = open(os.path.join(path, soulmod.parse(text).prompt), "rb").read()
    return doc, prompt, soulmod.digest(doc, prompt)


PUPPET_MANIFEST, PUPPET_PROMPT, PUPPET_DIGEST = role("puppet")
MASTER_MANIFEST, MASTER_PROMPT, MASTER_DIGEST = role("master")


def seed_grants(root, digest=PUPPET_DIGEST, manifest=PUPPET_MANIFEST,
                prompt=PUPPET_PROMPT):
    """Ничейный закреплённый бандл: строки реестра здесь нет, только
    хранилище бандлов -- этого спеке и ростеру и надо."""
    grants.prepare("alpha", role="puppet", digest=digest, manifest=manifest,
                   prompt=prompt, owner="op", request_id=f"seed-{digest[:8]}",
                   job="pu-alpha-seed", soul_inc=soulmod.incarnation(),
                   intent=f"seed-{digest[:8]}", root=root)


@contextlib.contextmanager
def soul_env(api, limit=None, roles=True, root=None, catalogue=(PACKAGED,)):
    """Среда глаголов add/update/delete/ping: снимок реестра с одним
    alpha (limit -- потолок), настройка роли сервера, корень реестра
    привязок и каталог ролей -- швы #582, Nomad -- параметром."""
    d = tempfile.mkdtemp(prefix="mop-test-582r-")
    g = root or tempfile.mkdtemp(prefix="mop-test-582g-")
    try:
        with open(os.path.join(d, "projects.json"), "w") as f:
            json.dump({"version": 1,
                       "projects": [{"origin": ALPHA, "limit": limit}
                                    if limit is not None else
                                    {"origin": ALPHA}]}, f)

        real_get = cluster.config.get

        def setting(name, default=None):
            # Шов -- только роль; остальное решает настоящая настройка:
            # подсунув сюда свой дефолт, проверка сломала бы чужие пути
            # (MOP_SERVER_LAN у кредов и т.п.).
            if name == "MOP_SOUL_ROLES":
                return "1" if roles else ""
            return real_get(name, default)
        with patched(projects, SNAPSHOT_DIR=d), \
                patched(cluster.config, get=setting), \
                patched(cluster, SOUL_ROOT=g), \
                patched(cluster, SOUL_CATALOGUE=list(catalogue)), \
                patched(spec, SOUL_STORE=g):
            yield g
    finally:
        shutil.rmtree(d, ignore_errors=True)
        if root is None:
            shutil.rmtree(g, ignore_errors=True)


def soul_job(name="pu-alpha-1", origin=ALPHA, branch=None, digest=PUPPET_DIGEST):
    """Джоб с тройкой роли в Meta, как его оставил бы add #582. Type --
    service: ростер видит только такие (visible), и без него проверка
    молчала бы -- зелёной не становясь."""
    meta = {"origin": origin, "soul": "puppet", "soul_digest": digest,
            "soul_inc": soulmod.incarnation()}
    if branch:
        meta["branch"] = branch
    return {"ID": name, "Status": "run", "Type": "service", "Meta": meta}


def answer(api, project, req):
    """Глагол через cluster.answer с явным Nomad: сеть не нужна."""
    return cluster.answer(project, req, api=api)


def try_spec(c, what, **kw):
    """job_spec с тройкой либо громкое красное имя: шов #582 ещё не
    реализован -- TypeError обязан стать провалом проверки, а не смертью
    файла (остальные проверки тоже должны сказать своё красное слово)."""
    try:
        return spec.job_spec(**kw)
    except TypeError as e:
        c.fail(what, f"TypeError: {e}")
        return None


# ── спека: тройка в Meta, размещение по способности драйвера ─────────────
def check_spec_triple_582(c):
    """job_spec с тройкой пишет её в Meta целиком, не трогая окружение
    задачи (версия шаблона та же -- масса-перерегистрация не нужна), и
    требует изоляционный драйвер способностью, а не именем роли."""
    triple = dict(soul="puppet", soul_digest=PUPPET_DIGEST,
                  soul_inc=soulmod.incarnation())
    g = tempfile.mkdtemp(prefix="mop-test-582t-")
    try:
        with patched(spec, SOUL_STORE=g):
            seed_grants(g)
            with_role = try_spec(c, "#582 job_spec must accept the soul triple",
                                 name="pu-alpha-1", origin=ALPHA, branch="b1",
                                 **triple)
            legacy = spec.job_spec("pu-alpha-1", ALPHA, branch="b1")["Job"]
        if with_role is None:
            return
        with_role = with_role["Job"]
        m = with_role["Meta"]
        c.check("#582 the triple rides Meta whole (soul, soul_digest, soul_inc)",
                (m.get("soul"), m.get("soul_digest"), m.get("soul_inc"))
                == ("puppet", PUPPET_DIGEST, triple["soul_inc"]), m)
        c.check("#582 a legacy spec carries no soul keys at all",
                not [k for k in legacy["Meta"] if k.startswith("soul")],
                legacy["Meta"])
        c.check("#582 the role does not touch the task env or the template version",
                with_role["TaskGroups"][0]["Tasks"][0]["Env"]
                == legacy["TaskGroups"][0]["Tasks"][0]["Env"]
                and with_role["Meta"][spec.SPEC_META]
                == legacy["Meta"][spec.SPEC_META])
        # Любой драйвер (puppet): дополнительного ограничения нет -- роль не
        # должна выдавать имя за привилегию размещения (#579).
        cons = [x["LTarget"] for x in with_role["Constraints"]]
        c.check("#582 an any-driver role adds no placement constraint",
                cons == [x["LTarget"] for x in legacy["Constraints"]], cons)
    finally:
        shutil.rmtree(g, ignore_errors=True)


def check_spec_isolating_582(c):
    """Изолирующая роль (master: driver: isolating) размещается только на
    драйвере с отдельными телами: ограничение строится по способности
    реестра драйверов (IS_CONTAINER), не по имени роли. Бандла нет --
    громкий отказ: молча поставить на host значило бы соседство без
    изоляции (факт 2 #579)."""
    g = tempfile.mkdtemp(prefix="mop-test-582i-")
    try:
        with patched(spec, SOUL_STORE=g):
            seed_grants(g, digest=MASTER_DIGEST, manifest=MASTER_MANIFEST,
                        prompt=MASTER_PROMPT)
            got = try_spec(c, "#582 job_spec must accept the soul triple",
                           name="pu-alpha-1", origin=ALPHA, soul="master",
                           soul_digest=MASTER_DIGEST,
                           soul_inc=soulmod.incarnation())
            if got is None:
                return
            got = got["Job"]
            by_target = {x["LTarget"]: x for x in got["Constraints"]}
            iso = by_target.get("${meta.mop_driver}")
            c.check("#582 an isolating role demands a container driver "
                    "by node meta, listing the capable ones",
                    iso is not None and iso["Operand"] == "regexp"
                    and "pve" in iso["RTarget"] and "host" not in iso["RTarget"],
                    list(by_target))
            # Хозяина изоляционных драйверов нет -- отказ до Nomad, а не
            # тихо непланируемый джоб.
            from mop import driver as drv
            with patched(drv, drivers=lambda: {"host": {"is_container": False}}):
                try:
                    spec.job_spec("pu-alpha-2", ALPHA, soul="master",
                                  soul_digest=MASTER_DIGEST,
                                  soul_inc=soulmod.incarnation())
                except RuntimeError as e:
                    c.check("#582 no isolating driver in the pool is a loud "
                            "refusal", "isolating" in str(e).lower(), str(e))
                except TypeError as e:
                    c.fail("#582 no isolating driver in the pool is a loud "
                           "refusal", f"TypeError: {e}")
                else:
                    c.fail("#582 no isolating driver in the pool is a loud "
                           "refusal", "спека собралась молча")
            # Роль «any» читает тот же бандл: без него -- тоже отказ.
            try:
                spec.job_spec("pu-alpha-3", ALPHA, soul="puppet",
                              soul_digest="c" * 64,
                              soul_inc=soulmod.incarnation())
            except RuntimeError as e:
                c.check("#582 a triple without its pinned bundle "
                        "is a loud refusal", "bundle" in str(e).lower(), str(e))
            except TypeError as e:
                c.fail("#582 a triple without its pinned bundle is a loud "
                       "refusal", f"TypeError: {e}")
            else:
                c.fail("#582 a triple without its pinned bundle is a loud "
                       "refusal", "спека собралась без бандла")
    finally:
        shutil.rmtree(g, ignore_errors=True)


def check_respec_keeps_triple_582(c):
    """respec -- одна дорога перерегистрации (#265): тройка из Meta
    переживает её, как ветку. Бандл для размещения -- тот же корень."""
    g = tempfile.mkdtemp(prefix="mop-test-582k-")
    try:
        with patched(spec, SOUL_STORE=g):
            seed_grants(g)
            meta = JobMeta.from_job(soul_job(branch="swarm"))
            got = spec.respec("pu-alpha-1", meta)["Job"]
            c.check("#582 respec carries the whole triple from the job meta",
                    (got["Meta"].get("soul"), got["Meta"].get("soul_digest"),
                     got["Meta"].get("soul_inc"))
                    == (meta.soul, meta.soul_digest, meta.soul_inc)
                    and got["Meta"].get("branch") == "swarm", got["Meta"])
            legacy = spec.respec("pu-alpha-1", JobMeta(ALPHA, "dev"))["Job"]
            c.check("#582 respec of a legacy job stays legacy",
                    not [k for k in legacy["Meta"] if k.startswith("soul")],
                    legacy["Meta"])
    finally:
        shutil.rmtree(g, ignore_errors=True)


# ── add: резерв до Nomad, идемпотентность, источник бандла ───────────────
def check_add_soul_582(c):
    """add с ролью: тройка в Meta зарегистрированного джоба, принципал
    активен ТОЛЬКО после Nomad, ответ эхом несёт имя роли -- клиент
    неполного ответа не пересоздаёт, а сверяется (#582)."""
    api = FakeNomad()
    with soul_env(api) as g:
        got = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                    "soul": "puppet",
                                    "request_id": "launch-1",
                                    "_caller": "anton"})
        job = (api.jobs.get(got.get("name")) or {}).get("Meta") or {}
        rec = next((grants.binding(p, root=g)
                    for p in _principals(g)), None)
        c.check("#582 add registers the job with the whole triple in Meta",
                bool(HEX64.match(job.get("soul_digest") or ""))
                and job.get("soul") == "puppet"
                and bool(UUID.match(job.get("soul_inc") or "")), job)
        c.check("#582 add echoes the triple with the name",
                got.get("ok") and bool(HEX64.match(got.get("soul_digest") or ""))
                and got.get("soul") == "puppet"
                and bool(UUID.match(got.get("soul_inc") or ""))
                and got.get("name"), got)
        c.check("#582 the binding is active and pins job, inc and owner",
                rec is not None and rec.get("active") is True
                and rec.get("job") == got.get("name")
                and rec.get("soul_inc") == job.get("soul_inc")
                and rec.get("soul_digest") == job.get("soul_digest")
                and rec.get("owner") == "anton", rec)


def _principals(root):
    """Все принципалы реестра привязок в этом корне."""
    path = os.path.join(root, "registry.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(json.load(f))


def check_add_pending_before_nomad_582(c):
    """Отказ Nomad оставляет резерв: запись неактивна, имя и инкарнация
    сохранены. Ретрай того же request_id поднимает СОХРАНЕННОГО
    принципала с тем же именем -- не второго на то же место; новый
    request_id тем временем выбирает другое имя."""
    class _OnceBroken(FakeNomad):
        def __init__(self):
            super().__init__()
            self.broken = True

        def register(self, sp):
            if self.broken:
                self.broken = False
                raise RuntimeError("nomad is down for a moment")
            super().register(sp)

    api = _OnceBroken()
    with soul_env(api) as g:
        first = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "soul": "puppet",
                                      "request_id": "launch-1",
                                      "_caller": "anton"})
        pending = [grants.binding(p, root=g) for p in _principals(g)]
        c.check("#582 a refused registration leaves a pending, inactive "
                "reservation with the name and inc saved",
                "error" in first and len(pending) == 1
                and pending[0].get("active") is False
                and pending[0].get("job") and pending[0].get("soul_inc"),
                (first, pending))
        retry = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "soul": "puppet",
                                      "request_id": "launch-1",
                                      "_caller": "anton"})
        after = [grants.binding(p, root=g) for p in _principals(g)]
        c.check("#582 the retry reconciles the saved principal and inc, "
                "not a second one",
                retry.get("ok") and len(after) == 1
                and after[0].get("active") is True
                and after[0].get("job") == retry.get("name")
                and after[0].get("soul_inc") == retry.get("soul_inc")
                and retry.get("name") == (pending[0] or {}).get("job"),
                (retry, after))
        # Захваченное имя чужим запросом не переезжает молча: имя из
        # незавершённого резерва занято ЧУЖОЙ регистрацией -- ретрай того
        # же request_id обязан отказаться громко ДО Nomad, а не поднять
        # третью душу на одном джобе. Регистратор здесь всегда сломан:
        # доехавший до Nomad ретрай умер бы чужой ошибкой, а не отказом
        # о имени.
        api2 = _OnceBroken()
        with soul_env(api2) as g2:
            answer(api2, "alpha", {"verb": "add", "origin": ALPHA,
                                   "soul": "puppet", "request_id": "solo-1",
                                   "_caller": "anton"})
            thief = answer(api2, "alpha", {"verb": "add", "origin": ALPHA,
                                           "_caller": "anton"})
            api2.broken = True
            again = answer(api2, "alpha", {"verb": "add", "origin": ALPHA,
                                           "soul": "puppet",
                                           "request_id": "solo-1",
                                           "_caller": "anton"})
            c.check("#582 a reserved name taken by another registration "
                    "is a loud refusal on retry",
                    "error" in again and "pu-alpha-1" in again.get("error", "")
                    and thief.get("name") == "pu-alpha-1"
                    and len(_principals(g2)) == 1, (thief, again))


def check_add_request_payload_582(c):
    """Тот же request_id с другой полезной нагрузкой -- отказ ДО
    регистрации: «похожий» запуск -- другой запрос (#581 через #582)."""
    api = FakeNomad()
    with soul_env(api) as g:
        first = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "soul": "puppet",
                                      "request_id": "launch-1",
                                      "branch": "one", "_caller": "anton"})
        shifted = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                        "soul": "puppet",
                                        "request_id": "launch-1",
                                        "branch": "two", "_caller": "anton"})
        c.check("#582 the same request id with another payload is refused "
                "before any second registration",
                first.get("ok") and "error" in shifted
                and len(_principals(g)) == 1
                and sorted(api.jobs) == [first.get("name")], (first, shifted))


def check_add_source_gates_582(c):
    """Источник роли -- каталог сервера: неизвестное имя -- отказ со
    списком; клиент не называет ни digest, ни инкарнацию (их выбирает
    сервер); выключенная настройка -- путь роли закрыт и в ping, и в add,
    легаси-add при этом жив."""
    api = FakeNomad()
    with soul_env(api, roles=False) as g:
        off = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                    "soul": "puppet", "request_id": "r",
                                    "_caller": "anton"})
        c.check("#582 a disabled role path refuses the soul add",
                "error" in off and not api.jobs
                and not _principals(g), (off, sorted(api.jobs)))
        ping = answer(api, "alpha", {"verb": "ping"})
        c.check("#582 a server with the role path off does not advertise "
                "the protocol", "soul_protocol" not in ping, ping.get("soul_protocol"))
        legacy = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                       "_caller": "anton"})
        c.check("#582 legacy add works with the role path off",
                legacy.get("ok") and not _principals(g), legacy)
    api = FakeNomad()
    with soul_env(api) as g:
        unknown = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                        "soul": "ghost", "request_id": "r",
                                        "_caller": "anton"})
        c.check("#582 an unknown role is refused with the catalogue hint",
                "error" in unknown and not api.jobs
                and not _principals(g)
                and any(n in (unknown.get("error") or "")
                        for n in ("puppet", "master", "builder")), unknown)
        pinned = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                       "soul": "puppet",
                                       "soul_digest": "d" * 64,
                                       "request_id": "r2", "_caller": "anton"})
        c.check("#582 the client cannot pin the digest or the incarnation",
                "error" in pinned and not api.jobs, pinned)
        ping = answer(api, "alpha", {"verb": "ping"})
        c.check("#582 an enabled server advertises soul_protocol "
                "v1 with request_id",
                ping.get("soul_protocol") == {"version": 1, "request_id": True},
                ping.get("soul_protocol"))


def check_add_legacy_untouched_582(c):
    """Легаси-add при включённой настройке -- прежний путь байт в байт:
    ни тройки, ни принципала, ни записи реестра."""
    api = FakeNomad()
    with soul_env(api) as g:
        got = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                    "_caller": "anton"})
        meta = (api.jobs.get(got.get("name")) or {}).get("Meta") or {}
        c.check("#582 a legacy add registers no soul and no grant",
                got.get("ok") and "soul" not in meta and not _principals(g)
                and "soul" not in got, (got, meta))


def check_add_workspace_boundary_582(c):
    """Роль не поднимается клиентским текстом (#579): add с душой и
    workspace/bootstrap_sent -- отказ до всего, кто бы ни просил -- у роли
    bootstrap серверный, операторски согласованный. Делегированный
    принципал до глагола вовсе не доезжает: легаси-rpc для него закрыт
    ограждением #603 (здесь проверяем, что ограждение стоит; канал роли --
    #603). Человек сохраняет сегодняшний путь: легаси-add со своим текстом."""
    api = FakeNomad()
    with soul_env(api) as g:
        refused = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                        "soul": "puppet", "request_id": "r",
                                        "workspace": "- name: evil\n",
                                        "_caller": "anton"})
        c.check("#582 a role add with client workspace text is refused "
                "whoever asks",
                "error" in refused and not api.jobs
                and not _principals(g), refused)
        fenced = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                       "soul": "puppet", "request_id": "r2",
                                       "_caller": "grant-" + "a" * 32})
        c.check("#582 a grant principal does not reach the legacy add "
                "at all (#603 fence)",
                "error" in fenced
                and "role channel" in fenced.get("error", "")
                and not api.jobs and not _principals(g), fenced)
        human = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "_caller": "anton",
                                      "workspace": "- name: play\n  tasks: []\n"})
        c.check("#582 the human keeps today's legacy workspace path",
                human.get("ok") and len(api.jobs) == 1, human)


def check_add_quota_counts_roles_582(c):
    """Квота считает все роли: лимит 1, жив легаси -- soul-add отказан;
    два одновременных soul-add на последний слот -- ровно один проходит
    (ворота #567 держат и путь роли)."""
    api = FakeNomad(jobs={"pu-alpha-1": {"ID": "pu-alpha-1", "Status": "run",
                                         "Type": "service",
                                         "Meta": {"origin": ALPHA}}})
    with soul_env(api, limit=1):
        got = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                    "soul": "puppet", "request_id": "r",
                                    "_caller": "anton"})
        c.check("#582 the quota counts role puppets against the same limit",
                "error" in got and "limit" in got.get("error", ""), got)

    api = FakeNomad()
    out = {}

    def one(tag, rid):
        out[tag] = cluster.answer("alpha", {"verb": "add", "origin": ALPHA,
                                            "soul": "puppet", "request_id": rid,
                                            "_caller": "anton"}, api=api)
    with soul_env(api, limit=1):
        ts = [threading.Thread(target=one, args=(t, f"race-{t}"))
              for t in ("a", "b")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=15)
        ok = [v for v in out.values() if v.get("ok")]
        refused = [v for v in out.values() if not v.get("ok")]
        c.check("#582 two racing soul adds on the last slot yield one puppet",
                len(ok) == 1 and len(refused) == 1
                and "limit" in refused[0].get("error", "")
                and sorted(api.jobs) == [ok[0].get("name")], out)


# ── сохранение тройки: update, restart, рецикл, сборка образа ────────────
def check_update_preserves_triple_582(c):
    """update без полей роли сохраняет тройку (как ветку #257) и узел
    (#289); явная та же роль -- допустима; чужая роль или другой digest
    -- отказ до перерегистрации: смена роли -- новый папет, у живой
    инкарнации свой бандл (#579)."""
    g = tempfile.mkdtemp(prefix="mop-test-582u-")
    api = FakeNomad(jobs={"pu-alpha-1": soul_job(branch="swarm")},
                    allocs={"pu-alpha-1": {"ID": "a1", "NodeName": "hyper"}})
    try:
        with patched(spec, SOUL_STORE=g), patched(cluster, SOUL_ROOT=g):
            seed_grants(g)
            got = answer(api, "alpha", {"verb": "update", "name": "pu-alpha-1",
                                        "origin": ALPHA, "force": True,
                                        "_caller": "anton"})
            meta = (api.jobs.get("pu-alpha-1") or {}).get("Meta") or {}
            old = api.jobs["pu-alpha-1"]["Meta"]
            c.check("#582 update without role fields keeps the triple "
                    "and the branch",
                    got.get("ok") and meta.get("soul_digest") == old["soul_digest"]
                    and meta.get("soul_inc") == old["soul_inc"]
                    and meta.get("branch") == "swarm", (got, meta))
            same = answer(api, "alpha", {"verb": "update", "name": "pu-alpha-1",
                                         "origin": ALPHA, "soul": "puppet",
                                         "force": True,
                                         "_caller": "anton"})
            c.check("#582 update naming the same role is a no-op, not a refusal",
                    same.get("ok"), same)
            registered = []
            api.register = lambda sp: registered.append(sp) or {}
            for what, req in (
                    ("another role",
                     {"verb": "update", "name": "pu-alpha-1", "origin": ALPHA,
                      "soul": "master", "force": True}),
                    ("another digest",
                     {"verb": "update", "name": "pu-alpha-1", "origin": ALPHA,
                      "soul_digest": "e" * 64, "force": True})):
                got = answer(api, "alpha", dict(req, _caller="anton"))
                c.check(f"#582 update with {what} is refused before "
                        "re-registration",
                        "error" in got and not registered, (what, got))
    finally:
        shutil.rmtree(g, ignore_errors=True)


def check_restart_keeps_meta_582(c):
    """restart -- аллокация, не спека: Meta не пересобирается, тройка
    остаётся байт в байт, register не зовётся."""
    api = FakeNomad(jobs={"pu-alpha-1": soul_job()},
                    allocs={"pu-alpha-1": {"ID": "a1", "NodeName": "hyper"}})
    before = dict(api.jobs["pu-alpha-1"]["Meta"])
    got = answer(api, "alpha", {"verb": "restart", "name": "pu-alpha-1",
                                "force": True, "_caller": "anton"})
    c.check("#582 restart leaves the job meta untouched",
            got.get("ok") and api.jobs["pu-alpha-1"]["Meta"] == before
            and not [x for x in api.calls if x[0] == "register"],
            (got, api.jobs["pu-alpha-1"]["Meta"]))


def check_recycle_road_keeps_triple_582(c):
    """Дорога рецикла -- delete(purge=False) + update: тройка и ветка
    переживают её, инкарнация та же -- рецикл чистит тело, не душу."""
    g = tempfile.mkdtemp(prefix="mop-test-582y-")
    api = FakeNomad(jobs={"pu-alpha-1": soul_job(branch="swarm")})
    try:
        with patched(spec, SOUL_STORE=g), patched(cluster, SOUL_ROOT=g), \
                patched(cluster.bootstrap, ROOT=tempfile.mkdtemp()):
            seed_grants(g)
            gone = answer(api, "alpha", {"verb": "delete", "name": "pu-alpha-1",
                                         "purge": False, "_caller": "anton"})
            back = answer(api, "alpha", {"verb": "update", "name": "pu-alpha-1",
                                         "origin": ALPHA, "force": True,
                                         "_caller": "anton"})
            meta = (api.jobs.get("pu-alpha-1") or {}).get("Meta") or {}
            c.check("#582 the recycle road keeps the same incarnation",
                    gone.get("ok") and back.get("ok")
                    and meta.get("soul_inc")
                    and meta.get("branch") == "swarm", (gone, back, meta))
    finally:
        shutil.rmtree(g, ignore_errors=True)


def check_image_roundtrip_582(c):
    """Сборка образа: журнал уносит тройку со снятым, подъём возвращает
    её в Meta -- папет поднимается со своей душой и своей веткой (#265
    для роли)."""
    g = tempfile.mkdtemp(prefix="mop-test-582m-")
    job = soul_job(branch="swarm")
    rows = [{"name": "pu-alpha-1", "node": "hyper", "job": job,
             "container": True, "state": "free", "kind": "free"}]
    from mop.common import puppets

    def wait(name):
        return None

    def wipe(node, name, *a, **k):
        return None
    try:
        with patched(spec, SOUL_STORE=g):
            seed_grants(g)
            api = FakeNomad(jobs={"pu-alpha-1": job})
            with patched(image, project_rows=lambda project, api=None: rows), \
                    patched(puppets, _wait_stopped=wait, wipe=wipe):
                gone = image.clear("alpha", api=api)
            c.check("#582 the build journal carries the whole triple",
                    len(gone) == 1 and gone[0].get("soul") == "puppet"
                    and gone[0].get("soul_digest") == job["Meta"]["soul_digest"]
                    and gone[0].get("soul_inc") == job["Meta"]["soul_inc"]
                    and gone[0].get("branch") == "swarm", gone)
            fresh = FakeNomad()
            image.restore(gone, api=fresh)
            meta = (fresh.jobs.get("pu-alpha-1") or {}).get("Meta") or {}
            c.check("#582 restore raises the puppet with its soul and branch",
                    meta.get("soul_digest") == job["Meta"]["soul_digest"]
                    and meta.get("soul_inc") == job["Meta"]["soul_inc"]
                    and meta.get("branch") == "swarm", meta)
    finally:
        shutil.rmtree(g, ignore_errors=True)


# ── удаление и ростер ────────────────────────────────────────────────────
def check_delete_revokes_binding_582(c):
    """Снятый папет не оставляет живого принципала: delete отзывает
    привязку (глагольный отзыв #581) -- активных кредов на несуществующий
    джоб нет; легаси-delete реестра не касается."""
    g = tempfile.mkdtemp(prefix="mop-test-582d-")
    api = FakeNomad(jobs={"pu-alpha-1": soul_job(),
                          "pu-alpha-2": {"ID": "pu-alpha-2", "Status": "run",
                                         "Type": "service",
                                         "Meta": {"origin": ALPHA}}})
    try:
        with patched(cluster, SOUL_ROOT=g):
            seed_grants(g)
            principal = grants.prepare("alpha", role="puppet",
                                       digest=PUPPET_DIGEST,
                                       manifest=PUPPET_MANIFEST,
                                       prompt=PUPPET_PROMPT, owner="anton",
                                       request_id="live-1", job="pu-alpha-1",
                                       soul_inc=soulmod.incarnation(),
                                       intent="live-1", root=g)["principal"]
            grants.activate(principal, root=g)
            gone = answer(api, "alpha", {"verb": "delete", "name": "pu-alpha-1",
                                         "_caller": "anton"})
            rec = grants.binding(principal, root=g)
            c.check("#582 deleting a soul puppet revokes its binding",
                    gone.get("ok") and rec is not None
                    and rec.get("active") is False, (gone, rec))
            legacy = answer(api, "alpha", {"verb": "delete", "name": "pu-alpha-2",
                                           "_caller": "anton"})
            c.check("#582 deleting a legacy puppet touches no binding",
                    legacy.get("ok"), legacy)
    finally:
        shutil.rmtree(g, ignore_errors=True)


def check_roster_mop_soul_582(c):
    """Ростер проецирует receives_dispatch из ЗАКРЕПЛЁННОГО бандла -- не
    из самодекларации: роль-джоб несёт MopSoul, легаси -- нет, бандл
    потерян -- поля нет (autopick такое не выбирает, fail closed)."""
    g = tempfile.mkdtemp(prefix="mop-test-582p-")
    master_job = soul_job("pu-alpha-1", digest=MASTER_DIGEST)
    master_job["Meta"]["soul"] = "master"
    orphan = soul_job("pu-alpha-3", digest="0" * 64)
    puppet_job = soul_job("pu-alpha-2")
    api = FakeNomad(jobs={"pu-alpha-1": master_job,
                          "pu-alpha-2": puppet_job,
                          "pu-alpha-3": orphan,
                          "pu-alpha-4": {"ID": "pu-alpha-4", "Status": "run",
                                         "Type": "service",
                                         "Meta": {"origin": ALPHA}}})
    try:
        with patched(cluster, SOUL_ROOT=g), patched(spec, SOUL_STORE=g):
            seed_grants(g)
            seed_grants(g, digest=MASTER_DIGEST, manifest=MASTER_MANIFEST,
                        prompt=MASTER_PROMPT)
            with cluster.using(api):
                items = {i["job"]["ID"]: i["job"]
                         for i in cluster.nomad_items("alpha")}
            c.check("#582 the roster carries MopSoul.receives_dispatch "
                    "from the pinned bundle",
            items.get("pu-alpha-2", {}).get("MopSoul")
                    == {"receives_dispatch": True}
            and items.get("pu-alpha-1", {}).get("MopSoul")
                    == {"receives_dispatch": False}, {n: items.get(n, {}).get("MopSoul")
                                                      for n in items})
            c.check("#582 legacy jobs and lost bundles carry no MopSoul",
                    "MopSoul" not in items.get("pu-alpha-4", {})
                    and "MopSoul" not in items.get("pu-alpha-3", {}),
                    {n: items.get(n, {}).get("MopSoul") for n in items})
    finally:
        shutil.rmtree(g, ignore_errors=True)


def main():
    c = Checks()
    for fn in (check_spec_triple_582, check_spec_isolating_582,
               check_respec_keeps_triple_582,
               check_add_soul_582, check_add_pending_before_nomad_582,
               check_add_request_payload_582, check_add_source_gates_582,
               check_add_legacy_untouched_582, check_add_workspace_boundary_582,
               check_add_quota_counts_roles_582,
               check_update_preserves_triple_582, check_restart_keeps_meta_582,
               check_recycle_road_keeps_triple_582, check_image_roundtrip_582,
               check_delete_revokes_binding_582, check_roster_mop_soul_582):
        fn(c)
    return c.report("soul_lifecycle")


if __name__ == "__main__":
    sys.exit(main())
