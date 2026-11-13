from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ParameterRange:
    name: str
    minimum: float
    maximum: float
    line_index: int = -1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParInfDocument:
    path: Path
    lines: List[str]
    encoding: str
    parameter_count: int
    simulation_count: int
    parameter_count_line: int
    simulation_count_line: int
    parameters: List[ParameterRange]


@dataclass
class SummaryResult:
    goal_type: str
    simulation_count: int
    best_simulation: int
    best_goal: float
    metrics: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RoundResult:
    round_number: int
    started_at: str
    finished_at: str
    summary: SummaryResult
    parameters_before: List[ParameterRange]
    suggested_ranges: List[ParameterRange]
    stop_reached: bool = False
    stop_reason: str = ""
    swatcup_iteration: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "round_number": self.round_number,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary.to_dict(),
            "parameters_before": [item.to_dict() for item in self.parameters_before],
            "suggested_ranges": [item.to_dict() for item in self.suggested_ranges],
            "stop_reached": self.stop_reached,
            "stop_reason": self.stop_reason,
            "swatcup_iteration": self.swatcup_iteration,
        }


@dataclass
class ProjectCheck:
    project: Path
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    parameter_count: Optional[int] = None
    simulation_count: Optional[int] = None
    parameters: List[ParameterRange] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "project": str(self.project),
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "parameter_count": self.parameter_count,
            "simulation_count": self.simulation_count,
            "parameters": [item.to_dict() for item in self.parameters],
        }
