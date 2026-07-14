from pathlib import Path

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
