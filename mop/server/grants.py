"""Реестр привязок делегированных принципалов #581 (docs/SOUL.md, #579).

Одна инкарнация -- один принципал `grant-<32hex>` (кодировки, разбор
манифеста и digest -- общий модуль #580, здесь только серверный слой).
Хранилище -- приватный каталог сервера, всё 0600:

    souls/registry.json                     записи принципалов
    souls/nats-grant-<id>.pass              пароль (не идентификатор)
    souls/bundles/<digest>/manifest.json    закреплённый бандл роли
    souls/bundles/<digest>/<промпт>         сырой промпт

Запись -- ядро binding() из #580 плюс серверные метаданные жизненного
цикла (request_id, intent, revoked_at), которые #580 не знает. Один
писатель: fcntl-блокировка на read-modify-write, замена файла атомарна;
одна лишь атомарная замена теряла бы обновления конкурентов.

Пишет подготовка (prepare/activate/revoke/cut), читают callout
(Provider), сервис кластера и bootstrap-глагол resolve (resolve,
resolve_target). Отзыв двухступенчатый (#579): глагольный -- active:false,
живая работа живёт; транспортный (cut) -- снос файла пароля, следующий
вход отказан. Годность входа решает admit: пароль (password_ok) -- одно,
активная связанная привязка -- другое; неактивированный принципал в
callout не входит вовсе -- сырые инбоксы до подтверждения регистрации
никуда не нужны.

Креды в тело -- bootstrap-ответом на подъёме (#590); пароль не попадает
ни в реестр, ни в вывод, ни в спеку. Digest и валидатор манифеста --
общие (#580): стор хранит те же manifest.json и промпт и сверяет бандл
той же функцией, своей копии хеша нет.
"""
import fcntl
import json
import os
import secrets
import threading

from ..common import busnames, fsutil, soul
from ..server import identity

# Каталог хранилища под приватным корнем сервера, рядом с паролями папетов.
STORE = "souls"

# Метаданные жизненного цикла поверх ядра записи #580: их сервер держит
# сам, и общий модуль о них не знает (binding_from их отбрасывает).
PENDING = "request_id", "intent", "revoked_at"


def _root_dir(root):
    """Корень хранилища: явный (проверки) или приватный каталог сервера."""
    if root is not None:
        return root
    from ..server import bootstrap
    return os.path.join(os.path.dirname(bootstrap.PUPPET_CREDS), STORE)


def _locked(path):
    """Блокировка писателя реестра: один процесс и много нитей.

    fcntl держит чужие процессы, threading.Lock -- свои нитки (callout
    зовёт из исполнителя, сервисы -- из своих). Блокировка на файл-замок,
    а не на registry.json: замок переживает атомарную замену реестра."""
    lock = os.path.join(path, "registry.lock")
    fd = open(lock, "a+")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


class _Writer:
    """Read-modify-write реестра под блокировкой, атомарной заменой."""

    def __init__(self, root):
        self.root = root
        self.fd = None
        self.reg = None

    def __enter__(self):
        fsutil.make_private_dir(self.root)
        self.fd = _locked(self.root)
        self.reg = _read(self.root)
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            _write(self.root, self.reg)
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()
        return False


_THREAD_LOCK = threading.Lock()


def _read(root):
    """Реестр из файла; отсутствие -- пустой, битый -- громкий отказ.

    Различены (поправка ревью): «нет файла» -- пустой реестр, «файл есть,
    но не разбирается» -- ValueError: тихо проглоченный битый реестр
    отозвал бы все привязки или, хуже, воскресил отозванные."""
    path = os.path.join(root, "registry.json")
    try:
        with open(path) as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    if not raw.strip():
        return {}
    try:
        reg = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"grants: registry {path} is corrupted: {e}") from e
    if not isinstance(reg, dict):
        raise ValueError(f"grants: registry {path} is not an object")
    return reg


def _write(root, reg):
    """Атомарная замена реестра: fsutil.write_private (tmp + fsync +
    rename, 0600) -- своей копии mkstemp/replace нет, инвариант fsutil."""
    path = os.path.join(root, "registry.json")
    fsutil.write_private(path, json.dumps(reg, ensure_ascii=False,
                                        sort_keys=True))


def _bundle_dir(root, digest):
    return os.path.join(root, "bundles", digest)


