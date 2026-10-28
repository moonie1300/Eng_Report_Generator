"""Engineering metrics and limit-based battery test assessment."""
import math
from statistics import mean


def assess_measurements(rows, max_voltage=None, max_abs_current=None):
    limits = (max_voltage, max_abs_current)
    if any(limit is not None and (not math.isfinite(limit) or limit <= 0) for limit in limits):
        raise ValueError('Assessment limits must be finite numbers greater than zero')
    if max_voltage is None and max_abs_current is None:
        return 'NOT ASSESSED'
    if max_voltage is not None and max(row[1] for row in rows) > max_voltage:
        return 'FAIL'
    if max_abs_current is not None and max(abs(row[2]) for row in rows) > max_abs_current:
        return 'FAIL'
    if max_voltage is None or max_abs_current is None:
        return 'PARTIALLY ASSESSED'
    return 'PASS'


def compute_metrics(rows):
    charge = discharge = 0.0
    for a, b in zip(rows, rows[1:]):
        hours = (b[0] - a[0]).total_seconds() / 3600
        p0, p1 = a[1]*a[2]/1000, b[1]*b[2]/1000
        segments = [(p0, p1, hours)]
        if p0 * p1 < 0:  # Split crossing so energy signs do not cancel.
            fraction = abs(p0)/(abs(p0)+abs(p1))
            segments = [(p0, 0, hours*fraction), (0, p1, hours*(1-fraction))]
        for start, end, length in segments:
            energy = (start + end)/2 * length
            if energy < 0:
                charge -= energy
            else:
                discharge += energy
    volts = [row[1] for row in rows]
    amps = [row[2] for row in rows]
    power = [voltage*current/1000 for _, voltage, current, _ in rows]
    return {
        'Duration (h)': f'{(rows[-1][0]-rows[0][0]).total_seconds()/3600:.2f}',
        'Samples': str(len(rows)),
        'Charge energy (kWh)': f'{charge:.2f}',
        'Discharge energy (kWh)': f'{discharge:.2f}',
        'Round-trip efficiency (%)': f'{100*discharge/charge:.2f}' if charge else 'N/A',
        'Voltage min / avg / max (V)': f'{min(volts):.2f} / {mean(volts):.2f} / {max(volts):.2f}',
        'Current min / max (A)': f'{min(amps):.2f} / {max(amps):.2f}',
        'Peak charge / discharge (kW)': f'{max(0,-min(power)):.2f} / {max(0,max(power)):.2f}',
        'SOC start / end (%)': f'{rows[0][3]:.1f} / {rows[-1][3]:.1f}',
    }
