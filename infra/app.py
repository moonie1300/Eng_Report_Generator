#!/usr/bin/env python3
import aws_cdk as cdk

from stack import ReportGeneratorStack

app = cdk.App()
ReportGeneratorStack(app, 'EngReportGenerator')
app.synth()
