"""
Dienstplan-App – Version 29 mit Best-Effort-Fallback

Installation:
    python3 -m pip install streamlit pandas XlsxWriter ortools

Start:
    python3 -m streamlit run dienstplan_app_v29_best_effort.py

Hinweis:
Die automatische Planung wird mit Google OR-Tools CP-SAT berechnet.
Harte Regeln werden gleichzeitig geprüft. Wenn sich die Vorgaben widersprechen,
wird der bestehende Plan nicht überschrieben.
"""

from __future__ import annotations

import calendar
import copy
import html
import io
import json
import random
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

try:
    from ortools.sat.python import cp_model
    ORTOOLS_AVAILABLE = True
except ImportError:
    cp_model = None
    ORTOOLS_AVAILABLE = False


APP_TITLE = "Dienstplaner"
DATA_FILE = Path("dienstplan_daten.json")

SHIFT_ORDER = ["F", "M", "S", "N"]
SHIFT_MAXIMUM = {"F": 4, "M": 2, "S": 4, "N": 1}
SHIFT_LABELS = {
    "": "Frei",
    "F": "Früh",
    "M": "Mittel",
    "S": "Spät",
    "N": "Nacht",
    "U": "Urlaub",
    "X": "Nicht verfügbar",
}

DEFAULT_SHIFTS = {
    "F": {"name": "Früh", "start": "07:00", "end": "15:00", "hours": 8, "minimum": 3},
    "M": {"name": "Mittel", "start": "09:00", "end": "17:00", "hours": 8, "minimum": 1},
    "S": {"name": "Spät", "start": "15:00", "end": "23:00", "hours": 8, "minimum": 3},
    "N": {"name": "Nacht", "start": "23:00", "end": "07:00", "hours": 8, "minimum": 1},
}


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


def work_rest_pattern_valid_after_assignment(
    state: dict,
    employee: str,
    dates: List[date],
    proposed_date: date,
    proposed_shift: str,
) -> bool:
    """
    Prüft nur die durch den vorgeschlagenen Dienst betroffenen Arbeitsblöcke.

    Dadurch werden die letzten Vormonatstage korrekt berücksichtigt, ohne dass
    eine ältere, bereits abgeschlossene Folge jeden Dienst des neuen Monats
    pauschal blockiert.

    8-Stunden-Regel:
    - maximal 5 Arbeitstage hintereinander
    - nach 4 Arbeitstagen mindestens 1 freier Tag
    - nach 5 Arbeitstagen mindestens 2 freie Tage

    10-Stunden-Regel:
    - enthält der Block mindestens einen Dienst mit 10 oder mehr Stunden,
      sind maximal 4 Arbeitstage erlaubt
    - nach 4 solchen Arbeitstagen müssen mindestens 2 freie Tage folgen
    """

    def shift_on(check_date: date) -> str:
        if check_date == proposed_date:
            return proposed_shift
        return get_schedule(state, employee, check_date)

    def is_workday(check_date: date) -> bool:
        return shift_on(check_date) in SHIFT_ORDER

    def shift_hours(check_date: date) -> float:
        shift = shift_on(check_date)
        if shift not in state["shifts"]:
            return 0.0
        return float(state["shifts"][shift]["hours"])

    def block_limits(block_days: List[date]) -> Tuple[int, int]:
        has_ten_hour_shift = any(
            shift_hours(day) >= 10.0
            for day in block_days
        )

        if has_ten_hour_shift:
            maximum_length = 4
            required_rest = 2 if len(block_days) == 4 else 0
        else:
            maximum_length = 5
            if len(block_days) == 5:
                required_rest = 2
            elif len(block_days) == 4:
                required_rest = 1
            else:
                required_rest = 0

        return maximum_length, required_rest

    # Zusammenhängenden Arbeitsblock ermitteln, der den neuen Dienst enthält.
    block_start = proposed_date
    while is_workday(block_start - timedelta(days=1)):
        block_start -= timedelta(days=1)

    block_end = proposed_date
    while is_workday(block_end + timedelta(days=1)):
        block_end += timedelta(days=1)

    block_days: List[date] = []
    cursor = block_start
    while cursor <= block_end:
        block_days.append(cursor)
        cursor += timedelta(days=1)

    maximum_length, required_rest_after = block_limits(block_days)

    if len(block_days) > maximum_length:
        return False

    # Falls nach dem Block bereits ein weiterer Arbeitstag existiert,
    # müssen die vorgeschriebenen freien Tage dazwischen vorhanden sein.
    for offset in range(1, required_rest_after + 1):
        if is_workday(block_end + timedelta(days=offset)):
            return False

    # Vor einem neu beginnenden Block prüfen, ob der vorherige Block
    # ausreichend Ruhe erhalten hat. Das ist besonders am Monatsanfang wichtig.
    if not is_workday(block_start - timedelta(days=1)):
        free_days_before = 0
        cursor = block_start - timedelta(days=1)

        while (
            not is_workday(cursor)
            and free_days_before < 3
        ):
            free_days_before += 1
            cursor -= timedelta(days=1)

        previous_block_days: List[date] = []
        while is_workday(cursor):
            previous_block_days.append(cursor)
            cursor -= timedelta(days=1)

        if previous_block_days:
            _, previous_required_rest = block_limits(previous_block_days)

            if free_days_before < previous_required_rest:
                return False

    return True

def assign_protected_weekends(
    state: dict,
    employees: List[Employee],
) -> None:
    """
    Reserviert für jede Person ein vollständiges freies Wochenende.
    Die Wochenenden werden möglichst gleichmäßig über den Monat verteilt.
    """
    weekends = weekend_pairs(state["year"], state["month"])
    protected: Dict[str, str] = {}

    if not weekends:
        state["_protected_weekends"] = protected
        return

    # Gleichmäßige Verteilung, damit nicht alle dasselbe Wochenende frei haben.
    for index, emp in enumerate(employees):
        candidates = weekends[index % len(weekends):] + weekends[:index % len(weekends)]

        chosen = candidates[0]
        for saturday, sunday in candidates:
            friday = saturday - timedelta(days=1)

            # Urlaub oder Sperre am Wochenende ist für ein freies Wochenende sogar passend.
            # Ein fixer Dienst am Samstag/Sonntag existiert derzeit nicht.
            if (
                get_schedule(state, emp.name, saturday) not in SHIFT_ORDER
                and get_schedule(state, emp.name, sunday) not in SHIFT_ORDER
                and get_schedule(state, emp.name, friday) != "N"
            ):
                chosen = (saturday, sunday)
                break

        protected[emp.name] = chosen[0].isoformat()

    state["_protected_weekends"] = protected


def protected_weekend_conflict(
    state: dict,
    employee: str,
    d: date,
    shift: str,
) -> bool:
    """
    Verhindert in der automatischen Planung Dienste am reservierten Wochenende.
    Ein Nachtdienst am Freitag zählt ebenfalls als Wochenendarbeit.
    """
    saturday_iso = state.get("_protected_weekends", {}).get(employee)
    if not saturday_iso:
        return False

    saturday = date.fromisoformat(saturday_iso)
    sunday = saturday + timedelta(days=1)
    friday = saturday - timedelta(days=1)

    if d in {saturday, sunday} and shift in SHIFT_ORDER:
        return True

    if d == friday and shift == "N":
        return True

    return False


def local_block_shape_penalty(
    state: dict,
    employee: str,
    d: date,
) -> float:
    """
    Bewertet die Lage eines neuen Dienstes:
    - isolierte Einzeldienste werden stark bestraft
    - Lücken zwischen Arbeitsblöcken werden bestraft
    - zusammenhängende 3- bis 4-Tage-Blöcke werden bevorzugt
    """
    work = lambda day: get_schedule(state, employee, day) in SHIFT_ORDER

    prev1 = work(d - timedelta(days=1))
    next1 = work(d + timedelta(days=1))
    prev2 = work(d - timedelta(days=2))
    next2 = work(d + timedelta(days=2))

    penalty = 0.0

    # Isolierter einzelner Dienst.
    if not prev1 and not next1:
        penalty += 34.0

    # Anschluss an einen bestehenden Block ist gut.
    if prev1 and next1:
        penalty -= 24.0
    elif prev1 or next1:
        penalty -= 12.0

    # Arbeit - Frei - neuer Dienst bzw. neuer Dienst - Frei - Arbeit vermeiden.
    if not prev1 and prev2:
        penalty += 20.0
    if not next1 and next2:
        penalty += 20.0

    # Blocklänge nach hypothetischer Einteilung abschätzen.
    left = consecutive_workdays_before(state, employee, d)

    right = 0
    cursor = d + timedelta(days=1)
    while get_schedule(state, employee, cursor) in SHIFT_ORDER:
        right += 1
        cursor += timedelta(days=1)

    projected_block = left + 1 + right

    if projected_block == 3:
        penalty -= 8.0
    elif projected_block == 4:
        penalty -= 16.0
    elif projected_block == 5:
        penalty += 28.0

    return penalty


def would_break_hard_rules(
    state: dict,
    employee: Employee,
    d: date,
    shift: str,
    dates: List[date],
    enforce_hour_limit: bool = True,
) -> bool:
    if is_hard_blocked(state, employee.name, d):
        return True

    if shift not in employee.allowed_shifts:
        return True

    # Neue Mitarbeitende dürfen nicht alleine in einer Schicht sein.
    # Da der Nachtdienst exakt mit einer Person besetzt wird, ist N für
    # neue Mitarbeitende automatisch ausgeschlossen.
    if employee.new_employee and shift == "N":
        return True

    if shift == "M" and int(state["shifts"]["M"]["minimum"]) == 0:
        return True

    if protected_weekend_conflict(state, employee.name, d, shift):
        return True

    current = get_schedule(state, employee.name, d)
    if current in SHIFT_ORDER:
        return True

    # Harte Obergrenze pro Schicht und Tag.
    current_count = sum(
        get_schedule(state, item["name"], d) == shift
        for item in state.get("employees", [])
    )
    if current_count >= SHIFT_MAXIMUM[shift]:
        return True

    prev = get_schedule(state, employee.name, previous_date(d))

    if prev == "S" and shift == "F":
        return True

    # Nachtdienste dürfen direkt aufeinanderfolgen (maximal zwei).
    # Nach dem LETZTEN Nachtdienst müssen jedoch zwei vollständige Tage
    # ohne Früh-, Mittel-, Spät- oder Nachtdienst frei bleiben.
    if prev == "N" and shift != "N":
        return True

    two_days_before = get_schedule(
        state, employee.name, d - timedelta(days=2)
    )
    one_day_before = get_schedule(
        state, employee.name, d - timedelta(days=1)
    )

    # Beispiel:
    # N - Frei - Frei - beliebiger erlaubter Dienst
    # Bei N - N beginnt die Zweitagesruhe erst nach dem zweiten N.
    if (
        two_days_before == "N"
        and one_day_before != "N"
        and shift in SHIFT_ORDER
    ):
        return True

    if shift == "N":
        allowed_days = parse_weekdays(employee.allowed_night_days)
        if d.weekday() not in allowed_days:
            return True
        if consecutive_nights_before(state, employee.name, d) >= 2:
            return True

        # Vor Urlaub oder einem gesperrten Tag ist kein Nachtdienst erlaubt.
        if get_availability(state, employee.name, next_date(d)) in {"U", "X"}:
            return True

        # Ein Zweierblock N-N darf nicht nach drei oder mehr direkt
        # davorliegenden Arbeitstagen entstehen. Damit ist z.B.
        # F-F-F-N-N ausgeschlossen.
        if prev == "N":
            first_night = d - timedelta(days=1)
            work_before_pair = consecutive_workdays_before(
                state, employee.name, first_night
            )
            if work_before_pair > 2:
                return True

    if not work_rest_pattern_valid_after_assignment(
        state, employee.name, dates, d, shift
    ):
        return True

    projected = calculate_hours(
        state, employee.name, dates
    ) + float(state["shifts"][shift]["hours"])
    if (
        enforce_hour_limit
        and projected > float(employee.monthly_target) + 5.0
    ):
        return True

    return False

def shift_distribution_penalty(
    state: dict,
    employee: Employee,
    employees: List[Employee],
    dates: List[date],
    shift: str,
) -> float:
    """Faire, proportionale Verteilung von Früh, Mittel und Spät."""
    if shift not in {"F", "M", "S"}:
        return 0.0

    own_count = count_shift(state, employee.name, shift, dates)
    own_ratio = own_count / max(float(employee.monthly_target), 1.0)

    ratios = [
        count_shift(state, other.name, shift, dates) / max(float(other.monthly_target), 1.0)
        for other in employees
    ]
    average_ratio = sum(ratios) / max(len(ratios), 1)

    penalty = max(0.0, own_ratio - average_ratio) * 240.0

    # Innerhalb der Person grob das Verhältnis der Mindestbesetzung beachten.
    total_day_shifts = sum(count_shift(state, employee.name, code, dates) for code in ("F", "M", "S"))
    total_minimum = sum(int(state["shifts"][code]["minimum"]) for code in ("F", "M", "S"))
    if total_day_shifts > 0 and total_minimum > 0:
        current_share = own_count / total_day_shifts
        target_share = int(state["shifts"][shift]["minimum"]) / total_minimum
        penalty += max(0.0, current_share - target_share) * 20.0

    return penalty


def weekly_distribution_penalty(
    state: dict,
    employee: Employee,
    dates: List[date],
    d: date,
    shift: str,
) -> float:
    """Verteilt Wochenstunden möglichst gleichmäßig, besonders bei Teilzeit."""
    week_dates = week_dates_for_day(dates, d)
    target = weekly_target_for_partial_week(employee, week_dates)
    projected = weekly_hours(state, employee.name, dates, d) + float(state["shifts"][shift]["hours"])

    penalty = abs(projected - target) * 0.35
    if employee.weekly_hours <= 20:
        if projected > target + 8:
            penalty += (projected - target - 8) * 5.0
        if projected > employee.weekly_hours + 8:
            penalty += 50.0
    elif projected > target + 16:
        penalty += (projected - target - 16) * 2.0

    return penalty



def same_shift_run_before(
    state: dict,
    employee: str,
    d: date,
    shift: str,
) -> int:
    """Zählt, wie viele unmittelbar vorherige Tage dieselbe Schicht hatten."""
    count = 0
    cursor = d - timedelta(days=1)

    while get_schedule(state, employee, cursor) == shift:
        count += 1
        cursor -= timedelta(days=1)

    return count


def shared_shift_days(
    state: dict,
    employee_a: str,
    employee_b: str,
    dates: List[date],
    shift: str,
    before_date: date,
) -> int:
    """Zählt bisherige gemeinsame Dienste zweier Personen in derselben Schicht."""
    return sum(
        1
        for work_date in dates
        if work_date < before_date
        and get_schedule(state, employee_a, work_date) == shift
        and get_schedule(state, employee_b, work_date) == shift
    )


