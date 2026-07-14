from __future__ import annotations

import copy
import random
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

try:
    from ortools.sat.python import cp_model
    ORTOOLS_AVAILABLE = True
except ImportError:
    cp_model = None
    ORTOOLS_AVAILABLE = False

from config import SHIFT_LABELS, SHIFT_MAXIMUM, SHIFT_ORDER
from models import Employee
from data_store import *

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


def shift_new_and_qualified_counts(
    state: dict,
    employees: List[Employee],
    d: date,
    shift: str,
) -> Tuple[int, int]:
    """
    Gibt (Anzahl Neue, Anzahl Qualifizierte) für eine Schicht zurück.

    Als qualifiziert gelten:
    - Senior
    - Erfahren

    Es muss immer gelten:
        Anzahl Neue <= Anzahl Qualifizierte
    """
    staff = shift_staff(state, employees, d, shift)

    new_count = sum(
        1 for member in staff if member.new_employee
    )
    qualified_count = sum(
        1 for member in staff if is_day_qualified(member)
    )

    return new_count, qualified_count


def new_employee_coverage_is_valid(
    state: dict,
    employees: List[Employee],
    d: date,
    shift: str,
) -> bool:
    if shift not in {"F", "M", "S"}:
        return True

    new_count, qualified_count = shift_new_and_qualified_counts(
        state,
        employees,
        d,
        shift,
    )
    return new_count <= qualified_count

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
                existing_new_count = sum(
                    1 for member in existing_staff
                    if member.new_employee
                )
                existing_qualified_count = sum(
                    1 for member in existing_staff
                    if is_day_qualified(member)
                )

                needs_qualified_for_new = (
                    existing_new_count > existing_qualified_count
                )

                needs_qualified_coverage = (
                    shift in {"F", "S"}
                    and not any(
                        is_day_qualified(member)
                        for member in existing_staff
                    )
                )

                qualified_candidates = [
                    emp for emp in candidates
                    if is_day_qualified(emp)
                ]

                if (
                    needs_qualified_for_new
                    and qualified_candidates
                ):
                    scoring_pool = qualified_candidates
                elif (
                    needs_qualified_coverage
                    and qualified_candidates
                ):
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

                    # Für jede neue Person muss mindestens eine gleichzeitige
                    # qualifizierte Person (Senior oder Erfahren) vorhanden sein.
                    projected_new_count = existing_new_count + 1
                    projected_qualified_count = existing_qualified_count

                    if projected_new_count <= projected_qualified_count:
                        filtered_pool.append(candidate)

                # Falls Neue aktuell nicht regelkonform ergänzt werden können,
                # werden nur die übrigen Kandidaten verwendet.
                if filtered_pool:
                    scoring_pool = filtered_pool
                else:
                    scoring_pool = [
                        candidate
                        for candidate in scoring_pool
                        if not candidate.new_employee
                    ]

                if not scoring_pool:
                    break

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
    Stellt für Früh-, Mittel- und Spätdienste sicher:

        Anzahl Neue <= Anzahl Senior + Anzahl Erfahren

    Beispiele:
    - Senior + Neu: erlaubt
    - Erfahren + Neu: erlaubt
    - Senior + Neu + Neu: nicht erlaubt
    - Senior + Erfahren + Neu + Neu: erlaubt

    Wenn qualifizierte Personen ergänzt werden können, werden sie hinzugefügt.
    Andernfalls werden überzählige neue Mitarbeitende aus der Schicht entfernt.
    """
    for d in dates:
        for shift in ("F", "M", "S"):
            while True:
                new_count, qualified_count = shift_new_and_qualified_counts(
                    state,
                    employees,
                    d,
                    shift,
                )

                if new_count <= qualified_count:
                    break

                qualified_candidates = [
                    candidate
                    for candidate in employees
                    if (
                        is_day_qualified(candidate)
                        and get_schedule(
                            state,
                            candidate.name,
                            d,
                        ) not in SHIFT_ORDER
                        and not would_break_hard_rules(
                            state,
                            candidate,
                            d,
                            shift,
                            dates,
                            enforce_hour_limit=True,
                        )
                    )
                ]

                if (
                    qualified_candidates
                    and len(
                        shift_staff(
                            state,
                            employees,
                            d,
                            shift,
                        )
                    ) < SHIFT_MAXIMUM[shift]
                ):
                    qualified_candidates.sort(
                        key=lambda candidate: (
                            calculate_hours(
                                state,
                                candidate.name,
                                dates,
                            )
                            / max(
                                float(candidate.monthly_target),
                                1.0,
                            )
                        )
                    )
                    set_schedule(
                        state,
                        qualified_candidates[0].name,
                        d,
                        shift,
                    )
                    continue

                new_staff = [
                    member
                    for member in shift_staff(
                        state,
                        employees,
                        d,
                        shift,
                    )
                    if member.new_employee
                ]

                if not new_staff:
                    break

                # Zuerst jene neue Person entfernen, die bereits am besten
                # mit Stunden versorgt ist.
                new_staff.sort(
                    key=lambda member: calculate_hours(
                        state,
                        member.name,
                        dates,
                    )
                    - float(member.monthly_target),
                    reverse=True,
                )
                set_schedule(
                    state,
                    new_staff[0].name,
                    d,
                    "",
                )

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
            ):
                new_count, qualified_count = (
                    shift_new_and_qualified_counts(
                        state,
                        employees,
                        d,
                        shift,
                    )
                )

                if new_count > qualified_count:
                    warnings.append(
                        f"{emp.name}, {d.strftime('%d.%m.%Y')}: "
                        f"{new_count} neue Person(en), aber nur "
                        f"{qualified_count} Senior/Erfahrene in "
                        f"{SHIFT_LABELS[shift]}."
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
    - Neue höchstens im Verhältnis 1:1 mit Senior/Erfahren; Neu nie im Nachtdienst
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
    new_employee_indices = [
        p
        for p, emp in enumerate(employees)
        if emp.new_employee
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

        # Betreuungsregel für neue Mitarbeitende:
        # Anzahl Neue darf die Anzahl Senior + Erfahren nicht überschreiten.
        for shift in ("F", "M", "S"):
            new_same_shift = sum(
                x[p, d, shift_index[shift]]
                for p in new_employee_indices
            )
            qualified_same_shift = sum(
                x[p, d, shift_index[shift]]
                for p in qualified_indices
            )

            model.Add(
                new_same_shift <= qualified_same_shift
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

    # Neue Mitarbeitende benötigen mindestens gleich viele Senior/Erfahrene.
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

