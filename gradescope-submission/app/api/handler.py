"""Lambda handler behind API Gateway: creates report jobs and reports their status.

POST /jobs                 create a job, return a presigned S3 upload for the CSV
POST /jobs/{job_id}/submit queue the uploaded CSV for a worker
GET  /jobs/{job_id}        job status, metrics and (when done) a PDF download URL
"""
import json
import math
import os
import re
import time
import uuid
from functools import lru_cache
from pathlib import PurePath

import boto3
from botocore.exceptions import ClientError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
URL_EXPIRY_SECONDS = 900
JOB_TTL_SECONDS = 7 * 24 * 3600
JOB_ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@lru_cache(maxsize=None)
def _s3():
    return boto3.client('s3')


@lru_cache(maxsize=None)
def _sqs():
    return boto3.client('sqs')


@lru_cache(maxsize=None)
def _table():
    return boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])


def _response(status, body):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': os.environ.get('ALLOWED_ORIGIN', '*'),
        },
        'body': json.dumps(body),
    }


def _optional_limit(value):
    if value in (None, ''):
        return None
    try:
        limit = float(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, 'Limits must be numbers') from exc
    if not math.isfinite(limit) or limit <= 0:
        raise ApiError(400, 'Limits must be numbers greater than zero')
    return str(limit)


def _load_job(job_id):
    if not JOB_ID_PATTERN.match(job_id or ''):
        raise ApiError(404, 'Job not found')
    item = _table().get_item(Key={'job_id': job_id}).get('Item')
    if not item:
        raise ApiError(404, 'Job not found')
    return item


def create_job(body):
    filename = PurePath(str(body.get('filename') or 'upload.csv')).name
    limits = {
        'max_voltage': _optional_limit(body.get('max_voltage')),
        'max_abs_current': _optional_limit(body.get('max_abs_current')),
    }
    job_id = uuid.uuid4().hex
    now = int(time.time())
    item = {
        'job_id': job_id,
        'status': 'AWAITING_UPLOAD',
        'filename': filename,
        'created_at': now,
        'expires_at': now + JOB_TTL_SECONDS,
        **{key: value for key, value in limits.items() if value is not None},
    }
    _table().put_item(Item=item)
    upload = _s3().generate_presigned_post(
        os.environ['BUCKET_NAME'],
        f'uploads/{job_id}.csv',
        Conditions=[['content-length-range', 1, MAX_UPLOAD_BYTES]],
        ExpiresIn=URL_EXPIRY_SECONDS,
    )
    return _response(201, {'job_id': job_id, 'upload': upload})


def submit_job(job_id):
    item = _load_job(job_id)
    if item['status'] != 'AWAITING_UPLOAD':
        raise ApiError(409, 'Job has already been submitted')
    try:
        _s3().head_object(Bucket=os.environ['BUCKET_NAME'], Key=f'uploads/{job_id}.csv')
    except ClientError as exc:
        raise ApiError(400, 'CSV has not been uploaded') from exc
    try:
        _table().update_item(
            Key={'job_id': job_id},
            UpdateExpression='SET #s = :queued',
            ConditionExpression='#s = :awaiting',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':queued': 'QUEUED', ':awaiting': 'AWAITING_UPLOAD'},
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise ApiError(409, 'Job has already been submitted') from exc
        raise
    _sqs().send_message(
        QueueUrl=os.environ['QUEUE_URL'], MessageBody=json.dumps({'job_id': job_id})
    )
    return _response(202, {'job_id': job_id, 'status': 'QUEUED'})


def get_job(job_id):
    item = _load_job(job_id)
    result = {
        'job_id': job_id,
        'status': item['status'],
        'filename': item.get('filename'),
    }
    if item['status'] == 'COMPLETED':
        stem = PurePath(item.get('filename') or 'report').stem
        result['metrics'] = item.get('metrics', {})
        result['download_url'] = _s3().generate_presigned_url(
            'get_object',
            Params={
                'Bucket': os.environ['BUCKET_NAME'],
                'Key': item['report_key'],
                'ResponseContentDisposition': f'attachment; filename="{stem}_report.pdf"',
            },
            ExpiresIn=URL_EXPIRY_SECONDS,
        )
    elif item['status'] == 'FAILED':
        result['error'] = item.get('error', 'Report generation failed')
    return _response(200, result)


def _parse_body(event):
    try:
        body = json.loads(event.get('body') or '{}')
    except json.JSONDecodeError as exc:
        raise ApiError(400, 'Request body must be JSON') from exc
    if not isinstance(body, dict):
        raise ApiError(400, 'Request body must be a JSON object')
    return body


def handler(event, context):
    try:
        route = (event.get('httpMethod'), event.get('resource'))
        job_id = (event.get('pathParameters') or {}).get('job_id')
        if route == ('POST', '/jobs'):
            return create_job(_parse_body(event))
        if route == ('POST', '/jobs/{job_id}/submit'):
            return submit_job(job_id)
        if route == ('GET', '/jobs/{job_id}'):
            return get_job(job_id)
        raise ApiError(404, 'Not found')
    except ApiError as exc:
        return _response(exc.status, {'error': str(exc)})
