from typing import Protocol


class UserIO(Protocol):
    def show(self, text: str) -> None: ...

    def ask(self, prompt: str) -> str: ...
