# Eng_Report_Generator

1. Run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py ./Sample_Data/sample_battery_test.csv \
  --max-voltage 60 --max-abs-current 40 -o engineering_report.pdf
open engineering_report.pdf
```

Use `python main.py your_file.csv -o my_report.pdf` for your own test data.
The optional `--max-voltage` and `--max-abs-current` limits are used to assess
the test: exceeding either supplied limit results in `FAIL`; otherwise the
assessment is `PASS` when both limits are supplied, or `PARTIALLY ASSESSED`
when only one limit is supplied. The current limit applies to both charging
and discharging. If neither limit is supplied, the report says `NOT ASSESSED`.
The example limits above are 60 V and 40 A; set limits appropriate to your
equipment before relying on an assessment.

Simple web interface (runs locally, no extra dependencies):

```bash
python webapp.py
```

Then open http://127.0.0.1:8000, choose a CSV, optionally enter the voltage and
current limits, and download the generated PDF. Use `--port` to change the port.

Run the automated tests with:

```bash
python -m unittest
```

Project modules:

- `main.py` handles command-line arguments and coordinates report generation.
- `measurements.py` reads and validates the input CSV.
- `calculations.py` computes engineering metrics and the pass/fail assessment.
- `pdf_report.py` builds the PDF and charts.
- `report.py` runs the shared validate → calculate → assess → PDF workflow.
- `webapp.py` and `static/index.html` provide the local web interface.

Please note all CSV files are fake and AI generated for study purposes lol

The CSV requires strictly increasing ISO timestamps and finite numeric
voltage/current/SOC values. Voltage must be above zero and SOC between 0 and
100. Timestamps must either all include or all omit a timezone. Additional
columns are ignored.

The CSV requires `timestamp,voltage_v,current_a,soc_pct` with ascending ISO timestamps, voltage in V, current in A, and SOC between 0 and 100. Negative current is charging and positive is discharging. Complete comparable charge/discharge cycles are needed for meaningful efficiency.
