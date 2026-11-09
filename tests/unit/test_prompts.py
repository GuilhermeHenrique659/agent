from pathlib import Path

import pytest

from autodev.config import ConfigError
from autodev.config.models import AgentOverride, AgentsConfig
from autodev.domain.role import Role
from autodev.domain.story import Story, Task
from autodev.pipeline.outputs import CriterionCheck, Finding, QaIssue, QaOutput
from autodev.pipeline.prompts import (
    MAX_FEEDBACK_CHARS,
    PRECEDENCE,
    build_bootstrap_prompt,
    build_brief_prompt,
    build_code_scan_prompt,
    build_implementer_prompt,
    build_merge_fix_prompt,
    build_merge_prompt,
    build_process_retro_prompt,
    build_qa_fix_prompt,
    build_qa_prompt,
    build_refiner_prompt,
    build_review_fix_prompt,
    build_reviewer_prompt,
    build_test_fix_prompt,
    build_test_writer_prompt,
    load_instructions,
)


def profile_for(role: Role, prompt: str | None = None):  # type: ignore[no-untyped-def]
    agents = AgentsConfig(roles={role: AgentOverride(prompt=prompt)})
    return agents.profile_for(role)


def make_story() -> Story:
    return Story(
        "S-0001",
        "Add greeting",
        description="Greet the user.",
        acceptance_criteria=["says hello", "says goodbye"],
        tasks=[Task("T1", "Say hello")],
    )


@pytest.mark.parametrize(
    "role", [Role.TEST_WRITER, Role.IMPLEMENTER, Role.REVIEWER, Role.QA, Role.MERGER]
)
def test_loads_builtin_instructions_per_role(role: Role, tmp_path: Path) -> None:
    text = load_instructions(role, profile_for(role), tmp_path)
    assert text.strip()
    assert "git" in text


def test_profile_prompt_overrides_builtin(tmp_path: Path) -> None:
    (tmp_path / "process").mkdir()
    (tmp_path / "process" / "impl.md").write_text("custom instructions")
    profile = profile_for(Role.IMPLEMENTER, "process/impl.md")
    text = load_instructions(Role.IMPLEMENTER, profile, tmp_path)
    assert text.startswith("custom instructions")


def test_missing_override_file_raises_config_error(tmp_path: Path) -> None:
    profile = profile_for(Role.IMPLEMENTER, "process/nope.md")
    with pytest.raises(ConfigError, match=r"nope\.md"):
        load_instructions(Role.IMPLEMENTER, profile, tmp_path)


def test_task_prompt_includes_story_criteria_task_context_and_feedback() -> None:
    story = make_story()
    context = [Path(".agent/standards/s.md")]
    for build in (build_test_writer_prompt, build_implementer_prompt):
        text = build(story, story.tasks[0], "main", context, "tests exploded")
        for expected in (
            "S-0001",
            "Add greeting",
            "Greet the user.",
            "says hello",
            "says goodbye",
            "T1",
            "Say hello",
            "main",
            ".agent/standards/s.md",
            "tests exploded",
        ):
            assert expected in text, expected
    assert "previous attempt" not in build_test_writer_prompt(story, story.tasks[0], "main", [])
    reviewer = build_reviewer_prompt(story, "develop", context)
    assert "git diff develop...HEAD" in reviewer
    assert "says goodbye" in reviewer
    fix = build_review_fix_prompt(
        story,
        "main",
        context,
        "needs work",
        [Finding(severity="blocking", file="a.py", message="bad")],
    )
    assert "a.py" in fix and "bad" in fix and "needs work" in fix


def test_feedback_is_truncated() -> None:
    story = make_story()
    feedback = "HEAD" + "@" * (MAX_FEEDBACK_CHARS + 100) + "TAIL"
    text = build_implementer_prompt(story, story.tasks[0], "main", [], feedback)
    assert "TAIL" in text
    assert "HEAD" not in text
    assert text.count("@") == MAX_FEEDBACK_CHARS - len("TAIL")


def test_qa_prompt_lists_criteria_and_context() -> None:
    text = build_qa_prompt(make_story(), "develop", [Path(".agent/standards/s.md")])
    for expected in ("S-0001", "says hello", "says goodbye", "develop", ".agent/standards/s.md"):
        assert expected in text, expected


def test_qa_fix_prompt_includes_issues_and_unmet_criteria() -> None:
    qa = QaOutput(
        approved=False,
        summary="not there yet",
        criteria=[
            CriterionCheck(criterion="says hello", met=True, evidence="test passes"),
            CriterionCheck(criterion="says goodbye", met=False, evidence="no output"),
        ],
        issues=[QaIssue(severity="blocking", message="crashes on empty input")],
    )
    text = build_qa_fix_prompt(make_story(), "main", [], qa, "gates failed")
    assert "not there yet" in text
    assert "crashes on empty input" in text
    assert "no output" in text
    assert "test passes" not in text
    assert "gates failed" in text
    assert "previous attempt" not in build_qa_fix_prompt(make_story(), "main", [], qa)


