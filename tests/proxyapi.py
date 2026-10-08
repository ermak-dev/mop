#!/usr/bin/env python3
"""Проверки команды ротации клиентского ключа: python3 tests/proxyapi.py

#539: библиотека #538 -- ОДИН оркестратор; серверный адаптер даёт ей
настоящие transport-швы (management API петли + клиентский probe), CLI --
тонкий фасад группы глаголов. Граница контроллера -- инвентарь
(как mop server user) плюс явный отказ puppet-контексту, ДО сети и файлов.
Ни одного живого запроса: HTTP-транспорт подменён на границе адаптера,
узлы -- фейковый список, пути -- временный каталог.

HYPOTHESIS (#539): без адаптера библиотечные швы -- NotImplementedError,
а CLI-логика в командлете -- вторая копия оркестратора, которая
разъедется с #538 на первом же изменении контракта.
SOLUTION: mop/server/proxyapi.py -- Effects-адаптер (urllib, только
петля, ProxyHandler({}) без redirect, ключ management из файла) +
граница контроллера + resolve путей + доставка прямым bus-каналом по
ЯВНОМУ списку узлов; mop/cli/server/proxykey/{__init__,start,resume,status}
-- печать safe-проекции, rc 0/1.
STATUS: FIXED (#539, раунд ревью) -- блокер конструктора (затенение
mgmt_key_path) и дефекты 1-7 закрыты; пробел 0abeaeb: 401
управленческого GET несёт http_status+reason, предупреждение о
блокировке 30 минут; ревью c7272bb: записанный start -- тихий rc 0,
класс исключения -- ManagementRefused (не внутреннее имя в блокере);
конфликт флагов получателей -- отказ; nodes_refusal удалён. Upstream
закреплён архивом v8.0.16 (sdk_config.go:63, config_v8.go:57,72,112).
"""
import io
import json
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import proxykey  # noqa: E402

SECRETS = ("old-key-539", "new-key-539", "mgmt-key-539")


# ── общий стенд: временные source/copy/mgmt/op + фейковый транспорт ──────
class FakeHTTP:
    """Транспорт адаптера на границе urllib: без сети, с журналом.

    GET api-keys -> список; PUT -> ({"http_status":..,"reason":..} | None);
    /v1/models?key -> 200 | 401. Ответы подставляет проверка."""

    def __init__(self, keys, accepted, put_outcome=None):
        self.keys = list(keys)
        self.accepted = set(accepted)
        self.put_outcome = put_outcome
        self.log = []

    def get_keys(self, bearer):
        self.log.append(("get", bearer))
        return list(self.keys)

    def put_keys(self, bearer, body):
        self.log.append(("put", bearer, list(body)))
        self.keys = list(body)
        return self.put_outcome

    def models(self, key):
        self.log.append(("probe", key))
        return 200 if key in self.accepted else 401


def stand(c, keys=("old-key-539",), accepted=("old-key-539",),
          put_outcome=None):
    """Временный контроллер: source, копия кластера, ключ management,
    состояние операции; (пути, FakeHTTP)."""
    d = tempfile.mkdtemp(prefix="mop-539-")
    src = os.path.join(d, "llm-proxy-client.pass")
    cp = os.path.join(d, "cluster-home", ".config", "mop", "secrets",
                      "llm-proxy-client.pass")
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    mgmt = os.path.join(d, "secrets", "llm-proxy-mgmt.pass")
    os.makedirs(os.path.dirname(mgmt), exist_ok=True)
    for p, k in ((src, keys[0]), (cp, keys[0]), (mgmt, "mgmt-key-539")):
        with open(p, "w", encoding="utf-8") as f:
            f.write(k + "\n")
        os.chmod(p, 0o600)
    op = os.path.join(d, "proxy-rotation.json")
    lock = os.path.join(d, "proxy-rotation.lock")
    inv = os.path.join(d, "inventory.yaml")
    with open(inv, "w", encoding="utf-8") as f:
        f.write("all:\n  hosts: {}\n")
    return {"src": src, "cp": cp, "mgmt": mgmt, "op": op, "lock": lock,
            "inv": inv, "dir": d}, FakeHTTP(keys, accepted, put_outcome)


def effects_of(http, mgmt_path):
    """Адаптер с подсунутым транспортом и файлом ключа management.

    Путь подсунут ЯВНО только там, где проверка про другой шов: путь по
    умолчанию (HOME-граница) проверяет свой отдельный прогон ниже --
    ретесты ревью, что штатный конструктор без аргументов работает.
    Старое имя параметра не ловится: регрессия сигнатуры обязана
    падать, а не откатываться молча."""
    from mop.server import proxyapi
    return proxyapi.Effects(transport=http, mgmt_file=mgmt_path)


