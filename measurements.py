"""Read and validate battery measurements from CSV files."""
import csv
from datetime import datetime
import math

COLUMNS = ('timestamp', 'voltage_v', 'current_a', 'soc_pct')


def read_measurements(path):
    with open(path, newline='', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            raise ValueError('CSV must have a header row')
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError('CSV contains duplicate column names')
        missing_columns = [column for column in COLUMNS if column not in reader.fieldnames]
        if missing_columns:
            raise ValueError('CSV needs columns: ' + ', '.join(missing_columns))
        rows = []
        timezone_aware = None
        for line, item in enumerate(reader, 2):
            try:
                if None in item:
                    raise ValueError('row has more values than the header')
                if any(not item.get(column) or not item[column].strip() for column in COLUMNS):
                    raise ValueError('required values must not be empty')
                time = datetime.fromisoformat(item['timestamp'].replace('Z', '+00:00'))
                is_timezone_aware = time.utcoffset() is not None
                if timezone_aware is None:
                    timezone_aware = is_timezone_aware
                elif timezone_aware != is_timezone_aware:
                    raise ValueError('timestamps must consistently include or omit a timezone')
                voltage = float(item['voltage_v'])
                current = float(item['current_a'])
                soc = float(item['soc_pct'])
                if not all(math.isfinite(value) for value in (voltage, current, soc)):
                    raise ValueError('voltage, current, and SOC must be finite numbers')
                if voltage <= 0 or not 0 <= soc <= 100:
                    raise ValueError('voltage must be >0 and SOC must be 0 to 100')
                rows.append((time, voltage, current, soc))
            except (ValueError, TypeError) as exc:
                raise ValueError(f'Invalid CSV line {line}: {exc}') from exc
    if len(rows) < 2:
        raise ValueError('At least two rows with strictly increasing timestamps are required')
    try:
        timestamps_increase = all(b[0] > a[0] for a, b in zip(rows, rows[1:]))
    except TypeError as exc:
        raise ValueError('Timestamps must consistently include or omit a timezone') from exc
    if not timestamps_increase:
        raise ValueError('At least two rows with strictly increasing timestamps are required')
    return rows
