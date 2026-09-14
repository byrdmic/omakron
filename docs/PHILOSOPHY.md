# Philosophy

Omakron trusts the agent.

A routine is a prompt, a model, a working folder, and a schedule. The model
decides what to do each run. The service starts it, keeps it within a time
and output budget, records what happened, and shows the result. The service
does not second-guess the work.

This page states the position so contributors build in one direction. It is
not a description of the current code. Several modules were written the other
way and are being changed to match. See "What changes" below.

## The position

The prompt is the program. Anything a person could ask Claude Code to do in a
terminal, a routine can do on a schedule. The routine author is responsible
for what the prompt asks for and which tools it is given.

The model has tools. A routine can read and edit files, run commands, use git,
and talk to outside services such as Trello through the same skills and MCP
servers a person would use. The service does not stand between the model and
those services.

The model reports its own outcome. A run succeeds when the process exits
cleanly and the result event reports no error. The service does not require a
report in a fixed shape and does not check the model's claims against a
contract. If the model says it moved a card and opened a branch, the run
record says so, and the person reads the run record.

The service is a scheduler and a recorder, not a judge. It owns the queue,
the run history, the deadline, the output limits, cancel, and retry. It keeps
every run's prompt, revision, output, and result durably. Those are
bookkeeping, not distrust.

Autonomy comes from the account, not from the tool. Routines run with the
signed-in Claude Code account and whatever access that account already has.
Omakron does not add its own permission layer on top.

## What this means for two example routines

Triage. The routine reads the board through the Trello skill, decides labels,
priority, and next step for each new card, and writes them to the card itself.
There is no read-only snapshot and no service-side write step.

Coding. The routine looks for cards marked ready, picks one, works in the
project folder, writes and runs code, commits to a branch, and updates the
card with the branch link and a summary. The routine decides when the card is
done. A person reviews the branch when they choose to.

## What this is not

This is not the "verify the contract" approach. That approach treats the
model's output as data to be validated and keeps the model away from outside
systems. Omakron considered it and chose against it for 2026. The trade is
stated plainly: a run can be marked successful when the work was wrong, and
the person finds out by reading the run record or the board. Omakron accepts
that trade in exchange for routines that can do real work unattended.

This is also not "thin glue over cron". Runs are durable records with a
claimed status and a saved routine revision, overlap of the same routine is
prevented, and missed occurrences are skipped rather than piled up. The
service exists so those guarantees hold.

## What changes

The following current behaviors contradict this page and are scheduled to be
removed or relaxed. Contributors should not extend them.

- The invocation profile in `src/omakron/runner.py` passes `--restricted`,
  `--safe-mode`, `--strict-mcp-config`, and `--permission-prompts none`. A
  routine needs tools, skills, and MCP servers, so the profile becomes a
  per-routine choice with a permissive default.
- `classify` in `src/omakron/runner.py` fails a run for any tool use and for
  any result that does not validate as a triage report. Tool use becomes
  normal, and the report contract becomes optional per routine.
- `src/omakron/snapshot.py` fetches issues on the model's behalf. The model
  fetches its own work through its tools. Snapshot sources stay only for
  routines that opt into them.
- `src/omakron/triage.py` hard-codes a report-only Linear routine with no
  tools. The seeded routine becomes a Trello triage routine with tools.
- The child environment in `runner.py` is filtered to six keys. The routine
  author chooses what the child inherits, and the default passes through the
  keys the account's skills and MCP servers need.

## What stays

- One service, one worker, one writer of its own database.
- Every run is claimed in a transaction and carries an immutable revision.
- Every run has a deadline and an output budget, and both are settings.
- Cancel and retry are explicit, and retry links to the original run.
- Prompt text and card text travel on stdin, never on argv.
- Tests never call the real CLI. The fake executable and fake clock stay.
- Run output, logs, and screenshots stay outside the repository.
