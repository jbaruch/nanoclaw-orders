"""Guard: tile.json's version must have a stamped CHANGELOG heading.

Versions 0.1.7–0.1.9 published without CHANGELOG entries (issue #17):
their PRs added no un-headed `### ` entry blocks, so the publish
workflow's stamp step had nothing to stamp and `tile.json` advanced
while `CHANGELOG.md` stood still.

This test pins the invariant the publish pipeline maintains when
authors do their part: the FIRST `## X.Y.Z` heading in CHANGELOG.md
names exactly the version `tile.json` declares. Un-headed entry
blocks above it are the pre-publish staging area and are ignored.

When a release slips through without an entry again, the very next
PR (and the next push to main) fails here, and the gap gets
backfilled one version deep instead of three.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HEADING_RE = re.compile(r"^## (\d+\.\d+\.\d+)(?=\s|$)", re.MULTILINE)


def test_first_changelog_heading_matches_tile_version():
    tile_version = json.loads((REPO_ROOT / "tile.json").read_text())["version"]
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text()
    match = HEADING_RE.search(changelog)
    assert match is not None, (
        "CHANGELOG.md has no '## X.Y.Z' release heading at all — restore the "
        "stamped release history (see issue #17)"
    )
    assert match.group(1) == tile_version, (
        f"CHANGELOG.md's first release heading is {match.group(1)} but tile.json "
        f"declares {tile_version} — a release published without a CHANGELOG "
        f"entry. Backfill a '## {tile_version} — <date>' section for it (and an "
        f"un-headed '### ' block for any in-flight change) before merging"
    )
