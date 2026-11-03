"""Pure public vehicle field definitions shared by producer and catalog queries."""
import re
from decimal import Decimal, InvalidOperation

ENERGY_TYPES = {"fuel": "燃油", "hybrid": "新能源混动", "range_extended": "新能源增程", "electric": "新能源纯电"}
SERIES_OPTIONS = ("国产新能源", "合资新能源", "国产车", "德系车", "日系车", "美系车", "韩系车", "法系车", "其他")
DRIVE_TYPES = {"front_wheel_drive": "前驱", "rear_wheel_drive": "后驱", "four_wheel_drive": "四驱"}
BATTERY_DECIMAL_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")

def canonical_enum(field, value):
    options = {"energy_type": ENERGY_TYPES, "drive_type": DRIVE_TYPES, "series": {v: v for v in SERIES_OPTIONS}}
    if field not in options:
        return value
    for key, label in options[field].items():
        if value in (key, label):
            return key
    raise ValueError("unsupported_vehicle_enum")


def numeric_field(field, value):
    if field == "mileage_km":
        return value if type(value) is int and 0 <= value <= 10_000_000 else None
    if field == "battery_capacity_kwh" and isinstance(value, str) and BATTERY_DECIMAL_RE.fullmatch(value):
        try:
            result = Decimal(value)
            return result if result.is_finite() and result > 0 else None
        except InvalidOperation:
            pass
    # Displacement is deliberately free text (e.g. 1.5T), never a comparable number.
    return None
