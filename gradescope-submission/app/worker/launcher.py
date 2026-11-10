"""Scheduled, single-worker reconciler for the existing SQS report queue."""
import logging
import os
import uuid

import boto3
from botocore.exceptions import ClientError

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
LOCK_ID = '_report_worker_launcher'
SCALING_SERVICE = 'n11180021-a3-report-workers'


def ensure_scaling_service_absent(ecs, cluster):
    response = ecs.describe_services(cluster=cluster, services=[SCALING_SERVICE])
    services = response.get('services', [])
    failures = response.get('failures', [])
    if not services and len(failures) == 1 and failures[0].get('reason') == 'MISSING':
        return
    if len(services) != 1 or failures or services[0].get('status') != 'INACTIVE':
        raise RuntimeError('Cannot launch a one-shot worker while the scaling service exists or its status is uncertain')


def _conditional_failure(exc):
    return exc.response['Error']['Code'] == 'ConditionalCheckFailedException'


def reconcile(table, sqs, ecs, config):
    ensure_scaling_service_absent(ecs, config['cluster'])
    lock = table.get_item(Key={'job_id': LOCK_ID}, ConsistentRead=True).get('Item')
    if lock and 'task_arn' in lock:
        response = ecs.describe_tasks(cluster=config['cluster'], tasks=[lock['task_arn']])
        if response.get('failures') or len(response.get('tasks', [])) != 1:
            raise RuntimeError('Cannot determine the status of the recorded worker task')
        task = response['tasks'][0]
        if task['lastStatus'] != 'STOPPED':
            LOGGER.info('Worker %s is %s', lock['task_arn'], task['lastStatus'])
            return
        containers = [container for container in task.get('containers', [])
                      if container.get('name') == 'report-worker']
        if len(containers) != 1 or containers[0].get('exitCode') != 0:
            raise RuntimeError(
                f'Worker {lock["task_arn"]} stopped unsuccessfully '
                f'({task.get("stoppedReason", "unknown")}); investigate before restarting'
            )
        try:
            table.delete_item(
                Key={'job_id': LOCK_ID},
                ConditionExpression='launch_token = :token AND task_arn = :task',
                ExpressionAttributeValues={':token': lock['launch_token'], ':task': lock['task_arn']},
            )
        except ClientError as exc:
            if _conditional_failure(exc):
                LOGGER.info('Launcher record changed; another invocation will reconcile')
                return
            raise
        LOGGER.info('Worker %s stopped: %s', lock['task_arn'], task.get('stoppedReason', 'unknown'))
        lock = None

    if not lock:
        attributes = sqs.get_queue_attributes(
            QueueUrl=config['queue_url'], AttributeNames=['ApproximateNumberOfMessages']
        )['Attributes']
        if int(attributes['ApproximateNumberOfMessages']) == 0:
            LOGGER.info('No visible messages')
            return
        lock = {'job_id': LOCK_ID, 'launch_token': uuid.uuid4().hex}
        try:
            table.put_item(Item=lock, ConditionExpression='attribute_not_exists(job_id)')
        except ClientError as exc:
            if _conditional_failure(exc):
                LOGGER.info('Another invocation reserved the worker launch')
                return
            raise

    token = lock['launch_token']
    ensure_scaling_service_absent(ecs, config['cluster'])
    response = ecs.run_task(
        cluster=config['cluster'],
        taskDefinition=config['task_definition'],
        launchType='FARGATE',
        count=1,
        clientToken=token,
        networkConfiguration={
            'awsvpcConfiguration': {
                'subnets': [config['subnet']],
                'securityGroups': [config['security_group']],
                'assignPublicIp': 'DISABLED',
            }
        },
        tags=[
            {'key': 'purpose', 'value': 'assessment 3'},
            {'key': 'qut-username', 'value': 'n11180021@qut.edu.au'},
        ],
    )
    tasks = response.get('tasks', [])
    if response.get('failures') or len(tasks) != 1:
        raise RuntimeError(f'ECS RunTask did not return one task: {response.get("failures", [])}')
    task_arn = tasks[0]['taskArn']
    table.update_item(
        Key={'job_id': LOCK_ID},
        UpdateExpression='SET task_arn = :task',
        ConditionExpression='launch_token = :token AND attribute_not_exists(task_arn)',
        ExpressionAttributeValues={':task': task_arn, ':token': token},
    )
    LOGGER.info('Started worker %s', task_arn)


def handler(event, context):
    config = {
        'queue_url': os.environ['QUEUE_URL'],
        'cluster': os.environ['CLUSTER'],
        'task_definition': os.environ['TASK_DEFINITION'],
        'subnet': os.environ['SUBNET_ID'],
        'security_group': os.environ['SECURITY_GROUP_ID'],
    }
    table = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])
    reconcile(table, boto3.client('sqs'), boto3.client('ecs'), config)
