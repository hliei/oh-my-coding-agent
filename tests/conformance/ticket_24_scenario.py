from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parent))

import test_ticket_24_interactive_repl as repl


_SCENARIOS = (
    repl.test_interactive_trusts_starts_runs_and_returns_to_idle,
    repl.test_nonempty_input_while_run_is_busy_is_discarded,
    repl.test_failed_busy_diagnostic_aborts_before_later_progression,
    repl.test_failed_pre_run_diagnostic_is_fatal_instead_of_returning_to_idle,
    repl.test_assistant_suffix_is_flushed_before_run_settlement,
    repl.test_distinct_assistant_text_blocks_append_without_replaying_prefix,
    repl.test_confirmed_cancellation_renders_once_and_session_is_reusable,
    repl.test_negative_tool_result_is_rendered_as_an_outcome,
    repl.test_tool_failure_and_model_error_are_distinct_transcript_records,
    repl.test_parallel_tool_records_keep_public_event_arrival_order,
    repl.test_empty_and_launcher_shaped_lines_are_not_intercepted,
    repl.test_sigint_at_trust_prompt_exits_before_session_construction,
    repl.test_renderer_write_failure_is_fatal_without_a_run_record,
)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        for index, scenario in enumerate(_SCENARIOS):
            case_root = root / f"case-{index:02d}"
            case_root.mkdir()
            scenario(case_root)
    actual = {
        "reference.plain-repl-transcript": {
            "A": "one normalized idle line per Run",
            "L": [
                "session new",
                "idle",
                "assistant start",
                "assistant suffix",
                "assistant end",
                "tool start",
                "tool outcome|failure",
                "run completed|model_error",
                "idle",
            ],
            "T": {
                "appendOnly": True,
                "safeEncoding": True,
                "negativeResult": "outcome",
                "runtimeError": "failure",
                "excluded": [
                    "tool update",
                    "turn",
                    "raw agent",
                    "private cause",
                ],
            },
            "E": {
                "awaitedListener": True,
                "rendererFailure": "fatal_no_replay_or_terminal_fabrication",
            },
            "C": "ordinary and recoverable Runs reuse one Session",
        },
        "reference.repl-reject-busy-ordinary-message": {
            "A": {
                "first": "admitted",
                "second": "discarded",
                "freshAfterSettlement": "admitted",
            },
            "L": "one Run before reuse",
            "T": "busy: Product Session has an active Run",
            "E": {
                "steering": False,
                "queue": False,
                "secondEvent": False,
                "secondMessage": False,
                "secondEffect": False,
            },
            "C": "original Run completes and returns to idle",
        },
    }
    print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
