#!/usr/bin/env python3
"""Душа роли и её бандл: python3 tests/soul.py

Эпик #578 по контракту docs/SOUL.md (#579, ревизия 3): все сессии пула --
папеты с декларативными ролями, роль описана soul.yaml и проверяется
сервером. Общий слой #580: разбор soul.yaml v1, бандл роли и канонический
digest, принципал grant-<id> и запись привязки, каталог ролей пакета,
тройка soul/soul_digest/soul_inc в мете джоба. Реестр и хранилище бандлов --
серверный #581, узел и обёртка -- #584/#583: здесь только общее владение.

HYPOTHESIS (#580): JobMeta молча теряет все три поля роли, строка ростера
не несёт роль, пакет везёт один файл скилла мастера, а разбора души нет
вовсе -- подделка в soul.yaml и чужой промпт проходят молча, принципал
grant-<id> не закодирован нигде.
SOLUTION: mop/common/soul.py -- один разбор, один digest, одни кодировки;
JobMeta несёт тройку целиком или никак; строка ростера несёт роль без
изменения прежнего вида; пакет везёт каталог ролей.
STATUS: RED
"""
import hashlib
import json
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)

from mop.common import busnames, domain, puppets, state  # noqa: E402

PACKAGE = os.path.join(ROOT, "mop", "skills")

# Поставляемая мастер-роль -- из примера контракта #579: глаголы сверены с
# таблицами агента (mop/node/agent.py VERBS) и сервиса кластера
# (mop/server/cluster.py VERBS) -- постфактум их проверит soul.parse.
MASTER_NODE = ["send", "tail", "state", "states", "ping", "local",
               "type", "wipe", "usage", "sizes", "clone"]
MASTER_CLUSTER = ["ping", "roster", "alloc", "spec", "pool", "stderr",
                  "bootstrap_result", "add", "update", "restart", "stop",
                  "delete", "landing"]
MASTER_YAML = f"""soul: master
version: 1
runtime:
  driver: isolating
  receives_dispatch: false
capabilities:
  node_verbs: [{', '.join(MASTER_NODE)}]
  cluster_verbs: [{', '.join(MASTER_CLUSTER)}]
  dispatch: true
  spawn: [puppet, builder]
prompt: soul.md
"""
PUPPET_YAML = """soul: puppet
version: 1
runtime: {driver: any, receives_dispatch: true}
capabilities:
  node_verbs: [send, tail, state, states, ping, local]
  # Обнаружение адресов (#585) -- только чтение своего проекта: без
  # анонимной broadcast-рассылки и без привилегированного locate-прокси.
  cluster_verbs: [ping, roster, alloc, spec]
  dispatch: false
  spawn: []
prompt: soul.md
"""
BUILDER_YAML = """soul: builder
version: 1
runtime: {driver: any, receives_dispatch: true}
capabilities:
  node_verbs: [send, tail, state, states, ping, local]
  cluster_verbs: [ping, roster, alloc, spec]
  dispatch: false
  spawn: []
prompt: soul.md
"""
UUID_OK = "123e4567-e89b-42d3-a456-426614174000"
HEX32 = "ab" * 16
HEX64 = "ab" * 32  # digest бандла -- SHA256 (#579, review #580)


def make_role(base, name, manifest_text, prompt="You are the role.\n",
              prompt_name="soul.md", manifest_name="soul.yaml", extra=None):
    """Каталог роли в tempdir: soul.yaml + промпт (+ extra-файл)."""
    d = os.path.join(base, name)
    os.makedirs(d)
    with open(os.path.join(d, manifest_name), "w") as f:
        f.write(manifest_text)
    with open(os.path.join(d, prompt_name), "wb") as f:
        f.write(prompt.encode() if isinstance(prompt, str) else prompt)
    for fname, text in (extra or {}).items():
        with open(os.path.join(d, fname), "w") as f:
            f.write(text)
    return d


def contract_digest(manifest_text, prompt):
    """Digest формулой контракта #579, посчитанной здесь же: канонический
    JSON разобранного манифеста (с умолчаниями схемы, одним набором ключей
    -- review #580: yaml без блока secrets и с явным пустым -- один бандл)
    + NUL + сырой промпт."""
    import yaml
    parsed = yaml.safe_load(manifest_text)
    parsed.setdefault("secrets", {})
    parsed["secrets"] = {"files": [], "vars": [], **parsed["secrets"]}
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode()
    raw = prompt.encode() if isinstance(prompt, str) else prompt
    return hashlib.sha256(canonical + b"\x00" + raw).hexdigest()