def group_mode_penalty(
    state: dict,
    employee: Employee,
    employees: List[Employee],
    dates: List[date],
    d: date,
    shift: str,
) -> float:
    """
    Starke Gruppenoptimierung für explizit angelegte Teams.

    Im Gruppenmodus sollen Mitglieder derselben Gruppe möglichst gemeinsam,
    in derselben Schicht und über mehrere zusammenhängende Tage arbeiten.
    Bevorzugt werden 4-Tage-Blöcke; 5 Tage bleiben nur dann möglich, wenn alle
    übrigen Regeln es erlauben.

    Harte Regeln, Senior-Abdeckung, Sollstunden, Ruhezeiten und freie
    Wochenenden bleiben vorrangig.
    """
    if not bool(state.get("group_mode", False)):
        return 0.0

    if shift not in {"F", "S"}:
        return 0.0

    penalty = 0.0
    previous_day = d - timedelta(days=1)
    two_days_before = d - timedelta(days=2)
    group_name = (employee.group_name or "").strip()

    same_run = same_shift_run_before(state, employee.name, d, shift)
    if same_run == 1:
        penalty -= 42.0
    elif same_run == 2:
        penalty -= 60.0
    elif same_run == 3:
        penalty -= 78.0
    elif same_run >= 4:
        penalty += 22.0

    opposite_shift = "S" if shift == "F" else "F"
    if get_schedule(state, employee.name, previous_day) == opposite_shift:
        penalty += 65.0
    if get_schedule(state, employee.name, two_days_before) == opposite_shift:
        penalty += 24.0

    coworkers_today = [
        other
        for other in employees
        if other.name != employee.name
        and get_schedule(state, other.name, d) == shift
    ]

    # Ohne explizite Gruppe bleibt die bisherige automatische Paarungslogik aktiv.
    if not group_name:
        for coworker in coworkers_today:
            shared = shared_shift_days(
                state,
                employee.name,
                coworker.name,
                dates,
                shift,
                d,
            )
            penalty -= min(shared, 8) * 4.0
        return penalty

    group_members = [
        other
        for other in employees
        if other.name != employee.name
        and (other.group_name or "").strip() == group_name
    ]

    same_group_today = [
        other for other in coworkers_today
        if (other.group_name or "").strip() == group_name
    ]
    foreign_group_today = [
        other for other in coworkers_today
        if (other.group_name or "").strip()
        and (other.group_name or "").strip() != group_name
    ]

    # Bereits begonnene Gruppe am selben Tag unbedingt vervollständigen.
    penalty -= len(same_group_today) * 85.0
    penalty += len(foreign_group_today) * 18.0

    # Mitglieder derselben Gruppe sollen gemeinsam vom Vortag weiterlaufen.
    same_group_yesterday = [
        member for member in group_members
        if get_schedule(state, member.name, previous_day) == shift
    ]
    same_group_two_days_before = [
        member for member in group_members
        if get_schedule(state, member.name, two_days_before) == shift
    ]

    if same_group_yesterday:
        penalty -= 72.0 + len(same_group_yesterday) * 28.0
    if same_group_two_days_before:
        penalty -= len(same_group_two_days_before) * 14.0

    # Wenn Gruppenmitglieder heute bereits in der Gegen-Schicht stehen,
    # die Gruppe nicht unnötig aufteilen.
    split_members_today = [
        member for member in group_members
        if get_schedule(state, member.name, d) == opposite_shift
    ]
    penalty += len(split_members_today) * 75.0

    # Explizite Gruppenausrichtung beachten.
    preferred = employee.group_preferred_shift
    if preferred in {"F", "S"}:
        if shift == preferred:
            penalty -= 34.0
        else:
            penalty += 34.0

    # Bei AUTO entscheidet die bisherige Gruppenhistorie, ob die Gruppe eher
    # Früh oder Spät arbeitet. Dadurch bleibt sie über den Monat stabil.
    if preferred == "AUTO" and group_members:
        all_names = [employee.name] + [member.name for member in group_members]
        group_early = sum(count_shift(state, name, "F", dates) for name in all_names)
        group_late = sum(count_shift(state, name, "S", dates) for name in all_names)
        if group_early > group_late and shift == "F":
            penalty -= min(group_early - group_late, 10) * 3.0
        elif group_late > group_early and shift == "S":
            penalty -= min(group_late - group_early, 10) * 3.0
        elif group_early > group_late and shift == "S":
            penalty += min(group_early - group_late, 10) * 2.0
        elif group_late > group_early and shift == "F":
            penalty += min(group_late - group_early, 10) * 2.0

    # Häufig gemeinsame Einsätze derselben Gruppe zusätzlich belohnen.
    for coworker in same_group_today:
        shared = shared_shift_days(
            state,
            employee.name,
            coworker.name,
            dates,
            shift,
            d,
        )
        penalty -= min(shared, 10) * 6.0

    # Der Gruppenmodus ist eine weiche Optimierung.
    # Er darf niemals so stark werden, dass einzelne Personen gar nicht
    # mehr eingeplant werden.
    return max(-120.0, min(120.0, penalty))


def is_day_qualified(employee: Employee) -> bool:
    """
    Senior und Erfahren gelten beide als qualifizierte Abdeckung
    für Früh-, Mittel- und Spätdienst.
    """
    return bool(employee.senior or employee.experienced)


def employee_status_label(employee: Employee) -> str:
    if employee.senior:
        return "Senior"
    if employee.experienced:
        return "Erfahren"
    if employee.new_employee:
        return "Neu"
    return "Nicht erfahren"



def shift_has_senior(
    state: dict,
    employees: List[Employee],
    d: date,
    shift: str,
) -> bool:
    return any(
        member.senior
        for member in shift_staff(state, employees, d, shift)
    )


def requires_four_day_ten_hour_blocks(
    state: dict,
    employee: Employee,
) -> bool:
    """
    40h-Personen sollen bei 10h-Tagdiensten in 4er-Blöcken arbeiten.
    """
    if float(employee.weekly_hours) < 39.5:
        return False

    active_day_shifts = [
        shift
        for shift in ("F", "M", "S")
        if (
            shift in employee.allowed_shifts
            and not (
                shift == "M"
                and int(state["shifts"]["M"]["minimum"]) == 0
            )
        )
    ]

    return any(
        float(state["shifts"][shift]["hours"]) >= 10.0
        for shift in active_day_shifts
    )


def four_day_block_penalty(
    state: dict,
    employee: Employee,
    d: date,
    shift: str,
) -> float:
    """
    Starke weiche Optimierung für 40h-Personen bei 10h-Diensten:
    laufende Blöcke bis auf 4 Tage vervollständigen und neue isolierte
    Blöcke vermeiden.
    """
    if not requires_four_day_ten_hour_blocks(state, employee):
        return 0.0

    if shift == "N":
        return 0.0

    left = consecutive_workdays_before(state, employee.name, d)

    right = 0
    cursor = d + timedelta(days=1)
    while get_schedule(state, employee.name, cursor) in SHIFT_ORDER:
        right += 1
        cursor += timedelta(days=1)

    projected = left + 1 + right
    penalty = 0.0

    if projected == 1:
        penalty += 55.0
    elif projected == 2:
        penalty += 22.0
    elif projected == 3:
        penalty -= 18.0
    elif projected == 4:
        penalty -= 45.0
    elif projected > 4:
        penalty += 500.0

    # Bereits begonnenen Block lieber vervollständigen.
    if left in {1, 2, 3}:
        penalty -= 18.0 * left

    return penalty


def internal_work_blocks(
    state: dict,
    employee: str,
    dates: List[date],
) -> List[List[date]]:
    blocks: List[List[date]] = []
    current: List[date] = []

    for d in dates:
        if get_schedule(state, employee, d) in SHIFT_ORDER:
            current.append(d)
        else:
            if current:
                blocks.append(current)
                current = []

    if current:
        blocks.append(current)

    return blocks


def work_block_penalty(
    state: dict,
    employee: Employee,
    employees: List[Employee],
    dates: List[date],
    d: date,
    shift: str,
) -> float:
    penalty = 0.0
    prev = get_schedule(state, employee.name, previous_date(d))
    nxt = get_schedule(state, employee.name, next_date(d))

    if prev == "":
        penalty += 1.0
    if nxt == "":
        penalty += 0.5

    if prev == "S" and shift == "M":
        penalty += 5.0

    if shift == "N":
        if employee.senior:
            penalty += 50.0

        night_count = count_shift(state, employee.name, "N", dates)
        penalty += night_count * 7.0

        # Zweierblöcke bevorzugen.
        if prev == "N":
            penalty -= 22.0
        elif get_schedule(state, employee.name, next_date(d)) == "N":
            penalty -= 18.0
        else:
            penalty += 8.0

        # Zweierblöcke sind bevorzugt. Nach dem letzten Nachtdienst
        # werden zwei freie Tage durch die harte Regel erzwungen.
        if (
            get_schedule(state, employee.name, next_date(d)) == ""
            and get_schedule(state, employee.name, d + timedelta(days=2)) == ""
        ):
            penalty -= 6.0

    hours = calculate_hours(state, employee.name, dates)
    projected = hours + float(state["shifts"][shift]["hours"])
    target = float(employee.monthly_target)

    if projected > target:
        penalty += (projected - target) * 1.8
    else:
        penalty += abs(target - projected) * 0.03

    # Vier-Tage-Blöcke bevorzugen, fünf Tage möglichst vermeiden.
    prior_days = consecutive_workdays_before(state, employee.name, d)
    if prior_days == 3:
        penalty -= 5.0
    elif prior_days == 4:
        penalty += 22.0

    if employee.weekly_hours <= 20:
        penalty += prior_days * 2.5
    else:
        penalty -= min(prior_days, 3) * 0.6

    penalty += local_block_shape_penalty(
        state,
        employee.name,
        d,
    )
    penalty += four_day_block_penalty(
        state,
        employee,
        d,
        shift,
    )
    penalty += group_mode_penalty(
        state,
        employee,
        employees,
        dates,
        d,
        shift,
    )
    penalty += shift_distribution_penalty(state, employee, employees, dates, shift)
    penalty += weekly_distribution_penalty(state, employee, dates, d, shift)
    return penalty

def ensure_fixed_services(state: dict, employees: List[Employee], dates: List[date]) -> None:
    for emp in employees:
        if not emp.fixed_monday_early:
            continue
        for d in dates:
            if d.weekday() == 0 and not is_hard_blocked(state, emp.name, d):
                if not get_schedule(state, emp.name, d):
                    if not would_break_hard_rules(state, emp, d, "F", dates):
                        set_schedule(state, emp.name, d, "F")


def schedule_nights(state: dict, employees: List[Employee], dates: List[date]) -> None:
    """Plant Nachtdienste bevorzugt als Zweierblöcke."""
    date_set = set(dates)

    for d in dates:
        if any(get_schedule(state, emp.name, d) == "N" for emp in employees):
            continue

        # Zuerst versuchen, d und d+1 derselben Person zu geben.
        pair_candidates = []
        d2 = d + timedelta(days=1)
        if d2 in date_set and not any(
            get_schedule(state, emp.name, d2) == "N" for emp in employees
        ):
            for emp in employees:
                if would_break_hard_rules(state, emp, d, "N", dates):
                    continue
                set_schedule(state, emp.name, d, "N")
                valid_second = not would_break_hard_rules(
                    state, emp, d2, "N", dates
                )
                set_schedule(state, emp.name, d, "")
                if valid_second:
                    pair_candidates.append(emp)

        if pair_candidates:
            non_seniors = [emp for emp in pair_candidates if not emp.senior]
            pool = non_seniors or pair_candidates
            pool.sort(
                key=lambda emp: (
                    count_shift(state, emp.name, "N", dates),
                    weekly_distribution_penalty(state, emp, dates, d, "N"),
                    calculate_hours(state, emp.name, dates) - emp.monthly_target,
                    random.random(),
                )
            )
            chosen = pool[0]
            set_schedule(state, chosen.name, d, "N")
            set_schedule(state, chosen.name, d2, "N")
            continue

        # Falls kein Zweierblock möglich ist, Einzel-Nacht zulassen.
        candidates = [
            emp for emp in employees
            if not would_break_hard_rules(state, emp, d, "N", dates)
        ]
        if not candidates:
            continue

        non_seniors = [emp for emp in candidates if not emp.senior]
        pool = non_seniors or candidates
        pool.sort(
            key=lambda emp: (
                count_shift(state, emp.name, "N", dates),
                weekly_distribution_penalty(state, emp, dates, d, "N"),
                calculate_hours(state, emp.name, dates) - emp.monthly_target,
                random.random(),
            )
        )
        set_schedule(state, pool[0].name, d, "N")

def shift_staff(state: dict, employees: List[Employee], d: date, shift: str) -> List[Employee]:
    return [emp for emp in employees if get_schedule(state, emp.name, d) == shift]


def senior_coverage_bonus(
    state: dict,
    employees: List[Employee],
    d: date,
    shift: str,
    employee: Employee,
) -> float:
    if not is_day_qualified(employee):
        return 0.0

    early_has = any(
        is_day_qualified(emp)
        for emp in shift_staff(state, employees, d, "F")
    )
    middle_has = any(
        is_day_qualified(emp)
        for emp in shift_staff(state, employees, d, "M")
    )
    late_has = any(
        is_day_qualified(emp)
        for emp in shift_staff(state, employees, d, "S")
    )

    if shift == "F":
        return -24.0 if not early_has else 6.0
    if shift == "S":
        return -24.0 if not late_has else 6.0
    if shift == "M":
        # Mittel-Senior nur dann stark bevorzugen, wenn Früh oder Spät
        # noch keine Senior-Abdeckung hat. Sind beide abgedeckt, unnötige
        # Senior-Bindung im Mittel vermeiden.
        if not early_has or not late_has:
            return -18.0 if not middle_has else 4.0
        return 12.0
    return 0.0


def schedule_minimum_staff(state: dict, employees: List[Employee], dates: List[date]) -> None:
    for d in dates:
        # Früh und Spät zuerst besetzen, damit die Senior-Abdeckung
        # dort priorisiert werden kann. Mittel dient bei Bedarf als Brücke.
        for shift in ["F", "S", "M"]:
            minimum = int(state["shifts"][shift]["minimum"])
            while len(shift_staff(state, employees, d, shift)) < minimum:
                candidates = [
                    emp for emp in employees
                    if not would_break_hard_rules(
                        state,
                        emp,
                        d,
                        shift,
                        dates,
                        enforce_hour_limit=True,
                    )
                ]
                if not candidates:
                    break

                # Falls Früh oder Spät noch keinen Senior hat und ein
                # verfügbarer Senior existiert, wird zuerst aus den Senioren gewählt.
                # Dadurch bleibt die Senior-Abdeckung erhalten, ohne andere Personen
                # dauerhaft aus der Planung zu drängen.
                existing_staff = shift_staff(state, employees, d, shift)
                has_new_without_senior = (
                    any(member.new_employee for member in existing_staff)
                    and not any(member.senior for member in existing_staff)
                )

                needs_senior = (
                    (
                        shift in {"F", "S"}
                        and not any(
                            is_day_qualified(member)
                            for member in existing_staff
                        )
                    )
                    or has_new_without_senior
                )

                senior_candidates = [
                    emp for emp in candidates if emp.senior
                ]
                qualified_candidates = [
                    emp for emp in candidates if is_day_qualified(emp)
                ]

                if has_new_without_senior and senior_candidates:
                    scoring_pool = senior_candidates
                elif needs_senior and qualified_candidates:
                    scoring_pool = qualified_candidates
                else:
                    scoring_pool = candidates

                def candidate_score(emp: Employee) -> Tuple[float, float, float, float]:
                    current_hours = calculate_hours(state, emp.name, dates)
                    target = max(float(emp.monthly_target), 1.0)
                    fulfillment_ratio = current_hours / target
                    remaining_hours = float(emp.monthly_target) - current_hours

                    # Priorität 1: Niemand darf bei 0 Stunden oder stark unter Soll
                    # stehen bleiben. Diese Werte werden lexikografisch vor allen
                    # Gruppen- und Blockboni verglichen.
                    if current_hours <= 0.0 and emp.monthly_target > 0:
                        starvation_level = 0.0
                    elif remaining_hours > 7.0:
                        starvation_level = 1.0
                    else:
                        starvation_level = 2.0

                    soft_score = (
                        work_block_penalty(
                            state, emp, employees, dates, d, shift
                        )
                        + senior_coverage_bonus(
                            state, employees, d, shift, emp
                        )
                    )

                    return (
                        starvation_level,
                        fulfillment_ratio,
                        soft_score,
                        random.random(),
                    )

                filtered_pool = []

                for candidate in scoring_pool:
                    if not candidate.new_employee:
                        filtered_pool.append(candidate)
                        continue

                    existing_count = len(existing_staff)
                    senior_already_present = any(
                        member.senior for member in existing_staff
                    )

                    # Neue Person nur zulassen, wenn Senior schon da ist oder
                    # noch mindestens ein Platz für einen Senior frei bleibt.
                    if (
                        senior_already_present
                        or existing_count + 1 < SHIFT_MAXIMUM[shift]
                    ):
                        filtered_pool.append(candidate)

                if filtered_pool:
                    scoring_pool = filtered_pool

                scoring_pool.sort(key=candidate_score)
                set_schedule(state, scoring_pool[0].name, d, shift)


def shift_has_monthly_deficit(
    state: dict,
    employees: List[Employee],
    dates: List[date],
    shift: str,
) -> bool:
    """True, wenn die Mindestbesetzung dieser Schicht an mindestens einem Tag fehlt."""
    minimum = int(state["shifts"][shift]["minimum"])
    if minimum <= 0:
        return False

    return any(
        len(shift_staff(state, employees, d, shift)) < minimum
        for d in dates
    )



