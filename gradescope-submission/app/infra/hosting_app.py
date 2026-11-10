"""Synthesize the standalone Assignment 3 static hosting stack."""
import aws_cdk as cdk

from hosting_stack import Assignment3HostingStack

app = cdk.App()
Assignment3HostingStack(
    app,
    'n11180021-a3-hosting',
    synthesizer=cdk.BootstraplessSynthesizer(),
)
app.synth()