def check_meta_580(c):
    """HYPOTHESIS (#580): JobMeta знает origin/branch/версию спеки, а тройку
    роли (soul, soul_digest, soul_inc) теряет молча -- регистрация #582 не
    сможет закрепить бандл за инкарнацией, и читатели меты не отличат папета
    с ролью от легаси.
    SOLUTION: три необязательных поля с правилом «все или ни одной»: половина
    тройки -- не роль без дайджеста, а сломанная запись; форматы -- те же
    кодировки, что в soul (#580), одним местом. Легаси-мета читается и
    пишется как прежде, байт в байт.
    STATUS: RED"""
    JobMeta = domain.JobMeta
    full = JobMeta("git@h:g/mop.git", None, None, soul="master",
                   soul_digest=HEX64, soul_inc=UUID_OK)
    meta = full.to_meta()
    for key, want in (("soul", "master"), ("soul_digest", HEX64),
                      ("soul_inc", UUID_OK)):
        c.expect(f"#580 to_meta carries {key}", meta.get(key), want)
    back = JobMeta.from_meta(dict(meta, origin="git@h:g/mop.git", branch=None))
    c.expect("#580 from_meta reads the triple back",
             (back.soul, back.soul_digest, back.soul_inc),
             (full.soul, full.soul_digest, full.soul_inc))
    # Легаси: тройки нет -- не только полей нет, но и прежний вид без новых
    # ключей: живые джобы и снимки не меняются от самой возможности.
    plain = JobMeta("git@h:g/mop.git", "feat/1", "3")
    legacy = plain.to_meta()
    c.check("#580 a legacy meta stays byte-for-byte without the triple",
            not any(k in legacy for k in ("soul", "soul_digest", "soul_inc")),
            legacy)
    c.expect("#580 a legacy job reads back without a soul",
             JobMeta.from_meta(legacy).soul, None)

    def refused(what, build, field):
        try:
            got = build()
        except ValueError as e:
            c.check(f"#580 {what}: the refusal names {field}", field in str(e), str(e))
            return
        c.fail(f"#580 {what} must be refused", repr(got))

    for what, build, field in [
            ("half a triple (digest only)",
             lambda: JobMeta("o", soul_digest=HEX64), "soul"),
            ("half a triple (incarnation only)",
             lambda: JobMeta("o", soul_inc=UUID_OK), "soul"),
            ("half a triple (name and digest)",
             lambda: JobMeta("o", soul="master", soul_digest=HEX64), "soul"),
            ("a digest that is not 32 hex",
             lambda: JobMeta("o", soul="master", soul_digest="xyz",
                             soul_inc=UUID_OK), "soul_digest"),
            ("an incarnation that is not a UUID",
             lambda: JobMeta("o", soul="master", soul_digest=HEX64,
                             soul_inc="zzz"), "soul_inc"),
            ("a soul name outside the role grammar",
             lambda: JobMeta("o", soul="Master!", soul_digest=HEX64,
                             soul_inc=UUID_OK), "soul")]:
        refused(what, build, field)