def ensure_every_employee_is_used(
    state: dict,
    employees: List[Employee],
    dates: List[date],
) -> None:
    """
    Verhindert, dass eine grundsätzlich einsetzbare Person mit 0 Stunden
    aus der automatischen Planung herausfällt.

    Zuerst wird versucht, einen zusätzlichen regelkonformen Dienst zu vergeben.
    Falls alle Schichten voll sind, wird ein Dienst von einer ausreichend
    versorgten Person innerhalb derselben Schicht übertragen.
    """
    for receiver in employees:
        if receiver.monthly_target <= 0:
            continue
        if calculate_hours(state, receiver.name, dates) > 0:
            continue

        direct_options = []

        for d in dates:
            for shift in SHIFT_ORDER:
                if shift == "M" and int(state["shifts"]["M"]["minimum"]) == 0:
                    continue

                if would_break_hard_rules(
                    state,
                    receiver,
                    d,
                    shift,
                    dates,
                    enforce_hour_limit=True,
                ):
                    continue

                current_count = len(shift_staff(state, employees, d, shift))
                if current_count >= SHIFT_MAXIMUM[shift]:
                    continue

                direct_options.append(
                    (
                        work_block_penalty(
                            state,
                            receiver,
                            employees,
                            dates,
                            d,
                            shift,
                        ),
                        d,
                        shift,
                    )
                )

        if direct_options:
            direct_options.sort(key=lambda item: item[0])
            _, best_date, best_shift = direct_options[0]
            set_schedule(state, receiver.name, best_date, best_shift)
            continue

        swap_options = []

        for d in dates:
            for shift in SHIFT_ORDER:
                if shift not in receiver.allowed_shifts:
                    continue
                if shift == "M" and int(state["shifts"]["M"]["minimum"]) == 0:
                    continue

                for donor in shift_staff(state, employees, d, shift):
                    if donor.name == receiver.name:
                        continue

                    shift_hours = float(state["shifts"][shift]["hours"])
                    donor_hours = calculate_hours(state, donor.name, dates)

                    if donor_hours - shift_hours < float(donor.monthly_target) - 7.0:
                        continue

                    set_schedule(state, donor.name, d, "")

                    valid_receiver = not would_break_hard_rules(
                        state,
                        receiver,
                        d,
                        shift,
                        dates,
                        enforce_hour_limit=True,
                    )

                    # Senior-Abdeckung nach dem Tausch prüfen.
                    senior_ok = True
                    if shift in {"F", "S"}:
                        remaining = shift_staff(state, employees, d, shift)
                        if (
                            is_day_qualified(donor)
                            and not is_day_qualified(receiver)
                        ):
                            senior_ok = any(
                                is_day_qualified(member)
                                for member in remaining
                            )

                    set_schedule(state, donor.name, d, shift)

                    if valid_receiver and senior_ok:
                        swap_options.append(
                            (
                                donor_hours - float(donor.monthly_target),
                                d,
                                shift,
                                donor,
                            )
                        )

        if swap_options:
            swap_options.sort(key=lambda item: item[0], reverse=True)
            _, best_date, best_shift, donor = swap_options[0]
            set_schedule(state, donor.name, best_date, "")
            set_schedule(state, receiver.name, best_date, best_shift)


def fill_target_hours(state: dict, employees: List[Employee], dates: List[date]) -> None:
    """Füllt Sollstunden bis mindestens Soll -7 auf.

    Zusätzliche Dienste werden bevorzugt als zweiter Mitteldienst vergeben.
    Früh/Spät dürfen höchstens 4, Mittel höchstens 2 Personen haben.
    """
    for _round in range(4):
        changed = False
        for emp in sorted(
            employees,
            key=lambda e: calculate_hours(state, e.name, dates) - e.monthly_target,
        ):
            safety = 0
            minimum_hours = float(emp.monthly_target) - 7.0

            while calculate_hours(state, emp.name, dates) + 0.1 < minimum_hours and safety < 300:
                safety += 1
                options = []

                for d in dates:
                    extra_shifts = ["F", "S"]
                    if int(state["shifts"]["M"]["minimum"]) > 0:
                        extra_shifts.insert(0, "M")

                    for shift in extra_shifts:
                        if would_break_hard_rules(state, emp, d, shift, dates):
                            continue

                        count = len(shift_staff(state, employees, d, shift))
                        minimum = int(state["shifts"][shift]["minimum"])

                        # Absolute Verteilungspriorität:
                        # Solange irgendwo im Monat für F oder S die Mindestbesetzung
                        # fehlt, darf an keinem bereits ausreichend besetzten Tag
                        # eine vierte Person in derselben Schicht ergänzt werden.
                        if (
                            shift in {"F", "S"}
                            and shift_has_monthly_deficit(
                                state, employees, dates, shift
                            )
                            and count >= minimum
                        ):
                            continue

                        extra_preference = 0.0
                        if shift == "M" and count == 1:
                            extra_preference = -10.0
                        elif shift in {"F", "S"} and count >= minimum:
                            extra_preference = 10.0

                        score = (
                            work_block_penalty(
                                state, emp, employees, dates, d, shift
                            )
                            + senior_coverage_bonus(
                                state, employees, d, shift, emp
                            )
                            + extra_preference
                        )
                        options.append((score, d, shift))

                if not options:
                    break

                options.sort(key=lambda x: x[0])
                _, best_date, best_shift = options[0]
                set_schedule(state, emp.name, best_date, best_shift)
                changed = True

        if not changed:
            break



def force_balance_all_employees(
    state: dict,
    employees: List[Employee],
    dates: List[date],
) -> None:
    """
    Letzte Ausgleichsphase für die automatische Planung.

    Ziel:
    - jede Person mindestens bis Soll -7 Stunden einteilen
    - niemanden über Soll +5 Stunden bringen
    - Tagesbesetzungen unverändert lassen, wenn Dienste getauscht werden
    - Senior-/Erfahrenen-Abdeckung möglichst erhalten

    Falls eine Person wegen Sperrtagen, erlaubten Diensten oder Ruhezeiten
    objektiv nicht ausreichend einsetzbar ist, bleibt eine Warnung bestehen.
    """
    max_iterations = max(len(employees) * len(dates) * 12, 300)

    for _ in range(max_iterations):
        deficits = [
            emp
            for emp in employees
            if calculate_hours(state, emp.name, dates)
            < float(emp.monthly_target) - 7.0 - 1e-9
        ]

        if not deficits:
            return

        deficits.sort(
            key=lambda emp: (
                calculate_hours(state, emp.name, dates)
                - float(emp.monthly_target)
            )
        )

        receiver = deficits[0]
        receiver_hours = calculate_hours(state, receiver.name, dates)
        improved = False

        # A) Direkten zusätzlichen Dienst versuchen.
        direct_options = []

        for d in dates:
            for shift in SHIFT_ORDER:
                if shift not in receiver.allowed_shifts:
                    continue
                if shift == "M" and int(state["shifts"]["M"]["minimum"]) == 0:
                    continue

                shift_hours = float(state["shifts"][shift]["hours"])

                if receiver_hours + shift_hours > float(receiver.monthly_target) + 5.0:
                    continue

                if len(shift_staff(state, employees, d, shift)) >= SHIFT_MAXIMUM[shift]:
                    continue

                if would_break_hard_rules(
                    state,
                    receiver,
                    d,
                    shift,
                    dates,
                    enforce_hour_limit=True,
                ):
                    continue

                direct_options.append(
                    (
                        work_block_penalty(
                            state,
                            receiver,
                            employees,
                            dates,
                            d,
                            shift,
                        ),
                        d,
                        shift,
                    )
                )

        if direct_options:
            direct_options.sort(key=lambda item: item[0])
            _, best_date, best_shift = direct_options[0]
            set_schedule(state, receiver.name, best_date, best_shift)
            improved = True

        if improved:
            continue

        # B) Einen vorhandenen Dienst derselben Schicht übertragen.
        swap_options = []

        for d in dates:
            for shift in SHIFT_ORDER:
                if shift not in receiver.allowed_shifts:
                    continue
                if shift == "M" and int(state["shifts"]["M"]["minimum"]) == 0:
                    continue

                shift_hours = float(state["shifts"][shift]["hours"])

                if receiver_hours + shift_hours > float(receiver.monthly_target) + 5.0:
                    continue

                for donor in shift_staff(state, employees, d, shift):
                    if donor.name == receiver.name:
                        continue

                    donor_hours = calculate_hours(state, donor.name, dates)

                    # Spender darf nach dem Tausch nicht unter Soll -7 fallen.
                    if (
                        donor_hours - shift_hours
                        < float(donor.monthly_target) - 7.0 - 1e-9
                    ):
                        continue

                    set_schedule(state, donor.name, d, "")

                    receiver_valid = not would_break_hard_rules(
                        state,
                        receiver,
                        d,
                        shift,
                        dates,
                        enforce_hour_limit=True,
                    )

                    qualified_ok = True
                    if shift in {"F", "M", "S"}:
                        remaining_staff = shift_staff(
                            state,
                            employees,
                            d,
                            shift,
                        )

                        if (
                            is_day_qualified(donor)
                            and not is_day_qualified(receiver)
                            and not any(
                                is_day_qualified(member)
                                for member in remaining_staff
                            )
                        ):
                            qualified_ok = False

                    set_schedule(state, donor.name, d, shift)

                    if not receiver_valid or not qualified_ok:
                        continue

                    donor_surplus = (
                        donor_hours - float(donor.monthly_target)
                    )
                    receiver_deficit = (
                        float(receiver.monthly_target) - receiver_hours
                    )

                    swap_options.append(
                        (
                            -donor_surplus,
                            -receiver_deficit,
                            work_block_penalty(
                                state,
                                receiver,
                                employees,
                                dates,
                                d,
                                shift,
                            ),
                            d,
                            shift,
                            donor,
                        )
                    )

        if swap_options:
            swap_options.sort(
                key=lambda item: (
                    item[0],
                    item[1],
                    item[2],
                )
            )
            _, _, _, best_date, best_shift, donor = swap_options[0]
            set_schedule(state, donor.name, best_date, "")
            set_schedule(state, receiver.name, best_date, best_shift)
            improved = True

        if not improved:
            # Für diese stärkste Unterdeckung gibt es aktuell keine
            # regelkonforme Verbesserung mehr.
            return


def rebalance_monthly_hours(
    state: dict,
    employees: List[Employee],
    dates: List[date],
) -> None:
    """
    Überträgt bestehende Dienste innerhalb derselben Schicht von Personen mit
    ausreichend Stunden an Personen unter Soll -7.

    Die Besetzung pro Tag und Schicht bleibt dabei unverändert. Die automatische
    Planung überschreitet weiterhin niemals Soll +5.
    """
    max_rounds = max(len(employees) * len(dates) * 3, 100)

    for _ in range(max_rounds):
        underplanned = sorted(
            [
                emp
                for emp in employees
                if calculate_hours(state, emp.name, dates)
                < float(emp.monthly_target) - 7.0 - 1e-9
            ],
            key=lambda emp: (
                calculate_hours(state, emp.name, dates)
                - float(emp.monthly_target)
            ),
        )

        if not underplanned:
            return

        changed = False

        for receiver in underplanned:
            receiver_hours = calculate_hours(state, receiver.name, dates)

            swap_options = []

            for d in dates:
                for shift in SHIFT_ORDER:
                    shift_hours = float(state["shifts"][shift]["hours"])

                    if receiver_hours + shift_hours > float(receiver.monthly_target) + 5.0:
                        continue

                    if would_break_hard_rules(
                        state,
                        receiver,
                        d,
                        shift,
                        dates,
                        enforce_hour_limit=True,
                    ):
                        continue

                    for donor in employees:
                        if donor.name == receiver.name:
                            continue
                        if get_schedule(state, donor.name, d) != shift:
                            continue

                        donor_hours = calculate_hours(state, donor.name, dates)

                        # Der Spender darf durch die Abgabe selbst nicht unter Soll -7 fallen.
                        if donor_hours - shift_hours < float(donor.monthly_target) - 7.0 - 1e-9:
                            continue

                        # Senior-Abdeckung nicht unnötig zerstören.
                        if is_day_qualified(donor) and shift in {"F", "S", "M"}:
                            remaining_qualified = [
                                emp
                                for emp in shift_staff(state, employees, d, shift)
                                if (
                                    emp.name != donor.name
                                    and is_day_qualified(emp)
                                )
                            ]
                            if (
                                not remaining_qualified
                                and not is_day_qualified(receiver)
                            ):
                                continue

                        donor_surplus = donor_hours - float(donor.monthly_target)
                        receiver_deficit = float(receiver.monthly_target) - receiver_hours

                        receiver_block_penalty = local_block_shape_penalty(
                            state,
                            receiver.name,
                            d,
                        )

                        # Einen Dienst nur ungern aus einem gut gebildeten Block
                        # des Spenders herauslösen.
                        donor_prev = get_schedule(
                            state, donor.name, d - timedelta(days=1)
                        ) in SHIFT_ORDER
                        donor_next = get_schedule(
                            state, donor.name, d + timedelta(days=1)
                        ) in SHIFT_ORDER

                        donor_removal_penalty = 0.0
                        if donor_prev and donor_next:
                            donor_removal_penalty += 30.0
                        elif donor_prev or donor_next:
                            donor_removal_penalty += 10.0

                        swap_options.append(
                            (
                                receiver_block_penalty + donor_removal_penalty,
                                -donor_surplus,
                                -receiver_deficit,
                                d,
                                shift,
                                donor,
                            )
                        )

            if not swap_options:
                continue

            swap_options.sort(key=lambda item: (item[0], item[1], item[2]))
            _, _, _, best_date, best_shift, donor = swap_options[0]

            set_schedule(state, donor.name, best_date, "")
            set_schedule(state, receiver.name, best_date, best_shift)
            changed = True
            break

        if not changed:
            return


def weekend_pairs(year: int, month: int) -> List[Tuple[date, date]]:
    dates = month_dates(year, month)
    pairs = []
    for d in dates:
        if d.weekday() == 5:
            sunday = d + timedelta(days=1)
            if sunday.month == month:
                pairs.append((d, sunday))
    return pairs


def employee_has_free_weekend(state: dict, employee: str) -> bool:
    for saturday, sunday in weekend_pairs(state["year"], state["month"]):
        friday = saturday - timedelta(days=1)

        friday_night = get_schedule(state, employee, friday) == "N"
        saturday_work = has_shift(state, employee, saturday)
        sunday_work = has_shift(state, employee, sunday)

        if not friday_night and not saturday_work and not sunday_work:
            return True
    return False


def try_create_free_weekend(state: dict, emp: Employee, dates: List[date]) -> None:
    if employee_has_free_weekend(state, emp.name):
        return

    for saturday, sunday in weekend_pairs(state["year"], state["month"]):
        friday = saturday - timedelta(days=1)
        affected = [friday, saturday, sunday]

        # Fixe Montag-Frühdienste sind davon nicht betroffen.
        # Urlaub/Sperren sind ohnehin frei.
        removable = []
        valid = True

        for d in affected:
            shift = get_schedule(state, emp.name, d)
            if d == friday and shift != "N":
                continue
            if d in {saturday, sunday} and shift in SHIFT_ORDER:
                removable.append((d, shift))
            elif d == friday and shift == "N":
                removable.append((d, shift))

        if not removable:
            continue

        # Nur entfernen, wenn dadurch die Mindestbesetzung nicht unterschritten wird.
        for d, shift in removable:
            if shift == "N":
                valid = False
                break
            staff_count = len(shift_staff(state, [Employee(**e) for e in state["employees"]], d, shift))
            if staff_count <= int(state["shifts"][shift]["minimum"]):
                valid = False
                break

        if valid:
            for d, _ in removable:
                set_schedule(state, emp.name, d, "")
            if employee_has_free_weekend(state, emp.name):
                return



def repair_new_employee_coverage(
    state: dict,
    employees: List[Employee],
    dates: List[date],
) -> None:
    """
    Sorgt nach der Planung dafür, dass jede neue Person in Früh/Mittel/Spät
    gemeinsam mit mindestens einem Senior arbeitet.

    Falls kein Senior ergänzt werden kann, wird der Dienst der neuen Person
    entfernt, damit kein unzulässiger Dienst bestehen bleibt.
    """
    for emp in employees:
        if not emp.new_employee:
            continue

        for d in dates:
            shift = get_schedule(state, emp.name, d)
            if shift not in {"F", "M", "S"}:
                continue

            if shift_has_senior(state, employees, d, shift):
                continue

            candidates = [
                senior
                for senior in employees
                if (
                    senior.senior
                    and senior.name != emp.name
                    and not would_break_hard_rules(
                        state,
                        senior,
                        d,
                        shift,
                        dates,
                        enforce_hour_limit=True,
                    )
                )
            ]

            if (
                candidates
                and len(shift_staff(state, employees, d, shift))
                < SHIFT_MAXIMUM[shift]
            ):
                candidates.sort(
                    key=lambda senior: calculate_hours(
                        state, senior.name, dates
                    ) / max(float(senior.monthly_target), 1.0)
                )
                set_schedule(state, candidates[0].name, d, shift)
            else:
                set_schedule(state, emp.name, d, "")


