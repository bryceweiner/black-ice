"""`blackice-watchers-provision` — fetch the weights, once, on purpose.

Runnable two ways, both of which end in the same place:

    uv run blackice-watchers-provision
    cd plugins/blackice-plugin-watchers && uv run python -m blackice_watchers.provision

and a third, the button on the plugin's dashboard panel, which calls the same
`models.provision` on a background thread.

This is the only code in the plugin that touches the network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import models
from . import settings as settings_mod


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blackice-watchers-provision",
        description=(
            "Download the person-detection, face-recognition, and ReID models "
            "the watchers plugin needs, and record their hashes. Nothing is "
            "ever downloaded at runtime."
        ),
    )
    parser.add_argument("--dir", type=Path, default=None,
                        help="where to put the models (default: data/watchers_models)")
    parser.add_argument("--model", default="",
                        help=f"only this one: {', '.join(s.key for s in models.MANIFEST)}")
    parser.add_argument("--force", action="store_true",
                        help="re-download even if the file is present and verified")
    parser.add_argument("--status", action="store_true",
                        help="report what is present and verified, and download nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    root = args.dir or settings_mod.model_dir()

    if args.status:
        report = models.status(root)
        print(json.dumps(report, indent=2) if args.json else _render(report))
        return 0 if report["ready"] else 1

    result = models.provision(
        root, only=args.model, force=args.force,
        progress=None if args.json else lambda message: print(f"  {message}", flush=True),
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_render(models.status(root)))
        for failure in result.get("failed", []):
            print(f"  failed: {failure}", file=sys.stderr)
        if not result.get("stack_installed"):
            print(
                "\nThe weights are in place but the libraries are not installed. Run:\n"
                "  uv pip install -e 'plugins/blackice-plugin-watchers[models]'",
                file=sys.stderr,
            )
    return 0 if result.get("ok") and result.get("ready") else 1


def _render(report: dict) -> str:
    lines = [f"models in {report['directory']}"]
    for model in report["models"]:
        mark = {"ok": "ok      ", "unlocked": "unlocked", "missing": "missing ",
                "mismatched": "CHANGED "}[model["state"]]
        digest = model["sha256"][:16] or "-"
        lines.append(f"  {mark} {model['file']:<28} {digest}  ({model['purpose']})")
    lines.append(
        f"  libraries: {'installed' if report['stack_installed'] else 'NOT installed'}"
    )
    lines.append(f"  recognition {'ready' if report['ready'] else 'not ready'}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