def check_parse_580(c):
    """HYPOTHESIS (#580): разбора soul.yaml нет: подделка полей, чужие глаголы,
    дубликаты, небезопасный YAML и побег промпта из каталога прошли бы молча,
    а сервер (#581/#582) верил бы имени роли, а не схеме.
    SOLUTION: soul.parse(text) -> Soul: жёсткая форма v1, точные имена глаголов
    своих таблиц, дубликаты -- отказ, safe_load + отказ на дубликаты ключей
    YAML, промпт -- одно имя файла в каталоге роли.
    STATUS: RED"""
    try:
        from mop.common import soul
    except ImportError as e:
        c.fail("#580 the common soul module must exist", str(e))
        return

    def one(text, **want):
        try:
            got = soul.parse(text)
        except ValueError as e:
            c.fail(f"#580 this soul.yaml must parse: {e}", text)
            return None
        for field, value in want.items():
            c.expect(f"#580 parse: {field}", getattr(got, field), value)
        return got

    master = one(MASTER_YAML, soul="master", version=1, prompt="soul.md")
    c.expect("#580 runtime of the master preset",
             (master.runtime.driver, master.runtime.receives_dispatch),
             ("isolating", False))
    c.expect("#580 capabilities of the master preset",
             (master.capabilities.node_verbs, master.capabilities.cluster_verbs,
              master.capabilities.dispatch, master.capabilities.spawn),
             (tuple(MASTER_NODE), tuple(MASTER_CLUSTER), True,
              ("puppet", "builder")))
    c.expect("#580 secrets default to empty allowlists",
             (master.secrets.files, master.secrets.vars), ((), ()))
    # Секреты -- явный allowlist точных имён (#579): пусто по умолчанию.
    secreted = one(MASTER_YAML.replace(
        "prompt: soul.md",
        "prompt: soul.md\nsecrets:\n  files: [gitlab-token]\n"
        "  vars: [CI_JOB_TOKEN]\n"), soul="master")
    c.expect("#580 secrets read as exact names",
             (secreted.secrets.files, secreted.secrets.vars),
             (("gitlab-token",), ("CI_JOB_TOKEN",)))

    def refused(text, because):
        try:
            got = soul.parse(text)
        except ValueError as e:
            c.check(f"#580 refused: {because}", because in str(e).lower(), str(e))
            return
        c.fail(f"#580 must be refused: {because}", repr(got))

    refused(MASTER_YAML.replace("soul: master\n", ""), "soul")
    refused(MASTER_YAML.replace("version: 1", "version: 2"), "version")
    refused(MASTER_YAML.replace("version: 1", "version: '1'"), "version")
    refused(MASTER_YAML.replace("soul: master", "soul: Master"), "soul")
    refused(MASTER_YAML.replace("soul: master", "soul: мастер"), "soul")
    refused(MASTER_YAML + "\nwhisper: yes\n", "unknown")
    refused(MASTER_YAML.replace("  driver: isolating", "  driver: host"), "driver")
    refused(MASTER_YAML.replace("receives_dispatch: false",
                                "receives_dispatch: nope"), "receives_dispatch")
    refused(MASTER_YAML.replace("  dispatch: true", ""), "dispatch")
    refused(MASTER_YAML.replace("  dispatch: true", "  dispatch: 'true'"),
            "dispatch")
    refused(MASTER_YAML.replace("spawn: [puppet, builder]",
                                "spawn: [puppet, puppet]"), "repeat")
    refused(MASTER_YAML.replace("node_verbs: [send",
                                "node_verbs: [send, send"), "repeat")
    refused(PUPPET_YAML.replace(
        "node_verbs: [send, tail, state, states, ping, local]", "node_verbs: 5"),
        "node_verbs")
    # Глаголы -- точные имена своих таблиц (#579): выдуманный не проходит.
    refused(MASTER_YAML.replace("cluster_verbs: [ping, roster",
                                "cluster_verbs: [nuke, roster"), "verb")
    refused(MASTER_YAML.replace("node_verbs: [send",
                                "node_verbs: [reboot, send"), "verb")
    # Душа не становится админом именем глагола: admin-глаголы узла и сервиса
    # в allowlist роли не входят вовсе.
    refused(MASTER_YAML.replace("node_verbs: [send",
                                "node_verbs: [disk, send"), "verb")
    refused(MASTER_YAML.replace("cluster_verbs: [ping, roster",
                                "cluster_verbs: [drain, roster"), "verb")
    # Промпт -- одно имя файла в каталоге роли: не абсолютный путь, не
    # вложенность, не побег вверх.
    refused(MASTER_YAML.replace("prompt: soul.md", "prompt: /etc/passwd"),
            "prompt")
    refused(MASTER_YAML.replace("prompt: soul.md", "prompt: ../SKILL.md"),
            "prompt")
    refused(MASTER_YAML.replace("prompt: soul.md", "prompt: sub/soul.md"),
            "prompt")
    refused(MASTER_YAML.replace("prompt: soul.md", "prompt: ''"), "prompt")
    # YAML-опасности: тег исполнения, дубликат ключа, не-отображение.
    refused("soul: !!python/object/apply:os.system [echo hi]\n", "yaml")
    refused(MASTER_YAML + "soul: puppet\n", "duplicate")
    refused("- a\n- b\n", "mapping")
    refused(":\n", "yaml")
    # secrets -- только files/vars списками; недостающее -- пустой список,
    # не отсутствие (#579: «по умолчанию -- пусто»).
    refused(MASTER_YAML + "secrets:\n  files: []\n  vars: []\n  other: []\n",
            "unknown")
    oned = one(MASTER_YAML + "secrets:\n  files: [gitlab-token]\n")
    c.expect("#580 a half secrets block defaults the rest to empty",
             (oned.secrets.files, oned.secrets.vars), (("gitlab-token",), ()))
    refused(MASTER_YAML + "secrets:\n  files: gitlab-token\n  vars: []\n",
            "files")
    # Блок цели -- не «пусто», а ложный тип: `or {}` читал false/[]/0/'' как
    # «блока нет» и молча выдавал пустой allowlist (review #580): явный
    # мусор в схеме -- отказ, а не умолчание.
    for block in ("false", "[]", "0", "''"):
        refused(MASTER_YAML + f"secrets: {block}\n", "secrets")
    # Элементы -- та же каноническая форма, что у хранилища (#592): файл --
    # путь от корня files/ без побегов, масок и ненормализованных написаний;
    # переменная -- KEY грамматики vars.env. Валидное принимается (ложный
    # отказ отрезал бы живые имена), мусор -- отказ.
    for path, ok in (("../escape", False), ("/abs", False),
                     ("a/../b", False), ("ok*", False),
                     ("sub/dir/file", True), (".env", True),
                     ("./plain", False)):
        try:
            soul.parse(MASTER_YAML.replace(
                "prompt: soul.md",
                "prompt: soul.md\nsecrets:\n  files: ['" + path + "']\n"))
            verdict = "accepted"
        except ValueError as e:
            verdict = str(e)
        c.check(f"#580 secrets.files {path!r} follows the #592 name rules",
                verdict != "accepted" if not ok else verdict == "accepted",
                verdict)
    for var, ok in (("OK_VAR", True), ("9LEAD", False), ("HAS-DASH", False),
                    ("with space", False), ("CI_JOB_TOKEN", True),
                    ("_UNDER", True)):
        try:
            soul.parse(MASTER_YAML.replace(
                "prompt: soul.md",
                "prompt: soul.md\nsecrets:\n  vars: ['" + var + "']\n"))
            verdict = "accepted"
        except ValueError as e:
            verdict = str(e)
        c.check(f"#580 secrets.vars {var!r} follows the #592 name rules",
                verdict != "accepted" if not ok else verdict == "accepted",
                verdict)


