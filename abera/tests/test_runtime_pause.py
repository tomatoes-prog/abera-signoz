from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from abera.runtime.daemon import Controller
from abera.runtime.host import RuntimeFailure
from abera.runtime.runtime_pause import paused


def fixture(tmp_path):
    controller = Controller.__new__(Controller)
    controller.group_id = 'a' * 20
    controller.owner = 'controller-owner'
    controller.lease = True
    controller.env = {'ENVIRONMENT': 'dev', 'HOST_INSTANCE_ID': 'i-owned'}
    controller.host = SimpleNamespace(root=tmp_path, state={'hostGroupId': controller.group_id,
        'tenants': [{'subscriptionId': 'tenant-one', 'state': 'ACTIVE', 'adminPassword': 'private'}]})
    controller.core = Mock()
    controller.core.get_item.return_value = {'Item': {'operationId': 'runtime-one', 'unit': 'runtime:abera-signoz'}}
    controller.jobs = Mock()
    controller.jobs.scan.return_value = {'Items': []}
    controller.work_once = Mock()
    controller.reconcile = Mock()
    intent = {'version': 1, 'environment': 'dev', 'groupId': controller.group_id, 'owner': 'runtime-one',
              'members': ['tenant-one'], 'requestedAt': 100, 'deadlineAt': 3700}
    marker = tmp_path / 'runtime-pause.json'
    marker.write_text(json.dumps(intent))
    return controller, intent, marker


def test_acknowledgement_stops_work_and_reconciliation_without_changing_tenants(tmp_path):
    controller, _, _ = fixture(tmp_path)
    before = deepcopy(controller.host.state)
    controller.cycle()
    controller.work_once.assert_not_called()
    controller.reconcile.assert_not_called()
    args = controller.jobs.update_item.call_args.kwargs
    assert args['Key'] == {'pk': 'HOST#' + controller.group_id}
    assert args['ExpressionAttributeValues'][':runtime'] == 'runtime-one'
    assert '#owner=:controller' in args['ConditionExpression'] and 'instanceId=:instance' in args['ConditionExpression']
    assert controller.host.state == before and 'private' not in str(args)


def test_busy_job_cannot_be_acknowledged_from_idle_heartbeat(tmp_path):
    controller, _, _ = fixture(tmp_path)
    controller.jobs.scan.return_value = {'Items': [{'pk': 'JOB#backup', 'status': 'RUNNING'}]}
    assert paused(controller) is False
    controller.jobs.update_item.assert_not_called()
    controller.cycle()
    controller.work_once.assert_called_once()
    controller.reconcile.assert_called_once()


def test_pause_requested_during_a_job_is_acknowledged_after_the_job_returns(tmp_path):
    controller, intent, marker = fixture(tmp_path)
    marker.unlink()
    def work():
        controller.jobs.update_item.assert_not_called()
        marker.write_text(json.dumps(intent))
    controller.work_once.side_effect = work
    controller.cycle()
    controller.work_once.assert_called_once()
    controller.reconcile.assert_not_called()
    controller.jobs.update_item.assert_called_once()


def test_busy_job_on_later_page_prevents_pause(tmp_path):
    controller, _, _ = fixture(tmp_path)
    controller.jobs.scan.side_effect = [{'Items': [], 'LastEvaluatedKey': {'pk': 'next'}},
                                      {'Items': [{'pk': 'JOB#backup', 'status': 'PENDING'}]}]
    assert paused(controller) is False
    assert controller.jobs.scan.call_count == 2
    assert controller.jobs.scan.call_args.kwargs['ExclusiveStartKey'] == {'pk': 'next'}
    controller.jobs.update_item.assert_not_called()


@pytest.mark.parametrize('drift', ['prod', 'owner', 'group', 'member', 'lease', 'deadline'])
def test_foreign_or_unfenced_pause_is_rejected(tmp_path, drift):
    controller, intent, marker = fixture(tmp_path)
    if drift == 'prod':
        controller.env['ENVIRONMENT'] = 'prod'
    elif drift == 'owner':
        intent['owner'] = 'foreign-owner'
    elif drift == 'group':
        intent['groupId'] = 'b' * 20
    elif drift == 'member':
        intent['members'] = ['unknown-tenant']
    elif drift == 'lease':
        controller.lease = False
    else:
        intent['deadlineAt'] += 3600
    marker.write_text(json.dumps(intent))
    with pytest.raises(RuntimeFailure):
        paused(controller)
    controller.jobs.update_item.assert_not_called()
