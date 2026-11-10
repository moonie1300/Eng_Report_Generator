"""Queue a CSV already uploaded by the existing upload API."""
import argparse
import json
import re
import time

import boto3
from botocore.exceptions import ClientError

from core.limits import optional_limit
from worker.queue_config import REGION, resolve_queue_url

JOB_ID = re.compile(
    r'^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12})$'
)


def submit_job(job_id, bucket, table, sqs, queue_url, s3, *, max_voltage=None, max_abs_current=None):
    if not JOB_ID.fullmatch(job_id):
        raise ValueError('Job ID must be a lowercase UUID or 32 lowercase hexadecimal characters')
    limits = {
        key: str(value) for key, value in (
            ('max_voltage', optional_limit(max_voltage)),
            ('max_abs_current', optional_limit(max_abs_current)),
        ) if value is not None
    }
    key = f'uploads/{job_id}.csv'
    s3.head_object(Bucket=bucket, Key=key)
    try:
        table.put_item(
            Item={
                'job_id': job_id,
                'status': 'QUEUED',
                'filename': f'{job_id}.csv',
                'created_at': int(time.time()),
                **limits,
            },
            ConditionExpression='attribute_not_exists(job_id)',
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        item = table.get_item(Key={'job_id': job_id}, ConsistentRead=True).get('Item')
        if not item or item.get('status') != 'QUEUED':
            raise ValueError('Job already exists and is not queued') from exc
        if any(key not in item or float(item[key]) != float(value) for key, value in limits.items()):
            raise ValueError('Existing queued job has different assessment limits') from exc

    # A retry may produce duplicate messages; the worker ignores completed jobs.
    sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({'job_id': job_id}))
    return job_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id', help='Job ID returned by the existing upload API')
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--table', required=True)
    parser.add_argument('--queue-url', help='Optional expected URL; must match the resolved queue')
    parser.add_argument('--region', default=REGION)
    parser.add_argument('--max-voltage', help='Optional positive voltage limit')
    parser.add_argument('--max-abs-current', help='Optional positive absolute current limit')
    args = parser.parse_args()
    try:
        if args.region != REGION:
            raise ValueError(f'Region must be {REGION}, not {args.region}')
        session = boto3.Session(region_name=REGION)
        sqs = session.client('sqs')
        queue_url = resolve_queue_url(sqs)
        if args.queue_url is not None and args.queue_url != queue_url:
            raise ValueError('Provided queue URL does not match the resolved queue URL')
        submit_job(
            args.job_id, args.bucket, session.resource('dynamodb').Table(args.table),
            sqs, queue_url, session.client('s3'),
            max_voltage=args.max_voltage, max_abs_current=args.max_abs_current,
        )
    except (ValueError, ClientError) as exc:
        parser.exit(1, f'Submission failed: {exc}\n')
    print(f'Queued job: {args.job_id}')


if __name__ == '__main__':
    main()
