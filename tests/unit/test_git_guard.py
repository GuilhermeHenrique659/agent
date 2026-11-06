import pytest

from autodev.adapters.git_guard import is_forbidden_git_command


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git push",
        "npm test && git checkout main",
        "git -C . reset --hard",
        "echo ok; git stash",
        "git rebase main",
        'sh -c "git commit -m x"',
        "bash -c 'git push'",
        "echo $(git stash)",
    ],
)
def test_blocks_mutating_git_commands(command: str) -> None:
    assert is_forbidden_git_command(command)


@pytest.mark.parametrize(
    "command",
    ["git status", "git diff main...HEAD", "git log --oneline", "pytest", "grep git README.md"],
)
def test_allows_read_only_git_and_other_commands(command: str) -> None:
    assert not is_forbidden_git_command(command)
