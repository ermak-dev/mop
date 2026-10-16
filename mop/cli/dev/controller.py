"""mop dev controller [--postgres 16|17] [--check controller|hermetic|preparation]: isolated PostgreSQL checks

Runs the selected check with the declared server extra and matching PostgreSQL
client tools in fresh Docker containers, using the same setup as CI. Requires
an existing Docker installation and a mop Git working copy; installs no host
packages and uses no pool. The snapshot contains tracked and nonignored source,
not installation inputs or .git. No host ports, installation credentials or
Docker socket enter test containers. Cleanup failures are reported explicitly.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid


def _source_snapshot(top, destination):
    try:
        files = subprocess.run(['git', '-C', str(top), 'ls-files', '--cached', '--others',
                                '--exclude-standard', '-z'], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise RuntimeError('isolated controller checks require a readable mop Git working copy') from None
    destination.mkdir()
    excluded = {'.git', '.claude', '__pycache__', 'node_modules', 'build'}
    for name in set(files.split(b'\0')) - {b''}:
        relative = Path(os.fsdecode(name))
        if relative.is_absolute() or '..' in relative.parts:
            raise RuntimeError('source snapshot contains an invalid relative path')
        if set(relative.parts) & excluded or relative.name.endswith('.egg-info'):
            continue
        if len(relative.parts) == 1 and relative.name in {'.env', 'inventory.yaml', 'sandbox.yaml'}:
            continue
        source = top / relative
        if not source.exists() and not source.is_symlink():
            continue
        if source.is_dir() and not source.is_symlink():
            raise RuntimeError('source snapshot requires explicit files, not unresolved submodules')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', choices=('16', '17'), default='17')
    parser.add_argument('--check', choices=('controller', 'hermetic', 'preparation'), default='controller')
    args = parser.parse_args(argv)
    docker = shutil.which('docker')
    if not docker:
        raise RuntimeError('isolated controller checks require an existing Docker installation')
    from .test import root
    import yaml
    top = Path(root())
    with (top / '.gitlab-ci.yml').open() as stream:
        ci = yaml.safe_load(stream)
    job = ci['test:py3.12' if args.postgres == '16' else 'controller:pg17']['variables']
    setup = ci['.tests']['variables']['SETUP']['value']
    prefix = 'mop-controller-test-' + uuid.uuid4().hex[:16]
    database, worker = prefix + '-db', prefix + '-runner'
    cleanup_errors = []

    def call(words, *, check=True, **kwargs):
        return subprocess.run([docker, *words], check=check, **kwargs)

    def cleanup(words, label):
        try:
            result = call(words, check=False, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            cleanup_errors.append(label)
        else:
            if result.returncode:
                cleanup_errors.append(label)

    def remove_owned(kind, name):
        listing = ['container', 'ls', '--all'] if kind == 'container' else ['network', 'ls']
        try:
            result = call([*listing, '--filter', 'name=' + name, '--format', '{{.Names}}' if
                           kind == 'container' else '{{.Name}}'], check=False,
                          capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            cleanup_errors.append(kind + ' lookup')
            return
        if result.returncode:
            cleanup_errors.append(kind + ' lookup')
        elif name in result.stdout.splitlines():
            words = ['rm', '-f', '-v', name] if kind == 'container' else ['network', 'rm', name]
            cleanup(words, kind + ' removal')

    result, failure, interrupted = None, None, False
    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        snapshot = Path(directory) / 'source'
        _source_snapshot(top, snapshot)
        try:
            call(['network', 'create', prefix], stdout=subprocess.DEVNULL)
            call(['run', '--rm', '-d', '--name', database, '--network', prefix,
                  '--network-alias', 'postgres', '-e', 'POSTGRES_DB=mop_ci',
                  '-e', 'POSTGRES_USER=mop_ci', '-e', 'POSTGRES_PASSWORD=isolated-test-only',
                  job['POSTGRES_IMAGE']], stdout=subprocess.DEVNULL)
            # Test script, не subprocess другого командлета.
            result = call(['run', '--rm', '--name', worker, '--network', prefix,
                           '-v', f'{snapshot}:/io', '-w', '/io',
                           '-v', 'mop-controller-fixture-pip:/root/.cache/pip',
                           '-e', 'MOP_TESTS_STRICT=1', '-e', 'MOP_TEST_POSTGRES_HOST=postgres',
                           '-e', f'POSTGRES_MAJOR={args.postgres}',
                           '-e', f'TESTS=python3 tests/{args.check}.py',
                           job['PYTHON_IMAGE'], 'bash', '-euc', setup], check=False).returncode
        except OSError as error:
            failure = f'isolated controller fixture unavailable: {type(error).__name__}'
        except subprocess.CalledProcessError as error:
            failure = f'isolated controller fixture step failed with exit {error.returncode}'
        except KeyboardInterrupt:
            interrupted = True
        finally:
            # --rm мог уже убрать завершённый runner: отсутствие — успех,
            # а недоступный daemon нельзя принять за отсутствие ресурса.
            remove_owned('container', worker)
            remove_owned('container', database)
            remove_owned('network', prefix)
            cleanup(['run', '--rm', '-v', f'{snapshot}:/io', 'alpine',
                     'chown', '-R', f'{os.getuid()}:{os.getgid()}', '/io'], 'snapshot ownership')
    if cleanup_errors:
        primary = failure or ('fixture interrupted' if interrupted else f'fixture check exit {result}')
        raise RuntimeError(primary + '; cleanup incomplete: ' + ', '.join(cleanup_errors))
    if interrupted:
        raise KeyboardInterrupt
    if failure:
        raise RuntimeError(failure)
    return result
