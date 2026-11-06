from pathlib import Path
from typing import Protocol


class ContextSource(Protocol):
    def paths(self) -> list[Path]:
        """Context files for agents, relative to the project root."""
        ...
