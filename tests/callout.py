#!/usr/bin/env python3
"""Проверка auth callout без пула: python3 tests/callout.py

NATS на каждом входе пользователя, которого нет в auth_users, спрашивает
сервис mop-callout (#206): запрос -- JWT, подписанный ключом сервера, в нём
логин и пароль; ответ -- JWT, подписанный издателем, с вложенным JWT
пользователя и его правами. Здесь -- чистая часть: JWT и ящики xkey против
векторов, снятых с настоящего nats-server 2.14.6 (стенд #203,
tests/callout_vectors.json), и решение сервиса: запрос -> человек или папет
-> ответ либо отказ. Подписка на шине и сам вход проверяются только на
стенде и на живом пуле.

HYPOTHESIS: личность на шине -- статический users.conf, и отправитель
называет себя сам (#161); проверить логин при входе некому.
SOLUTION: NATS спрашивает mop-callout; людей проверяет провайдер личностей
(#205), папетов -- файлы паролей сервера; права -- те же функции, что
рендерят users.conf сегодня.
STATUS: FIXED — see #206
"""
import base64
import json
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import busnames  # noqa: E402
from mop.server import natsconf, operators  # noqa: E402
from mop.server.identity import Identity, Refused  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
V = json.load(open(os.path.join(HERE, "callout_vectors.json")))
failed = []


def check(what, ok, detail=""):
    if not ok:
        failed.append(f"{what}" + (f": {detail}" if detail else ""))


def body(token):
    """Тело JWT без проверки подписи -- чтобы смотреть, что внутри."""
    p = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


def request(user, password, ctype="websocket", nkey="UAT4IJYF4MVE2SIJXESLQ2K2ZOAP3GIQJDLG7IAO2LOILSQIIFB2KQNE"):
    """Тело запроса сервера, как у вектора, с другим входящим."""
    req = body(V["request"])
    n = req["nats"]
    n["user_nkey"] = nkey
    n["client_info"] = dict(n["client_info"], user=user, type=ctype)
    n["connect_opts"] = dict(n["connect_opts"], user=user, **{"pass": password})
    return req


class Humans:
    """Провайдер личностей для проверки: один anton (admin), один ivan (user)."""
    PEOPLE = {"anton": ("a-pw", Identity("anton", "admin", ())),
              "ivan": ("i-pw", Identity("ivan", "user", ("rugent", "cloudpub")))}

    def authenticate(self, login, password):
        got = self.PEOPLE.get(login)
        if got is None:
            raise Refused(f"{login}: unknown login")
        if password != got[0]:
            raise Refused(f"{login}: wrong password")
        return got[1]

    def lookup(self, login):
        got = self.PEOPLE.get(login)
        return got[1] if got else None


