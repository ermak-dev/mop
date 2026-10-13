"""mop dev chat-probe [--output DIR] [--timeout seconds]: probe the installed interactive Claude

Uses a fresh home/workdir, a private randomly named tmux server and a loopback
Messages fixture with a dummy key. No existing session is resumed. All responses
are fixture-backed, not paid-provider evidence. Uses --bare to disable discovery
and keychain reads, with explicit Bash/AskUserQuestion tools where supported;
this may expose fewer tools than normal mode. Default permission mode remains
in force. External HTTP/HTTPS proxy attempts are refused, not OS-sandboxed. Each scenario reports observed, unsupported or unknown independently;
a partial probe is not a claim that every scenario works.

Writes report.json, lossless base64 terminal/input frames, pane snapshots and
fixture request bodies in a new private directory (default: ./chat-probe-UUID).
The directory must not already exist. --timeout is a per-wait bound, 1 to 30
seconds; each scenario is bounded below 120 seconds. Stops only its own tmux
server and fixture listener on exit. Requires installed claude and tmux.

Prints the report path and one verdict per scenario. Exit 1 for runner errors,
not for unsupported or unknown scenarios. Does not contact the pool or load
installation credentials.
"""
from pathlib import Path
import uuid

from mop.client import chatprobe
from mop.cli.term import usage


def main(argv):
    root = Path.cwd() / ("chat-probe-" + uuid.uuid4().hex)
    timeout = 20
    args = iter(argv)
    try:
        for arg in args:
            if arg == "--output":
                root = Path(next(args))
            elif arg == "--timeout":
                timeout = int(next(args))
                if not 1 <= timeout <= 30:
                    raise ValueError
            else:
                usage(__doc__)
    except (StopIteration, ValueError):
        usage(__doc__)
    report = chatprobe.run(root, timeout)
    print(report["report_path"])
    print("fixture-backed; paid provider not tested")
    for name, result in report["scenarios"].items():
        print(f"{name}: {result['outcome']}")
    for error in report["errors"]:
        print("runner error: " + error)
    return 1 if report["errors"] else 0
