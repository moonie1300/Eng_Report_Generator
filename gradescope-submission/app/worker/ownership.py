"""DynamoDB leases fence worker attempts and prevent stale result publication."""
import time
import uuid

from botocore.exceptions import ClientError

LEASE_SECONDS = 180


class LeaseLost(RuntimeError):
    pass


def conditional_failure(exc):
    return exc.response['Error']['Code'] == 'ConditionalCheckFailedException'


def claim(table, job_id, owner=None, now=None):
    owner = owner or uuid.uuid4().hex
    now = int(time.time()) if now is None else now
    try:
        response = table.update_item(
            Key={'job_id': job_id},
            UpdateExpression='SET #s = :processing, attempt_id = :owner, lease_until = :until',
            ConditionExpression=(
                'attribute_exists(job_id) AND (#s = :queued OR '
                '(#s = :processing AND (attribute_not_exists(lease_until) OR lease_until <= :now)))'
            ),
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':processing': 'PROCESSING', ':queued': 'QUEUED', ':owner': owner,
                ':until': now + LEASE_SECONDS, ':now': now,
            },
            ReturnValues='ALL_NEW',
        )
        return owner, response['Attributes']
    except ClientError as exc:
        if not conditional_failure(exc):
            raise
        return None


def owned_update(table, job_id, owner, update, values, names=None, now=None):
    now = int(time.time()) if now is None else now
    try:
        table.update_item(
            Key={'job_id': job_id}, UpdateExpression=update,
            ConditionExpression='#s = :processing AND attempt_id = :owner AND lease_until > :now',
            ExpressionAttributeNames={'#s': 'status', **(names or {})},
            ExpressionAttributeValues={':processing': 'PROCESSING', ':owner': owner, ':now': now, **values},
        )
    except ClientError as exc:
        if conditional_failure(exc):
            raise LeaseLost(f'Lease lost for job {job_id}, attempt {owner}') from exc
        raise


def renew(table, job_id, owner):
    now = int(time.time())
    owned_update(table, job_id, owner, 'SET lease_until = :until',
                 {':until': now + LEASE_SECONDS}, now=now)


def finish(table, job_id, owner, status, **attributes):
    values = {':final': status}
    names = {}
    assignments = ['#s = :final']
    for index, (key, value) in enumerate(attributes.items()):
        names[f'#a{index}'] = key
        values[f':v{index}'] = value
        assignments.append(f'#a{index} = :v{index}')
    owned_update(table, job_id, owner,
                 'SET ' + ', '.join(assignments) + ' REMOVE lease_until', values, names)
