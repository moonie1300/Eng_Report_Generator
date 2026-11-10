import unittest
from unittest.mock import MagicMock, patch

from worker.monitor import main, snapshot


class MonitorTests(unittest.TestCase):
    def test_snapshot_reports_queue_service_and_jobs(self):
        sqs, ecs, table = MagicMock(), MagicMock(), MagicMock()
        sqs.get_queue_attributes.return_value = {'Attributes': {'ApproximateNumberOfMessages': '1'}}
        ecs.describe_services.return_value = {'services': [{
            'desiredCount': 3, 'runningCount': 2, 'pendingCount': 1
        }]}
        table.get_item.return_value = {'Item': {
            'status': 'COMPLETED', 'worker_id': 'task', 'report_key': 'report',
        }}
        result = snapshot(sqs, ecs, table, ['a' * 32], 'queue-url', 'dlq-url')
        self.assertEqual(result['service']['runningCount'], 2)
        self.assertEqual(result['jobs'][0]['status'], 'COMPLETED')
        self.assertEqual(result['jobs'][0]['worker_id'], 'task')
        self.assertEqual(sqs.get_queue_attributes.call_count, 2)
        self.assertEqual([call.kwargs['QueueUrl'] for call in sqs.get_queue_attributes.call_args_list],
                         ['queue-url', 'dlq-url'])

    def test_missing_service_is_an_error(self):
        sqs, ecs, table = MagicMock(), MagicMock(), MagicMock()
        sqs.get_queue_attributes.return_value = {'Attributes': {}}
        ecs.describe_services.return_value = {'services': [], 'failures': [{'reason': 'MISSING'}]}
        with self.assertRaisesRegex(RuntimeError, 'MISSING'):
            snapshot(sqs, ecs, table, [], 'queue-url', 'dlq-url')

    def test_main_resolves_each_queue_once_for_multiple_samples(self):
        session = MagicMock()
        sqs = session.client.return_value
        sqs.meta.region_name = 'ap-southeast-2'
        sqs.get_queue_url.side_effect = [
            {'QueueUrl': 'https://sqs.ap-southeast-2.amazonaws.com/901444280953/n11180021-a3-report-jobs'},
            {'QueueUrl': 'https://sqs.ap-southeast-2.amazonaws.com/901444280953/n11180021-a3-report-jobs-dlq'},
        ]
        with patch('worker.monitor.boto3.Session', return_value=session), \
             patch('worker.monitor.snapshot', return_value={}) as take_snapshot, \
             patch('worker.monitor.time.sleep'), \
             patch('builtins.print'), \
             patch('sys.argv', ['monitor', '--samples', '3']):
            main()
        self.assertEqual(sqs.get_queue_url.call_count, 2)
        self.assertEqual(take_snapshot.call_count, 3)
        self.assertEqual(take_snapshot.call_args_list[0].args[-2:],
                         take_snapshot.call_args_list[-1].args[-2:])


if __name__ == '__main__':
    unittest.main()
