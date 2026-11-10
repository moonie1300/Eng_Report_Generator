# Eng_Report_Generator

1. Run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py ./Sample_Data/sample_battery_test.csv \
  --max-voltage 60 --max-abs-current 40 -o engineering_report.pdf
open engineering_report.pdf
```

Use `python main.py your_file.csv -o my_report.pdf` for your own test data.
The optional `--max-voltage` and `--max-abs-current` limits are used to assess
the test: exceeding either supplied limit results in `FAIL`; otherwise the
assessment is `PASS` when both limits are supplied, or `PARTIALLY ASSESSED`
when only one limit is supplied. The current limit applies to both charging
and discharging. If neither limit is supplied, the report says `NOT ASSESSED`.
The example limits above are 60 V and 40 A; set limits appropriate to your
equipment before relying on an assessment.

For the local PDF development/testing interface (no AWS dependencies):

```bash
python webapp.py
```

Then open http://127.0.0.1:8000/local, choose a CSV, optionally enter the voltage and
current limits, and download the generated PDF. Use `--port` to change the port.

### Local interface for cloud report jobs

Install the cloud dependencies and configure QUT AWS credentials locally:

```bash
python -m pip install -r worker/requirements.txt
python webapp.py
```

Open http://127.0.0.1:8000 (the existing `/cloud` address also works). Choose a
UTF-8 CSV, optionally provide voltage and absolute-current limits, and select
**Upload and submit job**. The local server uploads it to the existing S3
bucket and uses `worker.submit` to create the DynamoDB job and send its ID to
SQS. Valid supplied limits are stored as strings on the DynamoDB job; the
worker reads and applies them. No limits means `NOT ASSESSED`; a single passing
limit means `PARTIALLY ASSESSED`, and any exceeded supplied limit means `FAIL`.
The page automatically polls DynamoDB through the local server and highlights
only the actual reported `QUEUED`, `PROCESSING`, or `COMPLETED` status; a PDF
download appears when complete. Polling may skip a status if the worker
transitions between polls. `FAILED` shows the worker's validation error.

#### Demonstrating the completed-job cache

The local Python server caches the completed **status response** in memory for
60 seconds by default. Change the TTL at startup, for example:

```bash
python webapp.py --cache-ttl 10
```

The TTL must be finite and greater than zero. To demonstrate using the default
60-second TTL and an existing completed job (no new job or worker is needed):

1. Start `python webapp.py --cache-ttl 60` and open
   http://127.0.0.1:8000. In **Cache demonstration**, select **Reset statistics
   and clear cache**. This resets both entries and counters.
2. Enter an existing completed job ID, such as
   `ed169986-048a-4250-9a3f-a82fc73c53fe` if it still exists, and select
   **Check job**. Select **Show cache statistics**: expect one miss, one
   DynamoDB read and zero hits.
3. Before 60 seconds pass, select **Refresh status**, then **Show cache
   statistics**. Expect one hit, one miss and one DynamoDB read. The last
   response is a hit; no DynamoDB status read was made for it.
4. Wait at least 60 seconds after the first lookup (hits do not extend the
   TTL), select **Refresh status**, then **Show cache statistics**. Expect one
   hit, two misses and two DynamoDB reads. The last response is a miss. Times
   are measured values, not guaranteed performance improvements.

The counters cover only status lookups through `GET /cloud/jobs/{job_id}`:
hits, misses, attempted DynamoDB status reads, failed lookups, and average
successful hit/miss response times plus the last lookup time. A miss can fail
before a DynamoDB read, or during one; failed lookups do not enter the
successful averages. Timings are server-side milliseconds including waiting
for a concurrent lookup, excluding network transfer to the browser. Refreshing
statistics does not itself look up a job. PDF downloads, submission checks,
worker-launch checks and other AWS calls are not counted or cached.

This cache is per Python process, not shared across instances; restarting the
server clears entries and statistics. Entries expire based on monotonic time,
and at most 512 completed statuses are retained (least recently used is
evicted). `QUEUED`, `PROCESSING`, `FAILED`, missing jobs and errors are never
cached. A completed job changed or deleted outside this server can still be
shown as completed until its TTL expires or the cache is reset. Reset also
prevents in-flight pre-reset lookups from repopulating the cache or counters.
This is a local demonstration, not distributed caching.

The older automatic launcher is not deployed. For a queued job, **Start one
Fargate worker** asks for confirmation and, when the scaling service is absent,
starts the existing revision 1 one-shot task. The backend refuses this launch
while the scaling service exists or its status is uncertain. This incurs AWS
charges when launched. It processes the next message in the shared
queue, which is not necessarily the job shown. Run only one worker at a time
and check the queue before a demonstration. Keep the prepared automatic
launcher disabled; do not launch workers outside this interface at the same
time. The interface uses the same DynamoDB launch reservation as that
launcher and refuses to retry an uncertain launch. If a launch fails or its
outcome is unknown, inspect ECS, task logs and `_report_worker_launcher` before
an administrator or operator resolves the reservation; do not blindly retry.

The server binds to `127.0.0.1`, uses your AWS credentials through boto3, and
does not send credentials to browser JavaScript. Anyone with access to your
local computer and browser can use those credentials through the interface;
do not expose the server to a network. The credentials need S3 `PutObject`
and `GetObject`, DynamoDB `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`,
SQS `SendMessage`, ECS `RunTask`, `DescribeTasks`, `TagResource`, and
`iam:PassRole` for the existing task and execution roles. The worker's own
roles require the permissions listed below. No new AWS resources are created.

Submission spans S3, DynamoDB and SQS and is not atomic. If it reports an error
or the browser loses the response, preserve the displayed job ID and inspect
S3, the job record and queue before retrying; a queued record alone does not
prove that a message was sent. The most recent job ID is retained in the
browser tab's session storage for status checks; enter a job ID to reopen it
later. Local server access is not Cognito authentication; the existing Cognito
upload remains a separate flow.

On 8 November 2026, a test through these local HTTP endpoints queued job
`ed169986-048a-4250-9a3f-a82fc73c53fe` and started one task on revision 1.
The job reached `COMPLETED`, the worker exited successfully, and the downloaded
PDF was validated as one page. Polling observed `QUEUED` and `COMPLETED` but
did not capture `PROCESSING`. The test used the deployed `v1` image, not the
locally prepared `v2` image.

The local `v1` image matches the current ECR `v1` digest, and offline tests of
that image confirmed it reads both DynamoDB limit fields and applies them. The
deployed task definition `n11180021-a3-report-worker:1` references `v1`; no
image push or task-definition update is required for these frontend changes,
provided that mutable tag is not replaced. This limits workflow has not yet
been tested end to end in AWS. The `v2` image remains local and undeployed.

### AWS CSV upload

The **Upload to AWS** section in `static/index.html` targets the existing
`GET /a3/upload-url` API. It uses Cognito managed login with authorization code
and PKCE, sends the access token to the API, and uploads the file directly to
the returned S3 URL with `PUT` and `Content-Type: text/csv`. A successful upload
shows the job ID; it does **not** start report processing.

The public Cognito client and endpoints are configured in `static/index.html`.
The app client's currently registered callback is
`https://d84l1y8p4kdic.cloudfront.net`. Publish `static/index.html` at that
origin's root and `static/cloud-upload.js` at `/static/cloud-upload.js`. Use
that callback URL to sign in: the localhost page cannot complete the registered
callback without adding and configuring a separate localhost callback in
Cognito. The access token is stored in this tab's session storage until it
expires or **Clear sign-in** is selected. Clearing it does not sign out the
Cognito managed-login session; configure a Cognito logout URL and flow for that.

