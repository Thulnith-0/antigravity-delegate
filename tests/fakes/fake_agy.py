"""Stand-in for the real `agy` binary, driven by the FAKE_AGY_BEHAVIOR env var.

Tests point ANTIGRAVITY_CLI_PATH at this file so antigravity_delegate.py's subprocess
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
        # for the "command" permission, including the structured
        # denied_actions field agy 1.3.3 reports alongside the prose.
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
            "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
        }
        sys.stdout.write(json.dumps(payload))
        return 0

    if behavior == "write_denied":
        # Same shape, for the write_file permission.
        payload = {
            "conversation_id": "fake-conv-id",
            "status": "SUCCESS",
            "response": "",
            "duration_seconds": 0.01,
            "usage": {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10},
            "denied_actions": [{"action": "write_file", "display_name": "WriteFile"}],
        }
        sys.stdout.write(json.dumps(payload))
        return 0

    if behavior == "structured":
        # What agy returns when --json-schema is supplied: the validated object
        # under structured_output, in addition to the prose response.
        payload = {
            "conversation_id": "fake-conv-id",
            "status": "SUCCESS",
            "response": "Transcribed 1 entry.",
            "duration_seconds": 0.01,
            "usage": {"input_tokens": 42, "output_tokens": 7, "total_tokens": 49},
            "structured_output": {
                "entries": [{"file": "ledger.png", "entry_number": 47}]
            },
        }
        sys.stdout.write(json.dumps(payload))
        return 0

    # default: "success". argv is echoed into the response so that tests can
    # assert on the exact command line the server generated.
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
