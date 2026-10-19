"""Только новая owned systemd fixture, actual phases, не native/pool/deploy."""
from pathlib import Path
import shutil

from mop.node.host_scope_probe import run_probe as owned_probe
from mop.node.host_scope_probe import snapshot_sources as scope_snapshot

# Явный source closure; никакого checkout/.env/credentials/host bind mount.
SOURCES = ('mop/node/host_body.py', 'mop/node/host_phase_supervisor.py', 'mop/node/host_phase_client.py',
    'mop/node/host_phases.py', 'mop/node/host_phase_manifest.py',
    'mop/node/host_phase_source.py', 'mop/node/host_inputs.py', 'mop/node/host_input_files.py',
    'mop/common/credentials.py', 'mop/common/soul.py', 'mop/common/busnames.py',
    'mop/common/paths.py', 'mop/common/bootstrap_envelope.py', 'mop/common/installed_content.py',
    'mop/common/initial_transfer.py')
from mop.node.host_body import BODY_SOURCES
SOURCES = tuple(dict.fromkeys((*SOURCES, *BODY_SOURCES)))

DOCKERFILE = '''FROM python:3.12-slim-trixie
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus passwd git openssh-server openssl iproute2 postgresql-17 > /dev/null && rm -rf /var/lib/apt/lists/*
RUN /usr/local/bin/python3.12 -m pip install --no-cache-dir --no-compile --target /opt/runtime/python ansible-core==2.19.3 PyYAML==6.0.3 psycopg[binary]==3.2.12 aiohttp==3.12.15
RUN groupadd -g 21071 fixture-node && useradd -u 21071 -g 21071 -M -s /usr/sbin/nologin fixture-node && groupadd -g 21072 fixture-body && useradd -u 21072 -g 21072 -M -s /usr/sbin/nologin fixture-body && groupadd -g 21073 fixture-git && useradd -u 21073 -g 21073 -M -p x -s /bin/sh fixture-git
COPY package/ /opt/fixture/
COPY entry.py /opt/hostscope-entry.py
COPY phase-acceptance.py /opt/hostphase-acceptance.py
RUN ln -sf /usr/local/bin/python3.12 /usr/bin/python3 && mkdir -p /opt/runtime/empty /opt/runtime/plugins && printf '[defaults]\\ninventory = localhost,\\nretry_files_enabled = False\\nhost_key_checking = True\\nlocal_tmp = /tmp/ansible-local\\n[privilege_escalation]\\nbecome = False\\n' > /opt/runtime/ansible.cfg && chmod -R go-w /opt/fixture /opt/runtime && chmod 755 /opt/fixture/bin/mop /opt/fixture/mop/node/host_body.py
ENV container=docker
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/local/bin/python3.12", "-IB", "/opt/hostscope-entry.py"]
'''


def snapshot_sources(source, context):
    source, context = Path(source), Path(context)
    scope_snapshot(source, context)
    for name in SOURCES:
        target = context/'package'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source/name, target)
    shutil.copyfile(source/'tests/_host_phase_acceptance.py', context/'phase-acceptance.py')
    (context/'Dockerfile').write_text(DOCKERFILE)


async def run_probe(source):
    return await owned_probe(source, snapshotter=snapshot_sources, acceptance='/opt/hostphase-acceptance.py')