As checked on 8 November 2026, the deployed HTTP API and S3 bucket have **no
CORS configuration**, so a browser cannot complete the upload yet. Configure:

- API Gateway HTTP API `60cz77rxmk`: allow origin
  `https://d84l1y8p4kdic.cloudfront.net`, method `GET`, and request header
  `Authorization`. The browser's preflight uses `OPTIONS`.
- S3 bucket `cab432-n11180021-reports`: allow the same origin, method `PUT`,
  and request header `Content-Type`.

Apply and verify these settings in AWS before testing with a real account;
they are not changed by this repository. Confirm that the CloudFront
distribution serves the new files, invalidating cached copies if necessary.
The local PDF form still requires `python webapp.py` and is not provided by
the static CloudFront site.

Run the automated tests with:

```bash
python -m unittest
node --test tests/test_cloud_upload.js
```

The API and worker tests need `boto3` (`pip install -r requirements-dev.txt`);
they are skipped if it is missing. The browser-flow tests use Node.js.

## Prepared one-job AWS workflow

### Scalable worker

`worker.main` now supports lease-aware SQS processing. `--once` still polls once
and exits; without it the worker runs continuously as an ECS service and uses
the ECS task-protection agent endpoint before each poll. The service and
scaling policies in `infra/scalable-worker.yaml` were deployed and tested on
8 November 2026. Alarm actions were disabled after the controlled test; they
must be deliberately enabled before another automatic run. The previously
deployed one-shot revision 1 uses the old image, not this worker.

