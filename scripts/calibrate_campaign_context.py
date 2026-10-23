"""Measure saved exact provider requests; never changes runtime context bounds.

Provider prompt counts are observations, not proof of a universal tokenizer
bound. Retain conservative UTF8 accounting until a matched renderer is available.
"""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = []
    for path in sorted(args.evidence.glob("harness-*.json")):
        trial = json.loads(path.read_text())
        for exchange in trial.get("exchanges", []):
            request, response = exchange["request"], exchange["response"]
            serialized = json.dumps(request, ensure_ascii=False).encode()
            count = response.get("prompt_eval_count")
            records.append(
                {
                    "trial": path.name,
                    "request_sha256": hashlib.sha256(serialized).hexdigest(),
                    "request_bytes": len(serialized),
                    "prompt_tokens": count,
                    "cached_prompt_tokens": response.get("prompt_eval_cached_count"),
                    "tokens_per_byte": count / len(serialized) if type(count) is int else None,
                    "load_seconds": response.get("load_duration", 0) / 1e9,
                    "prefill_seconds": response.get("prompt_eval_duration", 0) / 1e9,
                    "decode_seconds": response.get("eval_duration", 0) / 1e9,
                    "output_tokens": response.get("eval_count"),
                    "wall_seconds": exchange.get("seconds"),
                }
            )
    known = [r for r in records if r["tokens_per_byte"] is not None]
    result = {
        "requests": records,
        "request_count": len(records),
        "max_observed_tokens_per_byte": max((r["tokens_per_byte"] for r in known), default=None),
        "runtime_bound_changed": False,
        "limitation": "Saved task distribution only; provider cache semantics and template "
        "framing may differ. Does not certify unseen code, tools or Unicode.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "requests"}))


if __name__ == "__main__":
    main()
