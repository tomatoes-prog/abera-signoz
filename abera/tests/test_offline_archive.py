from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from abera.runtime.host import RuntimeFailure
from abera.runtime.model import write_json, read_json
from abera.runtime.offline_archive import OfflineArchive, digest
from abera.tests.test_runtime_power import fixture


def world(tmp_path):
    f = fixture(tmp_path)
    for index, tenant in enumerate(f.host.state['tenants'], 1):
        tenant.update(slot=index, customerId='customer-' + str(index), allocationOperation='allocation-' + str(index), generation='generation-' + str(index))
    for container in f.containers:
        if container.get('service') in {'app-one', 'app-two'}:
            container['service'] = {'app-one': 'app-1', 'app-two': 'app-2'}[container['service']]
    compose = {'services': {c['service']: {'image': c['image']} for c in f.containers if c['kind'] == 'service'}}
    write_json(tmp_path / 'compose.json', compose)
    f.power.request_pause('runtime-one', f.members)
    f.clock[0] = 3700
    assert f.power.stop('runtime-one', f.members)
    f.actions.clear()
    bindings = {t['subscriptionId']: {'pk': 'SUB#' + t['subscriptionId'], 'subscriptionId': t['subscriptionId'],
        'slot': t['slot'], 'customerId': t['customerId'], 'allocationOperationId': t['allocationOperation']} for t in f.host.state['tenants']}
    deadline = datetime.fromtimestamp(7300, timezone.utc).isoformat()
    proof = {'version': 1, 'operation': 'ARCHIVE', 'runtimeOwner': 'runtime-one', 'deadlineAt': deadline,
        'unit': {'groupId': f.group, 'instanceId': 'i-owned', 'members': f.members, 'offlineArchive': {'bindings': bindings}},
        'shutdownCheckpoint': {'runtimeContainersStopped': True}}
    sub = {'activeOperationId': 'archive-one', 'productId': 'abera-signoz', 'customerId': 'customer-1'}
    op = {'operationId': 'archive-one', 'type': 'ARCHIVE', 'offlineRuntime': proof}
    lock = {'operationId': 'archive-one', 'unit': 'offline:archive:abera-signoz', 'runtimeOwner': 'runtime-one',
        'offlineProofDigest': digest(proof), 'deadlineAt': deadline, 'offlinePhase': 'FORWARD'}
    records = {('SUB#tenant-one', 'META'): sub, ('SUB#tenant-one', 'OP#archive-one'): op,
               ('CAPACITY#abera-signoz#MAINTENANCE', 'LOCK'): lock}
    f.core.get_item.side_effect = lambda **kw: {'Item': deepcopy(records.get((kw['Key']['pk'], kw['Key']['sk']), {}))}
    session = Mock()
    archive = OfflineArchive(f.power, session)
    return SimpleNamespace(**locals())


def test_start_and_compensation_never_wake_apps_or_controller(tmp_path):
    w = world(tmp_path)
    before = deepcopy(w.f.host.state)
    assert w.archive.start('archive-one', w.f.members, 'tenant-one')
    assert w.f.actions == [('start', ['2' * 64]), ('start', ['1' * 64])]
    assert all(not c['running'] for c in w.f.containers if c.get('service') not in {'clickhouse', 'keeper'})
    w.lock['offlinePhase'] = 'STOPPING'
    assert w.archive.stop('archive-one', w.f.members, 'tenant-one')
    assert not any(c['running'] for c in w.f.containers)
    assert w.f.host.state == before
    assert read_json(tmp_path / 'runtime-power.json')['members'] == w.f.members


@pytest.mark.parametrize('drift', ['neighbor', 'application', 'generation', 'sealed', 'expired'])
def test_changed_scope_cannot_start_storage(tmp_path, drift):
    w = world(tmp_path)
    if drift == 'neighbor': w.f.host.state['tenants'][1]['customerId'] = 'foreign'
    elif drift == 'application': w.f.containers[-1]['running'] = True
    elif drift == 'generation': w.proof['unit']['instanceId'] = 'replacement'
    elif drift == 'sealed': w.lock['offlinePhase'] = 'STOPPING'
    else: w.f.clock[0] = 7300
    with pytest.raises(RuntimeFailure): w.archive.start('archive-one', w.f.members, 'tenant-one')
    assert w.f.actions == []


def job(w, hook):
    request = {'hook': hook, 'operation': {'id': 'archive-one', 'type': 'ARCHIVE'},
        'subscription': {'id': 'tenant-one', 'customerId': 'customer-1'},
        'context': {'offlineRuntime': w.proof, 'archiveBackup': {'manifestVersionId': 'pinned'}}}
    row = {'groupId': w.f.group, 'status': 'PENDING', 'request': request}
    w.f.jobs.get_item.side_effect = lambda **kw: {'Item': row}
    return row


def test_backup_uses_sealed_subscription_backup_and_existing_journal(tmp_path):
    w = world(tmp_path)
    job(w, 'prepare')
    with patch('abera.runtime.offline_archive.Controller.backup', return_value={'manifestVersionId': 'native-version'}) as backup:
        assert w.archive.job('archive-one', w.f.members, 'tenant-one', 'prepare')
    assert backup.call_args.kwargs == {'final': True}
    written = w.f.jobs.update_item.call_args.kwargs['ExpressionAttributeValues'][':response']
    assert written['metadata']['archiveBackup']['manifestVersionId'] == 'native-version'
    assert all(not c['running'] for c in w.f.containers if c.get('service') not in {'clickhouse', 'keeper'})


def test_finalize_and_stop_rebase_original_minus_source_without_neighbor_wake(tmp_path):
    w = world(tmp_path)
    assert w.archive.start('archive-one', w.f.members, 'tenant-one')
    before = deepcopy(w.f.host.state['tenants'][1])
    w.sub['archiveDeletionCommittedAt'] = 'committed'
    job(w, 'finalize')
    def remove(host, sid):
        host.state['tenants'] = [tenant for tenant in host.state['tenants'] if tenant['subscriptionId'] != sid]
        w.f.containers[:] = [c for c in w.f.containers if c.get('service') != 'app-1']
        compose = read_json(tmp_path / 'compose.json')
        del compose['services']['app-1']
        write_json(tmp_path / 'compose.json', compose)
    with patch('abera.runtime.lifecycle.remove', side_effect=remove):
        assert w.archive.job('archive-one', w.f.members, 'tenant-one', 'finalize')
    w.lock['offlinePhase'] = 'STOPPING'
    assert w.archive.stop('archive-one', w.f.members, 'tenant-one')
    assert w.f.host.state['tenants'] == [before]
    saved = read_json(tmp_path / 'runtime-power.json')
    assert saved['members'] == ['tenant-two']
    assert all(c.get('service') != 'app-1' for c in saved['containers'])
    assert not any(c['running'] for c in w.f.containers)
    assert w.archive.stop('archive-one', w.f.members, 'tenant-one')  # lost response


def test_timeout_does_not_publish_success_and_stop_is_available_after_seal(tmp_path):
    w = world(tmp_path)
    job(w, 'prepare')
    with patch('abera.runtime.offline_archive.Controller.backup', side_effect=TimeoutError('bounded helper')):
        with pytest.raises(TimeoutError): w.archive.job('archive-one', w.f.members, 'tenant-one', 'prepare')
    w.f.jobs.update_item.assert_not_called()
    w.lock['offlinePhase'] = 'STOPPING'
    assert w.archive.stop('archive-one', w.f.members, 'tenant-one')
    assert not any(c['running'] for c in w.f.containers)
