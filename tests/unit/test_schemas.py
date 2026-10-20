from __future__ import annotations

import json

from coding_agent.agent.schemas import Plan, ReviewVerdict, submit_tool_schema


def test_submit_schema_is_self_contained() -> None:
    schema = submit_tool_schema("submit_review", "Submit it.", ReviewVerdict)
    text = json.dumps(schema)
    assert "$ref" not in text
    assert "$defs" not in text
    params = schema["function"]["parameters"]
    finding = params["properties"]["findings"]["items"]
    assert set(finding["properties"]["severity"]["enum"]) == {"high", "medium", "low"}
    assert params["properties"]["verdict"]["enum"] == ["approve", "request_changes"]


def test_review_blocking_findings_are_high_and_medium() -> None:
    review = ReviewVerdict.model_validate(
        {
            "verdict": "request_changes",
            "summary": "s",
            "findings": [
                {"severity": "low", "file": "a.py", "issue": "nit"},
                {"severity": "medium", "file": "a.py", "line": 3, "issue": "bug"},
            ],
        }
    )
    assert [f.issue for f in review.blocking()] == ["bug"]
    assert "[medium] `a.py:3`: bug" in review.as_markdown()


def test_plan_markdown_lists_files_steps_and_risks() -> None:
    plan = Plan(
        summary="Add divide.",
        files_to_change=[{"path": "calculator.py", "change": "add divide"}],  # type: ignore[list-item]
        steps=["write it", "test it"],
        test_strategy="pytest",
    )
    text = plan.as_markdown()
    assert "`calculator.py`: add divide" in text
    assert "2. test it" in text
    assert "(none listed)" in text  # no risks given
