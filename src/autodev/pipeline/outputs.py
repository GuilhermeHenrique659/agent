from typing import Literal

from pydantic import BaseModel, Field


class CriterionCoverage(BaseModel):
    criterion: int  # 1-based index into the story's acceptance criteria
    tests: list[str]  # test function names


class TestWriterOutput(BaseModel):
    __test__ = False  # not a pytest test class

    summary: str
    test_files: list[str]
    coverage: list[CriterionCoverage] = []


class ImplementerOutput(BaseModel):
    summary: str
    test_defect: str | None = None


class ScaffoldOutput(BaseModel):
    summary: str


class Finding(BaseModel):
    severity: Literal["blocking", "minor"]
    file: str | None = None
    message: str


class UncoveredCriterion(BaseModel):
    criterion: int  # 1-based index into the story's acceptance criteria
    reason: str


class ReviewOutput(BaseModel):
    approved: bool
    summary: str
    findings: list[Finding]
    test_changes_confirmed: bool = False
    uncovered_criteria: list[UncoveredCriterion] = []


class GlossaryEntry(BaseModel):
    term: str
    definition: str
    notes: str = ""


class EventStorming(BaseModel):
    domain_events: list[str] = []
    commands: list[str] = []
    actors: list[str] = []
    aggregates: list[str] = []
    policies: list[str] = []
    read_models: list[str] = []


class DiscoveryResult(BaseModel):
    title: str
    problem: str
    glossary: list[GlossaryEntry] = []
    event_storming: EventStorming = EventStorming()
    needs_architecture_review: bool = False
    architecture_notes: str = ""
    open_questions: list[str] = []


class Turn[R: BaseModel](BaseModel):
    """One reply of a chat agent: what the user sees, whether it is finished, and the result."""

    message: str
    done: bool
    result: R | None = None


class DiscoveryTurn(Turn[DiscoveryResult]):
    pass


class RefinedTask(BaseModel):
    title: str


class RefinementOutput(BaseModel):
    title: str
    user_story: str
    acceptance_criteria: list[str] = Field(min_length=1)
    tasks: list[RefinedTask] = Field(min_length=1)


class CriterionCheck(BaseModel):
    criterion: str
    met: bool
    evidence: str


class QaIssue(BaseModel):
    severity: Literal["blocking", "minor"]
    message: str


class Improvement(BaseModel):
    title: str
    description: str


class QaOutput(BaseModel):
    approved: bool
    summary: str
    criteria: list[CriterionCheck] = []
    issues: list[QaIssue] = []
    improvements: list[Improvement] = []


class MergeOutput(BaseModel):
    summary: str
    resolved_files: list[str] = []


class AdrDraft(BaseModel):
    title: str
    context: str
    decision: str
    consequences: str


class Commands(BaseModel):
    build: str | None = None
    test: str | None = None
    lint: str | None = None
    format: str | None = None


class TechBrief(BaseModel):
    summary: str
    adrs: list[AdrDraft] = Field(min_length=1)
    commands: Commands
    standards: list[str] = []
    junit_report: str | None = None


class TechBriefTurn(Turn[TechBrief]):
    pass


class ArchitectureReview(BaseModel):
    summary: str
    adrs: list[AdrDraft] = []
    components: list[str] = []
    design_notes: str = ""


class BootstrapOutput(BaseModel):
    summary: str


class ProcessProposal(BaseModel):
    kind: Literal["prompt", "standard", "config"]
    target: str  # prompt: role name; standard: standards file name; config: config key
    rationale: str
    content: str  # prompt: full new instructions; standard: markdown to append; config: advice


class ProcessReview(BaseModel):
    summary: str
    proposals: list[ProcessProposal] = []


class RefactoringProposal(BaseModel):
    title: str
    description: str
    rationale: str
    files: list[str] = []


class CodeReview(BaseModel):
    summary: str
    proposals: list[RefactoringProposal] = []


class LintRule(BaseModel):
    tool: str
    rule: str
    standard: str
    rationale: str


class LintProposal(BaseModel):
    summary: str
    rules: list[LintRule] = []