def no_network_log(c, http, what):
    c.check(f"#539 {what}: ни одного касания транспорта",
            not http.log, http.log)


# ── ретесты ревью: блокер конструктора и исходы probe ────────────────────
# HYPOTHESIS (блокер ревью): параметр mgmt_key_path затенял одноимённую
# функцию модуля, и штатный путь _common.effects() -> Effects() падал с
# TypeError 'NoneType' object is not callable: start и resume сломаны,
# а проверки этого не видели, потому что всегда подставляли путь.
def check_default_ctor_539(c):
    from mop.server import proxyapi
    box, http = stand(c)
    # Конструктор строится так же, как фронтенд: без mgmt_key_path.
    # Путь management-ключа по умолчанию -- paths.local: подменяем HOME,
    # чтобы он лёг в stand-каталог.
    from mop.common import paths
    with patched_env(HOME=box["dir"], INVENTORY=box["inv"]):
        want = paths.local(paths.SECRETS, "llm-proxy-mgmt.pass")
        os.makedirs(os.path.dirname(want), exist_ok=True)
        with open(want, "w", encoding="utf-8") as f:
            f.write("mgmt-key-539\n")
        try:
            eff = proxyapi.Effects(transport=http)
            c.check("#539 the no-arg construction works like the frontend",
                    eff is not None, eff)
            c.check("#539 the default management key path is the "
                    "controller's own",
                    eff._mgmt == want, (eff._mgmt, want))
        except TypeError as e:
            c.check("#539 the no-arg construction works like the frontend",
                    False, repr(e))


# HYPOTHESIS (дефект 1 ревью): probe == 200 засчитывал любой не-200
# (502 у лежащего прокси, 404, 500) как «ключ отвергнут» -- негативная
# проверка авторизации проходила ложно. Исходов три: 200 -> True,
# 401/403 -> False, остальное -- исключение оркестратору.
def check_probe_outcomes_539(c):
    from mop.server import proxyapi
    box, http = stand(c)
    eff = effects_of(http, box["mgmt"])

    class StatusHTTP(FakeHTTP):
        def __init__(self, statuses):
            self.statuses = list(statuses)
            self.log = []

        def models(self, key):
            self.log.append(("probe", key))
            return self.statuses.pop(0)

    for code, want in ((200, True), (401, False), (403, False)):
        st = StatusHTTP([code])
        eff = effects_of(st, box["mgmt"])
        got = eff.probe("old-key-539")
        c.check(f"#539 probe {code} maps to {want}",
                got is want, (code, got))

    for code in (502, 404, 500):
        st = StatusHTTP([code])
        eff = effects_of(st, box["mgmt"])
        try:
            eff.probe("old-key-539")
            c.fail(f"#539 probe {code} must not be a verdict")
        except RuntimeError as e:
            c.check(f"#539 probe {code} raises to the orchestrator",
                    "probe" in str(e).lower() or str(code) in str(e), repr(e))


# ── дополнение ревью: исходы PUT по кодам и слово блокера ────────────────
# HYPOTHESIS (дополнение, pu-mop-3): 400/422 -- подтверждённый отказ
# валидации ДО записи -> refused с причиной invalid; 401 -> unauthorized,
# 403 -> forbidden; 500 write_failed -- исход неясен, исключение. Причина
# выводится ИЗ КОДА (живой _Urllib не везёт reason), не из тела ответа.
def check_put_reasons_539(c):
    from mop.server import proxyapi
    # Живой транспорт возвращает только код -- как здесь.
    for code, want in ((400, "invalid"), (422, "invalid"),
                       (401, "unauthorized"), (403, "forbidden")):
        box, http = stand(c, put_outcome={"http_status": code})
        eff = effects_of(http, box["mgmt"])
        tag, why = eff.put_list(["old-key-539"])
        c.check(f"#539 PUT {code} is refused as {want}",
                tag == "refused" and why == {"http_status": code,
                                             "reason": want}, (tag, why))
    # 500 -- исход неясен: исключение, не refused.
    box, http = stand(c, put_outcome={"http_status": 500})
    eff = effects_of(http, box["mgmt"])
    try:
        eff.put_list(["old-key-539"])
        c.fail("#539 PUT 500 must stay unknown, not refused")
    except RuntimeError:
        c.check("#539 PUT 500 stays unknown (raises)", True, None)


