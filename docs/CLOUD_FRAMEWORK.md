# A framework that generalises across AWS, Azure and Google Cloud

This document answers one question in depth: **how do you build skyctl so the
same code and the same deployment intent run correctly on AWS, Azure, Google
Cloud (and Kubernetes, and a single VM) without pretending the clouds are the
same?** It covers the model, the per-cloud toolsets and implementation
designs, the evidence for each decision, and what remains unverified.

It supersedes the infrastructure parts of [DESIGN.md](DESIGN.md) (see §14 for
the corrections). Everything described here is implemented in
`skyctl/platform/`, `skyctl/adapters/`, `skyctl/deploy/` and
`skyctl/testing/`, and covered by the test suite.

---

## 0. Summary

Generalising across clouds is three separate problems. Mixing them up is
what makes most multi-cloud abstractions leak:

| Plane | Question | Generalised by | Lives in |
|---|---|---|---|
| **Runtime** | What does the code *call*? (storage, secrets, queues, models) | **Ports with stated semantics**, adapters chosen by URL scheme, proven by a contract test kit | `ports.py`, `adapters/`, `platform/services.py`, `testing/` |
| **Invocation** | How does the platform *call the code*? (cron, queues, pushes, HTTP) | **One `Invocation` vocabulary** + decoders for every envelope + one dispatcher | `platform/invocation.py`, `lambda_handler.py`, `server.py` |
| **Provisioning** | How does the deployment *come to exist*? | **One cloud-neutral spec**, validated against **capabilities**, **rendered** per target, checked by **cross-target conformance** | `deploy/` |

Two axes run through all three planes and must stay independent:

* **Compute platform** (where code runs: ECS, Lambda, Container Apps, Cloud
  Run, Kubernetes, Compose) is modelled as a `Platform` with compute
  profiles, **capabilities** and **limits**.
* **Service provider** (which bucket, queue or secret store) is chosen by
  **URL scheme** (`s3://`, `az://`, `gs://`, `sqs://`, `servicebus://`,
  `pubsub://`, …).

An EKS pod is *Kubernetes* on the compute axis and *AWS* on the service axis.
Cloud Run reading S3 is legal. Modelling them as one "cloud" setting is the
first mistake to avoid.

