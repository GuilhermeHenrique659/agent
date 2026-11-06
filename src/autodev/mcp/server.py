import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from autodev.app import commands
from autodev.app.commands import RuntimeFactory
from autodev.app.runtime import make_runtime as default_make_runtime
from autodev.mcp.sessions import SessionEvent, SessionManager
from autodev.ports.user_io import UserIO

INSTRUCTIONS = """\
autodev orchestrates AI agents that take a request (feature, fix, improvement) in a project \
through discovery, refinement, human approval, TDD implementation, review, QA and merge.

Every autodev_* command tool starts a session and returns an event: state \
(awaiting_reply, working, finished, failed, cancelled), messages (what autodev reported), \
question (when state is awaiting_reply) and error (when failed). When state is awaiting_reply, \
show the question to the user verbatim and send their exact answer with autodev_reply. Never \
answer, approve or give feedback on the user's behalf. When state is working, call \
autodev_poll. One session at a time per project; autodev_cancel stops one.
"""

_EVENT = (
    "Returns a session event: state, messages, question, error, session_id. If state is "
    "awaiting_reply, show `question` to the user verbatim and pass their exact answer to "
    "autodev_reply (never answer for them). If state is working, call autodev_poll. Show "
    "`messages` to the user."
)
_PATH = "`path` is the project root (default: the server's working directory)."


def _as_dict(event: SessionEvent) -> dict[str, Any]:
    return {**dataclasses.asdict(event), "state": event.state.value}


