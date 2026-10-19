"""Одноразовый local Docker fixture: normal mode никогда не запускается на host.

В image идут только два tracked модуля, native ELF и metadata пространства имён.
Ни домашний каталог, ни Docker socket, ни исходное дерево не монтируются.
"""
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
from urllib.parse import urlsplit

OWNER_LABEL = "mop.chatprobe.owner"
FIXTURE_UID = 26430
SOURCES = ("mop/client/chatprobe.py", "mop/client/chatprobe_isolation.py")
INBOX_SOURCES = ('mop/__init__.py', 'mop/client/__init__.py', 'mop/common/__init__.py',
    'mop/session.py', 'mop/common/endpoint.py', 'mop/common/deps.py',
    'mop/client/control.py', 'mop/client/action_catalogue.py', 'mop/client/local_bridge.py',
    'mop/client/private_inbox.py', 'mop/client/inboxprobe.py')
NAMESPACES = ("pid", "mnt", "net", "cgroup", "ipc")
RUNTIME_FIELDS = ("uid", "euid", "groups", "cap_eff", "no_new_privs", "namespaces", "interfaces",
                  "proc_private", "cgroup_readonly", "cgroup_root", "run_sockets_absent",
                  "external_network_unreachable")


def fixture_sources(tracked, *, private_inbox=False):
    """Конечный source closure; ни .env, ни config host-а не копируются."""
    selected = SOURCES + (INBOX_SOURCES if private_inbox else ())
    if not set(selected) <= set(tracked):
        raise ValueError('isolated probe requires its complete tracked source closure')
    return selected


def local_docker_endpoint(endpoint):
    """Никаких tcp/ssh contexts: snapshot нельзя публиковать удалённому daemon."""
    url = urlsplit(endpoint)
    if (url.scheme != "unix" or url.netloc or not url.path.startswith("/") or url.query
            or url.fragment or ".." in Path(url.path).parts or "%" in endpoint
            or any(ch.isspace() for ch in endpoint) or endpoint != "unix://" + url.path):
        raise ValueError("isolated probe requires a local Unix Docker endpoint")
    return endpoint


def container_create_args(image, name, owner, uid):
    """Фиксированный runtime без bind/volume/device и без host namespaces."""
    if type(uid) is not int or uid <= 0:
        raise ValueError("isolated probe requires a nonroot fixture UID")
    return ["create", "--name", name, "--label", OWNER_LABEL + "=" + owner,
            "--network", "none", "--cgroupns", "private", "--ipc", "private",
            "--user", f"{uid}:{uid}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges=true", "--pids-limit", "512",
            "--memory", "2g", "--cpus", "2", image]


def container_config_errors(info, image, owner, uid):
    """Проверяется ответ daemon, а не намерение в собственном argv."""
    errors = []
    config, host = info.get("Config", {}), info.get("HostConfig", {})
    checks = {
        "owned image/label": info.get("Image") == image and config.get("Labels", {}).get(OWNER_LABEL) == owner,
        "nonroot UID": config.get("User") == f"{uid}:{uid}" and uid > 0,
        "network none": host.get("NetworkMode") == "none",
        "private PID namespace": host.get("PidMode") in ("", "private"),
        "private cgroup namespace": host.get("CgroupnsMode") == "private",
        "private IPC namespace": host.get("IpcMode") == "private",
        "unprivileged": host.get("Privileged") is False and not host.get("CapAdd")
            and host.get("CapDrop") == ["ALL"],
        "no new privileges": host.get("SecurityOpt") in (["no-new-privileges=true"], ["no-new-privileges"]),
        "no host mounts/devices": not any((info.get("Mounts"), config.get("Volumes"),
            *(host.get(key) for key in ("Binds", "Mounts", "Devices", "DeviceRequests", "VolumesFrom")))),
    }
    for name, passed in checks.items():
        if not passed:
            errors.append("container isolation not proved: " + name)
    return errors


def runtime_projection(evidence):
    """Ни environ, ни mountinfo, ни произвольные payload поля не публикуются."""
    projected = {name: evidence.get(name) for name in RUNTIME_FIELDS}
    projected["namespaces"] = {name: evidence.get("namespaces", {}).get(name) for name in NAMESPACES}
    return projected