```
                ┌──────────────────────── provisioning plane ─────────────────────────┐
 skyctl.deploy. │  DeploySpec ──validate(capabilities, limits)──► render ──► conformance │
 toml           │      │                                         │                      │
                └──────┼─────────────────────────────────────────┼──────────────────────┘
                       ▼                                         ▼
              aws/main.tf.json   azure/main.tf.json   gcp/main.tf.json   manifests.json   compose.json
                       │  (creates)
┌──────────────────────┼──────────── invocation plane ────────────────────────────────────┐
│ EventBridge Sched.   ACA Job cron   Cloud Scheduler   CronJob   daemon   ← schedules     │
│ SQS→Lambda  SQS/SB/PubSub→worker   Pub/Sub push  Cloud Tasks  Event Grid ← events        │
│            └────────── decode_* ──► Invocation{kind,payload} ──► Dispatcher ──┐          │
└───────────────────────────────────────────────────────────────────────────────┼──────────┘
┌──────────────────────────────── runtime plane ────────────────────────────────▼──────────┐
│ Runtime = Platform(compute profile, caps, limits) × services(URL schemes)                 │
│ ports: ObjectStore(CAS) · JobStore(update) · WorkQueue · DelayedDelivery · SecretStore ·  │
│        SessionStore · ChatProvider        ──► core: scheduler · task agent · tools · MCP  │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. What "generalise" has to mean: evidence from the first version

The first cut ([DESIGN.md](DESIGN.md)) generalised by giving every cloud the
same interfaces. Testing that design against each cloud's real semantics
turned up five defects, each of which the model in this document exists to
prevent:

| # | Defect in v1 | Why a uniform interface hid it | Fix (section) |
|---|---|---|---|
| 1 | `POST /runs` ran the agent on a background thread after returning 202 | Works locally and on ECS; on Lambda the environment freezes after the response, and on request-billed Cloud Run / Container Apps the CPU is throttled or the replica scaled in | Capability `compute.background_after_response` selects thread vs queue (§6.4) |
| 2 | The tick saved its whole in-memory queue after sending | A job the CLI added mid-tick was silently deleted. `save(jobs)` had the same signature everywhere; the missing *semantics* was atomic update | CAS `ObjectStore.put(if_version=…)` + `JobStore.update()` (§5) |
| 3 | The tick "lease" was check-then-write | Not mutually exclusive on any bucket. The design claimed the platform guaranteed concurrency = 1 | CAS lease with fencing token; correctness moved into the claim protocol (§5.3) |
| 4 | "Concurrency 1 on all three schedulers" | Only a Kubernetes CronJob with `concurrencyPolicy: Forbid` prevents overlapping runs. EventBridge Scheduler, Container Apps Jobs and Cloud Scheduler will start a new run while the previous one is still going | Claim protocol makes overlap harmless; conformance records `overlap_guard = platform \| app` (§5.3, §7.5) |
| 5 | Long agent runs were assumed to finish inside one delivery | Service Bus locks cap at 5 min and Pub/Sub push at 600 s; the message is redelivered and a second worker starts the same run in parallel | `WorkQueue.extend()` heartbeat + `max_run_seconds` ceiling validated against each target's delivery deadline (§6.3) |

All five follow the same pattern. **An interface captures shape; it does not
capture behaviour.** So the framework states behaviour explicitly
(§3.1), tests it (§10), and lets each platform declare what it can and
cannot do (§3.2).

---

## 2. The model

### 2.1 Three planes

* **Runtime plane**: everything the process calls. Ports are
  `typing.Protocol`s with a written contract, and adapters are thin. The core
  (scheduler, task agent, tools, MCP) imports only ports.
* **Invocation plane**: everything that calls the process. Every trigger on
  every platform is decoded into `Invocation{kind, payload, id, source,
  attempt, trace}` and handled by one `Dispatcher`. The vocabulary is small:
  `tick`, `post.send`, `task.run`, `task.approve`.
* **Provisioning plane**: everything that must exist before the process runs.
  One spec, rendered per target, with validation before rendering and
  conformance after.

### 2.2 Two independent axes

```python
rt = Runtime(settings)                      # skyctl/platform/runtime.py
rt.platform.name, rt.compute.name           # ("gcp", "cloud-run")      ← compute axis
rt.object_store()                           # S3ObjectStore / AzureBlob… ← service axis (SKYCTL_STATE_URL)
rt.has(Cap.BACKGROUND_AFTER_RESPONSE)       # False on request-billed Cloud Run
rt.run_strategy()                           # "queue" | "thread" | CapabilityError(remedy)
```

### 2.3 Capabilities and limits

A capability is a named fact about a compute profile (or a service), for
example `compute.background_after_response`, `schedule.timezone`,
`schedule.one_shot`, `compute.event_jobs`, `state.atomic_cas`. Limits are
numbers: job timeout, request timeout, minimum cron interval, secret size.

They are used in three places:

1. **At runtime**, to pick a strategy: `run_strategy()` chooses thread or
   queue, and `Runtime.job_store()` warns when state has no atomic CAS.
2. **At deploy time**, to reject impossible specs *before* provisioning
   anything, with a remedy: a time-zone schedule on Azure Container Apps
   Jobs, a 30-minute job on Lambda, a 20-minute run behind a 600 s Pub/Sub
   push deadline (`skyctl deploy validate`).
3. **In documentation**: `skyctl platform list` prints the matrix, and the
   values live in one reviewable file, `platform/builtin.py`.

This is how the framework avoids the lowest common denominator. A feature
may rely on anything; a target that can't honour it says so precisely. Where
a capability is missing, a **substitute** is chosen:

| Missing capability | Substitute |
|---|---|
| platform scheduler (Compose, VM) | `skyctl daemon` runs the same schedules in-process |
| background work after response (Lambda, Cloud Run, ACA) | queue + worker |
| native one-shot scheduling (Compose, Kubernetes without extras) | the tick reconciler |
| per-message queue delay (Pub/Sub) | Cloud Tasks as `DelayedDelivery` |
| atomic CAS (arbitrary fsspec backends) | refuse multi-writer use, with a warning |

---

## 3. Principles

1. **State semantics, not just signatures.** Each port documents its
   guarantees: consistency, atomicity, delivery, limits (`skyctl/ports.py`).
2. **Capabilities over lowest common denominator.** Declare, then select a
   strategy or a substitute (§2.3).
3. **A contract test kit defines "supported".** An adapter supports a port
   when it passes `skyctl.testing.contracts`. That applies to built-ins and
   plugins alike.
4. **Correctness lives in the app; platforms provide speed.** At-least-once
   delivery and overlapping schedules are assumed everywhere. The claim
   protocol and write ledger make duplicates harmless, and native services
   (EventBridge `at()`, Cloud Tasks) are a fast path, never the only path.
5. **Prefer platform-native wiring at deploy time.** Inject secrets, grant
   identity and mount config natively where the platform can. Fall back to
   runtime SDK calls only where it can't.
6. **One artifact, many entry points.** One OCI image. `serve`, `worker`,
   `invoke`, `daemon`, `tick` and the Lambda handler are commands, not
   builds.
7. **Generate infrastructure; never hand-maintain N copies.** Hand-written
   per-cloud IaC drifts. Generated IaC plus conformance cannot drift silently.
8. **Keep the core dependency-free and extensible.** Cloud SDKs are extras,
   imported lazily. New platforms and service schemes arrive through entry
   points (`skyctl.platforms`, `skyctl.services`).

---

## 4. Runtime plane: ports, semantics and per-cloud mapping

Each table lists the service, the semantic detail that differs, and how the
adapter reconciles it.

### 4.1 State: `ObjectStore` with compare-and-swap

Contract: read-after-write per key; `put(if_absent=True)` succeeds for exactly
one concurrent caller; `put(if_version=v)` succeeds only at version `v`; a
losing conditional put raises `PreconditionFailed` and changes nothing.

| | AWS S3 | Azure Blob | GCS | File (VM/Compose) | fsspec fallback |
|---|---|---|---|---|---|
| Version token | ETag | ETag | generation | content hash | content hash |
| Create-only | `If-None-Match: *` | `overwrite=False` | `if_generation_match=0` | lock file + check | check-then-write |
| Compare-and-swap | `If-Match` | `etag` + `MatchConditions.IfNotModified` | `if_generation_match=g` | lock file + check | check-then-write |
| Lost race | 412, or 409 `ConditionalRequestConflict`; **404 if deleted** | `ResourceExistsError` / `ResourceModifiedError` | 412 | — | — |
| `atomic_cas` | ✅ | ✅ | ✅ | ✅ (one host / shared POSIX volume) | ❌ |
| Adapter | `adapters/s3.py` | `adapters/azure_blob.py` | `adapters/gcs.py` | `adapters/local.py` | `adapters/fsspec_store.py` |

Reconciliation notes:

* **S3 ETags are content hashes**, so a version can recur (A→B→A, the ABA
  problem). That is harmless for whole-document read-modify-write, because
  the stale writer's base equals the current content. Fencing tokens come
  from documents that embed a unique timestamp, so they never recur.
* **GCS reads pin the generation**: metadata and data are read with
  `if_generation_match`, so the returned version always describes the
  returned bytes.
* fsspec was the first version's choice for "one adapter, three clouds". It
  stays only as a fallback, because it exposes no portable conditional write.
  This is the concrete reason to drop from a generic library to native SDKs
  behind the port.

### 4.2 Scheduling state: `JobStore.update()`

Every mutation is `update(mutate)`, a CAS loop over one JSON document. That
removes lost updates between the CLI, tick, worker and HTTP (defect 2).

**When to move to a database:** past roughly 10³ jobs, or several writes per
second, one document becomes a contention point. The port is ready for
DynamoDB (conditional writes), Cosmos DB (ETag) or Firestore (transactions),
each implementing the same `update()` per item. This is not built; nothing
at skyctl's scale needs it yet.

### 4.3 Work queue: `WorkQueue`

Contract: at-least-once; a received message is hidden for the visibility
window; `ack` removes it; `nack(delay)` redelivers after `delay`; `extend`
keeps it hidden; handlers are idempotent.

| | SQS | Service Bus | Pub/Sub (pull) | memory |
|---|---|---|---|---|
| Visibility model | per-receive `VisibilityTimeout` (≤ 12 h) | peek-lock; **lock duration is a queue setting, ≤ 5 min**, renewable | ack deadline ≤ 600 s, `modifyAckDeadline` | per receive |
| `nack(delay)` | `ChangeMessageVisibility` | abandon (0), else **complete + re-send scheduled** (emulated) | `modifyAckDeadline(delay)` | native |
| `extend` | `ChangeMessageVisibility` | `renew_message_lock` | `modifyAckDeadline` | native |
| Per-message delay | ≤ 900 s | any (`scheduled_enqueue_time_utc`) | **none** → `QueueCapabilityError` | any |
| Redelivery count | `ApproximateReceiveCount` | `delivery_count` | `delivery_attempt` (only with a DLQ policy) | exact |
| Dead-lettering | redrive policy | built in (`max_delivery_count`) | dead-letter topic | worker drops after `max_attempts` |
| Settlement constraint | receipt handle | **same receiver link** (adapter keeps it open) | ack id | — |

### 4.4 Delayed delivery: `DelayedDelivery` (the fast path)

| | AWS | Azure | GCP | Kubernetes / Compose |
|---|---|---|---|---|
| Service | EventBridge Scheduler `at()` with `ActionAfterCompletion=DELETE` | Service Bus scheduled message | Cloud Tasks HTTP task with OIDC | none |
| Horizon | unlimited | unlimited | 30 days | — |
| Cancel | delete schedule | no-op (stray delivery is harmless) | delete task (name stays reserved for a while) | — |
| Delivers to | SQS work queue → worker | the queue → worker | `POST /invoke` | tick reconciler |

The scheduler registers the fast path when a job is created within the
horizon. Delivery calls `send_now()`, which is a no-op if the job isn't due
or was already sent. The tick reconciler remains the safety net either way.

### 4.5 Secrets: `SecretStore` + native injection

| | AWS | Azure | GCP | Kubernetes | Compose |
|---|---|---|---|---|---|
| Store | Secrets Manager (64 KiB) | Key Vault, RBAC (25 KB) | Secret Manager (64 KiB) | Secret (etcd) / CSI / ESO | `.env` |
| Runtime fetch (the `BSKY_*` map, keys unknown at deploy time) | `aws-sm://` | `azure-kv://` | `gcp-sm://` | not needed | not needed |
| Native injection (fixed-name secrets, e.g. the HTTP token) | ECS `secrets` (fetched by the **execution** role) | Container Apps Key Vault reference | Cloud Run `secret_key_ref` | `envFrom` | `env_file` |
| Gap | Lambda has **no native injection**: fetch at init | — | — | — | — |

