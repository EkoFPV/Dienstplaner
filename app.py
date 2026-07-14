"""Streamlit-Einstiegspunkt für den Dienstplaner.

Start:
    python3 -m streamlit run app.py
"""
from __future__ import annotations

import calendar
from dataclasses import asdict
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from config import *
from models import Employee
from data_store import *
from planner import *
from views import *

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
