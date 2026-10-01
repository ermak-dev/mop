"""pool dashboard on the web: mop server web [--port N] [--bind ADDR]

The same picture as `mop list`, `mop node` and `mop stat` on one page,
refreshed in place (docs/WEB.md):

  /            the page: the built application, web/dist/index.html
  /assets/N    the application's files from web/dist/assets (hashed names)
  /logo.png    the logo (docs/logo.png), also the favicon
  /api/pool    the current snapshot as JSON
  /events      the same snapshot pushed as server-sent events
  /healthz     200 once the first snapshot is in

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
from mop.server import web

# Собранное приложение: index и ассеты из web/dist, закоммиченного вместе с
# исходниками (docs/WEB.md); собирает `mop dev web build`, сверяет CI.
DIST = os.path.join(config.PROJECT, "web", "dist")
PAGE = os.path.join(DIST, "index.html")
LOGO = os.path.join(config.PROJECT, "docs", "logo.png")   # фавикон и шапка
PING_EVERY = 15      # с: пустой кадр SSE, чтобы прокси и браузер не рвали тишину

COLLECTOR = web.Collector()


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
        if path == "/api/pool":
            return self._json(200, COLLECTOR.current())
        if path == "/healthz":
            if COLLECTOR.at is None:
                return self._send(503, "no snapshot yet\n", "text/plain")
            return self._send(200, "ok\n", "text/plain")
        if path == "/events":
            return self.stream()
        self._send(404, "no such path\n", "text/plain")

    # ── реестр кредитов (#285) ──
    # Прямо в реестр на сервере, а не глаголом кластера: машинный
    # пользователь service не зовёт сервис кластера от чужого имени --
    # субъект с логином ему закрыт, а под service журнал записал бы действие
    # безымянным (#104, #309, docs/BUS.md); реестр -- файлы того же
    # пользователя пула на этой же машине.

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