# HYPOTHESIS (дополнение): 5 неудач management блокируют адрес на 30
# минут, исключения для петли нет. Слово блокера 401 обязано предупреждать
# о блокировке, 403 -- «доступ запрещён, возможна блокировка».
def check_show_wording_539(c):
    from mop.cli.server.proxykey import _common
    for status, reason, needle in (
            (401, "unauthorized", "30 minutes"),
            (403, "forbidden", "denied")):
        out = {"ok": False, "stage": "listed", "refused": True,
               "blocker": "PUT refused before applying",
               "http_status": status, "reason": reason}
        with patched(sys, stderr=io.StringIO(), stdout=io.StringIO()):
            rc = _common.show(out, as_json=False)
            text = sys.stderr.getvalue()
        c.check(f"#539 a refused {status} names the lockout in text",
                needle in text, text)
    out = {"ok": False, "stage": "listed", "refused": True,
           "blocker": "PUT refused before applying",
           "http_status": 400, "reason": "invalid"}
    with patched(sys, stderr=io.StringIO(), stdout=io.StringIO()):
        _common.show(out, as_json=False)
        text = sys.stderr.getvalue()
    c.check("#539 a validation refusal names it invalid",
            "validation refused" in text, text)


# ── мелочь ревью: конфликт флагов получателей -- отказ ────────────────────
# HYPOTHESIS: одновременные --nodes/--all-registered/--no-recipients молча
# выбирали один набор -- оператор не узнаёт, какой заморожен.
def check_flag_conflict_539(c):
    from mop.cli.server.proxykey import _common
    combos = ({"--nodes": "node1", "--all-registered": True},
              {"--nodes": "node1", "--no-recipients": True},
              {"--all-registered": True, "--no-recipients": True},
              {"--nodes": "node1", "--all-registered": True,
               "--no-recipients": True})
    for opts in combos:
        why = _common.recipients(opts)
        c.check(f"#539 conflicting recipient flags {[k for k in opts]}"
                " are refused",
                isinstance(why, str) and "one recipient" in why, why)


