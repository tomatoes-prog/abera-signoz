"""Resume a reviewed controller replacement without discarding its prior source."""
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from .host import RuntimeFailure
from .maintenance import command
from .model import read_json, write_json


def inspect(name):
    result = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=30)
    if result.returncode:
        if 'No such object' in result.stderr or 'No such container' in result.stderr:
            return None
        raise RuntimeFailure('Cannot inventory the controller; replacement stopped')
    return json.loads(result.stdout)[0]


def replace(image, token, *, root=Path('/opt/abera')):
    if not re.fullmatch(r'[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}', image) or not re.fullmatch(r'[a-f0-9]{32}', token):
        raise RuntimeFailure('Invalid controller update identity')
    path = root / 'maintenance' / (token + '-controller.json')
    progress = read_json(path) if path.exists() else None
    current = inspect('abera-controller')
    if current and current['Config']['Image'] == image:
        if progress and progress['image'] != image:
            raise RuntimeFailure('Controller replacement target changed')
        command(['docker', 'start', 'abera-controller'])
        return
    if progress:
        if progress['image'] != image:
            raise RuntimeFailure('Controller replacement target changed')
        old = progress['before']
    else:
        if not current:
            raise RuntimeFailure('Controller is missing without a reviewed checkpoint')
        old = current
        progress = {'image': image, 'before': old, 'stage': 'reviewed'}
        write_json(path, progress)
    binds = old['HostConfig']['Binds']
    if set(binds) != {'/var/run/docker.sock:/var/run/docker.sock', '/opt/abera:/opt/abera'}:
        raise RuntimeFailure('Unknown controller mounts')
    prior = 'abera-controller-before-' + token[:12]
    staged = root / ('release-' + token)
    if progress['stage'] == 'reviewed':
        staged.mkdir(exist_ok=True)
        extract = 'abera-controller-source-' + token[:12]
        source = inspect(extract)
        if source and source['Config']['Image'] != image:
            raise RuntimeFailure('Unexpected source container')
        if not source:
            command(['docker', 'create', '--name', extract, image])
        command(['docker', 'cp', extract + ':/opt/abera/release/.', str(staged)])
        command(['docker', 'rm', extract])
        progress['stage'] = 'prepared'
        write_json(path, progress)
    if current:
        if current['Id'] != old['Id'] or inspect(prior):
            raise RuntimeFailure('Unexpected controller identity; replacement stopped')
        time.sleep(2)  # old daemon persists the queued hook before it stops
        command(['docker', 'stop', '-t', '30', 'abera-controller'])
        command(['docker', 'rename', 'abera-controller', prior])
    preserved = inspect(prior)
    if not preserved or preserved['Id'] != old['Id']:
        raise RuntimeFailure('Previous controller identity was not preserved')
    previous_source = root / ('release-before-' + token)
    if not previous_source.exists():
        if not (root / 'release').is_dir():
            raise RuntimeFailure('Previous controller source is missing')
        (root / 'release').rename(previous_source)
    if staged.exists():
        if (root / 'release').exists():
            raise RuntimeFailure('Unexpected source directory during replacement')
        staged.rename(root / 'release')
    elif not (root / 'release').is_dir():
        raise RuntimeFailure('Prepared controller source is missing')
    args = ['docker', 'create', '--name', 'abera-controller', '--restart', 'unless-stopped', '--network', 'host',
            '--memory', str(old['HostConfig']['Memory']), '--env-file', str(root/'controller.env')]
    for bind in binds: args += ['-v', bind]
    log = old['HostConfig']['LogConfig']
    args += ['--log-driver', log['Type']]
    for key, value in log.get('Config', {}).items(): args += ['--log-opt', key + '=' + value]
    command(args + [image])
    command(['docker', 'start', 'abera-controller'])
    progress['stage'] = 'started'
    write_json(path, progress)


if __name__ == '__main__':
    replace(sys.argv[1], sys.argv[2])