The `BSKY_*` map is fetched at runtime because its keys depend on which
accounts exist, which is unknown when the infrastructure is rendered. Fixed
names are injected natively.

### 4.6 Models: `ChatProvider`

Two wire protocols cover every cloud:

| Protocol | Direct | AWS | Azure | GCP |
|---|---|---|---|---|
| Anthropic Messages | `anthropic:` | `bedrock:` (`AnthropicBedrock`, SigV4 via the task role) | `foundry:` (Claude on Microsoft Foundry; verify the client, §13) | `vertex:` (`AnthropicVertex`, ADC) |
| OpenAI Chat Completions | `openai:`, `openrouter:`, `ollama:`, `compat:` | — | `azure-openai:` (`/openai/v1`; API key **or managed-identity Entra token**) | (Vertex's OpenAI-compatible endpoint via `compat:`) |

Bedrock's Converse API would be a third protocol, for non-Anthropic models on
AWS. It is a natural extension but not built.

### 4.7 Identity (outbound)

| | AWS | Azure | GCP | Kubernetes |
|---|---|---|---|---|
| Workload identity | ECS task role / Lambda role | user-assigned managed identity (`AZURE_CLIENT_ID` selects it) | service account (metadata server) | IRSA / EKS Pod Identity; Azure Workload Identity; GKE Workload Identity |
| Credential chain | botocore default chain | `DefaultAzureCredential` | `google.auth.default()` | the cloud's |
| Static keys in config | never | never | never | never |

Conformance asserts that no rendered artifact contains
`AWS_SECRET_ACCESS_KEY`, `AZURE_CLIENT_SECRET` or
`GOOGLE_APPLICATION_CREDENTIALS`.

---

## 5. Coordination: how correctness survives every platform's delivery semantics

The assumption is the worst case from every platform at once: at-least-once
delivery, overlapping schedules, workers killed mid-task, clock skew.

### 5.1 Compare-and-swap everywhere

`cas_update(store, key, mutate)` is the only way shared state changes. It
reads `(data, version)`, applies `mutate`, and writes with `if_version`,
retrying with jittered backoff on `PreconditionFailed`.

### 5.2 Lease with fencing

The tick lease is a CAS-acquired document; its version is the **fencing
token**. It is an *optimisation*: an overlapping tick skips instead of
contending. It is not the correctness mechanism.

### 5.3 Claim → send → complete

```
claim     (one CAS update)  due jobs: status=sending, claimed_by=me, claimed_until=now+ttl
send      (outside the lock) post to Bluesky
complete  (CAS, fenced)     only if claimed_by is still me → apply result / retry_at
recover   (next tick)       sending + claimed_until passed → verify, then re-send or mark sent
```

* Two claimers can never claim the same job. Tested with 5 threads over 20
  jobs.
* A worker whose claim went stale **cannot overwrite** the new claimant's
  outcome, because completion is fenced.
* **Crash after send, before complete** is the hard case. Recovery performs
  *outbox verification*: it reads the account's own repo (`list_posts`,
  straight from the PDS) for the same text created after the stale claim.
  If the post is found, the job is marked sent, with no duplicate. Tested.
