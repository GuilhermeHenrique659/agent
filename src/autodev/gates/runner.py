import subprocess
from dataclasses import dataclass
from pathlib import Path

from autodev.config.models import ProjectConfig


@dataclass(frozen=True)
class GateResult:
    name: str
    ok: bool
    output: str
    skipped: bool = False


@dataclass(frozen=True)
class GateReport:
    results: list[GateResult]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.ok]


def run_command(command: str, workdir: Path, timeout_s: int) -> tuple[int, str]:
    # shell=True is intentional: commands come from the project's own config file.
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return -1, f"Command timed out after {timeout_s}s: {command}"
    return proc.returncode, proc.stdout + proc.stderr


class CommandGate:
    def __init__(self, name: str, command: str | None, timeout_s: int) -> None:
        self.name = name
        self.command = command
        self.timeout_s = timeout_s

    def check(self, workdir: Path) -> GateResult:
        if self.command is None:
            return GateResult(self.name, True, "No command configured", skipped=True)
        code, output = run_command(self.command, workdir, self.timeout_s)
        return GateResult(self.name, code == 0, output)


def run_gates(config: ProjectConfig, workdir: Path) -> GateReport:
    timeout = config.limits.command_timeout_s
    commands = config.commands
    gates = [
        CommandGate("build", commands.build, timeout),
        CommandGate("test", commands.test, timeout),
        CommandGate("lint", commands.lint, timeout),
    ]
    results: list[GateResult] = []
    for gate in gates:
        result = gate.check(workdir)
        results.append(result)
        if not result.ok:
            break
    return GateReport(results)
