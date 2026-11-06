import typer
from rich.console import Console
from rich.markdown import Markdown


class ConsoleIO:
    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def show(self, text: str) -> None:
        # Status lines are printed as-is: markdown would eat backticks and reflow them.
        if "\n" in text:
            self._console.print(Markdown(text))
        else:
            self._console.print(text, markup=False, highlight=False, soft_wrap=True)

    def ask(self, prompt: str) -> str:
        return str(typer.prompt(prompt, default="", show_default=False))
