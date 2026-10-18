#!/usr/bin/env python3
"""Только PID 1 новой Docker fixture; никаких host mounts/manager changes."""
import ctypes
import errno
import json
import os
from pathlib import Path
import sys


def blocked(reason, number=None):
    print(json.dumps(dict(status='blocked', phase='private-cgroup', reason=reason,
                          errno=number)), flush=True)
    raise SystemExit(77)


if os.getpid() != 1:
    blocked('fixture entry must be private PID 1')
if Path('/proc/self/cgroup').read_text().strip() != '0::/':
    blocked('private unified cgroup namespace is unavailable')
libc = ctypes.CDLL(None, use_errno=True)
# Mount propagation сначала private, только затем новый cgroup2 mount в namespace.
if libc.mount(None, b'/', None, 16384 | (1 << 18), None):
    blocked('private mount propagation refused', ctypes.get_errno())
# Не позволяем private systemd менять global kernel knobs. Это self-bind
# внутри уже private mount namespace, не bind host directory из Docker.
for path in (b'/sys', b'/proc/sys', b'/proc/sysrq-trigger'):
    flags = 4096 | (16384 if path == b'/sys' else 0)
    if libc.mount(path, path, None, flags, None):
        blocked('fixture kernel view bind refused', ctypes.get_errno())
    if libc.mount(None, path, None, 4096 | 32 | 1, None):
        blocked('fixture readonly kernel view refused', ctypes.get_errno())
if libc.umount2(b'/sys/fs/cgroup', 2):
    blocked('private cgroup mount replacement refused', ctypes.get_errno())
if libc.mount(b'none', b'/sys/fs/cgroup', b'cgroup2', 0, None):
    blocked('kernel refused private writable cgroup-v2 mount', ctypes.get_errno())
try:
    test = Path('/sys/fs/cgroup/mop-fixture-kernel-check')
    test.mkdir()
    if not (test/'cgroup.kill').exists():
        blocked('kernel cgroup.kill is unavailable')
    test.rmdir()
except OSError as error:
    blocked('private cgroup-v2 subtree creation refused', error.errno)
print(json.dumps(dict(status='private-cgroup-created')), flush=True)
os.execv('/sbin/init', ['/sbin/init', '--unit=multi-user.target', '--show-status=no', '--log-level=warning'])