In the controlled scaling test, nine queued 30,000-row jobs caused the
service to reach three running tasks. Monitoring observed three jobs in
`PROCESSING` concurrently; CloudWatch logs confirmed overlapping processing
on three distinct workers. All nine completed with distinct attempt IDs,
leases removed and PDFs written to attempt-specific keys. The input's maximum
voltage was 52.499 V and maximum absolute current was 39 A, so the reports
correctly showed `FAIL` against limits of 50 V and 25 A. PDFKit validated
all nine PDFs and their assessment and limit text. After processing, the idle
alarm scaled desired count from three to zero; queue and DLQ were empty and
both alarm actions were disabled. This test enabled scale-out and scale-in
actions separately, rather than demonstrating unattended operation with both
enabled throughout. Worker logs recorded `DEPLOYMENT_BLOCKED` task-protection
errors during scale-in; assess this behavior before production use.

A separate real AWS-backed cache demonstration with a 60-second TTL recorded
miss/hit/read counts of 1/0/1 on first access, 1/1/1 on the immediate second
access and 2/1/2 after expiry. This used the local HTTP endpoints and a
completed DynamoDB job, not browser screenshots.

The worker conditionally claims a DynamoDB job with a unique attempt ID and a
180-second lease. Every 30 seconds it renews task protection, the SQS message's
600-second visibility and the lease. A lease is reclaimable after expiry; only
its current unexpired owner can commit `COMPLETED` or `FAILED`. Each attempt
writes `reports/<job_id>/<attempt_id>.pdf`, so an old attempt cannot overwrite
the committed report. The local downloader supports both these keys and the
legacy `reports/<job_id>.pdf` keys. SQS delivery is at least once: work can be
repeated after crashes, but a stale attempt cannot publish its result. The
lease uses worker clocks, so clock skew must remain small relative to the lease.

Duplicates for an active lease are not deleted; a completed or validation-failed
job's duplicate is acknowledged. Processing failures leave the message for SQS
retry. The existing queue has visibility 600 seconds and `maxReceiveCount=3`;
SQS eventually moves repeatedly received messages to its existing DLQ. A job
whose message reaches the DLQ can remain `PROCESSING`: there is no automatic
DLQ-to-job status reconciliation yet. Inspect both queue and DLQ before any
redrive. Poison messages or duplicate messages can reach the DLQ independently;
do not assume DLQ membership means a job was not completed. Attempt PDFs can
remain in S3 after a failed or stale attempt; no automatic cleanup is provided.
Submission to DynamoDB and SQS is still non-atomic.

#### Manual DLQ investigation and reconciliation

The DLQ does not automatically change the job status or preserve a processing
exception on the job record. Do not mark a job failed just because a duplicate
message reached the DLQ. For a known job, first stop competing launchers and
workers, inspect the task exit reasons and CloudWatch logs, identify the DLQ
message and its job ID, and consistently read its DynamoDB item. DLQ inspection
can change message visibility and receive counts; do not delete, redrive or purge
messages while investigating. SQS counts are approximate and cannot establish
whether a particular job is still pending. If another viable message or active
attempt might exist, do not reconcile until its outcome is known.

