import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

try:
    from botocore.exceptions import ClientError
    from worker.submit import submit_job
    from worker.main import process_job
    from tests.fake_job_table import FakeJobTable
except ImportError as exc:
    raise unittest.SkipTest(f'boto3 is required for submit tests: {exc}')

JOB_ID = '12345678-1234-1234-1234-123456789abc'


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.table, self.sqs, self.s3 = MagicMock(), MagicMock(), MagicMock()

    def submit(self, job_id=JOB_ID):
        return submit_job(job_id, 'bucket', self.table, self.sqs, 'queue', self.s3)

    def test_submit_checks_upload_creates_job_and_queues(self):
        self.assertEqual(self.submit(), JOB_ID)
        self.s3.head_object.assert_called_once_with(
            Bucket='bucket', Key=f'uploads/{JOB_ID}.csv'
        )
        self.assertEqual(self.table.put_item.call_args.kwargs['Item']['status'], 'QUEUED')
        self.sqs.send_message.assert_called_once_with(
            QueueUrl='queue', MessageBody=json.dumps({'job_id': JOB_ID})
        )

    def test_missing_upload_is_not_queued(self):
        self.s3.head_object.side_effect = ClientError(
            {'Error': {'Code': '404', 'Message': 'Not found'}}, 'HeadObject'
        )
        with self.assertRaises(ClientError):
            self.submit()
        self.table.put_item.assert_not_called()
        self.sqs.send_message.assert_not_called()

    def test_retry_queued_job_after_send_failure(self):
        self.sqs.send_message.side_effect = ClientError(
            {'Error': {'Code': 'ServiceUnavailable', 'Message': 'Unavailable'}}, 'SendMessage'
        )
        with self.assertRaises(ClientError):
            self.submit()
        self.sqs.send_message.side_effect = None
        self.table.put_item.side_effect = ClientError(
            {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem'
        )
        self.table.get_item.return_value = {'Item': {'status': 'QUEUED'}}
        self.assertEqual(self.submit(), JOB_ID)
        self.assertEqual(self.sqs.send_message.call_count, 2)

    def test_finished_job_cannot_be_submitted_again(self):
        self.table.put_item.side_effect = ClientError(
            {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem'
        )
        self.table.get_item.return_value = {'Item': {'status': 'COMPLETED'}}
        with self.assertRaisesRegex(ValueError, 'not queued'):
            self.submit()
        self.sqs.send_message.assert_not_called()

    def test_limits_are_stored_as_strings_and_message_stays_compatible(self):
        submit_job(JOB_ID, 'bucket', self.table, self.sqs, 'queue', self.s3,
                   max_voltage='60.25', max_abs_current='40')
        item = self.table.put_item.call_args.kwargs['Item']
        self.assertEqual(item['max_voltage'], '60.25')
        self.assertEqual(item['max_abs_current'], '40.0')
        self.assertEqual(json.loads(self.sqs.send_message.call_args.kwargs['MessageBody']),
                         {'job_id': JOB_ID})

    def test_changed_limits_cannot_be_silently_discarded_on_retry(self):
        self.table.put_item.side_effect = ClientError(
            {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem'
        )
        self.table.get_item.return_value = {'Item': {'status': 'QUEUED', 'max_voltage': '60.0'}}
        with self.assertRaisesRegex(ValueError, 'different assessment limits'):
            submit_job(JOB_ID, 'bucket', self.table, self.sqs, 'queue', self.s3, max_voltage='50')
        self.sqs.send_message.assert_not_called()

    def test_invalid_id_cannot_be_used_as_s3_key(self):
        with self.assertRaises(ValueError):
            self.submit('../other')
        self.s3.head_object.assert_not_called()

    def test_submit_then_generate_pdf_from_uploaded_csv(self):
        self.table = FakeJobTable()
        sample = Path(__file__).resolve().parent.parent / 'Sample_Data' / 'test_normal.csv'
        self.s3.download_file.side_effect = (
            lambda bucket, key, destination: Path(destination).write_bytes(sample.read_bytes())
        )
        pdfs = {}
        self.s3.upload_file.side_effect = (
            lambda filename, bucket, key, **kwargs: pdfs.update({key: Path(filename).read_bytes()})
        )

        self.submit()
        job_id = json.loads(self.sqs.send_message.call_args.kwargs['MessageBody'])['job_id']
        process_job(job_id, self.table, self.s3, 'bucket')

        stored = self.table.items[JOB_ID]
        self.assertEqual(stored['status'], 'COMPLETED')
        self.assertTrue(stored['report_key'].startswith(f'reports/{JOB_ID}/'))
        self.assertTrue(pdfs[stored['report_key']].startswith(b'%PDF'))
        self.assertEqual(stored['metrics']['Test assessment'], 'NOT ASSESSED')

    def test_submit_limits_reach_worker_and_change_assessment(self):
        for limits, expected in (
            ({}, 'NOT ASSESSED'),
            ({'max_voltage': '100'}, 'PARTIALLY ASSESSED'),
            ({'max_voltage': '100', 'max_abs_current': '100'}, 'PASS'),
            ({'max_voltage': '1', 'max_abs_current': '100'}, 'FAIL'),
            ({'max_voltage': '100', 'max_abs_current': '1'}, 'FAIL'),
        ):
            with self.subTest(limits=limits):
                table, sqs, s3 = FakeJobTable(), MagicMock(), MagicMock()
                pdfs = {}
                sample = Path(__file__).resolve().parent.parent / 'Sample_Data' / 'test_normal.csv'
                s3.download_file.side_effect = lambda bucket, key, destination: Path(destination).write_bytes(sample.read_bytes())
                s3.upload_file.side_effect = lambda filename, bucket, key, **kwargs: pdfs.update({key: Path(filename).read_bytes()})
                submit_job(JOB_ID, 'bucket', table, sqs, 'queue', s3, **limits)
                stored = table.items[JOB_ID]
                self.assertEqual(stored['status'], 'QUEUED')
                message = json.loads(sqs.send_message.call_args.kwargs['MessageBody'])
                process_job(message['job_id'], table, s3, 'bucket')
                self.assertEqual(table.statuses, ['PROCESSING', 'COMPLETED'])
                self.assertEqual(stored['metrics']['Test assessment'], expected)
                self.assertTrue(pdfs[stored['report_key']].startswith(b'%PDF'))
                for key, value in limits.items():
                    self.assertEqual(float(stored[key]), float(value))


if __name__ == '__main__':
    unittest.main()
