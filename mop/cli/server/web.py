"""pool dashboard on the web: mop server web [--port N] [--bind ADDR]

The same picture as `mop list`, `mop node` and `mop stat` on one page,
refreshed in place (docs/WEB.md):

  /            the page: the built application, web/dist/index.html
  /assets/N    the application's files from web/dist/assets (hashed names)
  /logo.png    the logo (docs/logo.png), also the favicon
  /api/pool    the current snapshot as JSON
  /events      the same snapshot pushed as server-sent events
  /healthz     200 once the first snapshot is in
  /api/session who is logged in; login and logout by post with JSON on
               /api/login, /api/logout -- file/LDAP via the same identity
               as the bus, server-side session cookie, for the admin gate
  /api/journal the full event journal, admin only (current role re-checked
               per request); the public snapshot and /events carry no entries

No login on the page (the LAN is trusted, operator's decision 2026-09-26;
authorization comes later). The pool itself stays read-only here: a restart
from a button would kill the work in a puppet's clone, and puppet actions
snapshot carries names, owners and statuses only, and the journal never sees
a code.
Port and bind address default to MOP_WEB_PORT (9000) and MOP_WEB_BIND
(127.0.0.1): outside the server the page is reached through the TLS proxy.
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

COLLECTOR = web.Collector()
SESSIONS = websession.SessionStore()
RATE = websession.RateLimiter()


class Handler(BaseHTTPRequestHandler):
    server_version = "mop-web"

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
        self._send(404, "no such path\n", "text/plain")

    def api_login(self):
        """Вход: JSON {login, password} -> сессия в cookie.

        Ответ отказа -- одна фраза без причины: «нет такого логина»
        подсказывало бы перебор. Любая неудача считает в rate limit."""
        addr = websession.client_addr(self.headers, self.client_address)
        if not self._origin_ok() or (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json(403, {"error": "origin refused"})
        if not RATE.allowed(addr):
            return self._json(429, {"error": "too many attempts, try later"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
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
            login, role = self._who()
            return self._json(200, {"login": login, "role": role})
        if path == "/api/journal":
            return self.api_journal()
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
