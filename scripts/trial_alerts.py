"""Filter a comparison trial's event stream down to lines worth acting on.

Reads JSON event lines on stdin (from run_engine_comparison logs) and prints trial
boundaries, errors, and model calls that breach a code-owned expectation:
decode below 20 tok/s (Ollama), prefill above 15 s without a compaction (a cache
break), prompt above the working budget, or a changed tool schema mid-trial.
Each printed call is prefixed with its flags. Non-JSON lines pass through only when
they look like failures.

Usage: tail -F LOGS | .venv/bin/python scripts/trial_alerts.py [BUDGET_TOKENS]
"""

import json
import sys

BUDGET = int(sys.argv[1]) if len(sys.argv) > 1 else 32768


def main() -> None:
    schema: str | None = None
    messages = 0
    for raw in sys.stdin:
        line = raw.strip()
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if any(word in line for word in ("Traceback", "Error", "Killed")):
                print(line, flush=True)
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("event")
        if kind in {"trial_start", "trial_end"} or "verified_success" in event:
            schema, messages = None, 0
            print(line, flush=True)
            continue
        if kind != "model_call":
            continue
        flags = []
        compacted = event.get("messages", 0) < messages
        messages = event.get("messages", messages)
        decode = event.get("decode_tok_s")
        if decode is not None and decode < 20 and (event.get("output_tokens") or 0) > 32:
            flags.append(f"decode {decode} tok/s")
        prefill = event.get("prefill_s")
        if prefill is not None and prefill > 15 and not compacted:
            flags.append(f"prefill {prefill}s without compaction")
        if (event.get("prompt_tokens") or 0) > BUDGET:
            flags.append(f"prompt {event['prompt_tokens']} > budget {BUDGET}")
        current = event.get("tool_schema")
        if schema is not None and current != schema:
            flags.append("tool schema changed")
        schema = current or schema
        if compacted:
            flags.append("compacted")
        tag = "ALERT " if any(f != "compacted" for f in flags) else ""
        print(f"{tag}{'; '.join(flags) or 'ok'} | {line}", flush=True)


if __name__ == "__main__":
    main()
