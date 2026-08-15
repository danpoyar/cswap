"""The transcript-based migration-price estimator (CON-582).

A session's context size is the last assistant usage of its transcript:
``input_tokens + cache_creation_input_tokens + cache_read_input_tokens`` is
the prompt footprint its next turn re-creates at full price on a fresh
organization. The estimator must degrade to "unknown" (never raise, never
guess) on anything it can't read.
"""

import json
from pathlib import Path

from claude_swap.context_cost import (
    MoveCost,
    estimate_move_cost,
    last_context_tokens,
)


def _usage_line(
    input_t: int = 2,
    creation: int = 500,
    read: int = 300_000,
    sidechain: bool = False,
) -> str:
    """One assistant record in the live transcript shape (verified against
    ``~/.claude/projects/**/<session>.jsonl`` on 2026-08-15)."""
    return json.dumps({
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {
            "usage": {
                "input_tokens": input_t,
                "cache_creation_input_tokens": creation,
                "cache_read_input_tokens": read,
                "output_tokens": 10,
            }
        },
    })


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestLastContextTokens:
    def test_last_usage_record_wins(self, tmp_path):
        p = _write(tmp_path / "s.jsonl", [
            '{"type":"last-prompt"}',
            _usage_line(read=100),  # an older, smaller turn
            '{"type":"user","message":{"role":"user","content":"hi"}}',
            _usage_line(input_t=3, creation=700, read=330_000),
        ])
        assert last_context_tokens(p) == 3 + 700 + 330_000

    def test_sidechain_records_are_skipped(self, tmp_path):
        # A subagent record trailing the main chain must not understate the
        # session: its own tiny context is not the session's footprint.
        p = _write(tmp_path / "s.jsonl", [
            _usage_line(read=200_000),
            _usage_line(read=1_000, sidechain=True),
        ])
        assert last_context_tokens(p) == 2 + 500 + 200_000

    def test_trailing_garbage_is_skipped(self, tmp_path):
        p = _write(tmp_path / "s.jsonl", [
            _usage_line(read=50_000),
            '{"type":"assistant","message":{"usage":"broken"}}',
            "{not json",
        ])
        assert last_context_tokens(p) == 2 + 500 + 50_000

    def test_none_without_any_usage(self, tmp_path):
        p = _write(tmp_path / "s.jsonl", ['{"type":"last-prompt"}'])
        assert last_context_tokens(p) is None

    def test_none_for_missing_file(self, tmp_path):
        assert last_context_tokens(tmp_path / "absent.jsonl") is None

    def test_reads_only_the_tail(self, tmp_path):
        # A usage record pushed out of the tail window is not seen (and the
        # partial first line the seek lands in must be skipped, not crash).
        filler = json.dumps({"type": "user", "pad": "x" * 200})
        p = _write(
            tmp_path / "s.jsonl", [_usage_line(read=70_000)] + [filler] * 50
        )
        assert last_context_tokens(p, tail_bytes=1024) is None
        assert last_context_tokens(p) == 2 + 500 + 70_000


class TestEstimateMoveCost:
    def test_maps_sessions_to_transcripts_anywhere_in_the_tree(self, tmp_path):
        projects = tmp_path / "projects"
        _write(projects / "-dir-a" / "sid-a.jsonl", [_usage_line(read=100_000)])
        # Subagent-style nesting: the file is found by name at any depth.
        _write(
            projects / "-dir-b" / "nested" / "sid-b.jsonl",
            [_usage_line(read=9_000)],
        )
        cost = estimate_move_cost(
            projects,
            [("fix-a", "sid-a"), ("fix-b", "sid-b"), ("fix-c", "sid-c")],
        )
        assert cost.per_session == {
            "fix-a": 2 + 500 + 100_000,
            "fix-b": 2 + 500 + 9_000,
            "fix-c": None,
        }
        assert cost.total == (2 + 500 + 100_000) + (2 + 500 + 9_000)
        assert cost.unknown == ["fix-c"]

    def test_total_none_when_nothing_known(self, tmp_path):
        cost = estimate_move_cost(tmp_path / "projects", [("a", "sid-a")])
        assert cost.per_session == {"a": None}
        assert cost.total is None
        assert cost.unknown == ["a"]

    def test_blank_session_id_is_unknown(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        cost = estimate_move_cost(projects, [("a", "")])
        assert cost.per_session == {"a": None}

    def test_empty_input_is_empty(self, tmp_path):
        cost = estimate_move_cost(tmp_path / "projects", [])
        assert cost == MoveCost(per_session={})
        assert cost.total is None and cost.unknown == []
