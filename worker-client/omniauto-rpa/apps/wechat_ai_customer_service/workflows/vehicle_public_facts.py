"""Read-only projection of saved, customer-visible CheJin vehicle details."""
from typing import Any


_PUBLIC_DETAIL_LABELS = {
    "first_registration": "首次上牌（上牌日期，非年款）",
    "mileage_km": "表显里程（公里）",
    "exterior_color": "车身颜色",
    "interior_color": "内饰颜色",
    "location": "车辆所在地",
    "customer_description": "车辆描述",
}


def vehicle_specs_for_evidence(item: dict[str, Any]) -> str:
    """Enrich the existing specs contract without rewriting saved Product Master rows.

    Only the six declared public fields cross this boundary. Never copy the
    additional_details object wholesale: old/imported rows may hold private keys.
    """
    data = item.get("data") or {}
    specs = str(data.get("specs") or "")
    if (item.get("source") or {}).get("type") != "chejin_backend":
        return specs
    details = data.get("additional_details")
    if not isinstance(details, dict):
        return specs
    parts = [specs] if specs else []
    for key, label in _PUBLIC_DETAIL_LABELS.items():
        value = details.get(key)
        if key == "mileage_km":
            if type(value) is not int or value < 0:
                continue
        elif not isinstance(value, str) or not value.strip():
            continue
        parts.append(f"{label}：{value}")
    return " / ".join(parts)
