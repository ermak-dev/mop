#!/usr/bin/env python3
"""Чистые контракты локального smoke; установленный Claude не запускается."""
import importlib.util
from pathlib import Path
import tempfile
import hermetic  # noqa: F401
from _lib import Checks

# HYPOTHESIS: probe UI не доказывает связку Handler/Bridge/Registry.
# SOLUTION: отдельный фиксированный helper вне production слоёв.
# RESULT: отсутствие helper -- провал свойства, а не ошибка импорта.
# STATUS: FIXED — see #615

def main():
    c = Checks()
    path = Path(__file__).with_name("_chat_smoke.py")
    if not c.check("#615 integration helper exists", path.is_file()):
        return c.report("chat_smoke")
    spec = importlib.util.spec_from_file_location("smoke", path)
    s = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s)
    c.expect("#615 empty evidence is unknown", s.verdict({}), "unknown")
    c.expect("#615 every assertion required", s.verdict({"a": True, "b": False}), "unknown")
    c.expect("#615 unknown is not success", s.verdict({"a": None}), "unknown")
    c.expect("#615 explicit unsupported", s.verdict({}, unsupported=True), "unsupported")
    c.expect("#615 observed only with evidence", s.verdict({"a": True}), "observed")
    args = s.parser().parse_args(["--output", "/tmp/new", "--timeout", "3"])
    c.expect("#615 wait bounded", args.timeout, 3)
    for flags in (["--timeout", "0"], ["--timeout", "31"], ["--node-root", "relative"],
                  ["--history-root", "https://host/code"], ["--web-root", "../code"]):
        try:
            s.parser().parse_args(flags)
            c.fail("#615 unsafe argument refused", flags)
        except SystemExit:
            c.check("#615 unsafe argument refused", True)
    env = s.probe.isolated_env({"PATH": "/usr/bin", "SECRET": "secret", "TMUX": "foreign"},
                               "/private", "http://127.0.0.1:1234")
    c.check("#615 whitelist removes secrets and foreign socket", "secret" not in env.values() and "TMUX" not in env)
    c.expect("#615 dummy key", env["ANTHROPIC_API_KEY"], "chat-probe-dummy-key")
    c.expect("#615 SSH blocked", env["GIT_ALLOW_PROTOCOL"], "file")
    with tempfile.TemporaryDirectory() as directory:
        p = Path(directory) / "source.py"
        p.write_text("trusted source\n")
        before = s.source_hash(p)
        c.expect("#615 source digest stable", s.source_hash(p), before)
        p.write_text("changed\n")
        c.check("#615 source mutation detected", s.source_hash(p) != before)
    return c.report("chat_smoke")

if __name__ == "__main__":
    raise SystemExit(main())
