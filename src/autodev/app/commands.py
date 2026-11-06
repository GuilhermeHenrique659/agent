"""Every autodev command as an async function that talks to the user only through `UserIO`."""

import functools
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from autodev.adapters.claude_settings import hooks_warning, project_hook_events
from autodev.adapters.git import GitError, GitRepo
from autodev.adapters.knowledge_folder import make_context
from autodev.app.runtime import make_runtime as default_make_runtime
from autodev.config import ConfigError, load_config
from autodev.config.models import ProjectConfig
from autodev.domain.story import InvalidTransition, Story, StoryStatus
from autodev.gates.runner import run_gates
from autodev.improve.code_scan import run_code_scan
from autodev.improve.lint_rules import run_lint_proposal
from autodev.improve.process_retro import NoRunsYet, run_process_retro
from autodev.improve.state import new_done_count, should_suggest
from autodev.onboarding.doctor import DOCTOR_HINTS, check_connection, describe
from autodev.onboarding.onboard import OnboardError
from autodev.onboarding.onboard import onboard as run_onboard
from autodev.pipeline.adr import adr_dir, has_decisions
from autodev.pipeline.approval import approve_story
from autodev.pipeline.briefing import is_empty_repo, run_brief
from autodev.pipeline.refinement import refine_story
from autodev.pipeline.request_flow import discover_and_refine, handle_request
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_docs import render_story_summary
from autodev.pipeline.story_runner import RunError, StoryRunner
from autodev.ports.agent_runtime import AgentRuntime
from autodev.ports.user_io import UserIO

RuntimeFactory = Callable[[ProjectConfig, Path], AgentRuntime]


class CommandError(Exception):
    """A command failed in an expected way; the message is meant for the user."""


_EXPECTED_ERRORS = (ConfigError, RunError, OnboardError, GitError, InvalidTransition)