If the exact job is still `PROCESSING`, all processing attempts have stopped,
its lease has expired, and an operator has verified it cannot still complete,
the operator may choose either controlled redrive or to mark it failed. Preserve
the original exception and job/attempt ID from logs before any operation.
To mark failed, use a DynamoDB conditional update that matches the exact
observed `attempt_id`, status `PROCESSING` and `lease_until` less than or equal
to the current Unix time. Set `status` to `FAILED` and `error` to a concise
verified explanation, and remove `lease_until`. If the condition fails, stop
and re-read: never overwrite a newer attempt or a `COMPLETED` job. If the
record lacks a lease or the outcome is uncertain, investigate manually rather
than treating it as expired. Leave the DLQ message intact until recovery and
retention decisions have been approved.

The prepared template adds a task definition, an ECS service initially at zero,
an Application Auto Scaling target (maximum configurable from 1 to 3), two
scaling policies and two CloudWatch alarms. The service uses the existing
cluster, roles, subnet, security group, log group, queue, table and bucket.
Scale-out reacts to visible messages: threshold-relative steps request one
worker for 1–2 visible messages, two for 3–5, and three for 6 or more, capped
by the configured maximum. SQS metrics are approximate, and repeated alarms
can increase desired count further until the cap. The idle alarm uses
`FILL(IF(visible == 0, IF(inflight == 0, 1, 0), 0), 0)`: both metrics must
report zero in the same period for the result to be 1. Its threshold is 1,
with five evaluation periods and missing data treated as not breaching.
The scale-in policy removes one worker per adjustment. A read-only
CloudWatch `GetMetricData` probe using synthetic time series confirmed both
missing-input permutations and both-missing produce 0; two known zeros produce
1. Local tests model these cases. This is not a deployed alarm test:
CloudWatch can re-evaluate older datapoints after a stream stops and SQS
metrics are approximate and delayed. The controlled test confirmed the idle
policy could scale to zero after processing, but did not prove missing-data
behavior or safe scale-in for an active job in a live ECS service.

The service requests task protection before polling, renews it while processing
and clears it after handling. Protection is for five minutes and is renewed
every 30 seconds; a renewal error prevents final publication. It is not a
guarantee against crashes, forced stops, expired protection, API outages or
service deployment failures. Task protection was observed on AWS, but the
controlled scale-in began after jobs completed. Fargate and CloudWatch alarm
charges apply.
The cap governs service desired count, not unrelated tasks or replacements.

**Do not run the old v1 worker, manual Start Worker button, or prepared Lambda
launcher concurrently with the lease-aware service.** The updated local
manual launcher and prepared launcher both check `DescribeServices` and refuse
to launch if this service is active, draining or its state cannot be determined.
They require `ecs:DescribeServices`; access denied fails closed. Set
`REPORT_SCALING_SERVICE=1` as an additional local safeguard. These checks are
not atomic with service creation and cannot control old deployed code, other
copies of the local server, AWS CLI users or other principals. Stop all such
launchers and existing tasks before creating the service; keep them stopped.
If the service appears after a manual launch reservation, the reservation is
left for investigation rather than retried. Revision 1 remains available for
rollback only after the service and competing workers have been stopped.

On 8 November 2026 a Linux/amd64 image was pushed as `v3`; its digest is
`sha256:699c1379506e10dab97c7f664e345088a15d08e4614bf6f56484f6718f8c4a13`.
The ECR repository is mutable, so `v3` is not protected against retagging.
The separately registered one-shot task definition
`n11180021-a3-report-worker:2` references the image **by digest** and preserves
revision 1 settings and tags. A one-shot test was reported successful by the
operator; this does not test service task protection or autoscaling. The original CloudFormation
stack and the frontend's manual launch button still reference revision 1.
This registered revision does not update the stack or enable service scaling.

Before any service deployment, verify the template's default `WorkerImage`
digest and existing roles have S3, DynamoDB, SQS `ChangeMessageVisibility`, and ECS
`GetTaskProtection`/`UpdateTaskProtection` permissions, and verify QUT permits
the ECS service, scaling, alarms, service-linked role and `iam:PassRole`. Do not
modify shared IAM roles without approval. Local template linting cannot prove
those permissions, alarm behavior, or service startup in AWS.

#### Initial deployment procedure (already completed)

The following describes the initial deployment procedure for a new stack. Do
not rerun it against the existing `n11180021-a3-report-scaling` stack.

