import http.client
import json
import threading
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    from cloud_jobs import BUCKET, LOCK_ID, CloudJobs
    from worker.main import process_job
    from tests.fake_job_table import FakeJobTable
    import webapp
except ImportError as exc:
    raise unittest.SkipTest(f'boto3 and reportlab are required: {exc}')

JOB = '12345678-1234-1234-1234-123456789abc'
QUEUE = 'https://sqs.ap-southeast-2.amazonaws.com/901444280953/n11180021-a3-report-jobs'


class CloudJobTests(unittest.TestCase):
    def setUp(self):
        self.cloud = CloudJobs.__new__(CloudJobs)
        self.cloud.s3, self.cloud.sqs = MagicMock(), MagicMock()
        self.cloud.table, self.cloud.ecs = MagicMock(), MagicMock()
        self.cloud.table.get_item.return_value = {}
        self.cloud.ecs.describe_services.return_value = {'services': [], 'failures': [{'reason': 'MISSING'}]}
        self.cloud.sqs.meta.region_name = 'ap-southeast-2'
        self.cloud.sqs.get_queue_url.return_value = {'QueueUrl': QUEUE}

    def test_submit_uploads_then_uses_existing_submitter(self):
        with patch('cloud_jobs.submit_job') as submit:
            result = self.cloud.submit(JOB, 'timestamp,voltage_v\n')
        self.assertEqual(result['status'], 'QUEUED')
        self.cloud.s3.put_object.assert_called_once_with(
            Bucket=BUCKET, Key=f'uploads/{JOB}.csv', Body=b'timestamp,voltage_v\n',
            ContentType='text/csv', IfNoneMatch='*',
        )
        submit.assert_called_once_with(JOB, BUCKET, self.cloud.table, self.cloud.sqs, QUEUE, self.cloud.s3)
        self.cloud.sqs.get_queue_url.assert_called_once_with(
            QueueName='n11180021-a3-report-jobs', QueueOwnerAWSAccountId='901444280953'
        )

    def test_queue_resolution_failure_precedes_upload(self):
        self.cloud.sqs.get_queue_url.side_effect = RuntimeError('queue lookup failed')
        with self.assertRaisesRegex(RuntimeError, 'queue lookup failed'):
            self.cloud.submit(JOB, 'csv')
        self.cloud.s3.put_object.assert_not_called()
        self.cloud.table.put_item.assert_not_called()

    def test_duplicate_and_invalid_ids_do_not_upload(self):
        self.cloud.table.get_item.return_value = {'Item': {'status': 'QUEUED'}}
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.cloud.submit(JOB, 'csv')
        with self.assertRaises(ValueError):
            self.cloud.submit('../bad', 'csv')
        self.cloud.s3.put_object.assert_not_called()

    def test_submit_validates_limits_before_any_aws_operation(self):
        for value in ('0', '-1', 'NaN', 'Infinity', 'abc', [], True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.cloud.submit(JOB, 'csv', max_voltage=value)
        self.cloud.table.get_item.assert_not_called()
        self.cloud.s3.put_object.assert_not_called()

    def test_submit_forwards_optional_limits(self):
        with patch('cloud_jobs.submit_job') as submit:
            self.cloud.submit(JOB, 'csv', '60', '40.5')
        submit.assert_called_once_with(JOB, BUCKET, self.cloud.table, self.cloud.sqs,
                                       QUEUE, self.cloud.s3, max_voltage=60.0, max_abs_current=40.5)

    def test_status_and_completed_report(self):
        self.cloud.table.get_item.return_value = {
            'Item': {'status': 'COMPLETED', 'report_key': f'reports/{JOB}.pdf'}
        }
        self.cloud.s3.get_object.return_value = {'Body': BytesIO(b'%PDF-1.4 test')}
        self.assertEqual(self.cloud.status(JOB)['status'], 'COMPLETED')
        self.assertEqual(self.cloud.report(JOB), b'%PDF-1.4 test')
        self.cloud.s3.get_object.assert_called_once_with(Bucket=BUCKET, Key=f'reports/{JOB}.pdf')

    def test_cannot_read_other_key_or_unfinished_report(self):
        self.cloud.table.get_item.return_value = {'Item': {'status': 'COMPLETED', 'report_key': 'other.pdf'}}
        with self.assertRaisesRegex(ValueError, 'not ready'):
            self.cloud.report(JOB)
        self.cloud.s3.get_object.assert_not_called()

    def test_reads_only_committed_attempt_report(self):
        key = f'reports/{JOB}/' + 'a' * 32 + '.pdf'
        self.cloud.table.get_item.return_value = {'Item': {'status': 'COMPLETED', 'report_key': key}}
        self.cloud.s3.get_object.return_value = {'Body': BytesIO(b'%PDF-new')}
        self.assertEqual(self.cloud.report(JOB), b'%PDF-new')
        self.cloud.s3.get_object.assert_called_once_with(Bucket=BUCKET, Key=key)

    def test_start_worker_reserves_and_starts_one_task(self):
        self.cloud.table.get_item.side_effect = [{'Item': {'status': 'QUEUED'}}, {}]
        self.cloud.ecs.run_task.return_value = {'tasks': [{'taskArn': 'task-arn'}], 'failures': []}
        self.assertEqual(self.cloud.start_worker(JOB), {'task_arn': 'task-arn'})
        reservation = self.cloud.table.put_item.call_args.kwargs
        self.assertEqual(reservation['Item']['job_id'], LOCK_ID)
        self.assertEqual(reservation['ConditionExpression'], 'attribute_not_exists(job_id)')
        run = self.cloud.ecs.run_task.call_args.kwargs
        self.assertEqual(run['count'], 1)
        self.assertEqual(run['clientToken'], reservation['Item']['launch_token'])
        self.assertEqual(run['networkConfiguration']['awsvpcConfiguration']['assignPublicIp'], 'DISABLED')
        self.cloud.table.update_item.assert_called_once()

    def test_service_mode_blocks_manual_worker_launch(self):
        with patch.dict('os.environ', {'REPORT_SCALING_SERVICE': '1'}):
            with self.assertRaisesRegex(ValueError, 'disabled'):
                self.cloud.start_worker(JOB)
        self.cloud.table.get_item.assert_not_called()
        self.cloud.ecs.run_task.assert_not_called()

    def test_scaling_service_blocks_manual_launch_even_at_zero(self):
        self.cloud.ecs.describe_services.return_value = {
            'services': [{'status': 'ACTIVE', 'desiredCount': 0}], 'failures': []
        }
        with self.assertRaisesRegex(RuntimeError, 'scaling service'):
            self.cloud.start_worker(JOB)
        self.cloud.table.put_item.assert_not_called()
        self.cloud.ecs.run_task.assert_not_called()

    def test_uncertain_scaling_service_blocks_manual_launch(self):
        self.cloud.ecs.describe_services.return_value = {'services': [], 'failures': []}
        with self.assertRaisesRegex(RuntimeError, 'status is uncertain'):
            self.cloud.start_worker(JOB)
        self.cloud.ecs.run_task.assert_not_called()

    def test_service_appearing_before_launch_blocks_manual_run_task(self):
        self.cloud.table.get_item.side_effect = [{'Item': {'status': 'QUEUED'}}, {}]
        self.cloud.ecs.describe_services.side_effect = [
            {'services': [], 'failures': [{'reason': 'MISSING'}]},
            {'services': [{'status': 'ACTIVE'}], 'failures': []},
        ]
        with self.assertRaisesRegex(RuntimeError, 'scaling service'):
            self.cloud.start_worker(JOB)
        self.cloud.ecs.run_task.assert_not_called()

    def test_uncertain_launch_is_not_retried(self):
        self.cloud.table.get_item.side_effect = [
            {'Item': {'status': 'QUEUED'}}, {'Item': {'launch_token': 'token'}}
        ]
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            self.cloud.start_worker(JOB)
        self.cloud.ecs.run_task.assert_not_called()

    def test_failed_launch_leaves_reservation(self):
        self.cloud.table.get_item.side_effect = [{'Item': {'status': 'QUEUED'}}, {}]
        self.cloud.ecs.run_task.side_effect = RuntimeError('network failure')
        with self.assertRaisesRegex(RuntimeError, 'reservation remains'):
            self.cloud.start_worker(JOB)
        self.cloud.table.delete_item.assert_not_called()
        self.cloud.table.update_item.assert_not_called()

    def test_running_worker_blocks_new_task(self):
        self.cloud.table.get_item.side_effect = [
            {'Item': {'status': 'QUEUED'}},
            {'Item': {'launch_token': 'token', 'task_arn': 'task'}},
        ]
        self.cloud.ecs.describe_tasks.return_value = {'tasks': [{'lastStatus': 'RUNNING'}]}
        with self.assertRaisesRegex(ValueError, 'already running'):
            self.cloud.start_worker(JOB)
        self.cloud.ecs.run_task.assert_not_called()

    def test_completed_worker_reservation_is_replaced(self):
        self.cloud.table.get_item.side_effect = [
            {'Item': {'status': 'QUEUED'}},
            {'Item': {'launch_token': 'old', 'task_arn': 'previous-task'}},
        ]
        self.cloud.ecs.describe_tasks.return_value = {'tasks': [{
            'lastStatus': 'STOPPED', 'containers': [{'name': 'report-worker', 'exitCode': 0}]
        }]}
        self.cloud.ecs.run_task.return_value = {'tasks': [{'taskArn': 'new-task'}]}
        self.assertEqual(self.cloud.start_worker(JOB), {'task_arn': 'new-task'})
        self.cloud.table.delete_item.assert_called_once()
        self.cloud.table.put_item.assert_called_once()

    def test_failed_worker_preserves_reservation(self):
        self.cloud.table.get_item.side_effect = [
            {'Item': {'status': 'QUEUED'}},
            {'Item': {'launch_token': 'old', 'task_arn': 'previous-task'}},
        ]
        self.cloud.ecs.describe_tasks.return_value = {'tasks': [{
            'lastStatus': 'STOPPED', 'containers': [{'name': 'report-worker', 'exitCode': 1}]
        }]}
        with self.assertRaisesRegex(ValueError, 'previous worker failed'):
            self.cloud.start_worker(JOB)
        self.cloud.table.delete_item.assert_not_called()
        self.cloud.ecs.run_task.assert_not_called()


class CloudHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = webapp.ThreadingHTTPServer(('127.0.0.1', 0), webapp.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        webapp.STATUS_CACHE.reset()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_page_and_local_pdf_page_exist(self):
        for path in ('/', '/cloud', '/local', '/static/cloud-jobs.js'):
            self.assertEqual(self.request('GET', path)[0], 200)
        self.assertIn(b'Engineering Test Report Generator', self.request('GET', '/')[1])
        self.assertIn(b'Local PDF report', self.request('GET', '/local')[1])

    def test_cross_origin_and_missing_header_block_cloud_mutations(self):
        payload = json.dumps({'job_id': JOB, 'csv': 'csv'})
        self.assertEqual(self.request('POST', '/cloud/jobs', payload,
                                     {'Content-Type': 'application/json'})[0], 403)
        self.assertEqual(self.request('POST', '/cloud/jobs', payload,
                                     {'Content-Type': 'application/json', 'X-Local-Report': '1',
                                      'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.request('GET', f'/cloud/jobs/{JOB}',
                                     headers={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.request('POST', '/cloud/cache/reset', '{}',
                                     {'Content-Type': 'application/json'})[0], 403)
        self.assertEqual(self.request('GET', '/cloud/cache',
                                     headers={'Host': 'attacker.example'})[0], 403)

    def test_completed_status_cache_and_reset_through_http(self):
        cloud = MagicMock()
        cloud.status.return_value = {'job_id': JOB, 'status': 'COMPLETED'}
        with patch('cloud_jobs.CloudJobs', return_value=cloud) as constructor:
            for _ in range(2):
                code, body = self.request('GET', f'/cloud/jobs/{JOB}')
                self.assertEqual((code, json.loads(body)['status']), (200, 'COMPLETED'))
            self.assertEqual(constructor.call_count, 1)
            self.assertEqual(cloud.status.call_count, 1)
            stats = json.loads(self.request('GET', '/cloud/cache')[1])
            self.assertEqual((stats['misses'], stats['hits'], stats['dynamodb_reads']), (1, 1, 1))
            code, body = self.request('POST', '/cloud/cache/reset', '{}',
                                      {'Content-Type': 'application/json', 'X-Local-Report': '1'})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)['cached_entries'], 0)
            self.request('GET', f'/cloud/jobs/{JOB}')
            self.assertEqual(cloud.status.call_count, 2)

    def test_non_completed_http_statuses_always_read(self):
        cloud = MagicMock()
        cloud.status.side_effect = [
            {'job_id': JOB, 'status': status}
            for status in ('QUEUED', 'PROCESSING', 'FAILED')
        ]
        with patch('cloud_jobs.CloudJobs', return_value=cloud):
            for expected in ('QUEUED', 'PROCESSING', 'FAILED'):
                code, body = self.request('GET', f'/cloud/jobs/{JOB}')
                self.assertEqual((code, json.loads(body)['status']), (200, expected))
        stats = json.loads(self.request('GET', '/cloud/cache')[1])
        self.assertEqual((stats['misses'], stats['hits'], stats['dynamodb_reads']), (3, 0, 3))

    def test_job_status_and_pdf_routes(self):
        cloud = MagicMock()
        cloud.submit.return_value = {'job_id': JOB, 'status': 'QUEUED'}
        cloud.status.side_effect = [
            {'job_id': JOB, 'status': 'QUEUED'}, {'job_id': JOB, 'status': 'PROCESSING'},
            {'job_id': JOB, 'status': 'COMPLETED'},
        ]
        cloud.report.return_value = b'%PDF-test'
        with patch('cloud_jobs.CloudJobs', return_value=cloud):
            code, body = self.request('POST', '/cloud/jobs', json.dumps({
                'job_id': JOB, 'csv': 'csv', 'max_voltage': '60', 'max_abs_current': '40'
            }),
                                      {'Content-Type': 'application/json', 'X-Local-Report': '1'})
            self.assertEqual((code, json.loads(body)['status']), (200, 'QUEUED'))
            cloud.submit.assert_called_once_with(JOB, 'csv', '60', '40')
            for expected in ('QUEUED', 'PROCESSING', 'COMPLETED'):
                code, body = self.request('GET', f'/cloud/jobs/{JOB}')
                self.assertEqual((code, json.loads(body)['status']), (200, expected))
            self.assertEqual(self.request('GET', f'/cloud/jobs/{JOB}/report'), (200, b'%PDF-test'))

    def test_http_submission_limits_flow_through_queue_to_pdf(self):
        cloud = CloudJobs.__new__(CloudJobs)
        cloud.s3, cloud.sqs, cloud.table = MagicMock(), MagicMock(), FakeJobTable()
        cloud.sqs.meta.region_name = 'ap-southeast-2'
        cloud.sqs.get_queue_url.return_value = {'QueueUrl': QUEUE}
        objects = {}
        cloud.s3.put_object.side_effect = lambda **kwargs: objects.update({kwargs['Key']: kwargs['Body']})
        cloud.s3.download_file.side_effect = lambda bucket, key, path: Path(path).write_bytes(objects[key])
        cloud.s3.upload_file.side_effect = lambda path, bucket, key, **kwargs: objects.update({key: Path(path).read_bytes()})
        cloud.s3.get_object.side_effect = lambda **kwargs: {'Body': BytesIO(objects[kwargs['Key']])}
        csv = (Path(__file__).resolve().parent.parent / 'Sample_Data' / 'test_normal.csv').read_text()

        with patch('cloud_jobs.CloudJobs', return_value=cloud):
            code, body = self.request('POST', '/cloud/jobs', json.dumps({
                'job_id': JOB, 'csv': csv, 'max_voltage': '1', 'max_abs_current': '100'
            }), {'Content-Type': 'application/json', 'X-Local-Report': '1'})
            self.assertEqual((code, json.loads(body)['status']), (200, 'QUEUED'))
            message = json.loads(cloud.sqs.send_message.call_args.kwargs['MessageBody'])
            self.assertEqual(message, {'job_id': JOB})
            stored = cloud.table.items[JOB]
            self.assertEqual((stored['max_voltage'], stored['max_abs_current']), ('1.0', '100.0'))
            process_job(message['job_id'], cloud.table, cloud.s3, BUCKET)
            self.assertEqual(cloud.table.statuses, ['PROCESSING', 'COMPLETED'])
            self.assertEqual(stored['metrics']['Test assessment'], 'FAIL')
            self.assertEqual(json.loads(self.request('GET', f'/cloud/jobs/{JOB}')[1])['status'], 'COMPLETED')
            code, pdf = self.request('GET', f'/cloud/jobs/{JOB}/report')
            self.assertEqual(code, 200)
            self.assertTrue(pdf.startswith(b'%PDF'))


if __name__ == '__main__':
    unittest.main()
