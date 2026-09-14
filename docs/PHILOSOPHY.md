# Philosophy

Omakron trusts the agent.

A routine is a prompt, a model, a working folder, and a schedule. The model
decides what to do each run. The service starts it, keeps it within a time
and output budget, records what happened, and shows the result. The service
does not second-guess the work.

This page states the position so contributors build in one direction. The
code follows it; the last section says where.

## The position

The prompt is the program. Anything a person could ask Claude Code to do in a
terminal, a routine can do on a schedule. The routine author is responsible
for what the prompt asks for and which tools it is given.

The model has tools. A routine can read and edit files, run commands, use git,
and talk to outside services through the same skills and MCP servers a person
would use. The service does not stand between the model and those services.

The model reports its own outcome. A run succeeds when the process exits
cleanly and the result event reports no error. The service does not require a
result in a fixed shape and does not check the model's claims. If the model
says it finished the work, the run record says so, and the person reads the
run record.

The service is a scheduler and a recorder, not a judge. It owns the queue,
the run history, the deadline, the output limits, cancel, and retry. It keeps
every run's prompt, revision, output, and result durably. Those are
bookkeeping, not distrust.

Autonomy comes from the account, not from the tool. Routines run with the
signed-in Claude Code account and whatever access that account already has.
Omakron does not add its own permission layer on top. By default a routine
runs with the CLI's usual tools and no permission prompts; the routine author
can narrow that.

## What this means for a routine

A triage routine reads a board or tracker through the same skill or MCP
server a person would use, decides labels, priority, and next step for each
new item, and writes them to the item itself. There is no read-only snapshot
and no service-side write step.

A coding routine looks for items marked ready, picks one, works in the
project folder, writes and runs code, commits to a branch, and updates the
item with the branch link and a summary. The routine decides when the item is
done. A person reviews the branch when they choose to.

## What this is not

This is not the "verify the contract" approach. That approach treats the
model's output as data to be validated and keeps the model away from outside
systems. Omakron considered it and chose against it for 2026. The trade is
stated plainly: a run can be marked successful when the work was wrong, and
the person finds out by reading the run record or the work itself. Omakron
accepts that trade in exchange for routines that can do real work unattended.

The first version of Omakron was built that way, around a report-only issue
triage with a checked JSON report. That code was removed rather than kept as
an option, so there is one way to run.

This is also not "thin glue over cron". Runs are durable records with a
claimed status and a saved routine revision, overlap of the same routine is
prevented, and missed occurrences are skipped rather than piled up. The
service exists so those guarantees hold.

## Where the code follows this

- A routine carries `tools`, `permission_mode`, `mcp_config`, and
  `env_passthrough`. `src/omakron/routines.py` validates them and
  `src/omakron/store.py` saves them with every revision.
- `src/omakron/runner.py` builds the invocation from those choices. The
  defaults are the CLI's full tool set and `bypassPermissions`. There is no
  `--safe-mode` or `--restricted`, so the account's CLAUDE.md, skills, plugins,
  hooks, and MCP servers apply.
- `classify` in `src/omakron/runner.py` marks a run succeeded on exit status
  zero and a result event without an error. Tool use is never a failure, and
  the result text is kept as the model wrote it.
- The child environment is the session basics plus whatever keys the routine
  names. Keys from a parent Claude Code session are never inherited.
- `src/omakron/seed.py` seeds an example routine with tools.
- The model's result is stored as `result.md` in the run folder and shown in
  the run detail. A run is not marked succeeded until that file is written.

## What stays

- One service, one worker, one writer of its own database.
- Every run is claimed in a transaction and carries an immutable revision.
- Every run has a deadline and an output budget, and both are settings.
- Cancel and retry are explicit, and retry links to the original run.
- Prompt text and input text travel on stdin, never on argv.
- Tests never call the real CLI. The fake executable and fake clock stay.
- Run output, logs, and screenshots stay outside the repository.
