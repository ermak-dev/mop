"""Синтетический native-shaped producer: только собственные registry/key/socket.

Не Claude и не model readiness. tmux исполняет этот файл через exec python -I;
SO_PEERCRED, process start, cwd, namespaces и controlling tty настоящие.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import subprocess


def main():
    path = Path(sys.argv[1])
    config = Path(os.environ['CLAUDE_CONFIG_DIR'])
    registry = config/'sessions'
    registry.mkdir(parents=True, exist_ok=True)
    fields = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(32)
    os.chmod(path, 0o600)
    key = registry/(str(os.getpid())+'.'+hashlib.sha256(str(path).encode()).hexdigest()+'.key')
    key.write_text(json.dumps({'peerToken': 'fixture-only-not-authority',
                              'procStart': fields[19], 'pidDomain': 'fixture-not-interpreted'}))
    key.chmod(0o600)
    record = dict(pid=os.getpid(), cwd=os.getcwd(), sessionId='owned-session',
                  messagingSocketPath=str(path), status='idle', statusUpdatedAt=1)
    def change_cwd(_number, _frame):
        os.chdir(os.environ['HOME'])
        (config/'cwd-changed').touch()

    def redirect_stdin(_number, _frame):
        fd = os.open('/dev/null', os.O_RDONLY)
        os.dup2(fd, 0)
        os.close(fd)
        (config/'stdin-changed').touch()

    def hook(marker):
        # Дочерний stdlib hook наследует env только от уже exec-нутого producer.
        # Файл хранит лишь исход/тишину, никогда credential или его fingerprint.
        if len(sys.argv) < 3:
            return
        payload = dict(session_id='owned-session', hook_event_name='SessionStart')
        result = subprocess.run([sys.executable, '-I', sys.argv[2], 'hook'],
            input=json.dumps(payload), capture_output=True, text=True, timeout=2)
        (config/marker).write_text(json.dumps([result.returncode, result.stdout, result.stderr]))

    signal.signal(signal.SIGUSR1, change_cwd)
    signal.signal(signal.SIGUSR2, redirect_stdin)
    signal.signal(signal.SIGWINCH, lambda _number, _frame: hook('hook-later'))
    hook('hook-startup')
    pending = registry/'publishing'
    pending.write_text(json.dumps(record))
    pending.rename(registry/'owned.json')
    while True:
        connection, _ = listener.accept()
        # Любые кадры наблюдателя -- дефект. Пустой connect/close без побочных effects.
        if connection.recv(1):
            (config/'unexpected-frame').write_text('observer sent a frame')
        connection.close()


if __name__ == '__main__':
    main()
