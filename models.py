from __future__ import annotations

from dataclasses import dataclass
from typing import List
@dataclass
class Employee:
    name: str
    weekly_hours: float
    monthly_target: float
    senior: bool = False
    experienced: bool = False
    new_employee: bool = False
    fixed_monday_early: bool = False
    allowed_night_days: str = "Mo,Di,Mi,Do,Fr,Sa,So"
    allowed_shifts: List[str] = None
    group_name: str = ""
    group_preferred_shift: str = "AUTO"

    def __post_init__(self) -> None:
        if self.allowed_shifts is None:
            self.allowed_shifts = ["F", "M", "S", "N"]
        self.group_name = (self.group_name or "").strip()
        if self.group_preferred_shift not in {"AUTO", "F", "S"}:
            self.group_preferred_shift = "AUTO"

