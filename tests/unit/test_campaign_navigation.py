"""Worker navigation tools: bounded, ranked, never a capability or proof of behavior."""

import json

import pytest

from loopforge.entrypoints.campaign_navigation import (
    MAX_SYMBOL_OFFSET,
    find_symbol,
    repository_map,
    symbols,
)

FILES = {
    "app/reconcile.py": (
        "def reconcile(rows):\n    return total(rows)\n\nclass Ledger:\n    pass\n"
    ),
    "app/report.py": (
        "from app.reconcile import reconcile\n\ndef render():\n    return reconcile([])\n"
    ),
    "app/ui.ts": "export function draw() {\n  return 1;\n}\n",
    "app/notes.txt": "reconcile mentioned in prose",
    ".env": "SECRET=1",
    "keys/server.pem": "-----BEGIN-----",
}


def test_repository_map_ranks_query_matches_and_their_importers_first() -> None:
    mapped = json.loads(repository_map(FILES, "reconcile ledger"))
    paths = [entry["path"] for entry in mapped["files"]]
    assert paths[:2] == ["app/reconcile.py", "app/report.py"]
    first = mapped["files"][0]
    assert {"name": "Ledger", "start_line": 4, "end_line": 5} in first["symbols"]
    assert mapped["files"][1]["imports"] == ["app/reconcile.py"]
    assert mapped["trust"] == "untrusted_content"


def test_repository_map_excludes_secret_and_unsupported_files() -> None:
    paths = {entry["path"] for entry in json.loads(repository_map(FILES, "x"))["files"]}
    assert paths == {"app/reconcile.py", "app/report.py", "app/ui.ts"}


def test_repository_map_reports_omissions_inside_its_character_budget() -> None:
    many = {f"app/m{i}.py": f"def f{i}():\n    pass\n" for i in range(200)}
    mapped = repository_map(many, "f1", max_chars=2000)
    assert len(mapped) <= 2000
    assert json.loads(mapped)["omitted_files"] > 0


def test_symbols_cover_python_ast_javascript_declarations_and_invalid_python() -> None:
    assert symbols("app/ui.ts", FILES["app/ui.ts"]) == (("draw", 1, 3),)
    assert symbols("broken.py", "def (:") == ()
    assert symbols("data.json", '{"a": 1}') == ()


def test_find_symbol_lists_definitions_before_textual_references() -> None:
    found = json.loads(find_symbol(FILES, "reconcile"))
    kinds = [(m["path"], m["kind"]) for m in found["matches"]]
    assert kinds[0] == ("app/reconcile.py", "definition")
    assert ("app/report.py", "textual_reference") in kinds
    assert found["total_matches"] == len(found["matches"])
    assert not found["capped"]


def test_find_symbol_pages_and_marks_the_pagination_limit() -> None:
    many = {"a.py": "\n".join(f"target  # {i}" for i in range(1100))}
    first = json.loads(find_symbol(many, "target"))
    assert first["capped"]
    assert first["next_offset"] == 30
    last = json.loads(find_symbol(many, "target", offset=MAX_SYMBOL_OFFSET))
    assert last["pagination_limit_reached"]
    assert last["next_offset"] is None


@pytest.mark.parametrize(
    ("query", "offset"), [("two words", 0), ("x" * 121, 0), ("target", -1), ("target", 1001)]
)
def test_find_symbol_rejects_unbounded_queries_and_offsets(query: str, offset: int) -> None:
    with pytest.raises(ValueError, match="symbol"):
        find_symbol(FILES, query, offset=offset)


def test_find_symbol_never_searches_secret_files() -> None:
    assert json.loads(find_symbol(FILES, "SECRET"))["total_matches"] == 0
