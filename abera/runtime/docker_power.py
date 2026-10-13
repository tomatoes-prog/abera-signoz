"""Native Docker transport with bounded waits and no shell interpolation."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import subprocess
import time

from .host import RuntimeFailure


class DockerPower:
    def __init__(self, *, helper_id=None, helper_image=None, helper_group=None, run=subprocess.run, clock=time.monotonic):
        if helper_id is not None and not re.fullmatch('[a-f0-9]{12,64}', helper_id):
            raise RuntimeFailure('Operator helper requires its native container identity')
        self.helper_id, self.run, self.clock = helper_id, run, clock
        self.helper_image, self.helper_group = helper_image, helper_group

    def command(self, arguments, timeout=30):
        try:
            result = self.run(['docker', *arguments], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeFailure('Native Docker power command failed or timed out') from None
        if result.returncode:
            raise RuntimeFailure('Native Docker power command was rejected')
        return result.stdout

    def inventory(self):
        ids = self.command(['ps', '--all', '--quiet', '--no-trunc']).split()
        if not ids:
            return []
        if any(not re.fullmatch('[a-f0-9]{64}', cid) for cid in ids) or len(set(ids)) != len(ids):
            raise RuntimeFailure('Native Docker identities are incomplete')
        containers = json.loads(self.command(['inspect', *ids]))
        if {c.get('Id') for c in containers} != set(ids) or len(containers) != len(ids):
            raise RuntimeFailure('Native Docker inspection omitted an identity')
        result, helpers = [], 0
        for container in containers:
            cid = container['Id']
            config = container['Config']
            labels = config.get('Labels') or {}
            env = dict(e.split('=', 1) for e in config.get('Env', []) if '=' in e)
            name = container.get('Name', '').lstrip('/')
            if self.helper_id and cid.startswith(self.helper_id):
                if (helpers or not name.startswith('abera-runtime-power-')
                        or not self.helper_image or config.get('Image') != self.helper_image
                        or not self.helper_group or env.get('HOST_GROUP_ID') != self.helper_group
                        or env.get('ENVIRONMENT') != 'dev'):
                    raise RuntimeFailure('Operator helper native ownership is incomplete')
                helpers += 1
                continue
            kind = 'controller' if name == 'abera-controller' else 'service'
            result.append({'id': cid, 'kind': kind, 'name': name, 'image': config['Image'],
                'groupId': env.get('HOST_GROUP_ID'), 'project': labels.get('com.docker.compose.project'),
                'service': labels.get('com.docker.compose.service'), 'running': container['State']['Running'],
                'healthy': container['State'].get('Health', {}).get('Status', 'healthy') == 'healthy'})
        if self.helper_id and helpers != 1:
            raise RuntimeFailure('Operator helper was absent from the native Docker inventory')
        return result

    def _wait(self, ids, *, start, timeout):
        if any(not re.fullmatch('[a-f0-9]{64}', cid) for cid in ids):
            raise RuntimeFailure('Power requires exact native container IDs')
        deadline = self.clock() + timeout
        def change(cid):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise RuntimeFailure('Native container power deadline expired')
            args = ['start', cid] if start else ['stop', '--time', str(min(120, max(1, int(remaining)))), cid]
            self.command(args, timeout=remaining)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(change, ids))
        native = json.loads(self.command(['inspect', *ids], timeout=max(1, deadline-self.clock())))
        observed = {c['Id']: c for c in native}
        if set(observed) != set(ids) or len(native) != len(ids):
            raise RuntimeFailure('Native container exit or start evidence is incomplete')
        if not start:
            return all(c['State']['Running'] is False for c in observed.values())
        return all(c['State']['Running'] is True and c['State'].get('Health', {}).get('Status', 'healthy') == 'healthy'
                   for c in observed.values())

    def stop_wait(self, ids, timeout):
        return self._wait(ids, start=False, timeout=timeout)

    def start_wait(self, ids, timeout):
        return self._wait(ids, start=True, timeout=timeout)
