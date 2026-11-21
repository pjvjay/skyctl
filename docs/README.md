# skyctl documentation

## Using skyctl

| Document | Contents |
|---|---|
| [USAGE.md](USAGE.md) | Configuration; CLI (posting, threads, replies, scheduling, feeds, JSON output); Python SDK; persona agents on a private PDS; MCP server and task agent quick reference |
| [OPERATIONS.md](OPERATIONS.md) | Deploying and connecting clouds; operator permissions on AWS, Azure and Google Cloud; `run-cloud-task`; approval and the trust model; search providers and safe page fetching; responsible use of automated replies; hardening checklist; troubleshooting |

## Design

Read in this order; later documents refine earlier ones.

| Document | Contents |
|---|---|
| [DESIGN.md](DESIGN.md) | The original cloud + agent design: settings contract, tool registry, policy, MCP server, task agent, HTTP service. Superseded parts are marked inline. |
| [CLOUD_FRAMEWORK.md](CLOUD_FRAMEWORK.md) | How one codebase and one deployment intent run on AWS, Azure, Google Cloud, Kubernetes and a VM: ports with stated semantics and their adapters, the invocation plane, the deployment spec, renderers and conformance, per-cloud decisions and their evidence. |
| [MCP_AGENT.md](MCP_AGENT.md) | skyctl as an MCP server for agents: free text → plan → approval → deterministic execution; profiles; approval modes; grounding and citations; research tools; `run-cloud-task`; evaluation; threat model. |

## Project

| Document | Contents |
|---|---|
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Development setup, the checks CI runs, design principles reviews enforce |
| [../GOVERNANCE.md](../GOVERNANCE.md) | Roles, decision making, the path to the Apache Incubator |
| [../RELEASING.md](../RELEASING.md) | How releases are built, verified, signed, voted on and published |
| [../SECURITY.md](../SECURITY.md) | Reporting vulnerabilities; what is in scope |
| [../CHANGELOG.md](../CHANGELOG.md) | Changes per release |

## Where things live in the code

```
skyctl/
├── client.py, manager.py, scheduler.py, richtext.py, models.py, config.py   the Bluesky library
├── cli.py                      the `skyctl` command
├── agents.py, llm.py           persona agents (private PDS) and model backends
├── research.py                 web/news search providers, SSRF-safe page fetching
├── tools/                      tool registry, catalogue, policy, plan tools
├── planning/                   plan model, checks, store, deterministic executor
├── agent/                      task agent: chat providers, runner, TaskSpec
├── mcp_server.py, server.py    MCP server; HTTP service (/mcp, /runs, /plans, /invoke, /tick)
├── remote.py                   run-cloud-task: cloud profiles and connections
├── ports.py, settings.py       ports with stated semantics; the env-var contract
├── adapters/                   local, S3, Azure Blob, GCS, queues, delayed delivery, secrets
├── platform/                   platforms and capabilities, runtime, invocation, auth, logging, cron
├── deploy/                     deployment spec, validation, renderers, conformance
├── evals/                      free-text → plan evaluation harness and scenarios
└── testing/                    contract kit, SDK-faithful fakes, simulated Bluesky world, scripted agents
```
