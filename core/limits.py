"""Validation shared by local and cloud report submissions."""
import math


def optional_limit(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('Limits must be finite numbers greater than zero')
    try:
        limit = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError('Limits must be finite numbers greater than zero') from exc
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError('Limits must be finite numbers greater than zero')
    return limit
