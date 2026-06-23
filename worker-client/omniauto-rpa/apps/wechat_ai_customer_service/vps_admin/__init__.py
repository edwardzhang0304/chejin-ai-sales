"""VPS-side control plane for multi-tenant WeChat customer-service deployments."""

from .app import create_app

__all__ = ["create_app"]
