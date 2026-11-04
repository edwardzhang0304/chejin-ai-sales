"""Pure public vehicle field definitions shared by producer and catalog queries."""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

ENERGY_TYPES = {"fuel": "燃油", "hybrid": "新能源混动", "range_extended": "新能源增程", "electric": "新能源纯电"}
SERIES_OPTIONS = ("国产新能源", "合资新能源", "国产车", "德系车", "日系车", "美系车", "韩系车", "法系车", "其他")
DRIVE_TYPES = {"front_wheel_drive": "前驱", "rear_wheel_drive": "后驱", "four_wheel_drive": "四驱"}
BATTERY_DECIMAL_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")

# The public projection is an allowlist, including for imported/older rows.
# Never send additional_details wholesale to a model.
PUBLIC_DETAIL_FIELDS = (
    "brand", "series", "model", "energy_type", "displacement",
    "battery_capacity_kwh", "drive_type", "first_registration", "mileage_km",
    "exterior_color", "interior_color", "location", "customer_description",
)
PUBLIC_NUMERIC_UNITS = {"price": "万元", "stock": "台", "mileage_km": "公里",
                        "battery_capacity_kwh": "kWh"}


def public_vehicle_details(data):
    details = data.get("additional_details")
    if not isinstance(details, dict):
        return {}
    return {key: details[key] for key in PUBLIC_DETAIL_FIELDS
            if key in details and type(details[key]) in (str, int, float)
            and details[key] != ""}


_DIGITS = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_NUMBER = r"[0-9零〇一二两三四五六七八九十百千万亿点.]+"
_UNIT = r"万公里|万千米|万元|万块|公里|千米|kWh|千瓦时|小时|个月|元|块|万|台|辆|款|年|月|天|日|%|[LT]"
_NUMERIC_EXPRESSION = re.compile(rf"(?P<number>{_NUMBER})(?:\s*(?P<unit>{_UNIT}))?", re.I)
_UNIT_VALUE = {
    "元": ("万元", Decimal("0.0001")), "块": ("万元", Decimal("0.0001")),
    "万元": ("万元", Decimal(1)), "万块": ("万元", Decimal(1)), "万": ("万元", Decimal(1)),
    "公里": ("公里", Decimal(1)), "千米": ("公里", Decimal(1)),
    "万公里": ("公里", Decimal(10000)), "万千米": ("公里", Decimal(10000)),
    "kwh": ("kWh", Decimal(1)), "千瓦时": ("kWh", Decimal(1)),
    "台": ("台", Decimal(1)), "辆": ("台", Decimal(1)), "款": ("台", Decimal(1)),
    "年": ("年", Decimal(1)), "月": ("月", Decimal(1)),
    "个月": ("月", Decimal(1)), "天": ("天", Decimal(1)), "日": ("天", Decimal(1)),
    "小时": ("小时", Decimal(1)), "%": ("%", Decimal(1)),
    "l": ("L", Decimal(1)), "t": ("T", Decimal(1)),
}


def public_number(value):
    """Normalize a factual number, not customer intent or a budget policy."""
    text = unicodedata.normalize("NFKC", str(value)).replace("点", ".")
    try:
        result = Decimal(text)
        return result if result.is_finite() else None
    except InvalidOperation:
        pass
    if text.count(".") > 1:
        return None
    integer, _, fraction = text.partition(".")
    if any(c not in _DIGITS for c in fraction):
        return None
    total = section = current = 0
    for c in integer:
        if c in _DIGITS:
            current = current * 10 + _DIGITS[c]
        elif c in "十百千":
            section += (current or 1) * {"十": 10, "百": 100, "千": 1000}[c]
            current = 0
        elif c in "万亿":
            total += (section + current) * {"万": 10000, "亿": 100000000}[c]
            section = current = 0
        else:
            return None
    value = Decimal(total + section + current)
    if fraction:
        value += Decimal("0." + "".join(str(_DIGITS[c]) for c in fraction))
    return value


def factual_number_spans(text, *, excluded=()):
    """Each occurrence is independent; offsets refer to the original text.

    NFKC is applied per character so full-width characters retain their source
    position. Unknown units remain visible errors, never silently disappear.
    """
    normalized, positions = [], []
    for index, c in enumerate(text):
        for piece in unicodedata.normalize("NFKC", c):
            normalized.append(piece)
            positions.append(index)
    normalized = "".join(normalized)
    spans = []
    matches = list(_NUMERIC_EXPRESSION.finditer(normalized))
    for position, match in enumerate(matches):
        start, end = positions[match.start()], positions[match.end() - 1] + 1
        if any(a <= start and end <= b for a, b in excluded):
            continue
        number, unit = match.group("number"), match.group("unit")
        if number == "一" and normalized[max(0, match.start()-1):match.start()] in {"哪", "某"}:
            continue  # 哪一年/某一天 asks/refers to an unspecified date, not 1 year/day.
        # 万 belongs to the currency unit in an isolated public quote, while
        # 五万二千元 must retain the multiplier in the number itself.
        if unit in (None, "元", "块") and number.endswith("万"):
            number, unit = number[:-1], "万" + (unit or "")
        elif unit in ("公里", "千米") and number.endswith("万"):
            number, unit = number[:-1], "万" + unit
        if not unit and position + 1 < len(matches):
            following = matches[position + 1]
            between = normalized[match.end():following.start()].strip()
            if between in ("到", "至", "-", "—", "～", "~", "–"):
                unit = following.group("unit")
                if following.group("number").endswith("万") and unit in (None, "元", "块", "公里", "千米"):
                    unit = "万" + (unit or "")
        converted = _UNIT_VALUE.get((unit or "").lower())
        value = public_number(number)
        # Chinese single characters without a unit are ordinary prose (一起,
        # 一下). Arabic bare numbers are ambiguous factual expressions.
        if not unit and not re.search(r"[0-9]", number):
            continue
        spans.append({"start": start, "end": end, "text": text[start:end],
                      "unit": converted[0] if converted else None,
                      "value": str(value * converted[1]) if value is not None and converted else None})
    return spans

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
