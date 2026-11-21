#!/usr/bin/env python3
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
Check (default) or add (--fix) the Apache-2.0 licence header on every
source file, like Apache RAT does for releases.

    python scripts/license_headers.py            # exit 1 and list files without a header
    python scripts/license_headers.py --fix      # add the header where missing

Generated files (deploy/generated/), lock files and docs are excluded; see EXCLUDE.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER_LINES = [
    "Copyright 2026 The skyctl Authors",
    "SPDX-License-Identifier: Apache-2.0",
    "",
    'Licensed under the Apache License, Version 2.0 (the "License");',
    "you may not use this file except in compliance with the License.",
    "You may obtain a copy of the License at",
    "",
    "    http://www.apache.org/licenses/LICENSE-2.0",
    "",
    "Unless required by applicable law or agreed to in writing, software",
    'distributed under the License is distributed on an "AS IS" BASIS,',
    "WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.",
    "See the License for the specific language governing permissions and",
    "limitations under the License.",
]
MARKER = "SPDX-License-Identifier: Apache-2.0"
HASH_COMMENT = {".py", ".toml", ".yml", ".yaml", ".sh", ".cfg"}
INCLUDE_NAMES = {"Dockerfile", ".dockerignore", ".gitignore", ".env.example"}
EXCLUDE = (
    "deploy/generated/",
    ".git/",
    "build/",
    "dist/",
    ".venv/",
    "venv/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".pytest_cache/",
    "__pycache__/",
    ".egg-info/",
    "htmlcov/",
)


def header(prefix: str = "#") -> str:
    return "\n".join(f"{prefix} {line}".rstrip() for line in HEADER_LINES) + "\n"


def candidates() -> list[Path]:
    out = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT).as_posix()
        if not path.is_file() or any(part in rel + "/" for part in EXCLUDE) or ".egg-info" in rel:
            continue
        if path.name == "setup.cfg" and path.read_text().lstrip().startswith("[egg_info]"):
            continue  # written by setuptools into every sdist, not a source file
        if path.suffix in HASH_COMMENT or path.name in INCLUDE_NAMES:
            out.append(path)
    return sorted(out)


def has_header(text: str) -> bool:
    return MARKER in "\n".join(text.splitlines()[:20])


def add_header(text: str) -> str:
    lines = text.splitlines(keepends=True)
    prefix = ""
    if lines and lines[0].startswith("#!"):
        prefix, lines = lines[0], lines[1:]
    body = "".join(lines)
    return prefix + header() + ("\n" if body and not body.startswith("\n") else "") + body


def main(argv: list[str]) -> int:
    fix = "--fix" in argv
    missing = []
    for path in candidates():
        text = path.read_text(encoding="utf-8")
        if has_header(text):
            continue
        if fix:
            path.write_text(add_header(text), encoding="utf-8")
        else:
            missing.append(path.relative_to(ROOT).as_posix())
    if missing:
        print("Missing the Apache-2.0 header (run: python scripts/license_headers.py --fix):")
        print("\n".join(f"  {m}" for m in missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
