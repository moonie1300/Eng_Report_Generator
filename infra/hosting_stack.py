"""Private S3 and CloudFront hosting for the Assignment 3 interface."""
from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    Tags,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3 as s3,
)
from constructs import Construct


class Assignment3HostingStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add('purpose', 'assessment 3')
        Tags.of(self).add('qut-username', 'n11180021@qut.edu.au')

        bucket = s3.Bucket(
            self,
            'SiteBucket',
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        distribution = cloudfront.Distribution(
            self,
            'SiteDistribution',
            default_root_object='index.html',
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
            ),
        )

        CfnOutput(self, 'SiteUrl', value=f'https://{distribution.distribution_domain_name}')
        CfnOutput(self, 'SiteBucketName', value=bucket.bucket_name)
        CfnOutput(self, 'DistributionId', value=distribution.distribution_id)