def runtime_isolation_errors(evidence, host_namespaces, uid):
    """Kernel witness нужен до --version/--help и до первого native exec."""
    namespaces = evidence.get("namespaces", {})
    checks = {
        "distinct PID/mount/net/cgroup/IPC namespaces": all(
            re.fullmatch(name + r":\[[0-9]+\]", str(namespaces.get(name, "")))
            and re.fullmatch(name + r":\[[0-9]+\]", str(host_namespaces.get(name, "")))
            and namespaces[name] != host_namespaces[name] for name in NAMESPACES),
        "nonroot fixture identity": uid > 0 and evidence.get("uid") == evidence.get("euid") == uid
            and evidence.get("groups") == [uid],
        "zero effective capabilities": evidence.get("cap_eff") == "0000000000000000",
        "no new privileges": evidence.get("no_new_privs") == 1,
        "loopback only": evidence.get("interfaces") == ["lo"],
        "external network unreachable": evidence.get("external_network_unreachable") is True,
        "private proc mount": evidence.get("proc_private") is True,
        "private readonly cgroup root": evidence.get("cgroup_readonly") is True and evidence.get("cgroup_root") == "/",
        "no run/dbus/docker sockets": evidence.get("run_sockets_absent") is True,
    }
    return ["runtime isolation not proved: " + name for name, passed in checks.items() if not passed]


def _namespaces():
    return {name: os.readlink("/proc/self/ns/" + name) for name in NAMESPACES}


def runtime_evidence():
    """Только собственный /proc; host PID/cgroup не монтируются для проверки."""
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields, extra = left.split(), right.split()
        mounts.append((fields[3], fields[4], fields[5].split(","), extra[0]))
    proc = [row for row in mounts if row[1] == "/proc"]
    cgroup = [row for row in mounts if row[1] == "/sys/fs/cgroup"]
    # TEST-NET-1 не провайдер. Нужен именно kernel ENETUNREACH, не timeout.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(.3)
        unreachable = connection.connect_ex(("192.0.2.1", 443)) == errno.ENETUNREACH
    return runtime_projection(dict(external_network_unreachable=unreachable,
        uid=os.getuid(), euid=os.geteuid(), groups=sorted(os.getgroups()),
        cap_eff=status.get("CapEff", "").strip(), no_new_privs=int(status.get("NoNewPrivs", "0")),
        namespaces=_namespaces(), interfaces=sorted(path.name for path in Path("/sys/class/net").iterdir()),
        proc_private=len(proc) == 1 and proc[0][0] == "/" and proc[0][3] == "proc",
        cgroup_readonly=len(cgroup) == 1 and "ro" in cgroup[0][2] and cgroup[0][3] == "cgroup2",
        cgroup_root=cgroup[0][0] if len(cgroup) == 1 else None,
        run_sockets_absent=not any(stat.S_ISSOCK(path.lstat().st_mode)
            for directory in ("/run", "/var/run") for path in Path(directory).rglob("*"))))


def verify_normal_runtime():
    """Внутренний entrypoint не становится unsafe host-launch флагом."""
    metadata = json.loads(Path("/opt/probe/host-namespaces.json").read_text())
    evidence = runtime_evidence()
    errors = runtime_isolation_errors(evidence, metadata["namespaces"], metadata["uid"])
    if os.getuid() == metadata["host_uid"]:
        errors.append("runtime isolation not proved: fixture UID equals host UID")
    if errors:
        raise RuntimeError("; ".join(errors))
    return evidence


