# Dienstplaner

Aufgeteilte Version der Streamlit-Dienstplan-App.

## Dateien

- `app.py` – Streamlit-Oberfläche und Einstiegspunkt
- `config.py` – Schichtkonfiguration und Konstanten
- `models.py` – Datenmodell für Mitarbeitende
- `data_store.py` – Speichern, Laden und Kalender-Hilfsfunktionen
- `planner.py` – Regeln, Heuristik und OR-Tools-Planung
- `views.py` – Tabellen-, HTML- und Excel-Export

## Lokal starten

```bash
python3 -m pip install -r requirements.txt
python3 -m streamlit run app.py
```

## Streamlit Community Cloud

1. Alle Dateien in ein GitHub-Repository hochladen.
2. In Streamlit Community Cloud das Repository verbinden.
3. Als Main file path `app.py` auswählen.

## Hinweis zur Speicherung

Die App speichert Daten derzeit in `dienstplan_daten.json`. Auf kostenlosen Cloud-Deployments kann lokaler Dateispeicher bei Neustarts verloren gehen. Für einen echten Mehrbenutzerbetrieb sollte später SQLite/PostgreSQL oder ein anderer dauerhafter Speicher verwendet werden.