def improve_four_day_blocks_for_40h(
    state: dict,
    employees: List[Employee],
    dates: List[date],
) -> None:
    """
    Versucht bei 40h-Personen mit 10h-Tagdiensten interne Arbeitsblöcke
    auf exakt 4 Tage zu bringen.

    Blöcke am Monatsanfang oder Monatsende dürfen kürzer erscheinen,
    weil sie in den Vor- bzw. Folgemonat hineinreichen können.
    """
    if not dates:
        return

    first_day = dates[0]
    last_day = dates[-1]

    for _ in range(4):
        changed = False

        for emp in employees:
            if not requires_four_day_ten_hour_blocks(state, emp):
                continue

            blocks = internal_work_blocks(
                state,
                emp.name,
                dates,
            )

            for block in blocks:
                # Randblöcke können über Monatsgrenzen weiterlaufen.
                if block[0] == first_day or block[-1] == last_day:
                    continue

                if len(block) == 4:
                    continue

                if len(block) > 4:
                    # Sollte durch die 10h-Regel ohnehin nicht vorkommen.
                    continue

                # Kürzere interne Blöcke möglichst rechts oder links ergänzen.
                options = []

                left_target = block[0] - timedelta(days=1)
                right_target = block[-1] + timedelta(days=1)

                for target in [left_target, right_target]:
                    if target not in dates:
                        continue

                    for shift in ("F", "M", "S"):
                        if shift not in emp.allowed_shifts:
                            continue
                        if (
                            shift == "M"
                            and int(state["shifts"]["M"]["minimum"]) == 0
                        ):
                            continue
                        if (
                            len(shift_staff(state, employees, target, shift))
                            >= SHIFT_MAXIMUM[shift]
                        ):
                            continue
                        if would_break_hard_rules(
                            state,
                            emp,
                            target,
                            shift,
                            dates,
                            enforce_hour_limit=True,
                        ):
                            continue

                        options.append(
                            (
                                work_block_penalty(
                                    state,
                                    emp,
                                    employees,
                                    dates,
                                    target,
                                    shift,
                                ),
                                target,
                                shift,
                            )
                        )

                if options:
                    options.sort(key=lambda item: item[0])
                    _, target, shift = options[0]
                    set_schedule(state, emp.name, target, shift)
                    changed = True
                    break

            if changed:
                break

        if not changed:
            return


def validate_schedule(state: dict, employees: List[Employee], dates: List[date]) -> List[str]:
    warnings: List[str] = []

    for d in dates:
        for shift in ["F", "M", "S"]:
            staff = shift_staff(state, employees, d, shift)
            minimum = int(state["shifts"][shift]["minimum"])
            if len(staff) < minimum:
                warnings.append(
                    f"{d.strftime('%d.%m.%Y')}: {SHIFT_LABELS[shift]} nur "
                    f"{len(staff)} von mindestens {minimum} besetzt. "
                    "Möglicherweise reicht die verfügbare Personalkapazität unter "
                    "Einhaltung von Soll + 5 Stunden nicht aus."
                )
            if len(staff) > SHIFT_MAXIMUM[shift]:
                warnings.append(
                    f"{d.strftime('%d.%m.%Y')}: {SHIFT_LABELS[shift]} mit "
                    f"{len(staff)} Personen; maximal erlaubt sind {SHIFT_MAXIMUM[shift]}."
                )

        night_staff = shift_staff(state, employees, d, "N")
        if len(night_staff) != 1:
            warnings.append(
                f"{d.strftime('%d.%m.%Y')}: Nachtdienst ist mit {len(night_staff)} statt genau 1 Person besetzt."
            )

        early_senior = any(
            is_day_qualified(emp)
            for emp in shift_staff(state, employees, d, "F")
        )
        middle_senior = any(
            is_day_qualified(emp)
            for emp in shift_staff(state, employees, d, "M")
        )
        late_senior = any(
            is_day_qualified(emp)
            for emp in shift_staff(state, employees, d, "S")
        )
        if not early_senior and not middle_senior:
            warnings.append(
                f"{d.strftime('%d.%m.%Y')}: Frühbereich ohne Senior-Abdeckung."
            )
        if not late_senior and not middle_senior:
            warnings.append(
                f"{d.strftime('%d.%m.%Y')}: Spätbereich ohne Senior-Abdeckung."
            )

    for emp in employees:
        if requires_four_day_ten_hour_blocks(state, emp):
            for block in internal_work_blocks(
                state,
                emp.name,
                dates,
            ):
                touches_month_edge = (
                    block[0] == dates[0]
                    or block[-1] == dates[-1]
                )

                if not touches_month_edge and len(block) != 4:
                    warnings.append(
                        f"{emp.name}: interner Arbeitsblock mit "
                        f"{len(block)} statt 4 Tagen bei 10h-Diensten."
                    )

        if not employee_has_free_weekend(state, emp.name):
            warnings.append(f"{emp.name}: kein vollständig freies Wochenende im Monat.")

        for d in dates:
            shift = get_schedule(state, emp.name, d)
            availability = get_availability(state, emp.name, d)
            prev = get_schedule(state, emp.name, previous_date(d))

            if shift in SHIFT_ORDER and shift not in emp.allowed_shifts:
                warnings.append(
                    f"{emp.name}, {d.strftime('%d.%m.%Y')}: "
                    f"{SHIFT_LABELS[shift]} ist für diese Person nicht erlaubt."
                )

            if shift == "M" and int(state["shifts"]["M"]["minimum"]) == 0:
                warnings.append(
                    f"{emp.name}, {d.strftime('%d.%m.%Y')}: "
                    "Mitteldienst ist für diesen Monat deaktiviert."
                )

            if emp.new_employee and shift == "N":
                warnings.append(
                    f"{emp.name}, {d.strftime('%d.%m.%Y')}: "
                    "Neue Mitarbeitende dürfen keinen allein besetzten Nachtdienst machen."
                )

            if (
                emp.new_employee
                and shift in {"F", "M", "S"}
                and not shift_has_senior(
                    state,
                    employees,
                    d,
                    shift,
                )
            ):
                warnings.append(
                    f"{emp.name}, {d.strftime('%d.%m.%Y')}: "
                    "Neue Person ohne Senior in derselben Schicht."
                )

            if shift in SHIFT_ORDER and availability in {"U", "X"}:
                warnings.append(f"{emp.name}, {d.strftime('%d.%m.%Y')}: Dienst trotz Urlaub/Sperre.")

            if prev == "S" and shift == "F":
                warnings.append(f"{emp.name}, {d.strftime('%d.%m.%Y')}: Frühdienst direkt nach Spätdienst.")

            if prev == "N" and shift not in {"", "N", "U", "X"}:
                warnings.append(
                    f"{emp.name}, {d.strftime('%d.%m.%Y')}: "
                    "erster freier Tag nach dem letzten Nachtdienst fehlt."
                )

            two_days_before = get_schedule(
                state, emp.name, d - timedelta(days=2)
            )
            if (
                two_days_before == "N"
                and prev != "N"
                and shift in SHIFT_ORDER
            ):
                warnings.append(
                    f"{emp.name}, {d.strftime('%d.%m.%Y')}: "
                    "zweiter freier Tag nach dem letzten Nachtdienst fehlt."
                )

            if shift == "N" and get_availability(state, emp.name, next_date(d)) in {"U", "X"}:
                warnings.append(
                    f"{emp.name}, {d.strftime('%d.%m.%Y')}: Nachtdienst vor Urlaub oder gesperrtem Tag."
                )

            if shift == "N" and consecutive_nights_before(state, emp.name, d) >= 2:
                warnings.append(f"{emp.name}, {d.strftime('%d.%m.%Y')}: mehr als zwei Nachtdienste hintereinander.")

            if shift == "N" and prev == "N":
                first_night = d - timedelta(days=1)
                if consecutive_workdays_before(state, emp.name, first_night) > 2:
                    warnings.append(
                        f"{emp.name}, {d.strftime('%d.%m.%Y')}: Zweier-Nachtblock nach zu langem Arbeitsblock."
                    )

            if shift == "N":
                before_n = get_schedule(state, emp.name, previous_date(d)) == "N"
                after_n = get_schedule(state, emp.name, next_date(d)) == "N"
                if not before_n and not after_n:
                    warnings.append(
                        f"{emp.name}, {d.strftime('%d.%m.%Y')}: einzelner Nachtdienst; Zweierblock wäre bevorzugt."
                    )

            if shift in SHIFT_ORDER:
                # Den bestehenden Plan ohne hypothetische Änderung prüfen.
                if not work_rest_pattern_valid_after_assignment(
                    state,
                    emp.name,
                    dates,
                    d,
                    shift,
                ):
                    block_hours = float(state["shifts"][shift]["hours"])
                    if block_hours >= 10.0:
                        warnings.append(
                            f"{emp.name}, {d.strftime('%d.%m.%Y')}: "
                            "10-Stunden-Regel verletzt: maximal vier Arbeitstage "
                            "und danach mindestens zwei freie Tage."
                        )
                    else:
                        warnings.append(
                            f"{emp.name}, {d.strftime('%d.%m.%Y')}: "
                            "Arbeitsblock- oder Ruhezeitregel verletzt."
                        )

        actual = calculate_hours(state, emp.name, dates)

        if emp.monthly_target > 0 and actual <= 0:
            warnings.append(
                f"{emp.name}: wurde trotz positiver Sollstunden gar nicht eingeplant. "
                "Das ist kein gültiger automatischer Dienstplan. Bitte erlaubte "
                "Dienste, Sperrtage und Vormonatsruhe prüfen."
            )

        difference = actual - emp.monthly_target
        if difference > 5:
            warnings.append(
                f"{emp.name}: Iststunden liegen {difference:+.1f} h über Soll. Erlaubt sind höchstens +5 h."
            )
        elif difference < -7:
            warnings.append(
                f"{emp.name}: Iststunden liegen {difference:+.1f} h unter Soll. Erlaubt sind höchstens -7 h."
            )

        # Wochenverteilung prüfen, besonders bei Teilzeit.
        week_keys = sorted({(d.isocalendar().year, d.isocalendar().week) for d in dates})
        for year_week in week_keys:
            week_dates = [
                d for d in dates
                if (d.isocalendar().year, d.isocalendar().week) == year_week
            ]
            actual_week = calculate_hours(state, emp.name, week_dates)
            target_week = weekly_target_for_partial_week(emp, week_dates)
            tolerance = 8.0 if emp.weekly_hours <= 20 else 16.0
            if actual_week > target_week + tolerance:
                warnings.append(
                    f"{emp.name}, KW {year_week[1]}: {actual_week:.1f} h statt ungefähr {target_week:.1f} h."
                )

    return warnings



def validation_error_score(warnings: List[str]) -> int:
    """
    Weighted score for the rule-check results.

    Hard errors receive a much higher weight than soft planning hints.
    Lower is better.
    """
    score = 0

    for warning in warnings:
        text = warning.lower()

        # Absolute hour limits and missing people are most important.
        if "über soll" in text or "unter soll" in text:
            score += 100
        elif "gar nicht eingeplant" in text:
            score += 150

        # Staffing and forbidden assignments.
        elif "nur " in text and "von mindestens" in text:
            score += 90
        elif "statt genau 1 person" in text:
            score += 90
        elif "maximal erlaubt" in text:
            score += 80
        elif "nicht erlaubt" in text or "deaktiviert" in text:
            score += 80
        elif "urlaub" in text or "sperre" in text:
            score += 90

        # Rest-time and night-shift violations.
        elif "10-stunden-regel verletzt" in text:
            score += 90
        elif "arbeitsblock- oder ruhezeitregel verletzt" in text:
            score += 80
        elif "freier tag" in text or "folgetag" in text:
            score += 80
        elif "mehr als zwei nachtdienste" in text:
            score += 80
        elif "nachtblock" in text:
            score += 70

        # Qualification and weekend rules.
        elif "ohne senior" in text:
            score += 75
        elif "neue person ohne senior" in text:
            score += 100
        elif "kein vollständig freies wochenende" in text:
            score += 75

        # Softer optimization warnings.
        elif "einzelner nachtdienst" in text:
            score += 8
        elif "statt 4 tagen" in text:
            score += 15
        elif "kw " in text:
            score += 20
        else:
            score += 25

    return score


def run_repair_passes(
    state: dict,
    employees: List[Employee],
    dates: List[date],
) -> None:
    """
    One deterministic repair cycle using the existing planning functions.
    It does not delete the whole plan.
    """
    # Close daily staffing gaps first.
    schedule_nights(state, employees, dates)
    schedule_minimum_staff(state, employees, dates)

    # Restore free weekends where possible, then close resulting gaps.
    for emp in employees:
        try_create_free_weekend(state, emp, dates)
    schedule_minimum_staff(state, employees, dates)

    # Fix hour distribution.
    ensure_every_employee_is_used(state, employees, dates)
    fill_target_hours(state, employees, dates)
    rebalance_monthly_hours(state, employees, dates)
    force_balance_all_employees(state, employees, dates)

    # Improve blocks and special employee rules.
    improve_work_blocks(state, employees, dates)
    improve_four_day_blocks_for_40h(state, employees, dates)
    repair_new_employee_coverage(state, employees, dates)

    # Final balance and staffing check.
    force_balance_all_employees(state, employees, dates)
    schedule_minimum_staff(state, employees, dates)


def minimize_schedule_errors(
    state: dict,
    max_rounds: int = 12,
) -> Dict[str, int]:
    """
    Repeatedly runs repair passes and keeps a round only when the weighted
    validation score improves.

    This cannot guarantee a perfect plan when rules conflict or personnel
    capacity is insufficient, but it never knowingly accepts a worse round.
    """
    dates = month_dates(state["year"], state["month"])
    employees = [Employee(**item) for item in state["employees"]]

    before_warnings = validate_schedule(state, employees, dates)
    best_score = validation_error_score(before_warnings)
    best_warning_count = len(before_warnings)
    accepted_rounds = 0

    for _ in range(max_rounds):
        candidate = copy.deepcopy(state)
        candidate_employees = [
            Employee(**item)
            for item in candidate["employees"]
        ]

        run_repair_passes(
            candidate,
            candidate_employees,
            dates,
        )

        candidate_warnings = validate_schedule(
            candidate,
            candidate_employees,
            dates,
        )
        candidate_score = validation_error_score(candidate_warnings)

        # Accept only an actual improvement. Warning count is used as tie-breaker.
        if (
            candidate_score < best_score
            or (
                candidate_score == best_score
                and len(candidate_warnings) < best_warning_count
            )
        ):
            state["schedule"] = copy.deepcopy(candidate["schedule"])
            state["_protected_weekends"] = copy.deepcopy(
                candidate.get("_protected_weekends", {})
            )
            best_score = candidate_score
            best_warning_count = len(candidate_warnings)
            accepted_rounds += 1
        else:
            # Running the same deterministic repair sequence again would not help.
            break

        if best_score == 0:
            break

    after_employees = [Employee(**item) for item in state["employees"]]
    after_warnings = validate_schedule(
        state,
        after_employees,
        dates,
    )

    return {
        "before_count": len(before_warnings),
        "after_count": len(after_warnings),
        "before_score": validation_error_score(before_warnings),
        "after_score": validation_error_score(after_warnings),
        "accepted_rounds": accepted_rounds,
    }



def _history_shift_for_offset(
    state: dict,
    employee: str,
    offset: int,
) -> str:
    """
    offset -1 = letzter Tag des Vormonats,
    offset -5 = fünftletzter erfasster Tag des Vormonats.
    """
    month_start = date(int(state["year"]), int(state["month"]), 1)
    return get_schedule(
        state,
        employee,
        month_start + timedelta(days=offset),
    )


def _initial_required_free_days(
    state: dict,
    employee: Employee,
) -> int:
    """
    Bestimmt die am Monatsanfang noch offenen freien Tage aus dem Vormonat.
    """
    history = [
        _history_shift_for_offset(state, employee.name, offset)
        for offset in range(-5, 0)
    ]

    # Letzter Tag war Nacht: nach dem letzten N sind zwei freie Tage nötig.
    if history[-1] == "N":
        return 2

    # Vorletzter Tag war Nacht, letzter Tag frei: noch ein freier Tag nötig.
    if history[-2] == "N" and history[-1] not in SHIFT_ORDER:
        return 1

    consecutive = 0
    ten_hour_in_block = False

    for shift in reversed(history):
        if shift not in SHIFT_ORDER:
            break

        consecutive += 1
        if float(state["shifts"].get(shift, {}).get("hours", 0)) >= 10:
            ten_hour_in_block = True

    if ten_hour_in_block and consecutive >= 4:
        return 2

    if consecutive >= 5:
        return 2

    if consecutive == 4:
        return 1

    return 0