* Native delayed delivery, the daemon, the tick and `post.send` all use this
  one protocol.

### 5.4 Agent runs

`task.run` invocations set the write ledger's idempotency key from the
message / run id, so a redelivered run re-plans but **skips writes it already
made**. Deployment policy ceilings are re-applied **where work executes**,
because a queue message can be injected by anyone with send permission.

---

## 6. Invocation plane

### 6.1 Every trigger, one vocabulary

| Trigger | Arrives as | Decoder |
|---|---|---|
| ECS RunTask / Container Apps Job / Cloud Run Job / k8s CronJob | `skyctl invoke '<json>'` | `decode_json` |
| Cloud Tasks, EventBridge API destinations, curl | `POST /invoke`, plain JSON | `decode_http` |
| Pub/Sub push | `POST /invoke`, `{"message":{"data":b64}}` | `decode_http` |
| Eventarc / Event Grid (CloudEvents binary, structured, batch) | `POST /invoke` | `decode_http` |
| Event Grid schema, including `SubscriptionValidationEvent` | `POST /invoke`, `[{"eventType":…}]` | `decode_http` → `eventgrid.validate` |
| CloudEvents webhook handshake | `OPTIONS /invoke`, `WebHook-Request-Origin` | public route, echoes origin |
| Lambda: SQS event source mapping | `{"Records":[…]}` | `decode_lambda_event` |
| Lambda: EventBridge Scheduler / rule | target input / `{"detail":…}` | `decode_lambda_event` |
| Lambda: function URL | `{"requestContext":…, "body":…}` | `decode_lambda_event` → `decode_http` |
| Queue consumer | `QueueMessage.body` | `decode_json` |

`skyctl invoke --file event.json` accepts any of these envelopes, so a
production event can be replayed locally.

### 6.2 Outcomes map to each platform's retry signal

| Outcome | HTTP push (`/invoke`) | Lambda SQS batch | Lambda async | Queue worker |
|---|---|---|---|---|
| success | 200 | — | return | ack |
| retryable (`RetryableError`, unexpected exception) | **503** → platform retries | listed in `batchItemFailures` | raise → Lambda retries | nack with exponential backoff |
| permanent (bad input, policy ceiling, unknown kind) | **422** → no retry | not listed (logged, dropped) | return `ok=false` | ack + `invocation.dropped` log |

### 6.3 Long work vs. delivery deadlines

* The worker **heartbeats** `extend()` while a handler runs. A 20-minute run
  stays exclusive under a 5-minute Service Bus lock. Tested with a polling
  "second worker".
* `policy.max_run_seconds` caps agent runs, and `validate` rejects values
  that exceed a target's delivery deadline minus a margin: Pub/Sub push
  600 s, Lambda 900 s.
* SQS visibility for Lambda is set to at least 6× the function timeout.
* On SIGTERM (ECS scale-in, Kubernetes, Container Apps), the worker stops
  taking messages and finishes the current one. Unfinished work is
  redelivered.
* ECS worker scale-in uses *visible + in-flight* queue depth (CloudWatch
  metric math). Scaling on "visible" alone would kill a long run as soon as
  the queue looked empty.

### 6.4 Synchronous vs asynchronous runs

`Runtime.run_strategy()`:

* `queue` if `SKYCTL_QUEUE_URL` is set: `/runs` enqueues `task.run` and
  returns 202; a worker executes it.
* `thread` only where `compute.background_after_response` holds (local,
  Compose, ECS, Kubernetes, Cloud Run with instance-based billing).
* Otherwise HTTP 501 with the remedy. Defect 1 becomes a clear error instead
  of silent data loss.

---

## 7. Provisioning plane

### 7.1 The spec