def main():
    try:
        import nacl  # noqa: F401
        import nkeys  # noqa: F401
    except ImportError as e:
        # Как jinja2 у tests/deploy.py: библиотеки ставит MOP_PIP_DEPS, и на
        # машине до `mop deploy` их может не быть. Громко, не молча.
        hermetic.skip("the callout checks", f"{e} -- pip install nkeys pynacl "
                      f"(MOP_PIP_DEPS brings them with mop deploy)")
        print("callout: ok (skipped)")
        return 0
    try:
        from mop.server import callout, nkjwt
    except ImportError as e:
        print(f"FAIL the callout modules are missing: {e}")
        print("callout: FAILED")
        return 1

    # ── JWT: подпись сервера, формат, подделка ───────────────────────────
    req = nkjwt.decode(V["request"])
    check("a request signed by the real server decodes",
          req["nats"]["type"] == "authorization_request", req.get("nats", {}).get("type"))
    check("the request is signed by the server it names",
          req["iss"] == req["nats"]["server_id"]["id"], req["iss"])
    head, payload, sig = V["request"].split(".")
    forged = body(V["request"])
    forged["nats"]["connect_opts"]["user"] = "anton"
    forged_payload = base64.urlsafe_b64encode(
        json.dumps(forged).encode()).rstrip(b"=").decode()
    try:
        nkjwt.decode(f"{head}.{forged_payload}.{sig}")
        check("a forged request must be refused", False)
    except ValueError:
        pass
    # Ответ, который сервер принял на стенде: наш encode обязан повторить
    # его байт в байт -- с теми же claims и тем же iat.
    resp = nkjwt.decode(V["response"])
    user = nkjwt.decode(resp["nats"]["jwt"])
    for what, token, claims in (("response", V["response"], resp),
                                ("user claim", resp["nats"]["jwt"], user)):
        again = nkjwt.encode({k: v for k, v in claims.items() if k not in ("iss", "iat", "jti")},
                             V["issuer_seed"], now=claims["iat"])
        check(f"encode reproduces the {what} the server accepted", again == token,
              f"\n  got  {again}\n  want {token}")
    check("public key of the issuer seed", nkjwt.public_of(V["issuer_seed"]) == V["issuer"])
    check("public key of the xkey seed", nkjwt.public_of(V["xkey_seed"]) == V["xkey"])

    # ── xkey: ящик сервера и круг ────────────────────────────────────────
    opened = nkjwt.xkey_open(V["xkey_seed"], V["server_xkey"],
                             base64.b64decode(V["sealed_request"]))
    check("the server's sealed request opens with our xkey", opened.decode() == V["request"])
    a_seed, a_pub = nkjwt.new_xkey()
    b_seed, b_pub = nkjwt.new_xkey()
    check("xkey round trip", nkjwt.xkey_open(b_seed, a_pub,
                                             nkjwt.xkey_seal(a_seed, b_pub, b"hi")) == b"hi")
    try:
        nkjwt.xkey_open(b_seed, a_pub, b"not a box")
        check("garbage must not open", False)
    except ValueError:
        pass

    # ── решение: человек ─────────────────────────────────────────────────
    root = tempfile.mkdtemp(prefix="mop-test-callout-")
    with open(os.path.join(root, busnames.pass_file(busnames.puppet_user("mop"))), "w") as f:
        f.write("pu-pass\n")
    puppets = callout.PuppetProvider(root)
    keys = callout.Keys(V["issuer_seed"], V["xkey_seed"])

    def answer(r):
        out = nkjwt.decode(callout.respond(r, Humans(), puppets, keys))
        claim = nkjwt.decode(out["nats"]["jwt"]) if out["nats"].get("jwt") else None
        return out, claim

    def refused(what, r, words):
        out, claim = answer(r)
        err = out["nats"].get("error", "")
        check(f"{what}: refused", claim is None and err, out["nats"])
        for w in words:
            check(f"{what}: the reason names {w!r}", w in err, err)

    r = request("anton", "a-pw")
    out, claim = answer(r)
    check("human: answered to this server and this connection",
          (out["aud"], out["sub"], out["iss"], out["nats"]["type"]) ==
          (r["nats"]["server_id"]["id"], r["nats"]["user_nkey"], V["issuer"],
           "authorization_response"), out)
    # Две стороны с #207: публикация -- явным списком с логином вызывающего,
    # подписка -- прежние маски. Той же функцией, что рендерит users.conf.
    rights = operators.permissions(Humans.PEOPLE["anton"][1])

    def side(allow, deny):
        return {"allow": allow, **({"deny": deny} if deny else {})}
    want_pub = side(rights["publish"], rights["publish_deny"])
    want_sub = side(rights["allow"], rights["deny"])
    check("human: the claim is for this connection, in $G, by name",
          claim and (claim["sub"], claim["aud"], claim["name"], claim["iss"]) ==
          (r["nats"]["user_nkey"], "$G", "anton", V["issuer"]), claim)
    check("human: publish rights are operators.permissions' publish side",
          claim and claim["nats"]["pub"] == want_pub, claim and claim["nats"].get("pub"))
    check("human: subscribe rights are operators.permissions' subscribe side",
          claim and claim["nats"]["sub"] == want_sub, claim and claim["nats"].get("sub"))
    # exp закрывает соединение в срок (стенд #206): при лежащем callout
    # человек не вернулся бы, а «живые соединения живут» -- обещание перехода.
    check("human: the claim never expires", claim and "exp" not in claim, claim)
    out, claim = answer(request("ivan", "i-pw"))
    rights = operators.permissions(Humans.PEOPLE["ivan"][1])
    check("user role: rights of its projects, deny omitted when empty",
          claim and claim["nats"]["pub"] == {"allow": rights["publish"]}
          and claim["nats"]["sub"] == {"allow": rights["allow"]}, claim and claim["nats"])
    # #207: логин -- токен субъекта; писать от чужого имени нельзя.
    pub = (claim or {}).get("nats", {}).get("pub", {}).get("allow", [])
    check("user role: publishes rpc under its own login only",
          busnames.node("rugent", "*", "rpc", login="ivan") in pub
          and busnames.node("rugent", "*", "rpc", login="anton") not in pub, pub)
    refused("human over plain nats (#105: WebSocket only)", request("anton", "a-pw", "nats"),
            ["WebSocket"])
    refused("human with a wrong password", request("anton", "nope"), ["anton", "wrong password"])
    refused("unknown human", request("mallory", "x"), ["mallory", "unknown"])

    # ── решение: папет ───────────────────────────────────────────────────
    r = request("puppet-mop", "pu-pass", "nats")
    out, claim = answer(r)
    perms = natsconf.puppet_permissions("mop")
    check("puppet: its project's rights (natsconf.puppet_permissions)",
          claim and claim["nats"]["pub"] == {"allow": perms["publish"]}
          and claim["nats"]["sub"] == {"allow": perms["subscribe"]}, claim and claim["nats"])
    check("puppet: in $G, by name, no expiry",
          claim and (claim["aud"], claim["name"], "exp" in claim) == ("$G", "puppet-mop", False),
          claim)
    refused("puppet with a wrong password", request("puppet-mop", "nope", "nats"),
            ["puppet-mop", "wrong password"])
    refused("puppet of a project without a password file", request("puppet-rugent", "x", "nats"),
            ["puppet-rugent"])
    refused("puppet name that walks out of the directory",
            request("puppet-../../etc/passwd", "x", "nats"), ["not a puppet"])
    for machine in (busnames.SERVICE, busnames.node_user("hyper"), callout.USER):
        refused(f"machine user {machine} (belongs in auth_users)",
                request(machine, "x", "nats"), [machine, "auth_users"])
    refused("no login at all", request("", "", "nats"), ["no login"])

    # ── круг сервиса: запечатанный запрос -> запечатанный ответ ──────────
    server_seed, server_pub = nkjwt.new_xkey()
    sealed = nkjwt.xkey_seal(server_seed, V["xkey"], V["request"].encode())
    answered = callout.answer(sealed, server_pub, keys, Humans(), puppets)
    reply = nkjwt.decode(nkjwt.xkey_open(server_seed, V["xkey"], answered).decode())
    check("sealed round trip: the reply opens with the server's key and answers it",
          reply["aud"] == req["nats"]["server_id"]["id"] and reply["nats"]["type"] ==
          "authorization_response", reply)

    print("\n".join(f"FAIL {line}" for line in failed) if failed else "", end="\n" if failed else "")
    print("callout: FAILED" if failed else "callout: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
