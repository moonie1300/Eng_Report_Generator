# Eng_Report_Generator

1. Run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py sample_battery_test.csv -o engineering_report.pdf
open engineering_report.pdf
```

Use `python generate_report.py your_file.csv -o my_report.pdf` for your own test data.

The CSV requires `timestamp,voltage_v,current_a,soc_pct` with ascending ISO timestamps, voltage in V, current in A, and SOC between 0 and 100. Negative current is charging and positive is discharging. Complete comparable charge/discharge cycles are needed for meaningful efficiency.
