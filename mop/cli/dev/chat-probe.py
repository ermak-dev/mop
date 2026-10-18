"""mop dev chat-probe [--output DIR] [--timeout seconds] [--history-module /path/session.py] [--model-hooks] [--isolated-normal]: probe the installed interactive Claude

Uses a fresh home/workdir, a private randomly named tmux server and a loopback
Messages fixture with a dummy key. No existing session is resumed. All responses
are fixture-backed, not paid-provider evidence. Uses --bare to disable discovery
and keychain reads, with explicit Bash/AskUserQuestion tools where supported;
this may expose fewer tools than normal mode. Default permission mode remains
in force. External HTTP/HTTPS proxy attempts are refused; git SSH transports
are disabled and checked before Claude starts. This is not an OS network sandbox.
Managed settings presence refuses launch without reading their contents. Any
unexpected git, marketplace or credential files in its own home fail the run.
Each scenario reports observed, unsupported or unknown independently;
a partial probe is not a claim that every scenario works.

Writes report.json, lossless base64 terminal/input frames, pane snapshots and
fixture request bodies in a new private directory (default: ./chat-probe-UUID).
The directory must not already exist. --timeout is a per-wait bound, 1 to 30
seconds; each scenario is bounded below 120 seconds. Stops only its own tmux
server and fixture listener on exit. Uses a short private /tmp runtime outside
the caller's repository, with explicit tmux -S for every operation. Verifies
owned process PID/start identities after cleanup; leftovers are errors.
Requires installed claude, tmux and git. Temporary runtime is retained privately.

Captures live ready/after-model/after-clear registry, socket peer and key fields
without peerToken values. Optional --history-module explicitly selects a trusted
local regular session.py source (not a downloaded file); invokes its history
before and after /clear and records only safe headers/counts, not private records.

Optional --model-hooks replaces the default scenarios with same-model, real
change and return-to-target observations. A fixed quoted command uses isolated
Python and reads at most 4096 bytes; malformed and at-limit inputs are dropped.
Only event/session/to-model/from-model/receiver-time reach model-hooks.jsonl.
The new private fixture sink is activated after live PID/start/registry/socket
proof; startup hooks before activation are lost. This is fixture-only activation,
not production HookBridge/registration acceptance. Requested --model, statusline
and next fixture requests are not model confirmation. This probe implements no
readiness receipt or scope retirement. If the installed --help explicitly says
--bare skips settings hooks, hook scenarios report unsupported in this mode;
silence alone remains unknown and does not prove normal-mode hook behavior.
The host probe never removes --bare. Default scenarios/settings are unchanged.

--model-hooks --isolated-normal instead always builds a new owned nonroot Docker
fixture. It accepts only a local Unix Docker endpoint, refuses host managed
policy before copying/building/launching, snapshots only the installed native ELF
and two tracked fixture modules, and mounts no host paths or sockets. Public
image dependencies may be downloaded at build time. Runtime uses --network none,
private PID/cgroup/mount/net/IPC namespaces, a distinct fixture UID, dropped
capabilities and no-new-privileges. Both Docker inspect and kernel evidence must
prove isolation before the native CLI starts; failure has no host fallback.
Fresh home/config/runtime and the dummy loopback provider remain mandatory.
This mode cannot be combined with --history-module. Reports and raw fixture
artifacts are copied out privately before owned container/image cleanup; only
safe hook/identity projections reach report.json. Native version is measured
inside the container, not inferred from the host installation.

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
    history_module = None
    model_hooks = False
    isolated_normal = False
    args = iter(argv)
    try:
        for arg in args:
            if arg == "--output":
                root = Path(next(args))
            elif arg == "--model-hooks":
                model_hooks = True
            elif arg == "--isolated-normal":
                isolated_normal = True
            elif arg == "--history-module":
                history_module = next(args)
                if not chatprobe.valid_history_argument(history_module):
                    raise ValueError
            elif arg == "--timeout":
                timeout = int(next(args))
                if not 1 <= timeout <= 30:
                    raise ValueError
            else:
                usage(__doc__)
    except (StopIteration, ValueError):
        usage(__doc__)
    if isolated_normal and (not model_hooks or history_module):
        usage(__doc__)
    options = {"history_module": history_module}
    if model_hooks:
        options["model_hooks"] = True
    if isolated_normal:
        options["isolated_normal"] = True
    report = chatprobe.run(root, timeout, **options)
    print(report["report_path"])
    print("fixture-backed; paid provider not tested")
    for name, result in report["scenarios"].items():
        print(f"{name}: {result['outcome']}")
    for error in report["errors"]:
        print("runner error: " + error)
    return 1 if report["errors"] else 0