def _docker_endpoint(docker, source):
    """Context inspect читает лишь endpoint metadata, не выводит config/auth."""
    if source.get("DOCKER_CONTEXT") or not source.get("DOCKER_HOST"):
        env = {key: source[key] for key in ("PATH", "HOME", "DOCKER_CONFIG") if key in source}
        command = [docker, "context", "inspect"]
        if source.get("DOCKER_CONTEXT"):
            command.append(source["DOCKER_CONTEXT"])
        command += ["--format", "{{.Endpoints.docker.Host}}"]
        result = subprocess.run(command, env=env, cwd="/", capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise RuntimeError("Docker context endpoint could not be inspected; no fixture copied")
        endpoint = result.stdout.strip()
    else:
        endpoint = source["DOCKER_HOST"]
    return local_docker_endpoint(endpoint)


def _copy_regular(source, target, native=False):
    """Разрешённый fd, а не дерево родителя; symlink нужен только native launcher."""
    source = Path(source)
    original = source.lstat()
    if source.is_symlink() and not native:
        raise RuntimeError("fixture source symlinks are not allowed")
    resolved = source.resolve(strict=True)
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid not in (0, os.getuid())
                or before.st_mode & (0o022 if native else 0o002) or native and not before.st_mode & 0o111):
            raise RuntimeError("fixture copy requires a trusted regular file")
        digest = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb") as stream, target.open("xb") as output:
            header = stream.read(4)
            if native and header != b"\x7fELF":
                raise RuntimeError("installed Claude is not a standalone native ELF; no host fallback")
            output.write(header)
            digest.update(header)
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        after = os.fstat(fd)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if identity(before) != identity(after) or identity(original) != identity(source.lstat()):
            raise RuntimeError("trusted file changed during fixture snapshot")
        target.chmod(0o755 if native else 0o644)
        return dict(path=str(source), resolved_path=str(resolved), symlink=stat.S_ISLNK(original.st_mode),
                    uid=before.st_uid, mode=oct(stat.S_IMODE(before.st_mode)), size=before.st_size,
                    sha256=digest.hexdigest())
    finally:
        os.close(fd)


def _dockerfile(uid, owner, private_inbox=False):
    """Только публичные зависимости; runtime сеть выключена отдельной create."""
    inbox = '''COPY source /opt/probe/source
RUN python3 -I -c "import runpy,subprocess,sys; d=runpy.run_path('/opt/probe/source/mop/common/deps.py'); subprocess.run([sys.executable,'-m','pip','install',next(x for x in d['PIP'] if x.split('=')[0]=='aiohttp')],check=True)"
''' if private_inbox else ''
    return f'''FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends tmux git openssl ca-certificates libstdc++6 libgcc-s1 tini && rm -rf /var/lib/apt/lists/*
{inbox}
RUN groupadd -g {uid} fixture && useradd -u {uid} -g {uid} -M -d /tmp/fixture-home fixture && mkdir /evidence && chown {uid}:{uid} /evidence
COPY claude /usr/local/bin/claude
COPY chatprobe.py chatprobe_isolation.py host-namespaces.json /opt/probe/
LABEL {OWNER_LABEL}="{owner}"
USER {uid}:{uid}
WORKDIR /tmp
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/python3", "-I", "/opt/probe/chatprobe_isolation.py"]
'''


