"""PE-01 evidence-only contract. No business permission or recovery decisions."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Policy:
    sample_seconds: float = 15
    retention_seconds: float = 72 * 3600
    segment_bytes: int = 1024 * 1024
    budget_bytes: int = 64 * 1024 * 1024
    window_seconds: float = 600
    snapshot_seconds: float = 1
    ipc_bytes: int = 128 * 1024
    active_stages: int = 64
    cpu_percent: float = 90  # One logical CPU = 100%, not core-normalized.
    sustained_seconds: float = 120
    growth_bytes: int = 256 * 1024 * 1024
    ui_delay_seconds: float = 10
    loop_delay_seconds: float = 120
    stage_delay_seconds: float = 300
    stack_count: int = 3
    stack_interval_seconds: float = 5

    def payload(self):
        return asdict(self)


POLICY = Policy()
IDS = frozenset(
    {
        "task_id",
        "flow_id",
        "conversation_id",
        "batch_id",
        "action_id",
        "native_msg_id",
        "process_run_id",
        "stage_run_id",
    }
)


def correlations(value):
    # Explicit ID allowlist: never serialize stage payloads or message bodies.
    return {k: str(v)[:128] for k, v in value.items() if k in IDS and v is not None}


class Anomalies:
    """Continuous conditions merge into one event; first CPU sample is unknown."""

    def __init__(self, policy=POLICY):
        self.policy = policy
        self.since = {}
        self.baseline = None

    def check(self, now, resources, snapshot):
        p = self.policy
        reasons = []
        main = resources.get("worker", {})
        memory = main.get("rss_bytes")
        if isinstance(memory, int):
            if self.baseline is None or memory < self.baseline:
                self.baseline = memory
        conditions = {
            "sustained_cpu": (main.get("cpu_percent") or 0) >= p.cpu_percent,
            "memory_growth": memory is not None
            and self.baseline is not None
            and memory - self.baseline >= p.growth_bytes,
        }
        for name, active in conditions.items():
            if active:
                self.since.setdefault(name, now)
                if now - self.since[name] >= p.sustained_seconds:
                    reasons.append(name)
            else:
                self.since.pop(name, None)
        ui = snapshot.get("ui_monotonic")
        if ui is not None and now - ui > p.ui_delay_seconds:
            reasons.append("ui_response_gap")
        for name, loop in snapshot.get("loops", {}).items():
            # Intentional waits are labelled, not presumed deadlocks.
            if now - loop["at"] > p.loop_delay_seconds and loop.get("waiting") is None:
                reasons.append("loop_progress_gap:" + name)
        if any(
            now - s["at"] > p.stage_delay_seconds
            for s in snapshot.get("stages", {}).values()
        ):
            reasons.append("long_stage")
        return reasons
