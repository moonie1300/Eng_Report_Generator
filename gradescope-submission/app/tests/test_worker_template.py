import re
import unittest
from pathlib import Path


class WorkerTemplateTests(unittest.TestCase):
    def test_existing_resources_are_referenced(self):
        body = (Path(__file__).resolve().parent.parent / 'infra' / 'one-shot-worker.yaml').read_text()
        for resource_type in ('AWS::DynamoDB::Table', 'AWS::SQS::Queue',
                              'AWS::ECR::Repository', 'AWS::IAM::Role',
                              'AWS::Logs::LogGroup'):
            self.assertNotIn(f'Type: {resource_type}', body)
        self.assertRegex(body, r"(?m)^  JobTableName:\n    Type: String\n    Default: n11180021-a3-report-jobs$")
        self.assertIn('arn:aws:iam::901444280953:role/Execution-Role-CAB432-ECS', body)
        self.assertIn('arn:aws:iam::901444280953:role/Task-Role-CAB432-ECS', body)
        self.assertIn('n11180021-a3-report-worker:v1', body)
        self.assertIn('ExecutionRoleArn: !Ref ExecutionRoleArn', body)
        self.assertIn('TaskRoleArn: !Ref TaskRoleArn', body)
        self.assertIn('Image: !Ref WorkerImage', body)
        self.assertIn('{Name: BUCKET_NAME, Value: !Ref UploadBucketName}', body)
        self.assertIn('{Name: TABLE_NAME, Value: !Ref JobTableName}', body)
        self.assertIn('{Name: QUEUE_URL, Value: !Ref JobQueueUrl}', body)
        self.assertRegex(body, r'(?m)^  TableName:\n    Value: !Ref JobTableName$')
        self.assertIn('Command: [python, -m, worker.main, --once]', body)
        self.assertIn("Cpu: '256'", body)
        self.assertIn("Memory: '512'", body)
        self.assertIn('CpuArchitecture: X86_64', body)
        self.assertIn('OperatingSystemFamily: LINUX', body)
        self.assertIn('LogDriver: awslogs', body)
        self.assertIn('awslogs-group: /ecs/n11180021-a3-report-worker', body)
        self.assertNotIn('!Ref Logs', body)
        self.assertNotIn('DependsOn: Logs', body)

    def test_only_requested_resources_are_created_and_tagged(self):
        body = (Path(__file__).resolve().parent.parent / 'infra' / 'one-shot-worker.yaml').read_text()
        self.assertEqual(
            re.findall(r'(?m)^    Type: (AWS::[^\s]+)$', body),
            ['AWS::ECS::Cluster', 'AWS::ECS::TaskDefinition'],
        )
        self.assertEqual(body.count('{Key: purpose, Value: assessment 3}'), 2)
        self.assertEqual(body.count('{Key: qut-username, Value: n11180021@qut.edu.au}'), 2)

    def test_scaling_template_reuses_resources_and_starts_disabled(self):
        body = (Path(__file__).resolve().parent.parent / 'infra' / 'scalable-worker.yaml').read_text()
        resources = re.findall(r'(?m)^    Type: (AWS::[^\s]+)$', body)
        self.assertEqual(resources, [
            'AWS::ECS::TaskDefinition', 'AWS::ECS::Service',
            'AWS::ApplicationAutoScaling::ScalableTarget',
            'AWS::ApplicationAutoScaling::ScalingPolicy',
            'AWS::ApplicationAutoScaling::ScalingPolicy',
            'AWS::CloudWatch::Alarm', 'AWS::CloudWatch::Alarm',
        ])
        for expected in (
            'service/n11180021-a3-report-cluster/${WorkerService.Name}',
            'arn:aws:iam::901444280953:role/Task-Role-CAB432-ECS',
            'arn:aws:iam::901444280953:role/Execution-Role-CAB432-ECS',
            'cab432-n11180021-reports', 'n11180021-a3-report-jobs',
            '/ecs/n11180021-a3-report-worker', 'subnet-04cc288ea3b2e1e53',
            'sg-078997505ad1c6bbc', 'AssignPublicIp: DISABLED',
            'sha256:699c1379506e10dab97c7f664e345088a15d08e4614bf6f56484f6718f8c4a13',
            'Command: [python, -m, worker.main]', 'MinCapacity: 0',
            'MaxCapacity: !Ref MaximumWorkers', 'AllowedValues: [1, 2, 3]',
            'DesiredCount: 0', "Default: 'false'",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, body)
        self.assertEqual(body.count('ActionsEnabled: !If [ScalingEnabled, true, false]'), 2)

    def test_idle_alarm_missing_data_is_non_idle_in_local_model(self):
        body = (Path(__file__).resolve().parent.parent / 'infra' / 'scalable-worker.yaml').read_text()
        idle = body.split('  QueueIdle:\n', 1)[1]
        self.assertIn("Expression: 'FILL(IF(visible == 0, IF(inflight == 0, 1, 0), 0), 0)'", idle)
        self.assertIn('TreatMissingData: notBreaching', idle)
        self.assertIn('Threshold: 1', idle)
        self.assertIn('ComparisonOperator: GreaterThanOrEqualToThreshold', idle)
        self.assertIn('EvaluationPeriods: 5', idle)
        scale_in = body.split('  ScaleIn:\n', 1)[1].split('  QueueBacklog:\n', 1)[0]
        self.assertIn('MetricIntervalLowerBound: 0, ScalingAdjustment: -1', scale_in)
        for visible, inflight, idle_breach in (
            (None, None, False), (None, 0, False), (0, None, False),
            (None, 4, False), (4, None, False), (0, 0, True),
            (0, 1, False), (1, 0, False), (1, 1, False),
        ):
            with self.subTest(visible=visible, inflight=inflight):
                result = int(visible == 0 and inflight == 0)
                self.assertEqual(result >= 1, idle_breach)

    def test_scale_out_steps_grow_with_backlog(self):
        body = (Path(__file__).resolve().parent.parent / 'infra' / 'scalable-worker.yaml').read_text()
        scale_out = body.split('  ScaleOut:\n', 1)[1].split('  ScaleIn:\n', 1)[0]
        for step in (
            'MetricIntervalLowerBound: 0, MetricIntervalUpperBound: 2, ScalingAdjustment: 1',
            'MetricIntervalLowerBound: 2, MetricIntervalUpperBound: 5, ScalingAdjustment: 2',
            'MetricIntervalLowerBound: 5, ScalingAdjustment: 3',
        ):
            self.assertIn(step, scale_out)


if __name__ == '__main__':
    unittest.main()