1. Stop all local launcher servers, disable any previously deployed one-shot
   schedule, and verify no one-shot tasks are running or pending. Resolve any
   uncertain `_report_worker_launcher` reservation without deleting it blindly.
   Check the main queue and DLQ, current account and region, and verify the
   existing image digest, network, roles and log group. Never enable the old
   launcher alongside the service.
2. Obtain QUT confirmation of runtime and deployment/rollback permissions.
   The deployer or CloudFormation execution role needs CloudFormation stack
   and change-set operations, ECS task-definition and service create/describe/
   update/delete/tag operations, `iam:PassRole` on both ECS roles, Application
   Auto Scaling target and policy management, and CloudWatch alarm management.
   The ECS Application Auto Scaling service-linked role must already exist or
   its creation must be allowed. Rollback needs the matching delete/deregister
   permissions. Existing IAM policy inspection and simulation were denied;
   do not assume these permissions are present.
3. Validate and create a **disabled** change set. These commands create a
   CloudFormation change set and, if executed, AWS resources; obtain approval
   first. Use this only for a new stack name after checking it does not exist:

   ```bash
   aws cloudformation validate-template --template-body file://infra/scalable-worker.yaml --region ap-southeast-2
   aws cloudformation create-change-set \
     --stack-name n11180021-a3-report-scaling \
     --change-set-name initial-disabled --change-set-type CREATE \
     --template-body file://infra/scalable-worker.yaml \
     --parameters ParameterKey=EnableScalingActions,ParameterValue=false \
     --tags 'Key=purpose,Value=assessment 3' 'Key=qut-username,Value=n11180021@qut.edu.au' \
     --region ap-southeast-2
   aws cloudformation describe-change-set --stack-name n11180021-a3-report-scaling \
     --change-set-name initial-disabled --region ap-southeast-2
   ```

4. Review the change set and execute it only with separate approval:

   ```bash
   aws cloudformation execute-change-set --stack-name n11180021-a3-report-scaling \
     --change-set-name initial-disabled --region ap-southeast-2
   aws cloudformation wait stack-create-complete --stack-name n11180021-a3-report-scaling --region ap-southeast-2
   aws cloudformation describe-stack-resources --stack-name n11180021-a3-report-scaling --region ap-southeast-2
   aws ecs describe-services --cluster n11180021-a3-report-cluster \
     --services n11180021-a3-report-workers --region ap-southeast-2
   aws cloudwatch describe-alarms --alarm-names n11180021-a3-worker-backlog n11180021-a3-worker-idle \
     --region ap-southeast-2
   ```

   Verify desired/running/pending counts are zero, both alarm actions are
   disabled, the digest and existing resource references are correct, and no
   unrelated resources were created. Rollback may remove template-owned
   service, task definition, scaling policies and alarms; the pre-existing
   log group, queue, table, cluster and roles are not owned by this template.
5. Only after separately approved, controlled tests of task protection, idle
   alarm evaluation with missing/delayed metrics, retries and DLQ recovery
   should a reviewed stack update enable alarm actions. Stop on any failure;
   never infer that local tests demonstrate AWS scaling.

For assessment evidence after an approved deployment, collect JSON-line
snapshots of approximate queue and DLQ depths, ECS desired/running/pending
counts, and known job statuses, attempt IDs, worker IDs and report keys:

```bash
python -m worker.monitor JOB_ID_1 JOB_ID_2 --interval 10 --samples 30
```

The monitor resolves both queue URLs once using the configured region, exact
queue names and owner account, then reuses them for each snapshot. It requires
SQS `GetQueueUrl` and `GetQueueAttributes`, ECS `DescribeServices`, and
DynamoDB `GetItem`. It does not scan
the table or launch workers. Compare its timestamps with CloudWatch worker logs
and inspect actual PDFs. A successful cloud scale-out, zero-scale, retries and
DLQ demonstration is still required; none is claimed by the local tests.

