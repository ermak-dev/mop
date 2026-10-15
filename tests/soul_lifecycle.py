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
STATUS: FIXED — делегированный add по каналу роли: родитель из реестра,
роль цели по spawn бандла, снапшот согласованной политики наследуется
и закрепляется за запросом (метка с хешем снапшота, bootstrap.py),
актёр отдельным полем от владельца (grants.prepare actor=). См. #582.
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
from mop import driver  # noqa: E402
from mop.server import bootstrap, cluster, grants, image, spec  # noqa: E402

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

# Согласованные политики родителя для делегированных проверок: текст
# .mop/bootstrap.yaml, какой сервер хранит под ИМЕНЕМ папета (#133),
# а не проекта.
PLAY_A = "- vars:\n    answer: 42\n  tasks:\n  - name: greet\n    shell: echo hi\n"
PLAY_B = "- vars:\n    answer: 43\n  tasks:\n  - name: bye\n    shell: echo bye\n"


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
    привязок, каталог ролей и корень bootstrap -- швы #582, Nomad --
    параметром. Корень bootstrap свой: делегированный add читает
    согласованный снапшот родителя из реального хранилища, и проверка
    обязана держать его под контролем, а не в чужом HOME."""
    d = tempfile.mkdtemp(prefix="mop-test-582r-")
    g = root or tempfile.mkdtemp(prefix="mop-test-582g-")
    b = tempfile.mkdtemp(prefix="mop-test-582b-")
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
                patched(spec, SOUL_STORE=g), \
                patched(bootstrap, ROOT=b):
            yield g
    finally:
        shutil.rmtree(d, ignore_errors=True)
        if root is None:
            shutil.rmtree(g, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


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
        # PuSOUL: спека обязана нести всю тройку и в Meta, и в окружении --
        # с одним digest, одной инкарнацией.
        c.check("#582 Meta triple and env triple carry the same soul",
                (m.get("soul"), m.get("soul_digest"), m.get("soul_inc"))
                == (triple["soul"], triple["soul_digest"], triple["soul_inc"])
                and with_role["TaskGroups"][0]["Tasks"][0]["Env"]["PU_SOUL"]
                == triple["soul"]
                and with_role["TaskGroups"][0]["Tasks"][0]["Env"]["PU_SOUL_DIGEST"]
                == triple["soul_digest"]
                and with_role["TaskGroups"][0]["Tasks"][0]["Env"]["PU_SOUL_INC"]
                == triple["soul_inc"],
                with_role["TaskGroups"][0]["Tasks"][0]["Env"].get("PU_SOUL_DIGEST"))
        c.check("#582 the triple rides Meta whole (soul, soul_digest, soul_inc)",
                (m.get("soul"), m.get("soul_digest"), m.get("soul_inc"))
                == ("puppet", PUPPET_DIGEST, triple["soul_inc"]), m)
        c.check("#582 a legacy spec carries no soul keys at all",
                not [k for k in legacy["Meta"] if k.startswith("soul")],
                legacy["Meta"])
        # Транспорт тела (#582, для #590/#583): тройка едет окружением
        # PU_SOUL*, несекретно, и БОЛЬШЕ НИЧЕГО в окружении не меняется --
        # версия шаблона та же (масс-перерегистрация не нужна), легаси-
        # ключи те же байт в байт. Легаси-спека несёт PU_SOUL пустыми:
        # врапер одного поля на оба пути.
        role_env = with_role["TaskGroups"][0]["Tasks"][0]["Env"]
        legacy_env = legacy["TaskGroups"][0]["Tasks"][0]["Env"]
        transport = {k: v for k, v in role_env.items()
                     if k.startswith("PU_SOUL")}
        c.check("#582 the role adds exactly the PU_SOUL transport keys, "
                "the rest of the env and the template version unchanged",
                transport == {"PU_SOUL": "puppet",
                              "PU_SOUL_DIGEST": PUPPET_DIGEST,
                              "PU_SOUL_INC": triple["soul_inc"],
                              "PU_SOUL_DIR": driver.soul_dir(
                                  "pu-alpha-1", triple["soul_inc"])}
                and {k: v for k, v in role_env.items()
                     if not k.startswith("PU_SOUL")}
                == {k: v for k, v in legacy_env.items()
                    if not k.startswith("PU_SOUL")}
                and "PU_SOUL" in legacy_env
                and legacy_env["PU_SOUL"] == ""
                and "PU_SOUL_DIR" in legacy_env
                and legacy_env["PU_SOUL_DIR"] == ""
                and with_role["Meta"][spec.SPEC_META]
                == legacy["Meta"][spec.SPEC_META],
                {k: v for k, v in role_env.items()
                 if legacy_env.get(k) != v})
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


@contextlib.contextmanager
def extra_project(origin):
    """Ещё один проект в снимке реестра ДЕЙСТВУЮЩЕЙ soul_env: beta,
    зарегистрированная на сервере, -- чтобы отказ по чужому origin был
    отказом по ГРАНИЦЕ, а не по «нет такого проекта»."""
    path = os.path.join(projects.SNAPSHOT_DIR, "projects.json")
    with open(path) as f:
        doc = json.load(f)
    doc["projects"].append({"origin": origin})
    with open(path, "w") as f:
        json.dump(doc, f)
    try:
        yield
    finally:
        doc["projects"].pop()
        with open(path, "w") as f:
            json.dump(doc, f)


def soul_parent(api, g, request_id="parent-1", workspace=PLAY_A):
    """Живой родитель-мастер со согласованной политикой: add ЧЕЛОВЕКОМ --
    аутентифицированное создание роли сохраняет путь согласования
    workspace (мастер-ревью #582): текст человека и есть одобрение.
    workspace -- текст политики; "" -- явно согласованное пусто (mop без
    .mop/bootstrap.yaml); None -- поля нет вовсе, политика не заявлена.
    -> (принципал, имя, ответ); отказ -- принципал None, ответ объяснит."""
    fields = {"verb": "add", "origin": ALPHA, "soul": "master",
              "request_id": request_id, "_caller": "anton"}
    if workspace is not None:
        fields["workspace"] = workspace
    got = answer(api, "alpha", fields)
    if not got.get("ok"):
        return None, None, got
    rec = grants.by_job("alpha", got["name"], root=g)
    if not rec or not rec.get("active"):
        return None, got["name"], {"error": "parent grant is not active", "got": got}
    return rec["principal"], got["name"], got


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
            # Чужая регистрация на зарезервированном имени (#662: add
            # сам такое имя не выберет -- резерв виден заявкам роли), как
            # джоб, посаженный в Nomad мимо сервиса.
            api2.jobs["pu-alpha-1"] = {"ID": "pu-alpha-1", "Status": "run",
                                       "Type": "service",
                                       "Meta": {"origin": ALPHA}}
            api2.broken = True
            again = answer(api2, "alpha", {"verb": "add", "origin": ALPHA,
                                           "soul": "puppet",
                                           "request_id": "solo-1",
                                           "_caller": "anton"})
            c.check("#582 a reserved name taken by another registration "
                    "is a loud refusal on retry",
                    "error" in again and "pu-alpha-1" in again.get("error", "")
                    and len(_principals(g2)) == 1, again)


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
        c.check("#662 a roleless add is refused with the role pointer",
                bool(legacy.get("error")) and not api.jobs
                and not _principals(g)
                and all(w in legacy.get("error", "")
                        for w in ("role puppets only", "soul=")),
                legacy)
        off = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                    "_caller": "anton", "workspace":
                                    "- name: play\n  tasks: []\n"})
        c.check("#662 a roleless add with workspace text is refused before "
                "any effect",
                bool(off.get("error")) and not api.jobs
                and not any(f.endswith("-tasks.yml")
                            for f in os.listdir(bootstrap.ROOT)),
                off)
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


def check_add_roleless_refused_662(c):
    """HYPOTHESIS (#662): add без полей роли -- прежний легаси-путь:
    джоб без тройки, workspace-marker старины, ни принципала, ни реестра.
    SOLUTION: поля роли обязательны: запрос без них -- громкий отказ с
    указанием soul=<роль>, ДО ворот проекта (ни джоба, ни реестра, ни
    workspace). Сервер создаёт только ролевых папетов."""
    api = FakeNomad()
    with soul_env(api) as g:
        got = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                    "_caller": "anton"})
        c.check("#662 a roleless add is refused with the soul= pointer",
                bool(got.get("error")) and "soul=" in got.get("error", "")
                and "role puppets only" in got.get("error", "")
                and not api.jobs and not _principals(g), got)
        # Отказ -- до ворот проекта: workspace текста легаси-пути не
        # пишется вовсе.
        before = sorted(os.listdir(bootstrap.ROOT))
        seeded = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                       "_caller": "anton", "workspace":
                                       "- name: play\n  tasks: []\n"})
        after = sorted(os.listdir(bootstrap.ROOT))
        c.check("#662 a roleless add stores no workspace on the way out",
                bool(seeded.get("error"))
                and after == before, (seeded, before, after))


def check_add_delegated_582(c):
    """Делегированный add по каналу роли (#579/#603): принципал родителя
    поднимает ребёнка сам, сервер выбирает имя/инкарнацию/принципала,
    наследует СОГЛАСОВАННЫЙ снапшот bootstrap родителя (хранилище по
    ИМЕНИ папета, #133 -- не проекта), закрепляет его за запросом ретрая
    и НЕ принимает клиентский YAML ни в каком виде. Согласованная пустая
    политика -- законный случай (mop без .mop/bootstrap.yaml), её
    наследует ребёнок без файлов; отсутствие согласования -- отказ,
    а не тишина. Происхождение актёра хранится отдельно от хозяина:
    _caller принципала не становится человеческим владельцем и не
    открывает путь загрузки workspace."""
    # Живой родитель-мастер с согласованной политикой PLAY_A.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g)
        # Позитив: правильный канал роли, разрешённая роль цели, без
        # клиентского workspace. Ребёнок обязан появиться: джоб с тройкой,
        # активный грант, наследованные файлы bootstrap под ИМЕНЕМ ребёнка.
        got = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                    "soul": "puppet", "request_id": "child-1",
                                    "_caller": principal, "_subject":
                                    f"mop.alpha.cluster.role.{principal}"})
        child = got.get("name")
        child_meta = (api.jobs.get(child) or {}).get("Meta") or {}
        child_rec = grants.by_job("alpha", child, root=g) if child else None
        tasks, of_vars = bootstrap.files_of(bootstrap.ROOT, child or "")
        c.check("#582 a delegated add on the role channel creates a real "
                "role job",
                got.get("ok") and child_meta.get("soul") == "puppet"
                and bool(HEX64.match(child_meta.get("soul_digest") or ""))
                and bool(UUID.match(child_meta.get("soul_inc") or "")), got)
        c.check("#582 the delegated child's grant is active and owned by "
                "the human, provenance names the grant actor",
                child_rec is not None and child_rec.get("active") is True
                and child_rec.get("owner") == "anton"
                and child_rec.get("actor") == principal, child_rec)
        c.check("#582 the delegated child inherits the parent's approved "
                "snapshot under its own name",
                tasks is not None and of_vars is not None
                and open(tasks).read().find("greet") >= 0
                and open(of_vars).read().find("42") >= 0,
                (tasks, of_vars))
        # Ретрай того же request_id после смены политики родителя
        # наследует ЗАКРЕПЛЁННЫЙ снапшот, не свежий: ретрай не может
        # молча получить другую политику, чем первая попытка.
        retry = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "soul": "puppet", "request_id": "child-1",
                                      "_caller": principal, "_subject":
                                      f"mop.alpha.cluster.role.{principal}"})
        c.check("#582 a retry returns the reserved name, not a new child",
                retry.get("ok") and retry.get("name") == child, retry)

    # Смена политики родителя между попытками: новая попытка (другой
    # request_id) наследует свежий снапшот; закреплённый за старым
    # запросом остаётся прежним.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g, workspace=PLAY_A)
        # Первая попытка «упала в Nomad»: снапшот закреплён за child-x.
        class _NoReg(FakeNomad):
            def register(self, sp):
                raise RuntimeError("nomad is down")
        broken = _NoReg()
        broken.jobs = api.jobs
        first = answer(broken, "alpha", {"verb": "add", "origin": ALPHA,
                                         "soul": "puppet", "request_id": "child-x",
                                         "_caller": principal, "_subject":
                                         f"mop.alpha.cluster.role.{principal}"})
        # Политика родителя сменена человеком ПОСЛЕ резерва.
        bootstrap.store(bootstrap.ROOT, parent, PLAY_B)
        retry = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "soul": "puppet", "request_id": "child-x",
                                      "_caller": principal, "_subject":
                                      f"mop.alpha.cluster.role.{principal}"})
        rec = grants.by_request("child-x", root=g)
        tasks, of_vars = bootstrap.files_of(
            bootstrap.ROOT, (rec or {}).get("job") or "")
        c.check("#582 a pinned snapshot survives a parent policy change: "
                "the retry inherits what the first attempt pinned",
                "error" in first and retry.get("ok")
                and rec is not None and tasks is not None
                and open(tasks).read().find("greet") >= 0, (first, retry))

    # Согласованная пустая политика родителя: ребёнок без файлов -- это
    # наследование пустоты, не отказ и не тихая пропажа.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g, workspace="")
        got = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                    "soul": "puppet", "request_id": "child-e",
                                    "_caller": principal, "_subject":
                                    f"mop.alpha.cluster.role.{principal}"})
        tasks, of_vars = bootstrap.files_of(bootstrap.ROOT, got.get("name") or "")
        c.check("#582 an approved empty parent policy inherits cleanly",
                got.get("ok") and tasks is None and of_vars is None, got)

    # Родитель без согласования: согласование было и снято (файлы ушли,
    # как их снимает bootstrap.store с пустым текстом) -- делегированный
    # add отказан громко, ни джоба, ни резерва.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, got = soul_parent(api, g, workspace=PLAY_A)
        for suffix in ("tasks", "vars"):
            path = os.path.join(bootstrap.ROOT, f"{parent}-{suffix}.yml")
            if os.path.exists(path):
                os.unlink(path)
        child = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "soul": "puppet", "request_id": "nop-2",
                                      "_caller": principal, "_subject":
                                      f"mop.alpha.cluster.role.{principal}"})
        c.check("#582 an unapproved parent snapshot is refused loudly",
                "error" in child and not api.jobs.get("pu-alpha-2")
                and "approv" in (child.get("error") or ""), child)

    # Клиентский YAML от принципала -- отказ, не молчаливый игнор:
    # грант не грузит произвольный текст и не создаёт легаси-ребёнка.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g)
        refused = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                        "soul": "puppet", "request_id": "ev-1",
                                        "workspace": "- name: evil\n",
                                        "_caller": principal, "_subject":
                                        f"mop.alpha.cluster.role.{principal}"})
        c.check("#582 client YAML from a grant is rejected, not ignored",
                "error" in refused and "workspace" in (refused.get("error") or "")
                and len(api.jobs) == 1, refused)
        # Грант без души не создаёт легаси-ребёнка: omitting soul --
        # попытка обойти роль, а не легаси-добавление.
        unbound = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                        "_caller": principal, "_subject":
                                        f"mop.alpha.cluster.role.{principal}"})
        c.check("#582 a grant omitting soul creates no legacy child",
                "error" in unbound and len(api.jobs) == 1, unbound)

    # Разрешённая роль цели: spawn родителя. master позволяет puppet и
    # builder; попытка поднять master от master (spawn его не содержит)
    # -- отказ по роли цели, а не по грамматике субъекта.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g)
        denied = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                       "soul": "master", "request_id": "sp-1",
                                       "_caller": principal, "_subject":
                                       f"mop.alpha.cluster.role.{principal}"})
        c.check("#582 a spawn role outside the parent's allowlist is refused",
                "error" in denied and "spawn" in (denied.get("error") or "")
                and len(api.jobs) == 1, denied)
        # Чужой origin: граница проекта -- для всех актёров, делегированный
        # путь не открывает гранту проект-соседа. Beta ЗАРЕГИСТРИРОВАНА:
        # отказ по границе, а не «нет такого проекта».
        with extra_project("git@h:g/beta.git"):
            foreign = answer(api, "alpha", {"verb": "add",
                                            "origin": "git@h:g/beta.git",
                                            "soul": "puppet", "request_id": "sp-2",
                                            "_caller": principal, "_subject":
                                            f"mop.alpha.cluster.role.{principal}"})
        c.check("#582 a delegated add cannot cross the project boundary",
                "error" in foreign and "beta" in (foreign.get("error") or "")
                and len(api.jobs) == 1, foreign)

    # Человек без полей роли -- отказ #662: путь только ролевой, текст
    # легаси-пути больше никуда не кладётся.
    api = FakeNomad()
    with soul_env(api) as g:
        human = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "_caller": "anton",
                                      "workspace": "- name: play\n  tasks: []\n"})
        c.check("#662 a roleless add with workspace text is refused",
                bool(human.get("error")) and "soul=" in human.get("error", "")
                and len(api.jobs) == 0, human)


def check_add_delegated_gates_582(c):
    """Блокаторы ревью 809ffc7: шесть красных историй делегированного и
    человеческого add. Каждая -- про конкретный обход, не про стиль."""
    # 1a. Отозванный родитель на открытом соединении: свежая проверка
    #     привязки -- active и не отозвана -- ДО списка spawn.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g)
        grants.revoke(principal, root=g)
        got = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                    "soul": "puppet", "request_id": "rv-1",
                                    "_caller": principal, "_subject":
                                    f"mop.alpha.cluster.role.{principal}"})
        c.check("#582 a revoked parent cannot add a child",
                "error" in got and "revok" in (got.get("error") or "").lower()
                and len(api.jobs) == 1, got)
    # 1b. Родитель без ПРАВА add в cluster_verbs: рабочий (puppet) не
    #     поднимает детей даже по правильному каналу -- право у души.
    api = FakeNomad()
    with soul_env(api) as g:
        got = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                    "soul": "puppet", "request_id": "wk-1",
                                    "_caller": "anton"})
        assert got.get("ok"), got
        worker = grants.by_job("alpha", got["name"], root=g)["principal"]
        child = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "soul": "puppet", "request_id": "wk-2",
                                      "_caller": worker, "_subject":
                                      f"mop.alpha.cluster.role.{worker}"})
        c.check("#582 a parent without the add verb cannot spawn",
                "error" in child and ("add" in (child.get("error") or "")
                                      or "may not" in (child.get("error") or ""))
                and len(api.jobs) == 1, child)

    # 2. Новый грант человеком без верифицированного вызывающего: аноним
    #     не заводит роли; совместимость -- только для легаси-джобов.
    api = FakeNomad()
    with soul_env(api) as g:
        anon = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                     "soul": "puppet", "request_id": "an-1"})
        c.check("#582 a new role grant needs a verified human caller",
                "error" in anon and not api.jobs, anon)

    # 3. Реплей конфликтующего запроса: side effects ДО prepare не
    #     происходят -- файлы резерва не перезатираются чужим текстом.
    api = FakeNomad()
    with soul_env(api) as g:
        first = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "soul": "puppet", "request_id": "re-1",
                                      "workspace": PLAY_A,
                                      "_caller": "anton"})
        pinned = bootstrap.files_of(bootstrap.ROOT, first.get("name") or "")
        c.check("#582 the human role reserve pins its approved files",
                first.get("ok") and pinned[0] is not None, (first, pinned))
        before = open(pinned[0]).read()
        clash = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "soul": "puppet", "request_id": "re-1",
                                      "workspace": PLAY_B,
                                      "_caller": "anton"})
        after = open(bootstrap.files_of(bootstrap.ROOT,
                                        first.get("name"))[0]).read()
        c.check("#582 a conflicting replay changes no pinned file",
                "error" in clash and before == after, clash)

    # 4a. Реплей УЖЕ созданного request_id проходит МИМО квоты: успешный
    #     запуск, занявший последний слот, реплейится без отказа.
    api = FakeNomad()
    with soul_env(api, limit=1) as g:
        principal, parent, _p = soul_parent(api, g, request_id="q-1",
                                            workspace=PLAY_A)
        c.check("#582 limit=1 parent consumed the last slot",
                len(api.jobs) == 1, sorted(api.jobs))
        replay = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                       "soul": "master", "request_id": "q-1",
                                       "workspace": PLAY_A,
                                       "_caller": "anton"})
        c.check("#582 a replay of the same request passes the quota gate",
                replay.get("ok") and replay.get("name") == parent, replay)

    # 4b. Выбор имени учитывает резервы, невидимые в Nomad: вторая
    #     заявка не занимает имя висящего резерва первой.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g)
        # Резерв без регистрации: Nomad молчит, имя занято резервом.
        class _NoReg(FakeNomad):
            def register(self, sp):
                raise RuntimeError("nomad is down")
        broken = _NoReg()
        broken.jobs = api.jobs
        stuck = answer(broken, "alpha", {"verb": "add", "origin": ALPHA,
                                         "soul": "puppet", "request_id": "nm-1",
                                         "_caller": principal, "_subject":
                                         f"mop.alpha.cluster.role.{principal}"})
        rec = grants.by_request("nm-1", root=g)
        fresh = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "soul": "master", "request_id": "nm-2",
                                      "_caller": "anton"})
        c.check("#582 a pending reservation keeps its name against a new add",
                "error" in stuck and rec is not None
                and rec.get("job") == "pu-alpha-2"
                and fresh.get("ok") and fresh.get("name") != "pu-alpha-2",
                (stuck, rec, fresh))

    # 5. Реплей по сохранённому бандлу, а не по текущему каталогу: роль
    #    убрана из каталога -- ретрай всё равно поднимает резерв.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g, request_id="cat-1")
        with patched(cluster, SOUL_CATALOGUE=[]):
            retry = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                          "soul": "master", "request_id": "cat-1",
                                          "workspace": PLAY_A,
                                          "_caller": "anton"})
        c.check("#582 a replay reads the pinned bundle, not the catalogue",
                retry.get("ok") and retry.get("name") == parent, retry)


def check_seam_adoption_582(c):
    """Шов pu3 (#581 b8d9faf) вместо своих проверок в _answer/_delegated:
    authorize_request -- единственный вход авторизации глагола роли.
    Ручная сборка субъекта (f"{ROOT}.{project}.cluster.role.{caller}")
    запрещена: грамматика -- busnames.cluster, и _answer не должен
    держать свою копию сравнения. Проверки пускают РЕАЛЬНЫЙ путь через
    cluster.answer с субъектом от busnames.cluster."""
    from mop.common import busnames as bn
    # Инвариант кода: ручных subject-строк в кластере нет.
    raw = re.compile(r'cluster\.role')
    bad = []
    for line in open("mop/server/cluster.py"):
        if raw.search(line.split("#")[0]) and "busnames" not in line:
            bad.append(line.strip())
    c.check("#582 no hand-built role subject strings in cluster.py",
            not bad, bad)
    # Путь: правильный субъект от busnames.cluster проходит шов,
    # легаси-rpc субъект отзывается швом, а не нашей строкой.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g)
        good = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                     "soul": "puppet", "request_id": "se-1",
                                     "_caller": principal, "_subject":
                                     bn.cluster("alpha", login=principal)})
        c.check("#582 the busnames-made role subject passes the seam",
                good.get("ok") and good.get("name") == "pu-alpha-2", good)
        bad_subject = answer(api, "alpha",
                             {"verb": "add", "origin": ALPHA,
                              "soul": "puppet", "request_id": "se-2",
                              "_caller": principal, "_subject":
                              "mop.alpha.cluster.rpc.anton"})
        c.check("#582 the legacy rpc subject is refused by the seam",
                "error" in bad_subject
                and "role subject" in (bad_subject.get("error") or ""),
                bad_subject)


def check_role_channel_verbs_582(c):
    """Блокатор ревью 9747ed7: глаголы роли НЕ исчерпываются add. Шов
    authorize_request пускает по cluster_verbs души (пресет puppet:
    ping/roster/alloc/spec; мастер: ещё pool/stderr/update/...), но
    _answer вёл ЛЮБОЙ глагол роли в _delegated, а тот отказывал всему,
    кроме add: рабочие глаголы пресетов по каналу роли не работали вовсе.
    Маршрут: add -- делегированное заводение; ДРУГИЕ разрешённые глаголы
    -- обычные ворота (refusal/имя/аренда) и существующие обработчики,
    актёр -- принципал, аренда видит человека-хозяина; запрет -- швом
    (не в cluster_verbs) или воротами (чужая цель), легаси-rpc человека
    не меняется вовсе."""
    from mop.common import busnames as bn

    def worker_and_target(api, g):
        """Живой рабочий (puppet) и его джоб в Nomad: цели глаголов."""
        got = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                    "soul": "puppet", "request_id": "w-1",
                                    "_caller": "anton"})
        assert got.get("ok"), got
        worker = grants.by_job("alpha", got["name"], root=g)["principal"]
        return worker, got["name"]

    def on_role(api, project, worker, fields):
        return answer(api, project, {**fields, "_caller": worker,
                                     "_subject": bn.cluster(project,
                                                            login=worker)})

    # Позитив: рабочие глаголы пресета puppet по каналу роли.
    api = FakeNomad(jobs={"pu-alpha-9": soul_job("pu-alpha-9")})
    with soul_env(api) as g:
        worker, target = worker_and_target(api, g)
        api.jobs[target] = soul_job(target)
        got = on_role(api, "alpha", worker,
                      {"verb": "ping", "origin": ALPHA})
        c.check("#582 a worker's ping answers on the role channel",
                got.get("ok"), got)
        roster = on_role(api, "alpha", worker, {"verb": "roster"})
        c.check("#582 a worker's roster sees the project's puppets",
                roster.get("ok")
                and target in {r.get("job", {}).get("ID")
                               for r in roster.get("items", [])}, roster)
        alloc = on_role(api, "alpha", worker,
                        {"verb": "alloc", "name": target})
        c.check("#582 a worker's alloc reaches the named puppet",
                alloc.get("ok") or "no job" in (alloc.get("error") or ""),
                alloc)
        spec = on_role(api, "alpha", worker,
                       {"verb": "spec", "name": target})
        c.check("#582 a worker's spec reads the job meta",
                spec.get("ok") and (spec.get("meta") or {}).get("soul")
                == "puppet", spec)

    # Позитив: мастер управляет -- restart разрешён душой мастера,
    # аренда видит человека-хозяина, не принципала.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g)
        rec = grants.by_job("alpha", parent, root=g)
        target = soul_job("pu-alpha-9", digest=rec["soul_digest"])
        target["Meta"]["soul"] = rec["role"]
        target["Meta"]["soul_inc"] = rec["soul_inc"]
        api.jobs["pu-alpha-9"] = target
        api.allocs["pu-alpha-9"] = {"ID": "a1", "NodeName": "hyper",
                                    "JobID": "pu-alpha-9",
                                    "ClientStatus": "running",
                                    "TaskStates": {}}
        with patched(cluster, _clone_of=lambda name: None):
            got = on_role(api, "alpha", principal,
                          {"verb": "restart", "name": "pu-alpha-9",
                           "force": True})
        c.check("#582 a master's restart works on the role channel",
                got.get("ok"), got)
        # update от гранта: без client workspace -- текст политики
        # загружает только человек, грант его не приносит.
        with patched(cluster, _clone_of=lambda name: None):
            upd = on_role(api, "alpha", principal,
                          {"verb": "update", "name": "pu-alpha-9",
                           "origin": ALPHA, "force": True})
        c.check("#582 a grant update carries no client workspace",
                upd.get("error") is None
                and bootstrap.files_of(bootstrap.ROOT, "pu-alpha-9")
                == (None, None), (upd, bootstrap.files_of(
                    bootstrap.ROOT, "pu-alpha-9")))
        with patched(cluster, _clone_of=lambda name: None):
            poison = on_role(api, "alpha", principal,
                             {"verb": "update", "name": "pu-alpha-9",
                              "origin": ALPHA, "workspace": "- name: evil\n",
                              "force": True})
        c.check("#582 a grant update cannot upload workspace text",
                "error" in poison
                and bootstrap.files_of(bootstrap.ROOT, "pu-alpha-9")
                == (None, None), poison)

    # Негатив: глагол вне cluster_verbs души -- отказ шва; чужой проект
    # на субъектах роли не читается; отозванный источник -- отказ.
    api = FakeNomad()
    with soul_env(api) as g:
        worker, target = worker_and_target(api, g)
        no_verb = on_role(api, "alpha", worker, {"verb": "pool"})
        c.check("#582 a verb outside the soul is refused by the seam",
                "error" in no_verb
                and "may not" in (no_verb.get("error") or ""), no_verb)
        foreign = on_role(api, "beta", worker, {"verb": "ping"})
        c.check("#582 a foreign project subject is refused",
                "error" in foreign, foreign)
        grants.revoke(worker, root=g)
        revoked = on_role(api, "alpha", worker, {"verb": "ping"})
        c.check("#582 a revoked source is refused with its reason",
                "error" in revoked and "revok" in revoked.get("error", ""),
                revoked)


def check_role_channel_lease_582(c):
    """Блокатор ревью da580a2: владение на канале роли. Существующие
    ворота -- lease.caller(req) -> _caller, и на канале роли _caller --
    принципал: чужая живая аренда ЧЕЛОВЕКА обязана отказывать гранту,
    своя -- пускать, и заметка/журнал называют человека, а не грант.
    force из тела гранта не может снять чужую аренду: узел актёра force
    срезает (#584 -- контракт уже там), сервис кластера -- нет; без
    среза force:true брал бы чужое молча. Токен посадки -- тем же
    правилом: держатель -- человек-хозяин.
    HYPOTHESIS: сегодня _answer штампует только _owner, который ворота
    не читают, -- _caller остаётся принципалом, и may_touch видит его:
    своя аренда человека отказывает, чужая проходит, landing держит
    принципал, а force из тела снимает чужое. SOLUTION: доверенный
    _caller=ctx['owner'] после authorize_request (актёр -- отдельно),
    force срезается на глаголах управления ролью; легаси-force человека
    не меняется. STATUS: pending — RED first."""
    import time as _time
    from mop.common import busnames as bn, landing, lease
    from mop.common.domain import Owner

    def held_clone(login, dirty=0, ahead=0):
        """Факты клона с живой арендой: свежая запись владельца."""
        return {"clone": {"cur": "feat/1-x", "def": "master",
                          "dirty": dirty, "ahead": ahead,
                          "owner": Owner(login, int(_time.time()) - 60).to_dict()}}

    def idle_clone(login):
        """Факты клона с мёртвой арендой: чистый клон, окно вышло."""
        return {"clone": {"cur": "master", "def": "master",
                          "dirty": 0, "ahead": 0,
                          "owner": Owner(login, int(_time.time()) - 9999).to_dict()}}

    def on_role(api, project, worker, fields):
        return answer(api, project, {**fields, "_caller": worker,
                                     "_subject": bn.cluster(project,
                                                            login=worker)})

    # restart/update по каналу роли против РЕАЛЬНЫХ ворот (не подмены
    # _clone_of=None): чужая живая аренда отказывает, своя -- нет.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g)
        rec = grants.by_job("alpha", parent, root=g)
        target = soul_job("pu-alpha-9", digest=rec["soul_digest"])
        target["Meta"]["soul"] = rec["role"]
        target["Meta"]["soul_inc"] = rec["soul_inc"]
        api.jobs["pu-alpha-9"] = target
        api.allocs["pu-alpha-9"] = {"ID": "a1", "NodeName": "hyper",
                                    "JobID": "pu-alpha-9",
                                    "ClientStatus": "running",
                                    "TaskStates": {}}
        human = rec["owner"]
        # Своя живая аренда человека-хозяина: грант проходит -- ворота
        # видят человека; журнал называет его же, без self-declared:
        # штамп доверенного _caller меняет сам словарь запроса, который
        # журнал читает после обработчика (как у узла актёра #584).
        served = {"verb": "restart", "name": "pu-alpha-9", "_caller": principal,
                  "_subject": bn.cluster("alpha", login=principal)}
        with patched(cluster, _clone_of=lambda name: held_clone(human)):
            mine = answer(api, "alpha", served)
        c.check("#582 a grant passes its owner's own live lease",
                mine.get("ok"), mine)
        c.check("#582 the grant branch stamps the served request with "
                "the human owner",
                served.get("_caller") == human
                and served.get("_actor") == principal, served)
        line = " ".join(cluster.journal("alpha", served, {"ok": True}))
        c.check("#582 the journal names the grant's human owner",
                f"by {human}" in line and "self-declared" not in line, line)
        # Чужая живая аренда: отказ, имя владельца -- человек, не грант.
        with patched(cluster, _clone_of=lambda name: held_clone("olga")):
            theirs = on_role(api, "alpha", principal,
                             {"verb": "restart", "name": "pu-alpha-9"})
        c.check("#582 a grant cannot break a foreign live lease",
                "error" in theirs and "olga" in theirs.get("error", ""), theirs)
        # Мёртвая аренда -- не работа: чистый клон после окна проходит.
        with patched(cluster, _clone_of=lambda name: idle_clone("olga")):
            stale = on_role(api, "alpha", principal,
                            {"verb": "restart", "name": "pu-alpha-9"})
        c.check("#582 a dead foreign lease does not block a grant",
                stale.get("ok"), stale)
        # force из тела гранта НЕ снимает чужую живую аренду: срез -- как
        # у узла актёра (#584), своего force у гранта нет.
        with patched(cluster, _clone_of=lambda name: held_clone("olga")):
            forced = on_role(api, "alpha", principal,
                             {"verb": "restart", "name": "pu-alpha-9",
                              "force": True})
        c.check("#582 a grant's body force cannot take a foreign live lease",
                "error" in forced and "olga" in forced.get("error", ""), forced)
        # update -- тот же прогон ворот: своя аренда проходит.
        with patched(cluster, _clone_of=lambda name: held_clone(human)):
            upd = on_role(api, "alpha", principal,
                          {"verb": "update", "name": "pu-alpha-9",
                           "origin": ALPHA})
        c.check("#582 a grant update passes its owner's own lease",
                upd.get("error") is None, upd)

    # Токен посадки: держатель -- человек-хозяин, не принципал; отдаёт
    # тот же человек, чужой -- нет. Файл -- настоящий, под швом.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g)
        rec = grants.by_job("alpha", parent, root=g)
        human = rec["owner"]
        d = tempfile.mkdtemp(prefix="mop-test-582l-")
        try:
            with patched(landing, FILE=os.path.join(d, "landing.json")):
                got = on_role(api, "alpha", principal,
                              {"verb": "landing", "action": "take",
                               "puppet": "pu-alpha-2"})
                held_by = (landing.read().get("alpha") or {}).get("holder")
                c.check("#582 the landing token is held by the grant's "
                        "human owner",
                        got.get("ok") and held_by == human, (got, held_by))
                # Повторно берёт тот же человек -- повтор, не отказ.
                again = on_role(api, "alpha", principal,
                                {"verb": "landing", "action": "take",
                                 "puppet": "pu-alpha-2"})
                c.check("#582 a repeated take by the same human repeats",
                        again.get("ok"), again)
                # Токен держит чужой человек: грант без force не берёт и
                # не отдаёт, force:true из тела не отбирает (срезан).
                with patched(landing, FILE=os.path.join(d, "landing.json"),
                             read=lambda path=None: {"alpha": {
                                 "holder": "olga", "puppet": "pu-olga-1",
                                 "since": "2026-10-14T00:00:00Z"}},
                             write=lambda tokens, path=None: stolen.append(
                                 tokens) or tokens):
                    stolen = []
                    thief = on_role(api, "alpha", principal,
                                    {"verb": "landing", "action": "take",
                                     "puppet": "pu-alpha-3", "force": True})
                    c.check("#582 a grant's body force cannot take a "
                            "foreign-held token",
                            "error" in thief and not stolen, (thief, stolen))
                    rogue = on_role(api, "alpha", principal,
                                    {"verb": "landing", "action": "give",
                                     "force": True})
                    c.check("#582 a grant's body force cannot give away a "
                            "foreign-held token",
                            "error" in rogue and not stolen, (rogue, stolen))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # Легаси-контраст на тех же воротах: force человека снимает чужое,
    # держатель -- логин субъекта. Регресс двоих сразу -- не один фикс.
    api = FakeNomad()
    with soul_env(api) as g:
        api.jobs["pu-alpha-1"] = soul_job("pu-alpha-1")
        api.allocs["pu-alpha-1"] = {"ID": "a2", "NodeName": "hyper",
                                    "JobID": "pu-alpha-1",
                                    "ClientStatus": "running",
                                    "TaskStates": {}}
        with patched(cluster, _clone_of=lambda name: held_clone("olga")):
            human_force = answer(api, "alpha", {"verb": "restart",
                                                "name": "pu-alpha-1",
                                                "_caller": "anton",
                                                "force": True})
        c.check("#582 a human's force keeps taking a foreign live lease",
                human_force.get("ok")
                and (human_force.get("owner_note") or "") == "taken from olga",
                human_force)
        d = tempfile.mkdtemp(prefix="mop-test-582h-")
        try:
            with patched(landing, FILE=os.path.join(d, "landing.json")):
                got = answer(api, "alpha", {"verb": "landing", "action": "take",
                                            "puppet": "pu-alpha-1",
                                            "_caller": "anton"})
                held_by = (landing.read().get("alpha") or {}).get("holder")
                c.check("#582 a human keeps holding the landing token",
                        got.get("ok") and held_by == "anton", (got, held_by))
        finally:
            shutil.rmtree(d, ignore_errors=True)


def check_role_channel_journal_582(c):
    """Аудит-консистентность ревью 8d6b348: журнал называет ОБЕИХ --
    человека-хозяина (by ...) и грант-актёра с его джобом (provenance).
    Штамп доверенного _caller/_actor/_job ставится на исходный запрос
    РОВНО ОДИН раз -- сразу после authorize_request, для ВСЕХ глаголов
    роли, включая делегированный add (раньше add журналился принципалом:
    человек стирался). Легаси-журнал человека/сервиса -- байт в байт
    прежний; тело не подделает _actor (каркас срезает '_'-ключи), и
    полезная нагрузка (workspace) в строку не попадает.
    HYPOTHESIS: сегодня add-ветка журналирует «by grant-…» без человека,
    management -- «by anton» без актёра: два актёра одного человека
    неразличимы в аудите. SOLUTION: единый штамп после шва + provenance
    в journal(). STATUS: pending — RED first."""
    from mop.common import busnames as bn, service

    def served_req(principal, fields):
        return {**fields, "_caller": principal,
                "_subject": bn.cluster("alpha", login=principal)}

    # Подделка из тела: каркас срезает '_'-ключи ДО обработчика --
    # _actor журнал видит только от штампа шва, не от просителя.
    stripped = service.trusted({"verb": "ping", "_actor": "grant-spoof",
                                "_job": "pu-x"}, "anton")
    c.check("#582 the framework strips a body _actor before any handler",
            "_actor" not in stripped and "_job" not in stripped, stripped)

    # Делегированный add: ДВА принципала одного человека -- журнал
    # различает актёров, называя обоих: человека и грант (+его джоб).
    api = FakeNomad()
    with soul_env(api) as g:
        lines = []
        humans = []
        for n in ("p-a", "p-b"):
            fields = {"verb": "add", "origin": ALPHA, "soul": "master",
                      "request_id": f"jrn-{n}", "_caller": "anton",
                      "workspace": PLAY_A}
            parent_req = dict(fields)
            got = answer(api, "alpha", fields)
            assert got.get("ok"), got
            principal = grants.by_job("alpha", got["name"], root=g)["principal"]
            humans.append((principal, got["name"]))
            served = served_req(principal, {"verb": "add", "origin": ALPHA,
                                            "soul": "puppet",
                                            "request_id": f"kid-{n}"})
            kid = answer(api, "alpha", served)
            assert kid.get("ok"), kid
            lines.append(" ".join(cluster.journal("alpha", served, kid)))
        (pa, ja), (pb, jb) = humans
        c.check("#582 a delegated add journals the human owner, not the "
                "principal alone",
                all(f"by anton" in ln for ln in lines), lines)
        c.check("#582 two actors of the same human stay distinguishable "
                "in the journal",
                any(pa in ln for ln in lines)
                and any(pb in ln for ln in lines)
                and pa != pb, lines)
        c.check("#582 the delegated add names the acting grant's source job",
                any(ja in ln or jb in ln for ln in lines), (lines, ja, jb))

    # Управление: тот же штамп, та же пара имён. Легаси-строка -- без
    # provenance и без payload: человек и сервис байт в байт прежние.
    api = FakeNomad()
    with soul_env(api) as g:
        principal, parent, _p = soul_parent(api, g)
        api.allocs[parent] = {"ID": "a1", "NodeName": "hyper",
                              "JobID": parent, "ClientStatus": "running",
                              "TaskStates": {}}
        served = served_req(principal, {"verb": "restart",
                                        "name": parent})
        with patched(cluster, _clone_of=lambda name: None):
            got = answer(api, "alpha", served)
        assert got.get("ok"), got
        line = " ".join(cluster.journal("alpha", served, got))
        c.check("#582 a management verb journals both the human and the "
                "actor with its job",
                "by anton" in line and principal in line and parent in line,
                line)
        human_line = " ".join(cluster.journal("alpha",
                                              {"verb": "restart",
                                               "name": "pu-alpha-1",
                                               "_caller": "anton"},
                                              {"ok": True}))
        c.check("#582 the human journal line is unchanged, no provenance",
                "by anton" in human_line and "actor" not in human_line,
                human_line)
        body_line = " ".join(cluster.journal(
            "alpha", {"verb": "add", "origin": ALPHA, "name": "pu-alpha-1",
                      "_caller": "anton", "workspace": PLAY_A},
            {"ok": True}))
        c.check("#582 the journal line carries no workspace payload",
                "answer" not in body_line and "42" not in body_line, body_line)


def check_bootstrap_snapshot_coherence_582(c):
    """Блокатор 6: согласованный снапшот -- одни байты на чтение, хеш и
    запись. Хеш сверяется с прочитанными байтами, копируются они же;
    нечитаемый файл -- ошибка с причиной, не пустой снапшот."""
    from mop.common import fsutil
    root = tempfile.mkdtemp(prefix="mop-test-582a-")
    try:
        fsutil.make_private_dir(root)
        with open(os.path.join(root, "pu-p-tasks.yml"), "w") as f:
            f.write("- name: greet\n")
        bootstrap.note_approval(root, "pu-p")
        # Нечитаемый файл: снапшот не «пуст», а сломан.
        os.chmod(os.path.join(root, "pu-p-tasks.yml"), 0o000)
        try:
            why = bootstrap.copy_approval(root, "pu-p", "pu-c")
            c.check("#582 an unreadable approved file fails loudly, not "
                    "as an empty snapshot",
                    why is not None and "unreadable" in why, why)
            c.check("#582 a failed copy leaves no child files",
                    bootstrap.files_of(root, "pu-c") == (None, None),
                    bootstrap.files_of(root, "pu-c"))
        finally:
            os.chmod(os.path.join(root, "pu-p-tasks.yml"), 0o600)
        # Битый JSON метки: причина на поверхность.
        with open(os.path.join(root, "pu-p-approved.json"), "w") as f:
            f.write("{not json")
        why = bootstrap.copy_approval(root, "pu-p", "pu-c2")
        c.check("#582 a corrupted approval marker surfaces its reason",
                why is not None and "approval" in (why or ""), why)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_add_workspace_boundary_582(c):
    """Граница workspace различает актёров, а не запрещает всем (#582):
    аутентифицированный ЧЕЛОВЕК, заводящий роль, согласовывает политику
    своим текстом -- тот же одобренный путь, что у легаси-add (текст
    человека и есть одобрение); ГРАНТ текста не приносит вовсе --
    его ребёнок наследует снапшот родителя (check_add_delegated_582),
    а YAML от гранта отклонён до всего. Голая метка bootstrap_sent без
    текста -- не согласование. Легаси-rpc для гранта закрыт ограждением
    #603: правильный канал роли проверен делегированной проверкой."""
    api = FakeNomad()
    with soul_env(api) as g:
        human = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                      "soul": "puppet", "request_id": "r",
                                      "workspace": "- name: play\n  tasks: []\n",
                                      "_caller": "anton"})
        name = human.get("name") or ""
        mark = bootstrap.approval_of(bootstrap.ROOT, name)
        c.check("#582 a human role add approves the policy with its text",
                human.get("ok") and mark is not None
                and mark.get("key") == bootstrap._snapshot_key(
                    bootstrap.ROOT, name),
                (human, mark))
        bare = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                     "soul": "puppet", "request_id": "r2",
                                     "bootstrap_sent": {"source": "x"},
                                     "_caller": "anton"})
        c.check("#582 a bare bootstrap_sent marker is not an approval",
                "error" in bare and len(api.jobs) == 1, bare)
        fenced = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                       "soul": "puppet", "request_id": "r3",
                                       "_caller": "grant-" + "a" * 32})
        c.check("#582 a grant principal does not reach the human add "
                "at all (#603 fence)",
                "error" in fenced
                and "role subject" in fenced.get("error", "")
                and len(api.jobs) == 1, fenced)
        legacy = answer(api, "alpha", {"verb": "add", "origin": ALPHA,
                                       "_caller": "anton",
                                       "workspace": "- name: play\n  tasks: []\n"})
        c.check("#662 a roleless add with workspace text is refused",
                bool(legacy.get("error")) and "soul=" in legacy.get("error", "")
                and len(api.jobs) == 1, legacy)


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
               check_add_roleless_refused_662, check_add_delegated_582,
               check_add_delegated_gates_582,
               check_seam_adoption_582,
               check_role_channel_verbs_582,
               check_role_channel_lease_582,
               check_role_channel_journal_582,
               check_bootstrap_snapshot_coherence_582,
               check_add_workspace_boundary_582,
               check_add_quota_counts_roles_582,
               check_update_preserves_triple_582, check_restart_keeps_meta_582,
               check_recycle_road_keeps_triple_582, check_image_roundtrip_582,
               check_delete_revokes_binding_582, check_roster_mop_soul_582):
        fn(c)
    return c.report("soul_lifecycle")


if __name__ == "__main__":
    sys.exit(main())
