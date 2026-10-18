#!/usr/local/bin/python3 -I
"""Installed runner fixture: настоящий setsid/double-fork, не native CLI."""
import ctypes
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
if consumer not in ({'fixture': 'double-fork', 'schema_version': 1},
                    {'fixture': 'double-fork', 'schema_version': 1, 'hold_starter': True}):
    raise SystemExit(3)
starter = os.getpid()
if consumer.get('hold_starter'):
    # Только owned fixture: root фиксирует первый actual snapshot до double-fork.
    (body/'starter-ready').write_text(str(starter))
    deadline = time.monotonic()+5.
    while not (body/'starter-release').exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('fixture starter barrier deadline expired')
        time.sleep(.01)
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
# Kernel comm — opaque bytes: оба собственных descendants меняют имя,
# но не UID/starttime/cgroup/privileges. Pipe — barrier до root observation.
libc = ctypes.CDLL(None, use_errno=True)
names = ('тело'.encode(), b'\xff\xfe a)b(')
def set_name(value):
    if libc.prctl(15, ctypes.c_char_p(value), 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'fixture PR_SET_NAME failed')
set_name(names[0])
ready_read, ready_write = os.pipe()
opaque_child = os.fork()
if opaque_child == 0:
    os.close(ready_read)
    set_name(names[1])
    os.write(ready_write, b'1')
    os.close(ready_write)
    while True:
        time.sleep(1)
os.close(ready_write)
if os.read(ready_read, 1) != b'1':
    raise RuntimeError('opaque child did not reach fixture barrier')
os.close(ready_read)
# Consumer имеет право менять собственный cwd; retirement не зависит от
# повторного launch admission этой директории (#725).
body.chmod(0o755)
record = dict(pid=os.getpid(), starter=starter, sid=os.getsid(0), uid=os.getuid(), gid=os.getgid(),
              cgroup=Path('/proc/self/cgroup').read_text().strip(), checks=checks,
              opaque_names=[dict(pid=os.getpid(), name=names[0].hex()),
                            dict(pid=opaque_child, name=names[1].hex())])
(body/'descendant.json').write_text(json.dumps(record))
while True:
    time.sleep(1)
