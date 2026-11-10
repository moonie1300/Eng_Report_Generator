"""AWS resources: S3, DynamoDB, SQS, API Gateway + Lambda and ECS Fargate workers."""
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_sqs as sqs,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent
MAX_WORKERS = 3


class ReportGeneratorStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        bucket = s3.Bucket(
            self,
            'ReportBucket',
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(7))],
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.POST, s3.HttpMethods.GET],
                    allowed_origins=['*'],  # Restrict to the web interface origin once hosted.
                    allowed_headers=['*'],
                )
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        table = dynamodb.Table(
            self,
            'JobTable',
            partition_key=dynamodb.Attribute(name='job_id', type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute='expires_at',
            removal_policy=RemovalPolicy.DESTROY,
        )

        dead_letter_queue = sqs.Queue(self, 'JobDeadLetterQueue', retention_period=Duration.days(14))
        queue = sqs.Queue(
            self,
            'JobQueue',
            visibility_timeout=Duration.minutes(5),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dead_letter_queue),
        )

        api_function = lambda_.Function(
            self,
            'ApiFunction',
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler='handler.handler',
            code=lambda_.Code.from_asset(str(ROOT / 'api'), exclude=['__pycache__', '__init__.py']),
            timeout=Duration.seconds(10),
            environment={
                'BUCKET_NAME': bucket.bucket_name,
                'TABLE_NAME': table.table_name,
                'QUEUE_URL': queue.queue_url,
            },
        )
        bucket.grant_read_write(api_function)
        table.grant_read_write_data(api_function)
        queue.grant_send_messages(api_function)

        api = apigw.RestApi(
            self,
            'ReportApi',
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=['GET', 'POST', 'OPTIONS'],
            ),
        )
        integration = apigw.LambdaIntegration(api_function)
        jobs = api.root.add_resource('jobs')
        jobs.add_method('POST', integration)
        job = jobs.add_resource('{job_id}')
        job.add_method('GET', integration)
        job.add_resource('submit').add_method('POST', integration)

        # Public subnets without a NAT gateway keep the cost low; tasks need a public IP to reach AWS.
        vpc = ec2.Vpc(
            self,
            'WorkerVpc',
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name='public', subnet_type=ec2.SubnetType.PUBLIC)
            ],
        )
        workers = ecs_patterns.QueueProcessingFargateService(
            self,
            'Workers',
            vpc=vpc,
            queue=queue,
            image=ecs.ContainerImage.from_asset(
                str(ROOT),
                file='worker/Dockerfile',
                platform=ecr_assets.Platform.LINUX_AMD64,
            ),
            cpu=512,
            memory_limit_mib=1024,
            min_scaling_capacity=0,
            max_scaling_capacity=MAX_WORKERS,
            assign_public_ip=True,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            environment={
                'BUCKET_NAME': bucket.bucket_name,
                'TABLE_NAME': table.table_name,
                'QUEUE_URL': queue.queue_url,
            },
        )
        bucket.grant_read_write(workers.task_definition.task_role)
        table.grant_read_write_data(workers.task_definition.task_role)

        CfnOutput(self, 'ApiUrl', value=api.url)
        CfnOutput(self, 'BucketName', value=bucket.bucket_name)
