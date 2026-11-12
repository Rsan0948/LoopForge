"""Declared native structures cross the existing strict string action boundary."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter
from test_deepseek_model import TEMPLATE, _context  # pyright: ignore[reportPrivateUsage]
from test_openrouter_model import CAPABILITIES

from loopforge.adapters.git_workspace import GitWorkspaceManager
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.openrouter_model import OpenRouterModel
from loopforge.domain.workspace import FixtureFile, FixtureSpec
from loopforge.ports.model import JsonTextParameter, ModelToolSpec, ModelTurnError


class Edit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    old: str
    new: str


def encode(value: object) -> str:
    edits = TypeAdapter(list[Edit]).validate_python(value, strict=True)
    return TypeAdapter(list[Edit]).dump_json(edits).decode()


@pytest.mark.parametrize("provider", ["openrouter", "ollama"])
@pytest.mark.parametrize("valid", [True, False])
def test_structured_native_arguments_validate_before_string_action(
    provider: str, valid: bool
) -> None:
    edits = [{"old": 'x\n"雪"', "new": "y"}] if valid else [{"old": 7, "new": "y"}]
    tool = ModelToolSpec(
        name="apply_edits",
        description="Bounded edit",
        parameters={
            "type": "object",
            "properties": {"edits": {"type": "string"}},
            "required": ["edits"],
            "additionalProperties": False,
        },
        json_text_parameters={
            "edits": JsonTextParameter(
                schema={"type": "array", "items": Edit.model_json_schema()}, encode=encode
            )
        },
    )

    def respond(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert sent["tools"][0]["function"]["parameters"]["properties"]["edits"]["type"] == "array"
        for message in sent["messages"]:
            for call in message.get("tool_calls", []):
                saved = call["function"]["arguments"]
                args = json.loads(saved) if isinstance(saved, str) else saved
                assert args["edits"] == edits
        function = {"name": "apply_edits", "arguments": {"edits": edits}}
        if provider == "openrouter":
            function["arguments"] = json.dumps(function["arguments"])
            body = {
                "model": CAPABILITIES.model,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{"function": function}],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.00002},
            }
        else:
            body = {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": function}],
                },
                "done": True,
                "prompt_eval_count": 100,
                "eval_count": 20,
            }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(respond)
    model = (
        OpenRouterModel(
            api_key="test",
            capabilities=CAPABILITIES,
            model=CAPABILITIES.model,
            tools=(tool,),
            template=TEMPLATE,
            transport=transport,
        )
        if provider == "openrouter"
        else OllamaModel(model="test", tools=(tool,), template=TEMPLATE, transport=transport)
    )
    try:
        if valid:
            turn = model.propose_action(_context())
            assert json.loads(turn.action.arguments["edits"]) == edits
            model.propose_action(_context())
        else:
            with pytest.raises(ModelTurnError) as error:
                model.propose_action(_context())
            assert error.value.usage is not None
    finally:
        model.close()


def test_committed_patch_inventory_keeps_working_tree_clean(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path).materialize(
        FixtureSpec(fixture_id="patch", files=(FixtureFile(path="a.ts", content="old"),))
    )
    (workspace.root / "a.ts").write_text("new")
    workspace.run_git("add", "a.ts")
    workspace.run_git("commit", "-m", "checkpoint")
    assert workspace.status().clean
    assert workspace.patch_status().files == ("a.ts",)
    (workspace.root / "new.rs").write_text("new")
    assert set(workspace.patch_status().files) == {"a.ts", "new.rs"}
    baseline = replace(workspace, _base_revision=workspace.run_git("rev-parse", "HEAD").strip())
    assert baseline.patch_status().files == ("new.rs",)


def test_undeclared_native_structure_is_still_rejected() -> None:
    from pydantic import ValidationError  # noqa: PLC0415

    from loopforge.adapters.native_arguments import normalize_chat  # noqa: PLC0415
    from loopforge.adapters.ollama_model import ChatResponse  # noqa: PLC0415

    tool = ModelToolSpec(name="other", description="No structure encoding", parameters={})
    data: dict[str, object] = {
        "message": {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "other", "arguments": {"unexpected": []}}}],
        },
        "done": True,
        "prompt_eval_count": 1,
        "eval_count": 1,
    }
    with pytest.raises(ValidationError):
        ChatResponse.model_validate(normalize_chat(data, (tool,)))


@pytest.mark.parametrize("allowed", [True, False])
def test_committed_patch_uses_scope_constraints(tmp_path: Path, allowed: bool) -> None:
    from test_repair_verifier import (  # noqa: PLC0415
        StubSandbox,
        _criteria,  # pyright: ignore[reportPrivateUsage]
        _result,  # pyright: ignore[reportPrivateUsage]
        _state,  # pyright: ignore[reportPrivateUsage]
    )

    from loopforge.workloads.repair import RepairVerifier  # noqa: PLC0415

    workspace = GitWorkspaceManager(tmp_path).materialize(
        FixtureSpec(fixture_id="scope", files=(FixtureFile(path="a.ts", content="old"),))
    )
    (workspace.root / "a.ts").write_text("new")
    workspace.run_git("add", ".")
    workspace.run_git("commit", "-m", "checkpoint")
    verifier = RepairVerifier(
        StubSandbox({"run_tests": _result(0)}),
        workspace,
        _criteria(allowed_prefixes=("a.ts" if allowed else "other.ts",)),
    )
    assert verifier.verify(_state()).passed is allowed


def test_reverting_committed_change_to_original_bytes_is_not_a_patch(tmp_path: Path) -> None:
    workspace = GitWorkspaceManager(tmp_path).materialize(
        FixtureSpec(fixture_id="revert", files=(FixtureFile(path="a.ts", content="old"),))
    )
    (workspace.root / "a.ts").write_text("new")
    workspace.run_git("add", ".")
    workspace.run_git("commit", "-m", "checkpoint")
    (workspace.root / "a.ts").write_text("old")
    assert not workspace.status().clean
    assert not workspace.patch_status().files
