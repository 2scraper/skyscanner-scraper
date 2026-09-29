#!/usr/bin/env python3
"""repo_scan.py — the repo's pre-publication scans, ONE implementation.

CI runs `python3 repo_scan.py`; `smoke_test.py` imports the same functions
(CLAUDE.md §17: one implementation, invoked from both). Two scans:

- `credential_findings()`: key-shaped assignments (tolerating the escaped
  `\\"` quotes a JSON fixture stores — §24: a bare-quote rule matched zero
  times in the file most likely to hold a front-end key), bare 32-hex
  strings (the shape of a 2Captcha key), and `scheme://user:pass@host`
  URLs with non-placeholder credentials. Every text file is scanned,
  smoke_test.py included; a deliberately fake credential in a test is
  allowed only on a line carrying the marker `scan: fake-credential`.
- `wording_findings()`: phrases the family bans (§12) in every shipped
  file, and capability over-claims (§19) outside CHANGELOG.md's history.
  The phrases are assembled from pieces so this file can scan itself (§22).

Exit 0 when clean, 1 with one line per finding.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional

ROOT = Path(__file__).resolve().parent
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yml", ".yaml", ".json", ".csv", ".html", ".cfg", ".ini", ""}
SKIP_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", "dist", "build"}
FAKE_MARKER = "scan: fake-credential"
# Passwords the test suite has used in fake URLs since its first commit —
# the marker cannot be added to history, so the values are named instead.
FAKE_TEST_PASSWORDS = {"s3cr3t", "secretpass"}

_Q = r'\\?["\']'  # a quote, possibly JSON-escaped
KEY_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|apikey|client[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token|"
    r"twocaptcha_key|skyscanner_proxy|skyscanner_cdp_endpoint)" + _Q + r"?\s*[:=]\s*" + _Q
    + r"(?P<value>[A-Za-z0-9_\-./+]{16,})" + _Q
)
BARE_HEX32 = re.compile(r"(?<![A-Fa-f0-9])[a-f0-9]{32}(?![A-Fa-f0-9])")
URL_CREDENTIALS = re.compile(r"\b[a-z][a-z0-9+.-]*://(?P<user>[^\s:/@'\"{}<>]+):(?P<pw>[^\s@/'\"{}<>]+)@")
_PLACEHOLDER = re.compile(r"(?i)^(?:user(?:name)?|login|pass(?:word)?|pw|pwd|x+|\*+|secret|key|your[_-]?\w*|u|p|name|test|fake|example|redacted|\.\.\.)$")

# §12: never ship these (assembled so this file does not match itself).
BANNED_EVERYWHERE = (
    "cloud" + " browser", "anti" + "detect browser", "2scraper anti" + "detect",
    "gate." + "2prx.com", "--anti" + "detect", "anti" + "detect_local_api",
    "undetect" + "able", "100% " + "success", "bypass " + "all", "never gets " + "blocked", "guaranteed " + "to work",
)
# §19: capability claims about a vendor. CHANGELOG.md keeps its history.
BANNED_OUTSIDE_HISTORY = (
    "confirmed " + "permanent", "confirmed-" + "permanent", "no automated " + "task type",
    "has no automated " + "solve", "cannot be " + "solved", "structurally " + "unsolvable",
)
HISTORY_FILES = {"CHANGELOG.md"}


def shipped_files(root: Path = ROOT) -> List[Path]:
    """Tracked files when this is a git checkout (what would be published);
    otherwise every text file under root (e.g. inside the Docker image)."""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True, timeout=20)
        paths = [root / p for p in out.stdout.decode("utf-8").split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        paths = [p for p in root.rglob("*") if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS)]
    return [p for p in paths if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES]


def _lines(path: Path) -> Iterable[tuple]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    return enumerate(text.splitlines(), start=1)


def credential_findings(files: Optional[List[Path]] = None, root: Path = ROOT) -> List[str]:
    found = []
    for path in files if files is not None else shipped_files(root):
        rel = path.relative_to(root) if path.is_relative_to(root) else path
        for n, line in _lines(path):
            if FAKE_MARKER in line:
                continue
            if KEY_ASSIGNMENT.search(line):
                found.append(f"{rel}:{n}: key-shaped assignment")
            if BARE_HEX32.search(line):
                found.append(f"{rel}:{n}: 32-hex string (2Captcha key shape)")
            for m in URL_CREDENTIALS.finditer(line):
                if not (_PLACEHOLDER.match(m.group("user")) or _PLACEHOLDER.match(m.group("pw"))
                        or m.group("pw") in FAKE_TEST_PASSWORDS):
                    found.append(f"{rel}:{n}: credentials inside a URL")
    return found


def wording_findings(files: Optional[List[Path]] = None, root: Path = ROOT) -> List[str]:
    found = []
    for path in files if files is not None else shipped_files(root):
        rel = path.relative_to(root) if path.is_relative_to(root) else path
        phrases = BANNED_EVERYWHERE + (() if path.name in HISTORY_FILES else BANNED_OUTSIDE_HISTORY)
        for n, line in _lines(path):
            low = line.lower()
            found += [f"{rel}:{n}: banned wording {p!r}" for p in phrases if p in low]
    return found


def history_credential_findings(root: Path = ROOT) -> List[str]:
    """Credential scan over EVERY blob in the git history (all refs): a commit
    on top cannot remove what an older commit or tag already holds."""
    import tempfile
    objs = subprocess.run(["git", "rev-list", "--objects", "--all"], cwd=root,
                          capture_output=True, text=True, check=True, timeout=60).stdout.splitlines()
    found = []
    with tempfile.TemporaryDirectory() as d:
        for line in objs:
            sha, _, name = line.partition(" ")
            if not name or Path(name).suffix.lower() not in TEXT_SUFFIXES:
                continue
            blob = subprocess.run(["git", "cat-file", "-p", sha], cwd=root, capture_output=True, timeout=20).stdout
            tmp = Path(d) / Path(name).name
            tmp.write_bytes(blob)
            found += [f"{name}@{sha[:8]}{x[len(tmp.name):]}" for x in credential_findings([tmp], Path(d))]
    return sorted(set(found))


def main() -> int:
    if sys.argv[1:] == ["--history"]:
        findings = history_credential_findings()
        for f in findings:
            print(f)
        print(f"repo_scan --history: {len(findings)} finding(s)", file=sys.stderr)
        return 1 if findings else 0
    findings = credential_findings() + wording_findings()
    for f in findings:
        print(f)
    print(f"repo_scan: {len(shipped_files())} files scanned, {len(findings)} finding(s)", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
