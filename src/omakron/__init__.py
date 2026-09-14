"""Omakron: scheduled local Claude Code routines for the Omarchy bar.

- ``plugin``    manifest contract shared with the Omarchy shell
- ``runner``    Claude CLI invocation profile, process supervision, run outcome
- ``routines``  validation of a routine draft
- ``seed``      the example routine an empty database starts with
- ``store``     SQLite persistence: routines, transactional run claims, immutable revisions
- ``worker``    the run executor: claim, launch, record, store
- ``ipc``       bounded JSON-per-line framing shared by client and service
- ``service``   the user service: socket API plus the worker thread
- ``client``    local client used by the QML popup and at a terminal
- ``schedule``  shared cron validation, preview, and dispatch evaluator
- ``scheduler`` durable dispatch decisions
- ``history``   run summaries, details, retry, and retention
- ``manage``    installer, backup, upgrade, rollback, uninstall
- ``checks``    command-line entry point CI uses for the product checks
"""

__version__ = "0.1.0"
