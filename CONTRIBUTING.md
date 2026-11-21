# Contributing to skyctl

Thank you for helping. This document covers how to set up a development
environment, the checks every change must pass, and how design decisions
are made. skyctl aims to become an Apache Software Foundation project
([GOVERNANCE.md](GOVERNANCE.md)), so we already work the way ASF projects do:
in the open, by consensus, with every change reviewed.

## Development setup

```bash
git clone <repository-url> && cd skyctl
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # the package, every cloud adapter, tests, linters, type checker
```

Python 3.10–3.13 are supported. CI tests all four on Linux, 3.12 on macOS,
and the lowest supported version of every dependency on 3.10.

Optionally, run the fast checks on every commit:

```bash
pip install pre-commit && pre-commit install
```

## The checks (CI runs all of them; run them before you push)

```bash
ruff check skyctl tests scripts                   # lint
ruff format --check skyctl tests scripts          # formatting (run without --check to fix)
mypy                                              # types, checked as Python 3.10
pytest --cov                                      # 420+ offline tests; coverage must stay ≥ 85%
python scripts/license_headers.py                 # every source file has the Apache-2.0 header (--fix adds it)
python scripts/check_dependency_licenses.py       # no ASF Category X dependencies
skyctl deploy check deploy/examples/managed.deploy.toml      # cross-cloud conformance
skyctl deploy check deploy/examples/kubernetes.deploy.toml
python -m build && python scripts/verify_install.py          # the built artefacts, in a fresh venv (slow; CI does it)
```

If you change a dependency's lower bound, check it at the bottom of its range:

```bash
uv venv --python 3.10 .venv-lowest && uv pip install --python .venv-lowest --resolution lowest-direct -e ".[test,all]"
.venv-lowest/bin/python -m pytest -q
```

Tests that need an optional SDK call `require("module")` (from
`tests/conftest.py`): they skip only when the SDK is *not installed*; an
installed but incompatible version fails loudly instead of skipping.

The test suite needs no network and no credentials: Bluesky is simulated
(`skyctl/testing/world.py`), AWS is emulated with moto, and the other clouds'
SDKs are exercised through SDK-faithful fakes (`skyctl/testing/fakes.py`).
Emulator-backed integration tests (`pytest -m integration`) run in CI
against LocalStack, Azurite and fake-gcs-server.

If you change a deployment renderer, regenerate the golden files and review
the diff:

```bash
skyctl deploy render deploy/examples/managed.deploy.toml --all --out deploy/generated/managed
skyctl deploy render deploy/examples/kubernetes.deploy.toml --all --out deploy/generated/kubernetes
```

## Making a change

1. **Discuss first** for anything beyond a small fix: open an issue (later:
   the `dev@` mailing list) describing the problem and the approach.
2. **One logical change per pull request**, with tests. Bug fixes start
   with a failing test.
3. **Sign off your commits** (`git commit -s`), certifying the
   [Developer Certificate of Origin](https://developercertificate.org/).
   Once skyctl enters the Apache Incubator, committers will sign the ASF
   Individual Contributor License Agreement instead.
4. Add a line to `CHANGELOG.md` under *Unreleased*.
5. A maintainer reviews. Changes merge with at least one approving review
   and no unresolved objections (lazy consensus).

## Design principles that reviews will check

These are explained in `docs/`; the short version:

- **Ports state semantics, adapters prove them.** A new storage, queue or
  secret adapter must pass the contract kit in `skyctl/testing/contracts.py`
  (`check_object_store`, `check_cas_under_contention`, `check_work_queue`, …).
  See [docs/CLOUD_FRAMEWORK.md](docs/CLOUD_FRAMEWORK.md).
- **Agents propose, a deterministic executor disposes.** New write tools
  must be tagged `write` or `destructive` so profiles and policy govern
  them, and must return `{"success": …}` so the write ledger records only
  effective writes. See [docs/MCP_AGENT.md](docs/MCP_AGENT.md).
- **Network content is untrusted.** Tools that return third-party text wrap
  it (`_untrusted`). Tools that fetch URLs use `skyctl.research.SafeFetcher`
  (SSRF protection).
- **Generated infrastructure, not hand-written.** Cloud changes go in
  `skyctl/deploy/render/*` and must keep `skyctl deploy check` green.
- **No Category X dependencies** (GPL, LGPL, AGPL, SSPL, BUSL, …). Ask
  before adding any new runtime dependency.

## Adding a cloud, platform or service

See "Extending" in [docs/CLOUD_FRAMEWORK.md](docs/CLOUD_FRAMEWORK.md#12-extending-adding-a-fourth-cloud-or-a-new-service).
Third-party platforms and adapters can live in their own packages and
register through the `skyctl.platforms` / `skyctl.services` entry points.

## Reporting security issues

Never in a public issue. See [SECURITY.md](SECURITY.md).
