"""Record bounded DEV cancellation only after native writers have exited."""
import hashlib

from botocore.exceptions import ClientError

from .host import RuntimeFailure


def outstanding(power, members):
    """Validate every outstanding job before any container cancellation."""
    group = power.env['HOST_GROUP_ID']
    tenants = {t['subscriptionId']: t for t in power.host.state['tenants']}
    scan = {'ConsistentRead': True, 'FilterExpression': 'groupId=:gid AND #s IN (:pending,:running)',
            'ExpressionAttributeNames': {'#s': 'status'},
            'ExpressionAttributeValues': {':gid': group, ':pending': 'PENDING', ':running': 'RUNNING'}}
    jobs = []
    while True:
        page = power.jobs.scan(**scan)
        for job in page.get('Items', []):
            request = job.get('request') or {}
            sub, operation = request.get('subscription', {}), request.get('operation', {})
            sid = sub.get('id')
            tenant = tenants.get(sid, {})
            assigned = power.jobs.get_item(Key={'pk': 'SUB#' + str(sid)}, ConsistentRead=True).get('Item', {})
            allocation = request.get('context', {}).get('sharedHostAllocation', {})
            token = hashlib.sha256(f"{sid}:{operation.get('id')}:{request.get('hook')}".encode()).hexdigest()
            if (request.get('protocolVersion') != 4 or request.get('environment') != 'dev'
                    or sub.get('productId') != 'abera-signoz' or sid not in members
                    or not operation.get('id') or request.get('hook') not in {'prepare', 'verify', 'finalize', 'compensate'}
                    or job.get('pk') != 'JOB#' + token or job.get('groupId') != group
                    or assigned.get('pk') != 'SUB#' + str(sid) or assigned.get('groupId') != group
                    or not sub.get('customerId') or sub['customerId'] != assigned.get('customerId')
                    or tenant.get('customerId') != assigned.get('customerId')
                    or tenant.get('slot') != assigned.get('slot')
                    or tenant.get('allocationOperation') != assigned.get('allocationOperationId')
                    or allocation != {'groupId': group, 'slot': assigned.get('slot'), 'customerId': assigned.get('customerId'),
                                      'allocationOperationId': assigned.get('allocationOperationId')}):
                raise RuntimeFailure('Outstanding job ownership is incomplete; retain the runtime fence')
            jobs.append(job)
        if not page.get('LastEvaluatedKey'):
            return jobs
        scan['ExclusiveStartKey'] = page['LastEvaluatedKey']


def cancel(power, jobs, owner, members):
    """Conditional updates preserve a completion recorded before writer exit."""
    now = int(power.clock())
    for job in jobs:
        group = power._scope(owner, members)
        try:
            power.jobs.update_item(Key={'pk': job['pk']},
                UpdateExpression='SET #s=:failed, #r=:response, runtimeCancellationOwner=:runtime, cancelledAt=:now, expiresAt=:expiry REMOVE #q',
                ConditionExpression='#s=:observed AND groupId=:gid AND #request=:request',
                ExpressionAttributeNames={'#s': 'status', '#r': 'response', '#q': 'queue', '#request': 'request'},
                ExpressionAttributeValues={':failed': 'FAILED', ':observed': job['status'], ':gid': group,
                    ':request': job['request'], ':runtime': owner, ':now': now, ':expiry': now + 90 * 86400,
                    ':response': {'status': 'FAILED', 'error': {'code': 'RUNTIME_DRAIN_DEADLINE',
                        'message': 'DEV operating-hour shutdown cancelled this job after the one-hour deadline. Inspect retained partial work before recovery.',
                        'retryable': False}}})
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
            current = power.jobs.get_item(Key={'pk': job['pk']}, ConsistentRead=True).get('Item', {})
            if (current.get('request') != job['request'] or current.get('groupId') != group
                    or current.get('status') not in {'SUCCEEDED', 'FAILED'}):
                raise RuntimeFailure('Job changed during cancellation; retain the runtime fence') from None