def _core(rec):
    """Ядро записи #580 без серверских метаданных."""
    return {k: v for k, v in rec.items() if k not in PENDING}


def _pass_path(root, principal):
    return os.path.join(root, busnames.pass_file(principal))


def prepare(project, role, digest, manifest, prompt, owner, request_id,
            job, soul_inc, intent, root=None, actor=None):
    """Подготовить грант до регистрации: новый принципал, резерв job/inc.

    Идемпотентна по request_id с привязкой к интенту: ретрай того же
    запроса (тот же request_id, тот же интент и полезная нагрузка)
    возвращает сохранённую привязку -- после неопределённого исхода Nomad
    #582 реконсиляция активирует сохранённый принципал, а не заводит
    новый. Тот же request_id с другим интентом/digest/job -- громкий
    отказ ДО регистрации: «похожий» запуск -- другой запрос. Пароль --
    отдельная случайная строка, 0600, в реестр не попадает; бандл
    write-once по дайджесту (#579). Запись неактивна: активного гранта до
    подтверждения регистрации нет. Ответ -- вся запись: принципал в ней
    поле, отдельным кортежем он был бы второй правдой о том же.

    owner -- человек-хозяин записи; actor (#582) -- кто ЗАВЕЛ, отдельным
    полем: делегированный add заводит принципал роли, и его происхождение
    -- факт аудита, не право. Actor не входит в душу: владелец и после
    делегирования человек, и путь загрузки workspace грант не получает."""
    manifest = dict(manifest or {})
    with _THREAD_LOCK, _Writer(_root_dir(root)) as w:
        for principal, rec in w.reg.items():
            if rec.get("request_id") != request_id:
                continue
            same = (rec.get("intent") == intent
                    and rec.get("soul_digest") == digest
                    and rec.get("job") == job and rec.get("soul_inc") == soul_inc
                    and rec.get("owner") == owner and rec.get("project") == project
                    and rec.get("actor") == actor)
            if not same:
                raise ValueError(
                    f"grants: request {request_id} is already prepared with "
                    f"a different payload (intent/digest/job/inc/owner/project/actor)")
            return dict(rec)
        principal = soul.grant_user(secrets.token_hex(16))
        password = secrets.token_urlsafe(32)
        rec = soul.binding(principal, owner=owner, project=project, job=job,
                           soul_inc=soul_inc, soul_digest=digest, role=role,
                           active=False)
        rec.update(request_id=request_id, intent=intent, actor=actor)
        _store_bundle(_root_dir(root), digest, manifest, prompt)
        w.reg[principal] = rec
        fsutil.write_private(_pass_path(_root_dir(root), principal), password + "\n")
        return dict(rec)


def _store_bundle(root, digest, manifest, prompt):
    """Бандл write-once: другие байты под тем же дайджестом -- отказ.

    Digest держит #580 (from_manifest): стор хранит manifest.json и
    сырой промпт, сверяет той же функцией, своей копии хеша нет (#579:
    закрепление -- бандлом в хранилище, не байтами каталога)."""
    if soul.from_manifest(manifest, prompt).digest != digest:
        raise ValueError(f"grants: digest mismatch for bundle {digest[:8]}")
    path = _bundle_dir(root, digest)
    fsutil.make_private_dir(path)
    man = os.path.join(path, "manifest.json")
    pr = os.path.join(path, soul_bundle_name(manifest))
    for name, data in ((man, json.dumps(manifest, ensure_ascii=False,
                                        sort_keys=True).encode()),
                       (pr, prompt)):
        if os.path.exists(name):
            continue
        fsutil.write_private(name, data)
    again = bundle(digest, root=root)
    if again is None or again["manifest"] != manifest or again["prompt"] != prompt:
        raise ValueError(f"grants: bundle {digest[:8]} is pinned with "
                         f"different bytes")


def soul_bundle_name(manifest):
    """Имя файла промпта в хранилище: из манифеста, без своей копии
    разбора (имя файла -- поле манифеста, #580)."""
    return (manifest or {}).get("prompt") or "soul.md"


