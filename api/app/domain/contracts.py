from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.addresses import MAX_EXCEL_COLUMNS, MAX_EXCEL_ROWS, parse_a1
from app.domain.computation import MAX_EXECUTION_ATTEMPTS, Computation

MAX_ARTIFACT_CELLS = 10_000
# Workbook scope limits: the map is metadata-only, the selected artifacts are bounded.
MAX_MAP_SHEETS = 100
MAX_SHEET_TABLES = 20
MAX_SELECTED_ARTIFACTS = 5
MAX_WORKBOOK_CELLS = 20_000

ID_PATTERN = r"^[A-Za-z0-9_-]{8,128}$"
SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{32,128}$"

Scope = Literal["range", "sheet", "workbook"]


def _strip_question(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("question must not be blank")
    return value


class TabularArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheetName: str = Field(min_length=1, max_length=128)
    address: str = Field(min_length=1, max_length=256)
    values: list[list[Any]]
    formulas: list[list[Any]]
    numberFormats: list[list[Any]]

    @property
    def cell_count(self) -> int:
        return len(self.values) * len(self.values[0])

    @model_validator(mode="after")
    def validate_shape_and_size(self) -> "TabularArtifact":
        rows = len(self.values)
        columns = len(self.values[0]) if rows else 0
        if not rows or not columns:
            raise ValueError("artifact must contain at least one cell")
        if rows * columns > MAX_ARTIFACT_CELLS:
            raise ValueError("artifact exceeds the cell limit")
        for matrix_name in ("values", "formulas", "numberFormats"):
            matrix = getattr(self, matrix_name)
            if len(matrix) != rows or any(len(row) != columns for row in matrix):
                raise ValueError(f"{matrix_name} must match values dimensions")
        return self


class SheetTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    address: str = Field(min_length=1, max_length=256)


class WorkbookSheetMap(BaseModel):
    """Structural metadata of one sheet. It never carries cell values or formulas."""

    model_config = ConfigDict(extra="forbid")
    sheetName: str = Field(min_length=1, max_length=128)
    index: int = Field(ge=0)
    visibility: Literal["Visible", "Hidden", "VeryHidden"]
    usedRangeAddress: str | None = Field(default=None, max_length=256)
    rowCount: int = Field(ge=0, le=MAX_EXCEL_ROWS)
    columnCount: int = Field(ge=0, le=MAX_EXCEL_COLUMNS)
    tables: list[SheetTable] = Field(default_factory=list, max_length=MAX_SHEET_TABLES)

    @model_validator(mode="after")
    def validate_used_range(self) -> "WorkbookSheetMap":
        if self.usedRangeAddress is None:
            if self.rowCount or self.columnCount:
                raise ValueError("a sheet without a used range must have zero dimensions")
            return self
        used = parse_a1(self.usedRangeAddress)
        if (used.rows, used.columns) != (self.rowCount, self.columnCount):
            raise ValueError("used range dimensions must match its address")
        return self


class WorkbookMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheets: list[WorkbookSheetMap] = Field(min_length=1, max_length=MAX_MAP_SHEETS)

    @model_validator(mode="after")
    def validate_unique_sheets(self) -> "WorkbookMap":
        names = [sheet.sheetName for sheet in self.sheets]
        if len(set(names)) != len(names):
            raise ValueError("sheet names must be unique")
        return self

    def sheet(self, name: str) -> WorkbookSheetMap | None:
        return next((sheet for sheet in self.sheets if sheet.sheetName == name), None)


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysisId: str = Field(pattern=ID_PATTERN)
    sessionId: str | None = Field(default=None, pattern=SESSION_ID_PATTERN)
    scope: Literal["range", "sheet"]
    question: str = Field(min_length=1, max_length=4_000)
    artifact: TabularArtifact

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        return _strip_question(value)


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Scope


class SessionCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sessionId: str
    scope: Scope
    ttlSeconds: int


class RangeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheetName: str = Field(min_length=1, max_length=128)
    address: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="", max_length=500)


class WorkbookLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    maxArtifacts: int = MAX_SELECTED_ARTIFACTS
    maxCellsPerArtifact: int = MAX_ARTIFACT_CELLS
    maxTotalCells: int = MAX_WORKBOOK_CELLS


class WorkbookPlanRequest(BaseModel):
    """Phase 1: question + metadata-only map (omitted on follow-ups that reuse the session map)."""

    model_config = ConfigDict(extra="forbid")
    analysisId: str = Field(pattern=ID_PATTERN)
    question: str = Field(min_length=1, max_length=4_000)
    workbookMap: WorkbookMap | None = None

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        return _strip_question(value)


class WorkbookPlan(BaseModel):
    """Phase 1 result: the only ranges the add-in is allowed to read for this analysis."""

    model_config = ConfigDict(extra="forbid")
    analysisId: str
    status: Literal["planned"]
    selections: list[RangeSelection] = Field(min_length=1, max_length=MAX_SELECTED_ARTIFACTS)
    sheetsInWorkbook: int = Field(ge=1)
    limits: WorkbookLimits = Field(default_factory=WorkbookLimits)
    elapsedMs: int = Field(ge=0)


