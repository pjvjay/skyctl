# Operating skyctl

This guide is for people who deploy skyctl and run tasks against a deployment.
For *why* things work this way see [DESIGN.md](DESIGN.md),
[CLOUD_FRAMEWORK.md](CLOUD_FRAMEWORK.md) and [MCP_AGENT.md](MCP_AGENT.md).

Contents

1. [The run-cloud-task flow](#1-the-run-cloud-task-flow)
2. [Deploying and connecting a cloud](#2-deploying-and-connecting-a-cloud)
3. [Operator permissions per cloud](#3-operator-permissions-per-cloud)
4. [Running tasks](#4-running-tasks)
5. [Approval and the trust model](#5-approval-and-the-trust-model)
6. [Research: search providers and page fetching](#6-research-search-providers-and-page-fetching)
7. [Responsible use of automated replies](#7-responsible-use-of-automated-replies)
8. [Hardening checklist](#8-hardening-checklist)
9. [Monitoring and troubleshooting](#9-monitoring-and-troubleshooting)

---

## 1. The run-cloud-task flow

```bash
skyctl run_cloud_task --cloud aws -- description "Find a post about <topic>, look at the comments and \
respond to any spewing blatant misinformation with the facts from the most recent news"
```

```
 operator's machine                         the deployment (AWS | Azure | GCP | Kubernetes)
 ──────────────────                         ───────────────────────────────────────────────
 1. submit task.run ─────── work queue ───► 2. worker: agent in PLAN mode
                                               reads posts & threads, searches the news,
                                               fetches articles, proposes ONE plan
 3. follow runs/<id>.jsonl ◄── state bucket ◄─ transcript + plans/<id> (status: proposed)
 4. show the preview, ask "approve?"
 5. write the approval (bound to the     ──►  state bucket
    plan's hash)
 6. submit plan.execute ─── work queue ───► 7. worker: deterministic executor
                                               (no model), re-checks the plan,
                                               sends exactly the approved writes
 8. poll plans/<id> for step results ◄──────── step results, post URLs
```

Properties you can rely on:

- **Nothing is posted before a person approves.** The agent runs in `plan`
  mode: it has no direct write tools. Writes exist only as steps of a plan.
- **What you approve is what runs.** The approval stores the plan's SHA-256
  hash; the executor refuses a plan whose hash differs.
- **No model at execution time.** Text in the comments, profiles or web pages
  the agent read cannot change what the executor does.
- **Your machine never holds the Bluesky passwords.** They stay in the
  deployment's secret store; the worker executes with the deployment's identity.
- **Idempotent.** A redelivered queue message or a retried execution does not
  post twice (write ledger keyed by plan and step).

## 2. Deploying and connecting a cloud

```bash
pip install "skyctl[all]"

# 1. Describe the deployment once (cloud-neutral), check it, render one target.
cp deploy/examples/managed.deploy.toml deploy/skyctl.deploy.toml     # edit: image, accounts, policy, operators
skyctl deploy check  deploy/skyctl.deploy.toml
skyctl deploy render deploy/skyctl.deploy.toml --target aws --out build

# 2. Provision. The secret is your .env (Bluesky app passwords, model keys).
cd build/aws && terraform init && TF_VAR_secrets_json="$(cat ../../.env)" terraform apply && cd -

# 3. Connect this machine to it (reads the `skyctl_connection` Terraform output).
skyctl cloud import aws --terraform-dir build/aws
skyctl cloud list
```

The profile is saved in `~/.config/skyctl/clouds.toml` (`$SKYCTL_CONFIG_DIR`
or `$XDG_CONFIG_HOME` override the location). It holds only locations — the
state bucket URL, the queue URL, adapter settings such as `AWS_REGION` —
never secrets. Other ways to define a profile:

```bash
# By hand
skyctl cloud add aws --state-url s3://my-bucket/skyctl --queue-url sqs://sqs.us-east-1.amazonaws.com/123456789012/skyctl-work \
    --env AWS_REGION=us-east-1
# From a saved `terraform output -json`, e.g. in CI
skyctl cloud import gcp --outputs-file outputs.json
# From environment variables (nothing on disk), e.g. in a pipeline
export SKYCTL_CLOUD_AZURE_STATE_URL=az://skyctl/skyctl SKYCTL_CLOUD_AZURE_QUEUE_URL=azqueue://skyctlstate/skyctl-work \
       AZURE_STORAGE_ACCOUNT_NAME=skyctlstate        # adapter settings come from your environment here
# Through the deployment's HTTP endpoint instead of the bucket + queue
skyctl cloud add prod --endpoint https://skyctl.example.com --transport http   # uses SKYCTL_HTTP_TOKEN / SKYCTL_APPROVER_TOKEN
```

`--cloud local` needs no profile: it runs the agent on this machine with the
local settings (`.env`, `SKYCTL_STATE_URL`) and is the way to rehearse a task.

The cloud SDK on your machine uses its normal credential chain — `aws sso
login` / `AWS_PROFILE`, `az login`, `gcloud auth application-default login`.

## 3. Operator permissions per cloud

Operators need exactly two things: **send** on the work queue, and
**read/write** on the state bucket (to follow runs and record approvals).
The generated Terraform creates this grant; you attach people to it.

| Cloud | Generated grant | You do |
|---|---|---|
| AWS | `aws_iam_policy.operator` (`sqs:SendMessage` on the work queue; `s3:GetObject`/`s3:PutObject` on its objects and `s3:ListBucket` on the state bucket). Output: `operator_policy_arn`. | Attach the policy to an IAM Identity Center permission set, group or role. |
| Azure | Per entry in `[targets.azure] operators` (Entra object IDs): *Storage Blob Data Contributor* on the state container, *Storage Queue Data Message Sender* on the work queue. | List operator object IDs in the spec; re-apply. |
| Google Cloud | Per entry in `[targets.gcp] operators` (`user:…`, `group:…`): `roles/storage.objectUser` on the bucket, `roles/pubsub.publisher` on the topic. | List members in the spec; re-apply. |
| Kubernetes | The backing services are the same cloud resources, so the same grants apply. | As for the cloud hosting the cluster. |

Operators do **not** need: the secret store, the Bluesky passwords, the
model provider's keys, or any permission on the compute.

## 4. Running tasks

```bash
skyctl run-cloud-task --cloud aws --description "Summarise today's mentions on the work account" --account work
skyctl run_cloud_task --cloud gcp -- description "Thank everyone who replied to my announcement"   # same command
```

| Option | Default | Meaning |
|---|---|---|
| `--account` | deployment's scope | Account aliases the agent may use (repeatable). Cannot widen the deployment's `policy.accounts`. |
| `--max-writes` | 5 | Most writes the plan may contain. The deployment's `policy.max_writes` is a ceiling. |
| `--require-citations` | auto | Every post/reply must contain a link retrieved by `web_search`/`fetch_page` in this run. Turned on automatically when the request mentions news, facts, sources, fact-checking or misinformation. |
| `--approve` | `ask` | `ask` shows the plan and asks; `auto` approves without asking (scripts you trust); `never` leaves the plan for later review. Without a terminal, `ask` becomes `never` — skyctl never approves silently. |
| `--dry-run` | off | Plan only; even an approved plan only previews. |
| `--wait/--no-wait` | wait | `--no-wait` returns once queued; follow with `skyctl cloud status <run-id> --cloud …`. |
| `--timeout` | 900 s | Exit code 2 on timeout; the run continues in the cloud. |
| `--model` | deployment's | e.g. `bedrock:anthropic.claude-opus-5-5`, `vertex:…`, `azure-openai:<deployment>`. |
| `--json` | off | Machine-readable outcome on stdout. |

Exit codes: `0` completed, queued or awaiting approval; `1` failed, refused or
error; `2` timeout.

Reviewing later:

```bash
skyctl cloud status 20260301-091500-a1b2c3 --cloud aws
skyctl cloud approve <plan-id> --cloud aws        # shows the preview, asks, then requests execution
```

What the preview shows, per write: the account, the action, the full text,
the post it answers (author and text, as the agent read it), every link and
whether the agent actually retrieved it in this run, plus the check's
findings (for example a warning on a second reply to the same author).

```
[s1] reply as @newsdesk.example → reply to at://did:plc:…/app.bsky.feed.post/3l…
    in reply to @someone.example: “The forum was cancelled, there is no event anymore.”
    “That's not right: organisers confirmed on Monday that the forum goes ahead on Friday. https://news.example/…”
    link: https://news.example/… (retrieved in this run)
```

## 5. Approval and the trust model

| Actor | Can | Cannot |
|---|---|---|
| The model (agent) | read, research, propose plans | write directly, approve, change an approved plan |
| The worker identity | execute approved plans, read secrets | (it is the deployment; protect it like one) |
| An operator (queue transport) | submit tasks, approve, request execution | read secrets or passwords |
| An HTTP client with `SKYCTL_HTTP_TOKEN` | submit tasks, read runs/plans, request execution | approve (needs `SKYCTL_APPROVER_TOKEN` or an identity in `SKYCTL_PLAN_APPROVERS`) |

Points to understand:

- **With the queue transport, write access to the state bucket is the
  approval right.** Grant the operator role only to people allowed to approve
  posts. If you need submitters who cannot approve, give them the HTTP
  transport with `SKYCTL_HTTP_TOKEN` only and keep approval on a separate
  credential (`SKYCTL_APPROVER_TOKEN`, or OIDC identities listed in
  `SKYCTL_PLAN_APPROVERS`).
- **The executor re-checks every plan** against the deployment's policy
  (account scope, write ceiling, delete permission, grounding, citations)
  at execution time, so an approval cannot widen policy.
- **Grounding**: a reply can only target a post that a tool returned during
  the run, and links in posts must have been retrieved by a research tool
  (or given in the request). A model cannot reply to a URI it invented or
  publish a link it did not read.
- **Deployment ceilings** (`[policy]` in the spec → `SKYCTL_AGENT_*`
  variables) bind every run, whatever the task or CLI flags ask for.

## 6. Research: search providers and page fetching

The agent's `web_search` tool uses `SKYCTL_SEARCH_URL` (spec: `runtime.search`):

| URL | Provider | Needs |
|---|---|---|
| `gdelt://` (default) | GDELT DOC 2.0 news search | nothing |
| `brave://` | Brave Search API | `BRAVE_API_KEY` in the secret |
| `tavily://` | Tavily | `TAVILY_API_KEY` in the secret |
| `none` | research disabled | — |

`fetch_page`, link cards and persona sources fetch through `SafeFetcher`:
only `http(s)`; hosts that resolve to private, loopback, link-local
(including cloud metadata at 169.254.169.254), multicast or reserved
addresses are refused, checked again on every redirect; responses are read
up to a byte cap and only HTML/plain text is accepted.

Residual risk: DNS answers can change between the check and the connection
(DNS rebinding). If your deployment can reach sensitive internal services,
add network egress controls (VPC egress rules, Azure NSG/firewall, GCP
firewall/VPC-SC) so the workload can reach only the public internet.

## 7. Responsible use of automated replies

skyctl can reply to people at scale. The defaults are chosen so that it
behaves like a careful person, and we ask operators to keep them:

- **A person approves every public write.** Keep `--approve ask` (or
  `never` plus review) for anything that replies to other people. Use `auto`
  only for tasks whose output you would publish without reading.
- **Facts need sources.** For news and misinformation tasks citations are
  required automatically; the plan check rejects a reply without a link the
  agent actually retrieved. Read the cited article before approving.
- **Only clear-cut cases.** The agent is instructed to reply only to
  *blatant*, checkable falsehoods contradicted by recent reporting, to skip
  opinions, jokes, predictions and contested interpretations, and to say so
  in its report when evidence is unclear. Reject plans that argue opinions.
- **One civil reply per person.** The agent is instructed to reply at most
  once per author; the plan check warns on a second reply to the same post
  or author. Brief, factual, no ridicule.
- **Small batches.** Keep `--max-writes` low (the default is 5). Replying
  to many strangers in a short time reads as spam and can trigger rate
  limits or moderation.
- **Be transparent.** Say in the account's profile that it posts with
  automated assistance and who operates it.
- **Follow the platform's rules.** Read and follow the Bluesky Community
  Guidelines and Terms of Service and the AT Protocol developer guidance on
  automation and rate limits. You are responsible for what your accounts
  publish.
- **Persona agents (`skyctl agents`) run only against a private PDS** and
  refuse public hosts.

## 8. Hardening checklist

- [ ] Bluesky **app passwords**, never account passwords; one per deployment; rotate them.
- [ ] Secrets only in the cloud secret store (`TF_VAR_secrets_json` at apply time); nothing in the spec or images.
- [ ] `[policy]` set to the minimum: `accounts`, `max_writes`, `allow_delete = false`.
- [ ] Operator role granted only to people who may approve posts (section 5).
- [ ] HTTP service private (`service.public = false`) or behind OIDC; separate `SKYCTL_APPROVER_TOKEN`.
- [ ] `state.protect = true` and versioning on, so `terraform destroy` keeps the job document.
- [ ] Egress controls if the network hosts internal services (section 6).
- [ ] Logs in JSON (`runtime.log_format = "json"`) shipped to your SIEM; alerts on `refused` plans and failed writes.
- [ ] Pin the image by digest; rebuild for security updates.

## 9. Monitoring and troubleshooting

| Symptom | Check |
|---|---|
| `queued; waiting for a worker…` forever | Is the worker running and consuming the queue (ECS service / Lambda event source, Container Apps job on `poll_minutes`, Cloud Run push subscription, KEDA scaler)? Queue depth and dead-letter queue. |
| `No cloud profile 'aws'` | `skyctl cloud import aws --terraform-dir …` or `SKYCTL_CLOUD_AWS_*` variables. |
| `Could not connect to aws: …` | Cloud SDK installed (`pip install "skyctl[aws]"`) and credentials valid (`aws sts get-caller-identity`). |
| AccessDenied on the bucket or queue | Operator grant (section 3). |
| Plan `refused` at execution | Policy changed since proposal, or the plan failed a re-check; the plan record lists the findings. |
| `citations required` findings | The agent proposed a reply without a retrieved source; it should research more or drop the reply. |
| Timeout (exit 2) | The run continues; `skyctl cloud status <run-id> --cloud …`. |

Everything a run did is in the state bucket: `runs/<run-id>.jsonl` (every
model turn and tool call, with arguments), `plans/<plan-id>.json` (plan,
check, approval, step results), `ledger/` (writes already made).
