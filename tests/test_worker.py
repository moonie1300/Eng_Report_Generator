import concurrent.futures
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    from worker import main as worker
    from worker.ownership import LeaseLost, claim, finish, renew
    from tests.fake_job_table import FakeJobTable
except ImportError as exc:
    raise unittest.SkipTest(f'boto3 and reportlab are required for worker tests: {exc}')

SAMPLE = Path(__file__).resolve().parent.parent / 'Sample_Data' / 'test_normal.csv'
JOB_ID = 'b' * 32


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.table, self.s3 = FakeJobTable(), MagicMock()
        self.table.put_item(Item={'job_id': JOB_ID, 'status': 'QUEUED'})
        self.uploaded = {}
        self.s3.download_file.side_effect = lambda bucket, key, path: Path(path).write_bytes(SAMPLE.read_bytes())
        self.s3.upload_file.side_effect = lambda path, bucket, key, **kwargs: self.uploaded.update({key: Path(path).read_bytes()})

    def test_completes_job_and_uploads_pdf(self):
        self.table.items[JOB_ID]['max_voltage'] = '60'
        with self.assertLogs('worker.main', level='INFO') as logs:
            self.assertTrue(worker.process_job(JOB_ID, self.table, self.s3, 'bucket', worker_id='task'))
        item = self.table.items[JOB_ID]
        self.assertEqual(self.table.statuses, ['PROCESSING', 'COMPLETED'])
        self.assertEqual(item['metrics']['Test assessment'], 'PARTIALLY ASSESSED')
        self.assertTrue(self.uploaded[item['report_key']].startswith(b'%PDF'))
        self.assertEqual(item['worker_id'], 'task')
        self.assertIn(f'Starting job {JOB_ID}', logs.output[0])
        self.assertIn(f'Completed job {JOB_ID}', logs.output[-1])

    def test_invalid_csv_marks_job_failed_without_retry(self):
        self.s3.download_file.side_effect = lambda bucket, key, path: Path(path).write_bytes(b'timestamp,voltage_v\n')
        with self.assertLogs('worker.main', level='ERROR'):
            self.assertTrue(worker.process_job(JOB_ID, self.table, self.s3, 'bucket'))
        self.assertEqual(self.table.statuses, ['PROCESSING', 'FAILED'])
        self.s3.upload_file.assert_not_called()

    def test_missing_is_retained_and_finished_is_acknowledged(self):
        self.table.items.clear()
        self.assertFalse(worker.process_job(JOB_ID, self.table, self.s3, 'bucket'))
        self.table.items[JOB_ID] = {'status': 'COMPLETED'}
        self.assertTrue(worker.process_job(JOB_ID, self.table, self.s3, 'bucket'))
        self.s3.download_file.assert_not_called()

    def test_concurrent_claim_and_duplicate_message(self):
        entered, release = threading.Event(), threading.Event()
        def download(bucket, key, path):
            entered.set()
            self.assertTrue(release.wait(5))
            Path(path).write_bytes(SAMPLE.read_bytes())
        self.s3.download_file.side_effect = download
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker.process_job, JOB_ID, self.table, self.s3, 'bucket')
            self.assertTrue(entered.wait(5))
            self.assertFalse(pool.submit(worker.process_job, JOB_ID, self.table, self.s3, 'bucket').result(5))
            release.set()
            self.assertTrue(first.result(5))
        self.assertEqual(self.table.statuses, ['PROCESSING', 'COMPLETED'])
        self.assertEqual(len(self.uploaded), 1)

    def test_crashed_owner_lease_recovery_and_fencing(self):
        owner, _ = claim(self.table, JOB_ID, owner='a' * 32, now=100)
        self.assertIsNone(claim(self.table, JOB_ID, owner='b' * 32, now=279))
        self.assertEqual(claim(self.table, JOB_ID, owner='b' * 32, now=280)[0], 'b' * 32)
        with self.assertRaises(LeaseLost):
            finish(self.table, JOB_ID, owner, 'COMPLETED', report_key='stale')
        with self.assertRaises(LeaseLost):
            renew(self.table, JOB_ID, owner)
        self.assertEqual(self.table.items[JOB_ID]['attempt_id'], 'b' * 32)
        self.assertNotIn('report_key', self.table.items[JOB_ID])

    def test_heartbeat_failure_prevents_completion(self):
        with patch.object(worker.Heartbeat, 'check', side_effect=LeaseLost('renewal failed')):
            with self.assertRaises(LeaseLost):
                worker.process_job(JOB_ID, self.table, self.s3, 'bucket')
        self.assertEqual(self.table.items[JOB_ID]['status'], 'PROCESSING')
        self.assertEqual(self.uploaded, {})

    def test_final_heartbeat_failure_prevents_commit_after_upload(self):
        with patch.object(worker.Heartbeat, 'stop_and_check', side_effect=LeaseLost('renewal failed')):
            with self.assertRaises(LeaseLost):
                worker.process_job(JOB_ID, self.table, self.s3, 'bucket')
        self.assertEqual(self.table.items[JOB_ID]['status'], 'PROCESSING')
        self.assertNotIn('report_key', self.table.items[JOB_ID])
        self.assertEqual(len(self.uploaded), 1)

    def test_inflight_heartbeat_failure_is_observed_before_final_commit(self):
        started, release = threading.Event(), threading.Event()

        def renewal(_heartbeat):
            started.set()
            if not release.wait(5):
                raise TimeoutError('test did not release heartbeat')
            raise RuntimeError('visibility renewal failed')

        def upload(path, bucket, key, **kwargs):
            self.assertTrue(started.wait(5))
            self.uploaded[key] = Path(path).read_bytes()
            release.set()

        self.s3.upload_file.side_effect = upload
        with patch.object(worker, 'HEARTBEAT_SECONDS', 0.001), \
             patch.object(worker.Heartbeat, 'renew', renewal), \
             self.assertLogs('worker.main', level='ERROR'):
            with self.assertRaisesRegex(LeaseLost, 'visibility renewal failed'):
                worker.process_job(JOB_ID, self.table, self.s3, 'bucket')
        self.assertEqual(self.table.items[JOB_ID]['status'], 'PROCESSING')
        self.assertNotIn('report_key', self.table.items[JOB_ID])
        self.assertEqual(len(self.uploaded), 1)

    def test_heartbeat_renews_visibility_lease_and_protection(self):
        owner, _ = claim(self.table, JOB_ID)
        sqs, protection = MagicMock(), MagicMock()
        heartbeat = worker.Heartbeat(self.table, JOB_ID, owner, sqs, 'queue', 'receipt', protection)
        heartbeat.renew()
        protection.set.assert_called_once_with(True)
        sqs.change_message_visibility.assert_called_once_with(
            QueueUrl='queue', ReceiptHandle='receipt', VisibilityTimeout=worker.VISIBILITY_SECONDS)
        self.assertEqual(self.table.items[JOB_ID]['attempt_id'], owner)

    def test_message_deleted_only_for_terminal_job(self):
        sqs = MagicMock()
        message = {'Body': '{"job_id": "' + JOB_ID + '"}', 'ReceiptHandle': 'receipt'}
        claim(self.table, JOB_ID)
        worker.handle_message(message, self.table, self.s3, sqs, 'bucket', 'queue', None, 'worker')
        sqs.delete_message.assert_not_called()
        self.table.items[JOB_ID]['status'] = 'COMPLETED'
        worker.handle_message(message, self.table, self.s3, sqs, 'bucket', 'queue', None, 'worker')
        sqs.delete_message.assert_called_once_with(QueueUrl='queue', ReceiptHandle='receipt')

    def test_processing_exception_leaves_message_for_retry_and_dlq(self):
        sqs = MagicMock()
        self.s3.download_file.side_effect = OSError('S3 unavailable')
        message = {'Body': '{"job_id": "' + JOB_ID + '"}', 'ReceiptHandle': 'receipt'}
        with self.assertRaisesRegex(OSError, 'S3 unavailable'):
            worker.handle_message(message, self.table, self.s3, sqs, 'bucket', 'queue', None, 'worker')
        sqs.delete_message.assert_not_called()
        self.assertEqual(self.table.items[JOB_ID]['status'], 'PROCESSING')

    def test_old_attempt_cannot_commit_after_another_attempt_completes(self):
        old, _ = claim(self.table, JOB_ID, owner='a' * 32, now=100)
        new, _ = claim(self.table, JOB_ID, owner='b' * 32, now=280)
        with patch('worker.ownership.time.time', return_value=281):
            finish(self.table, JOB_ID, new, 'COMPLETED', report_key='new')
            with self.assertRaises(LeaseLost):
                finish(self.table, JOB_ID, old, 'COMPLETED', report_key='old')
        self.assertEqual(self.table.items[JOB_ID]['report_key'], 'new')

    def test_service_does_not_poll_when_protection_is_denied(self):
        sqs = MagicMock()
        protection = MagicMock()
        protection.set.side_effect = RuntimeError('AccessDenied')
        session = MagicMock()
        session.Table.return_value = self.table
        clients = {'s3': self.s3, 'sqs': sqs}
        with patch.dict('os.environ', {
            'BUCKET_NAME': 'bucket', 'QUEUE_URL': 'queue', 'TABLE_NAME': 'table'
        }), patch.object(worker.boto3, 'resource', return_value=session), \
             patch.object(worker.boto3, 'client', side_effect=lambda service: clients[service]), \
             patch.object(worker, 'TaskProtection', return_value=protection), \
             patch.object(worker.signal, 'signal'):
            with self.assertRaisesRegex(RuntimeError, 'AccessDenied'):
                worker.run()
        sqs.receive_message.assert_not_called()

    def test_service_keeps_protection_until_message_handling_finishes(self):
        sqs, protection = MagicMock(), MagicMock()
        protection.set.return_value = 'worker'
        resource = MagicMock()
        resource.Table.return_value = self.table
        message = {'Body': '{"job_id": "' + JOB_ID + '"}', 'ReceiptHandle': 'receipt'}
        sqs.receive_message.return_value = {'Messages': [message]}
        callbacks = {}

        def capture_signal(signum, callback):
            callbacks[signum] = callback

        def handle(*args):
            self.assertEqual(protection.set.call_args_list, [unittest.mock.call(True)])
            callbacks[worker.signal.SIGTERM](worker.signal.SIGTERM, None)

        with patch.dict('os.environ', {
            'BUCKET_NAME': 'bucket', 'QUEUE_URL': 'queue', 'TABLE_NAME': 'table'
        }), patch.object(worker.boto3, 'resource', return_value=resource), \
             patch.object(worker.boto3, 'client', side_effect=lambda name: {'s3': self.s3, 'sqs': sqs}[name]), \
             patch.object(worker, 'TaskProtection', return_value=protection), \
             patch.object(worker.signal, 'signal', side_effect=capture_signal), \
             patch.object(worker, 'handle_message', side_effect=handle):
            worker.run()
        self.assertEqual(protection.set.call_args_list, [unittest.mock.call(True), unittest.mock.call(False)])


if __name__ == '__main__':
    unittest.main()