def check_bundle_580(c):
    """HYPOTHESIS (#580): бандла нет: digest считать некому и не по чем --
    сервер #581 не сможет закрепить каталог по содержимому, а живая
    инкарнация -- продолжать работать со своим бандлом после обновления
    пакета ролей.
    SOLUTION: soul.load(dir) -> Bundle(role, manifest, prompt, digest):
    каталог роли -- ровно два файла, промпт читается сырыми байтами из
    своего каталога (симлинк наружу -- отказ), digest -- формула контракта
    одним общим местом: канонический JSON разобранного манифеста + NUL +
    сырой промпт.
    STATUS: RED"""
    from mop.common import soul
    base = tempfile.mkdtemp(prefix="mop-soul-580-")
    prompt = "You are the master.\n"
    d = make_role(base, "master", MASTER_YAML, prompt)
    bundle = soul.load(d)
    c.expect("#580 the bundle carries the role name", bundle.role, "master")
    c.expect("#580 the prompt rides as raw bytes", bundle.prompt,
             prompt.encode())
    # Формула -- здесь же, по контракту: тест считает её сам.
    c.expect("#580 the digest is the contract formula",
             bundle.digest, contract_digest(MASTER_YAML, prompt))
    # Digest -- по разобранному манифесту: другой порядок ключей того же
    # манифеста -- тот же digest.
    reordered = """version: 1
soul: master
prompt: soul.md
capabilities:
  spawn: [puppet, builder]
  dispatch: true
  cluster_verbs: [{', '.join(MASTER_CLUSTER)}]
  node_verbs: [{', '.join(MASTER_NODE)}]
runtime:
  receives_dispatch: false
  driver: isolating
""".replace("{', '.join(MASTER_CLUSTER)}", ", ".join(MASTER_CLUSTER)) \
     .replace("{', '.join(MASTER_NODE)}", ", ".join(MASTER_NODE))
    same = make_role(base, "same", reordered, prompt)
    c.expect("#580 key order does not change the digest",
             soul.load(same).digest, bundle.digest)
    # Любое содержимое меняет digest: и промпт, и манифест.
    other = make_role(base, "other", MASTER_YAML, "Changed.\n")
    c.check("#580 a changed prompt changes the digest",
            soul.load(other).digest != bundle.digest)
    caps = MASTER_YAML.replace("spawn: [puppet, builder]", "spawn: [puppet]")
    fewer = make_role(base, "fewer", caps, prompt)
    c.check("#580 a changed manifest changes the digest",
            soul.load(fewer).digest != bundle.digest)

    def refused(maker, because):
        try:
            got = soul.load(maker())
        except ValueError as e:
            c.check(f"#580 load refused: {because}", because in str(e).lower(),
                    str(e))
            return
        c.fail(f"#580 load must refuse: {because}", repr(got))

    # Промпт-симлинк наружу каталога -- не бандл роли.
    outside = os.path.join(base, "outside.md")
    with open(outside, "w") as f:
        f.write("stolen\n")

    def symlinked():
        d2 = os.path.join(base, "linkout")
        os.makedirs(d2)
        with open(os.path.join(d2, "soul.yaml"), "w") as f:
            f.write(MASTER_YAML.replace("prompt: soul.md", "prompt: out.md"))
        os.symlink(outside, os.path.join(d2, "out.md"))
        return d2
    refused(symlinked, "outside")

    def noprompt():
        d3 = make_role(base, "noprompt", MASTER_YAML)
        os.remove(os.path.join(d3, "soul.md"))
        return d3
    refused(noprompt, "prompt")
    refused(lambda: make_role(base, "extra", MASTER_YAML, prompt,
                              extra={"notes.txt": "hello"}), "exactly")
    refused(lambda: make_role(base, "binary", MASTER_YAML, b"\xff\xfe raw"),
            "utf-8")

    # ── review #580: отказ мастера на живых ролях ─────────────────────────
    # Digest -- SHA256, 64 знака hex (#579), а запись привязки и тройка
    # меты требовали 32: каждый живой бандл обязан был не проходить собственную
    # привязку. Раздельные правила: HEX64 для digest бандла, HEX32 для id
    # принципала.
    c.check("#580 review: the real bundle digest is 64 lower-case hex",
            len(bundle.digest) == 64
            and all(ch in "0123456789abcdef" for ch in bundle.digest),
            bundle.digest)
    try:
        soul.binding("grant-" + HEX32, "anton", "mop", "pu-mop-1", UUID_OK,
                     bundle.digest, "master")
    except ValueError as e:
        c.fail("#580 review: a binding must take the real 64-hex digest", str(e))
    try:
        domain.JobMeta("o", soul="master", soul_digest=bundle.digest,
                       soul_inc=UUID_OK)
    except ValueError as e:
        c.fail("#580 review: JobMeta must take the real 64-hex digest", str(e))
    # 32 hex -- не digest: усечение не появляется, правило отдельное.
    for short in (bundle.digest[:32], bundle.digest + "a", "x" * 64):
        try:
            soul.binding("grant-" + HEX32, "anton", "mop", "pu-mop-1", UUID_OK,
                         short, "master")
            c.fail(f"#580 review: a binding must refuse digest {short[:8]}…",
                   "accepted")
        except ValueError as e:
            c.check(f"#580 review: a binding refuses digest {short[:8]}…",
                    "64" in str(e), str(e))

    # Roundtrip канонического источника: load → manifest.to_dict() →
    # from_manifest — тот же digest. to_dict вставляет умолчания (secrets),
    # и вставленное меняло digest: сервер (#581) собирал бы бандл, чей
    # digest не совпадает ни с хранилищем, ни с метой джоба. Канонический
    # вход digest — словарь с ПОЛНЫМ набором ключей схемы: один источник
    # канонизации на все входы (load, from_manifest), формула --
    # документирована контрактом.
    rebuilt = soul.from_manifest(bundle.manifest.to_dict(), bundle.prompt)
    c.expect("#580 review: to_dict round-trips to the same digest",
             rebuilt.digest, bundle.digest)
    # Пакетный пресет -- тот же закон: реальный load→store→deliver без
    # расхождения форм. «Доставка» -- словарь, как его хранит сервер (#581)
    # и везёт шина: бандл → словарь → бандл с тем же digest и равным
    # содержимым.
    real = soul.load(os.path.join(PACKAGE, "puppet"))
    delivered = soul.from_manifest(real.manifest.to_dict(), real.prompt)
    c.expect("#580 review: a packaged preset round-trips its digest",
             delivered.digest, real.digest)
    c.expect("#580 review: the delivered bundle equals the stored one",
             (delivered.role, delivered.manifest.to_dict(), delivered.prompt),
             (real.role, real.manifest.to_dict(), real.prompt))
    # Явный пустой блок в yaml -- та же каноническая форма, что вставленный:
    # «по умолчанию -- пусто» не делает два написания одного смысла двумя
    # бандлами.
    explicit = make_role(base, "explicit", MASTER_YAML.replace(
        "prompt: soul.md", "prompt: soul.md\nsecrets:\n  files: []\n  vars: []\n"),
        prompt)
    c.expect("#580 review: an explicit empty secrets block is the same bundle",
             soul.load(explicit).digest, bundle.digest)

    # Промпт -- UTF-8 на обоих входах бандла: load отказывает битым байтам,
    # from_manifest принимал их молча и молча же считал digest (review #580).
    try:
        soul.from_manifest(bundle.manifest.to_dict(), b"\xff\xfe raw")
        c.fail("#580 review: from_manifest must refuse a non-UTF-8 prompt",
               "accepted")
    except ValueError as e:
        c.check("#580 review: from_manifest refuses a non-UTF-8 prompt",
                "utf-8" in str(e).lower(), str(e))

    # active -- точный bool, не «что угодно правдивого»: строка 'false'
    # становилась True и молча активировала привязку (review #580).
    try:
        soul.binding("grant-" + HEX32, "anton", "mop", "pu-mop-1", UUID_OK,
                     bundle.digest, "master", active="false")
        c.fail("#580 review: a binding must refuse active='false'", "accepted")
    except ValueError as e:
        c.check("#580 review: a binding refuses active='false'",
                "true or false" in str(e), str(e))


