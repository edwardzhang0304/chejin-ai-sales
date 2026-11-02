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
        "binding_id",
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
        self.baselines = {}
        self.details = []

    def check(self, now, resources, snapshot):
        p = self.policy
        reasons = []
        self.details = []
        seen = set()
        for label, resource in resources.items():
            role = label.split(":", 1)[0]
            if role not in ("worker", "ui", "ocr"):
                continue  # WeChat and sampler are separate resource references.
            if resource.get("unavailable", {}).get("process"):
                continue
            if role != "worker" and resource.get("identity_valid") is not True:
                continue
            identity = (role, resource.get("pid"), resource.get("created"))
            seen.add(identity)
            memory = resource.get("rss_bytes")
            if type(memory) is int:
                self.baselines[identity] = min(
                    memory, self.baselines.get(identity, memory)
                )
            conditions = {
                "sustained_cpu": (resource.get("cpu_percent") or 0) >= p.cpu_percent,
                "memory_growth": type(memory) is int
                and memory - self.baselines[identity] >= p.growth_bytes,
            }
            for name, active in conditions.items():
                key = (identity, name)
                if not active:
                    self.since.pop(key, None)
                    continue
                self.since.setdefault(key, now)
                if now - self.since[key] < p.sustained_seconds:
                    continue
                reason = (
                    name
                    if role == "worker"
                    else f"{name}:{role}:{identity[1]}@{identity[2]}"
                )
                reasons.append(reason)
                self.details.append(
                    {
                        "reason": reason,
                        "role": role,
                        "pid": identity[1],
                        "created": identity[2],
                        "metric": (
                            "cpu_percent"
                            if name == "sustained_cpu"
                            else "rss_growth_bytes"
                        ),
                        "value": (
                            resource.get("cpu_percent")
                            if name == "sustained_cpu"
                            else memory - self.baselines[identity]
                        ),
                        "threshold": (
                            p.cpu_percent if name == "sustained_cpu" else p.growth_bytes
                        ),
                        "sustained_seconds": now - self.since[key],
                    }
                )
        self.baselines = {
            k: v for k, v in self.baselines.items() if k in seen or k[0] == "worker"
        }
        self.since = {k: v for k, v in self.since.items() if k[0] in seen}
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


def expired(meta, *, session, now, mono, policy=POLICY, clock_uncertain=False):
    if meta.get("session") == session and mono is not None and "last_mono" in meta:
        return mono - meta["last_mono"] >= policy.retention_seconds
    if clock_uncertain:
        return False
    return now - meta["last"] >= policy.retention_seconds


def window_overlaps(meta, event, policy=POLICY):
    """One definition for persisted protection, retirement and export priority."""
    if event["session"] != meta["session"]:
        return False
    suffix = (
        "_mono"
        if all(k + "_mono" in meta and k + "_mono" in event for k in ("first", "last"))
        else ""
    )
    return (
        meta["last" + suffix] >= event["first" + suffix] - policy.window_seconds
        and meta["first" + suffix] <= event["last" + suffix] + policy.window_seconds
    )


def protected(meta, index, *, session, now, mono=None, policy=POLICY):
    return any(
        window_overlaps(meta, e, policy)
        and (
            e.get("end") is None
            or not expired(
                e,
                session=session,
                now=now,
                mono=mono,
                policy=policy,
                clock_uncertain=index.get("retention_clock_uncertain", False),
            )
        )
        for e in index["events"]
    )


def export_order(index, policy=POLICY):
    """Use writer's retention clock, never re-age evidence during read-only export."""
    policy = Policy(**index["policy"]) if "policy" in index else policy
    segments = index["segments"]
    clock = index.get("retention_clock", {})
    session = clock.get("session")
    now = clock.get("wall", max((m["last"] for m in segments), default=0))
    mono = clock.get("mono")
    tails = {}
    for m in segments:
        if index["sessions"].get(m["session"], {}).get("status") == "end_unconfirmed":
            tails[m["session"]] = max(tails.get(m["session"], ""), m["name"])
    current = [m["name"] for m in segments if m["session"] == session]
    first = min(current) if current else None

    def key(m):
        priority = (
            0
            if protected(m, index, session=session, now=now, mono=mono, policy=policy)
            else (
                1
                if m["name"] == tails.get(m["session"])
                else 2 if m["name"] == first else 3
            )
        )
        return (priority, -m["last"])

    return sorted(segments, key=key)
