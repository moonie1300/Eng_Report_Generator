import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from worker import queue_config, submit


QUEUE_URL = 'https://sqs.ap-southeast-2.amazonaws.com/901444280953/n11180021-a3-report-jobs'
DLQ_URL = 'https://sqs.ap-southeast-2.amazonaws.com/901444280953/n11180021-a3-report-jobs-dlq'


class QueueConfigTests(unittest.TestCase):
    def setUp(self):
        self.sqs = MagicMock()
        self.sqs.meta.region_name = queue_config.REGION
        self.sqs.get_queue_url.return_value = {'QueueUrl': QUEUE_URL}

    def test_resolves_exact_queue_and_owner(self):
        self.assertEqual(queue_config.resolve_queue_url(self.sqs), QUEUE_URL)
        self.sqs.get_queue_url.assert_called_once_with(
            QueueName=queue_config.QUEUE_NAME, QueueOwnerAWSAccountId=queue_config.ACCOUNT_ID
        )

    def test_resolves_dlq(self):
        self.sqs.get_queue_url.return_value = {'QueueUrl': DLQ_URL}
        self.assertEqual(queue_config.resolve_queue_url(self.sqs, queue_config.DLQ_NAME), DLQ_URL)

    def test_wrong_region_and_name_fail_before_lookup(self):
        self.sqs.meta.region_name = 'us-east-1'
        with self.assertRaisesRegex(ValueError, 'region'):
            queue_config.resolve_queue_url(self.sqs)
        self.sqs.meta.region_name = queue_config.REGION
        with self.assertRaisesRegex(ValueError, 'queue name'):
            queue_config.resolve_queue_url(self.sqs, 'wrong-queue')
        self.sqs.get_queue_url.assert_not_called()

    def test_inconsistent_resolved_urls_are_rejected(self):
        for url in (
            QUEUE_URL.replace('901444280953', '582641009890'),
            QUEUE_URL.replace('ap-southeast-2', 'us-east-1'),
            QUEUE_URL.replace('n11180021-a3-report-jobs', 'wrong-queue'),
            QUEUE_URL + '?unexpected=1',
            QUEUE_URL.replace('https://', 'http://'),
        ):
            with self.subTest(url=url):
                self.sqs.get_queue_url.return_value = {'QueueUrl': url}
                with self.assertRaisesRegex(ValueError, 'inconsistent'):
                    queue_config.resolve_queue_url(self.sqs)

    def test_aws_lookup_failure_is_not_silenced_or_retried(self):
        error = ClientError({'Error': {'Code': 'AWS.SimpleQueueService.NonExistentQueue',
                                       'Message': 'The specified queue does not exist.'}},
                            'GetQueueUrl')
        self.sqs.get_queue_url.side_effect = error
        with self.assertRaises(ClientError) as raised:
            queue_config.resolve_queue_url(self.sqs)
        self.assertIs(raised.exception, error)
        self.sqs.get_queue_url.assert_called_once()

    def test_submit_cli_rejects_mismatched_queue_before_writes(self):
        session = MagicMock()
        session.client.return_value = self.sqs
        with patch('worker.submit.boto3.Session', return_value=session), \
             patch('sys.argv', ['submit', 'a' * 32, '--bucket', 'bucket', '--table', 'table',
                                '--queue-url', QUEUE_URL.replace('901444280953', '582641009890')]), \
             patch('worker.submit.submit_job') as submit_job:
            with self.assertRaises(SystemExit):
                submit.main()
            submit_job.assert_not_called()
            session.resource.assert_not_called()

    def test_submit_cli_rejects_wrong_region_before_aws_calls(self):
        with patch('worker.submit.boto3.Session') as session, \
             patch('sys.argv', ['submit', 'a' * 32, '--bucket', 'bucket', '--table', 'table',
                                '--region', 'us-east-1']):
            with self.assertRaises(SystemExit):
                submit.main()
            session.assert_not_called()

    def test_read_only_preflight_rejects_wrong_caller_account(self):
        session = MagicMock()
        session.client.return_value.get_caller_identity.return_value = {'Account': '582641009890'}
        with patch('boto3.Session', return_value=session), self.assertRaisesRegex(ValueError, 'caller account'):
            queue_config.main()
        session.client.assert_called_once_with('sts')


if __name__ == '__main__':
    unittest.main()
