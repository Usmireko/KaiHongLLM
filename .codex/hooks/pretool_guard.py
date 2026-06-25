#!/usr/bin/env python3
import json
import re
import sys

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-fdx\b",
    r"storage/runs",
    r"\bdel\s+/s\b",
    r"\bRemove-Item\b.*-Recurse",
]

def main():
    raw = sys.stdin.read()
    text = raw

    try:
        payload = json.loads(raw)
        text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        pass

    hits = []
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, text, flags=re.IGNORECASE):
            hits.append(pat)

    if hits:
        print("[PRETOOL_GUARD_WARNING]")
        print("Potentially dangerous command detected:")
        for h in hits:
            print(f"- {h}")
        print("Proceed only if this was explicitly requested and scoped.")
        # Return 0 initially to avoid breaking workflow.
        return 0

    return 0

if __name__ == "__main__":
    sys.exit(main())