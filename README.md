# skyctl

**Bluesky accounts, agents and MCP tools — on your machine or in AWS, Azure and Google Cloud.**

skyctl is a Python library and command-line tool for managing several
Bluesky accounts, and a toolkit for letting AI agents act on them
*safely*: agents read, research and propose a plan; a person approves the
exact plan; a deterministic executor carries it out. The same package runs
locally, as an MCP server for Claude and other clients, and as one
container image on AWS, Azure, Google Cloud or any Kubernetes cluster,
with the infrastructure generated from one cloud-neutral spec.

```bash
skyctl run_cloud_task --cloud aws -- description "Find a post about the transit strike, look at the comments \
and respond to any blatant misinformation with the facts from the most recent news"
```

```
→ aws (queue) Find a post about the transit strike, look at the comments and respond to …
  citations required: the request mentions news/facts (--no-require-citations to turn off)
  queued as 20260301-091500-a1b2c3; waiting for a worker…
  · search_posts {"account": "work", "query": "transit strike"}
  · get_post_thread {"account": "work", "uri": "at://did:plc:…/app.bsky.feed.post/3l…"}
  · web_search {"query": "transit strike talks"}
  · fetch_page {"url": "https://news.example/transit-strike-talks"}
  · plan_check {"plan": {…}}
  · plan_create {"plan": {…}}
──────────────────────────────── proposed plan ─────────────────────────────────
20260301-091612-9f8e7d awaiting_approval (by agent-run:20260301-091500-a1b2c3, approval out_of_band)
Plan: Correct misinformation in the comments under the post about the transit strike
Hash: sha256:58d0ee5608b8…
1 write(s), 0 read(s); public accounts: work
[r1] reply as @newsdesk.example → reply to at://did:plc:…/app.bsky.feed.post/3m…
    in reply to @someone.example: “The strike was called off yesterday, trains are running normally.”
    “Not according to today's reporting: talks resume Monday and the strike continues until then.
     https://news.example/transit-strike-talks”
    link: https://news.example/transit-strike-talks (retrieved in this run)
Approve and execute this exact plan? [y/N]
```

Nothing is posted until you approve. The Bluesky passwords never leave the
deployment's secret store; your machine needs only permission to send to
the deployment's queue and read/write its state bucket.

## Features

- **Multi-account CLI and SDK** — post, thread, reply, quote, link cards,
  images with alt text, search, timelines, notifications, scheduled and
  recurring posts, JSON output, cached sessions.
- **Tools for agents** — one tool registry (read, research, write,
  destructive, plan) exported as MCP tools and as Anthropic / OpenAI tool
  schemas, governed by a policy (account scope, write budgets, deny-lists).
- **MCP server** (`skyctl mcp`, stdio or streamable HTTP) with profiles
  (`read`, `plan`, `direct`, `admin`) and approval modes (`client`,
  `elicit`, `out_of_band`).
- **Plan-then-execute** — agents propose plans that are checked (schema,
  policy, grapheme limits, grounding, citations), hashed, approved and
  executed without a model in the loop; idempotent under retries.
- **Research** — news/web search (GDELT with no key, Brave, Tavily) and an
  SSRF-safe page reader, with *citation grounding*: an agent can only cite
  sources it actually retrieved.
- **Task agent** — give it a goal in plain language, locally
  (`skyctl task run`) or in a deployment (`skyctl run-cloud-task`), with
  Anthropic, Bedrock, Vertex AI, Azure OpenAI, OpenAI-compatible or local models.
- **One image, three clouds** — native adapters for S3, Azure Blob and GCS
  (with real compare-and-swap), SQS, Azure Storage Queues, Pub/Sub, cloud
  secret stores; serverless (ECS/Lambda, Container Apps, Cloud Run) or
  Kubernetes (EKS, AKS, GKE); Terraform and manifests generated from one
  `*.deploy.toml` and checked by a cross-target conformance suite.
- **Evaluations** — score how well a model turns free-text requests into
  safe plans against a simulated network (`skyctl eval run`).

## Install