def run(root, timeout, managed, mcp_activation=False, private_inbox=False, native_binary=None):
    """Владение по nonce+image+id, копия отчёта private, cleanup не по имени."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o700)
    report = dict(report_path=str(root / "report.json"), fixture_backed=True, paid_provider_tested=False,
        isolation_mode="isolated-normal Docker fixture", scenarios={}, errors=[],
        docker_isolation={"verified": False}, cleanup={})
    owner = uuid.uuid4().hex
    tag, name = "mop-chatprobe:" + owner, "mop-chatprobe-" + owner
    image = container = docker = None
    native_host_identity = None
    uid = FIXTURE_UID if os.getuid() != FIXTURE_UID else FIXTURE_UID + 1
    from mop.client import chatprobe as p
    def save():
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        (root / "report.json").chmod(0o600)
    try:
        if managed or p.managed_paths():
            raise RuntimeError("host managed settings could override probe policy; no fixture copied or launched")
        docker = shutil.which("docker")
        if not docker:
            raise RuntimeError("isolated normal probe requires local Docker; no host fallback")
        endpoint = _docker_endpoint(docker, os.environ)
        report["docker_isolation"]["endpoint"] = endpoint
        with tempfile.TemporaryDirectory(prefix="mop-probe-build-") as directory:
            build = Path(directory)
            (build / "docker-config").mkdir(mode=0o700)
            client_env = {"PATH": os.defpath, "HOME": str(build), "DOCKER_CONFIG": str(build / "docker-config"),
                          "DOCKER_BUILDKIT": "0"}
            def call(words, seconds=30, check=True):
                result = subprocess.run([docker, "--host", endpoint, *words], env=client_env, cwd="/",
                    capture_output=True, text=True, timeout=seconds)
                if check and result.returncode:
                    # Вывод build/native бывает приватным: полный stderr только в private log.
                    with (root / "docker.log").open("a") as log:
                        log.write(result.stdout + result.stderr)
                    raise RuntimeError("owned Docker " + words[0] + " failed; see private docker.log")
                return result
            def inspect(kind, target):
                result = call([kind, "inspect", target])
                values = json.loads(result.stdout)
                if len(values) != 1:
                    raise RuntimeError("Docker inspect identity is ambiguous")
                return values[0]
            def own_container():
                info = inspect("container", container)
                if info.get("Id") != container or info.get("Config", {}).get("Labels", {}).get(OWNER_LABEL) != owner:
                    raise RuntimeError("owned container identity changed; action refused")
                return info
            def own_image(target):
                info = inspect("image", target)
                if info.get("Config", {}).get("Labels", {}).get(OWNER_LABEL) != owner:
                    raise RuntimeError("owned image identity changed; action refused")
                return info
            try:
                call(["version", "--format", "{{.Server.Version}}"], seconds=10)
                native = native_binary if private_inbox and native_binary is not None else shutil.which("claude")
                if not native:
                    raise RuntimeError("isolated normal probe requires an installed native Claude binary")
                # Новый context не включает даже исходный корень и его ignored файлы.
                context = build / "context"
                context.mkdir(mode=0o700)
                top = Path(__file__).resolve().parents[2]
                tracked = subprocess.run(["git", "-C", str(top), "ls-files", "-z"],
                    env={"PATH": os.defpath, "HOME": str(build), "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_CONFIG_GLOBAL": "/dev/null"}, cwd="/", capture_output=True, check=True, timeout=10)
                selected = fixture_sources(tracked.stdout.decode().split("\0"), private_inbox=private_inbox)
                if p.managed_paths():
                    raise RuntimeError("host managed settings appeared; fixture snapshot refused")
                manifest = {}
                for path in selected:
                    target = context / Path(path).name if path in SOURCES else context/'source'/path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    manifest[path] = _copy_regular(top/path, target)
                    if private_inbox and path in SOURCES:
                        package_target = context/'source'/path
                        package_target.parent.mkdir(parents=True, exist_ok=True)
                        _copy_regular(top/path, package_target)
                manifest["native"] = _copy_regular(native, context / "claude", native=True)
                report["snapshot"] = manifest
                metadata = dict(namespaces=_namespaces(), host_uid=os.getuid(), uid=uid, timeout=timeout,
                                mcp_activation=bool(mcp_activation), private_inbox=bool(private_inbox))
                (context / "host-namespaces.json").write_text(json.dumps(metadata))
                (context / "Dockerfile").write_text(_dockerfile(uid, owner, private_inbox))
                # Последняя policy проверка до передачи даже безопасного snapshot daemon-у.
                if p.managed_paths():
                    raise RuntimeError("host managed settings appeared; fixture build refused")
                result = call(["build", "--label", OWNER_LABEL + "=" + owner, "-t", tag, str(context)], seconds=600)
                (root / "build.log").write_text(result.stdout + result.stderr)
                image = own_image(tag)["Id"]
                report["docker_isolation"].update(image=image, owner=owner, fixture_uid=uid)
                if p.managed_paths():
                    raise RuntimeError("host managed settings appeared; fixture launch refused")
                container = call(container_create_args(image, name, owner, uid)).stdout.strip()
                if not re.fullmatch(r"[a-f0-9]{64}", container):
                    raise RuntimeError("Docker did not return an owned container ID")
                config = own_container()
                errors = container_config_errors(config, image, owner, uid)
                if errors:
                    raise RuntimeError("; ".join(errors))
                report["docker_isolation"]["inspect_verified"] = True
                if p.managed_paths():
                    raise RuntimeError("host managed settings appeared; fixture start refused")
                call(["start", container])
                running = own_container()
                native_host_identity = p.process_identity(running["State"]["Pid"]) if running["State"]["Running"] else None
                waited = call(["wait", container], seconds=300)
                stopped = own_container()
                if stopped["State"]["Running"]:
                    raise RuntimeError("owned container still running after wait")
                exitcode = int(waited.stdout.strip())
                logs = call(["logs", container])
                (root / "container.log").write_text(logs.stdout + logs.stderr)
                evidence = root / "fixture"
                evidence.mkdir(mode=0o700)
                call(["cp", container + ":/evidence/.", str(evidence)])
                for path in evidence.rglob("*"):
                    if path.is_symlink():
                        raise RuntimeError("copied fixture evidence contains a symlink")
                    path.chmod(0o700 if path.is_dir() else 0o600)
                kernel_path = evidence / "isolation.json"
                if kernel_path.exists():
                    kernel = runtime_projection(json.loads(kernel_path.read_text()))
                    errors = runtime_isolation_errors(kernel, metadata["namespaces"], uid)
                    report["docker_isolation"].update(kernel=kernel, verified=not errors)
                    report["errors"].extend(errors)
                inner_path = evidence / "native" / "report.json"
                if inner_path.exists():
                    inner = json.loads(inner_path.read_text())
                    # Собственный fixture report; сырые request/transcript не включаются в outer.
                    report.update({key: inner[key] for key in ("scenarios", "model_hooks", "mcp_activation", "claude_version",
                        "live_snapshots", "fixture_auth", "git_ssh_transport_blocked", "caller_git_metadata_absent",
                        "unexpected_home_paths", "initial_identity") if key in inner})
                    report["errors"].extend(inner.get("errors", []))
                    report["cleanup"]["native"] = inner.get("cleanup")
                if not report["docker_isolation"]["verified"]:
                    raise RuntimeError("container kernel isolation not proved; no host fallback")
                if exitcode or not inner_path.exists():
                    raise RuntimeError(f"owned fixture exited {exitcode}; see private container.log")
            finally:
                if container:
                    try:
                        own_container()
                        call(["rm", "--force", container])
                        remaining = call(["container", "ls", "--all", "--quiet", "--no-trunc",
                                          "--filter", "label=" + OWNER_LABEL + "=" + owner])
                        report["cleanup"]["container_removed"] = not remaining.stdout.strip()
                        if not report["cleanup"]["container_removed"]:
                            report["errors"].append("owned container remains after cleanup")
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                        report["errors"].append("container cleanup failed: " + str(error))
                # PID/start принадлежит только PID1 доказанного контейнера, не произвольному CLI.
                if native_host_identity:
                    current = p.process_identity(native_host_identity["pid"])
                    gone = not current or current["start_ticks"] != native_host_identity["start_ticks"]
                    report["cleanup"]["container_pid_start_gone"] = gone
                    report["cleanup"]["container_initial_process"] = native_host_identity
                    if not gone:
                        report["errors"].append("owned container PID/start remains after cleanup")
                if image or (context.exists() if 'context' in locals() else False):
                    try:
                        candidate = call(["image", "ls", "--quiet", "--no-trunc", "--filter", "reference=" + tag])
                        if candidate.stdout.strip():
                            target = own_image(tag)["Id"]
                            if image and target != image:
                                raise RuntimeError("owned image ID changed; removal refused")
                            call(["image", "rm", tag])
                        remaining = call(["image", "ls", "--quiet", "--no-trunc", "--filter", "label=" + OWNER_LABEL + "=" + owner])
                        report["cleanup"]["image_removed"] = not remaining.stdout.strip()
                        if not report["cleanup"]["image_removed"]:
                            report["errors"].append("owned image remains after cleanup")
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                        report["errors"].append("image cleanup failed: " + str(error))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["errors"].append(str(error))
    finally:
        names = (("mcp-activation", "live-registry-contract") if mcp_activation else
                 ("model-hooks-control", "model-hooks-same-model", "model-hooks-change", "model-hooks-return",
                  "live-registry-contract"))
        for name in names:
            report["scenarios"].setdefault(name, {"outcome": "unknown", "evidence": {"reason": "scenario not reached"}})
        for path in root.iterdir():
            if path.is_file():
                path.chmod(0o600)
        save()
    return report


def _container_main():
    """Фиксированный image entrypoint; ни subprocess argv, ни host launch API."""
    os.umask(0o077)
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("owned normal fixture requires Python 3.12; no native exec")
    evidence = verify_normal_runtime()
    Path("/evidence/isolation.json").write_text(json.dumps(evidence))
    spec = importlib.util.spec_from_file_location("chatprobe_fixture", "/opt/probe/chatprobe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    metadata = json.loads(Path("/opt/probe/host-namespaces.json").read_text())
    private_inbox = metadata.get('private_inbox') is True
    if private_inbox:
        sys.path.insert(0, '/opt/probe/source')
    report = probe._run_fixture("/evidence/native", metadata["timeout"], model_hooks=not private_inbox, _normal=True,
                                mcp_activation=metadata.get("mcp_activation") is True, private_inbox=private_inbox)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    if len(sys.argv) != 1:
        sys.exit("fixed container entrypoint takes no arguments")
    try:
        sys.exit(_container_main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit("isolated fixture blocked: " + str(error))
