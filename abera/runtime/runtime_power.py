"""Bounded DEV container power; storage and commercial tenant state are retained.

Transport must supply native Docker inventory, stop/wait and start/wait methods.
Only the admitted host's exact declared services and controller are eligible.
"""
from copy import deepcopy
import hashlib
import json
import re

from .host import RuntimeFailure
from .model import read_json, write_json


class RuntimePower:
    def __init__(self, host, docker, core, jobs, env, clock):
        self.host, self.docker, self.core, self.jobs, self.env, self.clock = host, docker, core, jobs, env, clock

    def _scope(self, owner, members):
        state = self.host.state
        group = self.env.get('HOST_GROUP_ID', '')
        if (self.env.get('ENVIRONMENT') != 'dev' or not re.fullmatch('[a-f0-9]{20}', group)
                or state.get('hostGroupId') != group or state.get('local') is not False
                or state.get('project') != 'abera-signoz-' + group
                or not isinstance(owner, str) or not re.fullmatch('[A-Za-z0-9:_-]{1,128}', owner)
                or not isinstance(members, list) or not 1 <= len(members) <= 4
                or any(not isinstance(m, str) or not re.fullmatch('[a-z0-9][a-z0-9-]{1,63}', m) for m in members)
                or len(set(members)) != len(members)
                or set(members) != {t['subscriptionId'] for t in state.get('tenants', [])}):
            raise RuntimeFailure('Power requires the admitted DEV host and every consumer')
        lock = self.core.get_item(Key={'pk': 'CAPACITY#abera-signoz#MAINTENANCE', 'sk': 'LOCK'}, ConsistentRead=True).get('Item', {})
        if lock.get('operationId') != owner or lock.get('unit') != 'runtime:abera-signoz':
            raise RuntimeFailure('Power requires the exact core runtime owner')
        return group

    def _document(self, path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise RuntimeFailure('Runtime power document is not confined')
        return read_json(path)

    def request_pause(self, owner, members):
        group = self._scope(owner, members)
        marker = self.host.root / 'runtime-pause.json'
        if marker.exists():
            intent = self._document(marker)
            if (set(intent) != {'version', 'environment', 'groupId', 'owner', 'members', 'requestedAt', 'deadlineAt'}
                    or intent.get('version') != 1 or intent.get('environment') != 'dev'
                    or type(intent.get('requestedAt')) is not int
                    or not 0 <= intent['requestedAt'] <= int(self.clock())
                    or intent.get('deadlineAt') != intent['requestedAt'] + 3600
                    or intent.get('owner') != owner or intent.get('groupId') != group or intent.get('members') != sorted(members)):
                raise RuntimeFailure('Another pause owns this host')
            return intent
        self._retire_restored_receipt(owner, members, group)
        now = int(self.clock())
        intent = {'version': 1, 'environment': 'dev', 'groupId': group, 'owner': owner,
                  'members': sorted(members), 'requestedAt': now, 'deadlineAt': now + 3600}
        write_json(marker, intent)
        return intent

    def _retire_restored_receipt(self, owner, members, group):
        """A new fenced cycle may replace only a natively restored old cycle.

        Keep the active receipt for resume retries, then retain one completed
        receipt for inspection. Neither commercial state nor storage is changed.
        """
        path = self.host.root / 'runtime-power.json'
        if not path.exists():
            return
        receipt = self._document(path)
        if (receipt.get('version') != 1 or receipt.get('groupId') != group
                or receipt.get('members') != sorted(members)
                or not isinstance(receipt.get('owner'), str)
                or not re.fullmatch('[A-Za-z0-9:_-]{1,128}', receipt['owner'])
                or receipt['owner'] == owner):
            raise RuntimeFailure('A completed power cycle cannot be replayed or reassigned')
        saved = receipt.get('containers')
        if (not isinstance(saved, list) or any(not isinstance(c, dict) or not c.get('id') for c in saved)
                or len({c['id'] for c in saved}) != len(saved)):
            raise RuntimeFailure('Previous power receipt has incomplete identities')
        inventory = self._owned_inventory()
        known = {c['id']: c for c in inventory}
        if ({c['id'] for c in inventory if c['running']} != {c['id'] for c in saved}
                or any(c['id'] not in known or any(c.get(k) != known[c['id']].get(k)
                       for k in ('kind', 'service', 'image', 'project', 'groupId', 'name')) for c in saved)):
            raise RuntimeFailure('Previous cycle must be fully restored before a new shutdown')
        # Persist the evidence before releasing the active receipt. Repeating a
        # new request after interruption validates the same native graph again.
        write_json(self.host.root / 'runtime-power-completed.json', receipt)
        path.unlink()

    def _owned_inventory(self):
        compose = read_json(self.host.root / 'compose.json')
        declared = compose['services']
        state = self.host.state
        inventory = self.docker.inventory()
        identifiers = set()
        for container in inventory:
            cid = container.get('id')
            if (not isinstance(cid, str) or not re.fullmatch('[a-f0-9]{12,64}', cid) or cid in identifiers
                    or type(container.get('running')) is not bool):
                raise RuntimeFailure('Docker inventory has incomplete or duplicate identities')
            identifiers.add(cid)
            if container.get('kind') == 'controller':
                if (container.get('groupId') != state['hostGroupId']
                        or container.get('name') != 'abera-controller'
                        or container.get('image') != self.env['ADMIN_IMAGE']):
                    raise RuntimeFailure('Controller belongs to another physical owner or image')
            elif (container.get('kind') != 'service' or container.get('project') != state['project']
                    or container.get('service') not in declared
                    or container.get('image') != declared[container['service']]['image']):
                raise RuntimeFailure('An unknown container cannot be stopped or adopted')
        return inventory

    def stop(self, owner, members):
        intent = self.request_pause(owner, members)
        now = int(self.clock())
        receipt_path = self.host.root / 'runtime-power.json'
        host_record = self.jobs.get_item(Key={'pk': 'HOST#' + intent['groupId']}, ConsistentRead=True).get('Item', {})
        acknowledged = (host_record.get('runtimePauseOwner') == owner
                        and host_record.get('runtimePauseControllerOwner') == host_record.get('owner')
                        and host_record.get('owner')
                        and host_record.get('runtimePauseMembersDigest') == hashlib.sha256(
                            json.dumps(sorted(members), separators=(',', ':')).encode()).hexdigest()
                        and host_record.get('runtimePauseConfirmedAt', -1) >= intent['requestedAt']
                        and host_record.get('instanceId') == self.env['HOST_INSTANCE_ID']
                        and host_record.get('expiresAt', 0) > now)
        # Once the owned receipt has been captured, stopping the controller
        # deliberately removes its heartbeat. A retry must continue the captured
        # shutdown rather than wait an hour for a heartbeat that cannot return.
        if not acknowledged and now < intent['deadlineAt'] and not receipt_path.exists():
            return False
        from .runtime_jobs import outstanding, cancel
        jobs = outstanding(self, members)
        if jobs and now < intent['deadlineAt']:
            raise RuntimeFailure('A cooperative pause cannot bypass outstanding jobs before the one-hour deadline')
        inventory = self._owned_inventory()
        live = [c for c in inventory if c.get('running') is True]
        if receipt_path.exists():
            receipt = self._document(receipt_path)
            if receipt.get('owner') != owner or receipt.get('members') != sorted(members):
                raise RuntimeFailure('Another power receipt owns this host')
            expected = {c['id']: c for c in receipt['containers']}
            if any(c['id'] not in expected or any(c.get(k) != expected[c['id']].get(k)
                                                for k in ('kind', 'service', 'image')) for c in live):
                raise RuntimeFailure('Live container graph changed after shutdown capture')
        else:
            receipt = {'version': 1, 'owner': owner, 'members': sorted(members), 'groupId': intent['groupId'],
                       'deadlineAt': intent['deadlineAt'], 'containers': [
                           {k: deepcopy(c[k]) for k in ('id', 'kind', 'service', 'image', 'project', 'groupId', 'name') if k in c}
                           for c in live]}
            write_json(receipt_path, receipt)
        if live and now >= intent['deadlineAt'] + 600:
            raise RuntimeFailure('Container cancellation exceeded the bounded DEV deadline')
        # Stop producer/controller, then application services, then databases.
        groups = [[c for c in live if c['kind'] == 'controller'],
                  [c for c in live if c['kind'] == 'service' and c['service'] not in {'clickhouse', 'keeper'}],
                  [c for c in live if c.get('service') == 'clickhouse'],
                  [c for c in live if c.get('service') == 'keeper']]
        for selected in groups:
            timeout = min(120, max(1, intent['deadlineAt'] + 600 - int(self.clock())))
            if selected and not self.docker.stop_wait([c['id'] for c in selected], timeout=timeout):
                return False
        # Native exit proof is required even when Docker accepted the requests.
        if any(c.get('running') is not False for c in self._owned_inventory()):
            return False
        cancel(self, jobs, owner, members)
        return True

    def verify(self, owner, members):
        """Read native container state without starting or recreating anything."""
        group = self._scope(owner, members)
        receipt = self._document(self.host.root / 'runtime-power.json')
        if (receipt.get('owner') != owner or receipt.get('members') != sorted(members)
                or receipt.get('groupId') != group):
            raise RuntimeFailure('Verification requires the retained cycle owner')
        if (self.host.root / 'runtime-pause.json').exists():
            return False
        inventory = self._owned_inventory()
        known = {c['id']: c for c in inventory}
        saved = receipt['containers']
        if any(c['id'] not in known or any(c.get(k) != known[c['id']].get(k)
                   for k in ('kind', 'service', 'image', 'project', 'groupId', 'name')) for c in saved):
            raise RuntimeFailure('Verification cannot adopt changed container identities')
        return ({c['id'] for c in inventory if c['running']} == {c['id'] for c in saved}
                and all(known[c['id']].get('healthy') is True for c in saved))

    def resume(self, owner, members):
        group = self._scope(owner, members)
        receipt = self._document(self.host.root / 'runtime-power.json')
        marker_path = self.host.root / 'runtime-pause.json'
        marker = self._document(marker_path) if marker_path.exists() else None
        if (receipt.get('owner') != owner or receipt.get('members') != sorted(members)
                or receipt.get('groupId') != group or (marker is not None and marker.get('owner') != owner)):
            raise RuntimeFailure('Resume requires the same retained power receipt')
        inventory = self._owned_inventory()
        known = {c['id']: c for c in inventory}
        saved = receipt['containers']
        if any(c['running'] and c['id'] not in {s['id'] for s in saved} for c in inventory):
            raise RuntimeFailure('Resume cannot adopt workloads that were not running before shutdown')
        if any(c['id'] not in known or any(c.get(k) != known[c['id']].get(k)
                                          for k in ('kind', 'service', 'image')) for c in saved):
            raise RuntimeFailure('Restore cannot recreate or adopt changed container identities')
        groups = [[c for c in saved if c.get('service') == 'keeper'],
                  [c for c in saved if c.get('service') == 'clickhouse'],
                  [c for c in saved if c['kind'] == 'service' and c['service'] not in {'clickhouse', 'keeper'}],
                  [c for c in saved if c['kind'] == 'controller']]
        for selected in groups:
            if selected and not self.docker.start_wait([c['id'] for c in selected], timeout=180):
                return False
        observed = self._owned_inventory()
        running = {c['id'] for c in observed if c['running']}
        if running != {c['id'] for c in saved}:
            return False
        # Controller stays paused until the original workloads are observed up.
        marker_path.unlink(missing_ok=True)
        return True


def main():
    """Executed only by the owned operator image through the reviewed transport."""
    import argparse
    import os
    from pathlib import Path
    import time
    import boto3
    from .docker_power import DockerPower
    from .host import Host

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', choices=('pause', 'stop', 'resume', 'verify'), required=True)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--members', required=True, help='Exact JSON array of admitted consumers')
    args = parser.parse_args()
    # Read the existing host configuration without evaluating it as shell code.
    config_path = Path('/opt/abera/controller.env')
    if config_path.is_symlink() or config_path.stat().st_size > 65536:
        raise RuntimeFailure('Host operator configuration is not confined')
    env = dict(line.split('=', 1) for line in config_path.read_text().splitlines() if '=' in line)
    bootstrap = read_json(Path('/opt/abera/bootstrap-images.json'))
    env['ADMIN_IMAGE'] = bootstrap['ADMIN_IMAGE']
    if (env.get('ENVIRONMENT') != 'dev' or env.get('CORE_TABLE') != 'abera-dev-control-plane-v2'
            or env.get('JOB_TABLE') != os.environ.get('JOB_TABLE')
            or env.get('HOST_GROUP_ID') != os.environ.get('HOST_GROUP_ID')):
        raise RuntimeFailure('Operator transport does not match the existing DEV host configuration')
    session = boto3.Session(region_name=env['AWS_REGION'])
    data = session.resource('dynamodb')
    docker = DockerPower(helper_id=os.environ['HOSTNAME'], helper_image=os.environ['RUNTIME_HELPER_IMAGE'],
                         helper_group=env['HOST_GROUP_ID'])
    power = RuntimePower(Host(Path('/opt/abera/state')), docker,
                         data.Table(env['CORE_TABLE']), data.Table(env['JOB_TABLE']), env, time.time)
    members = json.loads(args.members)
    if args.action == 'pause':
        power.request_pause(args.owner, members)
        result = {'pauseRequested': True}
    else:
        result = {'complete': getattr(power, args.action)(args.owner, members)}
    # Never export the environment, host state or container configuration.
    print(json.dumps({'version': 1, 'groupId': env['HOST_GROUP_ID'], 'owner': args.owner, **result}))


if __name__ == '__main__':
    main()
