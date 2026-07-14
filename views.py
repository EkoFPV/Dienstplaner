from __future__ import annotations

import calendar
import html
import io
from datetime import date
from typing import List

import pandas as pd

from config import SHIFT_LABELS, SHIFT_MAXIMUM, SHIFT_ORDER
from models import Employee, employee_status_label
from data_store import *
from planner import shift_staff

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

