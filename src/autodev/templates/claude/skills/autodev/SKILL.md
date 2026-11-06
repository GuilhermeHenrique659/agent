---
name: autodev
description: Use when the user mentions autodev, wants to onboard a repo into autodev, request a feature, fix or improvement through autodev, run, approve or refine an autodev story, run the autodev technical briefing, or run autodev improve.
---

# autodev

autodev takes a request through discovery, refinement, human approval, TDD implementation, review, QA and merge. It is driven through the `autodev_*` MCP tools. Each command tool starts a **session** and returns an event.

## Session protocol

1. Call the command tool (`autodev_onboard`, `autodev_request`, `autodev_run`, ...). Always pass `path` as the absolute path of the project root (normally your working directory): the MCP server's own working directory may be a different folder.
2. Show the event's `messages` to the user.
3. Look at `state`:
   - `awaiting_reply`: show `question` to the user **verbatim**, wait for their answer, then send exactly that answer with `autodev_reply` (including `/done` to finish discovery).
   - `working`: call `autodev_poll`, and give the user a brief progress update. For long `autodev_run` sessions, keep polling periodically.
   - `finished`: summarize the result for the user.
   - `failed`: show `error`. If it looks like authentication or connection trouble, suggest `autodev_doctor`.
   - `cancelled`: tell the user the session ended.
4. Repeat from step 2 with the event returned by `autodev_reply` or `autodev_poll`.

## Rules

- NEVER answer a question, approve a story, accept an ADR or give feedback on the user's behalf. Every `question` goes to the user.
- Only one session per project at a time. Finish or cancel the active one first.
- Use `autodev_cancel` if the user wants to stop. A running agent call cannot be interrupted, so the session may take a while to end.

## Typical flows

- **Onboarding a repo:** `autodev_onboard`, answer its questions through the user, then `autodev_brief` if the briefing was skipped.
- **Request to delivery:** `autodev_request` (discovery interview, refinement, then the user chooses approve, feedback or later) -> `autodev_approve` if they chose later -> `autodev_run` -> `autodev_resume` then `autodev_run` if the story is blocked.
- **Improve:** `autodev_improve` (optionally `lint=true`), relay each proposal to the user; stories it creates continue with `autodev_discover`.
- **Inspect:** `autodev_status`, `autodev_show`, `autodev_check`.
