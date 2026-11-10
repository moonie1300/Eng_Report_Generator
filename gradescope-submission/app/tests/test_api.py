import json
import os
import unittest
from unittest.mock import MagicMock, patch

try:
    from botocore.exceptions import ClientError
    from api import handler
except ImportError as exc:
    raise unittest.SkipTest(f'boto3 is required for API tests: {exc}')

JOB_ID = 'a' * 32


def event(method, resource, body=None, job_id=None):
    return {
        'httpMethod': method,
        'resource': resource,
        'pathParameters': {'job_id': job_id} if job_id else None,
        'body': json.dumps(body) if body is not None else None,
    }


class ApiHandlerTests(unittest.TestCase):
    def setUp(self):
        self.table, self.s3, self.sqs = MagicMock(), MagicMock(), MagicMock()
        environment = patch.dict(
            os.environ, {'BUCKET_NAME': 'bucket', 'QUEUE_URL': 'queue-url', 'TABLE_NAME': 'table'}
        )
        environment.start()
        self.addCleanup(environment.stop)
        for name, mock in (('_table', self.table), ('_s3', self.s3), ('_sqs', self.sqs)):
            patcher = patch.object(handler, name, return_value=mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, *args, **kwargs):
        response = handler.handler(event(*args, **kwargs), None)
        return response['statusCode'], json.loads(response['body'])

    def test_create_job_stores_limits_and_returns_presigned_upload(self):
        self.s3.generate_presigned_post.return_value = {'url': 'u', 'fields': {}}

        status, body = self.call('POST', '/jobs', {'filename': '../x.csv', 'max_voltage': '60'})

        self.assertEqual(status, 201)
        item = self.table.put_item.call_args.kwargs['Item']
        self.assertEqual(item['filename'], 'x.csv')
        self.assertEqual(item['max_voltage'], '60.0')
        self.assertNotIn('max_abs_current', item)
        self.assertEqual(item['status'], 'AWAITING_UPLOAD')
        self.assertEqual(body['upload'], {'url': 'u', 'fields': {}})

    def test_create_job_rejects_invalid_limits(self):
        for value in ('-1', '0', 'abc', 'inf'):
            with self.subTest(value=value):
                status, _ = self.call('POST', '/jobs', {'max_abs_current': value})
                self.assertEqual(status, 400)
        self.table.put_item.assert_not_called()

    def test_submit_queues_uploaded_job(self):
        self.table.get_item.return_value = {'Item': {'status': 'AWAITING_UPLOAD'}}

        status, body = self.call('POST', '/jobs/{job_id}/submit', job_id=JOB_ID)

        self.assertEqual((status, body['status']), (202, 'QUEUED'))
        self.sqs.send_message.assert_called_once_with(
            QueueUrl='queue-url', MessageBody=json.dumps({'job_id': JOB_ID})
        )

    def test_submit_requires_uploaded_csv(self):
        self.table.get_item.return_value = {'Item': {'status': 'AWAITING_UPLOAD'}}
        self.s3.head_object.side_effect = ClientError({'Error': {'Code': '404'}}, 'HeadObject')

        status, _ = self.call('POST', '/jobs/{job_id}/submit', job_id=JOB_ID)

        self.assertEqual(status, 400)
        self.sqs.send_message.assert_not_called()

    def test_submit_twice_is_rejected(self):
        self.table.get_item.return_value = {'Item': {'status': 'QUEUED'}}

        status, _ = self.call('POST', '/jobs/{job_id}/submit', job_id=JOB_ID)

        self.assertEqual(status, 409)
        self.sqs.send_message.assert_not_called()

    def test_get_completed_job_includes_metrics_and_download_url(self):
        self.table.get_item.return_value = {
            'Item': {
                'status': 'COMPLETED',
                'filename': 'run.csv',
                'report_key': f'reports/{JOB_ID}.pdf',
                'metrics': {'Test assessment': 'PASS'},
            }
        }
        self.s3.generate_presigned_url.return_value = 'https://download'

        status, body = self.call('GET', '/jobs/{job_id}', job_id=JOB_ID)

        self.assertEqual(status, 200)
        self.assertEqual(body['download_url'], 'https://download')
        self.assertEqual(body['metrics'], {'Test assessment': 'PASS'})

    def test_get_failed_job_returns_error(self):
        self.table.get_item.return_value = {'Item': {'status': 'FAILED', 'error': 'bad row'}}

        status, body = self.call('GET', '/jobs/{job_id}', job_id=JOB_ID)

        self.assertEqual((status, body['error']), (200, 'bad row'))

    def test_unknown_job_and_malformed_id_return_404(self):
        self.table.get_item.return_value = {}
        self.assertEqual(self.call('GET', '/jobs/{job_id}', job_id=JOB_ID)[0], 404)
        self.assertEqual(self.call('GET', '/jobs/{job_id}', job_id='../etc')[0], 404)

    def test_unknown_route_returns_404(self):
        self.assertEqual(self.call('DELETE', '/jobs')[0], 404)


if __name__ == '__main__':
    unittest.main()
