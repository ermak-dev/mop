"""Изолированная actual normal-native fixture; без pool и host credentials/config."""
from pathlib import Path
import shutil

from mop.node.host_phase_probe import DOCKERFILE, snapshot_sources as phase_snapshot
from mop.node.host_scope_probe import run_probe as owned_probe

def snapshot_sources(source, context, *, native):
    source, context = Path(source), Path(context)
    phase_snapshot(source, context)
    # Только product Python/CLI; ни .env, git metadata, config или HOME.
    for filename in (source/'mop').rglob('*.py'):
        relative = filename.relative_to(source)
        target = context/'package'/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(filename, target)
    shutil.copyfile(source/'tests/_host_native_acceptance.py', context/'native-acceptance.py')
    shutil.copyfile(native, context/'native-2.1.302')
    recipe = DOCKERFILE.replace('postgresql-17 >', 'postgresql-17 tmux >')
    # MCP — уже declared product dependency; build только owned disposable image.
    recipe = recipe.replace('aiohttp==3.12.15', 'aiohttp==3.12.15 mcp==1.26.0')
    recipe = recipe.replace('ENV container=docker',
        'COPY native-acceptance.py /opt/hostnative-acceptance.py\n'
        'COPY native-2.1.302 /opt/runtime/native\n'
        'RUN chmod 755 /opt/runtime/native /opt/fixture/mop/session.py && /opt/runtime/native --version | grep -Fx "2.1.302 (Claude Code)"\n'
        'ENV container=docker')
    (context/'Dockerfile').write_text(recipe)


async def run_probe(source, *, native):
    def snapshotter(source, context):
        snapshot_sources(source, context, native=Path(native))
    return await owned_probe(source, snapshotter=snapshotter, acceptance='/opt/hostnative-acceptance.py')
