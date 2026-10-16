"""
Lesson 01 - No API keys.

Run:  python find_keys.py                  (scans ./sample-config)
      python find_keys.py <folder>         (scans your own folder - read-only)

Python 3.8+, standard library only, made-up data, no API key needed.

The pattern:
  1. Every service authenticates to the model as an identity (on Google Cloud:
     Workload Identity), never with a key stored in a file.
  2. Before go-live, check that no service still holds a key - or quietly
     expects one at runtime.

This script is a simple stand-in for a real secret scanner. It looks at each
service folder and reports one of three things:
  KEY STORED    - a value that looks like a credential is written in a file
  KEY EXPECTED  - the config asks for a key from the environment at runtime
  IDENTITY      - the service authenticates as an identity, nothing stored
"""

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. What a credential looks like
# ---------------------------------------------------------------------------
# A setting whose name suggests a secret: api_key, apiKey, token, secret, password
SECRET_NAME = re.compile(r"(api[_-]?key|token|secret|password)", re.IGNORECASE)
# "name: value" or "name = value", with an optional quote around the value
SETTING = re.compile(r"""^\s*([\w.-]+)\s*[:=]\s*["']?([^"'\s#]+)""")
# A value read from the environment at runtime: ${SOMETHING} or $SOMETHING
ENV_REFERENCE = re.compile(r"^\$\{?\w+\}?$")
# A key pasted anywhere in free text (notes, docs, chat exports)
KEY_IN_TEXT = re.compile(r"\b(?:key|token)[\s:=-]+([A-Za-z0-9_-]{20,})", re.IGNORECASE)
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
# How a service says "I authenticate as an identity"
IDENTITY = re.compile(r"^\s*(auth|service_account)\s*:\s*(\S+)", re.IGNORECASE)


def mask(value):
    """Show just enough of a secret to find it, never the whole thing."""
    return value[:4] + "..." + f"({len(value)} chars)"


# ---------------------------------------------------------------------------
# 2. Scan one service folder
# ---------------------------------------------------------------------------
def scan_service(folder):
    stored, expected, identities = [], [], []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for n, line in enumerate(lines, start=1):
            where = f"{path.relative_to(folder)}:{n}"
            if PRIVATE_KEY.search(line):
                stored.append(f"{where}  private key block")
                continue
            setting = SETTING.match(line)
            if setting and SECRET_NAME.search(setting.group(1)):
                name, value = setting.groups()
                if ENV_REFERENCE.match(value):
                    expected.append(f"{where}  {name} = {value}")
                elif len(value) >= 12:
                    stored.append(f"{where}  {name} = {mask(value)}")
                continue
            ident = IDENTITY.match(line)
            if ident:
                identities.append(f"{where}  {ident.group(1)}: {ident.group(2)}")
                continue
            pasted = KEY_IN_TEXT.search(line)
            if pasted:
                stored.append(f"{where}  pasted in text: {mask(pasted.group(1))}")
    return stored, expected, identities


# ---------------------------------------------------------------------------
# 3. Report, one service at a time
# ---------------------------------------------------------------------------
def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "sample-config"
    services = sorted(p for p in root.iterdir() if p.is_dir())
    print(f"Scanning {len(services)} services in {root}\n")

    holding_keys = 0
    for service in services:
        stored, expected, identities = scan_service(service)
        if stored or expected:
            holding_keys += 1
        verdict = "KEY STORED" if stored else "KEY EXPECTED" if expected else \
                  "IDENTITY" if identities else "NO AUTH FOUND"
        print(f"{service.name:<18} {verdict}")
        for finding in stored:
            print(f"    stored:   {finding}")
        for finding in expected:
            print(f"    expected: {finding}   <- fails, or tempts someone to add a key")
        for finding in identities:
            print(f"    identity: {finding}")
        print()

    if holding_keys:
        print(f"{holding_keys} of {len(services)} services still hold or expect a key. "
              "Move them to an identity before go-live.")
    else:
        print("No keys found. Every service authenticates as an identity.")
    return 1 if holding_keys else 0


if __name__ == "__main__":
    sys.exit(main())
