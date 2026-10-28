"""Common stale-attempt horizon for both durable notification queues."""
from datetime import timedelta
from app.core.config import get_settings


def notification_cutoff(now):
    # Preserve the existing reminder horizon. Age alone is not proof of death:
    # consumers also take the send owner's row lock with SKIP LOCKED.
    return now - timedelta(seconds=max(120, 4 * get_settings().feishu_http_timeout_seconds))
