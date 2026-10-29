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

Reasoning cap (``--thinking-budget N``, default 10240, 0 disables): a thinking request
whose reasoning reaches N generated tokens without closing is steered to close it: the
next tokens are forced to Qwen's documented early-exit phrase and ``</think>``, and the
model then writes its answer and tool call within the request's output allowance. Lab
data: p99 reasoning output was 9.3K tokens; one matrix call ran 16,384 tokens without a
tool call (about 5.5 minutes lost). Ollama has no equivalent control.
"""

import json
import sys
from pathlib import Path

import mlx.core as mx
from mlx_lm import server
from mlx_lm.models import cache

_upstream_tokenize = server.ResponseGenerator._tokenize
_TAIL_WINDOW = 11  # upstream's thinking-tail search window


_EXIT_PHRASE = (
    "\n\nConsidering the limited time, I have to give the solution based on the "
    "thinking directly now.\n</think>\n\n"
)


class _Cap:
    budget = 10240
    exit_ids: tuple[int, ...] = ()
    end_id = -1
    # Set by _tokenize for the request whose logits processors are built next; the
    # server runs one prompt and one decode at a time (--prompt/--decode-concurrency 1).
    thinking = False


def _reasoning_cap():
    thinking = _Cap.thinking
    generated = 0
    forced: list[int] = []
    closed = False

    def cap(tokens, logits):
        nonlocal generated, closed
        if closed or not thinking:
            return logits
        if not forced and generated and tokens.size and int(tokens[-1]) == _Cap.end_id:
            closed = True
            return logits
        generated += 1
        if generated > _Cap.budget and not forced:
            forced.extend(_Cap.exit_ids)
        if not forced:
            return logits
        token = forced.pop(0)
        closed = not forced
        # Same shape as the input logits: the batched generator concatenates rows.
        hit = mx.broadcast_to(mx.arange(logits.shape[-1]) == token, logits.shape)
        return mx.where(hit, mx.zeros_like(logits), mx.full(logits.shape, -mx.inf, logits.dtype))

    return cap


_upstream_processors = server._make_logits_processors


def _make_logits_processors(args):
    processors = _upstream_processors(args)
    if _Cap.budget > 0 and _Cap.exit_ids:
        processors = [*processors, _reasoning_cap()]
    return processors


def _tokenize(self, tokenizer, request, args):
    prompt, segments, segment_types, initial_state, vision = _upstream_tokenize(
        self, tokenizer, request, args
    )
    if not prompt:
        # Raised here, the error reaches only this request. Upstream would insert the
        # empty prompt and its generation thread would die, failing every later request
        # until restart (live 2026-10-29: a chat request without the rendered prompt).
        msg = "request renders to an empty prompt"
        raise ValueError(msg)
    _Cap.thinking = initial_state == "reasoning"
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


_upstream_fetch = cache.LRUPromptCache.fetch_nearest_cache


def _fetch_nearest_cache(self, model, tokens):
    """Never leave a prompt with nothing to process.

    Upstream returns an exact match with no remaining tokens; the generator then
    rejects the empty prompt and its thread dies (an identical retry would do it).
    Reuse all but the last token instead, or run the request uncached.
    """
    prompt_cache, rest = _upstream_fetch(self, model, tokens)
    if rest or not tokens or prompt_cache is None:
        return prompt_cache, rest
    if cache.can_trim_prompt_cache(prompt_cache):
        cache.trim_prompt_cache(prompt_cache, 1)
        return prompt_cache, tokens[-1:]
    return None, tokens


cache.LRUPromptCache.fetch_nearest_cache = _fetch_nearest_cache
server.ResponseGenerator._tokenize = _tokenize
server._make_logits_processors = _make_logits_processors


def _configure_cap(argv: list[str]) -> list[str]:
    """Consume --thinking-budget and load the exit-phrase token ids from the model."""
    if "--thinking-budget" in argv:
        index = argv.index("--thinking-budget")
        _Cap.budget = int(argv[index + 1])
        argv = argv[:index] + argv[index + 2 :]
    from tokenizers import Tokenizer  # noqa: PLC0415 - lab interpreter only

    model = Path(argv[argv.index("--model") + 1])
    tokenizer = Tokenizer.from_file(str(model / "tokenizer.json"))
    _Cap.exit_ids = tuple(tokenizer.encode(_EXIT_PHRASE, add_special_tokens=False).ids)
    end = tokenizer.token_to_id("</think>")
    if end is None or end not in _Cap.exit_ids:
        msg = "model tokenizer has no </think> token"
        raise SystemExit(msg)
    _Cap.end_id = end
    print(
        json.dumps({"thinking_budget": _Cap.budget, "exit_tokens": len(_Cap.exit_ids)}),
        flush=True,
    )
    return argv


if __name__ == "__main__":
    sys.argv = _configure_cap(sys.argv)
    sys.argv[0] = "mlx_lm.server"
    server.main()
