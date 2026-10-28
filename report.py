"""Shared report-building workflow used by the CLI and the web interface."""
from calculations import assess_measurements, compute_metrics
from measurements import read_measurements
from pdf_report import make_pdf


def build_report(csv_path, output, input_name, max_voltage=None, max_abs_current=None):
    """Validate the CSV, write the PDF to output, and return the metrics table."""
    rows = read_measurements(csv_path)
    metrics = compute_metrics(rows)
    metrics['Test assessment'] = assess_measurements(rows, max_voltage, max_abs_current)
    if max_voltage is not None:
        metrics['Maximum voltage limit (V)'] = f'{max_voltage:.2f}'
    if max_abs_current is not None:
        metrics['Maximum absolute current limit (A)'] = f'{max_abs_current:.2f}'
    make_pdf(rows, metrics, output, input_name)
    return metrics
