import re

_FORBIDDEN = (
    "commit|push|pull|fetch|checkout|switch|reset|rebase|merge|branch|tag|stash|restore|clean"
    "|cherry-pick|revert|am|apply|rm|mv|init|clone|worktree|config"
)
# `git`, then any options (e.g. `-C path`, `--no-pager`), then a forbidden subcommand.
_PATTERN = re.compile(
    rf"(?:^|[\s;&|(`'\"])git"
    rf"(?:\s+-{{1,2}}[\w-]+(?:[= ]\S+)?|\s+-C\s+\S+)*"
    rf"\s+(?:{_FORBIDDEN})(?![\w-])"
)


def is_forbidden_git_command(command: str) -> bool:
    return _PATTERN.search(command) is not None
