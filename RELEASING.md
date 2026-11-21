# Releasing skyctl

## Today (pre-incubation)

The artefacts people vote on are byte-for-byte the artefacts that are
published: CI builds them once, from the tag, and the publishing job waits
for the vote.

1. **Prepare.** On `main`, with CI green:
   - bump `__version__` in `skyctl/__init__.py` (semantic versioning; pre-1.0 minor versions may break APIs);
   - move *Unreleased* entries in `CHANGELOG.md` under the new version with the date;
   - run every check in [CONTRIBUTING.md](CONTRIBUTING.md#the-checks-ci-runs-all-of-them-run-them-before-you-push).
2. **Tag the candidate.** `git tag -s vX.Y.Z -m "skyctl X.Y.Z" && git push origin vX.Y.Z`.
   CI lints, tests on Python 3.10–3.13 and at the lowest supported dependency
   versions, builds the sdist and wheel, installs each in a fresh virtual
   environment and runs the suite against it, and uploads them as the `dist`
   artefact. The `release` job then **waits**: the `pypi` GitHub environment
   requires a maintainer's approval.
3. **Verify and sign the candidate** (download the `dist` artefact of the tag's run):
   ```bash
   python scripts/verify_install.py dist/skyctl-X.Y.Z-py3-none-any.whl dist/skyctl-X.Y.Z.tar.gz
   for f in dist/*.whl dist/*.tar.gz; do gpg --armor --detach-sign "$f"; sha512sum "$f" > "$f.sha512"; done
   ```
   `verify_install.py` installs each artefact in a fresh venv, checks the
   metadata (version, `License-Expression: Apache-2.0`, LICENSE/NOTICE,
   `py.typed`), smoke-tests the CLI and runs the full suite against the
   installed package — for the sdist, from the unpacked sdist, which also
   proves the source release is complete and builds on its own.
4. **Vote.** Announce the candidate (artefacts, signatures, checksums, the
   tag's commit, the CI run) and hold a vote for at least 72 hours: three
   +1 maintainer votes and more +1 than -1.
5. **Publish.** If the vote passes, approve the waiting `release` job. It
   publishes the same artefacts to PyPI with trusted publishing (no API
   tokens; PyPI attestations), signs them with Sigstore, and attaches them
   with checksums to the GitHub release; attach the GPG signatures from step 3.
   If the vote fails, reject the job, delete the tag, fix, and start again
   with the next patch version.

## After entering the Apache Incubator

- The **source release** (`apache-skyctl-X.Y.Z-incubating-src.tar.gz`) is the
  release. PyPI wheels are convenience binaries built from it.
- Candidates go to `dist/dev`, are signed with a key in the project `KEYS`
  file, and are voted on by the PPMC and then the Incubator PMC (three
  binding +1 votes).
- Releases contain `LICENSE`, `NOTICE` and `DISCLAIMER`, and pass Apache RAT
  (our `scripts/license_headers.py` mirrors its check today).
