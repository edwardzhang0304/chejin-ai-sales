"""Finite registered entities for this customer; no inferred entity coverage."""
import unicodedata

from app.models.lead import Lead
from app.models.sales import Sales
from app.services.vehicle_service import _vehicle_query


def build_context(db, binding):
    entities: set[tuple[str, str]] = set()

    def add(kind, value):
        if isinstance(value, str):
            normalized = unicodedata.normalize("NFKC", value.strip()).casefold().strip()
            if normalized:
                entities.add((kind, normalized))

    lead = db.get(Lead, binding.lead_id)
    sales = db.get(Sales, binding.sales_id)
    if lead:
        add("person", lead.customer_name)
    if sales:
        add("person", sales.sales_name)
    # Includes inactive records in the existing tenant/category query: a
    # historical mention remains protected after a vehicle goes off sale.
    for item in db.scalars(_vehicle_query()):
        payload = item.payload if isinstance(item.payload, dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        add("vehicle", data.get("name"))
        aliases = data.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                add("vehicle", alias)
        details = data.get("additional_details")
        if isinstance(details, dict):
            for key in ("brand", "series", "model"):
                add("vehicle", details.get(key))
            add("location", details.get("location"))
    return {"version": 1, "known_entities": [
        {"kind": kind, "value": value} for kind, value in sorted(entities)
    ]}
