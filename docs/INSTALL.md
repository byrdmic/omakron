# Install and recover Omakron 0.1.0

This release supports Omarchy 4.0.2-1, Quickshell 0.3.1, Python 3.14, and Claude Code 2.1.270.
Setup refuses other versions, and the installed service checks compatibility at startup.
The runtime dependencies are pinned by version and hash in requirements/runtime.txt.
A separate virtual environment keeps them out of system Python.

Every command below previews its changes by default.
Add --apply to perform the printed operation after reviewing it.
Installation on the daily host follows acceptance of the named release.

## Clone the plugin

Use the exact accepted commit SHA, with all forty characters.
This step clones the repository into the user plugin directory.
It does not change the bar or start a service.

```sh
python scripts/manage.py install --source <repository-url-or-path> --revision <accepted-commit-sha>
python scripts/manage.py install --source <repository-url-or-path> --revision <accepted-commit-sha> --apply
```

The destination is $XDG_CONFIG_HOME/omarchy/plugins/omakron.routines.
Its default location is ~/.config/omarchy/plugins/omakron.routines.
The --source option is required and accepts a Git repository URL or local path.

## Set up the service and bar

Service setup installs the pinned dependencies and writes a quoted systemd user unit.
It validates that unit, enables it, and starts it with scheduled dispatch disabled.
The current executable PATH and the Claude executable location are retained.
It does not enable lingering or wake timers.

```sh
python scripts/manage.py setup-service
python scripts/manage.py setup-service --apply
python scripts/manage.py enable-widget
python scripts/manage.py enable-widget --apply
```

The widget appears after Agents in the right bar.
Other shell entries keep their values and order.
The service seeds the manual triage routine without enabling recurrence.
Scheduled routines need their saved policy enabled and global dispatch resumed.

The generated unit lives at $XDG_CONFIG_HOME/systemd/user/omakron.service.
The reference unit in packaging/ is explanatory and is not copied verbatim.
Custom XDG_CONFIG_HOME values require a user systemd manager configured for that directory.

## Back up and upgrade

A backup stops the service and scheduled dispatch before copying data.
A foreground service holding the state lock prevents backup or replacement.
Backups include Omakron code, its virtual environment, database, output, config, and unit.
They stay private beneath $XDG_DATA_HOME/omakron/backups.
Their hashes are verified before restoration.

```sh
python scripts/manage.py backup
python scripts/manage.py backup --apply
python scripts/manage.py upgrade --source <repository-url-or-path> --revision <accepted-commit-sha>
python scripts/manage.py upgrade --source <repository-url-or-path> --revision <accepted-commit-sha> --apply
```

Upgrade creates a backup first and starts the new service with dispatch disabled.
If installation fails, it restores the prior code, data, config, and unit.
The restored service stays stopped and the widget stays hidden.
The recovery record is written to $XDG_DATA_HOME/omakron/recovery.json.
Review that evidence before attempting another upgrade.

## Restore or remove

Rollback restores only Omakron files from the specified backup.
It preserves current unrelated shell settings and removes the Omakron widget.
It leaves scheduled dispatch disabled and the service stopped.
Service setup and widget activation are explicit next steps.

```sh
python scripts/manage.py rollback --backup <backup-directory>
python scripts/manage.py rollback --backup <backup-directory> --apply
python scripts/manage.py disable
python scripts/manage.py disable --apply
python scripts/manage.py uninstall
python scripts/manage.py uninstall --apply
```

Disable stops the user service and removes the widget without deleting data.
Uninstall first creates a backup, then removes the plugin and unit.
The routine database, output, Omakron settings, and backups remain.
There is no implicit command to erase retained user data.

## Repeat the rehearsal

The rehearsal uses real Git clones, dependency installs, and SQLite databases.
It validates generated systemd units and launches isolated foreground services.
It tests the cloned UI inside a disposable native Omarchy session.
It does not install a daily user unit or change daily shell configuration.

```sh
PYTHONPATH=src .venv/bin/python scripts/rehearse_install.py --revision <candidate-commit-sha> --out /path/to/new/evidence
```

Use `--baseline <older-commit-sha>` to rehearse an upgrade from an older revision.
Without it, the rehearsal reinstalls the candidate and verifies backup restoration.
Keep generated evidence private because it includes local paths and process output.
