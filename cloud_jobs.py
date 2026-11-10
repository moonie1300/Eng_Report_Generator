"""Local-only bridge from the browser to the existing AWS report workflow."""
import uuid
import os

import boto3
from botocore.exceptions import ClientError

from core.limits import optional_limit
from worker.launcher import ensure_scaling_service_absent
from worker.queue_config import REGION, resolve_queue_url
from worker.submit import JOB_ID, submit_job

BUCKET = 'cab432-n11180021-reports'
TABLE = 'n11180021-a3-report-jobs'
CLUSTER = 'n11180021-a3-report-cluster'
TASK_DEFINITION = 'n11180021-a3-report-worker:1'
LOCK_ID = '_report_worker_launcher'


class CloudJobs:
    def __init__(self):
        session = boto3.Session(region_name=REGION)
        self.s3 = session.client('s3')
        self.sqs = session.client('sqs')
        self.table = session.resource('dynamodb').Table(TABLE)
        self.ecs = session.client('ecs')

    @staticmethod
    def check_id(job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ValueError('Invalid job ID')

    def submit(self, job_id, csv_text, max_voltage=None, max_abs_current=None):
        self.check_id(job_id)
        if not isinstance(csv_text, str) or not csv_text.strip():
            raise ValueError('Choose a non-empty CSV file')
        limits = {
            key: value for key, value in (
                ('max_voltage', optional_limit(max_voltage)),
                ('max_abs_current', optional_limit(max_abs_current)),
            ) if value is not None
        }
        # Never overwrite an earlier job on a retry of an uncertain submission.
        if self.table.get_item(Key={'job_id': job_id}, ConsistentRead=True).get('Item'):
            raise ValueError('Job ID already exists; check its status instead of resubmitting')
        queue_url = resolve_queue_url(self.sqs)
        self.s3.put_object(
            Bucket=BUCKET, Key=f'uploads/{job_id}.csv',
            Body=csv_text.encode('utf-8'), ContentType='text/csv',
            IfNoneMatch='*',
        )
        submit_job(job_id, BUCKET, self.table, self.sqs, queue_url, self.s3, **limits)
        return {'job_id': job_id, 'status': 'QUEUED'}

    def status(self, job_id):
        self.check_id(job_id)
        item = self.table.get_item(Key={'job_id': job_id}, ConsistentRead=True).get('Item')
        if not item:
            raise LookupError('Job not found')
        result = {'job_id': job_id, 'status': item['status']}
        if item.get('error'):
            result['error'] = str(item['error'])
        return result

    def report(self, job_id):
        self.check_id(job_id)
        item = self.table.get_item(Key={'job_id': job_id}, ConsistentRead=True).get('Item')
        if not item:
            raise LookupError('Job not found')
        key = item.get('report_key')
        legacy = f'reports/{job_id}.pdf'
        prefix = f'reports/{job_id}/'
        attempt = key[len(prefix):-4] if isinstance(key, str) and key.startswith(prefix) and key.endswith('.pdf') else ''
        if (item.get('status') != 'COMPLETED' or not isinstance(key, str)
                or (key != legacy and not (len(attempt) == 32 and all(c in '0123456789abcdef' for c in attempt)))):
            raise ValueError('The report is not ready')
        response = self.s3.get_object(Bucket=BUCKET, Key=key)
        try:
            return response['Body'].read()
        finally:
            response['Body'].close()

    def start_worker(self, job_id):
        if os.environ.get('REPORT_SCALING_SERVICE') == '1':
            raise ValueError('Manual worker launches are disabled while the scaling service is enabled')
        ensure_scaling_service_absent(self.ecs, CLUSTER)
        if self.status(job_id)['status'] != 'QUEUED':
            raise ValueError('A worker can only be started for a queued job')
        lock = self.table.get_item(Key={'job_id': LOCK_ID}, ConsistentRead=True).get('Item')
        if lock:
            if 'task_arn' not in lock:
                raise ValueError('A worker launch has an uncertain outcome; investigate the reservation before retrying')
            response = self.ecs.describe_tasks(cluster=CLUSTER, tasks=[lock['task_arn']])
            tasks = response.get('tasks', [])
            if response.get('failures') or len(tasks) != 1:
                raise ValueError('Cannot determine the previous worker status; investigate before retrying')
            task = tasks[0]
            if task['lastStatus'] != 'STOPPED':
                raise ValueError('A worker is already running or starting')
            containers = [c for c in task.get('containers', []) if c.get('name') == 'report-worker']
            if len(containers) != 1 or containers[0].get('exitCode') != 0:
                raise ValueError('The previous worker failed; investigate before starting another')
            self.table.delete_item(
                Key={'job_id': LOCK_ID},
                ConditionExpression='launch_token = :token AND task_arn = :task',
                ExpressionAttributeValues={':token': lock['launch_token'], ':task': lock['task_arn']},
            )
        token = uuid.uuid4().hex
        try:
            self.table.put_item(
                Item={'job_id': LOCK_ID, 'launch_token': token},
                ConditionExpression='attribute_not_exists(job_id)',
            )
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                raise ValueError('Another worker launch is already reserved') from exc
            raise
        # Never automatically retry a reserved launch with an unknown outcome.
        try:
            ensure_scaling_service_absent(self.ecs, CLUSTER)
            response = self.ecs.run_task(
                cluster=CLUSTER, taskDefinition=TASK_DEFINITION, launchType='FARGATE',
                count=1, clientToken=token,
                networkConfiguration={'awsvpcConfiguration': {
                    'subnets': ['subnet-04cc288ea3b2e1e53'],
                    'securityGroups': ['sg-078997505ad1c6bbc'],
                    'assignPublicIp': 'DISABLED',
                }},
                tags=[
                    {'key': 'purpose', 'value': 'assessment 3'},
                    {'key': 'qut-username', 'value': 'n11180021@qut.edu.au'},
                ],
            )
            tasks = response.get('tasks', [])
            if response.get('failures') or len(tasks) != 1:
                raise RuntimeError(f'ECS did not return one task: {response.get("failures", [])}')
            task_arn = tasks[0]['taskArn']
            self.table.update_item(
                Key={'job_id': LOCK_ID}, UpdateExpression='SET task_arn = :task',
                ConditionExpression='launch_token = :token AND attribute_not_exists(task_arn)',
                ExpressionAttributeValues={':task': task_arn, ':token': token},
            )
        except Exception as exc:
            raise RuntimeError(
                f'Worker launch could not be confirmed: {exc}. The reservation remains; '
                'investigate ECS and DynamoDB before retrying.'
            ) from exc
        return {'task_arn': task_arn}