def activate(principal, root=None):
    """Подтвердить зарезервированную привязку: active становится True.

    Отказы: неизвестный принципал, отозванный. Пересвязь (другой job/inc)
    не делается: реюз имени джоба -- новый грант (#579)."""
    with _THREAD_LOCK, _Writer(_root_dir(root)) as w:
        rec = w.reg.get(principal)
        if rec is None:
            raise LookupError(f"grants: unknown principal {principal}")
        if rec.get("revoked_at"):
            raise ValueError(f"grants: {principal} is revoked; "
                             f"a reused job name needs a new grant")
        rec["active"] = True
        w.reg[principal] = rec
        return dict(rec)


def revoke(principal, root=None):
    """Глагольный отзыв (#579): active:false + момент; идемпотентен.

    Идущая работа живёт: уже запущенный процесс продолжает, новых глаголов
    нет -- их режет resolve."""
    with _THREAD_LOCK, _Writer(_root_dir(root)) as w:
        rec = w.reg.get(principal)
        if rec is None:
            raise LookupError(f"grants: unknown principal {principal}")
        rec["active"] = False
        rec["revoked_at"] = rec.get("revoked_at") or _now()
        w.reg[principal] = rec
        return dict(rec)


def cut(principal, root=None):
    """Транспортный отзыв: снос файла пароля. Следующий вход отказан;
    живое соединение держит свой ACL до переподключения (факт #579)."""
    with _THREAD_LOCK, _Writer(_root_dir(root)) as w:
        if principal not in w.reg:
            raise LookupError(f"grants: unknown principal {principal}")
        try:
            os.unlink(_pass_path(_root_dir(root), principal))
        except FileNotFoundError:
            pass
        return dict(w.reg[principal])


def binding(principal, root=None):
    """Запись принципала, либо None. Ядро #580 + метаданные сервера."""
    with _THREAD_LOCK:
        return dict(_read(_root_dir(root)).get(principal) or {}) or None


def by_request(request_id, root=None):
    """Сохранённая запись запроса, либо None (#582: ретрай).

    Слепа к состоянию -- и нарочно: ретрай после неопределённого исхода
    Nomad обязан найти СОХРАНЁННУЮ привязку в любом состоянии, включая
    отозванную (идемпотентность переживает отзыв), и выбрать про неё,
    а не заводить дубль. Две записи с одним request_id -- повреждение
    реестра: громкий отказ, не произвольный выбор."""
    got = [rec for rec in _read(_root_dir(root)).values()
           if rec.get("request_id") == request_id]
    if len(got) > 1:
        raise ValueError(f"grants: request {request_id} is registered "
                         f"twice: {', '.join(sorted(r.get('principal', '?')
                                                    for r in got))}")
    return dict(got[0]) if got else None


def _select_job(reg, project, job, soul_inc=None):
    """Привязка имени джоба из СНИМКА реестра -- ядро by_job и блока цели.

    Без инкарнации -- только живая запись (pending/revoked невидимы);
    две живых на одном имени -- ValueError: переиспользованное имя (#579)
    держит Новый грант, молчаливый выбор одной из них адресовал бы чужую
    душу. С инкарнацией -- та самая запись С её состоянием: bootstrap
    (#590) сверяет фактическую инкарнацию Nomad, а не только имя, и сам
    решает, что значит отозванная или ещё не подтверждённая."""
    got = [rec for rec in reg.values()
           if rec.get("project") == project and rec.get("job") == job
           and (soul_inc is None or rec.get("soul_inc") == soul_inc)]
    if soul_inc is None:
        got = [rec for rec in got
               if rec.get("active") and not rec.get("revoked_at")]
    if len(got) > 1:
        which = f"{project}/{job}" + (f"/{soul_inc}" if soul_inc else "")
        raise ValueError(f"grants: {which} has ambiguous bindings: "
                         f"{', '.join(sorted(r.get('principal', '?')
                                            for r in got))}")
    return dict(got[0]) if got else None


def by_job(project, job, soul_inc=None, root=None):
    """Живая привязка имени джоба (#584/#590), либо None.

    Публичный поиск поверх _select_job: потребители не открывают реестр
    на прямую. Живость без инкарнации -- active и не отозвана; точная
    запись по инкарнации приходит с её состоянием -- решение о ней
    за вызывающим."""
    with _THREAD_LOCK:
        return _select_job(_read(_root_dir(root)), project, job, soul_inc)


