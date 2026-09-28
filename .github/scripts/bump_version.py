"""Bump the app's patch version everywhere it is declared and print ``tag=vX.Y.Z`` for GITHUB_OUTPUT.

Used by .github/workflows/yt-dlp-autorelease.yml. Fails loudly if any declaration is missing, so a
release is never cut with mismatched version numbers (the updater compares app_version to the tag).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"

# (file, pattern) — group 1 is the text before the version, group 2 the version, group 3 the text after.
DECLARATIONS: tuple[tuple[Path, str], ...] = (
    (PYPROJECT, r'(^version = ")(\d+\.\d+\.\d+)(")'),
    (ROOT / "app" / "core" / "config.py", r'(app_version: str = Field\(\s*default=")(\d+\.\d+\.\d+)(")'),
    (ROOT / "app" / "core" / "config.py", r'(\("app_version", None, ")(\d+\.\d+\.\d+)("\))'),
    (ROOT / "test_settings.py", r'(assertEqual\(settings\.app\.app_version, ")(\d+\.\d+\.\d+)("\))'),
)


def current_version() -> str:
    match = re.search(DECLARATIONS[0][1], PYPROJECT.read_text(encoding="utf-8"), re.M)
    if not match:
        raise SystemExit('pyproject.toml has no version = "X.Y.Z" line')
    return match.group(2)


def next_patch(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def main() -> int:
    old = current_version()
    new = next_patch(old)
    # Validate every declaration before writing anything, so a mismatch never leaves half-bumped files.
    texts: dict[Path, str] = {}
    for path, pattern in DECLARATIONS:
        text = texts.get(path) or path.read_text(encoding="utf-8")
        found = re.findall(pattern, text, re.M)
        if len(found) != 1:
            raise SystemExit(f"{path.relative_to(ROOT)}: expected one declaration for {pattern!r}, found {len(found)}")
        if found[0][1] != old:
            raise SystemExit(f"{path.relative_to(ROOT)} has version {found[0][1]}, expected {old}")
        texts[path] = re.sub(pattern, lambda m: f"{m.group(1)}{new}{m.group(3)}", text, flags=re.M)
    for path, text in texts.items():
        path.write_text(text, encoding="utf-8", newline="\n")  # repo files are LF
    print(f"tag=v{new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
