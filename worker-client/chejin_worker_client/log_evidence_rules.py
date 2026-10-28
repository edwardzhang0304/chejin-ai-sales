"""Pure log evidence policy shared by writers and read-only collectors."""


def normalize_log_level(level):
    value = str(level or "").strip().upper()
    return "WARNING" if value == "WARN" else value


def is_fault_evidence(level, error_code=None, *, force_incident=False):
    return bool(force_incident or error_code
                or normalize_log_level(level) in {"WARNING", "ERROR", "CRITICAL"})


def stored_fault_evidence(level, error_code, metadata):
    # incident_id also preserves evidence from older forced-incident writers.
    metadata = metadata if isinstance(metadata, dict) else {}
    return is_fault_evidence(level, error_code, force_incident=(
        metadata.get("force_incident") is True or bool(metadata.get("incident_id"))))