The worker infrastructure is deployed, and one manually launched AWS job has
completed successfully. It leaves the existing authenticated upload
endpoint and the local PDF application unchanged.
`infra/one-shot-worker.yaml` is a separate CloudFormation template
that reuses `cab432-n11180021-reports` and the existing
`n11180021-a3-report-jobs` DynamoDB table and SQS queue, the queue's existing
DLQ, the ECR image tagged `v1`, the existing CloudWatch log group
`/ecs/n11180021-a3-report-worker`, and the CAB432 ECS roles. It creates a tagged
ECS cluster and Fargate task definition. It does not
create or manage those existing resources, an always-running ECS service,
networking, an API, or an S3 bucket. `JobTableName`
defaults to the existing table and may be overridden at deployment; it must
refer to a table in the stack's account and region with a String partition key
named `job_id` and no sort key. The worker's `TABLE_NAME` and `TableName`
output use that parameter. The existing task role must already grant access
to the specified table, queue, and S3 objects; the stack does not change it.

The existing table's tags are managed separately from the stack. On inspection,
`qut-username=n11180021@qut.edu.au` was present but `purpose=assessment 3` was
missing. After confirming with the account owner, add the missing tag without
removing existing tags:

```bash
aws dynamodb tag-resource --region ap-southeast-2 \
  --resource-arn arn:aws:dynamodb:ap-southeast-2:901444280953:table/n11180021-a3-report-jobs \
  --tags Key=purpose,Value='assessment 3'
```

After reviewing the template and obtaining permission, deploy from the
repository root. This creates billable resources:

```bash
aws cloudformation deploy --region ap-southeast-2 \
  --stack-name n11180021-a3-report-worker \
  --template-file infra/one-shot-worker.yaml \
  --tags 'purpose=assessment 3' 'qut-username=n11180021@qut.edu.au'
aws cloudformation describe-stacks --region ap-southeast-2 \
  --stack-name n11180021-a3-report-worker --query 'Stacks[0].Outputs'
```

The template defaults to the existing `v1` ECR image, which has processed
cloud jobs and was inspected locally as described above. `WorkerImage` may be
overridden at deployment.

The existing upload API must first upload `uploads/<job_id>.csv`. To submit
one uploaded job after the resources have been reviewed and deployed:

```bash
python -m worker.submit JOB_ID \
  --bucket cab432-n11180021-reports \
  --table n11180021-a3-report-jobs
```

The local bridge and submitter resolve the existing queue using the shared
configuration in `worker/queue_config.py` before writing an upload or job.
They require SQS `GetQueueUrl`; an optional `--queue-url` must match the
resolved URL. To verify the caller account and resolve both URLs without
uploading or submitting anything, run:

```bash
python -m worker.queue_config
```

The ECS worker and deployed API continue to use their existing `QUEUE_URL`
environment settings; these local changes do not update a task definition.
The submitter checks that the object exists, creates a `QUEUED` job, and sends
the ID to SQS. If sending fails, retry the same command; queued jobs may produce
duplicate messages, which are ignored after completion. For this initial
demonstration run **only one task at a time**. Run the Fargate task manually
after submitting; it polls once (up to 20 seconds), processes at most one
message, saves `reports/<job_id>.pdf`, records `COMPLETED` and exits. Invalid
CSV input is recorded as `FAILED`; other errors leave the message for a
later retry. Inspect status with:

```bash
aws dynamodb get-item --table-name n11180021-a3-report-jobs \
  --key '{"job_id":{"S":"JOB_ID"}}' --consistent-read --region ap-southeast-2
```

For the approved private subnet in VPC `vpc-007bab53289655834`, launch one
task after setting `TASK_DEFINITION_ARN` from the stack output:

```bash
aws ecs run-task --region ap-southeast-2 \
  --cluster n11180021-a3-report-cluster --launch-type FARGATE \
  --task-definition "$TASK_DEFINITION_ARN" \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-04cc288ea3b2e1e53],securityGroups=[sg-078997505ad1c6bbc],assignPublicIp=DISABLED}" \
  --tags 'key=purpose,value=assessment 3' 'key=qut-username,value=n11180021@qut.edu.au'
```

Check `failures` in the response, then use `aws ecs describe-tasks` with the
returned task ARN and inspect the `LogGroup` output if it does not complete.
Verify the completed record and the matching PDF in the existing bucket; the
worker exits rather than waiting for another job.

