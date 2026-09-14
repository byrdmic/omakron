# Contributing to Omakron

Read [docs/PHILOSOPHY.md](docs/PHILOSOPHY.md) first. It states the direction
Omakron is taking, which parts of the current code are being changed to match,
and which guarantees stay.

Describe the problem and expected behavior before changing code.
Use a branch for each change and include verification in the pull request.
Installation on a desktop is a separate operation from merging source changes.

## Branches

`master` is the only long-lived branch and is always the installable state of
the plugin. It is protected: changes land by pull request after the CI check
passes, and the branch must be current with `master` before merging.

Start one short-lived branch per change, named for its intent, such as
`fix/scheduler-overlap` or `feat/run-details`. Keep the branch small enough
that CI is the review, and split a branch that lives more than a few days.
Land refactors and behavior changes in separate pull requests so a later
bisect stays meaningful.

Pull requests are squash-merged, and the branch is deleted on merge. There is
no development branch and no release branch.

Releases are annotated tags on `master` that match the version in
`manifest.json`. Bump the version in the pull request that finishes the work
and tag after it merges.

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

CI runs the same script. It checks formatting, lint, tests, and the plugin
manifest. Invalid fixtures should continue failing with their expected errors.

Verify QML changes in a disposable Omarchy session:

```sh
python scripts/verify_popup.py --live-editor --out /path/to/private/evidence
```

Keep machine-generated evidence outside the repository. Logs and screenshots
can include account names, hostnames, local paths, and private issue content.
Use synthetic fixtures in committed tests and remove local details from any
verification summary you publish. Never commit API keys or live run output.

Real Claude calls require an existing login and may consume account usage.
CI uses a fake executable and does not make model calls.
