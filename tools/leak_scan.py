#!/usr/bin/env python3
"""leak_scan.py — fail if the repository contains identifiers that must never be published.

Usage:
    python tools/leak_scan.py                 # scan the repo this file lives in
    python tools/leak_scan.py --root DIR      # scan another tree (e.g. an export staging dir)

Exit code 0 = clean, 1 = at least one hit (each printed as path:line: [rule] excerpt).

What is scanned
  * Files: `git ls-files` + untracked-but-not-ignored files when DIR is a git work tree,
    otherwise every file under DIR (skipping .git, __pycache__, .runtime).
  * Text files line by line; binary/image files by FILE NAME only.

Rules
  1. hashed-term   Organisation / person / internal-system names. They are stored only as
                   truncated SHA-256 hashes so this public file does not itself leak them.
                   A sliding window over each (lower-cased) line is hashed and compared.
                   Extra plaintext terms can be added locally in `.leakterms` (git-ignored,
                   one term per line) — never commit that file.
  2. ip-private    10.x.x.x, 192.168.x.x, 172.16-31.x.x addresses.
  3. win-user-path Windows user/profile or cloud-drive paths (C:\\Users\\..., "My Drive" style).
  4. emp-id        8-digit numbers starting with 1 or 4 (the employee-ID shape of the source org).
  5. z-object      Customer-specific SAP repository objects (Z*/Y*/SAPMZ*/LZ*).
                   Allowed placeholders: ZXX*, YXX*, SAPMZXX*, YYYY*.
  6. inq-id        Real-looking inquiry IDs INQ-YYMMDD-HHMMSS. Allowed only in files under
                   sample-data/ or tests/ that carry the marker "SAMPLE" (synthetic data).
  7. phone / email Korean mobile numbers; e-mail addresses outside example.com/example.org.

Escape hatch: a line containing `leak-scan: allow` skips rules 2-7 (never rule 1).
Use it only for false positives (e.g. a numeric constant) and say why next to it.
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys

HASHED_TERMS = [  # (sha256(term)[:24], len(term), word-boundary-required)
    ("6ea61399f6775fb819b546c9", 2, 0),
    ("4453d17e912034082cb629d3", 6, 0),
    ("33bc4edc0668da5553366159", 5, 0),
    ("4a64c4f07c1571cb4cab0a50", 7, 0),
    ("4c27b3a9210322ccc25b4fd1", 4, 0),
    ("d7dce507edb00980fa5c7232", 6, 0),
    ("f1e53d79c5ce74a90d9698bb", 4, 0),
    ("4720682d1322963890fe5c37", 5, 1),
    ("47612b3175fece07f6c3e919", 4, 1),
    ("311d4e1e0c348acd05e33b9a", 9, 0),
    ("44380b77e3fe558614ce65af", 3, 0),
    ("ac49f94dd46c5d09575d1e46", 2, 0),
    ("1a52983333b2bf4276de90d9", 6, 0),
    ("eae05a1ed500c260c397c0df", 3, 0),
    ("1b553998876fed7b4908862a", 3, 0),
    ("212e75672693c40b60b18898", 3, 0),
    ("d44f6a5a66121b625b55cc1b", 4, 0),
    ("1248712441dbbf43bb37f91d", 3, 1),
    ("77909277a1ffc4b06a39d95f", 3, 1),
    ("2a96eefc83685400b1eaa8dd", 3, 1),
    ("1e8d546936a4361f69de9f3c", 10, 0),
    ("109136949994d4f56227c8a8", 5, 0),
    ("7efc723ca54fd16b9f181111", 11, 0),
    ("50f9080332a99fbd02ffac28", 4, 0),
]

REGEX_RULES = [
    ("ip-private", re.compile(r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
                              r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])")),
    ("win-user-path", re.compile(r"(?i)\b[A-Z]:[\\/]+(?:Users|Documents and Settings)[\\/]+\w|내 드라이브|My Drive[\\/]|Google Drive[\\/]")),
    ("emp-id", re.compile(r"(?<![\w#.\-/:])[14]\d{7}(?![\w.])")),
    ("z-object", re.compile(r"(?<![A-Za-z0-9_\-/.])(?!(?:ZXX|YXX|SAPMZXX|YYYY))(?:SAPMZ|SAPLZ|LZ|MZ|Z|Y)[A-Z][A-Z0-9_]{2,}(?![a-z])")),
    ("z-object", re.compile(r"(?i)\bz(?:tsd|rsd|ssd|esd|tco|dti|sdd|rmm|tmm)[\d_-]")),
    ("phone", re.compile(r"(?<!\d)01[016789]-\d{3,4}-\d{4}(?!\d)")),
    ("email", re.compile(r"[\w.+-]+@(?!(?:example\.(?:com|org)|users\.noreply\.github\.com|anthropic\.com)\b)[A-Za-z0-9-]+\.[A-Za-z.]{2,}")),
]
INQ_RE = re.compile(r"\bINQ-\d{6}-\d{6}\b")
# Words that look like Z/Y objects but are ordinary text/code tokens.
Z_ALLOW = {"ZWSP", "YAML", "YES", "YOUR", "YOU", "YEAR", "YMD", "ZIP", "ZONE", "ZERO", "ZOOM", "YAxis", "YELLOW", "YTD"}

BIN_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".xlsx", ".docx", ".pptx", ".woff", ".woff2", ".ttf"}
SKIP_DIRS = {".git", "__pycache__", ".runtime", "node_modules"}
SELF = os.path.abspath(__file__)


def list_files(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, check=True)
        if os.path.isdir(os.path.join(root, ".git")):
            return [p for p in out.stdout.decode("utf-8").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        pass
    res = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            res.append(os.path.relpath(os.path.join(d, f), root).replace("\\", "/"))
    return res


def load_local_terms(root):
    terms = []
    p = os.path.join(root, ".leakterms")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8"):
            t = line.strip().lower()
            if t and not t.startswith("#"):
                terms.append(t)
    return terms


# First characters of the hashed terms: only windows starting with one of these are hashed (speed).
FIRST_CHARS = set("동d아i한원w이오구n네스c")
_BY_LEN = {}
for _h, _n, _b in HASHED_TERMS:
    _BY_LEN.setdefault(_n, {})[_h] = _b


def hashed_hits(line):
    low = line.lower()
    hits = []
    for i, ch in enumerate(low):
        if ch not in FIRST_CHARS:
            continue
        for n, table in _BY_LEN.items():
            if i + n > len(low):
                continue
            w = low[i:i + n]
            bound = table.get(hashlib.sha256(w.encode("utf-8")).hexdigest()[:24])
            if bound is not None:
                if bound:
                    a = low[i - 1] if i > 0 else " "
                    b = low[i + n] if i + n < len(low) else " "
                    if (a.isalpha() and a.isascii()) or (b.isalpha() and b.isascii()):
                        continue
                hits.append(w[:1] + "*" * (n - 1))
    return hits


def scan(root):
    root = os.path.abspath(root)
    local_terms = load_local_terms(root)
    hits = []
    for rel in sorted(list_files(root)):
        path = os.path.join(root, rel)
        if os.path.abspath(path) == SELF or rel in (".leakterms", "tools/leak_scan.py") or not os.path.isfile(path):
            continue
        # file names are always checked
        for h in hashed_hits(rel):
            hits.append((rel, 0, "hashed-term(name)", h))
        if os.path.splitext(rel)[1].lower() in BIN_EXT:
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except UnicodeDecodeError:
            continue
        sample_ok = (rel.startswith(("sample-data/", "tests/")) or "/tests/" in rel) and "SAMPLE" in text
        for no, line in enumerate(text.splitlines(), 1):
            for h in hashed_hits(line):
                hits.append((rel, no, "hashed-term", h))
            low = line.lower()
            for t in local_terms:
                if t in low:
                    hits.append((rel, no, "local-term", t[:1] + "*" * (len(t) - 1)))
            if "leak-scan: allow" in line:
                continue
            for name, rx in REGEX_RULES:
                for m in rx.finditer(line):
                    tok = m.group(0)
                    if name == "z-object" and (tok in Z_ALLOW or tok.isdigit()):
                        continue
                    hits.append((rel, no, name, tok))
            if not sample_ok:
                for m in INQ_RE.finditer(line):
                    hits.append((rel, no, "inq-id", m.group(0)))
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(SELF)))
    a = ap.parse_args()
    hits = scan(a.root)
    for rel, no, rule, tok in hits:
        print("%s:%d: [%s] %s" % (rel, no, rule, tok))
    if hits:
        print("LEAK SCAN FAILED: %d hit(s)" % len(hits))
        return 1
    print("leak scan clean (%s)" % os.path.abspath(a.root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