def check_catalogue_580(c):
    """HYPOTHESIS (#580): каталога ролей нет: пакет везёт один файл скилла
    мастера, ролей puppet/builder нет, четвёртая роль требовала бы правки
    кода.
    SOLUTION: роли живут в пакете по одному на каталог (mop/skills/<role>/:
    soul.yaml + промпт), soul.catalogue(dirs) собирает {роль: каталог};
    внешний каталог установки -- тот же формат, новая роль -- новый каталог,
    без правки Python.
    STATUS: RED"""
    from mop.common import soul
    # Поставляемые роли: мастер (промпт -- прежний SKILL.md, один источник),
    # рабочий и сборщик -- минимальные, по контракту #579.
    got = soul.catalogue([PACKAGE])
    c.expect("#580 the package catalogue holds the three presets",
             sorted(got), ["builder", "master", "puppet"])
    master = soul.load(got["master"])
    c.expect("#580 the master preset keeps SKILL.md as its one prompt",
             master.manifest.prompt, "SKILL.md")
    c.check("#580 the master prompt is the existing skill, one source",
            master.prompt == open(os.path.join(PACKAGE, "master",
                                                "SKILL.md"), "rb").read())
    c.expect("#580 the packaged master manifest matches the contract",
             (master.manifest.runtime.driver,
              master.manifest.runtime.receives_dispatch,
              master.manifest.capabilities.dispatch,
              master.manifest.capabilities.spawn),
             ("isolating", False, True, ("puppet", "builder")))
    # Права мастера -- аудит реальных вызовов его инструментов, а не список
    # «на всякий случай» (review #580): ростер зовёт sizes и, через alloc
    # сервиса, clone (cluster._clone_of); MCP-инструменты -- pool, stderr,
    # states, type, wipe, send, tail. Только чтение и управление своим
    # проектом: админских и секретных глаголов нет и быть не может
    # (грамматика их не пускает).
    c.expect("#580 the master preset carries the audited tool calls",
             (master.manifest.capabilities.node_verbs,
              master.manifest.capabilities.cluster_verbs),
             (tuple(MASTER_NODE), tuple(MASTER_CLUSTER)))
    for role in ("puppet", "builder"):
        m = soul.load(got[role]).manifest
        # Обнаружение адресов (#585) -- чтение своего проекта: ping/roster/
        # alloc/spec, без анонимного broadcast и без locate-прокси.
        c.expect(f"#580 the {role} preset discovers addresses read-only",
                 (m.runtime.driver, m.runtime.receives_dispatch,
                  m.capabilities.dispatch, m.capabilities.spawn,
                  m.capabilities.cluster_verbs),
                 ("any", True, False, (),
                  ("ping", "roster", "alloc", "spec")))
    # Динамическая четвёртая роль -- каталогом, без правки кода.
    base = tempfile.mkdtemp(prefix="mop-soul-cat-580-")
    make_role(base, "watcher", PUPPET_YAML.replace("soul: puppet", "soul: watcher"))
    got = soul.catalogue([PACKAGE, base])
    c.expect("#580 a fourth role joins by directory",
             sorted(got), ["builder", "master", "puppet", "watcher"])
    # Кто раньше в списке -- тот и хозяин имени: приоритет каталогов зовёт
    # вызывающий, а не порядок на диске.
    mine = make_role(base, "puppet", PUPPET_YAML.replace(
        "cluster_verbs: [ping, roster, alloc, spec]",
        "cluster_verbs: [roster]"))
    c.expect("#580 the earlier catalogue wins the role name",
             soul.catalogue([PACKAGE, base])["puppet"], got["puppet"])

    def refused(maker, because):
        try:
            soul.catalogue(maker())
        except ValueError as e:
            c.check(f"#580 catalogue refused: {because}",
                    because in str(e).lower(), str(e))
            return
        c.fail(f"#580 catalogue must refuse: {because}", "")
    refused(lambda: [PACKAGE, os.path.join(base, "nope")], "nope")