```bash
pip install skyctl                 # CLI + SDK
pip install "skyctl[mcp]"          # + MCP server and HTTP service
pip install "skyctl[agents]"       # + task agent (Anthropic SDK)
pip install "skyctl[aws]"          # or [azure], [gcp]: cloud adapters for run-cloud-task and deployments
pip install "skyctl[all]"          # everything a cloud image needs
```

Python 3.10–3.13. From a source checkout: `pip install -e ".[dev]"`.

## Quick start

```bash
cp .env.example .env               # add your accounts; use app passwords, never your main password
skyctl accounts
skyctl post work "Hello from skyctl" --dry-run
skyctl post work "Hello from skyctl"
skyctl schedule work "Weekly update" --cron "0 9 * * mon" && skyctl run-scheduler
```

```python
from skyctl import BskyManager

manager = BskyManager.from_env()
result = manager.post("work", "Hello from Python")
print(result.url)
```

Use skyctl from Claude Desktop or Claude Code, with plans you approve:

```json
{"mcpServers": {"skyctl": {"command": "skyctl", "args": ["mcp", "--profile", "plan", "--approval", "elicit"]}}}
```

Run an agent in the cloud:

```bash
skyctl deploy render deploy/examples/managed.deploy.toml --target aws --out build
(cd build/aws && terraform init && TF_VAR_secrets_json="$(cat ../../.env)" terraform apply)
skyctl cloud import aws --terraform-dir build/aws
skyctl run-cloud-task --cloud aws --description "Thank everyone who replied to my announcement"
```

`--cloud` is `aws`, `azure`, `gcp`, any profile you define, or `local` to
rehearse on your machine.

## Documentation

| Document | For |
|---|---|
| [docs/USAGE.md](docs/USAGE.md) | CLI and Python SDK reference, persona agents, task agent, MCP quick reference |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | deploying, connecting clouds, operator permissions, `run-cloud-task`, approval and trust, responsible use, hardening |
| [docs/MCP_AGENT.md](docs/MCP_AGENT.md) | design: MCP server, plan protocol, approval, grounding, research, evaluation, threat model |
| [docs/CLOUD_FRAMEWORK.md](docs/CLOUD_FRAMEWORK.md) | design: ports and adapters, invocation, deployment spec, renderers, conformance, per-cloud choices |
| [docs/DESIGN.md](docs/DESIGN.md) | the original cloud + agent design (partly superseded, as marked) |
| [docs/README.md](docs/README.md) | index of all documentation |

## Security and responsible use

skyctl acts on behalf of real accounts, so its defaults are conservative:
agents in deployments work in **plan mode** with **out-of-band approval**;
approvals are bound to the plan's hash; execution re-checks policy;
replies can only target posts the agent actually read and cite only sources
it actually retrieved; third-party content is passed to models as untrusted
data; page fetching refuses private and metadata addresses. Persona agents
run only against a private PDS.

If you automate replies to other people, keep a human in the loop, keep
batches small, require sources for factual claims, label the account as
automated, and follow the Bluesky Community Guidelines — see
[docs/OPERATIONS.md §7](docs/OPERATIONS.md#7-responsible-use-of-automated-replies).

Report vulnerabilities privately: [SECURITY.md](SECURITY.md).

## Development

```bash
pip install -e ".[dev]"
pytest --cov                       # offline: simulated Bluesky, moto for AWS, SDK-faithful fakes for Azure/GCP
ruff check . && ruff format --check . && mypy
python scripts/license_headers.py && python scripts/check_dependency_licenses.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full checklist, design
principles and how changes are made, and [RELEASING.md](RELEASING.md) for
releases.

## Project status and governance

skyctl is beta software (0.x: minor versions may change APIs; see
[CHANGELOG.md](CHANGELOG.md)). The project is run in the open by
consensus and intends to apply to the
[Apache Incubator](https://incubator.apache.org/); see
[GOVERNANCE.md](GOVERNANCE.md). Participation is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).

skyctl is not affiliated with or endorsed by Bluesky Social PBC.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) and [AUTHORS](AUTHORS).