def admit(principal, root=None):
    """Годен ли принципал к НОВОМУ входу: активная связанная привязка.

    Не пароль (его проверяет password_ok) и не глагол (их режет resolve):
    неактивированный или отозванный принципал в callout не входит вовсе --
    сырые _INBOX-привилегии до подтверждения регистрации не нужны."""
    rec = binding(principal, root=root)
    if rec is None:
        return False, f"grants: unknown principal {principal}"
    if rec.get("revoked_at") or not rec.get("active"):
        return False, f"grants: {principal} is not an active binding"
    if not rec.get("job") or not rec.get("soul_inc"):
        return False, f"grants: {principal} is not bound to an incarnation"
    return True, ""


def password_ok(principal, password, root=None):
    """Верен ли пароль: constant-time, только пароль."""
    import hmac
    try:
        with open(_pass_path(_root_dir(root), principal)) as f:
            stored = f.read().strip()
    except OSError:
        return False
    return bool(stored) and hmac.compare_digest(stored, password or "")


def bundle(digest, root=None):
    """Закреплённый бандл по дайджесту, либо None."""
    path = _bundle_dir(_root_dir(root), digest)
    try:
        with open(os.path.join(path, "manifest.json")) as f:
            manifest = json.load(f)
        with open(os.path.join(path, soul_bundle_name(manifest)), "rb") as f:
            prompt = f.read()
    except (OSError, ValueError):
        return None
    return {"manifest": manifest, "prompt": prompt}


def resolve(principal, service, verb, target=None, root=None):
    """Вердикт ИСТОЧНИКУ по wire-contrакту #579: один вызов -- один
    атомарный снимок реестра, источник и цель из одного чтения.

    Ответ: эхо источника (principal, job, soul_inc, owner, project,
    soul_digest, role, active, capabilities, source_dispatch) и, при
    названной цели, вложенный блок target{found, job, project, role,
    receives_dispatch, soul_inc, ...}. Глагол -- по спискам роли, fail
    closed; кэша нет: отсутствие -- максимальный TTL. resolve_target --
    внутренний помощник того же снимка, отдельного wire-глагола нет."""
    reg = _read(_root_dir(root))
    rec = reg.get(principal)
    if rec is None:
        return {"allow": False,
                "reason": f"grants: unknown principal {principal}"}
    if rec.get("revoked_at") or not rec.get("active"):
        return {"allow": False,
                "reason": f"grants: {principal} is not an active binding"}
    got = bundle(rec.get("soul_digest"), root=root)
    if got is None:
        return {"allow": False,
                "reason": f"grants: bundle {rec.get('soul_digest')} "
                          f"is not in the store"}
    parsed = soul.parse(json.dumps(got["manifest"]))
    verbs = tuple(getattr(parsed.capabilities,
                          "node_verbs" if service == "node"
                          else "cluster_verbs"))
    out = {"allow": verb in verbs,
           "principal": principal,
           "job": rec.get("job"), "soul_inc": rec.get("soul_inc"),
           "owner": rec.get("owner"), "project": rec.get("project"),
           "soul_digest": rec.get("soul_digest"), "role": rec.get("role"),
           "active": True,
           "capabilities": {"node_verbs": list(parsed.capabilities.node_verbs),
                            "cluster_verbs": list(parsed.capabilities.cluster_verbs)},
           "source_dispatch": parsed.capabilities.dispatch}
    if target:
        out["target"] = _target_block(reg, rec.get("project"), target, root)
    if not out["allow"]:
        out["reason"] = (f"{principal} may not {service} {verb}: the role "
                         f"{rec.get('role')} allows "
                         f"{', '.join(verbs) or 'no verbs'}")
    else:
        out["reason"] = ""
    return out


def _target_block(reg, project, target, root):
    """Вложенный блок цели из ТОГО ЖЕ снимка реестра: второй ход в шину
    за целью разорвал бы атомарность одного эффекта (#579). Выбор -- то
    же ядро, что by_job: неоднозначные живые привязки цели -- тот же
    громкий отказ, не произвольная первая подошедшая."""
    rec = _select_job(reg, project, target)
    if rec is None:
        return {"found": False}
    return {"found": True, "job": rec.get("job"),
            "project": rec.get("project"),
            "role": rec.get("role"),
            "receives_dispatch": _target_flag(root, rec),
            "soul_inc": rec.get("soul_inc"),
            "owner": rec.get("owner"),
            "active": bool(rec.get("active"))}


