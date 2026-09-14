"""A second service must not reconcile or unlink the active owner's state."""

import subprocess
import sys


def test_second_service_cannot_touch_runs_or_socket(service):
    service.set_mode("slow")
    run_id = service.run_now("DEMO-9999")["run"]["id"]
    other_socket = service.root / "other.sock"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omakron.service",
            "--state-dir",
            str(service.state),
            "--config-dir",
            str(service.config),
            "--socket",
            str(other_socket),
        ],
        env=service.env(),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert "another Omakron service owns" in result.stderr
    assert service.socket.exists()
    assert service.get_run(run_id)["status"] != "interrupted"
    assert not other_socket.exists()
    assert service.wait_run(run_id)["status"] == "succeeded"