# ── пробел ревью: 401 управленческого GET идёт ПЕРВЫМ, до PUT ─────────────
# HYPOTHESIS: при неверном ключе management первым запросом resume идёт
# GET списка; предупреждение о блокировке должно срабатывать уже на нём.
# Сквозная: resume с фейковым транспортом, отдающим 401 на GET -- не
# вручную собранный out.
def check_get_lockout_539(c):
    from mop.cli.server.proxykey import resume, _common
    from mop.server import proxyapi
    box, http = stand(c)
    http.log.clear()
    # Транспорт отдаёт 401 на GET: шов _CodeRefused с целым кодом.
    refused = proxyapi._HTTPStatus("GET api-keys answered HTTP 401", 401)

    class RefusingHTTP(FakeHTTP):
        def get_keys(self, bearer):
            self.log.append(("get", bearer))
            raise refused

    rh = RefusingHTTP(keys=[], accepted=[])
    rh.log = []
    # Начатая операция в стадии ready: resume пойдёт в remote-шаг.
    eff = effects_of(rh, box["mgmt"])
    # Preflight ищет management-ключ, begin -- источник: оба в
    # paths.local ПОД подклеенным HOME, внутри того же контекста.
    from mop.common import paths
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]), \
            patched_env(INVENTORY=box["inv"], MOP_PROJECT=None,
                        MOP_BUS_CONFIG=None, HOME=box["dir"],
                        MOP_HOME=os.path.join(box["dir"], "abs-home")), \
            patched(_common, effects=lambda: eff), \
            patched(sys, stdout=io.StringIO(), stderr=io.StringIO()):
        for name, key in (("llm-proxy-mgmt.pass", "mgmt-key-539"),
                          ("llm-proxy-client.pass", "old-key-539")):
            p = paths.local(paths.SECRETS, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(key + "\n")
            os.chmod(p, 0o600)
        cp = paths.under(os.path.join(box["dir"], "abs-home"),
                         paths.SECRETS, "llm-proxy-client.pass")
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f:
            f.write("old-key-539\n")
        os.chmod(cp, 0o600)
        begin_rc = None
        from mop.cli.server.proxykey import start
        begin_rc = start.main(["--no-recipients", "--exclusive"])
        rh.log.clear()
        rc = resume.main(["--exclusive"])
        err_text = sys.stderr.getvalue()
    # Контракт #539: записанный start -- тихий rc 0 (ревью c7272bb);
    # resume на GET-401 -- rc 1 с предупреждением.
    c.check("#539 resume on a GET 401 reaches the remote step and fails",
            begin_rc == 0 and rc == 1, (begin_rc, rc))
    # «401» не в тексте НЕ потому, что код вреден: текстовый путь несёт
    # формулировку _wording (строки для человека), а типизированный код
    # живёт в http_status-поле JSON-проекции. Здесь проверяем связку
    # «предупреждение есть, сырого эха полей в свободный текст нет».
    c.check("#539 the GET-401 resume blocker warns about the lockout",
            "30 minutes" in err_text and "401" not in err_text, err_text)
    c.check("#539 the refusal came from the GET, not a PUT",
            any(e[0] == "get" for e in rh.log)
            and not any(e[0] == "put" for e in rh.log), rh.log)


# ── граница контроллера: puppet-контекст и не-контроллер отказ ДО сети ──
# HYPOTHESIS: без границы мастер-шелл/узел/пул-сессия дошли бы до
# management API с файлами контроллера. UID -- НЕ граница: папет может
# жить под тем же UID. Граница -- инвентарь установки (как mop server
# user) плюс явный отказ puppet-окружению (MOP_PROJECT/MOP_BUS_CONFIG,
# их сеет spec.py) ДО сети и файлов.
def check_controller_guard_539(c):
    from mop.server import proxyapi
    box, http = stand(c)
    eff = effects_of(http, box["mgmt"])

    # Инвентарь есть, puppet-окружения нет -- это контроллер.
    with patched_env(INVENTORY=box["inv"], MOP_PROJECT=None,
                     MOP_BUS_CONFIG=None):
        why = proxyapi.refusal_if_not_controller()
    c.check("#539 the controller passes the boundary", why is None, why)

    # Машина без инвентаря -- не контроллер (как mop server user).
    with patched_env(INVENTORY=os.path.join(box["dir"], "nope.yaml")):
        why = proxyapi.refusal_if_not_controller()
    c.check("#539 a machine without the inventory is refused",
            isinstance(why, str), why)

    # Puppet-контекст на самой машине контроллера -- отказ: папет может
    # жить под тем же UID и тем же домом, env-признак решает.
    for marker in ("MOP_PROJECT", "MOP_BUS_CONFIG"):
        with patched_env(INVENTORY=box["inv"], **{marker: "proj"}):
            why = proxyapi.refusal_if_not_controller()
        c.check(f"#539 a puppet context ({marker}) is refused "
                "even on the controller",
                isinstance(why, str) and "puppet" in why.lower(), why)
    no_network_log(c, http, "the boundary refusals")


# ── адаптер: pinned v8 маршруты, только петля, ключ из файла ────────────
def check_adapter_pinned_539(c):
    from mop.server import proxyapi
    box, http = stand(c)
    eff = effects_of(http, box["mgmt"])
    got = eff.fetch_list()
    c.expect("#539 fetch returns the pinned GET list", got, ["old-key-539"])
    c.check("#539 fetch hit the pinned api-keys route",
            http.log == [("get", "mgmt-key-539")], http.log)

    http.log.clear()
    verdict = eff.put_list(["old-key-539", "new-key-539"])
    c.expect("#539 a clean PUT is applied", verdict, ("applied", None))
    c.check("#539 PUT sent the whole list",
            http.log == [("put", "mgmt-key-539",
                          ["old-key-539", "new-key-539"])], http.log)

    http.log.clear()
    c.check("#539 the client probe is a client-key check",
            eff.probe("old-key-539") is True
            and http.log == [("probe", "old-key-539")], http.log)
    # Bearer management читается из файла и едет только в транспорт.
    c.check("#539 the management key rides only inside the transport calls",
            all(g[1] == "mgmt-key-539" for g in http.log
                if g[0] in ("get", "put")), http.log)
    c.check("#539 an unaccepted client key probes False",
            eff.probe("nope") is False, None)

    url = proxyapi.base_url()
    c.check("#539 the base URL is loopback with the settings port",
            url == "http://127.0.0.1:8317", url)
    # Порт -- из настройки, валидируется 1..65535: не клиентские данные.
    for bad in ("0", "65536", "x", "-1"):
        with patched_env(MOP_LLM_PROXY_PORT=bad):
            why = proxyapi.port_refusal()
        c.check(f"#539 port {bad!r} is refused before the URL",
                isinstance(why, str), why)
    with patched_env(MOP_LLM_PROXY_PORT="9317"):
        c.check("#539 a valid settings port builds the URL",
                proxyapi.base_url() == "http://127.0.0.1:9317",
                proxyapi.base_url())
    # Никаких клиентских URL: базу строит адаптер, только 127.0.0.1.
    c.check("#539 no client-controlled URL enters the adapter",
            "base_url" in dir(proxyapi) and "url" not in
            inspect_signature(eff), None)


def inspect_signature(eff):
    """Имена параметров Effects: URL пользователем не задаётся."""
    return [k for k in vars(eff) if not k.startswith("_")] + \
           [k for k in type(eff).__dict__ if not k.startswith("_")]


# ── адаптер: исходы PUT по контракту #537/#538 ──────────────────────────
def check_adapter_outcomes_539(c):
    from mop.server import proxyapi
    # Подтверждённый отказ ДО применения: 403 + enum reason.
    box, http = stand(c, put_outcome={"http_status": 403,
                                      "reason": "forbidden"})
    eff = effects_of(http, box["mgmt"])
    tag, why = eff.put_list(["old-key-539"])
    c.expect("#539 a pre-apply refusal carries the tag", tag, "refused")
    c.expect("#539 the refusal carries the http status",
             why["http_status"], 403)
    c.expect("#539 the refusal carries the enum reason", why["reason"],
             "forbidden")

    # Неясные исходы (не 401/403, не.handler-proven) -- unknown-путь
    # библиотеки: исключение наружу, решает оркестратор.
    for code in (500, 200, 301):
        box, http = stand(c, put_outcome={"http_status": code})
        eff = effects_of(http, box["mgmt"])
        try:
            eff.put_list(["old-key-539"])
            c.fail(f"#539 HTTP {code} on PUT must not be a clean verdict")
        except Exception as e:  # noqa: BLE001 -- исход неясен, не отказ
            c.check(f"#539 HTTP {code} on PUT raises to the orchestrator",
                    type(e).__name__ in ("RuntimeError", "OSError",
                                         "TimeoutError"), repr(e))


# ── resolve путей: источник и копия раздельны, один путь разрешён ────────
def check_paths_539(c):
    from mop.server import proxyapi
    box, http = stand(c)
    # Копия стоит под cluster-home/.config/mop/secrets: MOP_HOME -- сам
    # cluster-home (paths.under дом + .config/mop/secrets/файл).
    mophome = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(box["cp"]))))
    with patched_env(HOME=box["dir"], MOP_HOME=mophome):
        src, cp = proxyapi.resolve_paths()
    c.check("#539 the source resolves under the controller HOME",
            src == os.path.join(box["dir"], ".config", "mop", "secrets",
                                "llm-proxy-client.pass"), src)
    c.check("#539 the copy resolves under the MOP_HOME setting",
            cp == box["cp"], (cp, box["cp"]))

    # Один и тот же физический путь разрешён: маленькая установка.
    with patched_env(HOME=box["dir"]):
        src2, cp2 = proxyapi.resolve_paths(cluster_copy=box["src"])
    c.expect("#539 an explicit matching physical path is allowed",
             (src2, cp2), (os.path.join(box["dir"], ".config", "mop",
                                        "secrets",
                                        "llm-proxy-client.pass"),
              box["src"]))

    # Режим копии -- до эффекта, ДО remote: resume-гuard проверяет
    # пригодность обоих путей (режим/владелец) перед КАЖДОЙ мутирующей
    # фазой, и отказ едет ДО первого сетевого касания. begin только
    # записывает приватный чекпоинт -- это его контракт (#538).
    http.accepted.add("new-key-539")
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]):
        proxykey.begin(box["src"], box["cp"], ["node1"], exclusive=True,
                       effects=effects_of(http, box["mgmt"]))
    os.chmod(box["cp"], 0o644)
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]):
        out = proxykey.resume(exclusive=True,
                              effects=effects_of(http, box["mgmt"]))
    c.check("#539 a non-private copy refuses resume before any effect",
            not out.get("ok") and "private" in str(out.get("blocker", ""))
            and not http.log, (out, http.log))
    os.chmod(box["cp"], 0o600)