def resolve_target(project, target, root=None):
    """Вердикт ЦЕЛИ как внутренний помощник: тот же блок, что resolve
    вкладывает в ответ. Сервер отвечает за право адресата (#579): узел
    не спрашивает Nomad, которого у него нет; транзакцию аренды/git
    держит узел (#584)."""
    return _target_block(_read(_root_dir(root)), project, target, root)


def _target_flag(root, rec):
    got = bundle(rec.get("soul_digest"), root=root)
    if got is None:
        return False
    return _runtime(got["manifest"], "receives_dispatch")


def _role_verbs(manifest, service):
    parsed = soul.parse(json.dumps(manifest))
    return tuple(getattr(parsed.capabilities,
                         "node_verbs" if service == "node"
                         else "cluster_verbs"))


def _flag(manifest, name):
    return bool((manifest.get("capabilities") or {}).get(name))


def spawn_of(principal, root=None):
    """Разрешённые роли цели принципала (#582): список spawn его бандла.
    Пусто -- принципал никого не поднимает; нет бандла -- тоже пусто:
    fail closed. Делегированный add сверяет роль цели по ЭТОМУ списку,
    не по грамматике субъекта: право поднимать -- у души, не у провода."""
    rec = binding(principal, root=root)
    if rec is None:
        return ()
    got = bundle(rec.get("soul_digest"), root=root) or {}
    return tuple((got.get("manifest") or {}).get(
        "capabilities", {}).get("spawn") or ())


def _runtime(manifest, name):
    return bool((manifest.get("runtime") or {}).get(name))


def _now():
    import time
    return time.time()


def permissions(principal, rec=None, root=None):
    """Права соединения принципала: субъекты busnames, свой токен.

    Проволочное ограждение #603: НИ ОДНОГО легаси-rpc-субъекта человека --
    ни node rpc, ни cluster rpc, ни с логином, ни без. Старый агент или
    контроллер на смешанном роллауте исполнял бы глагол роли БЕЗ
    резолвера; role-субъекты (node.<n>.role.<grant>, cluster.role.<grant>)
    высаживает #603, до них принципал публикацией в чужие каналы не
    говорит вовсе. Dispatch -- отдельный канал, без изменений. Подписка --
    только _INBOX (ответы); после #603 -- собственный приватный префикс.
    Легаси-набор puppet-<проект> (ан .msg, broadcast, events) принципалу
    не достаётся вовсе (#579)."""
    rec = rec or binding(principal, root=root)
    if rec is None:
        return {"publish": [], "subscribe": []}
    got = bundle(rec.get("soul_digest"), root=root) or {}
    manifest = got.get("manifest") or {}
    project = rec.get("project")
    gid = soul.grant_id(principal) or principal
    publish = [busnames.INBOX]
    if _flag(manifest, "dispatch"):
        publish.append(busnames.dispatch_subject(project, "*", gid))
    # Кластерные глаголы роли едут субъектом cluster.role.<grant> (#603),
    # не легаси cluster.rpc: helper высаживает #603, до него -- не выдан.
    return {"publish": publish, "subscribe": [busnames.INBOX]}


class Provider:
    """Гейт callout для принципалов: пароль + годность входа.

    Тем же интерфейсом, что провайдеры людей и папетов: callout.rights
    ведёт его своей веткой ДО веток машины и человека -- принципал не
    человек и не auth_users-машина. Корень хранилища -- его поле: у
    callout своего нет, права рендерятся тем же корнем, каким проверялся
    пароль."""

    def __init__(self, root=None):
        self.root = root

    def authenticate(self, user, password):
        """-> запись привязки, либо identity.Refused."""
        if not soul.is_grant(user):
            raise identity.Refused(f"{user!r} is not a grant principal")
        if not password_ok(user, password, root=self.root):
            raise identity.Refused(f"{user}: wrong password")
        ok, why = admit(user, root=self.root)
        if not ok:
            raise identity.Refused(f"{user}: {why}")
        return binding(user, root=self.root)

    def permissions(self, user):
        """Права соединения принципала: своим корнем хранилища."""
        return permissions(user, root=self.root)
