import unittest
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from worker.launcher import LOCK_ID, reconcile


CONFIG = {
    'queue_url': 'queue', 'cluster': 'cluster', 'task_definition': 'task:1',
    'subnet': 'subnet', 'security_group': 'sg',
}


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.table, self.sqs, self.ecs = MagicMock(), MagicMock(), MagicMock()
        self.table.get_item.return_value = {}
        self.sqs.get_queue_attributes.return_value = {
            'Attributes': {'ApproximateNumberOfMessages': '1'}
        }
        self.ecs.run_task.return_value = {'tasks': [{'taskArn': 'task-arn'}], 'failures': []}
        self.ecs.describe_services.return_value = {'services': [], 'failures': [{'reason': 'MISSING'}]}

    def reconcile(self):
        reconcile(self.table, self.sqs, self.ecs, CONFIG)

    def test_idle_queue_does_not_launch(self):
        self.sqs.get_queue_attributes.return_value['Attributes']['ApproximateNumberOfMessages'] = '0'
        self.reconcile()
        self.table.put_item.assert_not_called()
        self.ecs.run_task.assert_not_called()

    def test_scaling_service_or_unknown_state_prevents_launch(self):
        for response in (
            {'services': [{'status': 'ACTIVE'}], 'failures': []},
            {'services': [{'status': 'DRAINING'}], 'failures': []},
            {'services': [], 'failures': []},
            {'services': [], 'failures': [{'reason': 'ACCESS_DENIED'}]},
        ):
            with self.subTest(response=response):
                self.ecs.describe_services.return_value = response
                with self.assertRaisesRegex(RuntimeError, 'scaling service'):
                    self.reconcile()
        self.table.put_item.assert_not_called()
        self.ecs.run_task.assert_not_called()

    def test_service_appearing_after_reservation_prevents_launch(self):
        self.ecs.describe_services.side_effect = [
            {'services': [], 'failures': [{'reason': 'MISSING'}]},
            {'services': [{'status': 'ACTIVE'}], 'failures': []},
        ]
        with self.assertRaisesRegex(RuntimeError, 'scaling service'):
            self.reconcile()
        self.ecs.run_task.assert_not_called()

    def test_visible_work_reserves_and_starts_one_task(self):
        self.reconcile()
        item = self.table.put_item.call_args.kwargs['Item']
        self.assertEqual(item['job_id'], LOCK_ID)
        self.assertEqual(self.ecs.run_task.call_args.kwargs['clientToken'], item['launch_token'])
        self.assertEqual(self.ecs.run_task.call_args.kwargs['count'], 1)
        self.assertEqual(
            self.ecs.run_task.call_args.kwargs['networkConfiguration']['awsvpcConfiguration']['assignPublicIp'],
            'DISABLED',
        )
        self.assertEqual(self.table.update_item.call_args.kwargs['ExpressionAttributeValues'][':task'], 'task-arn')

    def test_running_task_blocks_additional_launch(self):
        self.table.get_item.return_value = {'Item': {'launch_token': 'token', 'task_arn': 'existing'}}
        self.ecs.describe_tasks.return_value = {'tasks': [{'lastStatus': 'RUNNING'}]}
        self.reconcile()
        self.ecs.run_task.assert_not_called()
        self.sqs.get_queue_attributes.assert_not_called()

    def test_stopped_task_unlocks_and_checks_queue(self):
        self.table.get_item.return_value = {'Item': {'launch_token': 'token', 'task_arn': 'existing'}}
        self.ecs.describe_tasks.return_value = {'tasks': [{
            'lastStatus': 'STOPPED',
            'containers': [{'name': 'report-worker', 'exitCode': 0}],
        }]}
        self.reconcile()
        self.table.delete_item.assert_called_once()
        self.ecs.run_task.assert_called_once()

    def test_failed_or_unknown_exit_blocks_new_tasks(self):
        self.table.get_item.return_value = {'Item': {'launch_token': 'token', 'task_arn': 'existing'}}
        for containers in ([{'name': 'report-worker', 'exitCode': 1}], []):
            with self.subTest(containers=containers):
                self.ecs.describe_tasks.return_value = {'tasks': [{
                    'lastStatus': 'STOPPED', 'containers': containers,
                }]}
                with self.assertRaisesRegex(RuntimeError, 'stopped unsuccessfully'):
                    self.reconcile()
        self.table.delete_item.assert_not_called()
        self.ecs.run_task.assert_not_called()

    def test_unknown_task_status_does_not_unlock_or_launch(self):
        self.table.get_item.return_value = {'Item': {'launch_token': 'token', 'task_arn': 'existing'}}
        self.ecs.describe_tasks.return_value = {'tasks': [], 'failures': [{'reason': 'MISSING'}]}
        with self.assertRaisesRegex(RuntimeError, 'Cannot determine'):
            self.reconcile()
        self.table.delete_item.assert_not_called()
        self.ecs.run_task.assert_not_called()

    def test_incomplete_launch_reuses_token_after_timeout(self):
        self.table.get_item.return_value = {'Item': {'job_id': LOCK_ID, 'launch_token': 'saved-token'}}
        self.ecs.run_task.side_effect = TimeoutError('response lost')
        with self.assertRaises(TimeoutError):
            self.reconcile()
        self.ecs.run_task.side_effect = None
        self.reconcile()
        self.assertEqual(
            [call.kwargs['clientToken'] for call in self.ecs.run_task.call_args_list],
            ['saved-token', 'saved-token'],
        )
        self.table.put_item.assert_not_called()

    def test_conditional_reservation_conflict_does_not_launch(self):
        self.table.put_item.side_effect = ClientError(
            {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem'
        )
        self.reconcile()
        self.ecs.run_task.assert_not_called()

    def test_ecs_failure_is_visible_and_reservation_remains(self):
        self.ecs.run_task.return_value = {'tasks': [], 'failures': [{'reason': 'RESOURCE'}]}
        with self.assertRaisesRegex(RuntimeError, 'RunTask'):
            self.reconcile()
        self.table.delete_item.assert_not_called()
        self.table.update_item.assert_not_called()


if __name__ == '__main__':
    unittest.main()
