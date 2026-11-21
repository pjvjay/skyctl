# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may change APIs).

## [Unreleased]

### Fixed
- The type check failed with `atproto` 0.0.74 and later, which ship `py.typed`: `BskyClient._build_facets` bound one variable to three different facet types. The variable is now annotated; behaviour is unchanged.
- CI: the `integration` job could not start its containers. fake-gcs-server was given its arguments through the service's `options`, which makes `docker create` fail; it is now started as a step.

## [0.2.0] - 2026-11-17

### Licensing
- **Relicensed from MIT to Apache-2.0** by the copyright holder, as a first step towards the Apache Software Foundation ([GOVERNANCE.md](GOVERNANCE.md)). Added `NOTICE` and `AUTHORS`, and licence headers on every source file.

### Added
- `skyctl run-cloud-task` (alias `run_cloud_task`): run a free-text task *in* a deployment. The agent plans in the cloud, the operator reviews and approves locally, and the deployment executes. Transports: work queue + state bucket, HTTP, or local. Profiles via `skyctl cloud add|import|list|remove|status|approve`.
- Research tools: `get_post_thread` (a post's comments), `web_search` (GDELT by default, Brave, Tavily) and `fetch_page` (SSRF-safe). Citation grounding: published links must come from research; optional `require_citations`.
- The plan protocol (`plan_check`/`plan_create`/`plan_execute`/…): hash-bound approval; deterministic, resumable, idempotent execution; grounding against invented post URIs.
- MCP server profiles (`read`, `plan`, `direct`, `admin`), approval modes (`none`, `client`, `elicit`, `out_of_band`), prompts, resources, progress notifications and elicitation.
- Cross-cloud framework: ports with stated semantics and native adapters (S3, Azure Blob and Storage Queues, GCS, SQS, Pub/Sub, Cloud Tasks, EventBridge Scheduler), capability-driven runtime, invocation plane (`skyctl invoke`, `worker`, `daemon`, Lambda handler).
- Deployment specs rendered to AWS (ECS, Lambda, EKS), Azure (Container Apps, AKS), Google Cloud (Cloud Run, GKE), Kubernetes and Compose, with 12 conformance checks. Operator IAM and a `skyctl_connection` Terraform output.
- Evaluation harness (`skyctl eval`) with a simulated Bluesky network, including a misinformation-correction scenario.
- `skyctl --version`, `py.typed`, PEP 639 licence metadata, and a `dev` extra.
- Plan previews show the post a reply answers (author and text, as read) and, for every link, whether the agent retrieved it in the run. The plan check warns on a second reply to the same author.
- Documentation: `docs/USAGE.md` (CLI/SDK reference, moved out of the README), `docs/OPERATIONS.md` (operator guide, responsible use), `docs/README.md` (index). `scripts/verify_install.py` verifies built artefacts in a fresh virtual environment.

### Fixed
- Failed writes were recorded in the idempotency ledger, so a retry could skip a post that never went out.
- The ledger was keyed on the task name, so a daily task could never repeat a write on a later day.
- Link-card fetching could reach private or metadata addresses (SSRF).
- Page fetching read the whole response before applying its size cap; it now streams and stops at the cap.
- Dependency lower bounds that did not work, found by testing at the lowest resolution: `apscheduler>=3.10.2` (older releases import `pkg_resources`), `boto3>=1.35.69` (S3 conditional writes), `mcp>=1.21.1` (older SDKs do not import with current pydantic); tests `moto>=5.1.5`.
- Tests for optional SDKs skipped silently when the SDK was installed but broken; they now fail (`require()` in `tests/conftest.py`).
- The container image did not ship `NOTICE` in the package metadata.
- `mcp>=1.9` allowed the incompatible 2.x SDK; pinned to `>=1.12,<2`.
- Streamable-HTTP MCP behind a load balancer failed with 421 (Host check).
- `FileObjectStore.list()` crashed under a relative state directory.
- `skyctl cancel <unknown id>` exited 0.
- The Anthropic `fallbacks` request field is now sent in the request body, so it works across SDK versions.

## [0.1.0]

- Initial release: multi-account posting, threads, replies, scheduling, persona agents on a private PDS (MIT licence).
