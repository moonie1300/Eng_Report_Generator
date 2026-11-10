"""SQS worker for one-shot tasks and protected ECS services."""
import argparse
import json
import logging
import os
import signal
import socket
import tempfile
import threading
from pathlib import Path

import boto3

from core.report import build_report
from worker.ownership import LeaseLost, claim, finish, renew
from worker.protection import TaskProtection
from worker.submit import JOB_ID

LOGGER = logging.getLogger(__name__)
VISIBILITY_SECONDS = 600
HEARTBEAT_SECONDS = 30


class Heartbeat:
    def __init__(self, table, job_id, attempt, sqs=None, queue_url=None,
                 receipt=None, protection=None):
        self.table, self.job_id, self.attempt = table, job_id, attempt
        self.sqs, self.queue_url, self.receipt = sqs, queue_url, receipt
        self.protection = protection
        self.stopped = threading.Event()
        self.failure = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stopped.wait(HEARTBEAT_SECONDS):
            try:
                self.renew()
            except Exception as exc:
                LOGGER.exception('Heartbeat failed for job %s attempt %s', self.job_id, self.attempt)
                self.failure = exc
                self.stopped.set()

    def renew(self):
        if self.protection:
            self.protection.set(True)
        if self.sqs:
            self.sqs.change_message_visibility(
                QueueUrl=self.queue_url, ReceiptHandle=self.receipt,
                VisibilityTimeout=VISIBILITY_SECONDS,
            )
        renew(self.table, self.job_id, self.attempt)

    def __enter__(self):
        self.thread.start()
        return self

    def check(self):
        if self.failure:
            raise LeaseLost(f'Heartbeat failed for job {self.job_id}: {self.failure}') from self.failure

    def stop_and_check(self):
        self.stopped.set()
        self.thread.join()
        self.check()

    def __exit__(self, exc_type, exc_value, traceback):
        self.stopped.set()
        self.thread.join()


def process_job(job_id, table, s3, bucket, *, sqs=None, queue_url=None,
                receipt=None, protection=None, worker_id=None):
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise ValueError('Invalid job ID in queue message')
    reservation = claim(table, job_id)
    if reservation is None:
        item = table.get_item(Key={'job_id': job_id}, ConsistentRead=True).get('Item')
        if item and item.get('status') in ('COMPLETED', 'FAILED'):
            LOGGER.info('Skipping finished job %s', job_id)
            return True
        LOGGER.warning('Job %s is missing or owned by another worker; message retained', job_id)
        return False
    attempt, item = reservation
    worker_id = worker_id or socket.gethostname()
    LOGGER.info('Starting job %s attempt %s worker %s', job_id, attempt, worker_id)
    with Heartbeat(table, job_id, attempt, sqs, queue_url, receipt, protection) as heartbeat:
        limits = {key: float(item[key]) for key in ('max_voltage', 'max_abs_current') if key in item}
        report_key = f'reports/{job_id}/{attempt}.pdf'
        with tempfile.TemporaryDirectory() as folder:
            csv_path, pdf_path = Path(folder) / 'input.csv', Path(folder) / 'report.pdf'
            s3.download_file(bucket, f'uploads/{job_id}.csv', str(csv_path))
            try:
                metrics = build_report(csv_path, pdf_path, item.get('filename', 'upload.csv'), **limits)
            except (ValueError, UnicodeError) as exc:
                heartbeat.stop_and_check()
                finish(table, job_id, attempt, 'FAILED', error=str(exc), worker_id=worker_id)
                LOGGER.error('Job %s attempt %s failed validation: %s', job_id, attempt, exc)
                return True
            heartbeat.check()
            s3.upload_file(str(pdf_path), bucket, report_key,
                           ExtraArgs={'ContentType': 'application/pdf'})
        heartbeat.stop_and_check()
        finish(table, job_id, attempt, 'COMPLETED', report_key=report_key,
               metrics=metrics, worker_id=worker_id)
        LOGGER.info('Completed job %s attempt %s worker %s report %s',
                    job_id, attempt, worker_id, report_key)
        return True


def handle_message(message, table, s3, sqs, bucket, queue_url, protection, worker_id):
    job_id = json.loads(message['Body'])['job_id']
    if process_job(job_id, table, s3, bucket, sqs=sqs,
                   queue_url=queue_url, receipt=message['ReceiptHandle'],
                   protection=protection, worker_id=worker_id):
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message['ReceiptHandle'])


def run(once=False):
    bucket = os.environ['BUCKET_NAME']
    queue_url = os.environ['QUEUE_URL']
    table = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])
    s3, sqs = boto3.client('s3'), boto3.client('sqs')
    protection = None if once else TaskProtection()
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    while not stopping:
        protected = False
        try:
            worker_id = protection.set(True) if protection else socket.gethostname()
            protected = bool(protection)
            response = sqs.receive_message(
                QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=20,
                VisibilityTimeout=VISIBILITY_SECONDS,
            )
            for message in response.get('Messages', []):
                try:
                    handle_message(message, table, s3, sqs, bucket, queue_url, protection, worker_id)
                except Exception:
                    LOGGER.exception('Message processing failed; SQS will retry or redrive it')
                    if once:
                        raise
        finally:
            if protected:
                protection.set(False)
        if once:
            break


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Poll once and exit')
    run(once=parser.parse_args().once)
