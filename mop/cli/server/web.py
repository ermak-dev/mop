"""pool dashboard on the web: mop server web [--port N] [--bind ADDR]

The same picture as `mop list`, `mop node` and `mop stat` on one page,
refreshed in place (docs/WEB.md):

  /            the page
  /logo.png    the logo (docs/logo.png), also the favicon
  /api/pool    the current snapshot as JSON
  /events      the same snapshot pushed as server-sent events
  /healthz     200 once the first snapshot is in
  /api/creds/login/start  post {name, mode?}: start a claude login, answers {url}
  /api/creds/login/code   post {name, code}: finish it, answers {ok, owner} or {error}

No login on the page (the LAN is trusted, operator's decision 2026-09-26;
authorization comes later). The pool itself stays read-only here: a restart
from a button would kill the work in a puppet's clone, and puppet actions
stay with `mop`. The credential registry (docs/CRED.md) is the one thing the
page writes: re-authorizing a claude credential from its row; adding and
removing credentials is `mop cred`. Secrets never come back: the
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
from mop.server import credreg, web

PAGE = os.path.join(config.PROJECT, "web", "index.html")
LOGO = os.path.join(config.PROJECT, "docs", "logo.png")   # фавикон и шапка
PING_EVERY = 15      # с: пустой кадр SSE, чтобы прокси и браузер не рвали тишину

COLLECTOR = web.Collector()


class Handler(BaseHTTPRequestHandler):
    server_version = "mop-web"

    def log_message(self, fmt, *args):
        """Молчим про каждый запрос: под systemd журнал забился бы SSE-пингами.
        Ошибки печатает log_error, и он остаётся."""

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            # Читается на каждый запрос: правка страницы видна без рестарта,
            # а стоит это одного файла в page cache.
            with open(PAGE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/logo.png":
            with open(LOGO, "rb") as f:
                return self._send(200, f.read(), "image/png")
        if path == "/api/pool":
            return self._send(200, json.dumps(COLLECTOR.current(), ensure_ascii=False))
        if path == "/healthz":
            if COLLECTOR.at is None:
                return self._send(503, "no snapshot yet\n", "text/plain")
            return self._send(200, "ok\n", "text/plain")
        if path == "/events":
            return self.stream()
        self._send(404, "no such path\n", "text/plain")

    # ── реестр кредитов (#285) ──
    # Прямо в реестр на сервере, а не глаголом кластера: машинный
    # пользователь service не пишет в rpc сервиса (#104, docs/BUS.md), а
    # реестр -- файлы того же пользователя пула на этой же машине.
    ROUTES = {"/api/creds/login/start": (web.parse_login_start, "login_start"),
              "/api/creds/login/code": (web.parse_login_code, "login_code")}

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        route = self.ROUTES.get(path)
        if route is None:
            return self._send(404, json.dumps({"error": "no such path"}))
        parse, action = route
        try:
            size = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, json.dumps({"error": "body: JSON is expected"}))
        fields, err = parse(body)
        if err:
            return self._send(400, json.dumps({"error": err}, ensure_ascii=False))
        try:
            got = self._cred(action, fields)
        except Exception as e:
            # Причина -- странице, без трассы; ключ и код в тексте отказов не бывают.
            return self._send(500, json.dumps({"error": str(e) or type(e).__name__}, ensure_ascii=False))
        code = 400 if got.get("error") else 200
        if code == 200:
            COLLECTOR.event({"event": f"cred {action}", "name": fields["name"], "node": "-",
                             "project": "-", "text": got.get("owner") or ""})
            COLLECTOR.refresh_creds()
        return self._send(code, json.dumps(got, ensure_ascii=False))

    @staticmethod
    def _cred(action, f):
        if action == "login_start":
            return {"ok": True, "url": credreg.login_start(f["name"], f["mode"])}
        got = credreg.login_code(f["name"], f["code"])
        return got if got.get("error") else {**got, "name": f["name"]}

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
        bus.subscribe(bus.events(busnames.ANY), lambda msg: COLLECTOR.event(web.journal_entry(msg)))
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