def solve_schedule_with_ortools(
    state: dict,
    time_limit_seconds: int = 45,
    relax_min_hours: bool = False,
) -> Dict[str, object]:
    """
    Erstellt den Dienstplan mit Google OR-Tools CP-SAT.

    Harte Regeln:
    - höchstens ein Dienst je Person und Tag
    - Verfügbarkeit und erlaubte Schichten
    - Früh/Mittel/Spät/Nacht Mindest- und Höchstbesetzung
    - Nacht genau 1 Person
    - Sollstunden zwischen Soll -7 und Soll +5
    - Spät -> Früh verboten
    - maximal zwei Nachtdienste hintereinander
    - nach dem letzten Nachtdienst zwei Tage frei
    - kein N vor Urlaub oder Sperre
    - freie Wochenenden
    - Vormonatsübergang
    - Neu nur gemeinsam mit Senior; Neu nie im Nachtdienst
    - qualifizierte Abdeckung durch Senior oder Erfahren
    - 8h-/10h-Arbeitsblockregeln

    Weiche Ziele:
    - Sollstunden möglichst genau
    - Seniors möglichst nicht nachts
    - Nachtdienste fair verteilen und möglichst paarweise
    - Arbeitsblöcke statt Einzeldienste
    - 40h-Personen bei 10h-Diensten möglichst in 4er-Blöcken
    - Gruppen im Gruppenmodus möglichst gemeinsam in Früh oder Spät
    - Früh/Mittel/Spät möglichst fair verteilen
    """
    if not ORTOOLS_AVAILABLE:
        return {
            "ok": False,
            "status": "ORTOOLS_MISSING",
            "message": (
                "Google OR-Tools ist nicht installiert. "
                "Bitte ausführen: python3 -m pip install ortools"
            ),
        }

    dates = month_dates(int(state["year"]), int(state["month"]))
    employees = [Employee(**item) for item in state["employees"]]

    if not employees:
        return {
            "ok": False,
            "status": "NO_EMPLOYEES",
            "message": "Es sind keine Mitarbeitenden angelegt.",
        }

    model = cp_model.CpModel()
    person_indices = range(len(employees))
    day_indices = range(len(dates))
    shift_indices = range(len(SHIFT_ORDER))
    shift_index = {shift: idx for idx, shift in enumerate(SHIFT_ORDER)}

    # CP-SAT arbeitet mit ganzen Zahlen. Eine Dezimalstelle reicht für z. B. 134,4 h.
    hour_scale = 10
    shift_hours = {
        shift: int(round(float(state["shifts"][shift]["hours"]) * hour_scale))
        for shift in SHIFT_ORDER
    }

    x = {}
    work = {}

    for p in person_indices:
        emp = employees[p]

        for d in day_indices:
            work[p, d] = model.NewBoolVar(f"work_p{p}_d{d}")

            for s in shift_indices:
                shift = SHIFT_ORDER[s]
                x[p, d, s] = model.NewBoolVar(
                    f"x_p{p}_d{d}_{shift}"
                )

            model.Add(
                sum(x[p, d, s] for s in shift_indices) == work[p, d]
            )

            # Urlaub / gesperrt.
            if is_hard_blocked(state, emp.name, dates[d]):
                model.Add(work[p, d] == 0)

            # Nicht erlaubte Schichten.
            for shift in SHIFT_ORDER:
                s = shift_index[shift]

                if shift not in emp.allowed_shifts:
                    model.Add(x[p, d, s] == 0)

                if shift == "M" and int(state["shifts"]["M"]["minimum"]) == 0:
                    model.Add(x[p, d, s] == 0)

                if (
                    shift == "N"
                    and dates[d].weekday()
                    not in parse_weekdays(emp.allowed_night_days)
                ):
                    model.Add(x[p, d, s] == 0)

                # Neue Mitarbeitende können nicht im allein besetzten N arbeiten.
                if emp.new_employee and shift == "N":
                    model.Add(x[p, d, s] == 0)

            # Fixer Montag-Frühdienst.
            if (
                emp.fixed_monday_early
                and dates[d].weekday() == 0
                and not is_hard_blocked(state, emp.name, dates[d])
                and "F" in emp.allowed_shifts
            ):
                model.Add(x[p, d, shift_index["F"]] == 1)

    # ------------------------------------------------------------------
    # Schichtbesetzung
    # ------------------------------------------------------------------
    for d in day_indices:
        for shift in SHIFT_ORDER:
            s = shift_index[shift]
            staff_count = sum(x[p, d, s] for p in person_indices)
            minimum = int(state["shifts"][shift]["minimum"])
            maximum = int(SHIFT_MAXIMUM[shift])

            if shift == "N":
                model.Add(staff_count == 1)
            elif shift == "M" and minimum == 0:
                model.Add(staff_count == 0)
            else:
                model.Add(staff_count >= minimum)
                model.Add(staff_count <= maximum)

    # ------------------------------------------------------------------
    # Stunden je Person
    # ------------------------------------------------------------------
    total_hours = {}
    hour_deviations = []

    for p in person_indices:
        emp = employees[p]

        total_hours[p] = sum(
            shift_hours[SHIFT_ORDER[s]] * x[p, d, s]
            for d in day_indices
            for s in shift_indices
        )

        target = int(round(float(emp.monthly_target) * hour_scale))
        lower = int(round((float(emp.monthly_target) - 7.0) * hour_scale))
        upper = int(round((float(emp.monthly_target) + 5.0) * hour_scale))

        if not relax_min_hours:
            model.Add(total_hours[p] >= max(0, lower))

        # Soll +5 bleibt immer eine absolute harte Obergrenze.
        model.Add(total_hours[p] <= max(0, upper))

        deviation = model.NewIntVar(
            0,
            max(target, upper, 1),
            f"hour_dev_{p}",
        )
        model.AddAbsEquality(deviation, total_hours[p] - target)
        hour_deviations.append(deviation)

        if relax_min_hours:
            under_hours = model.NewIntVar(
                0,
                max(target, 1),
                f"under_hours_{p}",
            )
            model.Add(under_hours >= target - total_hours[p])
            model.Add(under_hours >= 0)
            # Starke Strafe: Unterstunden nur dann, wenn sie zur vollständigen
            # Besetzung wirklich nötig sind.
            hour_deviations.append(under_hours * 8)

    # ------------------------------------------------------------------
    # Schichtfolgen, Arbeitsblöcke und Vormonat
    # ------------------------------------------------------------------
    all_ten_hour_day_shifts = any(
        float(state["shifts"][shift]["hours"]) >= 10.0
        for shift in ("F", "M", "S")
        if not (
            shift == "M"
            and int(state["shifts"]["M"]["minimum"]) == 0
        )
    )

    for p in person_indices:
        emp = employees[p]

        # Noch offene Ruhe aus dem Vormonat.
        initial_free = _initial_required_free_days(state, emp)
        for d in range(min(initial_free, len(dates))):
            model.Add(work[p, d] == 0)

        # Spät -> Früh verboten.
        previous_last_shift = _history_shift_for_offset(
            state,
            emp.name,
            -1,
        )
        if previous_last_shift == "S" and len(dates) > 0:
            model.Add(x[p, 0, shift_index["F"]] == 0)

        for d in range(len(dates) - 1):
            model.Add(
                x[p, d, shift_index["S"]]
                + x[p, d + 1, shift_index["F"]]
                <= 1
            )

        # Maximal zwei Nachtdienste hintereinander.
        previous_nights = [
            1
            if _history_shift_for_offset(state, emp.name, offset) == "N"
            else 0
            for offset in (-2, -1)
        ]
        extended_nights = previous_nights + [
            x[p, d, shift_index["N"]]
            for d in day_indices
        ]

        for start in range(len(extended_nights) - 2):
            model.Add(
                sum(extended_nights[start:start + 3]) <= 2
            )

        # Nach dem letzten N müssen zwei freie Tage folgen.
        for d in day_indices:
            n_today = x[p, d, shift_index["N"]]

            if d + 1 < len(dates):
                n_tomorrow = x[p, d + 1, shift_index["N"]]
                last_night = model.NewBoolVar(
                    f"last_night_p{p}_d{d}"
                )
                model.Add(last_night <= n_today)
                model.Add(last_night + n_tomorrow <= 1)
                model.Add(last_night >= n_today - n_tomorrow)

                model.Add(work[p, d + 1] == 0).OnlyEnforceIf(last_night)
                if d + 2 < len(dates):
                    model.Add(work[p, d + 2] == 0).OnlyEnforceIf(last_night)
            else:
                # Am Monatsende wird der Übergang im nächsten Monat über
                # die Vormonats-Eingabe berücksichtigt.
                pass

        # Kein Nachtdienst vor Urlaub oder Sperre.
        for d in range(len(dates) - 1):
            if is_hard_blocked(state, emp.name, dates[d + 1]):
                model.Add(x[p, d, shift_index["N"]] == 0)

        # Arbeitsblockgrenzen.
        history_work = [
            1
            if _history_shift_for_offset(
                state,
                emp.name,
                offset,
            ) in SHIFT_ORDER
            else 0
            for offset in range(-5, 0)
        ]
        extended_work = history_work + [
            work[p, d]
            for d in day_indices
        ]

        if all_ten_hour_day_shifts:
            # Bei 10h-Diensten höchstens vier Arbeitstage in fünf Tagen.
            for start in range(len(extended_work) - 4):
                model.Add(
                    sum(extended_work[start:start + 5]) <= 4
                )

            # Vier Arbeitstage -> danach zwei freie Tage.
            for d in range(len(dates) - 5):
                four_block = model.NewBoolVar(
                    f"four_block_10h_p{p}_d{d}"
                )
                block_sum = sum(work[p, d + k] for k in range(4))
                model.Add(block_sum == 4).OnlyEnforceIf(four_block)
                model.Add(block_sum <= 3).OnlyEnforceIf(four_block.Not())
                model.Add(work[p, d + 4] == 0).OnlyEnforceIf(four_block)
                model.Add(work[p, d + 5] == 0).OnlyEnforceIf(four_block)
        else:
            # Bei 8h-Diensten höchstens fünf Arbeitstage in sechs Tagen.
            for start in range(len(extended_work) - 5):
                model.Add(
                    sum(extended_work[start:start + 6]) <= 5
                )

            # Fünf Arbeitstage -> danach zwei freie Tage.
            for d in range(len(dates) - 6):
                five_block = model.NewBoolVar(
                    f"five_block_8h_p{p}_d{d}"
                )
                block_sum = sum(work[p, d + k] for k in range(5))
                model.Add(block_sum == 5).OnlyEnforceIf(five_block)
                model.Add(block_sum <= 4).OnlyEnforceIf(five_block.Not())
                model.Add(work[p, d + 5] == 0).OnlyEnforceIf(five_block)
                model.Add(work[p, d + 6] == 0).OnlyEnforceIf(five_block)

            # Vier Arbeitstage -> mindestens ein freier Tag.
            for d in range(len(dates) - 4):
                four_block = model.NewBoolVar(
                    f"four_block_8h_p{p}_d{d}"
                )
                block_sum = sum(work[p, d + k] for k in range(4))
                model.Add(block_sum == 4).OnlyEnforceIf(four_block)
                model.Add(block_sum <= 3).OnlyEnforceIf(four_block.Not())
                model.Add(work[p, d + 4] == 0).OnlyEnforceIf(four_block)

    # ------------------------------------------------------------------
    # Mindestens ein vollständiges freies Wochenende
    # ------------------------------------------------------------------
    weekends = weekend_pairs(int(state["year"]), int(state["month"]))

    for p in person_indices:
        free_weekend_vars = []

        for index, (saturday, sunday) in enumerate(weekends):
            saturday_index = dates.index(saturday)
            sunday_index = dates.index(sunday)
            friday = saturday - timedelta(days=1)

            free_weekend = model.NewBoolVar(
                f"free_weekend_p{p}_{index}"
            )
            free_weekend_vars.append(free_weekend)

            model.Add(work[p, saturday_index] == 0).OnlyEnforceIf(
                free_weekend
            )
            model.Add(work[p, sunday_index] == 0).OnlyEnforceIf(
                free_weekend
            )

            if friday in dates:
                friday_index = dates.index(friday)
                model.Add(
                    x[p, friday_index, shift_index["N"]] == 0
                ).OnlyEnforceIf(free_weekend)

        if free_weekend_vars:
            model.Add(sum(free_weekend_vars) >= 1)

    # ------------------------------------------------------------------
    # Qualifikation: Senior oder Erfahren deckt Früh/Spät ab.
    # Mitteldienst darf eine fehlende Abdeckung übernehmen.
    # ------------------------------------------------------------------
    qualified_indices = [
        p
        for p, emp in enumerate(employees)
        if emp.senior or emp.experienced
    ]
    senior_indices = [
        p
        for p, emp in enumerate(employees)
        if emp.senior
    ]

    for d in day_indices:
        qualified_f = sum(
            x[p, d, shift_index["F"]]
            for p in qualified_indices
        )
        qualified_m = sum(
            x[p, d, shift_index["M"]]
            for p in qualified_indices
        )
        qualified_s = sum(
            x[p, d, shift_index["S"]]
            for p in qualified_indices
        )

        model.Add(qualified_f + qualified_m >= 1)
        model.Add(qualified_s + qualified_m >= 1)

        # Neue Mitarbeitende brauchen in derselben Schicht einen Senior.
        for p in person_indices:
            if not employees[p].new_employee:
                continue

            for shift in ("F", "M", "S"):
                senior_same_shift = sum(
                    x[q, d, shift_index[shift]]
                    for q in senior_indices
                )
                model.Add(
                    x[p, d, shift_index[shift]]
                    <= senior_same_shift
                )

    # ------------------------------------------------------------------
    # Zielfunktion
    # ------------------------------------------------------------------
    objective_terms = []

    # Sollstunden möglichst exakt.
    objective_terms.extend(
        120 * deviation
        for deviation in hour_deviations
    )

    # Senioren nachts stark vermeiden; Erfahrene dürfen normal N machen.
    for p in person_indices:
        if employees[p].senior:
            for d in day_indices:
                objective_terms.append(
                    100 * x[p, d, shift_index["N"]]
                )

    # Nachtdienste fair auf Nicht-Seniors verteilen.
    night_counts = []
    for p in person_indices:
        if employees[p].senior:
            continue

        night_count = model.NewIntVar(
            0,
            len(dates),
            f"night_count_{p}",
        )
        model.Add(
            night_count
            == sum(
                x[p, d, shift_index["N"]]
                for d in day_indices
            )
        )
        night_counts.append(night_count)

    if len(night_counts) >= 2:
        max_nights = model.NewIntVar(
            0,
            len(dates),
            "max_nights",
        )
        min_nights = model.NewIntVar(
            0,
            len(dates),
            "min_nights",
        )
        model.AddMaxEquality(max_nights, night_counts)
        model.AddMinEquality(min_nights, night_counts)
        objective_terms.append(30 * (max_nights - min_nights))

    # Nachtdienste möglichst in Zweierblöcken.
    for p in person_indices:
        for d in day_indices:
            single_night = model.NewBoolVar(
                f"single_night_p{p}_d{d}"
            )
            neighbors = []

            if d > 0:
                neighbors.append(x[p, d - 1, shift_index["N"]])
            if d + 1 < len(dates):
                neighbors.append(x[p, d + 1, shift_index["N"]])

            if neighbors:
                model.Add(single_night <= x[p, d, shift_index["N"]])
                for neighbor in neighbors:
                    model.Add(single_night + neighbor <= 1)
                model.Add(
                    single_night
                    >= x[p, d, shift_index["N"]] - sum(neighbors)
                )
                objective_terms.append(12 * single_night)

    # Arbeitsstarts minimieren -> zusammenhängende Blöcke.
    for p in person_indices:
        previous_work_constant = (
            1
            if _history_shift_for_offset(
                state,
                employees[p].name,
                -1,
            ) in SHIFT_ORDER
            else 0
        )

        for d in day_indices:
            start_var = model.NewBoolVar(
                f"work_start_p{p}_d{d}"
            )

            if d == 0:
                if previous_work_constant:
                    model.Add(start_var == 0)
                else:
                    model.Add(start_var == work[p, d])
            else:
                model.Add(start_var <= work[p, d])
                model.Add(start_var + work[p, d - 1] <= 1)
                model.Add(
                    start_var
                    >= work[p, d] - work[p, d - 1]
                )

            objective_terms.append(18 * start_var)

    # 40h-Personen bei 10h-Diensten: 4er-Blöcke belohnen.
    if all_ten_hour_day_shifts:
        for p in person_indices:
            if float(employees[p].weekly_hours) < 39.5:
                continue

            for d in range(len(dates) - 3):
                four_consecutive = model.NewBoolVar(
                    f"reward_four_p{p}_d{d}"
                )
                block_sum = sum(work[p, d + k] for k in range(4))
                model.Add(block_sum == 4).OnlyEnforceIf(
                    four_consecutive
                )
                model.Add(block_sum <= 3).OnlyEnforceIf(
                    four_consecutive.Not()
                )
                objective_terms.append(-35 * four_consecutive)

    # Schichtverteilung je Person ungefähr ausgleichen.
    for p in person_indices:
        f_count = sum(
            x[p, d, shift_index["F"]]
            for d in day_indices
        )
        s_count = sum(
            x[p, d, shift_index["S"]]
            for d in day_indices
        )
        fs_difference = model.NewIntVar(
            0,
            len(dates),
            f"fs_difference_{p}",
        )
        model.AddAbsEquality(
            fs_difference,
            f_count - s_count,
        )
        objective_terms.append(5 * fs_difference)

    # Gruppenmodus: Mitglieder derselben Gruppe möglichst gemeinsam F/S.
    if bool(state.get("group_mode", False)):
        groups: Dict[str, List[int]] = {}

        for p, emp in enumerate(employees):
            if emp.group_name:
                groups.setdefault(emp.group_name, []).append(p)

        for group_name, members in groups.items():
            if len(members) < 2:
                continue

            for d in day_indices:
                for shift in ("F", "S"):
                    preferred = employees[members[0]].group_preferred_shift
                    pair_weight = 18
                    if preferred == shift:
                        pair_weight = 28

                    for i in range(len(members)):
                        for j in range(i + 1, len(members)):
                            together = model.NewBoolVar(
                                f"together_{group_name}_{i}_{j}_{d}_{shift}"
                            )
                            left = x[
                                members[i],
                                d,
                                shift_index[shift],
                            ]
                            right = x[
                                members[j],
                                d,
                                shift_index[shift],
                            ]

                            model.Add(together <= left)
                            model.Add(together <= right)
                            model.Add(together >= left + right - 1)
                            objective_terms.append(
                                -pair_weight * together
                            )

    model.Minimize(sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 42

    status = solver.Solve(model)

    if status not in (
        cp_model.OPTIMAL,
        cp_model.FEASIBLE,
    ):
        status_name = solver.StatusName(status)

        return {
            "ok": False,
            "status": status_name,
            "message": (
                "Mit den aktuellen harten Regeln wurde keine gültige Lösung "
                "gefunden. Prüfe insbesondere Gesamtstunden, Verfügbarkeiten, "
                "erlaubte Dienste, Senior-Abdeckung, Vormonat und "
                "Mindestbesetzungen."
            ),
        }

    new_schedule = {
        emp.name: {}
        for emp in employees
    }

    for p in person_indices:
        for d in day_indices:
            for s in shift_indices:
                if solver.Value(x[p, d, s]) == 1:
                    new_schedule[employees[p].name][
                        date_key(dates[d])
                    ] = SHIFT_ORDER[s]

    state["schedule"] = new_schedule

    mode_text = (
        "mit gelockerter Mindeststunden-Untergrenze"
        if relax_min_hours
        else "mit allen strikten Stundenregeln"
    )

    return {
        "ok": True,
        "status": solver.StatusName(status),
        "relaxed_min_hours": bool(relax_min_hours),
        "message": (
            "Dienstplan mit OR-Tools erstellt "
            f"({mode_text}). Solver-Status: {solver.StatusName(status)}."
        ),
        "objective": solver.ObjectiveValue(),
        "wall_time": solver.WallTime(),
    }



def solve_schedule_with_fallback(
    state: dict,
    time_limit_seconds: int = 45,
) -> Dict[str, object]:
    """
    Dreistufige Planung:

    1. OR-Tools mit allen strikten Regeln.
    2. OR-Tools mit gelockerter Untergrenze Soll -7.
    3. Falls auch das unlösbar ist: bisherige Heuristik als Best-Effort-Plan.

    Stufe 3 garantiert nicht, dass alle Regeln erfüllt sind. Dafür wird aber
    immer ein möglichst brauchbarer Plan erzeugt und anschließend in der
    Regelprüfung transparent ausgewiesen, welche Punkte noch offen sind.
    """
    strict_result = solve_schedule_with_ortools(
        state,
        time_limit_seconds=time_limit_seconds,
        relax_min_hours=False,
    )

    if strict_result.get("ok"):
        strict_result["planning_mode"] = "ORTOOLS_STRICT"
        return strict_result

    relaxed_result = solve_schedule_with_ortools(
        state,
        time_limit_seconds=time_limit_seconds,
        relax_min_hours=True,
    )

    if relaxed_result.get("ok"):
        relaxed_result["planning_mode"] = "ORTOOLS_RELAXED"
        relaxed_result["message"] = (
            "Ein vollständig besetzter Plan wurde mit OR-Tools erstellt. "
            "Die Untergrenze Soll −7 musste gelockert werden. Soll +5, "
            "Besetzungen und die übrigen harten Regeln bleiben erhalten."
        )
        return relaxed_result

    # Letzter Fallback: bewährte bisherige Heuristik.
    # Dadurch erhält der Nutzer weiterhin einen Plan, auch wenn sich die
    # mathematisch harten OR-Tools-Regeln gegenseitig ausschließen.
    generate_schedule(state)

    dates = month_dates(state["year"], state["month"])
    employees = [Employee(**item) for item in state["employees"]]
    warnings = validate_schedule(state, employees, dates)

    return {
        "ok": True,
        "status": "BEST_EFFORT",
        "planning_mode": "HEURISTIC_FALLBACK",
        "relaxed_min_hours": True,
        "warning_count": len(warnings),
        "message": (
            "OR-Tools konnte wegen widersprüchlicher harter Regeln keine "
            "vollständige mathematische Lösung finden. Deshalb wurde automatisch "
            "der bisherige Best-Effort-Planer verwendet. Der Dienstplan wurde "
            f"erstellt; bitte die Regelprüfung kontrollieren "
            f"({len(warnings)} Hinweis(e))."
        ),
    }


def generate_schedule(state: dict) -> None:
    dates = month_dates(state["year"], state["month"])
    employees = [Employee(**item) for item in state["employees"]]

    # Bestehende Dienste löschen, Urlaub und Sperren bleiben erhalten.
    state["schedule"] = {emp.name: {} for emp in employees}

    # Für jede Person wird bereits vor der Planung ein vollständiges freies
    # Wochenende reserviert. Automatische Dienste dürfen dieses nicht belegen.
    assign_protected_weekends(state, employees)

    ensure_fixed_services(state, employees, dates)
    schedule_nights(state, employees, dates)

    # Zuerst wird der gesamte Monat ausschließlich bis zur Mindestbesetzung
    # aufgefüllt. Dadurch werden nicht am Monatsanfang schon vier Personen
    # eingeplant, während später im Monat noch Früh- oder Spätdienste fehlen.
    schedule_minimum_staff(state, employees, dates)

    # Freie Wochenenden bestmöglich herstellen, bevor Zusatzdienste entstehen.
    for emp in employees:
        try_create_free_weekend(state, emp, dates)

    # Durch das Freimachen eines Wochenendes können Lücken entstanden sein.
    # Diese werden zuerst geschlossen.
    schedule_minimum_staff(state, employees, dates)

    # Zuerst sicherstellen, dass keine grundsätzlich einsetzbare Person
    # vollständig ohne Dienst bleibt.
    ensure_every_employee_is_used(state, employees, dates)

    # Erst wenn die Grundbesetzung über den gesamten Monat verarbeitet wurde,
    # werden zusätzliche Dienste zur Erreichung der Sollstunden verteilt.
    fill_target_hours(state, employees, dates)

    # Anschließend werden bereits vergebene Dienste fair umverteilt.
    # Dadurch bleiben Personen nicht stark unter Soll, nur weil andere Personen
    # bei der Grundbesetzung bevorzugt wurden.
    rebalance_monthly_hours(state, employees, dates)

    # Nach der Umverteilung nochmals alle noch möglichen Lücken bis Soll -7 füllen.
    fill_target_hours(state, employees, dates)

    # Harte finale Stundenverteilung: Jede Person wird nochmals gezielt bis
    # mindestens Soll -7 aufgefüllt, sofern eine regelkonforme Verteilung möglich ist.
    force_balance_all_employees(state, employees, dates)

    # Abschließend werden unnötig isolierte Zusatzdienste möglichst zu
    # zusammenhängenden 3- bis 4-Tage-Blöcken verschoben.
    improve_work_blocks(state, employees, dates)

    # Die Blockoptimierung darf keine Person wieder stark unter Soll drücken.
    force_balance_all_employees(state, employees, dates)

    # 40h-Personen mit 10h-Diensten möglichst in 4er-Blöcke bringen.
    improve_four_day_blocks_for_40h(state, employees, dates)

    # Neue Mitarbeitende dürfen nur gemeinsam mit einem Senior arbeiten.
    repair_new_employee_coverage(state, employees, dates)

    # Nach den letzten Korrekturen nochmals Stunden ausgleichen.
    force_balance_all_employees(state, employees, dates)



def improve_work_blocks(
    state: dict,
    employees: List[Employee],
    dates: List[date],
) -> None:
    """
    Versucht isolierte Zusatzdienste derselben Person auf andere passende Tage
    derselben Schicht zu verschieben. Mindest- und Höchstbesetzung bleiben erhalten.
    """
    for _ in range(3):
        changed = False

        for emp in employees:
            for source_date in dates:
                shift = get_schedule(state, emp.name, source_date)
                if shift not in {"F", "M", "S"}:
                    continue

                source_staff = len(shift_staff(state, employees, source_date, shift))
                minimum = int(state["shifts"][shift]["minimum"])

                # Dienste aus Mindestbesetzung nicht entfernen.
                if source_staff <= minimum:
                    continue

                source_penalty = local_block_shape_penalty(
                    state, emp.name, source_date
                )

                # Nur klar fragmentierte Dienste verschieben.
                if source_penalty < 15.0:
                    continue

                best_option = None

                for target_date in dates:
                    if target_date == source_date:
                        continue

                    target_staff = len(
                        shift_staff(state, employees, target_date, shift)
                    )
                    if target_staff >= SHIFT_MAXIMUM[shift]:
                        continue

                    # Dienst temporär entfernen, damit die Zielprüfung korrekt ist.
                    set_schedule(state, emp.name, source_date, "")

                    valid = not would_break_hard_rules(
                        state,
                        emp,
                        target_date,
                        shift,
                        dates,
                        enforce_hour_limit=True,
                    )

                    target_penalty = (
                        local_block_shape_penalty(
                            state, emp.name, target_date
                        )
                        if valid
                        else float("inf")
                    )

                    set_schedule(state, emp.name, source_date, shift)

                    if not valid:
                        continue

                    improvement = source_penalty - target_penalty
                    if improvement > 12.0:
                        option = (improvement, target_date)
                        if best_option is None or option[0] > best_option[0]:
                            best_option = option

                if best_option is not None:
                    _, target_date = best_option
                    set_schedule(state, emp.name, source_date, "")
                    set_schedule(state, emp.name, target_date, shift)
                    changed = True
                    break

            if changed:
                break

        if not changed:
            return


def render_summary(state: dict, employees: List[Employee], dates: List[date]) -> pd.DataFrame:
    rows = []
    for emp in employees:
        actual = calculate_hours(state, emp.name, dates)
        rows.append(
            {
                "Name": emp.name,
                "Wochenstunden": emp.weekly_hours,
                "Soll": emp.monthly_target,
                "Ist": actual,
                "Differenz": actual - emp.monthly_target,
                "Status": employee_status_label(emp),
                "Früh": count_shift(state, emp.name, "F", dates),
                "Mittel": count_shift(state, emp.name, "M", dates),
                "Spät": count_shift(state, emp.name, "S", dates),
                "Nacht": count_shift(state, emp.name, "N", dates),
            }
        )
    return pd.DataFrame(rows)


def render_calendar_dataframe(state: dict, employees: List[Employee], dates: List[date]) -> pd.DataFrame:
    rows = []
    for emp in employees:
        row = {"Name": emp.name}
        for d in dates:
            column = f"{d.day:02d} {calendar.day_abbr[d.weekday()]}"
            availability = get_availability(state, emp.name, d)
            shift = get_schedule(state, emp.name, d)
            row[column] = availability if availability in {"U", "X"} else shift
        rows.append(row)
    return pd.DataFrame(rows)


def render_calendar_html(state: dict, employees: List[Employee], dates: List[date]) -> str:
    """Excel-ähnliche Monatsansicht mit farbigen Diensten und Tageszählung."""
    css = """
    <style>
      .schedule-wrap {overflow-x:auto; border:1px solid #cfcfcf; border-radius:10px; max-height:760px;}
      table.schedule {border-collapse:separate; border-spacing:0; font-family:Arial,sans-serif; font-size:12px; min-width:max-content; width:100%;}
      .schedule th,.schedule td {border-right:1px solid #d8d8d8; border-bottom:1px solid #d8d8d8; text-align:center; min-width:48px; height:46px; padding:2px 4px; box-sizing:border-box;}
      .schedule thead th {position:sticky; top:0; z-index:5; background:#f4f4f4; font-weight:700;}
      .schedule .name {position:sticky; left:0; z-index:4; min-width:145px; max-width:145px; text-align:left; padding-left:8px; background:#ef8b3a; font-weight:700;}
      .schedule thead .name {z-index:7;}
      .schedule .hours-col {min-width:58px; background:#fff3e8;}
      .schedule .metric {min-width:56px; background:#f7f7f7; font-weight:600;}
      .schedule .weekend {background:#e8e8e8;}
      .schedule .shift-F {background:#ffffff; color:#111;}
      .schedule .shift-M {background:#dff2df; color:#111;}
      .schedule .shift-S {background:#fff0b8; color:#111;}
      .schedule .shift-N {background:#5a164f; color:#fff; font-weight:700;}
      .schedule .shift-U {background:#ffe0e0; color:#b00020; font-weight:700;}
      .schedule .shift-X {background:#333; color:#fff; font-weight:700;}
      .schedule .shift-code {font-size:13px; font-weight:700; line-height:1.15;}
      .schedule .shift-hours {font-size:10px; line-height:1.15; opacity:.9; margin-top:3px;}
      .schedule .count-label {position:sticky; left:0; z-index:3; text-align:left; padding-left:8px; font-weight:700; background:#d8e8f6;}
      .schedule .count-F {background:#fff7cf;}
      .schedule .count-M {background:#dff2df;}
      .schedule .count-S {background:#ffe3c2;}
      .schedule .count-N {background:#d9b7d3;}
      .schedule .understaffed {background:#ffd0d0 !important; color:#a30000; font-weight:800;}
      .schedule .night-invalid {background:#ffb3b3 !important; color:#8b0000; font-weight:800;}
      .legend {display:flex; gap:14px; flex-wrap:wrap; margin:8px 0 12px 0; font-size:13px;}
      .legend span {display:inline-flex; align-items:center; gap:5px;}
      .swatch {width:15px; height:15px; display:inline-block; border:1px solid #aaa; border-radius:3px;}
    </style>
    """

    legend = """
    <div class='legend'>
      <span><i class='swatch shift-F'></i>Früh</span>
      <span><i class='swatch shift-M'></i>Mittel</span>
      <span><i class='swatch shift-S'></i>Spät</span>
      <span><i class='swatch shift-N'></i>Nacht</span>
      <span><i class='swatch shift-U'></i>Urlaub</span>
      <span><i class='swatch shift-X'></i>Gesperrt</span>
    </div>
    """

    header = ["<thead><tr>", "<th class='name'>Name</th>", "<th class='hours-col'>Wo.h</th>"]
    for d in dates:
        weekend = " weekend" if d.weekday() >= 5 else ""
        weekday = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][d.weekday()]
        header.append(f"<th class='{weekend.strip()}'>{d.day:02d}<br>{weekday}</th>")
    header.extend(["<th class='metric'>Soll</th>", "<th class='metric'>Ist</th>", "<th class='metric'>Diff</th>", "</tr></thead>"])

    body = ["<tbody>"]
    for emp in employees:
        actual = calculate_hours(state, emp.name, dates)
        diff = actual - float(emp.monthly_target)
        body.append("<tr>")
        body.append(f"<td class='name'>{html.escape(emp.name)}</td>")
        body.append(f"<td class='hours-col'>{emp.weekly_hours:g}</td>")

        for d in dates:
            availability = get_availability(state, emp.name, d)
            shift = get_schedule(state, emp.name, d)
            value = availability if availability in {"U", "X"} else shift
            weekend = " weekend" if d.weekday() >= 5 and not value else ""

            if value in SHIFT_ORDER:
                hours = float(state["shifts"][value]["hours"])
                hours_text = f"{hours:g}"
                content = f"<div class='shift-code'>{value}</div><div class='shift-hours'>{hours_text}</div>"
                cell_class = f"shift-{value}"
            elif value in {"U", "X"}:
                content = f"<div class='shift-code'>{value}</div>"
                cell_class = f"shift-{value}"
            else:
                content = ""
                cell_class = weekend.strip()

            body.append(f"<td class='{cell_class}'>{content}</td>")

        body.append(f"<td class='metric'>{emp.monthly_target:g}</td>")
        body.append(f"<td class='metric'>{actual:g}</td>")
        body.append(f"<td class='metric'>{diff:+g}</td>")
        body.append("</tr>")

    # Tageszählung unter dem Dienstplan
    for shift in SHIFT_ORDER:
        body.append("<tr>")
        body.append(f"<td class='count-label count-{shift}'>{SHIFT_LABELS[shift]}</td>")
        body.append("<td class='count-label'></td>")
        minimum = int(state["shifts"][shift]["minimum"])
        for d in dates:
            count = len(shift_staff(state, employees, d, shift))
            invalid = (
                (shift == "N" and count != 1)
                or (shift != "N" and count < minimum)
                or count > SHIFT_MAXIMUM[shift]
            )
            cls = "night-invalid" if shift == "N" and invalid else ("understaffed" if invalid else f"count-{shift}")
            body.append(f"<td class='{cls}'>{count}</td>")
        body.extend(["<td class='metric'></td>", "<td class='metric'></td>", "<td class='metric'></td>", "</tr>"])

    body.append("</tbody>")
    return css + legend + "<div class='schedule-wrap'><table class='schedule'>" + "".join(header + body) + "</table></div>"



