"""pool dashboard on the web: mop server web [--port N] [--bind ADDR]

`mop list`, `mop node` and `mop stat` on one page, refreshed in place:

  /            the page, /assets/N its files, /logo.png
  /api/pool    snapshot as JSON; /events — the same as SSE,
               plus an `operation` frame when a click reaches its verdict
  /healthz     200 once the first snapshot is in
  /api/session the session; /api/login and /api/logout by JSON post
               (file/LDAP, bus identity)
  /api/journal the full journal, admin only; the public feeds none

The overview is available without login; logging in opens the admin side —
the journal and the proxy panel. Admin clicks ride the shared workflow
as a thread-per-operation executor: the verdict lands as an SSE `operation`
frame and an ops.record audit line in the journal. Port and bind:
MOP_WEB_PORT (9000), MOP_WEB_BIND (127.0.0.1); beyond — the TLS proxy.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mop.cli import lib
from mop.common import bus, busnames, config, ops, puppets, state
from mop.common.domain import Alloc
from mop.server import adminapi, opworker, web, websession

# Собранное приложение: index и ассеты из web/dist, закоммиченного вместе с
# исходниками (docs/WEB.md); собирает `mop dev web build`, сверяет CI.
DIST = os.path.join(config.PROJECT, "web", "dist")
PAGE = os.path.join(DIST, "index.html")
LOGO = os.path.join(config.PROJECT, "docs", "logo.png")   # фавикон и шапка
PING_EVERY = 15      # с: пустой кадр SSE, чтобы прокси и браузер не рвали тишину

BODY_MAX = 16 * 1024       # байт тела входа: {login, password} -- это байты,
                           # не гигабайты (#415)
COLLECTOR = web.Collector()
SESSIONS = websession.SessionStore()
RATE = websession.RateLimiter()

# Операции дашборда (#486): реестр -- та же точка стыковки, что у воркера
# (opworker.register, #470/#471), исполнитель и адаптер -- рядом; Origin для
# POST -- публичный https-origin установки (#415). Личность каждого запроса
# адаптер берёт сам из handler.who (куки -> SESSIONS -> провайдер).
OPS = adminapi.Operations()
opworker.register(OPS)
WORKER = opworker.Worker()

# Кэш ростера для резолвера проекта (#498): [момент, {имя -> проект}].
# #498 вёз функцию без этих определений -- первый же вызов резолвера
# падал NameError; #499 чинит точку, на которой стоит исполнитель.
_ROSTER_LOCK = threading.Lock()
_ROSTER_CACHE = [0.0, {}]

def _resolve_project(name):
    """Проект цели по имени (#498): спек кластера спрашивать нельзя --
    глагол принадлежит проекту цели и отвечает belongs-отказом субъекту
    mop (круг). Пул же делит ростер по субъекту, а оператор видит все:
    спросим ростер каждого проекта реестра (список -- операторским
    projects) и найдём имя. Кэш строк -- 60 с: имя живёт дольше круга
    операций, а реестр меняется редко. Нет нигде -- None: транспорт уйдёт
    субъектом процесса, отказ доедет словами сервиса."""
    now = time.time()
    with _ROSTER_LOCK:
        if now - _ROSTER_CACHE[0] > 60:
            rows = {}
            try:
                got = bus.call_cluster("projects", project=bus.ADMIN) or {}
                for p in got.get("names") or []:
                    items = bus.call_cluster("roster", project=p) or {}
                    for i in items.get("items") or []:
                        meta = puppets.JobMeta.from_job(i.get("job") or {})
                        if meta.origin:
                            rows[i["job"]["ID"]] = puppets.project_of(meta.origin)
            except Exception:
                pass  # шина легла: кэш прежний, транспорт скажет словами
            _ROSTER_CACHE[0], _ROSTER_CACHE[1] = now, rows
        return _ROSTER_CACHE[1].get(name)

# Резолвер проекта цели (#498): один на службу, из строк сборчика;
# подмены WORKER в проверках резолвер не трогают.
RESOLVER = _resolve_project

# Исполнитель #499: эффект операции -- воркер run тем же транспортом,
# что CLI (#496/#498); поток на операцию даёт адаптер (adminapi), сюда
# приходит уже свободная цель. Личность -- (login, role) HTTP-сессии.
# Действие с целью проходит обязательный ops.safety (#566): воркер зовёт
# предикат ДО эффекта, но факты и занятость собирает ЭТА обвязка, из
# живых источников, -- сам по себе предикат их не спрашивает, а без
# фактов инвариант #460 молчит. Add -- без цели (его ворота -- реестр
# #470), send -- за своими воротами _wrap_send: оба идут без предиката.
UNSAFE_VERBS = frozenset(("restart", "recycle", "delete", "wipe",
                          "update", "model"))


def _safety_of(target, owner):
    """Предикат #460 для run: свежие факты и занятость под target gate.

    -> (predикат, транспорт, None) | (None, None, отказ). Транспорт --
    подтверждённый проектом цели: эффект воркера едет им же, а не
    глобальным резолвером, чья догадка могла быть stale. Отказ --
    готовая причина для вердикта run'а: контекст цели не собрался
    (спека, аллокация, узел) -- «не знаю» не значит «можно», действие
    не исполняется вовсе.
    Субъект -- ПРОЕКТ ЦЕЛИ (#498): префетч едет тем же адресным
    транспортом, что и эффект воркера, а не субъектом процесса --
    проектному веб-процессу belongs-отказ ответил бы до того, как
    известен origin. Кандидат проекта -- из ростера (резолвер); спека
    подтверждает его origin'ом, и подтверждённый проект ПЕРЕВЯЗЫВАЕТ
    транспорт своей связкой резолвера -- иначе alloc и эффект
    продолжали бы ехать первой догадкой ростера, и stale-кандидат
    оставлял мутацию на субъекте процесса. Узел -- последней аллокации
    в любом статусе (#377): легитимный wipe -- по остановленному.
    Свидетельство сессии -- ответ state, и он не заменяется целиком:
    fallback на глагол clone берёт у него ТОЛЬКО факты клона, и только
    для подтверждённо остановленной цели (present False: state фактов
    клона без tmux не даёт, #40) -- живой или неразобранный ответ
    fallback'ом не подменяется, неизвестность сессии не становится
    idle. Занятость -- общий вердикт сессии (#501): слово статуса
    разобралось -- всё, что не free и не hung, живая работа, и
    незнакомое слово тоже; не разобралось или её нет -- жив ли tmux
    (present) решает, и живый ответ без строки сессии -- занят. Флаг
    disruptive предикат передаёт ops.safety как пришёл от run:
    model/update сессию не убивают (#468) и не получают ложный отказ
    над живой чистой целью."""
    transport = opworker.default_transport(RESOLVER)
    try:
        project = RESOLVER(target)
        spec = transport("spec", name=target) or {}
        origin = (spec.get("meta") or {}).get("origin")
        if not origin:
            return (None, None, f"{target}: carries no origin — whose target it "
                                f"is cannot be told, and acting on it is not "
                                f"for the web")
        confirmed = ops.target_project(origin)
        if confirmed != project:
            # Ростер мог соврать (поздний meta-фикс), а спека авторитетна:
            # перевязываем транспорт подтверждённым проектом, а не только
            # локальную переменную -- дальше едут alloc, узел и эффект.
            transport = opworker.default_transport(lambda _name: confirmed)
            project = confirmed
        alloc = Alloc.from_dict(transport("alloc", name=target).get("alloc"))
        if not alloc or not alloc.node:
            raise LookupError(f"{target}: no allocation — node unknown")
        node = alloc.node
        got = bus.request(node, "state", name=target, project=project)
        # Ошибка state -- уже ответ «не знаю», safety откажет ею; глагол
        # clone -- не обход ошибки, а дорога к фактам ПОДТВЕРЖДЁННО
        # остановленной цели. Берём у него только клон: present/session
        # ответа state -- свидетельство сессии, и оно не стирается.
        if (isinstance(got, dict) and got.get("present") is False
                and not got.get("clone") and not got.get("error")):
            extra = bus.request(node, "clone", name=target, project=project)
            got = {**got, "clone": (extra or {}).get("clone")}
    except Exception as e:  # noqa: BLE001 -- отказ с причиной, не тишина
        return None, None, f"{target}: cannot inspect it before acting — {e}"
    st = state._session_state((got or {}).get("session"))
    if st is not None:
        busy = state._state_from_session(st, None).kind not in ("free", "hung")
    else:
        # Строки сессии нет или не разобралась: жив ли tmux -- единственное
        # свидетельство; жив -- занята (не idle), мёртв -- сессии нет.
        busy = (got or {}).get("present") is not False

    def predicate(name, _project, disruptive=True, **_):
        return ops.safety(name, project, owner=owner, facts=got,
                          busy=busy, disruptive=disruptive)

    return predicate, transport, None


def _execute(action, target, params, who):
    login = (who or (None,))[0]
    name = target or ""
    safety, transport = None, opworker.default_transport(RESOLVER)
    if action in UNSAFE_VERBS and name:
        safety, transport, refusal = _safety_of(name, login)
        if safety is None:
            return lambda: {"status": "refused", "error": refusal}
    return lambda: WORKER.run(action, name, params, login,
                              transport=transport, safety=safety)

# Очередь кадров operation для /events (#499): терминал приходит из
# потока операции, SSE-читатель забирает своим темпом.
_OP_LOCK = threading.Lock()
_OP_FRAMES = []


class _LiveApi(adminapi.Api):
    """Адаптер с Origin'ом установки «на запрос»: Api ждёт строку в
    конструкторе, а настройка читается живой конфигурацией (в проверках --
    подменённой), не моментом импорта модуля."""

    @property
    def origin(self):
        return websession.public_origin(config.get)

    @origin.setter
    def origin(self, _):
        pass


ADMINAPI = _LiveApi(OPS, origin=None, executor=_execute)

# Терминал операции (#499): SSE-кадр operation в /events и аудит-запись
# ops.record в журнал шины -- подписка listen_events кладёт её в
# COLLECTOR (/api/journal), событие уходит и остальным мастерам.
def _on_operation(frame):
    payload = json.dumps(frame, ensure_ascii=False)
    with _OP_LOCK:
        _OP_FRAMES.append(payload)
        del _OP_FRAMES[:-100]
    COLLECTOR.kick()
    try:
        bus.emit(ops.record(frame.get("action"), frame.get("target"),
                            None, frame.get("login")))
    except Exception:
        pass  # журнал вторичен: тишина не роняет вердикт


ADMINAPI.on_terminal(_on_operation)


class Gateway:
    """Управление LLM-прокси без ключа в браузере (#417).

    Единственный путь браузера к management API CPA: авторизация -- наша
    admin-сессия, ключ читается из закрытой копии сервера (#418 кладёт её
    в ~/.config/mop/secrets/llm-proxy-mgmt.pass) на каждый запрос и живёт
    только в исходящем loopback-запросе. Клиентский Bearer снимается,
    ответы редачатся (хэш -> плейсхолдер), write'ы идут через
    guard_management_write, отказы ключа гасит KeyBreaker. Upstream --
    всегда 127.0.0.1:MOP_LLM_PROXY_PORT; никакой клиентский ввод не меняет
    origin."""

    def __init__(self, upstream=None, key=None, breaker=None):
        self._upstream = upstream      # проверка подменяет
        self._key = key
        self.breaker = breaker or web.KeyBreaker()

    def key(self):
        if self._key is not None:
            return self._key() if callable(self._key) else self._key
        try:
            with open(web.mgmt_key_path(), encoding="utf-8") as f:
                got = f.read().rstrip("\r\n")
        except OSError:
            return None
        return got or None

    def call(self, method, path, body, headers=None, key=None):
        """Запрос только на петлю: окружение HTTP_PROXY не участвует.
        -> (код, тело, content-type, заголовки upstream). Редиректы
        оставляем вызывающему."""
        import http.client

        key = self.key() if key is None else key
        if not key:
            return 503, b"proxy management key unavailable", "text/plain", {}
        forwarded = {k: v for k, v in (headers or {}).items()
                     if k.lower() not in ("authorization", "cookie", "host")}
        forwarded["Authorization"] = f"Bearer {key}"
        conn = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port(), timeout=75)
            conn.request(method, path, body=body or None, headers=forwarded)
            r = conn.getresponse()
            if r.status in (301, 302, 303, 307, 308):
                return r.status, (r.getheader("Location") or "").encode(), \
                    "text/plain", {}
            got = r.read(web.PANEL_MAX + 1)
            if len(got) > web.PANEL_MAX:
                return 502, b"proxy management answer too large", "text/plain", {}
            # Заголовки upstream нужны браузеру (#523): панель открывает
            # разделы плагинов по X-Cpa-Support-Plugin и называет версию
            # сервера по X-Cpa-Version. Отдаются не все -- наружу идёт
            # whitelist web.response_headers, а не сырой набор.
            heads = {k: v for k, v in r.getheaders() if k.lower() != "content-type"}
            return r.status, got, r.getheader("Content-Type", "application/json"), heads
        except (http.client.HTTPException, OSError, ValueError):
            return 502, b"proxy management upstream unreachable", "text/plain", {}
        finally:
            if conn is not None:
                conn.close()

    def port(self):
        return config.num("MOP_LLM_PROXY_PORT")

    def fetch_panel(self):
        """Сток-панель -> (код, тело). Редирект -- только относительный,
        короткий и в пределах нескольких шагов: абсолютный (чужой scheme/
        host) -- fail closed, Bearer с секретом не уходит с петли."""
        path = "/management.html"
        key = self.key()
        if not key:
            return 503, b"proxy management key unavailable"
        def upstream(method, target, body, sent_key):
            code, got, _, _ = self.call(method, target, body, key=sent_key)
            return code, got
        for _ in range(web.REDIRECT_MAX):
            code, body = self.breaker("GET", path, b"", key, upstream)
            if code not in (301, 302, 303, 307, 308):
                return code, body
            target = body.decode("utf-8", "replace").strip()
            if target not in ("/management.html", "management.html"):
                return 502, b"proxy management redirect refused"
            path = "/management.html"
        return 502, b"proxy management too many redirects"


GATEWAY = Gateway()


class Handler(BaseHTTPRequestHandler):
    server_version = "mop-web"
    # Таймаут соединения (#415): недосланное тело не паркует поток вечно,
    # socket.timeout читается как обычный отказ (400), соединение закрывается.
    timeout = 30

    def log_message(self, fmt, *args):
        """Молчим про каждый запрос: под systemd журнал забился бы SSE-пингами.
        Ошибки печатает log_error, и он остаётся."""

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store",
              extra=None):
        """Один выход ответа. extra -- заголовки upstream после whitelist:
        отправляются ПОСЛЕ send_response -- send_header до строки состояния
        кладёт их на провод первыми, и браузер читает ответ как HTTP/0.9
        (#523, живой сервер: все /v8/management/* падали)."""
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, body):
        """JSON-ответ страницы -- одним местом (#319): не-ASCII -- UTF-8, без
        экранирования, как у снимка. Два ответа об ошибке прежде шли без
        ensure_ascii=False; их тексты -- ASCII, и байты у них те же."""
        return self._send(code, json.dumps(body, ensure_ascii=False))

    def _origin_ok(self):
        """Origin строго публичный https origin (#415): без Origin -- отказ,
        чужой -- отказ. CSRF без токена: Origin и требование JSON закрывают
        и форму, и чужую страницу."""
        return websession.origin_ok(self.headers,
                                    websession.public_origin(config.get))

    @property
    def who(self):
        """Личность запроса атрибутом (#486): адаптер операций читает
        handler.who, живой Handler считает её методом _who -- мост, чтобы
        контракт адаптера не узнал про куки."""
        return self._who()

    def _who(self):
        """Личность по cookie: (login, role) | (None, None)."""
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if part.strip().startswith(websession.COOKIE + "="):
                got = SESSIONS.get(part.strip().split("=", 1)[1])
                if got:
                    return got
        return None, None

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/login":
            return self.api_login()
        if path == "/api/logout":
            return self.api_logout()
        if path.startswith("/v8/management/"):
            return self.llm_api(self.path)
        if path == "/api/admin/ops":
            return ADMINAPI.post(self)
        self._send(404, "no such path\n", "text/plain")

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/v8/management/"):
            return self.llm_api(self.path)
        if path == "/api/admin/ops":
            return ADMINAPI.post(self)
        self._send(404, "no such path\n", "text/plain")

    def do_PATCH(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/v8/management/"):
            return self.llm_api(self.path)
        if path == "/api/admin/ops":
            return ADMINAPI.post(self)
        self._send(404, "no such path\n", "text/plain")

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/v8/management/"):
            return self.llm_api(self.path)
        if path == "/api/admin/ops":
            return ADMINAPI.post(self)
        self._send(404, "no such path\n", "text/plain")

    def _llm_admin(self):
        """Гейт управления прокси (#417): гость -- 401, вошедший не-admin --
        403, актуальный admin -- (login, role). Роль перечитывается
        провайдером на каждом запросе, как у /api/journal (#424)."""
        try:
            login, role = self._who()
        except Exception:
            return "401"
        if not login:
            return "401"
        if role != "admin":
            return "403"
        return (login, role)

    def llm_shim(self):
        """Шим сток-панели (#417): sentinel в localStorage, переход на
        raw-панель тем же документом (#526). Фрейм исторически писал ключ
        до кода панели, не трогая storage главной страницы, но шим и панель
        на одном origin -- фрейм давал только двойную прокрутку.
        Не-admin не получает ни sentinel, ни страницы."""
        who = self._llm_admin()
        if who == "401":
            return self._json(401, {"error": "login required"})
        if who == "403":
            return self._json(403, {"error": "admin only"})
        page = ("<!doctype html><html><head><meta charset=\"utf-8\">"
                "<title>LLM Proxy</title></head><body><script>"
                "localStorage.removeItem('cli-proxy-auth');"
                "localStorage.removeItem('apiBase');"
                "localStorage.removeItem('apiUrl');"
                "localStorage.setItem('managementKey', " + json.dumps(web.SENTINEL) + ");"
                "localStorage.setItem('isLoggedIn', 'true');"
                "location.replace('/llm/raw/management.html');"
                "</script></body></html>").encode()
        return self._send(200, page, "text/html; charset=utf-8")

    def llm_raw(self, path):
        """Сток-панель без sentinel: только admin, редиректы upstream не
        проходят (redirect unsupported -- fail closed на 502)."""
        who = self._llm_admin()
        if who == "401":
            return self._json(401, {"error": "login required"})
        if who == "403":
            return self._json(403, {"error": "admin only"})
        upstream_path = path[len("/llm/raw"):] or "/management.html"
        if upstream_path != "/management.html":
            return self._send(404, "no such panel asset\n", "text/plain")
        gw = GATEWAY if not hasattr(self, "_gw") else self._gw
        code, body = gw.fetch_panel()
        return self._send(code, body, "text/html; charset=utf-8")

    def llm_api(self, path):
        """Проксирование management API (#417): admin-гейт, Origin для
        unsafe, guard на write, breaker, редакция чтений."""
        route = path.split("?", 1)[0]
        who = self._llm_admin()
        if who == "401":
            return self._json(401, {"error": "login required"})
        if who == "403":
            return self._json(403, {"error": "admin only"})
        if self.command in ("POST", "PUT", "PATCH", "DELETE") and not self._origin_ok():
            return self._json(403, {"error": "origin refused"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad content length"})
        if length < 0:
            return self._json(400, {"error": "bad content length"})
        if length > web.PANEL_MAX:
            return self._json(413, {"error": "request too large"})
        try:
            body = self.rfile.read(length) if length else b""
        except OSError:
            return self._json(400, {"error": "incomplete request"})
        if len(body) != length:
            return self._json(400, {"error": "incomplete request"})
        gw = GATEWAY if not hasattr(self, "_gw") else self._gw
        key = gw.key()
        if key is None:
            return self._send(503, b"proxy management key unavailable", "text/plain")
        if self.command in ("POST", "PUT", "PATCH", "DELETE"):
            verdict = web.guard_management_write(self.command, route, body, gw.key)
            if verdict["action"] == "refuse":
                return self._json(verdict["status"], {"error": "management write refused"})
            if verdict["action"] == "inject":
                body = verdict["body"]
        forwarded = {name: self.headers[name] for name in ("Content-Type", "Accept")
                     if self.headers.get(name)}
        def upstream(method, p, b, k):
            code, got, ctype, heads = gw.call(method, p, b, forwarded, key=k)
            return code, (got, ctype, heads)
        try:
            code, answer = gw.breaker(self.command, path, body, key, upstream)
        except Exception:
            return self._send(502, b"proxy management upstream unreachable", "text/plain")
        if isinstance(answer, tuple):
            got, ctype = answer[0], answer[1]
            heads = answer[2] if len(answer) > 2 else {}
        else:
            got, ctype, heads = answer, "text/plain", {}
        if code in (301, 302, 303, 307, 308):
            return self._send(502, b"proxy management redirect refused", "text/plain")
        if self.command == "GET" and code == 200 \
                and (route.rstrip("/").endswith("/config") or route.endswith("/config.yaml")):
            got = web.redact_management_read(route, got)
            if got is None:
                return self._send(502, b"proxy management config unreadable", "text/plain")
        # Тип тела и whitelist заголовков (#417/#523): панель читает
        # X-Cpa-Support-Plugin и X-Cpa-Version из ответов; cookie и
        # аутентификационные вызовы upstream браузеру не нужны. Порядок
        # важен: заголовки уходят после строки состояния, внутри _send.
        return self._send(code, got, ctype, extra=web.response_headers(heads))

    def api_login(self):
        """Вход: JSON {login, password} -> сессия в cookie.

        Ответ отказа -- одна фраза без причины: «нет такого логина»
        подсказывало бы перебор. Любая неудача считает в rate limit."""
        addr = websession.client_addr(self.headers, self.client_address)
        if not self._origin_ok() or (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json(403, {"error": "origin refused"})
        if not RATE.allowed(addr):
            return self._json(429, {"error": "too many attempts, try later"})
        # Тело входа -- килобайты, не гигабайты: один заголовок Content-Length
        # без потолка парковал поток в read навсегда (#415), и недосланное
        # тело держало его до разрыва соединения.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > BODY_MAX:
            return self._json(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length))
            login, password = body["login"], body["password"]
            if not isinstance(login, str) or not isinstance(password, str):
                raise ValueError
        except Exception:
            return self._json(400, {"error": "bad request: JSON {login, password}"})
        try:
            who = websession.provider().authenticate(login, password)
        except Exception:
            RATE.note_failure(addr)
            return self._json(401, {"error": "invalid login or password"})
        token = SESSIONS.open(who)
        RATE.note_success(addr)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie",
                         f"{websession.COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict")
        self.send_header("Cache-Control", "no-store")
        data = json.dumps({"login": who.login, "role": who.role}, ensure_ascii=False).encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def api_logout(self):
        """Отзыв сессии: cookie больше не работает, ответ -- гость."""
        if not self._origin_ok() or (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json(403, {"error": "origin refused"})
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if part.strip().startswith(websession.COOKIE + "="):
                SESSIONS.close(part.strip().split("=", 1)[1])
        return self._json(200, {"login": None, "role": None})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            # Читается на каждый запрос: свежая раскатка видна без рестарта,
            # а стоит это одного файла в page cache. Индекс не кэшируется:
            # он называет ассеты по хешам, и старый индекс просил бы файлы,
            # которых после раскатки уже нет.
            with open(PAGE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8", web.cache_control(path))
        asset = web.asset_path(path, DIST)
        if asset is not None:
            try:
                with open(asset, "rb") as f:
                    data = f.read()
            except OSError:
                return self._send(404, "no such asset\n", "text/plain")
            return self._send(200, data, web.content_type(asset), web.cache_control(path))
        if path == "/logo.png":
            with open(LOGO, "rb") as f:
                return self._send(200, f.read(), "image/png")
        if path == "/api/session":
            # Провал провайдера -- гость с причиной, не 500 в журнал (#415):
            # тот же fail closed, что у /api/journal (#424).
            try:
                login, role = self._who()
            except Exception:
                return self._json(200, {"login": None, "role": None,
                                        "error": "provider unavailable"})
            return self._json(200, {"login": login, "role": role})
        if path == "/api/journal":
            return self.api_journal()
        if path.startswith("/api/puppet/"):
            return self.api_puppet(path)
        if path == "/api/admin/models":
            return ADMINAPI.models(self)
        if path == "/api/admin/projects":
            return ADMINAPI.projects(self, _registry_projects())
        if path.startswith("/api/admin/ops/"):
            return self._json(200, ADMINAPI.result(path.rsplit("/", 1)[-1], self.who))
        if path == "/llm/management.html":
            return self.llm_shim()
        if path.startswith("/llm/raw/"):
            return self.llm_raw(path)
        if path.startswith("/v8/management/"):
            return self.llm_api(self.path)
        if path == "/api/pool":
            return self._json(200, COLLECTOR.current())
        if path == "/healthz":
            if COLLECTOR.at is None:
                return self._send(503, "no snapshot yet\n", "text/plain")
            return self._send(200, "ok\n", "text/plain")
        if path == "/events":
            return self.stream()
        self._send(404, "no such path\n", "text/plain")


    def api_journal(self):
        """Полный журнал событий -- только актуальному admin (#424).

        Роль перечитывается на каждом запросе (SessionStore -> провайдер,
        #415): сменившийся admin -> user и умерший провайдер не пускают
        (fail closed -- как гость), а не ходят с ролью на момент входа.
        Записей в публичном снимке нет, ключи кредов сюда не попадают."""
        try:
            login, role = self._who()
        except Exception:
            login, role = None, None
        if not login:
            return self._json(401, {"error": "login required"})
        if role != "admin":
            return self._json(403, {"error": "admin only"})
        with COLLECTOR._cond:
            entries = list(COLLECTOR.admin_journal)
        return self._json(200, {"journal": entries})

    def api_puppet(self, path):
        """Карточка папета и его хвост -- только актуальному admin (#462).

        Тот же fail closed, что у /api/journal: сменившийся admin -> user и
        умерший провайдер не пускают. Цель -- реальный job из строк ростера:
        имя из пути до читателей не доезжает (404 без запроса к шине).
        Хвост -- ограничен и помечает обрезание; ничего не пишется и не
        логируется: log_message молчит, ответы no-store."""
        try:
            login, role = self._who()
        except Exception:
            login, role = None, None
        if not login:
            return self._json(401, {"error": "login required"})
        if role != "admin":
            return self._json(403, {"error": "admin only"})
        # /api/puppet/<имя>[/tail]: имя -- остаток пути целиком;
        # слэши в нём -- не наша грамматика, такой цели в ростере нет.
        rest = path[len("/api/puppet/"):]
        name, _, sub = rest.partition("/")
        rows = getattr(COLLECTOR, "rows", None) or []
        row = web.puppet_card(rows, name)
        if row is None:
            return self._json(404, {"error": "no such puppet"})
        if not sub:
            return self._json(200, {"puppet": row.to_dict()})
        if sub == "tail":
            lines, note = web.puppet_tail(name, puppets.pane_lines, puppets.node_of)
            if lines is None:
                return self._json(502, {"error": note})
            return self._json(200, {"lines": lines, "truncated": note})
        # #507: маршрута stderr больше нет -- кнопка «лог» ушла из GUI;
        # неизвестный подсубъект по-прежнему 404.
        return self._json(404, {"error": "no such puppet"})

    def stream(self):
        """SSE: снимок при подключении и на каждое изменение, пинг в тишине;
        терминал операции -- отдельным кадром operation (#499). Соединение
        живёт в своём потоке; обрыв клиента — нормальный выход."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        version = 0
        # Курсор кадров -- с текущей длины буфера (#519): подписчик получает
        # только терминалы, пришедшие после его подключения, а не хвост,
        # накопившийся до него (иначе каждое F5 перерисовывает старые балуны).
        # Сам буфер остаётся: терминал приходит из потока операции между
        # пробуждениями читателя, и живому соединению без него кадр теряется.
        with _OP_LOCK:
            sent = len(_OP_FRAMES)
        try:
            while True:
                got, snap = COLLECTOR.wait(version, PING_EVERY)
                if got != version:
                    version = got
                    payload = json.dumps(snap or COLLECTOR.current(), ensure_ascii=False)
                    self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode())
                else:
                    self.wfile.write(b": ping\n\n")
                # Кадры operation (#499): терминалы операций, накопившиеся
                # с прошлого круга; payload в них нет (#461).
                with _OP_LOCK:
                    frames = _OP_FRAMES[sent:]
                    sent = len(_OP_FRAMES)
                for frame in frames:
                    self.wfile.write(f"event: operation\ndata: {frame}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def parse(argv):
    port, bind = config.num("MOP_WEB_PORT"), config.get("MOP_WEB_BIND")
    it = iter(argv)
    for a in it:
        if a == "--port":
            try:
                port = int(next(it, ""))
            except ValueError:
                lib.usage(__doc__)
        elif a == "--bind":
            bind = next(it, None) or lib.usage(__doc__)
        else:
            lib.usage(__doc__)
    return port, bind


def listen_events():
    """Журнал проекта с шины: под кредами admin видны все проекты. Шина легла —
    дашборд живёт опросом, а причина видна в снимке."""
    try:
        bus.subscribe(bus.events(busnames.ANY),
                      lambda msg: COLLECTOR.event(web.journal_entry(msg), msg=msg))
    except bus.BusError as e:
        with COLLECTOR._cond:
            COLLECTOR.errors["events"] = str(e)


def _registry_projects():
    """Реестр сервера для GET /api/admin/projects (#472/#486): набор
    origin'ов, как его держит сервер; читать -- не менять."""
    from mop.common import projects as registry
    try:
        return registry.read()
    except Exception:
        return set()


def main(argv):
    port, bind = parse(argv)
    COLLECTOR.start()
    threading.Thread(target=listen_events, daemon=True, name="mop-web-events").start()
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    print(f"mop-web: http://{bind}:{port} — states every {web.STATES_EVERY}s, "
          f"sizes every {web.SIZES_EVERY}s, usage every {web.USAGE_EVERY}s", flush=True)
    srv.serve_forever()



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
