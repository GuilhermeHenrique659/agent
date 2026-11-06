import shutil
from importlib.resources import as_file, files
from pathlib import Path

_IGNORED = ("backlog/", "runs/", "auth.json")


def create_agent_dir(path: Path) -> Path:
    """Copy the .agent/ template into the project at `path`; returns the new directory."""
    target = path / ".agent"
    with as_file(files("autodev") / "templates" / "agent") as template:
        shutil.copytree(template, target)
    return target


def ensure_agent_gitignore(path: Path) -> bool:
    """Make sure .agent/.gitignore lists the orchestrator state and credentials; True if changed."""
    gitignore = path / ".agent" / ".gitignore"
    text = gitignore.read_text() if gitignore.is_file() else ""
    present = set(text.split())
    missing = [line for line in _IGNORED if line not in present]
    if not missing:
        return False
    prefix = "" if not text or text.endswith("\n") else "\n"
    gitignore.write_text(text + prefix + "".join(f"{line}\n" for line in missing))
    return True