def check_principals_580(c):
    """HYPOTHESIS (#580): принципал делегированной роли не закодирован:
    grant-<id> не зарезервирован на шине, человек мог бы зваться
    grant-<32hex> и подменять записи реестра, incarnation-UUID нечем
    выдать, запись привязки -- ни формы, ни кодировок.
    SOLUTION: soul.grant_user/grant_id/is_grant -- префикс grant- и 32 знака
    нижнего hex, soul.incarnation -- стабильный UUID инкарнации,
    soul.binding/binding_from -- запись реестра привязок (#581) одним
    местом; префикс зарезервирован в busnames и запрещён человеку в
    operators.
    STATUS: RED"""
    from mop.common import soul
    c.expect("#580 a grant id becomes the bus principal",
             soul.grant_user(HEX32), "grant-" + HEX32)
    c.expect("#580 the principal parses back",
             soul.grant_id("grant-" + HEX32), HEX32)
    c.expect("#580 a non-grant name parses as no grant",
             soul.grant_id("puppet-mop"), None)
    c.check("#580 a legacy project user is not a grant",
            not soul.is_grant("puppet-mop"))
    c.check("#580 the grant check wants the prefix",
            not soul.is_grant("grant-nope") and not soul.is_grant(HEX32))
    # Принципал -- машинное имя: bus.login (#161) и callout относятся к нему
    # как к машине, иначе grant читался человеческим входом (review #580).
    # Маршрутизация каналов -- #603; здесь только классификация префикса.
    c.check("#580 review: a grant principal is a machine name",
            busnames.is_machine(soul.grant_user(HEX32)),
            busnames.is_machine("puppet-mop"))
    for bad in ("AB" * 16, "a" * 31, "a" * 33, "z" * 32):
        try:
            soul.grant_user(bad)
            c.fail(f"#580 grant_user must refuse {bad!r}", "built")
        except ValueError as e:
            c.check(f"#580 grant_user refuses {bad!r}", "hex" in str(e).lower(),
                    str(e))
    import uuid
    incs = {soul.incarnation() for _ in range(64)}
    c.check("#580 incarnations are unique", len(incs) == 64)
    c.check("#580 an incarnation is a canonical UUID v4",
            all(uuid.UUID(x).version == 4 and str(uuid.UUID(x)) == x for x in incs))
    # Префикс зарезервирован на шине: человеку такое имя не выдать (#579).
    c.expect("#580 busnames holds the grant prefix", busnames.GRANT_PREFIX,
             "grant-")
    c.check("#580 the grant prefix is reserved",
            "grant-" in busnames.RESERVED_PREFIXES
            and busnames.RESERVED_PREFIXES == (
                "master-", "puppet-", "node-", "grant-"),
            busnames.RESERVED_PREFIXES)
    from mop.server import operators
    for name in ("grant-" + HEX32, "grant-hello"):
        try:
            operators.parse(f"{name}:user:mop")
            c.fail(f"#580 operators must refuse the name {name!r}", "built")
        except ValueError as e:
            c.check(f"#580 operators refuse {name!r}", "not a person" in str(e),
                    str(e))
    # Запись привязки (#581): принципал -> {owner, project, job, soul_inc,
    # soul_digest, role, active}. Ключи точные: иное -- не запись.
    record = soul.binding("grant-" + HEX32, "anton", "mop", "pu-mop-1",
                          UUID_OK, "cd" * 32, "master")
    c.expect("#580 a binding record has the exact shape", record,
             {"principal": "grant-" + HEX32, "owner": "anton", "project": "mop",
              "job": "pu-mop-1", "soul_inc": UUID_OK, "soul_digest": "cd" * 32,
              "role": "master", "active": True})
    c.expect("#580 the record round-trips", soul.binding_from(record), record)
    for broken, because in (
            (dict(record, role="Nope!"), "role"),
            (dict(record, soul_digest="x"), "soul_digest"),
            (dict(record, soul_inc="x"), "soul_inc"),
            (dict(record, principal="anton"), "principal"),
            ({k: v for k, v in record.items() if k != "job"}, "job")):
        try:
            soul.binding_from(broken)
            c.fail(f"#580 binding_from must refuse: {because}", repr(broken))
        except ValueError as e:
            c.check(f"#580 binding_from refuses: {because}",
                    because in str(e).lower(), str(e))
    # Поля сервера поверх ядра (#581: request_id, intent, revoked_at) --
    # проекция снимает их, не отказом и не копией.
    dressed = dict(record, request_id="r-7", intent="dispatch", revoked_at=None)
    c.expect("#580 binding_from projects the server's extra fields away",
             soul.binding_from(dressed), record)


