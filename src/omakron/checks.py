"""Product checks as a command line: ``python -m omakron.checks <check> ...``.

Exit status 0 means the check passed; 1 means it found problems, which are
printed one per line; 2 means the command was used wrongly. CI runs these
against real inputs that must pass and against fixtures built to fail.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from omakron.plugin import validate_manifest_dir
from omakron.report import validate_report
from omakron.runner import classify, parse_stream


def _labels(value: str | None) -> list[str] | None:
    return None if value is None else [x.strip() for x in value.split(",") if x.strip()]


def check_manifest(args: argparse.Namespace) -> list[str]:
    return validate_manifest_dir(Path(args.plugin_dir))


def check_report(args: argparse.Namespace) -> list[str]:
    try:
        obj = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [f"cannot read report: {exc}"]
    return validate_report(obj, _labels(args.team_labels))


def check_stream(args: argparse.Namespace) -> list[str]:
    try:
        text = Path(args.transcript).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read transcript: {exc}"]
    verdict = classify(
        exit_code=args.exit_code, stream=parse_stream(text), team_labels=_labels(args.team_labels)
    )
    return [] if verdict.ok else [f"{verdict.outcome}: {p}" for p in verdict.problems]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m omakron.checks", description=__doc__)
    sub = parser.add_subparsers(dest="check", required=True)

    manifest = sub.add_parser("manifest", help="validate an Omarchy plugin folder")
    manifest.add_argument("plugin_dir")
    manifest.set_defaults(func=check_manifest)

    report = sub.add_parser("report", help="validate a saved triage report")
    report.add_argument("report")
    report.add_argument("--team-labels", help="comma-separated labels the snapshot offered")
    report.set_defaults(func=check_report)

    stream = sub.add_parser("stream", help="classify a stream-json transcript as a run outcome")
    stream.add_argument("transcript")
    stream.add_argument("--exit-code", type=int, default=0)
    stream.add_argument("--team-labels")
    stream.set_defaults(func=check_stream)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    problems = args.func(args)
    if problems:
        for problem in problems:
            print(problem)
        print(f"{args.check}: FAIL ({len(problems)} problem{'s' if len(problems) != 1 else ''})")
        return 1
    print(f"{args.check}: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
