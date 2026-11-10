"""Read-only, timestamped snapshots of the SQS worker service and selected jobs."""
import argparse
import json
import time
from datetime import datetime, timezone

import boto3

from cloud_jobs import CLUSTER, TABLE
from worker.queue_config import DLQ_NAME, REGION, resolve_queue_url
from worker.submit import JOB_ID

SERVICE = 'n11180021-a3-report-workers'
ATTRIBUTES = ['ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible',
              'ApproximateNumberOfMessagesDelayed']


def snapshot(sqs, ecs, table, job_ids, queue_url, dlq_url):
    queue = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=ATTRIBUTES)['Attributes']
    dlq = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=ATTRIBUTES)['Attributes']
    response = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])
    services = response.get('services', [])
    if response.get('failures') or len(services) != 1:
        raise RuntimeError(f'Cannot inspect worker service: {response.get("failures", [])}')
    service = services[0]
    jobs = []
    for job_id in job_ids:
        if not JOB_ID.fullmatch(job_id):
            raise ValueError(f'Invalid job ID: {job_id}')
        item = table.get_item(Key={'job_id': job_id}, ConsistentRead=True).get('Item')
        jobs.append({
            'job_id': job_id, 'status': item.get('status') if item else 'NOT_FOUND',
            'attempt_id': item.get('attempt_id') if item else None,
            'worker_id': item.get('worker_id') if item else None,
            'report_key': item.get('report_key') if item else None,
        })
    return {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'queue': queue, 'dlq': dlq,
        'service': {key: service[key] for key in ('desiredCount', 'runningCount', 'pendingCount')},
        'jobs': jobs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_ids', nargs='*', help='Job IDs to include in each snapshot')
    parser.add_argument('--interval', type=int, default=10, help='Seconds between snapshots')
    parser.add_argument('--samples', type=int, default=1, help='Number of snapshots')
    args = parser.parse_args()
    if args.interval <= 0 or args.samples <= 0:
        parser.error('interval and samples must be positive')
    session = boto3.Session(region_name=REGION)
    sqs, ecs = session.client('sqs'), session.client('ecs')
    table = session.resource('dynamodb').Table(TABLE)
    queue_url = resolve_queue_url(sqs)
    dlq_url = resolve_queue_url(sqs, DLQ_NAME)
    for index in range(args.samples):
        print(json.dumps(snapshot(sqs, ecs, table, args.job_ids, queue_url, dlq_url)), flush=True)
        if index + 1 < args.samples:
            time.sleep(args.interval)


if __name__ == '__main__':
    main()