def build_excel_export(
    state: dict,
    employees: List[Employee],
    dates: List[date],
) -> bytes:
    """
    Erstellt eine formatierte XLSX-Datei mit Farben, Dienstkürzel,
    Stunden, Wochenendmarkierung, Soll/Ist/Differenz und Tagessummen.

    CSV kann grundsätzlich keine Zellfarben oder Formatierungen speichern.
    """
    output = io.BytesIO()

    with pd.ExcelWriter(
        output,
        engine="xlsxwriter",
    ) as writer:
        workbook = writer.book
        worksheet = workbook.add_worksheet("Dienstplan")
        writer.sheets["Dienstplan"] = worksheet

        colors = {
            "header": "#E9ECEF",
            "name": "#F28C45",
            "weekend": "#E4E4E4",
            "F": "#FFFFFF",
            "M": "#D9EFD9",
            "S": "#FFF0B3",
            "N": "#64145A",
            "U": "#F8D7DA",
            "X": "#3A3A3A",
            "summary_F": "#FFF5C7",
            "summary_M": "#DDF2DD",
            "summary_S": "#FFE0BF",
            "summary_N": "#D8B6D5",
            "warning": "#FFC7CE",
        }

        border = 1
        title_format = workbook.add_format(
            {
                "bold": True,
                "font_size": 16,
                "align": "left",
                "valign": "vcenter",
            }
        )
        header_format = workbook.add_format(
            {
                "bold": True,
                "bg_color": colors["header"],
                "border": border,
                "align": "center",
                "valign": "vcenter",
            }
        )
        name_format = workbook.add_format(
            {
                "bold": True,
                "bg_color": colors["name"],
                "border": border,
                "align": "left",
                "valign": "vcenter",
            }
        )
        number_format = workbook.add_format(
            {
                "border": border,
                "align": "center",
                "valign": "vcenter",
                "num_format": "0.0",
            }
        )

        shift_formats = {}
        for shift in ["F", "M", "S", "N", "U", "X"]:
            font_color = "#FFFFFF" if shift in {"N", "X"} else "#111111"
            shift_formats[shift] = workbook.add_format(
                {
                    "bold": True,
                    "bg_color": colors[shift],
                    "font_color": font_color,
                    "border": border,
                    "align": "center",
                    "valign": "vcenter",
                    "text_wrap": True,
                }
            )

        free_format = workbook.add_format(
            {
                "border": border,
                "align": "center",
                "valign": "vcenter",
            }
        )
        weekend_free_format = workbook.add_format(
            {
                "bg_color": colors["weekend"],
                "border": border,
                "align": "center",
                "valign": "vcenter",
            }
        )

        summary_formats = {
            shift: workbook.add_format(
                {
                    "bold": True,
                    "bg_color": colors[f"summary_{shift}"],
                    "border": border,
                    "align": "center",
                    "valign": "vcenter",
                }
            )
            for shift in ["F", "M", "S", "N"]
        }
        warning_formats = {
            shift: workbook.add_format(
                {
                    "bold": True,
                    "bg_color": colors["warning"],
                    "font_color": "#9C0006",
                    "border": border,
                    "align": "center",
                    "valign": "vcenter",
                }
            )
            for shift in ["F", "M", "S", "N"]
        }

        worksheet.write(0, 0, f"Dienstplan {state['month']:02d}/{state['year']}", title_format)

        # Kopfzeile
        headers = ["Name", "Wochenstunden"]
        headers.extend(
            [f"{d.day:02d}\n{calendar.day_abbr[d.weekday()]}" for d in dates]
        )
        headers.extend(["Soll", "Ist", "Differenz", "Status"])

        header_row = 2
        for col, value in enumerate(headers):
            worksheet.write(header_row, col, value, header_format)

        worksheet.set_row(header_row, 34)
        worksheet.set_column(0, 0, 18)
        worksheet.set_column(1, 1, 14)
        worksheet.set_column(2, 1 + len(dates), 6.5)
        worksheet.set_column(2 + len(dates), 5 + len(dates), 12)

        first_employee_row = header_row + 1

        for row_offset, emp in enumerate(employees):
            row = first_employee_row + row_offset
            worksheet.set_row(row, 38)

            worksheet.write(row, 0, emp.name, name_format)
            worksheet.write_number(row, 1, float(emp.weekly_hours), number_format)

            for day_index, d in enumerate(dates):
                col = 2 + day_index
                availability = get_availability(state, emp.name, d)
                shift = get_schedule(state, emp.name, d)

                if availability in {"U", "X"}:
                    worksheet.write(row, col, availability, shift_formats[availability])
                elif shift in SHIFT_ORDER:
                    hours = float(state["shifts"][shift]["hours"])
                    hours_text = f"{hours:g}"
                    worksheet.write(
                        row,
                        col,
                        f"{shift}\n{hours_text}",
                        shift_formats[shift],
                    )
                else:
                    fmt = weekend_free_format if d.weekday() >= 5 else free_format
                    worksheet.write_blank(row, col, None, fmt)

            actual = calculate_hours(state, emp.name, dates)
            summary_start = 2 + len(dates)
            worksheet.write_number(row, summary_start, float(emp.monthly_target), number_format)
            worksheet.write_number(row, summary_start + 1, actual, number_format)
            worksheet.write_number(
                row,
                summary_start + 2,
                actual - float(emp.monthly_target),
                number_format,
            )
            worksheet.write(
                row,
                summary_start + 3,
                employee_status_label(emp),
                number_format,
            )

        # Tagessummen unten
        summary_start_row = first_employee_row + len(employees) + 1

        for summary_index, shift in enumerate(["F", "M", "S", "N"]):
            row = summary_start_row + summary_index
            worksheet.write(row, 0, shift, summary_formats[shift])
            worksheet.write_blank(row, 1, None, summary_formats[shift])

            minimum = int(state["shifts"][shift]["minimum"])
            maximum = SHIFT_MAXIMUM[shift]

            for day_index, d in enumerate(dates):
                count = len(shift_staff(state, employees, d, shift))
                valid = (
                    count == 1
                    if shift == "N"
                    else minimum <= count <= maximum
                )
                fmt = summary_formats[shift] if valid else warning_formats[shift]
                worksheet.write_number(row, 2 + day_index, count, fmt)

            for col in range(2 + len(dates), 6 + len(dates)):
                worksheet.write_blank(row, col, None, summary_formats[shift])

        worksheet.freeze_panes(first_employee_row, 2)
        worksheet.autofilter(
            header_row,
            0,
            first_employee_row + len(employees) - 1,
            5 + len(dates),
        )
        worksheet.set_landscape()
        worksheet.fit_to_pages(1, 0)
        worksheet.repeat_rows(header_row, header_row)

    output.seek(0)
    return output.getvalue()


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title("Dienstplaner")

    if "state" not in st.session_state:
        st.session_state.state = load_state()

    state = st.session_state.state

    tabs = st.tabs(
        [
            "1. Monat",
            "2. Mitarbeitende",
            "3. Vormonat",
            "4. Schichten",
            "5. Sperrtage & Urlaub",
            "6. Dienstplan",
            "7. Prüfung",
        ]
    )

    with tabs[0]:
        col1, col2 = st.columns(2)
        with col1:
            year = st.number_input(
                "Jahr",
                min_value=2020,
                max_value=2100,
                value=int(state["year"]),
                step=1,
            )
        with col2:
            month = st.selectbox(
                "Monat",
                options=list(range(1, 13)),
                index=int(state["month"]) - 1,
                format_func=lambda m: calendar.month_name[m],
            )

        st.markdown("### Planungsmodus")
        group_mode = st.toggle(
            "Gruppenmodus aktivieren",
            value=bool(state.get("group_mode", False)),
            help=(
                "Im Gruppenmodus werden die auf der Mitarbeitendenseite "
                "angelegten Gruppen möglichst gemeinsam, in derselben Schicht "
                "und in zusammenhängenden 4-Tage-Blöcken eingeplant. Alle "
                "bisherigen Regeln bleiben unverändert gültig."
            ),
        )

        if group_mode:
            st.info(
                "Gruppenmodus aktiv: Früh- und Spätteams werden möglichst stabil "
                "gehalten. Senior-Abdeckung, Stundenlimits, Ruhezeiten, freie "
                "Wochenenden sowie Mindest- und Höchstbesetzung haben weiterhin Vorrang."
            )
        else:
            st.caption(
                "Normalmodus aktiv: Die Planung funktioniert exakt nach den "
                "bisherigen Regeln ohne zusätzliche Gruppenoptimierung."
            )

        if st.button("Monat und Modus übernehmen"):
            state["year"] = int(year)
            state["month"] = int(month)
            state["group_mode"] = bool(group_mode)
            save_state(state)
            st.success("Monat und Planungsmodus wurden gespeichert.")

    with tabs[1]:
        st.subheader("Mitarbeitende")

        with st.form("employee_form", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns(4)
            name = c1.text_input("Name")
            weekly_hours = c2.number_input(
                "Wochenstunden",
                min_value=0.0,
                value=40.0,
                step=1.0,
            )
            monthly_target = c3.number_input(
                "Sollstunden im Monat",
                min_value=0.0,
                value=160.0,
                step=1.0,
            )
            employee_status = c4.selectbox(
                "Status",
                options=["NORMAL", "NEU", "ERFAHREN", "SENIOR"],
                format_func=lambda value: {
                    "NORMAL": "Nicht erfahren",
                    "NEU": "Neu",
                    "ERFAHREN": "Erfahren",
                    "SENIOR": "Senior Case Manager",
                }[value],
            )

            c5, c6 = st.columns(2)
            fixed_monday_early = c5.checkbox("Jeden Montag fixer Frühdienst")
            allowed_night_days = c6.text_input(
                "Erlaubte Nachtdiensttage",
                value="Mo,Di,Mi,Do,Fr,Sa,So",
                help="Beispiel für Steffi: Di,Mi,Do",
            )

            st.markdown("**Erlaubte Dienste**")
            s1, s2, s3, s4 = st.columns(4)
            allow_f = s1.checkbox("Früh", value=True)
            allow_m = s2.checkbox("Mittel", value=True)
            allow_s = s3.checkbox("Spät", value=True)
            allow_n = s4.checkbox("Nacht", value=True)

            st.markdown("**Gruppe (optional)**")
            g1, g2 = st.columns(2)
            group_name = g1.text_input(
                "Gruppenname",
                placeholder="z. B. Team A",
                help="Personen mit demselben Gruppennamen werden im Gruppenmodus möglichst gemeinsam geplant.",
            )
            group_preferred_shift = g2.selectbox(
                "Bevorzugte Gruppenschicht",
                options=["AUTO", "F", "S"],
                format_func=lambda value: {
                    "AUTO": "Automatisch",
                    "F": "Früh",
                    "S": "Spät",
                }[value],
            )

            submitted = st.form_submit_button("Mitarbeiter hinzufügen")
            if submitted:
                clean_name = name.strip()
                if not clean_name:
                    st.error("Bitte einen Namen eingeben.")
                elif any(item["name"].lower() == clean_name.lower() for item in state["employees"]):
                    st.error("Dieser Name existiert bereits.")
                elif not any([allow_f, allow_m, allow_s, allow_n]):
                    st.error("Bitte mindestens einen erlaubten Dienst auswählen.")
                else:
                    employee = Employee(
                        name=clean_name,
                        weekly_hours=float(weekly_hours),
                        monthly_target=float(monthly_target),
                        senior=(employee_status == "SENIOR"),
                        experienced=(employee_status == "ERFAHREN"),
                        new_employee=(employee_status == "NEU"),
                        fixed_monday_early=bool(fixed_monday_early),
                        allowed_night_days=allowed_night_days,
                        allowed_shifts=[
                            shift
                            for shift, enabled in {
                                "F": allow_f,
                                "M": allow_m,
                                "S": allow_s,
                                "N": allow_n,
                            }.items()
                            if enabled
                        ],
                        group_name=group_name.strip(),
                        group_preferred_shift=group_preferred_shift,
                    )
                    state["employees"].append(asdict(employee))
                    state["schedule"].setdefault(clean_name, {})
                    state["availability"].setdefault(clean_name, {})
                    save_state(state)
                    st.success(f"{clean_name} wurde hinzugefügt.")

        if state["employees"]:
            employee_df = pd.DataFrame(state["employees"])

            if not employee_df.empty:
                employee_df["Status"] = employee_df.apply(
                    lambda row: (
                        "Senior"
                        if bool(row.get("senior", False))
                        else (
                            "Erfahren"
                            if bool(row.get("experienced", False))
                            else (
                                "Neu"
                                if bool(row.get("new_employee", False))
                                else "Nicht erfahren"
                            )
                        )
                    ),
                    axis=1,
                )
                employee_df = employee_df.drop(
                    columns=["senior", "experienced", "new_employee"],
                    errors="ignore",
                )

            st.dataframe(
                employee_df,
                width="stretch",
                hide_index=True,
            )

            st.markdown("### Mitarbeiterstatus bearbeiten")
            st.caption(
                "Senior und Erfahren zählen beide als qualifizierte "
                "Abdeckung in Früh-, Mittel- und Spätdiensten. "
                "Erfahrene werden im Nachtdienst normal berücksichtigt; "
                "Senior Case Manager werden dort weiterhin möglichst vermieden. "
                "Neue Mitarbeitende dürfen nur gemeinsam mit einem Senior "
                "in derselben Schicht arbeiten."
            )

            status_col1, status_col2 = st.columns(2)
            status_employee = status_col1.selectbox(
                "Person für Statusänderung",
                options=[item["name"] for item in state["employees"]],
                key="status_employee_select",
            )

            status_data = next(
                item
                for item in state["employees"]
                if item["name"] == status_employee
            )

            if status_data.get("senior", False):
                current_status = "SENIOR"
            elif status_data.get("experienced", False):
                current_status = "ERFAHREN"
            elif status_data.get("new_employee", False):
                current_status = "NEU"
            else:
                current_status = "NORMAL"

            new_status = status_col2.selectbox(
                "Neuer Status",
                options=["NORMAL", "NEU", "ERFAHREN", "SENIOR"],
                index=["NORMAL", "NEU", "ERFAHREN", "SENIOR"].index(current_status),
                format_func=lambda value: {
                    "NORMAL": "Nicht erfahren",
                    "NEU": "Neu",
                    "ERFAHREN": "Erfahren",
                    "SENIOR": "Senior Case Manager",
                }[value],
                key=f"status_edit_{status_employee}",
            )

            if st.button("Status speichern"):
                for item in state["employees"]:
                    if item["name"] == status_employee:
                        item["senior"] = new_status == "SENIOR"
                        item["experienced"] = new_status == "ERFAHREN"
                        item["new_employee"] = new_status == "NEU"
                        break
                save_state(state)
                st.success("Mitarbeiterstatus gespeichert.")
                st.rerun()

            st.markdown("### Gruppen verwalten")
            st.caption(
                "Gib mehreren Personen denselben Gruppennamen. Im Gruppenmodus "
                "werden diese Personen möglichst gemeinsam in derselben Früh- "
                "oder Spätschicht und in zusammenhängenden 4-Tage-Blöcken geplant."
            )

            group_col1, group_col2, group_col3 = st.columns(3)
            group_employee = group_col1.selectbox(
                "Person auswählen",
                options=[item["name"] for item in state["employees"]],
                key="group_employee_select",
            )
            selected_group_data = next(
                item for item in state["employees"]
                if item["name"] == group_employee
            )
            existing_group_names = sorted(
                {
                    item.get("group_name", "").strip()
                    for item in state["employees"]
                    if item.get("group_name", "").strip()
                }
            )
            group_value = group_col2.text_input(
                "Gruppe",
                value=selected_group_data.get("group_name", ""),
                placeholder="z. B. Team A",
                key=f"group_name_edit_{group_employee}",
            )
            preferred_value = selected_group_data.get(
                "group_preferred_shift", "AUTO"
            )
            if preferred_value not in {"AUTO", "F", "S"}:
                preferred_value = "AUTO"
            preferred_shift = group_col3.selectbox(
                "Gruppenschicht",
                options=["AUTO", "F", "S"],
                index=["AUTO", "F", "S"].index(preferred_value),
                format_func=lambda value: {
                    "AUTO": "Automatisch",
                    "F": "Früh",
                    "S": "Spät",
                }[value],
                key=f"group_shift_edit_{group_employee}",
            )

            group_button_col1, group_button_col2 = st.columns(2)

            if group_button_col1.button("Gruppenzuordnung speichern"):
                for item in state["employees"]:
                    if item["name"] == group_employee:
                        item["group_name"] = group_value.strip()
                        item["group_preferred_shift"] = preferred_shift
                        break
                save_state(state)
                st.success("Gruppenzuordnung gespeichert.")
                st.rerun()

            if group_button_col2.button(
                "Person aus Team entfernen",
                disabled=not bool(
                    selected_group_data.get("group_name", "").strip()
                ),
            ):
                for item in state["employees"]:
                    if item["name"] == group_employee:
                        item["group_name"] = ""
                        item["group_preferred_shift"] = "AUTO"
                        break
                save_state(state)
                st.success(
                    f"{group_employee} wurde aus der Gruppe entfernt."
                )
                st.rerun()

            if existing_group_names:
                st.markdown("#### Team entfernen")

                remove_team_col1, remove_team_col2 = st.columns([3, 1])

                team_to_remove = remove_team_col1.selectbox(
                    "Gruppe vollständig auflösen",
                    options=[""] + existing_group_names,
                    format_func=lambda value: (
                        "Bitte Gruppe auswählen"
                        if value == ""
                        else value
                    ),
                    key="remove_complete_team",
                )

                if remove_team_col2.button(
                    "Team entfernen",
                    disabled=not team_to_remove,
                ):
                    for item in state["employees"]:
                        if (
                            item.get("group_name", "").strip()
                            == team_to_remove
                        ):
                            item["group_name"] = ""
                            item["group_preferred_shift"] = "AUTO"

                    save_state(state)
                    st.success(
                        f"Die Gruppe „{team_to_remove}“ wurde aufgelöst. "
                        "Die Mitarbeitenden bleiben erhalten."
                    )
                    st.rerun()

                group_rows = []
                for group in existing_group_names:
                    members = [
                        item["name"]
                        for item in state["employees"]
                        if item.get("group_name", "").strip() == group
                    ]
                    orientations = {
                        item.get("group_preferred_shift", "AUTO")
                        for item in state["employees"]
                        if item.get("group_name", "").strip() == group
                    }
                    orientation_label = (
                        {"AUTO": "Automatisch", "F": "Früh", "S": "Spät"}.get(
                            next(iter(orientations)), "Gemischt"
                        )
                        if len(orientations) == 1
                        else "Gemischt"
                    )
                    group_rows.append(
                        {
                            "Gruppe": group,
                            "Mitglieder": ", ".join(members),
                            "Ausrichtung": orientation_label,
                        }
                    )
                st.dataframe(
                    pd.DataFrame(group_rows),
                    width="stretch",
                    hide_index=True,
                )

            remove_name = st.selectbox(
                "Mitarbeiter entfernen",
                options=[""] + [item["name"] for item in state["employees"]],
            )
            if st.button("Ausgewählte Person entfernen", disabled=not remove_name):
                state["employees"] = [
                    item for item in state["employees"] if item["name"] != remove_name
                ]
                state["schedule"].pop(remove_name, None)
                state["availability"].pop(remove_name, None)
                state.get("previous_month_tail", {}).pop(remove_name, None)
                save_state(state)
                st.rerun()

    with tabs[2]:
        st.subheader("Vormonats-Übergabe")

        st.caption(
            "Trage für jede Person die letzten fünf Kalendertage des Vormonats ein. "
            "Der rechte Wert ist der letzte Tag des Vormonats. Frei bedeutet kein Dienst."
        )

        if not state["employees"]:
            st.info("Bitte zuerst Mitarbeitende anlegen.")
        else:
            previous_month_start = date(
                int(state["year"]),
                int(state["month"]),
                1,
            )
            previous_month_last = previous_month_start - timedelta(days=1)
            previous_dates = [
                previous_month_last - timedelta(days=offset)
                for offset in range(4, -1, -1)
            ]

            shift_options = ["", "F", "M", "S", "N"]
            shift_names = {
                "": "Frei",
                "F": "Früh",
                "M": "Mittel",
                "S": "Spät",
                "N": "Nacht",
            }

            for employee_data in state["employees"]:
                employee_name = employee_data["name"]
                st.markdown(f"**{employee_name}**")

                current_sequence = state.get(
                    "previous_month_tail",
                    {},
                ).get(employee_name, ["", "", "", "", ""])

                current_sequence = (
                    [""] * 5 + list(current_sequence)
                )[-5:]

                cols = st.columns(5)
                new_sequence = []

                for index, previous_day in enumerate(previous_dates):
                    with cols[index]:
                        value = st.selectbox(
                            previous_day.strftime("%d.%m."),
                            options=shift_options,
                            index=shift_options.index(
                                current_sequence[index]
                                if current_sequence[index] in shift_options
                                else ""
                            ),
                            format_func=lambda item: shift_names[item],
                            key=(
                                f"previous_month_{employee_name}_"
                                f"{previous_day.isoformat()}"
                            ),
                        )
                        new_sequence.append(value)

                state.setdefault("previous_month_tail", {})[
                    employee_name
                ] = new_sequence

            if st.button("Vormonatsdaten speichern"):
                save_state(state)
                st.success("Vormonatsdaten gespeichert.")

        st.info(
            "Beispiele: Endet der Vormonat mit fünf Arbeitstagen, berücksichtigt "
            "der Planer die nötigen freien Tage am Monatsanfang. Endet er mit N, "
            "bleiben die erforderlichen Folgetage ebenfalls frei."
        )

    with tabs[3]:
        st.subheader("Schichtzeiten und Mindestbesetzung")
        st.info(
            "Fixe Obergrenzen: Früh 4, Mittel 2, Spät 4, Nacht genau 1. "
            "Mitteldienst 0 bedeutet: im gesamten Monat vollständig deaktiviert. "
            "Die automatische Planung überschreitet niemals Soll + 5 Stunden."
        )

        changed = False
        for code in SHIFT_ORDER:
            data = state["shifts"][code]
            st.markdown(f"**{data['name']} ({code})**")
            c1, c2, c3, c4 = st.columns(4)
            start = c1.text_input(f"Beginn {code}", value=data["start"], key=f"start_{code}")
            end = c2.text_input(f"Ende {code}", value=data["end"], key=f"end_{code}")
            hours = c3.number_input(
                f"Stunden {code}",
                min_value=0.5,
                max_value=24.0,
                value=float(data["hours"]),
                step=0.5,
                key=f"hours_{code}",
            )
            minimum = c4.number_input(
                f"Mindestbesetzung {code}",
                min_value=0 if code == "M" else 1,
                max_value=SHIFT_MAXIMUM[code],
                value=int(data["minimum"]),
                step=1,
                disabled=(code == "N"),
                key=f"minimum_{code}",
                help=(
                    "Beim Mitteldienst bedeutet 0: vollständig deaktiviert. "
                    "Dann wird im gesamten Monat kein Mitteldienst automatisch eingeteilt."
                    if code == "M"
                    else None
                ),
            )

            state["shifts"][code]["start"] = start
            state["shifts"][code]["end"] = end
            state["shifts"][code]["hours"] = float(hours)
            state["shifts"][code]["minimum"] = 1 if code == "N" else int(minimum)
            changed = True

        if changed and st.button("Schichteinstellungen speichern"):
            save_state(state)
            st.success("Schichteinstellungen gespeichert.")

    with tabs[4]:
        st.subheader("Sperrtage und Urlaub")
        dates = month_dates(state["year"], state["month"])

        if not state["employees"]:
            st.info("Bitte zuerst Mitarbeitende anlegen.")
        else:
            selected_employee = st.selectbox(
                "Mitarbeiter",
                options=[item["name"] for item in state["employees"]],
                key="availability_employee",
            )

            pending_availability = {}
            cols = st.columns(7)
            for index, d in enumerate(dates):
                with cols[index % 7]:
                    current = get_availability(state, selected_employee, d)
                    label = f"{d.strftime('%a')} {d.day:02d}"
                    choice = st.selectbox(
                        label,
                        options=["", "U", "X"],
                        index=["", "U", "X"].index(current if current in {"U", "X"} else ""),
                        format_func=lambda value: {
                            "": "Verfügbar",
                            "U": "Urlaub",
                            "X": "Gesperrt",
                        }[value],
                        key=f"availability_{selected_employee}_{d.isoformat()}",
                    )
                    pending_availability[d] = choice

            if st.button("Sperrtage speichern"):
                conflicts = [
                    d for d, value in pending_availability.items()
                    if value in {"U", "X"}
                    and get_schedule(state, selected_employee, previous_date(d)) == "N"
                ]

                if conflicts:
                    conflict_text = ", ".join(d.strftime("%d.%m.%Y") for d in conflicts)
                    st.error(
                        "Nicht gespeichert: Am Vortag dieser Urlaub-/Sperrtage ist ein "
                        f"Nachtdienst eingetragen: {conflict_text}. Bitte zuerst den Nachtdienst ändern."
                    )
                else:
                    for d, choice in pending_availability.items():
                        set_availability(state, selected_employee, d, choice)
                    save_state(state)
                    st.success("Sperrtage und Urlaub gespeichert.")

    with tabs[5]:
        st.subheader("Dienstplan")

        if bool(state.get("group_mode", False)):
            st.success(
                "Gruppenmodus aktiv: Der Planer versucht, stabile Früh- und "
                "Spätteams zu bilden."
            )
        else:
            st.info("Normalmodus aktiv.")
        dates = month_dates(state["year"], state["month"])
        employees = [Employee(**item) for item in state["employees"]]

        if not employees:
            st.info("Bitte zuerst Mitarbeitende anlegen.")
        else:
            col1, col2, col3 = st.columns([1, 1, 2])
            with col1:
                if st.button("Dienstplan automatisch erstellen", type="primary"):
                    with st.spinner(
                        "OR-Tools berechnet den Dienstplan. "
                        "Das kann je nach Regeln einige Sekunden dauern..."
                    ):
                        result = solve_schedule_with_fallback(
                            state,
                            time_limit_seconds=45,
                        )

                    if result.get("ok"):
                        save_state(state)

                        if result.get("planning_mode") == "HEURISTIC_FALLBACK":
                            st.warning(result["message"])
                        elif result.get("relaxed_min_hours"):
                            st.warning(result["message"])
                        else:
                            st.success(result["message"])

                        st.rerun()
                    else:
                        st.error(result["message"])

            with col2:
                if st.button("Alle Dienste löschen"):
                    state["schedule"] = {emp.name: {} for emp in employees}
                    save_state(state)
                    st.rerun()

            if not ORTOOLS_AVAILABLE:
                st.error(
                    "Google OR-Tools fehlt. Installiere es im Terminal mit: "
                    "python3 -m pip install ortools"
                )
            else:
                st.caption(
                    "Automatische Planung: Google OR-Tools CP-SAT. "
                    "Manuelle Änderungen bleiben weiterhin möglich."
                )

            st.caption(
                "Manuelle Änderungen: Mitarbeiter und Tag auswählen, danach Dienst setzen."
            )

            c1, c2, c3 = st.columns(3)
            selected_employee = c1.selectbox(
                "Mitarbeiter",
                options=[emp.name for emp in employees],
                key="manual_employee",
            )
            selected_date = c2.selectbox(
                "Tag",
                options=dates,
                format_func=lambda d: d.strftime("%a, %d.%m.%Y"),
                key="manual_date",
            )
            current_shift = get_schedule(state, selected_employee, selected_date)
            selected_shift = c3.selectbox(
                "Dienst",
                options=["", "F", "M", "S", "N"],
                index=["", "F", "M", "S", "N"].index(
                    current_shift if current_shift in {"", "F", "M", "S", "N"} else ""
                ),
                format_func=lambda value: SHIFT_LABELS[value],
                key="manual_shift",
            )

            if st.button("Dienst übernehmen"):
                set_schedule(
                    state,
                    selected_employee,
                    selected_date,
                    selected_shift,
                )
                save_state(state)
                st.success(
                    "Dienst manuell gespeichert. Mögliche Regelverletzungen "
                    "werden im Reiter „Prüfung“ angezeigt."
                )
                st.rerun()

            summary_df = render_summary(state, employees, dates)
            st.markdown("### Stundenübersicht")
            st.dataframe(
                summary_df.style.format(
                    {
                        "Wochenstunden": "{:.1f}",
                        "Soll": "{:.1f}",
                        "Ist": "{:.1f}",
                        "Differenz": "{:+.1f}",
                    }
                ),
                width="stretch",
                hide_index=True,
            )

            calendar_df = render_calendar_dataframe(state, employees, dates)
            st.markdown("### Monatsübersicht")
            st.markdown(
                render_calendar_html(state, employees, dates),
                unsafe_allow_html=True,
            )

            csv = calendar_df.to_csv(index=False, sep=";").encode("utf-8-sig")

            download_col1, download_col2 = st.columns(2)

            with download_col1:
                st.download_button(
                    "Dienstplan als CSV herunterladen",
                    data=csv,
                    file_name=(
                        f"Dienstplan_{state['year']}_"
                        f"{state['month']:02d}.csv"
                    ),
                    mime="text/csv",
                    help=(
                        "CSV enthält die Werte, kann aber technisch keine "
                        "Farben oder Zellformatierungen speichern."
                    ),
                )

            with download_col2:
                try:
                    excel_data = build_excel_export(
                        state,
                        employees,
                        dates,
                    )
                    st.download_button(
                        "Dienstplan als Excel herunterladen",
                        data=excel_data,
                        file_name=(
                            f"Dienstplan_{state['year']}_"
                            f"{state['month']:02d}.xlsx"
                        ),
                        mime=(
                            "application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet"
                        ),
                        help=(
                            "Excel enthält Farben, Dienstkürzel, Stunden, "
                            "Wochenenden, Soll/Ist/Differenz und Tagessummen."
                        ),
                    )
                except ModuleNotFoundError:
                    st.error(
                        "Für den formatierten Excel-Export fehlt XlsxWriter. "
                        "Installiere es mit: python3 -m pip install XlsxWriter"
                    )

    with tabs[6]:
        st.subheader("Regelprüfung")
        dates = month_dates(state["year"], state["month"])
        employees = [Employee(**item) for item in state["employees"]]

        if not employees:
            st.info("Noch keine Mitarbeitenden vorhanden.")
        else:
            warnings = validate_schedule(state, employees, dates)

            action_col1, action_col2 = st.columns([1, 2])

            with action_col1:
                optimize_clicked = st.button(
                    "Plan mit OR-Tools neu optimieren",
                    type="primary",
                    disabled=not bool(warnings),
                    help=(
                        "Führt mehrere Reparaturrunden aus. Eine Runde wird nur "
                        "übernommen, wenn die gewichtete Fehlerbewertung besser wird."
                    ),
                )

            with action_col2:
                st.caption(
                    "Der Optimierer versucht Besetzung, Stunden, Ruhezeiten, "
                    "Senior-Abdeckung, freie Wochenenden und Arbeitsblöcke zu "
                    "verbessern. Widersprüchliche Regeln oder zu wenig Personal "
                    "können trotzdem Restfehler verursachen."
                )

            if optimize_clicked:
                with st.spinner(
                    "OR-Tools berechnet den Plan vollständig neu..."
                ):
                    result = solve_schedule_with_fallback(
                        state,
                        time_limit_seconds=60,
                    )

                if result.get("ok"):
                    save_state(state)

                    if result.get("planning_mode") == "HEURISTIC_FALLBACK":
                        st.warning(result["message"])
                    elif result.get("relaxed_min_hours"):
                        st.warning(result["message"])
                    else:
                        st.success(result["message"])

                    st.rerun()
                else:
                    st.error(result["message"])

            if not warnings:
                st.success("Keine Regelverletzungen gefunden.")
            else:
                st.warning(f"{len(warnings)} Hinweise oder Regelverletzungen gefunden:")
                for warning in warnings:
                    st.write(f"• {warning}")

    st.divider()
    if st.button("Gesamten Stand speichern"):
        save_state(state)
        st.success(f"Gespeichert in {DATA_FILE.name}")


if __name__ == "__main__":
    main()
