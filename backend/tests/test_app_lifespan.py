from fastapi.testclient import TestClient

from app import main as app_main


def test_lifespan_runs_startup_recovery_and_stops_background_loop(monkeypatch):
    calls: list[str] = []

    class FakeRecoveryLoop:
        def start(self) -> None:
            calls.append("recovery_start")

        def stop(self) -> None:
            calls.append("recovery_stop")

    monkeypatch.setattr(app_main.settings, "auto_create_tables", False)
    monkeypatch.setattr(
        type(app_main.settings),
        "assert_runtime_safe",
        lambda _self: calls.append("runtime_safe"),
    )
    monkeypatch.setattr(
        app_main,
        "_recover_observability_on_startup_best_effort",
        lambda: calls.append("observability_recovery") or 0,
    )
    monkeypatch.setattr(
        app_main,
        "retry_pending_vehicle_file_cleanups",
        lambda: calls.append("vehicle_cleanup") or {"pending": 0},
    )
    monkeypatch.setattr(
        app_main,
        "recover_handoff_notifications",
        lambda: calls.append("handoff_recovery")
        or {"unknown_settled": 0, "pending_attempted": 0},
    )
    monkeypatch.setattr(app_main, "C3BatchRecoveryLoop", FakeRecoveryLoop)

    app = app_main.create_app()
    assert app.router.on_startup == []
    assert app.router.on_shutdown == []

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert calls == [
            "runtime_safe",
            "observability_recovery",
            "vehicle_cleanup",
            "handoff_recovery",
            "recovery_start",
        ]

    assert calls[-1] == "recovery_stop"