The submitter's AWS credentials need S3 `GetObject`, DynamoDB `PutItem` and
`GetItem`, and SQS `SendMessage` permissions for the respective resources.
Before deployment, verify permission to pass both existing ECS roles and
create the template resources. The execution role needs ECR image-pull and
CloudWatch Logs permissions; the task role needs S3 `GetObject` for uploads and
`PutObject` for reports, DynamoDB `GetItem`/`UpdateItem`, and SQS
`ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes`. Role-policy inspection
was denied, so these permissions remain unverified. The approved private subnet
has an active route through an available NAT gateway and its security group
allows outbound traffic; service reachability is not yet demonstrated. Tag
launched tasks with `purpose=assessment 3` and
`qut-username=n11180021@qut.edu.au`.

The table write and SQS send are separate operations, so retry submission if
the send fails. This minimal worker has no automatic retry launcher or DLQ
status reconciliation: a job whose message reaches the DLQ can remain
`PROCESSING`. Inspect the queue, DLQ and task logs before retrying. It has no
per-user job authorisation or cloud status/download API. The separately
deployed scaling service and local caching demonstration are described above;
this one-shot workflow alone does not demonstrate autoscaling.

### Prepared automatic worker launcher

`worker/launcher.py` and `infra/worker-launcher.yaml` prepare an EventBridge
rule that invokes a Lambda every minute. The rule is **disabled by default**;
neither the launcher nor the logging changes have been deployed or tested in
AWS. Once enabled and permitted, the Lambda checks for visible SQS work and
starts one existing one-shot Fargate task. It does not consume SQS messages.
The next invocation checks the previous task; another is started only after
the recorded task has stopped. SQS counts are approximate, so launching is
eventual rather than instantaneous.

The launcher reserves the `_report_worker_launcher` item in the existing job
table and persists a unique ECS `clientToken` before `RunTask`. Retries use the
same token if the launch response or table update is lost; ECS deduplication
only applies while the token is retained. Lambda concurrency is limited to one. The launcher
must be the **only** automatic or manual launcher while enabled; manually
started tasks do not acquire this reservation. This is a serial, one-job-at-a-time
mechanism, not autoscaling or demonstrated load distribution.

The reservation is intentionally not cleared if ECS returns an uncertain
result, an explicit launch failure, a recorded task cannot be found, or a
task stops without a confirmed zero exit code: the
Lambda logs an error instead of risking duplicate launches. Investigate ECS
and Lambda logs and resolve the reservation manually only after confirming
no worker is running. ECS idempotency tokens are not retained indefinitely,
and the launcher currently retries an unresolved reservation without an expiry
guard. After token expiry a retry could start a duplicate worker. Address this
before enabling the rule; disable the rule while investigating any stuck
reservation.
The existing SQS visibility timeout and DLQ still apply; a failed task leaves
its message for retry after investigation and manual recovery, and a message sent to the DLQ may leave its job in
`PROCESSING`. Inspect the DLQ and task logs. The submitter's table write and
queue send are still separate and submission must be retried if sending fails.

The template creates **one Lambda, one EventBridge rule and one Lambda invoke
permission**, reusing the queue, table, cluster, task definition, subnet,
security group and existing `CAB432-Lambda-Role` by default. It does not
modify that role. An administrator must verify it permits:

- Lambda CloudWatch Logs creation and writes.
- `sqs:GetQueueAttributes` on the report queue.
- `dynamodb:GetItem`, `PutItem`, `UpdateItem` and `DeleteItem` on the job table.
- `ecs:RunTask` for the worker task definition and `ecs:DescribeTasks` on the
  cluster, `ecs:DescribeServices` for the scaling-service guard, and
  `ecs:TagResource` for the requested task tags.
- `iam:PassRole` on both existing ECS execution and task roles, restricted to
  `ecs-tasks.amazonaws.com`.

The current student session cannot inspect or simulate role policies, so QUT
permission to create/invoke the launcher and pass the ECS roles is **not
verified**. If the existing shared Lambda role lacks these rights, ask the
course administrator for an approved dedicated role and pass its ARN as
`LauncherRoleArn`; do not change the shared role without approval. Until then,
the previously tested manual single-task workflow is the practical alternative.