`skyctl.deploy.toml` describes intent: `[service]`, `[worker]`,
`[[schedules]]` (cron + IANA time zone + **an Invocation**), `[policy]`
(ceilings), `[state]` (versioning, retention), `[tasks.*]`. It also holds one
`[targets.<name>]` section per target, containing only provider facts
(region, network, identity). Secrets never appear in it. See
`deploy/examples/skyctl.deploy.toml`.

Schedules are Invocations, not shell commands, so a schedule means the same
thing whether it becomes an ECS override, a Lambda input, a CronJob's args,
or an entry in the Compose daemon.

### 7.2 Validation (before rendering)

`skyctl deploy validate` checks each target's capabilities and limits:
time zones, job timeouts, minimum intervals, delivery deadlines, cron
expressibility (EventBridge can't express "day-of-month OR day-of-week"),
Lambda's ECR-only images, HTTP on Lambda, public services without real auth,
Kubernetes identity and bucket requirements.

### 7.3 Rendering

| Target | Output | Compute | Notable design |
|---|---|---|---|
| `aws` | `main.tf.json` | ECS Fargate **or** Lambda (same image) | ALB + HTTPS; worker 0..N on queue depth; Scheduler → RunTask with `arn_without_revision`; `at()` delayed delivery |
| `azure` | `main.tf.json` | Container Apps app + jobs | KEDA Service Bus event job; Key Vault references; Entra-only storage; one UAMI for everything, including Azure OpenAI |
| `gcp` | `main.tf.json` | Cloud Run service + jobs | Pub/Sub **push** into `/invoke` with OIDC and a DLQ; Cloud Tasks; deterministic service URL to avoid a self-reference cycle |
| `kubernetes` | `manifests.json` (v1 List) | Deployment / CronJob / KEDA ScaledJob | the portable tier; only the SA annotation and KEDA trigger are cloud-specific |
| `compose` | `compose.json` | daemon + http | capability substitution made concrete |

Why **Terraform JSON** rather than HCL templates, CDKTF or Pulumi:

* JSON is native Terraform syntax, so renderers are plain Python producing
  dicts. There is no template language and no extra runtime dependency, and
  the output is testable as data.
* The output is readable and reviewable. Golden files under
  `deploy/generated/` are checked by a test, so a renderer change shows up
  as a reviewable diff.
* It works with Terraform or OpenTofu unchanged.
* Pulumi is the better choice if you want the IaC to *be* a program with
  state-aware previews, and a Pulumi backend could consume the same
  `DeploySpec`. CDKTF depends on a toolchain whose long-term maintenance
  status is uncertain, so I avoided it.

### 7.4 Capability substitution in practice

Compose has no scheduler, queue or delayed delivery. The Compose renderer
does not fail; it emits `skyctl daemon`, which runs every schedule (any time
zone, one instance at a time). `/runs` uses threads, which is safe there. The
tick sends one-time posts. State is `file:///data` on a shared volume with
lock-file CAS.

### 7.5 Conformance (after rendering)

`skyctl deploy check` runs 11 cross-target invariants on the *facts* each
renderer reports (env, schedules, workloads, exposure) and on the generated
Terraform:

| Check | Guarantees |
|---|---|
| `env.contract` | every target sets all 16 common `SKYCTL_*` keys; a renderer *cannot* override a common key (raises) |
| `env.policy-parity` | agent ceilings are identical on every target |
| `state.cas` | state is `s3://`, `az://`, `gs://` or `file://` |
| `schedules.complete` | every schedule exists on every target with the same Invocation |
| `schedules.overlap` | overlap is prevented by the platform (CronJob `Forbid`, daemon) or guarded by the app (claim protocol) |
| `exposure` | public endpoints exist iff `service.public` |
| `terraform.references` | every `${…}` resolves (a stand-in for `terraform validate`, which needs network access) |
| `terraform.providers` | a target uses only its own provider's resources |
| `secrets.handling` | secret inputs are `sensitive`; no app-password-shaped literals |
| `iam.least-privilege` | AWS: no wildcard actions, `Resource:*` only for the allow-listed Bedrock actions. Azure: no subscription scope, Owner or Contributor. GCP: no `allUsers`, only allow-listed project roles |
| `workloads.worker` | a worker exists wherever the spec asks for one |

Each check is **mutation-tested**: a test breaks the invariant in a rendered
artifact and asserts the check fails (`tests/test_deploy.py`).

---

## 8. Cross-cutting concerns

### 8.1 Inbound authentication (`platform/auth.py`)

An ordered chain set by `SKYCTL_AUTH`:

| Method | Used by | Verification |
|---|---|---|
| `bearer` | humans, MCP clients | constant-time compare; ignores JWTs |
| `google-oidc` | Cloud Run IAM callers, Pub/Sub push, Cloud Tasks, Cloud Scheduler | RS256 via Google JWKS; issuer `accounts.google.com` with and without `https://`; audience list; `SKYCTL_OIDC_ALLOWED` restricts subjects |
| `entra-oidc` | Event Grid (Entra auth), managed identities | Entra JWKS; **v1 and v2 issuers** |
| `oidc` | Cognito, Okta, Kubernetes service accounts | discovery or explicit JWKS |
| `aws-alb` | an ALB `authenticate-oidc` action | ES256 with the regional ALB key; **`signer` must equal our ALB ARN** |
| `easyauth`, `platform` | Easy Auth, Cloud Run IAM, Lambda URL `AWS_IAM` | trusts the platform; opt-in only |

A token from an issuer no method handles is *rejected*, not passed through.
"Any valid Google token for this audience" includes identities from other
projects, which is why `SKYCTL_OIDC_ALLOWED` exists and the GCP renderer sets
it to the invoker service account.

### 8.2 Observability (`platform/logging.py`)

One call (`log_event`, `emit_metric`); the platform decides the shape:

| | Log fields the backend understands | Trace header read | Metrics |
|---|---|---|---|
| AWS | `level`, `xray_trace_id` | `X-Amzn-Trace-Id` / `_X_AMZN_TRACE_ID` | CloudWatch **EMF** |
| Azure | `level`, `operation_Id`, `operation_ParentId` | `traceparent` | log line (or OTel → App Insights) |
| GCP | `severity`, `logging.googleapis.com/trace`, `…/spanId`, `…/trace_sampled` | `X-Cloud-Trace-Context`, `traceparent` | log line (or OTel → Cloud Monitoring) |
| other | `level`, `trace_id`, `span_id` | `traceparent` | log line |

OpenTelemetry remains the cross-cloud upgrade path (`[otel]` extra). All
three clouds now ingest OTLP, via ADOT, the Azure Monitor distro, or Google's
OTLP endpoint.

### 8.3 Limits (values in `platform/builtin.py`; re-check on upgrade)

| | Job run time | HTTP request | Delivery deadline that matters | Secret size |
|---|---|---|---|---|
| ECS Fargate | none | ALB idle ≤ 4000 s | SQS visibility (≤ 12 h, extendable) | 64 KiB |
| Lambda | 900 s | 900 s | function timeout | 64 KiB (env total 4 KB) |
| Container Apps | configurable (`replicaTimeout`) | ~4 min ingress | Service Bus lock ≤ 5 min (renewed) | 25 KB |
| Cloud Run | jobs 24 h | 60 min | Pub/Sub push 600 s | 64 KiB |
| Kubernetes | none | your ingress | your queue | 1 MiB (etcd object) |

### 8.4 Cost shape at idle

Every cloud target scales to zero except these:

* **ECS**: the HTTP service keeps ≥ 1 task running, and a NAT gateway is
  needed for private subnets.
* **Service Bus Standard** has a base charge; Standard was chosen for
  reliable scheduled-message support.
* **Container Apps**: the KEDA poller itself is free.

The tick job's per-invocation start-up is the main recurring cost. A
5-minute tick is the default.

---

## 9. Per-cloud toolset and implementation design

This section answers *"what would be their relevant toolset and design of
their implementation?"* directly.

### 9.1 Toolset matrix

| | AWS | Azure | Google Cloud | Kubernetes |
|---|---|---|---|---|
| Python SDKs (`skyctl[…]` extra) | `boto3`, `awslambdaric` | `azure-identity`, `azure-storage-blob`, `azure-keyvault-secrets`, `azure-servicebus` | `google-cloud-storage`, `-secret-manager`, `-pubsub`, `-tasks` | none (services by URL) |
| Credential chain | botocore default | `DefaultAzureCredential` | `google.auth.default()` | the hosting cloud's workload identity |
| IaC | Terraform/OpenTofu `hashicorp/aws`; CDK; CloudFormation | `azurerm` (+ `azapi`); Bicep; `azd` | `hashicorp/google`; Infrastructure Manager; Config Connector | manifests / Kustomize / Helm; KEDA; Secrets Store CSI / External Secrets |
| CLI | `aws` | `az` | `gcloud` | `kubectl` |
| Emulators | **moto** (in-process; API shape), **LocalStack** | **Azurite**, Service Bus emulator | **fake-gcs-server**, Pub/Sub and Firestore emulators | kind, k3d |
| CI auth | `aws-actions/configure-aws-credentials` (OIDC → role) | `azure/login` (OIDC → federated credential) | `google-github-actions/auth` (WIF) | the cloud's, then `kubectl` |
| Observability | CloudWatch, EMF, ADOT → X-Ray | Log Analytics, App Insights (OTel distro) | Cloud Logging, Cloud Trace (OTLP) | OTel Collector |
| Registry | ECR (**required for Lambda**) | ACR | Artifact Registry | any |
| Model endpoint | Bedrock | Azure OpenAI, Foundry | Vertex AI | any |

### 9.2 AWS implementation design

```
                 EventBridge Scheduler ──cron()──► ECS RunTask (skyctl invoke '{"kind":"tick"}')
                         │ at() one-shot (app-created, self-deleting)
                         ▼
 ALB(HTTPS) ──► ECS service (skyctl serve) ──/runs──► SQS work ──► ECS worker 0..N (skyctl worker)
                                                        │ redrive            ▲ step scaling on
                                                        ▼                    │ visible+in-flight
                                                      SQS DLQ
 task role: S3 (bucket/*), SQS (one queue), Secrets Manager (one secret), Scheduler (one group), Bedrock
```

* **Compute profiles:** `ecs` (default) or `lambda`, from the same image. Lambda
  uses `awslambdaric` and `skyctl.platform.lambda_handler.handler`. HTTP on
  Lambda is left to the Lambda Web Adapter, outside this renderer.
* **Secrets:** the `BSKY_*` map is fetched at runtime through the task role.
  The HTTP token is injected natively by ECS, via the *execution* role.
* **State:** S3 with If-Match / If-None-Match CAS, KMS, versioning, and a
  lifecycle on `runs/`.
* **Queue:** SQS + DLQ. Under Lambda, the event source mapping uses
  `ReportBatchItemFailures` and visibility ≥ 6× the timeout.
* **Schedules:** EventBridge Scheduler with the cron dialect translated (6
  fields, `?`, 1 = SUN), time-zone aware, targeting `arn_without_revision`
  so each deploy needs no schedule update.
* **Delayed delivery:** `at()` schedules into the work queue, deleted after
  firing.
* **Gotchas handled:** reserved Lambda env keys; ECR-only images for Lambda;
  standalone security-group rule resources (inline `ingress` blocks in
  Terraform JSON require every optional attribute); scale-in only at zero
  in-flight.

### 9.3 Azure implementation design

```
 ACA Job (schedule trigger, UTC cron) ──► skyctl invoke '<json>'
 ACA App (ingress; Easy Auth optional) ──/runs──► Service Bus queue ──KEDA──► ACA Job (event trigger)
                                                     │ scheduled messages = delayed delivery     skyctl worker --once
                                                     └ built-in DLQ (max_delivery_count)
 one user-assigned identity: Key Vault Secrets User · Blob Data Contributor · SB Data Sender/Receiver · OpenAI User
```

* **Compute:** Container Apps for HTTP; Container Apps **Jobs** for both
  schedules and the event-driven worker (KEDA `azure-servicebus`).
* **Identity:** one UAMI, selected with `AZURE_CLIENT_ID`. Azure OpenAI uses
  an Entra token minted from it; no API key is needed.
* **State:** Blob with `shared_access_key_enabled = false` (Entra only), so
  the provider runs with `storage_use_azuread = true`.
* **Queue:** Service Bus Standard, `lock_duration = PT5M` (the maximum). The
  worker renews locks. `nack(delay)` is emulated as complete + scheduled
  re-send.
* **Delayed delivery:** scheduled messages on the same queue
  (`SKYCTL_DELAYED_URL=queue`).
* **Limits enforced:** schedule cron is **UTC only**, so non-UTC schedules
  are a validation error.
* **Deliberate compromise:** the KEDA scale rule uses a *listen-only* SAS
  rule. It only counts messages; the app itself uses Entra. Managed-identity
  auth for ACA scale rules exists in newer API versions; adopt it once the
  `azurerm` provider exposes it.

### 9.4 Google Cloud implementation design

```
 Cloud Scheduler (any TZ) ──jobs:run──► Cloud Run Job (skyctl invoke '<json>')
 Cloud Run service (IAM-only; internal ingress unless public) ◄── POST /invoke ◄── Pub/Sub PUSH (OIDC, DLQ)
          │ /runs → publish                                    ◄── POST /invoke ◄── Cloud Tasks (OIDC, delayed)
 runtime SA: bucket objectAdmin · secretAccessor · topic publisher · tasks enqueuer · actAs invoker · aiplatform.user
 invoker SA: run.invoker on service and jobs (used by Pub/Sub, Tasks, Scheduler to mint OIDC tokens)
```

* **Compute:** one Cloud Run service that is both the HTTP surface *and* the
  push worker, plus Cloud Run Jobs for schedules. No always-on worker exists
  on GCP.
* **Auth:** the platform enforces IAM (no `allUsers`), and the app verifies
  the Google ID token again (`google-oidc`, audience = service URL and
  `/invoke`, allowed = invoker SA). That is defence in depth.
* **Self-reference:** the service needs its own URL (Cloud Tasks target,
  OIDC audience). Using `service.uri` in its own env is a Terraform cycle, so
  the deterministic `https://<name>-<project-number>.<region>.run.app` is
  used instead.
* **Ingress:** internal unless public. Pub/Sub push, Cloud Tasks and Cloud
  Scheduler in the same project still count as internal.
* **Limits enforced:** `max_run_seconds` must stay under the 600 s push
  deadline.

### 9.5 Kubernetes implementation design (the portable tier)

Kubernetes is the strongest generaliser for compute: Deployment, CronJob and
KEDA ScaledJob behave the same on EKS, AKS and GKE. Only two things vary by
cloud: the ServiceAccount annotation for workload identity, and the KEDA
trigger type and its `podIdentity` provider. Secrets are injected natively
(`envFrom`); sync them from the cloud store with the Secrets Store CSI Driver
or External Secrets Operator. It is also the only target where the platform
itself prevents overlapping schedule runs (`concurrencyPolicy: Forbid`).
Pods run non-root with a read-only root FS, all capabilities dropped, and
`RuntimeDefault` seccomp.

Trade-off: you operate a cluster. For one small service, the managed
serverless targets are cheaper and simpler. Kubernetes wins when you already
run a platform team's cluster or need the same operational surface
everywhere.

---

## 10. Testing strategy: what each tier proves

| Tier | Proves | Does **not** prove | Where |
|---|---|---|---|
| Contract kit over in-memory / file stores | the semantics as written | anything about clouds | `tests/test_contracts.py` |
| SDK-faithful fakes (Azure Blob, GCS, Service Bus, Pub/Sub, Cloud Tasks) | adapters call the SDK correctly and map its exceptions | server behaviour | `skyctl/testing/fakes.py` |
| **moto** (S3, SQS, EventBridge Scheduler) | real request/response shapes and error codes | **concurrency**: moto checks S3 preconditions *without a lock* (found while writing these tests) | `tests/test_contracts.py`, `tests/test_messaging.py` |
| Emulators (LocalStack, Azurite, fake-gcs-server) in CI | wire compatibility, and some concurrency | production concurrency guarantees | `tests/test_integration.py` (`-m integration`) |
| Real cloud sandbox | the actual guarantees | — | same tests, pointed at a real bucket |
| Conformance + mutation tests | rendered deployments are equivalent and each check can fail | that Terraform *applies* | `tests/test_deploy.py` |
| Golden files | renderer changes are visible in review | — | `deploy/generated/` |

The moto finding is the clearest example of why the tiers exist. The same
contention test passes against the file, memory, Blob-fake and GCS-fake
stores, but fails intermittently on moto, because moto's check-then-write
isn't atomic. Real S3 evaluates preconditions atomically. Investigating it
also exposed a real adapter bug: a conditional PUT on a key deleted mid-race
returns 404, which must be treated as a lost race.

---

## 11. Build vs. adopt

| Option | What it offers | Why not (or not yet) |
|---|---|---|
| **Dapr** | building blocks (state with ETags, pub/sub, bindings, secrets) with components for all three clouds | sidecar runtime: awkward on Lambda, and it adds a process to every job. Its state-store ETag semantics match `ObjectStore`, so a `dapr://` service adapter is a clean future plugin |
| **fsspec** | one API over every bucket | no portable conditional write; kept as a non-atomic fallback |
| **Apache OpenDAL** | unified storage with conditional writes on several services | promising replacement for the three native stores; adds a Rust wheel, and its condition support varies per service |
| **Pulumi / CDK / CDKTF** | IaC as code | a `DeploySpec` → Pulumi backend is straightforward; JSON was chosen for zero dependencies and reviewable output |
| **Crossplane / Radius / Score** | platform-level app abstractions | heavier than needed for one service. Score's "workload spec + platform translators" is the closest relative of `DeploySpec` and is worth aligning with if it matures |
| **KEDA** | one autoscaling model over every queue | adopted on Kubernetes and (managed) on Container Apps |
| **CloudEvents** | one event envelope | adopted as an input format; the internal vocabulary stays `Invocation` because SQS, Pub/Sub push and Cloud Tasks don't emit CloudEvents natively |
| **OpenTelemetry** | one telemetry API | adopted as the upgrade path (§8.2) |

---

## 12. Extending: adding a fourth cloud or a new service

**A new compute platform** (for example OCI, Fly.io or Cloudflare
Containers):

1. Subclass `Platform`. Implement `detect()`, `compute_profiles` (kind,
   capabilities, limits), `trace_from_headers`, `log_fields`, `identity`,
   `toolset`.
2. Register it through the `skyctl.platforms` entry point. A broken plugin
   only logs a warning (tested).
3. If it needs IaC, add a renderer and entries in `validate.SCHEDULE_PROFILE`
   / `DELIVERY_DEADLINE`. Run `skyctl deploy check`; conformance applies
   automatically.

**A new service** (for example `oci://` state or `nats://` queue):

1. Implement the port, and register `"<kind>:<scheme>"` through the
   `skyctl.services` entry point.
2. Pass the contract kit (`check_object_store`,
   `check_cas_under_contention`, `check_job_store_lease`,
   `check_work_queue`), against an emulator and then a real sandbox.
3. Declare honestly: `atomic_cas`, `supports_delay`, `max_delay_seconds`,
   `max_horizon_seconds`.

---

## 13. What is not verified, and open decisions

Not verified in this environment (no network access to registries or
clouds):

* **`terraform validate` / `apply` was not run** on the generated
  configurations. Reference integrity, provider purity and structure are
  checked by conformance; attribute names follow provider documentation as I
  know it. CI runs `terraform validate` before `apply`.
* The Azure output targets **azurerm 3.x** attribute names. 4.x renames
  some (for example `storage_account_id` on containers).
* EventBridge Scheduler's ECS templated target is assumed to pass `Input` as
  the RunTask overrides (`containerOverrides`), as EventBridge rules do.
  Verify in a sandbox.
* The `foundry:` client: the installed Anthropic SDK has no dedicated
  Foundry client, so a Messages-API fallback is used.
* Platform limits (§8.3) are from provider documentation as of writing and
  change. They live in one file for that reason.
* Emulator-backed integration tests are written and wired into CI, but were
  skipped here (no Docker).

Open decisions for you:

1. **JobStore backend:** stay on one CAS document (proposed), or move to
   DynamoDB / Cosmos / Firestore now.
2. **Azure queue tier:** Service Bus Standard (proposed, for scheduled
   messages) vs. Storage Queues (cheaper, no scheduled enqueue, so
   tick-only).
3. **Kubernetes as primary target** if you already run clusters, versus the
   managed serverless targets (proposed for a single service).
4. **OpenDAL** to replace the three native stores with one dependency, once
   its conditional-write coverage is confirmed for all three.

---

## 14. Corrections to DESIGN.md (v1)

| DESIGN.md said | Correct statement |
|---|---|
| §5.1/§5.2: set concurrency = 1 on scheduled jobs; "all three clouds support this" | Only a Kubernetes CronJob (`Forbid`) and the in-process daemon prevent overlap. Correctness comes from the claim protocol (§5.3) |
| §5.2: JSON-on-object-store with a "short lease" | The lease is CAS-acquired and fenced, and it is only an optimisation; every write is a CAS update |
| §3: fsspec as the `ObjectStore` for all buckets | Native S3 / Blob / GCS stores with real CAS; fsspec is a non-atomic fallback |
| §8: `POST /runs` runs on a worker thread | Strategy depends on `compute.background_after_response`; queue + worker on Lambda, Cloud Run and Container Apps |
| §10: hand-written Terraform roots per cloud | Generated from one spec (`skyctl deploy render`), gated by `skyctl deploy check` |
