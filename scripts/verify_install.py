# Copyright 2026 The skyctl Authors
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Verify built artefacts the way a user receives them: in a fresh virtual
environment, with nothing from the source tree on the import path.

    python scripts/verify_install.py                       # every wheel and sdist in dist/
    python scripts/verify_install.py dist/skyctl-0.2.0-py3-none-any.whl
    python scripts/verify_install.py dist/skyctl-0.2.0.tar.gz --no-tests

For each artefact:

1. create a venv and install ``<artefact>[all,test]`` (an sdist is built from
   source by pip, which proves the source release builds on its own);
2. check the installed metadata: version, ``License-Expression: Apache-2.0``,
   LICENSE and NOTICE shipped, ``py.typed`` present, the ``skyctl`` console
   script on PATH;
3. smoke-test the CLI (``--version``, ``--help``, ``run_cloud_task --help``,
   ``eval list``, ``deploy check`` on the examples);
4. run the test suite against the *installed* package, from the unpacked
   sdist (or the source tree, for a wheel), with the venv's ``pytest``
   executable so the source checkout is not importable.

Exit status is non-zero if anything fails. Needs network access for pip.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Failure(Exception):
    pass


def log(msg: str) -> None:
    print(f"[verify] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None, capture: bool = True) -> str:
    proc = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=capture)
    if proc.returncode != 0:
        detail = (proc.stdout or "")[-3000:] + (proc.stderr or "")[-3000:] if capture else ""
        raise Failure(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")
    return proc.stdout if capture else ""


def version_of(artefact: Path) -> str:
    m = re.match(r"skyctl-(\d[^-]*?)(?:-py3.*\.whl|\.tar\.gz)$", artefact.name)
    if not m:
        raise Failure(f"cannot read a version from {artefact.name}")
    return m.group(1)


def make_venv(where: Path) -> tuple[Path, Path, dict]:
    run([sys.executable, "-m", "venv", "--clear", str(where)])
    bindir = where / ("Scripts" if os.name == "nt" else "bin")
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(where)
    return python, bindir, env


def source_tree_for(artefact: Path, scratch: Path) -> Path:
    """Where to take tests/ and deploy/ from: the sdist itself, or the source tree for a wheel."""
    if artefact.name.endswith(".tar.gz"):
        with tarfile.open(artefact) as tar:
            try:
                tar.extractall(scratch / "sdist", filter="data")
            except TypeError:  # Python < 3.12 without the filter argument
                tar.extractall(scratch / "sdist")  # our own artefact
        [top] = list((scratch / "sdist").iterdir())
        for required in ("LICENSE", "NOTICE", "pyproject.toml", "tests", "deploy", "docs", "skyctl"):
            if not (top / required).exists():
                raise Failure(f"sdist is missing {required}")
        return top
    return ROOT


def check_metadata(python: Path, env: dict, version: str) -> None:
    script = f"""
import importlib.metadata as md, pathlib, sys
import skyctl
dist = md.distribution("skyctl")
meta = dist.metadata
assert meta["Version"] == {version!r} == skyctl.__version__, (meta["Version"], skyctl.__version__)
assert meta.get("License-Expression") == "Apache-2.0", meta.get("License-Expression")
names = {{pathlib.PurePath(str(f)).name for f in dist.files or []}}
for needed in ("LICENSE", "NOTICE", "py.typed"):
    assert needed in names, f"{{needed}} not installed"
location = pathlib.Path(skyctl.__file__).resolve()
assert "site-packages" in location.parts, f"imported from {{location}}, not the venv"
print(location)
"""
    where = run([str(python), "-c", script], cwd=Path(tempfile.gettempdir()), env=env).strip()
    log(f"  metadata ok; imported from {where}")


def smoke(bindir: Path, env: dict, version: str, tree: Path, scratch: Path) -> None:
    exe = str(bindir / ("skyctl.exe" if os.name == "nt" else "skyctl"))
    env = dict(env, SKYCTL_CONFIG_DIR=str(scratch / "config"))
    out = run([exe, "--version"], cwd=scratch, env=env).strip()
    if out != f"skyctl {version}":
        raise Failure(f"--version printed {out!r}")
    for args in (["--help"], ["run_cloud_task", "--help"], ["run-cloud-task", "--help"], ["eval", "list"]):
        run([exe, *args], cwd=scratch, env=env)
    for example in sorted((tree / "deploy" / "examples").glob("*.deploy.toml")):
        run([exe, "deploy", "check", str(example)], cwd=scratch, env=env)
    log("  CLI smoke tests ok")


def run_tests(bindir: Path, env: dict, tree: Path, pytest_args: list[str]) -> None:
    """
    Run the suite from `tree` with the venv's pytest *executable*: unlike
    `python -m pytest`, it does not put the working directory on sys.path, so
    `import skyctl` resolves to the installed package (test_project.py asserts
    this when SKYCTL_EXPECT_INSTALLED is set). For an sdist, `tree` is the
    unpacked sdist, so the project checks also prove the sdist is complete.
    """
    pytest_exe = str(bindir / ("pytest.exe" if os.name == "nt" else "pytest"))
    env = dict(env, SKYCTL_EXPECT_INSTALLED="1")
    log(f"  running the test suite from {tree} against the installed package")
    run([pytest_exe, "-q", "-p", "no:cacheprovider", *pytest_args], cwd=tree, env=env, capture=False)


def verify(artefact: Path, extras: str, tests: bool, pytest_args: list[str], keep: bool) -> None:
    version = version_of(artefact)
    scratch = Path(tempfile.mkdtemp(prefix="skyctl-verify-"))
    log(f"{artefact.name} (version {version}) in {scratch}")
    try:
        python, bindir, env = make_venv(scratch / "venv")
        run([str(python), "-m", "pip", "install", "-q", "--upgrade", "pip"], env=env)
        target = f"{artefact.resolve()}[{extras}]" if extras else str(artefact.resolve())
        run([str(python), "-m", "pip", "install", "-q", target], cwd=scratch, env=env)
        log("  installed")
        tree = source_tree_for(artefact, scratch)
        check_metadata(python, env, version)
        smoke(bindir, env, version, tree, scratch)
        if tests:
            run_tests(bindir, env, tree, pytest_args)
        log(f"  PASSED {artefact.name}")
    finally:
        if keep:
            log(f"  kept {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("artefacts", nargs="*", type=Path, help="wheels / sdists (default: everything in dist/)")
    parser.add_argument("--extras", default="all,test", help="extras to install (default: all,test)")
    parser.add_argument("--no-tests", action="store_true", help="skip the test suite")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directories")
    parser.add_argument("--pytest-args", default="", help="extra arguments for pytest, e.g. '-x -k cli'")
    args = parser.parse_args(argv)

    artefacts = args.artefacts or sorted(
        [*(ROOT / "dist").glob("skyctl-*.whl"), *(ROOT / "dist").glob("skyctl-*.tar.gz")]
    )
    if not artefacts:
        print("no artefacts: run `python -m build` first", file=sys.stderr)
        return 2
    failed = []
    for artefact in artefacts:
        try:
            verify(artefact, args.extras, not args.no_tests, args.pytest_args.split(), args.keep)
        except Failure as e:
            log(f"  FAILED {artefact.name}: {e}")
            failed.append(artefact.name)
    if failed:
        log(f"failed: {', '.join(failed)}")
        return 1
    log(f"all {len(artefacts)} artefact(s) verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
