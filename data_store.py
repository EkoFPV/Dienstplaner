from __future__ import annotations

import calendar
import json
from datetime import date, timedelta
from typing import Dict, List, Tuple

from config import DATA_FILE, DEFAULT_SHIFTS, SHIFT_ORDER
from models import Employee

def empty_state() -> dict:
    today = date.today()
    return {
        "year": today.year,
        "month": today.month,
        "employees": [],
        "shifts": DEFAULT_SHIFTS.copy(),
        "availability": {},
        "schedule": {},
        "group_mode": False,
        "previous_month_tail": {},
    }

def load_state() -> dict:
    if not DATA_FILE.exists():
        return empty_state()

    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty_state()

    base = empty_state()
    base.update(data)
    base.setdefault("previous_month_tail", {})

    for employee in base.get("employees", []):
        employee.setdefault("experienced", False)
        employee.setdefault("new_employee", False)
        employee.setdefault("allowed_shifts", ["F", "M", "S", "N"])

        # Ältere oder versehentlich leer gespeicherte Datensätze dürfen
        # eine Person nicht vollständig aus der Planung ausschließen.
        if not employee.get("allowed_shifts"):
            employee["allowed_shifts"] = ["F", "M", "S", "N"]
        employee.setdefault("group_name", "")
        employee.setdefault("group_preferred_shift", "AUTO")
    for key, value in DEFAULT_SHIFTS.items():
        base["shifts"].setdefault(key, value.copy())
    return base

def save_state(state: dict) -> None:
    DATA_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def month_dates(year: int, month: int) -> List[date]:
    last_day = calendar.monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, last_day + 1)]

def date_key(d: date) -> str:
    return d.isoformat()

def employee_key(name: str) -> str:
    return name.strip()

def get_previous_month_shift(
    state: dict,
    employee: str,
    d: date,
) -> str:
    """
    Liest die letzten bis zu fünf Kalendertage des Vormonats.

    Die Eingabe wird pro Person als Liste gespeichert, zum Beispiel:
    ["F", "F", "M", "S", "S"]

    Der letzte Listeneintrag entspricht dem letzten Tag des Vormonats.
    Leere Einträge bedeuten frei.
    """
    current_month_start = date(
        int(state["year"]),
        int(state["month"]),
        1,
    )

    delta = (current_month_start - d).days
    if delta < 1 or delta > 5:
        return ""

    sequence = state.get("previous_month_tail", {}).get(employee, [])
    padded = ([""] * 5 + list(sequence))[-5:]

    # delta 1 = letzter Tag des Vormonats = letzter Eintrag
    return padded[-delta]

def get_schedule(state: dict, employee: str, d: date) -> str:
    current_month_start = date(
        int(state["year"]),
        int(state["month"]),
        1,
    )

    if d < current_month_start:
        return get_previous_month_shift(state, employee_key(employee), d)

    return state["schedule"].get(employee_key(employee), {}).get(date_key(d), "")

def set_schedule(state: dict, employee: str, d: date, shift: str) -> None:
    key = employee_key(employee)
    state["schedule"].setdefault(key, {})
    if shift:
        state["schedule"][key][date_key(d)] = shift
    else:
        state["schedule"][key].pop(date_key(d), None)

def get_availability(state: dict, employee: str, d: date) -> str:
    return state["availability"].get(employee_key(employee), {}).get(date_key(d), "")

def set_availability(state: dict, employee: str, d: date, status: str) -> None:
    key = employee_key(employee)
    state["availability"].setdefault(key, {})
    if status:
        state["availability"][key][date_key(d)] = status
    else:
        state["availability"][key].pop(date_key(d), None)

def parse_weekdays(value: str) -> set[int]:
    mapping = {
        "mo": 0,
        "di": 1,
        "mi": 2,
        "do": 3,
        "fr": 4,
        "sa": 5,
        "so": 6,
    }
    result = set()
    for part in value.split(","):
        cleaned = part.strip().lower()
        if cleaned in mapping:
            result.add(mapping[cleaned])
    return result or set(range(7))

def calculate_hours(state: dict, employee: str, dates: List[date]) -> float:
    total = 0.0
    for d in dates:
        shift = get_schedule(state, employee, d)
        if shift in state["shifts"]:
            total += float(state["shifts"][shift]["hours"])
    return total

def count_shift(state: dict, employee: str, shift: str, dates: List[date]) -> int:
    return sum(get_schedule(state, employee, d) == shift for d in dates)

def is_hard_blocked(state: dict, employee: str, d: date) -> bool:
    return get_availability(state, employee, d) in {"U", "X"}

def has_shift(state: dict, employee: str, d: date) -> bool:
    return get_schedule(state, employee, d) in SHIFT_ORDER

def previous_date(d: date) -> date:
    return d - timedelta(days=1)

def next_date(d: date) -> date:
    return d + timedelta(days=1)

def consecutive_workdays_before(state: dict, employee: str, d: date) -> int:
    count = 0
    cursor = previous_date(d)
    while get_schedule(state, employee, cursor) in SHIFT_ORDER:
        count += 1
        cursor = previous_date(cursor)
    return count

def consecutive_nights_before(state: dict, employee: str, d: date) -> int:
    count = 0
    cursor = previous_date(d)
    while get_schedule(state, employee, cursor) == "N":
        count += 1
        cursor = previous_date(cursor)
    return count

def free_days_before(state: dict, employee: str, d: date) -> int:
    """Zählt direkt vor d liegende freie Tage, maximal bis drei Tage zurück."""
    count = 0
    cursor = previous_date(d)
    while get_schedule(state, employee, cursor) not in SHIFT_ORDER and count < 3:
        count += 1
        cursor = previous_date(cursor)
    return count

def previous_work_block_length(state: dict, employee: str, d: date) -> int:
    """Länge des letzten Arbeitsblocks vor d, begrenzt auf den aktuellen Monat."""
    month_start = date(int(state["year"]), int(state["month"]), 1)
    cursor = previous_date(d)

    while cursor >= month_start and get_schedule(state, employee, cursor) not in SHIFT_ORDER:
        cursor = previous_date(cursor)

    count = 0
    while cursor >= month_start and get_schedule(state, employee, cursor) in SHIFT_ORDER:
        count += 1
        cursor = previous_date(cursor)

    return count

def week_dates_for_day(dates: List[date], d: date) -> List[date]:
    iso = d.isocalendar()
    return [
        candidate for candidate in dates
        if candidate.isocalendar().year == iso.year
        and candidate.isocalendar().week == iso.week
    ]

def weekly_target_for_partial_week(employee: Employee, week_dates: List[date]) -> float:
    return float(employee.weekly_hours) * (len(week_dates) / 7.0)

def weekly_hours(state: dict, employee: str, dates: List[date], d: date) -> float:
    return calculate_hours(state, employee, week_dates_for_day(dates, d))

