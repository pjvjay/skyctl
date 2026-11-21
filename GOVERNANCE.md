# Governance

skyctl is an open-source project licensed under the Apache License 2.0.
Its long-term goal is to become a project of the **Apache Software
Foundation (ASF)**. This document describes how the project is run today
and the concrete path to the Apache Incubator. The current rules are
deliberately close to the ASF's, so the transition changes as little as
possible.

## Roles (today)

| Role | Who | Can |
|---|---|---|
| User | anyone | use skyctl, report issues, ask questions |
| Contributor | anyone who sends a change | propose changes via pull requests |
| Committer | invited by the maintainers | review and merge changes |
| Maintainer | initially the original author; others by consensus of maintainers | decide direction, releases, invite committers and maintainers |

Committers are invited on merit: sustained, high-quality contributions,
reviews, and help to others. Code is only one form of contribution;
documentation, testing, triage and support count too.

## Decision making

- **Lazy consensus** for day-to-day changes: a proposal stands unless
  someone objects within a reasonable time (72 hours for anything
  significant).
- **Votes** for releases, new committers/maintainers and governance
  changes: +1 / 0 / -1. Releases need at least three +1 votes from
  maintainers and more +1 than -1. A -1 on a code change is a veto and must
  come with a technical justification.
- **Discussion happens in public** (issues, and later a `dev@` mailing
  list). Decisions made elsewhere are brought back to the public channel
  before they take effect. "If it didn't happen on the list, it didn't happen."

## Path to the Apache Software Foundation

The ASF accepts new projects through the **Apache Incubator**
(https://incubator.apache.org/). The steps, and where skyctl stands:

| Step | What it involves | Status |
|---|---|---|
| 1. Apache-compatible licensing | Apache-2.0 for all code; no Category X dependencies (GPL, LGPL, AGPL, SSPL, BUSL, …); licence headers on every source file | **Done.** Relicensed from MIT to Apache-2.0 (see the note below); headers enforced by `scripts/license_headers.py`; dependencies audited by `scripts/check_dependency_licenses.py` (all Category A, plus `certifi` as Category B) |
| 2. An open, diverse community | contributors and committers from more than one organisation; public decision making | **In progress.** This is the main requirement for graduation, and it cannot be engineered: it comes from users and contributors |
| 3. Name check | the name must not conflict with existing trademarks; podlings run a formal name search | **To do.** "skyctl" must be searched; "Bluesky" is a third-party trademark used here only descriptively |
| 4. Champion and mentors | an ASF Member who champions the proposal, and (usually three) Incubator PMC mentors | **To do.** Engage on general@incubator.apache.org |
| 5. Proposal and vote | a written proposal (scope, community, initial committers, dependencies, risks) discussed and voted on by the Incubator PMC | **To do.** |
| 6. IP clearance | a **Software Grant Agreement** from the copyright holders for the existing code; **ICLAs** from all initial committers (and **CCLAs** from employers where required) | **To do.** |
| 7. Move to ASF infrastructure | repository under the `apache` GitHub organisation; dev@/private@/commits@ lists; ASF issue tracking | At acceptance |
| 8. Incubating releases | signed source releases (GPG + SHA-512) named "Apache skyctl (incubating)", voted by the podling PMC and the Incubator PMC; a DISCLAIMER file; ASF licence headers | See [RELEASING.md](RELEASING.md) |
| 9. Graduation | a self-governing community, several releases made the Apache Way, a diverse committer base | Later |

### What changes at acceptance (mechanically)

- `NOTICE` gains the ASF attribution, and source headers switch to the ASF
  header ("Licensed to the Apache Software Foundation (ASF) under one or
  more contributor license agreements…"). **Only after the Software Grant
  is recorded.** Using that header before then would be incorrect.
  `scripts/license_headers.py` makes the switch a one-line change.
- DCO sign-offs are replaced by ICLAs.
- Releases follow ASF release policy (source release primary; PyPI wheels
  are convenience binaries built from the voted source).

## Licensing note: relicensing from MIT to Apache-2.0

The project was first published under the MIT licence. It was relicensed to
Apache-2.0 for version 0.2.0 by its copyright holder (see `AUTHORS`).
Relicensing is valid only with the consent of **all** copyright holders. If
any contributions from other authors predate 0.2.0, their consent must be
recorded (for example in the pull request that relicensed the project)
before the Software Grant is signed.

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
