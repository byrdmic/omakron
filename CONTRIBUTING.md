# Contributing to Omakron

Read [docs/PHILOSOPHY.md](docs/PHILOSOPHY.md) first. It states the direction
Omakron is taking, which parts of the current code are being changed to match,
and which guarantees stay.

Describe the problem and expected behavior before changing code.
Use a branch for each change and include verification in the pull request.
Installation on a desktop is a separate operation from merging source changes.

## Repository layout

The repository root is the plugin folder. Its manifest and QML live in
`manifest.json` and `ui/`. The Python service, runner, scheduler, and client
live in `src/omakron/`. Tests use synthetic issues, a fake Claude executable,
and disposable service processes. Scripts provide native UI checks and
installation rehearsals. Runtime and development dependencies are hash-pinned
in `requirements/`.

## Checks

```sh
python -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/dev.txt
PATH=.venv/bin:$PATH scripts/check.sh
```

CI runs the same script. It checks formatting, lint, tests, the plugin manifest,
and synthetic reports. Invalid fixtures should continue failing with their
expected errors.

Verify QML changes in a disposable Omarchy session:

```sh
python scripts/verify_popup.py --live-editor --out /path/to/private/evidence
```

Keep machine-generated evidence outside the repository. Logs and screenshots
can include account names, hostnames, local paths, and private issue content.
Use synthetic fixtures in committed tests and remove local details from any
verification summary you publish. Never commit API keys or live issue snapshots.

Real Claude calls require an existing login and may consume account usage.
CI uses a fake executable and does not make model calls.
