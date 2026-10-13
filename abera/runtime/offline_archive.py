"""Bounded archive while OFF: keeper/ClickHouse only, never tenant applications.

Called by the immutable SSM operator, under the same host flock as runtime
power. It consumes one exact journaled job; it does not start the controller.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

from .host import RuntimeFailure
from .model import read_json, write_json
from .daemon import Controller, plain, dynamo


def digest(value):
    return hashlib.sha256(json.dumps(plain(value), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class OfflineArchive:
    def __init__(self, power, session):
        self.power, self.session = power, session
        self.host, self.core, self.jobs, self.env = power.host, power.core, power.jobs, power.env

    def scope(self, owner, members, sid, *, stopping=False):
        p = self.power
        group = self.env.get('HOST_GROUP_ID')
        if (self.env.get('ENVIRONMENT') != 'dev' or not re.fullmatch('[a-f0-9]{20}', group or '')
                or not re.fullmatch('[A-Za-z0-9:_-]{1,128}', owner or '') or sid not in members
                or len(set(members)) != len(members) or not 1 <= len(members) <= 4
                or self.host.state.get('hostGroupId') != group or self.host.state.get('local') is not False
                or self.host.state.get('project') != 'abera-signoz-' + group):
            raise RuntimeFailure('Offline archive scope is not the owned DEV host')
        sub = plain(self.core.get_item(Key={'pk': 'SUB#' + sid, 'sk': 'META'}, ConsistentRead=True).get('Item', {}))
        op = plain(self.core.get_item(Key={'pk': 'SUB#' + sid, 'sk': 'OP#' + owner}, ConsistentRead=True).get('Item', {}))
        proof = op.get('offlineRuntime', {})
        unit = proof.get('unit', {})
        lock = plain(self.core.get_item(Key={'pk': 'CAPACITY#abera-signoz#MAINTENANCE', 'sk': 'LOCK'}, ConsistentRead=True).get('Item', {}))
        if (sub.get('activeOperationId') != owner or sub.get('productId') != 'abera-signoz'
                or op.get('operationId') != owner or op.get('type') != 'ARCHIVE'
                or proof.get('version') != 1 or proof.get('operation') != 'ARCHIVE'
                or unit.get('groupId') != group or unit.get('instanceId') != self.env.get('HOST_INSTANCE_ID')
                or unit.get('members') != sorted(members) or not proof.get('runtimeOwner')
                or proof.get('shutdownCheckpoint', {}).get('runtimeContainersStopped') is not True
                or lock.get('operationId') != owner or lock.get('unit') != 'offline:archive:abera-signoz'
                or lock.get('runtimeOwner') != proof['runtimeOwner'] or lock.get('offlineProofDigest') != digest(proof)
                or lock.get('deadlineAt') != proof.get('deadlineAt')
                or lock.get('offlinePhase') not in ({'FORWARD', 'STOPPING', 'CLEANUP', 'CONTAINMENT'} if stopping else {'FORWARD'})):
            raise RuntimeFailure('Offline archive lost its exact core operation or borrowed runtime fence')
        deadline = datetime.fromisoformat(proof['deadlineAt'].replace('Z', '+00:00')).timestamp()
        if p.clock() >= deadline + (600 if stopping else 0):
            raise RuntimeFailure('Offline archive fixed budget expired')
        tenants = {t['subscriptionId']: t for t in self.host.state.get('tenants', [])}
        if (len(tenants) != len(self.host.state.get('tenants', []))
                or set(tenants) not in (set(members), set(members) - {sid})):
            raise RuntimeFailure('Offline archive neighbor membership changed')
        for member, tenant in tenants.items():
            binding = unit.get('offlineArchive', {}).get('bindings', {}).get(member, {})
            if (tenant.get('customerId') != binding.get('customerId') or tenant.get('slot') != binding.get('slot')
                    or tenant.get('allocationOperation') != binding.get('allocationOperationId')):
                raise RuntimeFailure('Offline archive tenant allocation changed')
        return sub, op, proof

    def capture(self, owner, members, sid, *, stopping=False):
        sub, op, proof = self.scope(owner, members, sid, stopping=stopping)
        path = self.host.root / ('offline-archive-' + owner + '.json')
        receipt = self.power._document(self.host.root / 'runtime-power.json')
        pause = self.power._document(self.host.root / 'runtime-pause.json')
        if (receipt.get('owner') != proof['runtimeOwner'] or pause.get('owner') != proof['runtimeOwner']
                or receipt.get('groupId') != self.env['HOST_GROUP_ID']):
            raise RuntimeFailure('Offline archive requires the original retained OFF receipts')
        if path.exists():
            saved = self.power._document(path)
            if saved.get('proofDigest') != digest(proof) or saved.get('subscriptionId') != sid:
                raise RuntimeFailure('Another offline operation owns the host evidence')
        else:
            if receipt.get('members') != sorted(members) or pause.get('members') != sorted(members):
                raise RuntimeFailure('Offline archive cannot adopt a changed runtime member set')
            if sid not in {t['subscriptionId'] for t in self.host.state['tenants']}:
                raise RuntimeFailure('The archive source disappeared before the maintenance capture')
            inventory = self.power._owned_inventory()
            if any(c['running'] for c in inventory):
                raise RuntimeFailure('Every application and controller must be OFF before maintenance')
            known = {c['id']: c for c in inventory}
            if any(c.get('id') not in known or any(c.get(key) != known[c['id']].get(key)
                    for key in ('kind', 'service', 'image', 'project', 'groupId', 'name')) for c in receipt.get('containers', [])):
                raise RuntimeFailure('A native container changed after the original OFF receipt')
            saved = {'version': 1, 'proofDigest': digest(proof), 'subscriptionId': sid,
                'tenants': [{'subscriptionId': t['subscriptionId'], 'slot': t['slot'], 'digest': digest(t)}
                            for t in self.host.state['tenants']], 'power': receipt, 'pause': pause,
                'inventory': [{k: c[k] for k in ('id', 'kind', 'service', 'image', 'project', 'groupId', 'name') if k in c}
                              for c in inventory], 'phase': 'CAPTURED'}
            write_json(path, saved)
        current = {t['subscriptionId']: t for t in self.host.state['tenants']}
        before = {t['subscriptionId']: t for t in saved['tenants']}
        if any(member not in current or digest(current[member]) != tenant['digest'] for member, tenant in before.items() if member != sid):
            raise RuntimeFailure('Offline archive changed a neighboring tenant')
        inventory = self.power._owned_inventory()
        source = before[sid]
        removable = {f'{kind}-{source["slot"]}' for kind in ('app', 'collector', 'gateway', 'migrate')} if sub.get('archiveDeletionCommittedAt') else set()
        expected = {c['id']: c for c in saved['inventory']}
        mandatory = {c['id'] for c in saved['inventory'] if c.get('service') not in removable}
        if (not mandatory <= {c['id'] for c in inventory} <= set(expected)
                or any(any(c.get(k) != expected[c['id']].get(k) for k in ('kind', 'service', 'image', 'project', 'groupId', 'name')) for c in inventory)
                or any(c['running'] and c.get('service') not in {'clickhouse', 'keeper'} for c in inventory)):
            raise RuntimeFailure('Offline archive cannot adopt another container or wake an application')
        return sub, op, proof, saved, inventory, path

    def start(self, owner, members, sid):
        _, _, _, saved, inventory, path = self.capture(owner, members, sid)
        for service in ('keeper', 'clickhouse'):
            selected = [c for c in inventory if c.get('service') == service]
            if len(selected) != 1:
                raise RuntimeFailure('Offline archive storage container is missing')
            self.scope(owner, members, sid)
            if not selected[0]['running'] and not self.power.docker.start_wait([selected[0]['id']], timeout=90):
                return False
        _, _, _, _, inventory, _ = self.capture(owner, members, sid)
        ready = all(c['running'] and c.get('healthy') is True for c in inventory if c.get('service') in {'keeper', 'clickhouse'})
        if ready:
            write_json(path, {**saved, 'phase': 'READY'})
        return ready

    def job(self, owner, members, sid, hook):
        sub, op, proof, saved, _, _ = self.capture(owner, members, sid, stopping=hook == 'compensate')
        key = 'JOB#' + hashlib.sha256(f'{sid}:{owner}:{hook}'.encode()).hexdigest()
        row = plain(self.jobs.get_item(Key={'pk': key}, ConsistentRead=True).get('Item', {}))
        request = row.get('request', {})
        if (row.get('groupId') != self.env['HOST_GROUP_ID'] or row.get('status') not in {'PENDING', 'SUCCEEDED', 'FAILED'}
                or request.get('hook') != hook or request.get('operation', {}).get('id') != owner
                or request.get('operation', {}).get('type', '').upper() != 'ARCHIVE'
                or request.get('subscription', {}).get('id') != sid
                or request.get('subscription', {}).get('customerId') != sub.get('customerId')
                or request.get('context', {}).get('offlineRuntime') != proof):
            raise RuntimeFailure('Offline archive requires the exact persistent lifecycle job')
        if row['status'] in {'SUCCEEDED', 'FAILED'}:
            return True
        from . import lifecycle
        worker = object.__new__(Controller)
        worker.host, worker.env, worker.jobs = self.host, self.env, self.jobs
        worker.s3 = self.session.client('s3')
        result = {'status': 'DONE'}
        # No Controller.execute/fence/heartbeat/reconcile: each of those belongs
        # to a running runtime, while this operation borrows the OFF fence.
        if hook == 'prepare':
            if not self.start(owner, members, sid):
                return False
            result['metadata'] = {'archiveBackup': worker.backup(request, final=True)}
        elif hook == 'finalize':
            if not sub.get('archiveDeletionCommittedAt') or not request.get('context', {}).get('archiveBackup'):
                raise RuntimeFailure('Offline archive cannot remove source before core commits the verified archive')
            self.scope(owner, members, sid)
            worker.archive_authority(request, 'CORE_ARCHIVE')
            lifecycle.remove(self.host, sid)
            result['metadata'] = {'tenantRemoved': True}
        elif hook == 'compensate':
            # Never call set_state(ACTIVE) or restart anything during failure.
            if not sub.get('archiveDeletionCommittedAt'):
                worker.archive_authority(request, 'OPERATIONAL')
        else:
            raise RuntimeFailure('Unsupported offline lifecycle hook')
        self.capture(owner, members, sid, stopping=hook == 'compensate')
        self.jobs.update_item(Key={'pk': key}, UpdateExpression='SET #s=:done, #r=:response REMOVE #q',
            ConditionExpression='#s=:pending AND #request=:request AND groupId=:gid',
            ExpressionAttributeNames={'#s': 'status', '#r': 'response', '#q': 'queue', '#request': 'request'},
            ExpressionAttributeValues=dynamo({':done': 'SUCCEEDED', ':response': result, ':pending': 'PENDING', ':request': request, ':gid': self.env['HOST_GROUP_ID']}))
        return True

    def stop(self, owner, members, sid):
        _, _, _, saved, inventory, path = self.capture(owner, members, sid, stopping=True)
        for service in ('clickhouse', 'keeper'):
            selected = [c for c in inventory if c.get('service') == service and c['running']]
            self.scope(owner, members, sid, stopping=True)
            if selected and not self.power.docker.stop_wait([c['id'] for c in selected], timeout=90):
                return False
        _, _, _, _, inventory, _ = self.capture(owner, members, sid, stopping=True)
        if any(c['running'] for c in inventory):
            return False
        remaining = sorted(t['subscriptionId'] for t in self.host.state['tenants'])
        receipt, pause = deepcopy(saved['power']), deepcopy(saved['pause'])
        receipt['members'] = pause['members'] = remaining
        native = {c['id'] for c in inventory}
        receipt['containers'] = [c for c in receipt['containers'] if c['id'] in native]
        self.scope(owner, members, sid, stopping=True)
        write_json(self.host.root / 'runtime-power.json', receipt)
        write_json(self.host.root / 'runtime-pause.json', pause)
        write_json(path, {**saved, 'phase': 'OFF', 'remaining': remaining})
        return True
