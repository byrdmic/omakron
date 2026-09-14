# CLAUDE.md

Omakron is a native Omarchy bar plugin with a local Python service.
It runs scheduled, Claude Code routines.

Work within the requested issue scope and read CONTRIBUTING.md for checks.

## Commands

Set up once. Python 3.14 is required, and every dependency is hash-pinned.

```sh
python -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/dev.txt
```

Run the full product checks before a pull request. CI runs exactly this script.

```sh
PATH=.venv/bin:$PATH scripts/check.sh
```

Individual checks, with `PATH=.venv/bin:$PATH` and `PYTHONPATH=src` set:

```sh
ruff check . && ruff format --check .
pytest -q
pytest -q tests/test_scheduler.py
pytest -q tests/test_runner_outcome.py -k "exit_zero"
pytest -q -m "not slow"                      # skip the real-service subprocess tests
python -m omakron.checks manifest .
python -m omakron.checks report tests/fixtures/reports/valid-triage-report.json --team-labels Bug,Feature,Improvement,Docs
```

Tests marked `slow` start the real service as a subprocess against the fake `claude` executable.
The `service` and `service_factory` fixtures in tests/conftest.py provide that harness.
Markers are strict, and xfail is strict.

Native UI verification needs a disposable nested Omarchy session, not CI:

```sh
python scripts/verify_popup.py --live-editor --out /path/to/private/evidence
```

Running the service by hand, and one bounded real-model smoke test:

```sh
PYTHONPATH=src python -m omakron.service
PYTHONPATH=src python -m omakron.client status
PYTHONPATH=src python scripts/smoke_vertical_slice.py --out /path/to/scratch
```

Installer actions preview by default and apply only with `--apply`.
Use `python scripts/manage.py <action>` and see docs/INSTALL.md.

## Architecture

The repository root is the plugin folder the shell installs.
Two halves talk over one user-only Unix socket.

The QML half is `manifest.json` plus `ui/`.
Widget.qml is the bar entry point and hosts RoutinesPanel.qml.
ServiceClient.qml spawns `python3 scripts/client.py request` for every call.
It writes one JSON request to stdin and reads one JSON reply.
The popup never touches the database or the Claude CLI directly.

The Python half is `src/omakron/`, and the service is the only writer.
A request flows client -> ipc -> service -> store, and runs flow scheduler or run_now -> store queue -> worker -> runner -> claude.
Each module has a docstring stating its contract. Read that first.

- `ipc.py` frames one bounded JSON object per line. Requests over 64 KiB and responses over 1 MiB are refused.
- `service.py` owns the socket API, settings, and the single worker thread. It seeds the "Issue triage" routine on an empty database and marks orphaned runs interrupted on restart.
- `store.py` is SQLite under `$XDG_STATE_HOME/omakron`. Runs are claimed in one `BEGIN IMMEDIATE` transaction, carry an immutable routine revision, and reach a terminal status only after output is durably written.
- `worker.py` executes one run in a fixed order: claim, snapshot, launch, judge, store.
- `runner.py` holds the Claude invocation profile, process supervision with a deadline and bounded output, and outcome classification. Classification uses supervisor facts plus the report contract, never the model's own words.
- `report.py` validates the triage report object. Exit status zero and `is_error: false` alone never make a run succeed.
- `snapshot.py` fetches the one issue a run triages. Sources are a fixture folder, a manual inbox import, or a read-only Linear lookup. The model never talks to Linear.
- `triage.py` defines the seeded routine, its fixed system prompt, and how prompt plus snapshot are laid out on stdin.
- `schedule.py` and `scheduler.py` share one cron evaluator for previews and dispatch. Missed occurrences beyond 60 seconds are skipped, same-routine overlap is prevented, and queued work expires after five minutes.
- `history.py` builds run summaries and details and handles retention cleanup.
- `plugin.py` mirrors the shell's manifest validation so CI can refuse what the shell would refuse.
- `manage.py` is the standard-library-only installer, backup, upgrade, rollback, and uninstall.
- `checks.py` is the CLI that check.sh calls for manifest, report, and stream checks.

Tests never call the real CLI. `tests/fake_claude.py` speaks enough `stream-json` to exercise the runner, and `FAKE_CLAUDE_MODE` selects its behavior.
`tests/conftest.py` provides a fake clock for schedule and deadline tests.
Fixtures under `tests/fixtures/` include inputs built to fail. check.sh proves they still fail.

## Implementation constraints

Keep manifest entry points relative and never add repository symlinks.
Never modify `/usr/share/omarchy` or the daily shell configuration to test.
Verify QML in a disposable Omarchy session.

The Claude invocation profile lives in `src/omakron/runner.py`.
Changes to its flags need new verification of the execution restrictions.
Do not use permission bypass, `--bare`, or `--json-schema`.
Exit status zero is insufficient; a report also needs to parse and validate.
Prompt text and issue text are data, never argv or shell source.
The child environment is filtered to a short passthrough list. Do not widen it casually.

Pin dependencies with hashes in `requirements/`.
Preserve the expected failures of invalid fixtures unless the contract changes.
Use synthetic issues and neutral example paths in committed files.
Keep execution logs, screenshots, and live snapshots outside the repository.
Never relabel a fixture snapshot as live data.
Real Claude calls need an existing login and consume account usage. Only the smoke scripts make them.
