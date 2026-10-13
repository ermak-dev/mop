"""Приватные подключения браузера к узловому PTY, без очереди операций."""
import base64
import binascii
import threading
import time

from mop.common import bus, ops, puppets


class BridgeError(ValueError):
    """Проверенный отказ приватного интерфейса, не сырой stderr."""

    def __init__(self, code, error):
        super().__init__(error)
        self.code, self.error = code, error


def target_context(name):
    """Узел и проект из серверной спеки, не из запроса браузера."""
    spec = puppets._cluster("spec", name=name) or {}
    origin = (spec.get("meta") or {}).get("origin")
    project = ops.target_project(origin)
    if not project or project == bus.ADMIN:
        raise BridgeError(409, "cannot determine the target project")
    return {"node": puppets.node_of(name), "project": project}


def _integer(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _identifier(value):
    return (isinstance(value, str) and 0 < len(value) <= 128
            and value.isascii() and all(ch.isalnum() or ch in "-_" for ch in value))


class TerminalBridge:
    """Один writer цели; каждый handle закреплён за логином и HTTP-сессией.

    authorize перечитывает действующие права, включая после long-poll.
    transport не публикует байты/handle в события. Журнал принимает только
    общую audit-запись взятия/освобождения управления, без клавиатуры.
    """

    def __init__(self, transport=None, resolve=None, *, audit=lambda record: None,
                 clock=time.monotonic, maximum=8, idle=60):
        self.transport = transport or bus.request
        self.resolve = resolve or target_context
        self.audit, self.clock = audit, clock
        self.maximum, self.idle = maximum, idle
        self._lock = threading.RLock()
        self._bindings, self._writers = {}, {}
        self._pending = 0

    @staticmethod
    def _actor(authorize):
        try:
            login, role, browser = authorize()
        except Exception:
            raise BridgeError(401, "authentication provider unavailable") from None
        if not login:
            raise BridgeError(401, "login required")
        if role != "admin":
            raise BridgeError(403, "admin only")
        if not isinstance(browser, str) or not browser:
            raise BridgeError(401, "browser session required")
        return login, browser

    def _rpc(self, node, verb, **fields):
        try:
            got = self.transport(node, verb, **fields)
        except bus.Refused as e:
            raise BridgeError(409, str(e)) from None
        except Exception:
            raise BridgeError(502, "terminal operation could not be confirmed") from None
        if not isinstance(got, dict):
            raise BridgeError(502, "terminal returned an invalid response")
        if got.get("error"):
            code = 502 if got.get("timeout") or got.get("outcome") == "unknown" else 409
            raise BridgeError(code, str(got["error"]))
        return got

    def _audit(self, entry, status):
        record = ops.record("terminal_control", entry["name"], entry["project"],
                            entry["actor"][0], status=status)
        try:
            self.audit(record)
        except Exception:
            raise BridgeError(503, "terminal control audit unavailable") from None

    def _detach(self, tid):
        with self._lock:
            entry = self._bindings.pop(tid, None)
            if entry is None:
                return None
            if self._writers.get(entry["name"]) == tid:
                del self._writers[entry["name"]]
        try:
            got = self._rpc(entry["node"], "term_close", project=bus.ADMIN,
                            name=entry["name"], tid=tid, owner=entry["actor"][0])
            if got.get("ok") is not True:
                raise BridgeError(502, "terminal release could not be confirmed")
            if entry["mode"] == "rw":
                self._audit(entry, "released")
        except BridgeError as e:
            return e.error
        return None

    def expire(self):
        """Закрыть только протухшие attach-клиенты; failures вернуть вызывающему."""
        with self._lock:
            expired = [tid for tid, entry in self._bindings.items()
                       if entry["until"] <= self.clock()]
        failures = []
        for tid in expired:
            reason = self._detach(tid)
            if reason:
                failures.append(reason)
        return failures

    def revoke(self, browser):
        """Logout отзывает handles этой HTTP-сессии, не сессии другого браузера."""
        with self._lock:
            tids = [tid for tid, entry in self._bindings.items()
                    if entry["actor"][1] == browser]
        failures = []
        for tid in tids:
            reason = self._detach(tid)
            if reason:
                failures.append(reason)
        return {"closed": len(tids), "errors": failures}

    def open(self, name, mode, cols, rows, authorize):
        actor = self._actor(authorize)
        if mode not in ("ro", "rw") or not _integer(cols, 20, 500) or not _integer(rows, 5, 200):
            raise BridgeError(400, "invalid terminal mode or dimensions")
        failures = self.expire()
        if failures:
            raise BridgeError(502, "; ".join(failures))
        reservation = object()
        with self._lock:
            if len(self._bindings) + self._pending >= self.maximum:
                raise BridgeError(429, "terminal connection limit reached")
            if mode == "rw" and name in self._writers:
                raise BridgeError(409, "terminal control is already held")
            self._pending += 1
            if mode == "rw":
                self._writers[name] = reservation
        try:
            try:
                context = self.resolve(name)
            except BridgeError:
                raise
            except Exception:
                raise BridgeError(409, "cannot inspect the terminal target") from None
            node, project = context.get("node"), context.get("project")
            if not node or not project or project == bus.ADMIN:
                raise BridgeError(409, "cannot determine the target project")
            entry = {"name": name, "node": node, "project": project,
                     "actor": actor, "mode": mode, "until": self.clock() + self.idle}
            if mode == "rw":
                try:
                    facts = self._rpc(node, "state", project=project, name=name)
                except BridgeError as e:
                    raise BridgeError(409, e.error) from None
                try:
                    safe, reason = ops.safety(name, project, owner=actor[0], facts=facts,
                                              continuation=True)
                except (ValueError, TypeError, AttributeError):
                    safe, reason = False, "cannot inspect the terminal target"
                if not safe:
                    raise BridgeError(409, reason)
                self._audit(entry, "accepted")
            got = self._rpc(node, "term_open", project=bus.ADMIN, name=name,
                            owner=actor[0], mode=mode, cols=cols, rows=rows)
            tid = got.get("tid")
            if (not _identifier(tid) or not _identifier(got.get("epoch"))
                    or got.get("mode") != mode or not _integer(got.get("cursor"), 0, 2**63 - 1)):
                raise BridgeError(502, "terminal attachment could not be confirmed")
            entry["epoch"] = got["epoch"]
            with self._lock:
                if tid in self._bindings:
                    raise BridgeError(502, "terminal attachment identifier is already in use")
                self._bindings[tid] = entry
                if mode == "rw":
                    self._writers[name] = tid
            try:
                if self._actor(authorize) != actor:
                    raise BridgeError(403, "terminal principal changed")
            except BridgeError as e:
                reason = self._detach(tid)
                if reason:
                    e.error += f"; {reason}"
                raise
            return {key: got[key] for key in ("tid", "epoch", "cursor", "mode", "cols", "rows")}
        finally:
            with self._lock:
                self._pending -= 1
                if self._writers.get(name) is reservation:
                    del self._writers[name]

    def _entry(self, name, tid, authorize, write=False):
        actor = self._actor(authorize)
        with self._lock:
            entry = self._bindings.get(tid)
            if entry is None or entry["name"] != name:
                raise BridgeError(404, "no such terminal")
            if entry["actor"] != actor:
                raise BridgeError(403, "terminal belongs to another browser session")
            expired = entry["until"] <= self.clock()
            if write and entry["mode"] != "rw":
                raise BridgeError(403, "terminal is read-only")
        if expired:
            reason = self._detach(tid)
            raise BridgeError(410, reason or "terminal attachment expired")
        return entry

    def read(self, name, tid, cursor, authorize, wait=15):
        if not _integer(cursor, 0, 2**63 - 1) or not _integer(wait, 0, 15):
            raise BridgeError(400, "invalid terminal cursor or wait")
        entry = self._entry(name, tid, authorize)
        got = self._rpc(entry["node"], "term_read", project=bus.ADMIN,
                        name=name, tid=tid, owner=entry["actor"][0], cursor=cursor, wait=wait)
        try:
            if self._actor(authorize) != entry["actor"]:
                raise BridgeError(403, "terminal principal changed")
        except BridgeError as e:
            reason = self._detach(tid)
            if reason:
                e.error += f"; {reason}"
            raise
        if got.get("epoch") != entry["epoch"]:
            reason = self._detach(tid)
            raise BridgeError(409, reason or "terminal conversation changed; reopen explicitly")
        data = got.get("data")
        try:
            raw = base64.b64decode(data, validate=True) if isinstance(data, str) else None
        except (ValueError, binascii.Error):
            raw = None
        if (raw is None or len(raw) > 65536 or not _integer(got.get("cursor"), 0, 2**63 - 1)
                or not isinstance(got.get("reset"), bool) or not isinstance(got.get("closed"), bool)
                or (not got["reset"] and got["cursor"] < cursor)):
            raise BridgeError(502, "terminal returned an invalid frame")
        if got["closed"] or got["reset"]:
            reason = self._detach(tid)
            if reason:
                got["error"] = reason
        else:
            with self._lock:
                if self._bindings.get(tid) is entry:
                    entry["until"] = self.clock() + self.idle
        return {key: got.get(key) for key in ("data", "cursor", "reset", "closed", "exit", "epoch", "error")}

    def write(self, name, tid, seq, data, authorize):
        if not _integer(seq, 1, 2**63 - 1) or not isinstance(data, str):
            raise BridgeError(400, "invalid terminal input")
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise BridgeError(400, "invalid terminal input") from None
        if not 0 < len(raw) <= 4096:
            raise BridgeError(400, "terminal input exceeds the byte limit")
        entry = self._entry(name, tid, authorize, write=True)
        try:
            got = self._rpc(entry["node"], "term_write", project=bus.ADMIN,
                            name=name, tid=tid, owner=entry["actor"][0], seq=seq, data=data)
            if (got.get("seq") != seq or got.get("outcome") not in ("written", "duplicate")
                    or not _integer(got.get("written"), 0, len(raw))):
                raise BridgeError(502, "terminal input could not be confirmed")
            return {key: got.get(key) for key in ("written", "seq", "outcome")}
        except BridgeError as e:
            return {"outcome": "refused" if e.code == 409 else "unknown",
                    "seq": seq, "written": None, "error": e.error}

    def resize(self, name, tid, cols, rows, authorize):
        if not _integer(cols, 20, 500) or not _integer(rows, 5, 200):
            raise BridgeError(400, "invalid terminal dimensions")
        entry = self._entry(name, tid, authorize, write=True)
        return self._rpc(entry["node"], "term_resize", project=bus.ADMIN,
                         name=name, tid=tid, owner=entry["actor"][0], cols=cols, rows=rows)

    def close(self, name, tid, authorize):
        self._entry(name, tid, authorize)
        reason = self._detach(tid)
        return {"ok": reason is None, "error": reason}
