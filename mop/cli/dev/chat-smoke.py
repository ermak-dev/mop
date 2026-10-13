"""mop dev chat-smoke [--output DIR] [--timeout seconds] [--node-root DIR] [--history-root DIR] [--web-root DIR] [--keep-serving]: local fixture-backed chat integration

Runs the fixed trusted tests/_chat_smoke.py helper, excluded from ordinary CI.
Requires installed Claude 2.1.296, tmux and git. Uses a fresh private home,
short own tmux socket and loopback Messages fixture with a dummy API key.
Exercises the actual authenticated HTTP Handler, TerminalBridge, node Registry,
body identity probe and session history source; only transport and identity
storage are local fixtures. No pool, NATS, deployment or paid provider calls.

Optional absolute trusted checkout roots select prerequisite node/history
sources or committed web/dist. Records revision/source hashes and refuses a
source mutation during the run. They are operator-supplied code, not downloads.
The output directory must be new; --timeout bounds each wait to 1..30 seconds.
Prints English JSON progress and a private report path. Unknown and unsupported
scenarios are never passed. Exit 1 means runner error or incomplete required
coverage. Default permission dialogs remain enabled; bare mode can omit tools.
Raw terminal control is a trusted admin capability, not a tmux key filter.

--keep-serving retains only this local fixture for private browser inspection
until interrupted; private report holds its fixture URL and test session token.
Never publish that fixture or use real credentials. Cleanup checks owned process
PID/start identities, attach clients and listener sockets, not foreign sessions.
"""
from pathlib import Path
import subprocess
import sys


def main(argv):
    helper = Path(__file__).resolve().parents[3] / "tests" / "_chat_smoke.py"
    return subprocess.run([sys.executable, "-I", str(helper), *argv], cwd="/").returncode
