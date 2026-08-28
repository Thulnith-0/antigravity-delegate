"""Stand-in for the real `agy` binary, driven by the FAKE_AGY_BEHAVIOR env var.

Tests point ANTIGRAVITY_CLI_PATH at this file so antigravity_mcp.py's subprocess
and JSON-parsing logic can be exercised without a real Antigravity account.
Behaviors mirror what was empirically observed from the real CLI (agy 1.1.22):
default headless mode auto-approves writes but auto-denies shell commands with
a stderr message, rather than hanging.
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    behavior = os.environ.get("FAKE_AGY_BEHAVIOR", "success")

    if behavior == "hang":
        time.sleep(300)
        return 0

    if behavior == "nonzero_no_output":
        sys.stderr.write("fake agy: simulated crash\n")
        return 1

    if behavior == "malformed_json":
        sys.stdout.write("not json at all\n")
        return 0

    if behavior == "shell_denied":
        # Mirrors the real message observed when headless mode can't prompt
        # for the "command" permission.
        sys.stderr.write(
            'jetski: no output produced -- a tool required the "command" '
            "permission that headless mode cannot prompt for, so it was "
            "auto-denied.\n"
        )
        payload = {
            "conversation_id": "fake-conv-id",
            "status": "SUCCESS",
            "response": "",
            "duration_seconds": 0.01,
            "usage": {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10},
        }
        sys.stdout.write(json.dumps(payload))
        return 0

    # default: "success"
    payload = {
        "conversation_id": "fake-conv-id",
        "status": "SUCCESS",
        "response": f"fake response for argv={sys.argv[1:]}",
        "duration_seconds": 0.01,
        "usage": {"input_tokens": 42, "output_tokens": 7, "total_tokens": 49},
    }
    sys.stdout.write(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
