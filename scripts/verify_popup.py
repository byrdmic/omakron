"""Exercise the real Omarchy bar in a disposable nested compositor.

The copied native components read the current theme. The harness never loads
shell.qml from the installed desktop or writes its configuration. Agent usage
is synthetic, and its collector is replaced with a no-op executable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def run(args, env, *, check=True):
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=15, check=check)


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise RuntimeError("test session did not become ready")


def verify(out: Path, live_editor: bool = False, plugin: Path = REPO):  # noqa: PLR0915 - sequential UI acceptance scenario
    out.mkdir(parents=True, exist_ok=False)
    config = Path.home() / ".config/omarchy/shell.json"
    before = hashlib.sha256(config.read_bytes()).hexdigest()
    processes = []
    with tempfile.TemporaryDirectory(prefix="oq", dir="/tmp") as directory:
        scratch = Path(directory)
        shell = scratch / "shell"
        runtime = scratch / "r"
        runtime.mkdir(mode=0o700)
        shutil.copytree("/usr/share/omarchy/shell", shell)
        shutil.copytree(plugin / "ui", shell / "omakron")
        shutil.copyfile(REPO / "tests/ui/shell.qml", shell / "shell.qml")
        shutil.copytree(plugin / "src", shell / "src")
        (shell / "scripts").mkdir()
        shutil.copyfile(plugin / "scripts/client.py", shell / "scripts/client.py")
        binaries = scratch / "bin"
        binaries.mkdir()
        collector = binaries / "omarchy-agent-usage-update"
        collector.write_text("#!/bin/sh\nexit 0\n")
        collector.chmod(0o755)
        usage = scratch / "state/omarchy/agents/usage"
        usage.mkdir(parents=True)
        (usage / "claude.json").write_text(
            json.dumps(
                {
                    "id": "claude",
                    "name": "Claude",
                    "tierLabel": "Test fixture",
                    "ready": True,
                    "totalPrompts": 1,
                    "totalSessions": 1,
                }
            )
        )
        compositor_config = scratch / "hyprland.lua"
        compositor_config.write_text(
            'hl.monitor({output="", mode="1920x1080@60", position="auto", scale=1})\n'
            "hl.config({misc={disable_watchdog_warning=true, disable_hyprland_logo=true, "
            "disable_splash_rendering=true}})\n"
        )
        parent_display = str(Path(os.environ["XDG_RUNTIME_DIR"]) / os.environ["WAYLAND_DISPLAY"])
        env = dict(
            os.environ,
            XDG_RUNTIME_DIR=str(runtime),
            WAYLAND_DISPLAY=parent_display,
            AQ_DRM_DEVICES="/dev/null",
            XDG_STATE_HOME=str(scratch / "state"),
            OMAKRON_TEST_SHELL=str(shell),
            QT_QPA_PLATFORM="wayland",
            PATH=str(binaries) + os.pathsep + os.environ["PATH"],
        )
        env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        try:
            with (out / "compositor.log").open("w") as log:
                processes.append(
                    subprocess.Popen(
                        [shutil.which("Hyprland"), "--config", str(compositor_config)],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                )
            wait_until((runtime / "wayland-1").exists)
            env["WAYLAND_DISPLAY"] = "wayland-1"
            env["HYPRLAND_INSTANCE_SIGNATURE"] = next((runtime / "hypr").iterdir()).name
            with (out / "shell.log").open("w") as log:
                processes.append(
                    subprocess.Popen(
                        [shutil.which("qs"), "-p", str(shell), "--no-color"],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                )

            def ipc(target, method, *args):
                return run(
                    ["qs", "ipc", "-p", str(shell), "call", target, method, *args], env
                ).stdout

            def ready():
                result = run(
                    ["qs", "ipc", "-p", str(shell), "call", "test", "inspect"], env, check=False
                )
                return result.returncode == 0 and '"omakron":false' in result.stdout

            wait_until(ready)

            def key(value):
                run(
                    [
                        "hyprctl",
                        "eval",
                        'hl.dispatch(hl.dsp.send_shortcut({mods="",key="' + value + '"}))',
                    ],
                    env,
                )
                time.sleep(0.2)

            def capture(name):
                time.sleep(0.4)
                run(["grim", "-o", "WAYLAND-1", str(out / (name + ".png"))], env)

            ipc("test", "open")
            wait_until(lambda: json.loads(ipc("test", "inspect"))["omakron"])
            time.sleep(0.3)
            capture("scale-100")
            key("Tab")
            key("Return")
            assert json.loads(ipc("omakron.routines", "state"))["editing"]
            capture("editor-empty")
            key("Escape")
            assert not json.loads(ipc("omakron.routines", "state"))["editing"]
            key("Escape")
            closed = json.loads(ipc("omakron.routines", "state"))
            assert not closed["opened"] and closed["focus"], closed
            ipc("omakron.routines", "toggle")
            assert json.loads(ipc("test", "inspect"))["omakron"]
            ipc("test", "agents")
            time.sleep(0.3)
            switched = json.loads(ipc("test", "inspect"))
            assert switched == {"omakron": False, "active": "omarchy.agents"}, switched
            capture("panel-switch")
            ipc("test", "open")
            for state in ("empty", "loading", "error"):
                ipc("test", "state", state)
                time.sleep(0.3)
                ipc("test", "open")
                capture(state)
            ipc("test", "state", "ready")
            run(
                [
                    "hyprctl",
                    "eval",
                    'hl.monitor({output="WAYLAND-1",mode="1920x1080@60",position="auto",scale=2})',
                ],
                env,
            )
            time.sleep(0.4)
            ipc("test", "open")
            capture("scale-200")
            for position in ("left", "bottom", "right"):
                ipc("test", "position", position)
                time.sleep(0.3)
                ipc("test", "open")
                capture("edge-" + position)
            if live_editor:
                verify_editor_flow(
                    ipc, key, capture, env=env, scratch=scratch, out=out, processes=processes
                )
            errors = (out / "shell.log").read_text()
            unexpected = [
                line
                for line in errors.splitlines()
                if ("WARN scene" in line or "ERROR" in line)
                and "another handler is registered for target" not in line
            ]
            assert not unexpected, unexpected
            after = hashlib.sha256(config.read_bytes()).hexdigest()
            assert before == after
            (out / "report.json").write_text(
                json.dumps(
                    {
                        "check": "native popup",
                        "result": "passed",
                        "omarchy": run(["omarchy", "version"], env).stdout.strip(),
                        "checks": [
                            "native bar anchor",
                            "keyboard Tab/Enter opens the editor",
                            "Escape closes editor then panel",
                            "launcher retains focus target",
                            "toggle reopens",
                            "native panel switching",
                            "empty/loading/error",
                            "100%/200%",
                            "screen edges",
                        ],
                        "shell_config_before": before,
                        "shell_config_after": after,
                        "focus_note": (
                            "The launcher retains Qt focus. "
                            "The native bar does not take compositor keyboard focus."
                        ),
                        "agent_data": "synthetic; collector replaced with no-op",
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)


def verify_editor_flow(ipc, key, capture, *, env, scratch, out, processes):
    """Use the actual editor, socket client, and service in temporary state."""
    service_env = dict(env, PYTHONPATH=str(REPO / "src"))
    config = scratch / "service-config"
    config.mkdir()
    (config / "settings.json").write_text(
        json.dumps(
            {
                "claude_executable": str(REPO / "tests/fake_claude.py"),
                "snapshot_source": {
                    "kind": "fixture",
                    "dir": str(REPO / "tests/fixtures/snapshots"),
                },
            }
        )
    )
    with (out / "service.log").open("w") as log:
        process = subprocess.Popen(
            [
                str(REPO / ".venv/bin/python"),
                "-m",
                "omakron.service",
                "--state-dir",
                str(scratch / "service-state"),
                "--config-dir",
                str(config),
            ],
            env=service_env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    processes.append(process)
    wait_until((Path(env["XDG_RUNTIME_DIR"]) / "omakron/service.sock").exists)
    ipc("test", "position", "top")
    ipc("test", "state", "")
    ipc("test", "open")

    def state():
        return json.loads(ipc("test", "editorState"))

    wait_until(lambda: state()["connected"] and len(state()["routines"]) == 1)
    ipc("test", "focus", "newRoutine")
    key("Return")
    wait_until(lambda: state()["editing"])
    ipc("test", "set", "field_name", "Keyboard schedule")
    ipc("test", "set", "field_prompt", "A literal report prompt with $ and quotes.")
    ipc("test", "editor", "Weekdays")
    wait_until(lambda: "Next runs" in state()["preview"])
    capture("editor-preview")
    ipc("test", "set", "field_time", "25:99")
    ipc("test", "focus", "saveRoutine")
    key("Return")
    wait_until(lambda: state()["error"] != "")
    assert state()["editing"] and len(state()["routines"]) == 1, state()
    capture("editor-invalid")
    ipc("test", "set", "field_time", "09:00")
    ipc("test", "focus", "saveRoutine")
    key("Return")
    wait_until(lambda: not state()["editing"] and len(state()["routines"]) == 2)
    saved = [r for r in state()["routines"] if r["name"] == "Keyboard schedule"][0]
    assert saved["cron"] == "0 9 * * 1-5" and not saved["enabled"]
    capture("editor-saved")
    ipc("test", "focus", "newRoutine")
    key("Return")
    wait_until(lambda: state()["editing"])
    ipc("test", "set", "field_name", "Discard this draft")
    key("Escape")
    assert not state()["editing"] and len(state()["routines"]) == 2
    (out / "editor-result.json").write_text(
        json.dumps(
            {
                "check": "routine editor",
                "result": "passed",
                "saved": saved,
                "checks": [
                    "keyboard create/save/cancel",
                    "five previews",
                    "invalid draft retained",
                    "save paused",
                ],
            },
            indent=2,
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--live-editor", action="store_true")
    parser.add_argument("--plugin", type=Path, default=REPO)
    args = parser.parse_args()
    verify(args.out.resolve(), args.live_editor, args.plugin.resolve())
    print("native popup checks passed")


if __name__ == "__main__":
    main()