def reports_errors[**P, R](
    fn: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Turn the errors a user can cause into `CommandError`."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except _EXPECTED_ERRORS as exc:
            raise CommandError(str(exc)) from exc

    return wrapper


def _get_story(store: StoryStore, story_id: str) -> Story:
    try:
        return store.get(story_id)
    except KeyError as exc:
        raise CommandError(f"Story {story_id} not found.") from exc


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _warn_about_hooks(path: Path, io: UserIO) -> None:
    if events := project_hook_events(path):
        io.show(hooks_warning(events))


@reports_errors
async def onboard(
    path: Path,
    io: UserIO,
    *,
    yes: bool = False,
    brief: bool = True,
    home: Path,
    make_runtime: RuntimeFactory = default_make_runtime,
) -> None:
    report = await run_onboard(
        path, io=io, make_runtime=make_runtime, yes=yes, run_brief_step=brief, home=home
    )
    for action in report.actions:
        io.show(f"- {action}")
    io.show("Next steps:")
    for step in report.next_steps:
        io.show(f"- {step}")


@reports_errors
async def doctor(
    path: Path, io: UserIO, *, make_runtime: RuntimeFactory = default_make_runtime
) -> None:
    config = load_config(path)
    result = await check_connection(config, path, make_runtime(config, path))
    if not result.ok:
        raise CommandError(f"{result.detail}\n{DOCTOR_HINTS}")
    io.show(describe(result))


@reports_errors
async def status(path: Path, io: UserIO) -> None:
    load_config(path)
    stories = StoryStore(path).values()
    if not stories:
        io.show("No stories yet.")
        return
    rows = [[s.id, s.title, s.status.value, str(s.attempts)] for s in stories]
    io.show(_table(["id", "title", "status", "attempts"], rows))


@reports_errors
async def show(path: Path, io: UserIO, story_id: str) -> None:
    load_config(path)
    story = _get_story(StoryStore(path), story_id)
    io.show(render_story_summary(story))
    io.show(f"{story.id}: {story.status.value}")


@reports_errors
async def check(path: Path, io: UserIO) -> None:
    config = load_config(path)
    report = run_gates(config, path)
    rows = [
        [r.name, "skipped" if r.skipped else "passed" if r.ok else "failed"] for r in report.results
    ]
    io.show(_table(["gate", "result"], rows))
    if not report.ok:
        for r in report.failures:
            io.show(f"{r.name} failed:\n\n```\n{r.output}\n```")
        raise CommandError("Quality gates failed.")


async def _run_brief(
    config: ProjectConfig, path: Path, runtime: AgentRuntime, io: UserIO
) -> ProjectConfig:
    """Run the technical briefing, report what it wrote and return the reloaded config."""
    repo = GitRepo(path)
    was_empty = is_empty_repo(repo)
    before = {p.name for p in adr_dir(path).glob("*.md")}
    brief_result = await run_brief(
        config=config,
        project_root=path,
        runtime=runtime,
        repo=repo,
        context=make_context(config, path),
        io=io,
        log=RunLog(path, "brief"),
    )
    for adr in sorted(p for p in adr_dir(path).glob("*.md") if p.name not in before):
        io.show(f"ADR: {adr.relative_to(path)}")
    for name in ("build", "test", "lint"):
        io.show(f"{name}: {getattr(brief_result.commands, name)}")
    io.show(f"Bootstrap: {'ran' if was_empty else 'skipped'}")
    return load_config(path)


async def _offer_brief(
    config: ProjectConfig, path: Path, runtime: AgentRuntime, io: UserIO
) -> ProjectConfig:
    if has_decisions(path):
        return config
    answer = io.ask("No technical decisions recorded yet. Run the technical briefing first? [Y/n]")
    if answer.strip().lower() in ("", "y", "yes"):
        return await _run_brief(config, path, runtime, io)
    return config


async def _run_story(
    config: ProjectConfig, path: Path, runtime: AgentRuntime, story_id: str
) -> Story:
    runner = StoryRunner(
        config,
        path,
        runtime,
        GitRepo(path),
        StoryStore(path),
        make_context(config, path),
        lambda sid: RunLog(path, sid),
    )
    return await runner.run(story_id)


def _show_final_status(story: Story, config: ProjectConfig, path: Path, io: UserIO) -> None:
    io.show(f"{story.id}: {story.status.value}")
    if story.blocked_reason:
        io.show(f"Blocked: {story.blocked_reason}")
    if story.status == StoryStatus.DONE:
        store = StoryStore(path)
        if should_suggest(path, store, config.improve.every_n_stories):
            io.show(
                f"{new_done_count(path, store)} stories delivered since the last improvement. "
                "Run `autodev improve` to review the process and the code."
            )


@reports_errors
async def brief(
    path: Path, io: UserIO, *, make_runtime: RuntimeFactory = default_make_runtime
) -> None:
    config = load_config(path)
    _warn_about_hooks(path, io)
    await _run_brief(config, path, make_runtime(config, path), io)


@reports_errors
async def run(
    path: Path,
    io: UserIO,
    story_id: str,
    *,
    make_runtime: RuntimeFactory = default_make_runtime,
) -> None:
    config = load_config(path)
    story = await _run_story(config, path, make_runtime(config, path), story_id)
    _show_final_status(story, config, path, io)


@reports_errors
async def resume(path: Path, io: UserIO, story_id: str) -> None:
    load_config(path)
    store = StoryStore(path)
    story = _get_story(store, story_id)
    story.resume()
    store.save(story)
    io.show(f"{story.id}: {story.status.value}")


async def _review_loop(
    story: Story,
    config: ProjectConfig,
    path: Path,
    runtime: AgentRuntime,
    io: UserIO,
    store: StoryStore,
) -> None:
    io.show(render_story_summary(story))
    while True:
        choice = io.ask("[a]pprove, [f]eedback, [l]ater").strip().lower()
        if choice == "a":
            approve_story(
                story.id, config=config, project_root=path, repo=GitRepo(path), store=store
            )
            io.show(f"{story.id}: approved")
            if io.ask("Implement now? [y/N]").strip().lower() == "y":
                _show_final_status(
                    await _run_story(config, path, runtime, story.id), config, path, io
                )
            return
        if choice == "f":
            feedback = io.ask("Feedback")
            await refine_story(
                story,
                config=config,
                project_root=path,
                runtime=runtime,
                context=make_context(config, path),
                log=RunLog(path, story.id),
                feedback=feedback,
            )
            store.save(story)
            io.show(render_story_summary(story))
        elif choice == "l":
            io.show(
                f"Continue later with `autodev approve {story.id}` "
                f'or `autodev refine {story.id} -m "..."`.'
            )
            return


@reports_errors
async def request(
    path: Path,
    io: UserIO,
    text: str | None = None,
    *,
    make_runtime: RuntimeFactory = default_make_runtime,
) -> None:
    config = load_config(path)
    runtime = make_runtime(config, path)
    if not text or not text.strip():
        text = io.ask("Describe your request")
    config = await _offer_brief(config, path, runtime, io)
    store = StoryStore(path)
    story = await handle_request(
        text,
        config=config,
        project_root=path,
        runtime=runtime,
        store=store,
        context=make_context(config, path),
        io=io,
        log_factory=lambda sid: RunLog(path, sid),
    )
    await _review_loop(story, config, path, runtime, io, store)


@reports_errors
async def discover(
    path: Path,
    io: UserIO,
    story_id: str,
    *,
    make_runtime: RuntimeFactory = default_make_runtime,
) -> None:
    config = load_config(path)
    store = StoryStore(path)
    story = _get_story(store, story_id)
    if story.status != StoryStatus.DRAFT:
        raise RunError(
            f"Story {story.id} is {story.status.value}; only draft stories can be discovered"
        )
    runtime = make_runtime(config, path)
    config = await _offer_brief(config, path, runtime, io)
    await discover_and_refine(
        story,
        config=config,
        project_root=path,
        runtime=runtime,
        store=store,
        context=make_context(config, path),
        io=io,
        log=RunLog(path, story.id),
    )
    await _review_loop(story, config, path, runtime, io, store)


@reports_errors
async def approve(path: Path, io: UserIO, story_id: str) -> None:
    config = load_config(path)
    story = approve_story(
        story_id, config=config, project_root=path, repo=GitRepo(path), store=StoryStore(path)
    )
    io.show(f"{story.id}: {story.status.value}")


@reports_errors
async def refine(
    path: Path,
    io: UserIO,
    story_id: str,
    feedback: str,
    *,
    make_runtime: RuntimeFactory = default_make_runtime,
) -> None:
    config = load_config(path)
    store = StoryStore(path)
    story = _get_story(store, story_id)
    if story.status not in (StoryStatus.DISCOVERED, StoryStatus.REFINED):
        raise RunError(
            f"Story {story.id} is {story.status.value}; "
            "only discovered or refined stories can be refined"
        )
    await refine_story(
        story,
        config=config,
        project_root=path,
        runtime=make_runtime(config, path),
        context=make_context(config, path),
        log=RunLog(path, story.id),
        feedback=feedback,
    )
    store.save(story)
    io.show(render_story_summary(story))
    io.show(f"{story.id}: {story.status.value}")


@reports_errors
async def improve(
    path: Path,
    io: UserIO,
    *,
    process: bool = False,
    code: bool = False,
    lint: bool = False,
    make_runtime: RuntimeFactory = default_make_runtime,
) -> None:
    both = not process and not code and not lint
    config = load_config(path)
    runtime = make_runtime(config, path)
    store = StoryStore(path)
    repo = GitRepo(path)
    context = make_context(config, path)
    log = RunLog(path, "improve")
    if process or both:
        try:
            accepted = await run_process_retro(
                config=config,
                project_root=path,
                runtime=runtime,
                repo=repo,
                context=context,
                store=store,
                io=io,
                log=log,
            )
        except NoRunsYet as exc:
            if not both:
                raise
            io.show(str(exc))
        else:
            for p in accepted:
                if p.kind == "prompt":
                    io.show(f"Wrote .agent/process/{p.target}.md")
                elif p.kind == "standard":
                    io.show(f"Updated .agent/standards/{p.target}")
                else:
                    io.show(f"Config recommendation: {p.target}: {p.content}")
            if any(p.kind == "standard" for p in accepted):
                io.show(
                    "Standards changed. Run `autodev improve --lint` to turn them into lint rules."
                )
    if code or both:
        stories = await run_code_scan(
            config=config,
            project_root=path,
            runtime=runtime,
            repo=repo,
            context=context,
            store=store,
            io=io,
            log=log,
        )
        for story in stories:
            io.show(f"Created {story.id}: {story.title}")
    if lint:
        lint_story = await run_lint_proposal(
            config=config,
            project_root=path,
            runtime=runtime,
            context=context,
            store=store,
            io=io,
            log=log,
        )
        if lint_story is None:
            io.show("No lint story created.")
        else:
            io.show(f"Created {lint_story.id}: {lint_story.title}")
            io.show(f"Next: autodev discover {lint_story.id}")