def check_row_580(c):
    """HYPOTHESIS (#580): сборка строки ростера (puppets._row) держит мету
    руками и выбрасывает всё, кроме origin: роль папета не доезжает ни
    мастеру, ни дашборду (#585/#587), и каждый потребитель стал бы
    перечитывать Meta джоба сам.
    SOLUTION: строка ростера несёт тройку роли из той же JobMeta -- поля
    soul/soul_digest/soul_inc, отсутствующая роль остаётся None и НЕ меняет
    прежний провод: ключи появляются только у папета с ролью.
    STATUS: RED"""
    item = {"job": {"ID": "pu-mop-1", "Status": "running", "Meta": {
        "origin": "git@h:g/mop.git", "soul": "master",
        "soul_digest": HEX64, "soul_inc": UUID_OK}},
        "alloc": None, "error": None, "state": "free", "kind": "free",
        "owner": None}
    row = puppets._row(item)
    c.expect("#580 the row carries the role triple",
             (row.soul, row.soul_digest, row.soul_inc),
             ("master", HEX64, UUID_OK))
    as_dict = row.to_dict()
    c.expect("#580 the role rides the wire as plain fields",
             (as_dict["soul"], as_dict["soul_digest"], as_dict["soul_inc"]),
             ("master", HEX64, UUID_OK))
    # Легаси-папет без роли: прежние ключи байт в байт, новых -- нет.
    legacy_item = {"job": {"ID": "pu-mop-2", "Status": "running", "Meta": {
        "origin": "git@h:g/mop.git"}}, "alloc": None, "error": None,
        "state": "free", "kind": "free", "owner": None}
    legacy = puppets._row(legacy_item).to_dict()
    c.check("#580 a legacy row keeps the exact old shape",
            not any(k in legacy for k in ("soul", "soul_digest", "soul_inc")),
            legacy)
    c.expect("#580 the legacy wire is byte-for-byte", legacy,
             {"name": "pu-mop-2", "node": "-", "alloc_status": "running",
              "state": "free", "kind": "free", "owner": "-",
              "llm": "-", "detail": None, "branch": None,
              "origin": "git@h:g/mop.git", "disk_kb": None})
    # Провод туда-обратно: строка с ролью читается назад, легаси-словарь --
              # в строку без роли.
    back = state.PuppetRow.from_dict(as_dict)
    c.expect("#580 the row round-trips",
             (back.soul, back.soul_digest, back.soul_inc),
             ("master", HEX64, UUID_OK))
    back = state.PuppetRow.from_dict(legacy)
    c.expect("#580 a legacy dict reads back without a role", back.soul, None)


def check_package_580(c):
    """HYPOTHESIS (#580): package-data везёт ровно один файл --
    skills/master/*: роли не доезжают на сервер установки, и #581/#582
    работают с каталогом, которого нет.
    SOLUTION: данные пакета -- весь каталог ролей skills/*/*: и файл
    скилла мастера, и soul.yaml всех ролей, и промпты; интерфейс снаружи
    (~/.claude/skills/master) не меняется.
    STATUS: RED"""
    import tomllib
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        data = tomllib.load(f)
    shipped = data.get("tool", {}).get("setuptools", {}).get(
        "package-data", {}).get("mop", [])
    c.check("#580 package data ships the whole role catalogue",
            "skills/*/*" in shipped, shipped)


def main():
    c = Checks()
    for fn in (check_meta_580, check_parse_580, check_bundle_580,
               check_catalogue_580, check_principals_580, check_row_580,
               check_package_580):
        try:
            fn(c)
        except Exception as e:
            c.fail(fn.__name__, f"{type(e).__name__}: {e}")
    return c.report("soul")


if __name__ == "__main__":
    sys.exit(main())
