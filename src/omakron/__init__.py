"""Omakron: scheduled local Claude Code routines for the Omarchy bar.

Package layout follows the accepted architecture (internal design, the service architecture):

- ``plugin``   manifest contract shared with the Omarchy shell
- ``report``   triage report contract the supervisor enforces before saving
- ``runner``   Claude CLI invocation profile, process supervision, outcome classification
- ``triage``   the first routine: prompts, run parameter rule, stdin layout, seed definition
- ``snapshot`` issue snapshot sources (fixture folder, read-only Linear lookup)
- ``store``    SQLite persistence: routines, transactional run claims, immutable snapshots
- ``worker``   the run executor: claim, snapshot, launch, judge, store
- ``ipc``      bounded JSON-per-line framing shared by client and service
- ``service``  the user service: socket API plus the worker thread
- ``client``   local client used by the QML popup and at a terminal
- ``schedule`` shared cron validation, preview, and dispatch evaluator
- ``checks``   command-line entry point CI uses for the product checks
"""

__version__ = "0.1.0"