class WorkbookAnalysisRequest(BaseModel):
    """Phase 2: bounded artifacts read by the add-in for the planned selections only."""

    model_config = ConfigDict(extra="forbid")
    analysisId: str = Field(pattern=ID_PATTERN)
    artifacts: list[TabularArtifact] = Field(min_length=1, max_length=MAX_SELECTED_ARTIFACTS)

    @model_validator(mode="after")
    def validate_total_size(self) -> "WorkbookAnalysisRequest":
        if sum(artifact.cell_count for artifact in self.artifacts) > MAX_WORKBOOK_CELLS:
            raise ValueError("artifacts exceed the workbook cell limit")
        return self


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheetName: str
    address: str


class Coverage(BaseModel):
    """Which part of the workbook was analysed (Story 3.3): never presented as the whole book unless it was."""

    model_config = ConfigDict(extra="forbid")
    sheetsInWorkbook: int = Field(ge=1)
    sheetsWithData: int = Field(ge=0)
    sheetsUsed: list[str] = Field(min_length=1)
    # True only when every sheet with data was read in full (its whole used range).
    complete: bool


class Narrative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conclusion: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    risks: list[str] = []
    assumptions: list[str] = []
    nextCheck: str = Field(min_length=1)
    # Story 3.2: the model must flag insufficient data instead of masking it with a confident conclusion.
    insufficientData: bool = False
    missingData: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_missing_data(self) -> "Narrative":
        if self.insufficientData and not any(item.strip() for item in self.missingData):
            raise ValueError("insufficient data must name what is missing")
        return self


class ModelAnswer(Narrative):
    """Final answer contract of the model. basedOnComputation=true is accepted only after a successful execution (AD-5)."""

    basedOnComputation: bool = False

    def narrative(self) -> Narrative:
        return Narrative.model_validate(self.model_dump(exclude={"basedOnComputation"}))


class AnalysisError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    nextAction: str


class CompletedAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysisId: str
    status: Literal["completed"]
    narrative: Narrative
    sources: list[Source] = Field(min_length=1)
    coverage: Coverage | None = None
    # Present only when a sandbox job actually succeeded; it carries the code that ran, never a draft.
    computation: Computation | None = None
    elapsedMs: int = Field(ge=0)


class FailedAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysisId: str
    status: Literal["failed"]
    error: AnalysisError
    # Number of sandbox attempts made before the failure; no code, tables or charts are returned.
    computationAttempts: int | None = Field(default=None, ge=1, le=MAX_EXECUTION_ATTEMPTS)
    elapsedMs: int = Field(ge=0)


class CancelledAnalysisResult(BaseModel):
    """Reported only after cleanup: no narrative, sources or computation of a cancelled analysis is ever returned."""

    model_config = ConfigDict(extra="forbid")
    analysisId: str
    status: Literal["cancelled"]
    message: str
    nextAction: str
    elapsedMs: int = Field(ge=0)


AnalysisResult = CompletedAnalysisResult | FailedAnalysisResult | CancelledAnalysisResult

# Server-side lifecycle stages (Story 3.1). The add-in adds its own local stages
# (reading_context, mapping_workbook) before the first request reaches the API.
ServerStage = Literal[
    "selecting_artifacts",
    "planned",
    "preparing_answer",
    "running_computation",
    "retrying_computation",
    "finalizing",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
]
TERMINAL_STAGES = frozenset({"completed", "failed", "cancelled"})


class AnalysisStatus(BaseModel):
    """Typed progress snapshot returned by every start, status and cancel endpoint."""

    model_config = ConfigDict(extra="forbid")
    analysisId: str
    stage: ServerStage
    # Sandbox attempt in progress (1 = first run, 2..3 = automatic corrections).
    computationAttempt: int | None = Field(default=None, ge=1, le=MAX_EXECUTION_ATTEMPTS)
    maxComputationAttempts: int = MAX_EXECUTION_ATTEMPTS
    elapsedMs: int = Field(ge=0)
    # Present only in stage "planned" (workbook phase 1 finished, the add-in may read exactly these ranges).
    plan: WorkbookPlan | None = None
    # Present only in a terminal stage and always matching it.
    result: AnalysisResult | None = None

    @model_validator(mode="after")
    def validate_stage_payload(self) -> "AnalysisStatus":
        if (self.stage == "planned") != (self.plan is not None):
            raise ValueError("a plan is present exactly in stage planned")
        if (self.stage in TERMINAL_STAGES) != (self.result is not None):
            raise ValueError("a result is present exactly in a terminal stage")
        if self.result is not None and self.result.status != self.stage:
            raise ValueError("result status must match the stage")
        return self