def build_server(
    manager: SessionManager, make_runtime: RuntimeFactory = default_make_runtime
) -> MCPServer:
    server = MCPServer(name="autodev", instructions=INSTRUCTIONS)

    async def start(
        command: str, path: str | None, flow: Callable[[Path, UserIO], Awaitable[None]]
    ) -> dict[str, Any]:
        root = Path(path) if path else Path.cwd()
        try:
            event = await asyncio.to_thread(manager.start, command, root, lambda io: flow(root, io))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _as_dict(event)

    async def session_call(call: Callable[[], SessionEvent]) -> dict[str, Any]:
        try:
            return _as_dict(await asyncio.to_thread(call))
        except KeyError as exc:
            raise ToolError(str(exc.args[0])) from exc
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="autodev_onboard",
        description=(
            "Integrate autodev into a new or existing project (creates .agent/, git, config, "
            "checks the model connection, optionally runs the technical briefing). Idempotent; "
            "asks the user several questions unless yes=true. Use brief=false to skip the "
            f"briefing. {_PATH} {_EVENT}"
        ),
    )
    async def autodev_onboard(
        path: str | None = None, yes: bool = False, brief: bool = True
    ) -> dict[str, Any]:
        return await start(
            "onboard",
            path,
            lambda root, io: commands.onboard(
                root, io, yes=yes, brief=brief, home=Path.home(), make_runtime=make_runtime
            ),
        )

    @server.tool(
        name="autodev_doctor",
        description=(
            "Check that autodev can reach the model (one cheap call). Use when a session failed "
            f"with an authentication or connection error. {_PATH} {_EVENT}"
        ),
    )
    async def autodev_doctor(path: str | None = None) -> dict[str, Any]:
        return await start(
            "doctor", path, lambda root, io: commands.doctor(root, io, make_runtime=make_runtime)
        )

    @server.tool(
        name="autodev_status",
        description=f"List the backlog stories with their status. {_PATH} {_EVENT}",
    )
    async def autodev_status(path: str | None = None) -> dict[str, Any]:
        return await start("status", path, lambda root, io: commands.status(root, io))

    @server.tool(
        name="autodev_show",
        description=(
            f"Show one story: user story, acceptance criteria, tasks and status. {_PATH} {_EVENT}"
        ),
    )
    async def autodev_show(story_id: str, path: str | None = None) -> dict[str, Any]:
        return await start("show", path, lambda root, io: commands.show(root, io, story_id))

    @server.tool(
        name="autodev_check",
        description=(
            f"Run the project's quality gates (build, test, lint) and report them. {_PATH} {_EVENT}"
        ),
    )
    async def autodev_check(path: str | None = None) -> dict[str, Any]:
        return await start("check", path, lambda root, io: commands.check(root, io))

    @server.tool(
        name="autodev_brief",
        description=(
            "Run the technical briefing: an interview with the user that defines the stack and "
            "architecture, writes ADRs and bootstraps an empty repo. Questions must be relayed "
            f"to the user verbatim. {_PATH} {_EVENT}"
        ),
    )
    async def autodev_brief(path: str | None = None) -> dict[str, Any]:
        return await start(
            "brief", path, lambda root, io: commands.brief(root, io, make_runtime=make_runtime)
        )

    @server.tool(
        name="autodev_bootstrap",
        description=(
            "Retry only the project skeleton creation (empty repository) from the technical "
            f"decisions recorded by the briefing. {_PATH} {_EVENT}"
        ),
    )
    async def autodev_bootstrap(path: str | None = None) -> dict[str, Any]:
        return await start(
            "bootstrap",
            path,
            lambda root, io: commands.bootstrap(root, io, make_runtime=make_runtime),
        )

    @server.tool(
        name="autodev_request",
        description=(
            "Take a request (feature, fix or improvement) through discovery and refinement, "
            "then ask the user to approve it. Discovery is an interview: relay each question "
            "verbatim, and send the user's answer (including `/done` when they want to finish "
            f"discovery). Never approve for the user. `text` is the request. {_PATH} {_EVENT}"
        ),
    )
    async def autodev_request(text: str, path: str | None = None) -> dict[str, Any]:
        return await start(
            "request",
            path,
            lambda root, io: commands.request(root, io, text, make_runtime=make_runtime),
        )

    @server.tool(
        name="autodev_discover",
        description=(
            "Run discovery and refinement on a DRAFT story (for example one suggested by QA or "
            f"by improve), then ask the user to approve it. {_PATH} {_EVENT}"
        ),
    )
    async def autodev_discover(story_id: str, path: str | None = None) -> dict[str, Any]:
        return await start(
            "discover",
            path,
            lambda root, io: commands.discover(root, io, story_id, make_runtime=make_runtime),
        )

    @server.tool(
        name="autodev_approve",
        description=(
            "Approve a refined story and commit its domain docs. Only call this when the user "
            f"explicitly asked to approve that story. {_PATH} {_EVENT}"
        ),
    )
    async def autodev_approve(story_id: str, path: str | None = None) -> dict[str, Any]:
        return await start("approve", path, lambda root, io: commands.approve(root, io, story_id))

    @server.tool(
        name="autodev_refine",
        description=(
            "Refine a discovered or refined story again, applying the user's feedback "
            f"verbatim. {_PATH} {_EVENT}"
        ),
    )
    async def autodev_refine(
        story_id: str, feedback: str, path: str | None = None
    ) -> dict[str, Any]:
        return await start(
            "refine",
            path,
            lambda root, io: commands.refine(
                root, io, story_id, feedback, make_runtime=make_runtime
            ),
        )

    @server.tool(
        name="autodev_run",
        description=(
            "Implement an approved story (TDD per task), review, QA and merge it into main. "
            "Can take a long time: it usually returns state working; keep calling autodev_poll "
            f"and give the user brief progress updates. {_PATH} {_EVENT}"
        ),
    )
    async def autodev_run(story_id: str, path: str | None = None) -> dict[str, Any]:
        return await start(
            "run",
            path,
            lambda root, io: commands.run(root, io, story_id, make_runtime=make_runtime),
        )

    @server.tool(
        name="autodev_resume",
        description=(f"Unblock a blocked story so autodev_run can continue it. {_PATH} {_EVENT}"),
    )
    async def autodev_resume(story_id: str, path: str | None = None) -> dict[str, Any]:
        return await start("resume", path, lambda root, io: commands.resume(root, io, story_id))

    @server.tool(
        name="autodev_improve",
        description=(
            "Review the process (run logs) and the code, and with lint=true propose lint rules "
            "from the standards. With no flag it runs process and code. It only proposes; each "
            "proposal is a question for the user, to relay verbatim. "
            f"{_PATH} {_EVENT}"
        ),
    )
    async def autodev_improve(
        path: str | None = None, process: bool = False, code: bool = False, lint: bool = False
    ) -> dict[str, Any]:
        return await start(
            "improve",
            path,
            lambda root, io: commands.improve(
                root, io, process=process, code=code, lint=lint, make_runtime=make_runtime
            ),
        )

    @server.tool(
        name="autodev_reply",
        description=(
            "Send the user's exact answer to the question of a session whose state is "
            "awaiting_reply. Never invent or guess the answer: ask the user. Returns the next "
            "session event."
        ),
    )
    async def autodev_reply(session_id: str, answer: str) -> dict[str, Any]:
        return await session_call(lambda: manager.reply(session_id, answer))

    @server.tool(
        name="autodev_poll",
        description=(
            "Wait for news from a session (a question, completion, failure) and return the "
            "next session event. Use while state is working."
        ),
    )
    async def autodev_poll(session_id: str) -> dict[str, Any]:
        return await session_call(lambda: manager.poll(session_id))

    @server.tool(
        name="autodev_cancel",
        description=(
            "Cancel a session when the user wants to stop. A running agent call cannot be "
            "interrupted, so the session may stay working until it returns."
        ),
    )
    async def autodev_cancel(session_id: str) -> dict[str, Any]:
        return await session_call(lambda: manager.cancel(session_id))

    return server
