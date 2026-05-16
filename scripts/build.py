#!/usr/bin/env python3
"""Build dist/index.html by inlining data/dashboard.json into src/template.html.

Single argument optional: the path to dashboard.json (default: data/dashboard.json).
The template has a single placeholder, {{DASHBOARD_JSON}}, inside a
<script type="application/json"> tag. The script-tag JSON encoding is
HTML-safe — we still escape any "</script>" sequence defensively.

Writes:
  dist/index.html      — the deployed page
  dist/_status.json    — small machine-readable build status (CI checks this)

Exits non-zero if data/dashboard.json or src/template.html is missing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = ROOT / "data" / "dashboard.json"
TEMPLATE = ROOT / "src" / "template.html"
DIST_DIR = ROOT / "dist"
PLACEHOLDER = "{{DASHBOARD_JSON}}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", default=str(DEFAULT_JSON), help="dashboard.json path")
    p.add_argument("--template", default=str(TEMPLATE), help="template.html path")
    p.add_argument("--out", default=str(DIST_DIR / "index.html"), help="output index.html")
    args = p.parse_args(argv)

    data_path = Path(args.data)
    tpl_path = Path(args.template)

    if not data_path.is_file():
        print(f"build: missing {data_path}", file=sys.stderr)
        return 1
    if not tpl_path.is_file():
        print(f"build: missing {tpl_path}", file=sys.stderr)
        return 1

    with data_path.open("r", encoding="utf-8") as fh:
        dashboard = json.load(fh)

    # Compact JSON keeps the page small. Defense-in-depth: if any value
    # ever contained "</script>" we'd break out of the script tag, so we
    # escape the slash. (Real journal data won't, but better safe.)
    payload = json.dumps(dashboard, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")

    template = tpl_path.read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        print(f"build: template missing placeholder {PLACEHOLDER!r}", file=sys.stderr)
        return 2
    html = template.replace(PLACEHOLDER, payload)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    status = {
        "ok": True,
        "schema_version": 1,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "dashboard_schema_version": dashboard.get("schema_version"),
        "data_as_of": dashboard.get("data_as_of"),
        "bytes": out_path.stat().st_size,
    }
    (out_path.parent / "_status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")

    print(f"build: wrote {out_path} ({status['bytes']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
