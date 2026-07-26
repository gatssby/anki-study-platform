#!/usr/bin/env python3
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES = {"tagging_token.txt", ".env", "id_rsa", "id_ed25519"}
PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "bearer_literal": re.compile(rb"Authorization\s*[:=]\s*[\"']?Bearer\s+[A-Za-z0-9._~-]{20,}", re.I),
    "token_literal": re.compile(rb"X-Tagging-Token\s*[:=]\s*[\"'][A-Za-z0-9._~-]{20,}[\"']", re.I),
}


def candidate_files():
    git = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=False,
    )
    if git.returncode == 0:
        for raw in git.stdout.split(b"\0"):
            if raw:
                yield ROOT / raw.decode("utf-8")
        return
    for base in (ROOT / "addon-local", ROOT / "remote-backend", ROOT / "tests", ROOT / "scripts", ROOT / ".github"):
        if base.exists():
            yield from (path for path in base.rglob("*") if path.is_file() and "__pycache__" not in path.parts)


def main() -> int:
    findings = []
    for path in candidate_files():
        if path.name in FORBIDDEN_NAMES or path.suffix.casefold() in {".pem", ".key"}:
            findings.append(f"forbidden_name:{path.relative_to(ROOT)}")
            continue
        try:
            body = path.read_bytes()
        except OSError:
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(body):
                findings.append(f"{name}:{path.relative_to(ROOT)}")
    if findings:
        raise SystemExit("Potential secrets found:\n" + "\n".join(sorted(findings)))
    print("No secret literals found in versioned/source files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