# ── получатели: явный список, замороженный, без ready-фильтра ───────────
def check_recipients_539(c):
    from mop.server import proxyapi
    box, http = stand(c)

    # Полный известный состав -- БЕЗ ready-фильтра: offline-узел тоже
    # получатель, его «NOT REACHED» держит операцию открытой.
    fake_pool = [{"name": "node1", "status": "ready", "placeable": True},
                 {"name": "node2", "status": "down", "placeable": True},
                 {"name": "node3", "status": "drained", "placeable": False}]

    class Row:
        def __init__(self, n):
            self.name = n["name"]

    from mop.common import puppets as puppets_mod
    with patched(puppets_mod, pool=lambda: [Row(n) for n in fake_pool]):
        got = proxyapi.all_nodes()
    c.expect("#539 the recipient set is the full pool, no ready filter",
             sorted(got), ["node1", "node2", "node3"])

    # Недоступный список узлов -- блокер ДО эффекта, не пустой набор:
    def boom():
        raise RuntimeError("cluster unreachable")

    with patched(puppets_mod, pool=boom):
        why = proxyapi.all_nodes_guarded()
    c.check("#539 an unreachable node listing blocks, not empties",
            isinstance(why, str), why)


# ── доставка: прямой bus-канал по ЯВНОМУ списку, не proxykey.push ───────
# HYPOTHESIS: old push() решает по sha-гейту и settled -- резюмируемая
# ротация с FAILED/NOT REACHED «завершилась» бы или молча пропустила.
def check_delivery_539(c):
    from mop.server import proxyapi
    from mop.common import bus
    box, http = stand(c, accepted=("old-key-539",))
    eff = effects_of(http, box["mgmt"])
    sent = []

    def fake_request_many(verb, nodes, timeout=None, channel=None,
                          project=None, files=None, **fields):
        sent.append((verb, list(nodes), files))
        # Ответ агента write -- словарь written (#135), не строка:
        # строки сводит bus.results_from, агент их не говорит.
        return {n: {"written": ["secrets.env"]} for n in nodes}

    with patched(bus, request_many=fake_request_many):
        out = eff.deliver("new-key-539", ["node1", "node2"])
    c.check("#539 delivery goes through the write channel with the "
            "explicit saved nodes",
            sent and sent[0][0] == "write" and sent[0][1] == ["node1", "node2"],
            sent)
    files = sent[0][2] if sent else []
    c.check("#539 delivery carries the secrets.env file payload",
            files and files[0][0].endswith("secrets.env"), files)
    from mop.common import bus as bus_mod
    content = bus_mod.file_data(files[0][1]) if files else b""
    c.expect("#539 the payload is the one-key secrets.env",
             content, b"MOP_PROXY_KEY=new-key-539\n")
    c.expect("#539 full OK answers settle", out, {"node1": "OK", "node2": "OK"})

    # FAILED ответ -- обычный итог шины, НЕ перехват: опера держит открытой.
    def failing(verb, nodes, timeout=None, channel=None, project=None,
                files=None, **fields):
        return {"node1": {"written": ["secrets.env"]},
                "node2": {"error": "refused"}}

    with patched(bus, request_many=failing):
        out = eff.deliver("new-key-539", ["node1", "node2"])
    c.check("#539 a raw agent reply rides the bus reducer, not a fake OK",
            out.get("node2", "").startswith("FAILED"), out)
    no_network_log(c, http, "delivery stays off the management channel")


