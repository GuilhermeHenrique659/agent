import typer
from rich.console import Console
from rich.markdown import Markdown


class ConsoleIO:
    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def show(self, text: str) -> None:
        self._console.print(Markdown(text))

    def ask(self, prompt: str) -> str:
        return str(typer.prompt(prompt, default="", show_default=False))
