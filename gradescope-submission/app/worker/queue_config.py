"""Canonical SQS configuration for the existing Assignment 3 queues."""
from urllib.parse import urlsplit

REGION = 'ap-southeast-2'
ACCOUNT_ID = '901444280953'
QUEUE_NAME = 'n11180021-a3-report-jobs'
DLQ_NAME = 'n11180021-a3-report-jobs-dlq'


def resolve_queue_url(sqs, queue_name=QUEUE_NAME):
    if queue_name not in (QUEUE_NAME, DLQ_NAME):
        raise ValueError(f'Unexpected queue name: {queue_name}')
    if sqs.meta.region_name != REGION:
        raise ValueError(f'SQS client must use region {REGION}, not {sqs.meta.region_name}')
    url = sqs.get_queue_url(QueueName=queue_name, QueueOwnerAWSAccountId=ACCOUNT_ID)['QueueUrl']
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.netloc != f'sqs.{REGION}.amazonaws.com'
            or parts.path != f'/{ACCOUNT_ID}/{queue_name}' or parts.query or parts.fragment):
        raise ValueError(f'SQS returned a queue URL inconsistent with the configured account, region or name: {url}')
    return url


def main():
    import boto3

    session = boto3.Session(region_name=REGION)
    account = session.client('sts').get_caller_identity()['Account']
    if account != ACCOUNT_ID:
        raise ValueError(f'AWS caller account must be {ACCOUNT_ID}, not {account}')
    sqs = session.client('sqs')
    print(f'Account: {account}; region: {REGION}')
    for name in (QUEUE_NAME, DLQ_NAME):
        print(f'{name}: {resolve_queue_url(sqs, name)}')


if __name__ == '__main__':
    main()