def test_merge_prompt_lists_conflicted_files_and_feedback() -> None:
    text = build_merge_prompt(make_story(), ["a.py", "pkg/b.py"], "markers remain in a.py")
    for expected in ("a.py", "pkg/b.py", "Add greeting", "says hello", "markers remain in a.py"):
        assert expected in text, expected
    assert "previous attempt" not in build_merge_prompt(make_story(), ["a.py"], None)


def test_merge_fix_prompt_includes_gate_output() -> None:
    text = build_merge_fix_prompt(make_story(), "main", [Path("k.md")], "lint exploded")
    assert "lint exploded" in text and "says hello" in text and "k.md" in text


def test_process_override_file_used_when_present(tmp_path: Path) -> None:
    (tmp_path / "process").mkdir()
    (tmp_path / "process" / "implementer.md").write_text("convention override")
    text = load_instructions(Role.IMPLEMENTER, profile_for(Role.IMPLEMENTER), tmp_path)
    assert text.startswith("convention override")
    builtin = load_instructions(Role.REVIEWER, profile_for(Role.REVIEWER), tmp_path)
    assert not builtin.startswith("convention override")


def test_profile_prompt_takes_precedence_over_process_file(tmp_path: Path) -> None:
    (tmp_path / "process").mkdir()
    (tmp_path / "process" / "implementer.md").write_text("convention override")
    (tmp_path / "process" / "other.md").write_text("explicit")
    profile = profile_for(Role.IMPLEMENTER, "process/other.md")
    text = load_instructions(Role.IMPLEMENTER, profile, tmp_path)
    assert text.startswith("explicit") and "convention override" not in text


def test_process_retro_prompt_includes_stats_and_instructions() -> None:
    prompt = build_process_retro_prompt(
        "## Totals\nruns: 3",
        {Role.IMPLEMENTER: "IMPL RULES", Role.QA: "QA RULES"},
        [Path(".agent/standards/testing.md")],
    )
    assert "runs: 3" in prompt
    assert "IMPL RULES" in prompt and "QA RULES" in prompt
    assert "implementer" in prompt
    assert ".agent/standards/testing.md" in prompt


def test_code_scan_prompt_includes_churn() -> None:
    prompt = build_code_scan_prompt(
        [("src/app.py", 12), ("src/util.py", 3)], [Path(".agent/standards/stack.md")]
    )
    assert "src/app.py" in prompt and "12" in prompt
    assert "src/util.py" in prompt
    assert ".agent/standards/stack.md" in prompt


def test_precedence_section_appended_to_builtin(tmp_path: Path) -> None:
    for role in (Role.IMPLEMENTER, Role.REVIEWER, Role.ARCHITECT):
        text = load_instructions(role, profile_for(role), tmp_path)
        assert text.endswith(PRECEDENCE)
        assert text.count("## Precedence") == 1
        assert "take precedence over any conflicting instruction" in text


def test_precedence_section_appended_to_override_files(tmp_path: Path) -> None:
    (tmp_path / "process").mkdir()
    (tmp_path / "process" / "implementer.md").write_text("convention override")
    (tmp_path / "process" / "other.md").write_text("explicit")
    text = load_instructions(Role.IMPLEMENTER, profile_for(Role.IMPLEMENTER), tmp_path)
    assert text.startswith("convention override") and text.endswith(PRECEDENCE)
    profile = profile_for(Role.IMPLEMENTER, "process/other.md")
    text = load_instructions(Role.IMPLEMENTER, profile, tmp_path)
    assert text.startswith("explicit") and text.endswith(PRECEDENCE)


def test_brief_prompt_mentions_project_conventions() -> None:
    text = build_brief_prompt([], empty=False)
    for expected in ("CLAUDE.md", "AGENTS.md", ".claude/rules/", "not restate"):
        assert expected in text


def test_brief_prompt_requires_self_contained_commands() -> None:
    text = build_brief_prompt([], empty=True)
    assert "no activated environment" in text
    assert "never assume tools are already installed" in text
    assert "uv run pytest" in text


def test_bootstrap_prompt_requires_running_commands_as_written() -> None:
    text = build_bootstrap_prompt("SPEC-TEXT", [], None)
    assert "SPEC-TEXT" in text
    assert "exactly as written" in text
    assert "lockfile" in text
    assert "network or sandbox" in text


def test_brief_prompt_asks_for_format_command() -> None:
    text = build_brief_prompt([], empty=True)
    assert "format" in text and "auto-format" in text
    assert "self-contained" in text


def test_test_fix_prompt_limits_to_test_files() -> None:
    text = build_test_fix_prompt(
        make_story(), Task("T1", "Say two"), "main", [], "Gate lint failed:\nE501 in tests/a.py"
    )
    assert "Task T1: Say two" in text
    assert "E501 in tests/a.py" in text
    assert "only test files" in text
    assert "do not change what the tests assert" in text.lower()


def test_refiner_prompt_states_task_limit_and_testable_rule(tmp_path: Path) -> None:
    story = Story("S-0001", "t", description="d")
    prompt = build_refiner_prompt(story, [], max_tasks=5)
    assert "at most 5 tasks" in prompt
    assert "failing test" in prompt
    assert "documentation" in prompt
    text = load_instructions(Role.REFINER, profile_for(Role.REFINER), tmp_path)
    assert "failing test" in text and "documentation" in text
