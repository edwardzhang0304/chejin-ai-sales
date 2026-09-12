"""Shared runtime startup and update liveness; UI owns the Qt timer adapter."""
from pathlib import Path

from .storage import append_log


def start_runtime_services(self) -> None:
    """Start local runtime services before post-update health is declared."""

    if self._runtime_services_started:
        return
    self._runtime_services_started = True
    if self.binding:
        self.runner.start(self.binding)
    self.update_coordinator.start_result_reconciliation()


def post_update_runtime_health_snapshot(self) -> dict:
    """Return local liveness only; never probe backend or WeChat."""

    if not self._runtime_services_started:
        return {
            "ready": False,
            "binding_state": "bound" if self.binding else "unbound",
            "ui_event_loop_alive": True,
            "required_threads": [],
            "threads": {},
            "startup_failures": ["runtime_services_not_started"],
        }
    if not self.binding:
        return {
            "ready": True,
            "binding_state": "unbound",
            "ui_event_loop_alive": True,
            "required_threads": [],
            "threads": {},
            "startup_failures": [],
        }
    return {
        **self.runner.post_update_runtime_health_snapshot(),
        "ui_event_loop_alive": True,
    }


def schedule_post_update_startup(window, startup_update: dict, single_shot) -> None:
    from .post_update_health import RuntimeHealthGate

    plan = dict(startup_update.get("plan") or {})
    token = str(startup_update.get("token") or "")
    window.update_coordinator.set_post_update_context(plan, token)
    health_gate = RuntimeHealthGate(plan, token)

    def finish_post_update_startup() -> None:
        try:
            window.start_runtime_services()
            marker = health_gate.observe(
                window.post_update_runtime_health_snapshot()
            )
        except Exception as exc:
            from .update_diagnostics import record_update_startup_failure

            record_update_startup_failure(
                Path(plan["healthy_marker_path"]).parent / "update-plan.json",
                phase="runtime_health", exc=exc,
            )
            append_log(
                "ERROR",
                "post_update_runtime_health_failed",
                "新客户端运行服务未通过稳定健康检查，等待更新器回滚。",
                error_code="UPDATE_RUNTIME_HEALTH_FAILED",
                metadata={"exception_type": type(exc).__name__},
            )
            window.close()
            return
        if marker is None:
            single_shot(250, finish_post_update_startup)

    single_shot(
        0,
        finish_post_update_startup,
    )
