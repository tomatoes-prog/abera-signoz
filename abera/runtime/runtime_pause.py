"""Acknowledge a DEV runtime pause only between host operations.

The driver owns the deadline and eventual forced cancellation. A heartbeat
thread must never call this function: it cannot prove the work thread is idle.
"""
import hashlib
import json
import re
import time

from .host import RuntimeFailure


def paused(controller):
    marker = controller.host.root / 'runtime-pause.json'
    if not marker.exists():
        return False
    if marker.is_symlink() or marker.stat().st_size > 8192:
        raise RuntimeFailure('Runtime pause marker is not a confined regular document')
    try:
        intent = json.loads(marker.read_text())
    except (ValueError, OSError):
        raise RuntimeFailure('Runtime pause marker is invalid') from None
    members = intent.get('members') if isinstance(intent, dict) else None
    state = controller.host.state
    if (controller.env.get('ENVIRONMENT') != 'dev' or not isinstance(intent, dict)
            or set(intent) != {'version', 'environment', 'groupId', 'owner', 'members', 'requestedAt', 'deadlineAt'}
            or type(intent['version']) is not int or intent['version'] != 1 or intent['environment'] != 'dev'
            or intent['groupId'] != controller.group_id or state.get('hostGroupId') != controller.group_id
            or not isinstance(intent['owner'], str) or not re.fullmatch('[A-Za-z0-9:_-]{1,128}', intent['owner'])
            or not isinstance(members, list) or not 1 <= len(members) <= 4
            or any(not isinstance(s, str) or not re.fullmatch('[a-z0-9][a-z0-9-]{1,63}', s) for s in members)
            or len(set(members)) != len(members)
            or set(members) != {t['subscriptionId'] for t in state.get('tenants', [])}
            or type(intent['requestedAt']) is not int or intent['deadlineAt'] != intent['requestedAt'] + 3600):
        raise RuntimeFailure('Runtime pause does not match the DEV host and all consumers')
    lock = controller.core.get_item(Key={'pk': 'CAPACITY#abera-signoz#MAINTENANCE', 'sk': 'LOCK'}, ConsistentRead=True).get('Item', {})
    if lock.get('operationId') != intent['owner'] or lock.get('unit') != 'runtime:abera-signoz' or not controller.lease:
        raise RuntimeFailure('Runtime pause requires the same core maintenance owner and live controller lease')
    scan = {'ConsistentRead': True,
            'FilterExpression': 'groupId=:gid AND #s IN (:pending,:running)',
            'ExpressionAttributeNames': {'#s': 'status'},
            'ExpressionAttributeValues': {':gid': controller.group_id, ':pending': 'PENDING', ':running': 'RUNNING'},
            'ProjectionExpression': 'pk, groupId, #s'}
    while True:
        page = controller.jobs.scan(**scan)
        if page.get('Items'):
            return False
        if not page.get('LastEvaluatedKey'):
            break
        scan['ExclusiveStartKey'] = page['LastEvaluatedKey']
    digest = hashlib.sha256(json.dumps(sorted(members), separators=(',', ':')).encode()).hexdigest()
    controller.jobs.update_item(Key={'pk': 'HOST#' + controller.group_id},
        UpdateExpression='SET runtimePauseOwner=:runtime, runtimePauseMembersDigest=:digest, runtimePauseConfirmedAt=:now',
        ConditionExpression='#owner=:controller AND instanceId=:instance AND expiresAt>:now',
        ExpressionAttributeNames={'#owner': 'owner'},
        ExpressionAttributeValues={':runtime': intent['owner'], ':digest': digest, ':now': int(time.time()),
                                   ':controller': controller.owner, ':instance': controller.env['HOST_INSTANCE_ID']})
    return True