# ── дефект 3 ревью: --nodes сверяется с составом и алфавитом ─────────────
# HYPOTHESIS: опечатка замораживала узла, который никогда не ответит
# (навечно pending), а имя с точкой или * и > уезжало прямо в субъект
# шины. Каждое имя -- известный узел И чистый токен субъекта, отказ до
# begin.
def check_nodes_validation_539(c):
    from mop.server import proxyapi
    from mop.common import puppets as puppets_mod

    class Row:
        def __init__(self, name):
            self.name = name

    fake_pool = lambda: [Row(n) for n in ("node1", "node2")]  # noqa: E731

    def refusal_for(names):
        try:
            return proxyapi.node_refusals(names)
        except AttributeError as e:
            return repr(e)  # функции нет -- RED до исправления

    with patched(puppets_mod, pool=fake_pool):
        got = proxyapi.all_nodes_guarded()
        c.check("#539 a reachable pool passes the preflight",
                got == ["node1", "node2"], got)

        # Неизвестное имя -- отказ до begin, не вечный pending. Заглавные
        # отсюда же: состава с таким именем нет.
        for unknown in (["node1", "nope"], ["Node1"]):
            why = refusal_for(unknown)
            c.check(f"#539 an unknown node name {unknown[-1]!r} is "
                    "refused before begin",
                    isinstance(why, str) and unknown[-1] in why, why)

        # Грязные токены субъекта шины -- отказ ДО состава: точка режет
        # токен, * и > -- маски, пробел/пустое -- не имя.
        for bad in ("node1.*", "node.1", "node1>", "*node1", "node 1", ""):
            why = refusal_for([bad] if bad else ["", "node1"])
            c.check(f"#539 a dirty subject token {bad!r} is refused",
                    isinstance(why, str) and bad in why, why)

        # Известные и чистые -- проходят.
        why = refusal_for(["node1", "node2"])
        c.check("#539 known clean names pass", why is None, why)


# ── дефект 4 ревью: --all-registered -- один защищённый вызов состава ────
# HYPOTHESIS: nodes_refusal() звал all_nodes(), затем all_nodes() шёл
# вторым без защиты: второй отказ -- traceback, два вызова могут дать
# разное.
def check_all_registered_once_539(c):
    from mop.server import proxyapi
    from mop.common import puppets as puppets_mod
    calls = []

    class Row:
        def __init__(self, name):
            self.name = name

    def counting():
        calls.append(1)
        return [Row("node1")]

    with patched(puppets_mod, pool=counting):
        try:
            got = proxyapi.all_nodes_guarded()
        except AttributeError as e:
            got = repr(e)  # функции нет -- RED до исправления
    c.check("#539 --all-registered lists the port exactly once".replace(
        "port", "pool"), len(calls) == 1 and got == ["node1"], (len(calls), got))

    calls.clear()

    def boom():
        calls.append(1)
        raise RuntimeError("cluster unreachable")

    # Падающий состав -- именованный строка-блокер, не traceback, и
    # ровно один вызов (второго больше не существует).
    with patched(puppets_mod, pool=boom):
        got = proxyapi.all_nodes_guarded()
    c.check("#539 a failing listing surfaces as a blocker, not tracebacks",
            isinstance(got, str) and "RuntimeError" in got
            and len(calls) == 1, (got, len(calls)))


