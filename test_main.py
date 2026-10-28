import csv
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from calculations import assess_measurements, compute_metrics
from measurements import read_measurements


class ReadMeasurementsTests(unittest.TestCase):
    def read_csv(self, header, rows):
        temporary_file = tempfile.NamedTemporaryFile(
            mode='w', newline='', encoding='utf-8', suffix='.csv', delete=False
        )
        self.addCleanup(Path(temporary_file.name).unlink)
        with temporary_file:
            writer = csv.writer(temporary_file)
            writer.writerow(header)
            writer.writerows(rows)
        return read_measurements(temporary_file.name)

    def test_reads_required_columns_and_ignores_extra_columns(self):
        rows = self.read_csv(
            ['timestamp', 'voltage_v', 'current_a', 'soc_pct', 'status'],
            [
                ['2026-01-01T00:00:00', '48', '-2', '20', 'RUNNING'],
                ['2026-01-01T00:00:01', '49', '3', '21', 'RUNNING'],
            ],
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1:], (48.0, -2.0, 20.0))

    def test_rejects_missing_columns(self):
        with self.assertRaisesRegex(ValueError, 'soc_pct'):
            self.read_csv(
                ['timestamp', 'voltage_v', 'current_a'],
                [['2026-01-01T00:00:00', '48', '1']],
            )

    def test_rejects_missing_values_and_non_numeric_values(self):
        for value in ('', 'invalid'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'line 3'):
                self.read_csv(
                    ['timestamp', 'voltage_v', 'current_a', 'soc_pct'],
                    [
                        ['2026-01-01T00:00:00', '48', '1', '20'],
                        ['2026-01-01T00:00:01', value, '1', '21'],
                    ],
                )

    def test_rejects_non_finite_values(self):
        for value in ('nan', 'inf', '-inf'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'finite'):
                self.read_csv(
                    ['timestamp', 'voltage_v', 'current_a', 'soc_pct'],
                    [
                        ['2026-01-01T00:00:00', '48', '1', '20'],
                        ['2026-01-01T00:00:01', '48', value, '21'],
                    ],
                )

    def test_rejects_invalid_or_mixed_timezone_timestamps(self):
        invalid_rows = [
            ['2026-01-01T00:00:00', '48', '1', '20'],
            ['not-a-timestamp', '48', '1', '21'],
        ]
        with self.assertRaisesRegex(ValueError, 'line 3'):
            self.read_csv(
                ['timestamp', 'voltage_v', 'current_a', 'soc_pct'], invalid_rows
            )

        mixed_timezone_rows = [
            ['2026-01-01T00:00:00', '48', '1', '20'],
            ['2026-01-01T00:00:01+00:00', '48', '1', '21'],
        ]
        with self.assertRaisesRegex(ValueError, 'consistently'):
            self.read_csv(
                ['timestamp', 'voltage_v', 'current_a', 'soc_pct'],
                mixed_timezone_rows,
            )

    def test_rejects_non_increasing_timestamps(self):
        with self.assertRaisesRegex(ValueError, 'strictly increasing'):
            self.read_csv(
                ['timestamp', 'voltage_v', 'current_a', 'soc_pct'],
                [
                    ['2026-01-01T00:00:01', '48', '1', '20'],
                    ['2026-01-01T00:00:01', '48', '1', '21'],
                ],
            )


class EngineeringCalculationTests(unittest.TestCase):
    def setUp(self):
        start = datetime(2026, 1, 1)
        self.rows = [
            (start, 10.0, -2.0, 20.0),
            (start + timedelta(hours=1), 10.0, 2.0, 30.0),
        ]

    def test_integrates_charge_and_discharge_across_power_sign_change(self):
        metrics = compute_metrics(self.rows)

        self.assertEqual(metrics['Duration (h)'], '1.00')
        self.assertEqual(metrics['Charge energy (kWh)'], '0.01')
        self.assertEqual(metrics['Discharge energy (kWh)'], '0.01')
        self.assertEqual(metrics['Round-trip efficiency (%)'], '100.00')

    def test_passes_when_measurements_are_at_or_below_limits(self):
        self.assertEqual(assess_measurements(self.rows, 10, 2), 'PASS')

    def test_fails_when_voltage_or_absolute_current_exceeds_limit(self):
        self.assertEqual(assess_measurements(self.rows, 9.9, 2), 'FAIL')
        self.assertEqual(assess_measurements(self.rows, 10, 1.9), 'FAIL')

    def test_reports_partial_and_unassessed_limits(self):
        self.assertEqual(assess_measurements(self.rows), 'NOT ASSESSED')
        self.assertEqual(
            assess_measurements(self.rows, max_voltage=10), 'PARTIALLY ASSESSED'
        )

    def test_rejects_invalid_assessment_limits(self):
        with self.assertRaisesRegex(ValueError, 'greater than zero'):
            assess_measurements(self.rows, max_voltage=float('inf'))
        with self.assertRaisesRegex(ValueError, 'greater than zero'):
            assess_measurements(self.rows, max_abs_current=0)


if __name__ == '__main__':
    unittest.main()
