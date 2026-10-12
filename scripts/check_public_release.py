"""Scan a source-only release; optional private deny patterns never enter the export."""
import argparse
import re
from pathlib import Path

IGNORE_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "bin", "obj"}
PRIVATE_PATH = re.compile(r"(?i)(?:[A-Z]:[/\\]+Users[/\\]+|/(?:home|Users)/[^/\s]+/)")
INTERNAL_ADDRESS = re.compile(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
TOKEN = re.compile(r"\b(?:(?:AKIA|ASIA)[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{30,})\b")
CREDENTIAL = re.compile(r'''(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=]\s*["']([^"'\r\n]+)["']''')
PRIVATE_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".dll", ".exe", ".pdb", ".pyc", ".docx", ".jsonl"}

def scan(root: Path, deny_patterns=()):
    patterns = [re.compile(p, re.IGNORECASE) for p in deny_patterns]
    findings = []
    checked = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] in ("dist", "build") or any(part in IGNORE_DIRS for part in relative.parts):
            continue
        label = relative.as_posix()
        if any(pattern.search(label) for pattern in patterns):
            findings.append((label, "private identifier in filename"))
        if not path.is_file():
            continue
        checked += 1
        if path.name == "config.toml" or path.name.startswith(".env") or path.suffix in PRIVATE_EXTENSIONS:
            findings.append((label, "private/generated artifact"))
            continue
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            findings.append((label, "unreviewed binary content"))
            continue
        if any(pattern.search(content) for pattern in patterns):
            findings.append((label, "private identifier in content"))
        if PRIVATE_PATH.search(content):
            findings.append((label, "personal filesystem path"))
        if INTERNAL_ADDRESS.search(content):
            findings.append((label, "internal network address"))
        if TOKEN.search(content):
            findings.append((label, "credential token pattern"))
        if any(not match.group(1).lower().startswith(("example", "placeholder", "your_", "changeme"))
               for match in CREDENTIAL.finditer(content)):
            findings.append((label, "literal credential assignment"))
        if re.search(r"(?m)^-----BEGIN (?:RSA )?PRIVATE KEY-----$", content):
            findings.append((label, "private key material"))
    return checked, findings

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--deny-pattern", action="append", default=[])
    args = parser.parse_args()
    count, findings = scan(args.root.resolve(), args.deny_pattern)
    # Never print matching source text, credentials or denied identifiers.
    for path, category in findings:
        print(f"{path}: {category}")
    print(f"Scanned {count} source files: {len(findings)} finding(s).")
    raise SystemExit(1 if findings else 0)

if __name__ == "__main__":
    main()
