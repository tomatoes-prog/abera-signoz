from copy import deepcopy
import hashlib

import boto3
from moto import mock_aws
import pytest

from abera.runtime.host import RuntimeFailure
from test_runtime_power import fixture


@pytest.fixture
def f(tmp_path):
    with mock_aws():
        f = fixture(tmp_path)
        data = boto3.resource('dynamodb', region_name='us-east-2')
        table = data.create_table(TableName='jobs', KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'pk', 'AttributeType': 'S'}], BillingMode='PAY_PER_REQUEST')
        f.power.jobs = table
        assigned = {'pk': 'SUB#tenant-one', 'subscriptionId': 'tenant-one', 'customerId': 'customer-one',
                    'groupId': f.group, 'slot': 1, 'allocationOperationId': 'paid-create'}
        f.host.state['tenants'][0].update(customerId='customer-one', slot=1, allocationOperation='paid-create')
        request = {'protocolVersion': 4, 'environment': 'dev', 'hook': 'prepare',
            'subscription': {'id': 'tenant-one', 'customerId': 'customer-one', 'productId': 'abera-signoz'},
            'operation': {'id': 'backup-one', 'type': 'BACKUP'}, 'context': {'sharedHostAllocation': {
                'groupId': f.group, 'slot': 1, 'customerId': 'customer-one', 'allocationOperationId': 'paid-create'}}}
        pk = 'JOB#' + hashlib.sha256(b'tenant-one:backup-one:prepare').hexdigest()
        job = {'pk': pk, 'groupId': f.group, 'status': 'RUNNING', 'queue': 'READY#' + f.group, 'request': request}
        table.put_item(Item=assigned)
        table.put_item(Item=job)
        f.table, f.assigned, f.job = table, assigned, job
        f.power.request_pause('runtime-one', f.members)
        yield f


def test_one_hour_cancellation_is_recorded_only_after_native_writers_exit(f):
    original = deepcopy(f.host.state)
    f.clock[0] = 3699
    assert f.power.stop('runtime-one', f.members) is False
    assert f.actions == []
    f.clock[0] = 3700
    change = f.docker.stop_wait.side_effect
    f.docker.stop_wait.side_effect = lambda ids, **kw: False
    assert f.power.stop('runtime-one', f.members) is False
    assert f.table.get_item(Key={'pk': f.job['pk']})['Item']['status'] == 'RUNNING'
    f.docker.stop_wait.side_effect = change
    assert f.power.stop('runtime-one', f.members) is True
    result = f.table.get_item(Key={'pk': f.job['pk']})['Item']
    assert result['status'] == 'FAILED' and 'queue' not in result
    assert result['response']['error']['code'] == 'RUNTIME_DRAIN_DEADLINE'
    assert result['runtimeCancellationOwner'] == 'runtime-one'
    assert f.host.state == original
    assert f.table.get_item(Key={'pk': f.assigned['pk']})['Item'] == f.assigned
    assert f.power.resume('runtime-one', f.members) is True
    assert f.table.get_item(Key={'pk': f.job['pk']})['Item']['status'] == 'FAILED'


@pytest.mark.parametrize('drift', ['customer', 'slot', 'request', 'identity'])
def test_unknown_job_owner_prevents_all_container_cancellation(f, drift):
    f.clock[0] = 3700
    job = deepcopy(f.job)
    if drift == 'customer':
        job['request']['subscription']['customerId'] = 'foreign'
    elif drift == 'slot':
        job['request']['context']['sharedHostAllocation']['slot'] = 2
    elif drift == 'request':
        job['request'] = {'action': 'ASSERT_EMPTY_HOST'}
    else:
        f.table.delete_item(Key={'pk': job['pk']})
        job['pk'] = 'JOB#' + 'b' * 64
    f.table.put_item(Item=job)
    with pytest.raises(RuntimeFailure, match='ownership'):
        f.power.stop('runtime-one', f.members)
    assert f.actions == []
    assert f.table.get_item(Key={'pk': job['pk']})['Item']['status'] == 'RUNNING'


def test_completion_before_writer_exit_is_not_overwritten_as_cancelled(f):
    f.clock[0] = 3700
    change = f.docker.stop_wait.side_effect
    def stop(ids, **kw):
        complete = change(ids, **kw)
        if '2' * 64 in ids:
            f.table.put_item(Item={**f.job, 'status': 'SUCCEEDED', 'response': {'status': 'DONE'}})
        return complete
    f.docker.stop_wait.side_effect = stop
    assert f.power.stop('runtime-one', f.members) is True
    record = f.table.get_item(Key={'pk': f.job['pk']})['Item']
    assert record['status'] == 'SUCCEEDED' and record['response'] == {'status': 'DONE'}
    assert 'runtimeCancellationOwner' not in record