# ── дефект 5 ревью: deliver рендерит блоб единственным рендером ──────────
# HYPOTHESIS: у двух писателей одного secrets.env должен быть один
# рендер: строка вручную разъедется с proxykey.blob молча.
def check_deliver_blob_539(c):
    from mop.server import proxyapi
    from mop.server import proxykey
    box, http = stand(c)
    eff = effects_of(http, box["mgmt"])
    sent = []

    def fake_request_many(verb, nodes, timeout=None, channel=None,
                          project=None, files=None, **fields):
        sent.append((verb, list(nodes), files))
        return {n: {"written": ["secrets.env"]} for n in nodes}

    from mop.common import bus
    # Индикатор единственного рендера: подменяем proxykey.blob ЗАМЕТНО
    # (другой суффикс). Ручная строка в deliver этого не заметит --
    # проверка обязана быть красной до перехода deliver на blob().
    def loud_blob(env):
        return proxykey.blob.__wrapped__(env) + "#rendered\n" \
            if hasattr(proxykey.blob, "__wrapped__") else \
            f"MOP_PROXY_KEY={env.get('MOP_PROXY_KEY')}\n#rendered\n"

    with patched(bus, request_many=fake_request_many), \
            patched(proxykey, blob=loud_blob):
        eff.deliver("new-key-539", ["node1"])
    files = sent[0][2] if sent else []
    content = bus.file_data(files[0][1]) if files else b""
    c.check("#539 delivery renders the blob with the one renderer",
            content.endswith(b"#rendered\n"), content)


# ── дефект 6 ревью: status --json invalid тоже rc 1 ──────────────────────
# HYPOTHESIS: текстовый путь invalid -- rc 1, а JSON -- rc 0: по
# docstring invalid -- rc 1 в обоих режимах.
def check_status_json_invalid_539(c):
    from mop.cli.server.proxykey import status
    box, http = stand(c)
    # Валидный JSON с недопустимой схемой: библиотека отдаёт
    # {"stage": "invalid", "reason": ...} -- сценарий ревью, где
    # текстовый путь давал rc 1, а --json молча rc 0.
    with open(box["op"], "w", encoding="utf-8") as f:
        json.dump({"bogus": True}, f)
    rc, out_text = None, ""
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]), \
            patched_env(INVENTORY=box["inv"]), \
            patched(sys, stdout=io.StringIO(), stderr=io.StringIO()):
        rc = status.main(["--json"])
        out_text = sys.stdout.getvalue()
    c.check("#539 status --json on an invalid state is rc 1",
            rc == 1 and "invalid" in out_text.lower(), (rc, out_text))


# ── дефект 7 ревью: неабсолютный MOP_HOME -- отказ до эффектов ───────────
# HYPOTHESIS: config.get("MOP_HOME") or "" при пустой настройке давал
# путь от CWD; неабсолютный MOP_HOME -- отказ до эффектов.
def check_mophome_absolute_539(c):
    from mop.server import proxyapi
    box, http = stand(c)
    for bad in ("", "relative/home", "./rel", ".."):
        why = proxyapi.resolve_refusal(cluster_home=bad)
        c.check(f"#539 a non-absolute MOP_HOME {bad!r} is refused",
                isinstance(why, str) and "MOP_HOME" in why, why)
    why = proxyapi.resolve_refusal(cluster_home=os.path.join(box["dir"],
                                                              "abs-home"))
    c.check("#539 an absolute MOP_HOME passes the preflight",
            why is None, why)
    no_network_log(c, http, "the MOP_HOME preflight")


