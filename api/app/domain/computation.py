from __future__ import annotations

import base64
import binascii
import math
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator, model_validator

# Tool-call limits (AD-3): the only tool is run_analysis and its arguments are strictly bounded.
TOOL_NAME = "run_analysis"
MAX_CODE_CHARS = 20_000
MAX_CODE_LINES = 400
MAX_METHOD_CHARS = 2_000
# Same as MAX_SELECTED_ARTIFACTS in contracts.py: a tool call can reference every artifact of one analysis.
MAX_TOOL_INPUTS = 5
# First run plus at most two automatic corrections (AD-5, FR-17).
MAX_EXECUTION_ATTEMPTS = 3
MAX_CORRECTIONS = MAX_EXECUTION_ATTEMPTS - 1

# Execution-artifact limits; mirrored in sandbox-image/finlens_runner.py.
MAX_TABLES = 5
MAX_TABLE_ROWS = 200
MAX_TABLE_COLUMNS = 30
MAX_CELL_CHARS = 200
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 300
MAX_CHARTS = 3
MAX_PNG_BYTES = 1_000_000
MAX_PNG_SIDE = 2_000
MAX_STDOUT_CHARS = 4_000
MAX_DIAGNOSTIC_CHARS = 400

ARTIFACT_REF_PATTERN = r"^artifact_[1-5]$"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

ArtifactRef = Annotated[str, Field(pattern=ARTIFACT_REF_PATTERN)]


def artifact_ref(index: int) -> str:
    """Opaque per-analysis reference; never derived from sheet names or cell content."""
    return f"artifact_{index + 1}"


class RunAnalysisArguments(BaseModel):
    """Arguments of the run_analysis tool. Anything else (commands, URLs, paths, env, image) is rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS, description="Python 3 code that uses frame()/table() and publishes results with show_table()/show_chart()/print().")
    inputs: list[ArtifactRef] = Field(min_length=1, max_length=MAX_TOOL_INPUTS, description='Artifact references from the request payload, e.g. ["artifact_1"].')
    method: str = Field(min_length=1, max_length=MAX_METHOD_CHARS, description="Short methodology explanation in the language of the question.")

    @field_validator("inputs")
    @classmethod
    def unique_inputs(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("inputs must be unique")
        return value

    @field_validator("code")
    @classmethod
    def bounded_lines(cls, value: str) -> str:
        if value.count("\n") + 1 > MAX_CODE_LINES:
            raise ValueError("code exceeds the line limit")
        return value


def run_analysis_tool_schema() -> dict:
    """OpenAI-compatible function definition generated from the strict schema."""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Execute Python (pandas, numpy, matplotlib) on copies of the supplied tabular artifacts in an isolated sandbox without network or files. Use it for every derived figure or chart.",
            "parameters": RunAnalysisArguments.model_json_schema(),
        },
    }


TableCell = Union[StrictBool, StrictInt, StrictFloat, StrictStr, None]


class ResultTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    columns: list[Annotated[str, Field(max_length=MAX_CELL_CHARS)]] = Field(min_length=1, max_length=MAX_TABLE_COLUMNS)
    rows: list[list[TableCell]] = Field(max_length=MAX_TABLE_ROWS)
    totalRows: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def validate_shape(self) -> "ResultTable":
        width = len(self.columns)
        for row in self.rows:
            if len(row) != width:
                raise ValueError("table rows must match the columns")
            for cell in row:
                if isinstance(cell, float) and not math.isfinite(cell):
                    raise ValueError("table cells must be finite")
                if isinstance(cell, str) and len(cell) > MAX_CELL_CHARS:
                    raise ValueError("table cell is too long")
        if self.totalRows < len(self.rows) or self.truncated != (self.totalRows > len(self.rows)):
            raise ValueError("table row counters are inconsistent")
        return self


class ChartDescription(BaseModel):
    """Text alternative of a chart (NFR-12): produced by the executed code, not by the narrative."""

    model_config = ConfigDict(extra="forbid")
    metric: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    period: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    insight: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)


class ResultChart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    pngBase64: str = Field(min_length=1, max_length=(MAX_PNG_BYTES + 2) // 3 * 4)
    widthPx: int = Field(ge=1, le=MAX_PNG_SIDE)
    heightPx: int = Field(ge=1, le=MAX_PNG_SIDE)
    description: ChartDescription

    @model_validator(mode="after")
    def validate_png(self) -> "ResultChart":
        try:
            png = base64.b64decode(self.pngBase64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("chart is not valid base64") from error
        if len(png) > MAX_PNG_BYTES or not png.startswith(PNG_SIGNATURE) or png[12:16] != b"IHDR":
            raise ValueError("chart is not a bounded PNG")
        if (int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")) != (self.widthPx, self.heightPx):
            raise ValueError("chart dimensions do not match the PNG")
        return self


class RunnerSuccess(BaseModel):
    """Raw successful output of one sandbox job, validated on the host before anyone sees it."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["succeeded"]
    tables: list[ResultTable] = Field(max_length=MAX_TABLES)
    charts: list[ResultChart] = Field(max_length=MAX_CHARTS)
    stdout: str = Field(max_length=MAX_STDOUT_CHARS)
    stdoutTruncated: bool = False
    durationMs: int = Field(ge=0)


_ERROR_TYPE = re.compile(r"[^A-Za-z0-9_.]")


class SafeDiagnostic(BaseModel):
    """The only failure information the model receives: bounded and stripped of paths and secrets."""

    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[A-Z_]{3,40}$")
    errorType: str = Field(max_length=64)
    message: str = Field(max_length=MAX_DIAGNOSTIC_CHARS)
    line: int | None = Field(default=None, ge=1, le=100_000)

    @field_validator("errorType")
    @classmethod
    def clean_type(cls, value: str) -> str:
        return _ERROR_TYPE.sub("", value)[:64] or "Error"


class RunnerFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["failed"]
    error: SafeDiagnostic


class ComputationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheetName: str
    address: str


class Computation(BaseModel):
    """Execution artifact shown to the analyst: only code that actually ran successfully."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["succeeded"]
    code: str
    method: str
    attempts: int = Field(ge=1, le=MAX_EXECUTION_ATTEMPTS)
    inputs: list[ComputationInput] = Field(min_length=1)
    tables: list[ResultTable]
    charts: list[ResultChart]
    stdout: str = ""
    durationMs: int = Field(ge=0)