On the latest read-only check, `lambda:GetFunction` for
`n11180021-a3-report-launcher` was explicitly denied by
`Do-Not-Delete-LT1-DenyPolicy-2`, so the function name's availability is
unknown. This denial alone does not establish whether function creation is
allowed. `iam:GetRole` confirmed that the existing role trusts Lambda, but
`iam:ListRolePolicies`, `iam:ListAttachedRolePolicies`, and
`iam:SimulatePrincipalPolicy` were denied, leaving its effective runtime
permissions unknown. Ask QUT to check the name and verify the permissions
listed above, including applicable account-level restrictions, without changing
the shared role. For deployment, QUT must also verify the deploying principal
or CloudFormation service role can manage the template's Lambda function,
reserved concurrency, invoke permission and EventBridge rule and targets, read
the launcher ZIP from S3, and pass the existing Lambda execution role to Lambda.
CloudFormation may require corresponding update, tagging, and rollback/delete
permissions. Approval or an administrator-performed check is needed before
deployment; no IAM changes are implied.

The worker image has been built locally as `n11180021-a3-report-worker:v2` for
Linux/amd64, but has not been pushed or deployed.

There are recurring EventBridge and Lambda charges for minute-level polling,
plus Fargate runtime, CloudWatch Logs, SQS and DynamoDB request charges; exact
cost depends on AWS pricing and free-tier eligibility. No idle Fargate service
is created. The existing worker's new job start/completion/error logging only
takes effect after a new container image is built, pushed and referenced by a
new task definition revision. The launcher's ZIP must contain `launcher.py`
at its root and be uploaded to a same-region S3 bucket before deployment. Review
and approve all resources and permissions first; leave the rule disabled until
the new task revision is verified and `TaskDefinitionArn` is updated.

## Older cloud architecture scaffold

The following describes an earlier, separate scaffold, not the existing upload
pipeline or the one-shot workflow above. It has not been deployed or verified
as an end-to-end system.

```
Web interface (frontend/)  →  API Gateway + Lambda (api/)
                                 ├─ S3: uploaded CSVs and generated PDFs
                                 ├─ DynamoDB: job status and metadata
                                 └─ SQS queue  →  ECS Fargate workers (worker/)
```

1. `POST /jobs` creates a job and returns a presigned S3 upload (10 MB limit).
2. The browser uploads the CSV straight to S3, then calls `POST /jobs/{id}/submit`,
   which queues the job in SQS.
3. Fargate workers (0–3 tasks, scaled by queue depth) run the shared `core/`
   report code, save the PDF to S3 and update DynamoDB. Invalid CSVs mark the job
   `FAILED` with the validation message; unexpected errors are retried three
   times, then moved to a dead-letter queue.
4. The browser polls `GET /jobs/{id}` until `COMPLETED`, then shows the metrics
   and a presigned PDF download link. Jobs and files expire after 7 days.

Deploy (needs AWS credentials, Docker and the CDK CLI, `npm install -g aws-cdk`):

```bash
python -m pip install -r requirements-dev.txt
cd infra
cdk bootstrap   # first time only
cdk deploy
```

Copy the `ApiUrl` output into `frontend/config.js`, then open
`frontend/index.html` (or host the `frontend/` folder on any static host).
The scaffold's bucket and API currently allow any origin and have no
authentication. They are separate from the existing authenticated upload API
used by `static/index.html`; restrict their access before real use.

## Project layout

- `core/` — shared report logic: `measurements.py` (CSV validation),
  `calculations.py` (metrics and assessment), `pdf_report.py` (PDF/charts) and
  `report.py` (the combined workflow).
- `api/handler.py` — Lambda handler for the job API.
- `worker/` — SQS-consuming Fargate worker and its `Dockerfile`.
- `infra/` — AWS CDK (Python) stack.
- `frontend/` — static web interface for the cloud API.
- `main.py` — command-line interface; `webapp.py` + `static/` — local-only web interface.
- `tests/` — unit tests.

Please note all CSV files are fake and AI generated for study purposes lol

The CSV requires strictly increasing ISO timestamps and finite numeric
voltage/current/SOC values. Voltage must be above zero and SOC between 0 and
100. Timestamps must either all include or all omit a timezone. Additional
columns are ignored.

The CSV requires `timestamp,voltage_v,current_a,soc_pct` with ascending ISO timestamps, voltage in V, current in A, and SOC between 0 and 100. Negative current is charging and positive is discharging. Complete comparable charge/discharge cycles are needed for meaningful efficiency.
