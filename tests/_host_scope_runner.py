#!/usr/local/bin/python3 -I
"""Installed runner fixture: настоящий setsid/double-fork, не native CLI."""
import json
import os
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, '/opt/fixture')
from mop.node.host_scope import Scope

if len(sys.argv) != 3 or sys.argv[1] != '--scope':
    raise SystemExit(2)
scope = Scope.from_unit(sys.argv[2])
body = Path('/var/lib/mop/host-bodies') / scope.launch_id
consumer = json.loads((body/'consumer.json').read_text())
if consumer != {'fixture': 'double-fork', 'schema_version': 1}:
    raise SystemExit(3)
starter = os.getpid()
if os.fork():
    raise SystemExit(0)
os.setsid()
if os.fork():
    os._exit(0)

checks = {}
try:
    Path('/var/lib/mop/node-service/bearer').read_bytes()
except OSError:
    checks['node_credential_denied'] = True
else:
    checks['node_credential_denied'] = False
sock = socket.socket(socket.AF_UNIX)
try:
    sock.connect('/run/mop-host/supervisor.sock')
except OSError:
    checks['supervisor_denied'] = True
else:
    checks['supervisor_denied'] = False
finally:
    sock.close()
try:
    Path('/sys/fs/cgroup/cgroup.procs').write_text(str(os.getpid()))
except OSError:
    checks['cgroup_migration_denied'] = True
else:
    checks['cgroup_migration_denied'] = False
for name, path in (('system_manager_denied', '/run/systemd/private'),
                   ('system_bus_denied', '/run/dbus/system_bus_socket')):
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.connect(path)
    except OSError:
        checks[name] = True
    else:
        checks[name] = False
    finally:
        sock.close()
checks['no_user_manager_runtime'] = not os.access('/run/user', os.R_OK)
# Consumer имеет право менять собственный cwd; retirement не зависит от
# повторного launch admission этой директории (#725).
body.chmod(0o755)
record = dict(pid=os.getpid(), starter=starter, sid=os.getsid(0), uid=os.getuid(), gid=os.getgid(),
              cgroup=Path('/proc/self/cgroup').read_text().strip(), checks=checks)
(body/'descendant.json').write_text(json.dumps(record))
while True:
    time.sleep(1)
