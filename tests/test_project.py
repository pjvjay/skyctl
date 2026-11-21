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
Project hygiene: the things a release manager (and later the Apache
Incubator) checks by hand, checked on every change.

Tests that need the source tree (pyproject.toml, LICENSE, docs) skip when
the suite runs against an installed package (scripts/verify_install.py).
"""

import importlib
import importlib.metadata as md
import importlib.resources
import importlib.util
import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

import skyctl

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
IN_SOURCE_TREE = all(
    (ROOT / name).exists()
    for name in ("pyproject.toml", "MANIFEST.in", "skyctl/__init__.py", "scripts/license_headers.py")
)
source_tree = pytest.mark.skipif(not IN_SOURCE_TREE, reason="needs the source tree")


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


# ----------------------------------------------------------------------
# Package and metadata
# ----------------------------------------------------------------------


def test_version_is_pep440_and_matches_installed_metadata():
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?(\.dev\d+)?", skyctl.__version__)
    assert md.version("skyctl") == skyctl.__version__


@pytest.mark.skipif(not os.getenv("SKYCTL_EXPECT_INSTALLED"), reason="set by scripts/verify_install.py")
def test_suite_runs_against_the_installed_package():
    location = Path(skyctl.__file__).resolve()
    assert "site-packages" in location.parts, f"skyctl imported from {location}, not the installed package"


def test_installed_metadata_declares_apache_2():
    meta = md.metadata("skyctl")
    assert meta.get("License-Expression") == "Apache-2.0"
    assert meta["Requires-Python"] == ">=3.10"
    assert "MIT" not in (meta.get("License") or "")


def test_package_is_typed():
    assert importlib.resources.files("skyctl").joinpath("py.typed").is_file()


def test_public_api_is_importable():
    for name in skyctl.__all__:
        assert getattr(skyctl, name) is not None, name


@pytest.mark.parametrize(
    "module",
    [
        "skyctl.cli",
        "skyctl.remote",
        "skyctl.research",
        "skyctl.planning",
        "skyctl.agent",
        "skyctl.tools",
        "skyctl.platform",
        "skyctl.deploy",
        "skyctl.evals",
        "skyctl.testing",
        "skyctl.adapters",
    ],
)
def test_core_modules_import_without_optional_extras(module):
    importlib.import_module(module)


def test_console_script_entry_point():
    [ep] = [e for e in md.entry_points(group="console_scripts") if e.name == "skyctl"]
    assert ep.value == "skyctl.cli:cli"


def test_cli_version_and_every_command_has_help():
    from skyctl.cli import cli

    runner = CliRunner()
    assert runner.invoke(cli, ["--version"]).output.strip() == f"skyctl {skyctl.__version__}"

    def walk(group, path):
        for name, cmd in group.commands.items():
            assert (cmd.help or "").strip(), f"`skyctl {' '.join([*path, name])}` has no help text"
            result = runner.invoke(cli, [*path, name, "--help"])
            assert result.exit_code == 0, (path, name, result.output)
            if hasattr(cmd, "commands"):
                walk(cmd, [*path, name])

    walk(cli, [])


# ----------------------------------------------------------------------
# pyproject.toml
# ----------------------------------------------------------------------


@source_tree
def test_pyproject_licence_and_python_versions():
    project = _pyproject()["project"]
    assert project["license"] == "Apache-2.0"
    assert set(project["license-files"]) >= {"LICENSE", "NOTICE"}
    assert not any(c.startswith("License ::") for c in project["classifiers"]), "PEP 639: no licence classifiers"
    versions = {c.rsplit(" ", 1)[-1] for c in project["classifiers"] if re.match(r".*Python :: 3\.\d+$", c)}
    assert versions == {"3.10", "3.11", "3.12", "3.13"}
    assert project["requires-python"] == ">=3.10"
    assert project["dynamic"] == ["version"]


@source_tree
def test_extras_reference_only_existing_extras():
    extras = _pyproject()["project"]["optional-dependencies"]
    for name, deps in extras.items():
        for dep in deps:
            m = re.fullmatch(r"skyctl\[([^\]]+)\]", dep)
            if m:
                for inner in m.group(1).split(","):
                    assert inner in extras, f"extra {name!r} references missing extra {inner!r}"
    assert {"agents", "mcp", "aws", "azure", "gcp", "all", "test", "dev"} <= set(extras)


@source_tree
def test_mcp_sdk_stays_on_1x():
    extras = _pyproject()["project"]["optional-dependencies"]
    pins = [d for deps in extras.values() for d in deps if d.startswith("mcp")]
    assert pins and all("<2" in p for p in pins), pins


# ----------------------------------------------------------------------
# Licensing (what Apache RAT and the Incubator check)
# ----------------------------------------------------------------------


@source_tree
def test_license_is_the_apache_2_text():
    text = (ROOT / "LICENSE").read_text()
    assert text.lstrip().startswith("Apache License")
    assert "Version 2.0, January 2004" in text and "END OF TERMS AND CONDITIONS" in text
    assert "Permission is hereby granted, free of charge" not in text  # no MIT remnants


@source_tree
def test_notice_and_authors():
    notice = (ROOT / "NOTICE").read_text()
    assert notice.startswith("skyctl\n") and "The skyctl Authors" in notice
    authors = [line for line in (ROOT / "AUTHORS").read_text().splitlines() if line and not line.startswith("#")]
    assert authors, "AUTHORS lists the copyright holders"


@source_tree
def test_every_source_file_has_the_licence_header():
    spec = importlib.util.spec_from_file_location("license_headers", ROOT / "scripts" / "license_headers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    missing = [p.relative_to(ROOT).as_posix() for p in mod.candidates() if not mod.has_header(p.read_text())]
    assert missing == [], "run: python scripts/license_headers.py --fix"


@source_tree
def test_no_mit_licence_left_in_sources():
    offenders = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "skyctl").rglob("*.py")
        if re.search(r"MIT License|SPDX-License-Identifier: MIT", p.read_text())
    ]
    assert offenders == []


# ----------------------------------------------------------------------
# Documentation and packaging manifest
# ----------------------------------------------------------------------

_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _markdown_files() -> list[Path]:
    return sorted([*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md")])


@source_tree
def test_relative_markdown_links_resolve():
    broken = []
    for doc in _markdown_files():
        text = re.sub(r"(?s)```.*?```", "", doc.read_text())  # links inside code blocks are examples
        for target in _MD_LINK.findall(text):
            if re.match(r"[a-z]+:", target) or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            if path and not (doc.parent / path).exists():
                broken.append(f"{doc.relative_to(ROOT)} → {target}")
    assert broken == []


@source_tree
def test_markdown_anchors_to_other_documents_exist():
    def anchors(doc: Path) -> set[str]:
        out = set()
        for heading in re.findall(r"(?m)^#+ (.+)$", re.sub(r"(?s)```.*?```", "", doc.read_text())):
            slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
            out.add(slug)
        return out

    broken = []
    for doc in _markdown_files():
        for target in _MD_LINK.findall(doc.read_text()):
            if re.match(r"[a-z]+:", target) or "#" not in target:
                continue
            path, anchor = target.split("#", 1)
            dest = (doc.parent / path) if path else doc
            if dest.suffix == ".md" and dest.exists() and anchor not in anchors(dest):
                broken.append(f"{doc.relative_to(ROOT)} → {target}")
    assert broken == []


@source_tree
def test_manifest_includes_exist():
    missing = []
    for line in (ROOT / "MANIFEST.in").read_text().splitlines():
        words = line.split()
        if words and words[0] in ("include", "graft"):
            missing += [w for w in words[1:] if not (ROOT / w).exists()]
    assert missing == []


@source_tree
def test_project_documents_exist():
    for name in (
        "README.md",
        "LICENSE",
        "NOTICE",
        "AUTHORS",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "GOVERNANCE.md",
        "RELEASING.md",
        "docs/README.md",
        "docs/USAGE.md",
        "docs/OPERATIONS.md",
        "scripts/verify_install.py",
        "scripts/license_headers.py",
        "scripts/check_dependency_licenses.py",
    ):
        assert (ROOT / name).is_file(), name


@source_tree
def test_container_image_ships_license_and_notice():
    dockerfile = (ROOT / "Dockerfile").read_text()
    copy = next(line for line in dockerfile.splitlines() if line.startswith("COPY pyproject.toml"))
    assert {"LICENSE", "NOTICE"} <= set(copy.split()), "license-files must be in the build context"
    assert 'org.opencontainers.image.licenses="Apache-2.0"' in dockerfile


@source_tree
def test_changelog_has_an_entry_for_this_version():
    assert re.search(rf"^## \[{re.escape(skyctl.__version__)}\]", (ROOT / "CHANGELOG.md").read_text(), re.M)


@source_tree
def test_readme_shows_the_headline_command_and_it_parses():
    from skyctl.remote import parse_description

    readme = (ROOT / "README.md").read_text()
    assert "skyctl run_cloud_task --cloud aws -- description" in readme
    assert parse_description(None, ("description", "Find a post")) == "Find a post"
