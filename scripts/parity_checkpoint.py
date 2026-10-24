"""Benchmark-only conversation restoration; production runtime authority is unchanged.

The existing Ollama adapter owns all subsequent history, context admission,
thinking policy and parsing. This helper seeds its conversation once, using an
operator-selected captured request with recorded provenance.
"""

from copy import deepcopy
from typing import cast

from loopforge.adapters.ollama_model import OllamaModel
from loopforge.domain.context import ModelContext
from loopforge.domain.prompts import RenderedPrompt
from loopforge.domain.types import RunId
from loopforge.ports.model import ModelTurn


class CheckpointOllamaModel(OllamaModel):
    def restore_checkpoint(self, checkpoint: dict[str, object]) -> None:
        if self._conversations:
            msg = "Cannot replace an active conversation checkpoint"
            raise ValueError(msg)
        messages = cast("list[dict[str, object]]", checkpoint["messages"])
        if (
            checkpoint["model"] != self.capabilities.model
            or len(messages) < 2
            or messages[0]["role"] != "system"
            or messages[-1]["role"] != "tool"
            or messages[-1].get("tool_name") not in {t.name for t in self._tools}
        ):
            msg = "Checkpoint must match this model and end at a registered tool result"
            raise ValueError(msg)
        self._checkpoint_messages = deepcopy(messages)
        self._checkpoint_seeded: set[RunId] = set()
        self._checkpoint_first_context: set[RunId] = set()

    def propose_action(self, context: ModelContext) -> ModelTurn:
        if context.run_id not in self._checkpoint_seeded:
            self._conversations[context.run_id] = deepcopy(self._checkpoint_messages)
            self._seen_items[context.run_id] = {(i.item_id, i.content) for i in context.items}
            # This tool result is already in the snapshot. Retain the adapter's
            # normal on-evidence thinking decision without appending it twice.
            self._pending_calls[context.run_id] = str(self._checkpoint_messages[-1]["tool_name"])
            self._turn_count = sum(m["role"] == "assistant" for m in self._checkpoint_messages)
            self._checkpoint_seeded.add(context.run_id)
            self._checkpoint_first_context.add(context.run_id)
        return super().propose_action(context)

    def _conversation(
        self, context: ModelContext, rendered: RenderedPrompt
    ) -> list[dict[str, object]]:
        if context.run_id in self._checkpoint_first_context:
            self._checkpoint_first_context.remove(context.run_id)
            # Its result is already present in the checkpoint. Clear the
            # temporary thinking-policy cursor before parsing a new response,
            # including a response that fails validation and must be retried.
            self._pending_calls.pop(context.run_id, None)
            return self._conversations[context.run_id]
        return super()._conversation(context, rendered)
