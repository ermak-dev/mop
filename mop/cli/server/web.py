"""pool dashboard on the web: mop server web [--port N] [--bind ADDR]

`mop list`, `mop node` and `mop stat` on one page, refreshed in place
(docs/WEB.md):

  /            the page (web/dist), /assets/N its files, /logo.png
  /api/pool    snapshot as JSON; /events — the same as SSE
  /healthz     200 once the first snapshot is in
  /api/session the session; /api/login and /api/logout by JSON post
               (file/LDAP, bus identity)
  /api/journal the full journal, admin only; the public feeds none

No login for the page (the LAN is trusted); the pool stays read-only —
a restart button would kill work in a clone. Port and bind:
MOP_WEB_PORT (9000), MOP_WEB_BIND (127.0.0.1); beyond — the TLS proxy.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mop.cli import lib
from mop.common import bus, busnames, config
from mop.server import web, websession

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
        -> (код, тело, content-type). Редиректы оставляем вызывающему."""
        import http.client

        key = self.key() if key is None else key
        if not key:
            return 503, b"proxy management key unavailable", "text/plain"
        forwarded = {k: v for k, v in (headers or {}).items()
                     if k.lower() not in ("authorization", "cookie", "host")}
        forwarded["Authorization"] = f"Bearer {key}"
        conn = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port(), timeout=75)
            conn.request(method, path, body=body or None, headers=forwarded)
            r = conn.getresponse()
            if r.status in (301, 302, 303, 307, 308):
                return r.status, (r.getheader("Location") or "").encode(), "text/plain"
            got = r.read(web.PANEL_MAX + 1)
            if len(got) > web.PANEL_MAX:
                return 502, b"proxy management answer too large", "text/plain"
            return r.status, got, r.getheader("Content-Type", "application/json")
        except (http.client.HTTPException, OSError, ValueError):
            return 502, b"proxy management upstream unreachable", "text/plain"
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
            code, got, _ = self.call(method, target, body, key=sent_key)
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

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
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
        self._send(404, "no such path\n", "text/plain")

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/v8/management/"):
            return self.llm_api(self.path)
        self._send(404, "no such path\n", "text/plain")

    def do_PATCH(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/v8/management/"):
            return self.llm_api(self.path)
        self._send(404, "no such path\n", "text/plain")

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/v8/management/"):
            return self.llm_api(self.path)
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
        """Шим сток-панели (#417): sentinel в localStorage, панель -- в
        iframe отдельного raw-маршута. Не-admin не получает ни sentinel,
        ни страницы."""
        who = self._llm_admin()
        if who == "401":
            return self._json(401, {"error": "login required"})
        if who == "403":
            return self._json(403, {"error": "admin only"})
        page = ("<!doctype html><html><head><meta charset=\"utf-8\">"
                "<title>LLM Proxy</title>"
                "<style>body{margin:0}iframe{border:0;width:100vw;height:100vh}</style>"
                "</head><body><script>"
                "localStorage.removeItem('cli-proxy-auth');"
                "localStorage.removeItem('apiBase');"
                "localStorage.removeItem('apiUrl');"
                "localStorage.setItem('managementKey', " + json.dumps(web.SENTINEL) + ");"
                "localStorage.setItem('isLoggedIn', 'true');"
                "</script>"
                "<iframe src=\"/llm/raw/management.html\"></iframe>"
                "</body></html>").encode()
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
            code, got, ctype = gw.call(method, p, b, forwarded, key=k)
            return code, (got, ctype)
        try:
            code, answer = gw.breaker(self.command, path, body, key, upstream)
        except Exception:
            return self._send(502, b"proxy management upstream unreachable", "text/plain")
        if isinstance(answer, tuple):
            got, ctype = answer
        else:
            got, ctype = answer, "text/plain"
        if code in (301, 302, 303, 307, 308):
            return self._send(502, b"proxy management redirect refused", "text/plain")
        if self.command == "GET" and code == 200 \
                and (route.rstrip("/").endswith("/config") or route.endswith("/config.yaml")):
            got = web.redact_management_read(route, got)
            if got is None:
                return self._send(502, b"proxy management config unreadable", "text/plain")
        # Только тип тела уходит наружу, заголовки upstream не копируем.
        return self._send(code, got, ctype)

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

    def stream(self):
        """SSE: снимок при подключении и на каждое изменение, пинг в тишине.
        Соединение живёт в своём потоке; обрыв клиента — нормальный выход."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        version = 0
        try:
            while True:
                got, snap = COLLECTOR.wait(version, PING_EVERY)
                if got != version:
                    version = got
                    payload = json.dumps(snap or COLLECTOR.current(), ensure_ascii=False)
                    self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode())
                else:
                    self.wfile.write(b": ping\n\n")
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


def main(argv):
    port, bind = parse(argv)
    COLLECTOR.start()
    threading.Thread(target=listen_events, daemon=True, name="mop-web-events").start()
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    print(f"mop-web: http://{bind}:{port}/ — states every {web.STATES_EVERY}s, "
          f"sizes every {web.SIZES_EVERY}s, usage every {web.USAGE_EVERY}s", flush=True)
    srv.serve_forever()



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
