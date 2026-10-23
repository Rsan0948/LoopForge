"""Upstream mlx_lm.server with a reusable cache checkpoint for tool-result turns.

Upstream segments a chat prompt into (context | thinking tail) only when the last
message is from the user; it then stores a prompt-cache checkpoint just before the
tail. In an agent loop every later request ends with a tool result, so no checkpoint
is stored. For hybrid models such as Qwen3.6-A3B (recurrent layers; the cache cannot
be trimmed) the only remaining entry is "prompt + generated tokens", which the next
request never matches exactly, because history renders the previous assistant turn
without the generation-time think block. Every turn then re-processes everything after
the last user-final checkpoint.

This wrapper applies upstream's own tail split to tool-final chat requests too. Prompt
tokens, sampling, parsing and weights are unchanged; only where the checkpoint is
stored differs. Run with the lab interpreter and the same arguments as mlx_lm.server.
"""

import sys

from mlx_lm import server

_upstream_tokenize = server.ResponseGenerator._tokenize
_TAIL_WINDOW = 11  # upstream's thinking-tail search window


def _tokenize(self, tokenizer, request, args):
    prompt, segments, segment_types, initial_state, vision = _upstream_tokenize(
        self, tokenizer, request, args
    )
    messages = getattr(request, "messages", None) or []
    if (
        request.request_type == "chat"
        and vision is None
        and len(segments) == 1
        and messages
        and messages[-1].get("role") == "tool"
        and tokenizer.has_thinking
    ):
        tail = tokenizer.rfind_think_start(prompt, start=len(prompt) - _TAIL_WINDOW)
        if 0 < tail < len(prompt):
            return (
                prompt,
                [prompt[:tail], prompt[tail:]],
                ["user", "assistant"],
                initial_state,
                vision,
            )
    return prompt, segments, segment_types, initial_state, vision


server.ResponseGenerator._tokenize = _tokenize

if __name__ == "__main__":
    sys.argv[0] = "mlx_lm.server"
    server.main()
