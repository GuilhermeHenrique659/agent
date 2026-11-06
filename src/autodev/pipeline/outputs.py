from typing import Literal

from pydantic import BaseModel


class TestWriterOutput(BaseModel):
    __test__ = False  # not a pytest test class

    summary: str
    test_files: list[str]


class ImplementerOutput(BaseModel):
    summary: str


class Finding(BaseModel):
    severity: Literal["blocking", "minor"]
    file: str | None = None
    message: str


class ReviewOutput(BaseModel):
    approved: bool
    summary: str
    findings: list[Finding]
