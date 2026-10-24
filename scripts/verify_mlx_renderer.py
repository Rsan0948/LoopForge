"""Offline renderer/parser parity check. Run using the isolated MLX Python."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from mlx_lm.tokenizer_utils import load

ROOT = Path(__file__).resolve().parents[1]
SHA = "b3503ee35a142224b348c9cfb6556c62355fcdb235f5799ac88b89548a51a7e6"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request",
        type=Path,
        default=ROOT
        / ".loopforge/tasks/reconciliation-forced-attempt/review-coverage-2026-10-23/worker-prompts"
        / (SHA + ".json"),
    )
    args = parser.parse_args()
    raw = args.request.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA
    native = json.loads(raw)
    lab = ROOT / ".loopforge/mlx-lab"
    model = json.loads((lab / "model.json").read_text())
    template = (ROOT / "scripts/ollama_renderer/chat_template.jinja").read_text()
    tokenizer = load(
        Path(model["local_path"]),
        tokenizer_config_extra={
            "chat_template": template,
            "trust_remote_code": False,
            "local_files_only": True,
        },
    )
    assert tokenizer.has_tool_calling
    rendered = subprocess.run(
        [str(lab / "renderer-oracle/renderer-oracle")],
        input=raw.decode(),
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    prompt = json.loads(rendered.stdout)["prompt"]
    actual = tokenizer.apply_chat_template(
        native["messages"],
        tools=native["tools"],
        tokenize=False,
        add_generation_prompt=True,
        ollama_rendered_prompt=prompt,
        enable_thinking=native["think"],
    )
    assert actual.encode() == prompt.encode()
    tokens = len(tokenizer.encode(actual, add_special_tokens=False))
    assert tokens == 22473
    valid = (
        "<function=read_file>\n<parameter=path>\napp/tests/test_api.py\n</parameter>\n</function>"
    )
    assert tokenizer.tool_parser(valid, native["tools"]) == {
        "name": "read_file",
        "arguments": {"path": "app/tests/test_api.py"},
    }
    try:
        tokenizer.tool_parser("not a function", native["tools"])
    except ValueError:
        pass
    else:
        msg = "Malformed tool text accepted"
        raise AssertionError(msg)
    result = {
        "passed": True,
        "tokens": tokens,
        "byte_exact": True,
        "parser": tokenizer.tool_parser.__module__,
        "request_sha256": SHA,
        "rendered_prompt_sha256": hashlib.sha256(actual.encode()).hexdigest(),
        "renderer_binary_sha256": hashlib.sha256(
            (lab / "renderer-oracle/renderer-oracle").read_bytes()
        ).hexdigest(),
        "inference_calls": 0,
    }
    (lab / "artifacts/verified-renderer-parity.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