# ── ревью c7272bb: успешный start тих и rc 0; блокер без _CodeRefused ────
# HYPOTHESIS: успешный start (операция записана, стадия ready) возвращал
# rc 1 «pending: ready» -- скрипт не отличит удачный start от отказа.
# Контракт: start записан -- rc 0 и тишина; отказ -- rc 1 с причиной.
# resume остаётся прежним: completed -- 0, pending/отказ -- 1.
def check_start_quiet_539(c):
    from mop.cli.server.proxykey import start, _common
    from mop.common import paths
    box, http = stand(c)
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]), \
            patched_env(INVENTORY=box["inv"], MOP_PROJECT=None,
                        MOP_BUS_CONFIG=None, HOME=box["dir"],
                        MOP_HOME=os.path.join(box["dir"], "abs-home")), \
            patched(_common, effects=lambda: effects_of(http, box["mgmt"])), \
            patched(sys, stdout=io.StringIO(), stderr=io.StringIO()):
        for name in ("llm-proxy-mgmt.pass", "llm-proxy-client.pass"):
            p = paths.local(paths.SECRETS, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            key = "mgmt-key-539" if "mgmt" in name else "old-key-539"
            with open(p, "w", encoding="utf-8") as f:
                f.write(key + "\n")
            os.chmod(p, 0o600)
        cp = paths.under(os.path.join(box["dir"], "abs-home"),
                         paths.SECRETS, "llm-proxy-client.pass")
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f:
            f.write("old-key-539\n")
        os.chmod(cp, 0o600)
        rc = start.main(["--no-recipients", "--exclusive"])
        out_text = sys.stdout.getvalue()
        err_text = sys.stderr.getvalue()
    c.check("#539 a successful start is quiet with rc 0",
            rc == 0 and not out_text.strip() and not err_text.strip(),
            (rc, out_text, err_text))


# HYPOTHESIS: имя класса исключения попадает оператору через
# type(e).__name__ («cannot read the key list (…)») -- внутреннее имя
# класса не читается человеком. Класс зовётся ManagementRefused.
def check_blocker_class_name_539(c):
    from mop.server import proxyapi
    cls = getattr(proxyapi, "ManagementRefused", None)
    c.check("#539 the typed GET refusal class is operator-readable",
            cls is not None and cls.__name__ == "ManagementRefused", cls)
    # Блокер несёт имя класса: оно обязано читаться человеком.
    if cls is not None:
        e = cls("GET api-keys answered HTTP 401")
        e.http_status = 401
        e.reason = "unauthorized"
        c.check("#539 the blocker names ManagementRefused, not an underscore class",
                "ManagementRefused" in f"cannot read the key list "
                f"({type(e).__name__})",
                type(e).__name__)


def check_cli_539(c):
    from mop.cli.server.proxykey import start, status
    box, http = stand(c)
    eff = effects_of(http, box["mgmt"])

    # status: нет операции -- тихий ноль.
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]), \
            patched_env(INVENTORY=box["inv"], MOP_PROJECT=None,
                        MOP_BUS_CONFIG=None), \
            patched(sys, stdout=io.StringIO(), stderr=io.StringIO()):
        rc = status.main([])
        out_text, err_text = sys.stdout.getvalue(), sys.stderr.getvalue()
    c.check("#539 status with no operation is a quiet zero",
            rc == 0 and not out_text.strip(), (rc, out_text))

    # start без --exclusive -- usage-отказ: писатель подтверждается явно.
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]), \
            patched_env(INVENTORY=box["inv"], MOP_PROJECT=None,
                        MOP_BUS_CONFIG=None), \
            patched(sys, stdout=io.StringIO(), stderr=io.StringIO()):
        rc = start.main(["--nodes", "node1"])
        out_text, err_text = sys.stdout.getvalue(), sys.stderr.getvalue()
    c.check("#539 start without --exclusive refuses with rc 1",
            rc == 1 and "exclusive" in (err_text + out_text).lower(),
            (rc, err_text))

    # Не-контроллер: rc 1, ДО транспорта.
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]), \
            patched_env(INVENTORY=os.path.join(box["dir"], "nope.yaml")), \
            patched(sys, stdout=io.StringIO(), stderr=io.StringIO()):
        rc = start.main(["--nodes", "node1", "--exclusive"])
    c.check("#539 a non-controller start is refused with rc 1",
            rc == 1, rc)
    no_network_log(c, http, "the CLI refusal")


def main():
    c = Checks()
    check_controller_guard_539(c)
    check_adapter_pinned_539(c)
    check_adapter_outcomes_539(c)
    check_default_ctor_539(c)
    check_probe_outcomes_539(c)
    check_put_reasons_539(c)
    check_show_wording_539(c)
    check_flag_conflict_539(c)
    check_get_lockout_539(c)
    check_start_quiet_539(c)
    check_blocker_class_name_539(c)
    check_nodes_validation_539(c)
    check_all_registered_once_539(c)
    check_deliver_blob_539(c)
    check_status_json_invalid_539(c)
    check_mophome_absolute_539(c)
    check_paths_539(c)
    check_recipients_539(c)
    check_delivery_539(c)
    check_cli_539(c)
    return c.report("proxyapi")


if __name__ == "__main__":
    sys.exit(main())
