"""Build dist/index.html from src/template.html + a data source.

Default source is ``data/sample.json`` (canned demo data). Production cron
points at a real quant-bot project root via ``--from quant-bot --root <path>``.

Adapter failure NEVER fails the cron: we log the warning, fall back to
``data/sample.json``, and exit 0.

If ``TOMCASH_STRICT=1`` is set, any *unexpected* exception (i.e. a real bug,
not a known AdapterError) will cause a non-zero exit so CI can fail loudly.
The default behavior remains "stay green" so the scheduled cron never
breaks the build.

Usage:
  python scripts/build.py
  python scripts/build.py --from sample --also-demo
  python scripts/build.py --from quant-bot --root /abs/path/to/quant-bot
  python scripts/build.py --from quant-bot --root /abs/path --mode DAY0
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "src" / "template.html"
DIST = ROOT / "dist"
DEFAULT_DATA = ROOT / "data" / "sample.json"
DAY0_DATA = ROOT / "data" / "day0_sample.json"
FONTS_DIR = DIST / "fonts"
FONTS_CSS = DIST / "fonts.css"

# Google Fonts gstatic woff2 URLs for the latin subset of the variable fonts
# we use. Verified from the Google Fonts CSS API. Pinned versions so a CDN
# revision doesn't quietly invalidate our local cache. See _fetch_fonts().
FONT_SOURCES: list[dict[str, str]] = [
    {
        "name": "inter",
        "family": "Inter",
        "weight_range": "100 900",
        "style": "normal",
        "url": "https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa1ZL7W0Q5nw.woff2",
    },
    {
        "name": "fraunces",
        "family": "Fraunces",
        "weight_range": "100 900",
        "style": "normal",
        "url": "https://fonts.gstatic.com/s/fraunces/v38/6NU78FyLNQOQZAnv9bYEvDiIdE9Ea92uemAk_WBq8U_9v0c2Wa0KxC9TeP2Xz5c.woff2",
    },
    {
        "name": "jetbrainsmono",
        "family": "JetBrains Mono",
        "weight_range": "100 800",
        "style": "normal",
        "url": "https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbV2o-flEEny0FZhsfKu5WU4xD7OwGtT0rU.woff2",
    },
]

# Make repo root importable so ``adapters.quant_bot`` resolves as a package.
sys.path.insert(0, str(ROOT))

log = logging.getLogger("lendawgbot.build")


# Self-hosted fonts (Path B). _fetch_fonts() runs at build time, downloads
# the latin-subset variable woff2 files to dist/fonts/, and writes
# dist/fonts.css with local @font-face rules. The template references
# ./fonts.css; the page is then truly self-contained (no third-party runtime
# dependencies). On network failure we fall back to the Google Fonts CDN —
# the build never breaks. Disable with --no-inline-fonts.


def _download_font(url: str, dest: Path, *, timeout: float = 10.0) -> tuple[int, bool]:
    """Download `url` to `dest` and return (bytes_on_disk, fetched).

    Sends a desktop User-Agent so Google Fonts serves woff2 (older clients
    get legacy formats). Cached: if `dest` already exists and is non-empty,
    we keep it and return ``fetched=False``.
    """
    if dest.exists() and dest.stat().st_size > 0:
        return dest.stat().st_size, False
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return len(body), True


def _build_font_css(fonts: list[dict[str, str]]) -> str:
    """Build the local @font-face CSS pointing at ./fonts/<name>.woff2."""
    rules: list[str] = []
    for f in fonts:
        rules.append(
            "@font-face {\n"
            f"  font-family: '{f['family']}';\n"
            f"  font-style: {f['style']};\n"
            f"  font-weight: {f['weight_range']};\n"
            "  font-display: swap;\n"
            f"  src: url('./fonts/{f['name']}.woff2') format('woff2-variations'),\n"
            f"       url('./fonts/{f['name']}.woff2') format('woff2');\n"
            "}"
        )
    return "\n".join(rules) + "\n"


def _fetch_fonts(out_dir: Path = DIST) -> tuple[bool, list[str]]:
    """Download woff2 files and write `<out_dir>/fonts.css`.

    Returns ``(ok, warnings)``. ``ok=False`` means at least one download
    failed and the caller should keep the Google Fonts CDN <link> tags so
    the page still renders. Network failure NEVER breaks the build.
    """
    warnings: list[str] = []
    fonts_dir = out_dir / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    fetched = 0
    cached = 0
    for f in FONT_SOURCES:
        dest = fonts_dir / f"{f['name']}.woff2"
        try:
            n, did_fetch = _download_font(f["url"], dest)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            msg = f"font fetch failed for {f['name']} ({e}); falling back to Google Fonts CDN"
            log.warning(msg)
            warnings.append(msg)
            return False, warnings
        total_bytes += n
        if did_fetch:
            fetched += 1
        else:
            cached += 1

    css = _build_font_css(FONT_SOURCES)
    (out_dir / "fonts.css").write_text(css)
    print(
        f"Fonts: {len(FONT_SOURCES)} files, {total_bytes / 1024:.1f} KB total "
        f"({fetched} downloaded, {cached} cached) -> {fonts_dir.relative_to(ROOT)}"
    )
    return True, warnings


def _minify_html(s: str) -> str:
    """Minimal whitespace-collapsing minifier.

    Preserves <pre>, <textarea>, and <script type="application/json">
    contents byte-for-byte. We do NOT touch CSS or JS bodies inside generic
    <style> / <script> blocks beyond collapsing whitespace runs - a heavier
    minifier would need a real parser. Strips HTML comments (except IE
    conditionals), collapses inter-tag whitespace, squashes blank lines.
    """
    placeholders: list[str] = []

    def stash(m: "re.Match[str]") -> str:
        placeholders.append(m.group(0))
        return f"\x00\x00{len(placeholders) - 1}\x00\x00"

    # Pull out anything we must not touch. Order: script-json before any
    # generic match so the placeholder swallows the whole tag.
    s = re.sub(
        r'<script[^>]*type="application/json"[^>]*>.*?</script>',
        stash, s, flags=re.DOTALL,
    )
    s = re.sub(r'<pre[^>]*>.*?</pre>', stash, s, flags=re.DOTALL)
    s = re.sub(r'<textarea[^>]*>.*?</textarea>', stash, s, flags=re.DOTALL)

    # Remove HTML comments (but not IE conditional `<!--[if`)
    s = re.sub(r'<!--(?!\[if).*?-->', '', s, flags=re.DOTALL)
    # Collapse whitespace between tags
    s = re.sub(r'>\s+<', '><', s)
    # Collapse runs of spaces/tabs inside a line
    s = re.sub(r'[ \t]+', ' ', s)
    # Remove blank lines
    s = re.sub(r'\n\s*\n', '\n', s)

    # Restore stashed sections
    s = re.sub(
        r'\x00\x00(\d+)\x00\x00',
        lambda m: placeholders[int(m.group(1))],
        s,
    )
    return s


def _provenance_for(
    source: str,
    source_detail: str,
    data_as_of: str | None,
    variant: str,
) -> dict[str, Any]:
    """Build the `_provenance` dict the renderer reads to draw the bar.

    - `source` is one of "quant-bot", "sample", "sample-fallback", "day0_sample".
    - `source_detail` is a short human-readable identifier of the actual
      file/path (e.g. "verdict_2026-04-25.json", "data/sample.json").
    - `data_as_of` is the ISO timestamp of when the underlying data was
      written (NOT when this HTML was assembled). Falls back to None.
    - `variant` is "real" or "demo" — mirrors DATA.dashboard_variant so the
      provenance bar can pick DEMO copy without consulting the toggle.
    """
    return {
        "source": source,
        "source_detail": source_detail,
        "data_as_of": data_as_of,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": (os.environ.get("GITHUB_SHA", "")[:7] or None),
        "variant": variant,
    }


# Regex matching the Google Fonts <link> tags in template.html. Used when
# self-hosting fonts to strip the CDN refs and inject local ones in their
# place.
_GOOGLE_FONTS_BLOCK_RE = re.compile(
    r'<link[^>]+fonts\.googleapis\.com[^>]*/?>'
    r'|<link[^>]+fonts\.gstatic\.com[^>]*/?>'
)


def _swap_to_local_fonts(html: str) -> str:
    """Replace the Google Fonts <link>s with local refs + inter preload.

    The template ships with Google Fonts CDN <link>s by default (so the page
    still renders if --no-inline-fonts is passed or fetch fails). When
    _fetch_fonts() succeeds, we strip those tags and inject our local refs.
    """
    replacement = (
        '<link rel="preload" href="./fonts/inter.woff2" as="font" '
        'type="font/woff2" crossorigin />'
        '<link rel="preload" href="./fonts.css" as="style" />'
        '<link rel="stylesheet" href="./fonts.css" />'
    )
    cleaned = _GOOGLE_FONTS_BLOCK_RE.sub("", html)
    if "</head>" in cleaned:
        cleaned = cleaned.replace("</head>", replacement + "</head>", 1)
    return cleaned


def _render(data: dict[str, Any], output_path: Path, source_label: str, mode_label: str,
            variant: str = "real", *, minify: bool = True,
            local_fonts: bool = False) -> None:
    """Embed `data` into the template and write to `output_path`.

    `variant` is either "real" (index.html — current quant-bot truth) or
    "demo" (demo.html — fabricated showcase). The template's header toggle
    reads this off DATA.dashboard_variant to decide which segment is active.

    When `local_fonts=True`, the Google Fonts CDN <link>s are stripped and
    replaced with refs to ./fonts.css (written by _fetch_fonts()).
    """
    if not TEMPLATE.exists():
        sys.exit(f"Template not found: {TEMPLATE}")

    template = TEMPLATE.read_text()
    # Refresh the timestamp so the dashboard reflects build time.
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    data["dashboard_variant"] = variant

    if local_fonts:
        template = _swap_to_local_fonts(template)

    payload = (
        json.dumps(data, separators=(",", ":"), default=str)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    output = template.replace("__DATA_JSON__", payload)

    pre_bytes = len(output.encode("utf-8"))
    if minify:
        output = _minify_html(output)
    post_bytes = len(output.encode("utf-8"))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output)
    size_kb = output_path.stat().st_size / 1024

    if minify:
        saved = pre_bytes - post_bytes
        pct = (saved / pre_bytes * 100) if pre_bytes else 0.0
        print(
            f"Built {output_path} ({size_kb:.1f} KB) from {source_label} "
            f"[mode={mode_label}] minify: {pre_bytes} -> {post_bytes} bytes "
            f"(-{saved}, -{pct:.1f}%)"
        )
    else:
        print(
            f"Built {output_path} ({size_kb:.1f} KB) from {source_label} "
            f"[mode={mode_label}] minify: OFF"
        )


def _write_status(out_dir: Path, *, ok: bool, source: str, mode: str,
                  data_as_of: str | None, warnings: list[str], errors: list[str],
                  publisher: str = "local") -> None:
    """Emit dist/_status.json for external monitors.

    This is the SINGLE source of truth for the heartbeat file. No workflow
    is allowed to write _status.json inline anymore — both pages.yml and
    deploy.yml must call this function via build.py and then run a
    verification step against the file shape.

    Schema contract (`schema_version`):
      Bumping `schema_version` is a BREAKING CHANGE. Any monitor consumer
      keying off field names must be updated in lockstep. Today's consumers:
        - `.github/workflows/pages.yml`     (Verify status step)
        - `.github/workflows/deploy.yml`    (Verify variants step)
      If you add/rename a top-level field, bump `schema_version` and grep
      for `schema_version==` across the repo before merging.
    """
    now = datetime.now(timezone.utc)
    data_age_hours = None
    if data_as_of:
        try:
            d = datetime.fromisoformat(data_as_of.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            data_age_hours = round((now - d).total_seconds() / 3600, 2)
        except Exception:
            pass

    run_id = os.environ.get("GITHUB_RUN_ID")
    run_url = None
    if run_id:
        run_url = (
            os.environ.get("GITHUB_SERVER_URL", "")
            + "/" + os.environ.get("GITHUB_REPOSITORY", "")
            + "/actions/runs/" + run_id
        )

    payload = {
        "ok": ok,
        "schema_version": 1,
        "publisher": publisher,                 # github-pages | cloudflare-pages | local
        "last_build_at": now.isoformat(),
        "last_build_source": source,            # quant-bot | sample | sample-fallback
        "last_build_commit": os.environ.get("GITHUB_SHA", "")[:8],
        "last_build_run_url": run_url,
        "mode": mode,
        "data_as_of": data_as_of,
        "data_age_hours": data_age_hours,
        "warnings": warnings,
        "errors": errors,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_status.json").write_text(json.dumps(payload, indent=2, default=str))


def _load_sample() -> tuple[dict[str, Any], str]:
    """Load canned sample JSON. Raises SystemExit if it's missing."""
    if not DEFAULT_DATA.exists():
        sys.exit(f"Data not found: {DEFAULT_DATA}")
    return json.loads(DEFAULT_DATA.read_text()), "sample"


def _quant_bot_source_detail(root: Path, mode_label: str) -> tuple[str, str | None]:
    """Best-effort inspection of which artifact the adapter actually used.

    Returns (source_detail, data_as_of_iso). `data_as_of_iso` is the file
    mtime of the source artifact. The adapter API doesn't surface the
    chosen path, so we re-detect using the same rules as detect_mode().
    """
    detail = "quant-bot"
    data_as_of: str | None = None

    if mode_label == "DAY0":
        day0_dir = root / "journal" / "day0"
        if day0_dir.is_dir():
            candidates = list(day0_dir.glob("verdict_*.json"))
            if candidates:
                latest = max(candidates, key=lambda p: p.stat().st_mtime)
                detail = latest.name
                data_as_of = datetime.fromtimestamp(
                    latest.stat().st_mtime, tz=timezone.utc
                ).isoformat()
    else:  # LIVE
        # Trades parquet is the truth for cumulative numbers; the live jsonl
        # is the heartbeat. Prefer the parquet's mtime for "as of" since
        # that's what drives the hero number.
        trades_path = root / "data" / "processed" / "trades.parquet"
        if trades_path.exists():
            detail = "data/processed/trades.parquet"
            data_as_of = datetime.fromtimestamp(
                trades_path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        else:
            live_dir = root / "journal" / "live"
            if live_dir.is_dir():
                live_files = sorted(live_dir.glob("*.jsonl"), reverse=True)
                if live_files:
                    detail = f"journal/live/{live_files[0].name}"
                    data_as_of = datetime.fromtimestamp(
                        live_files[0].stat().st_mtime, tz=timezone.utc
                    ).isoformat()

    return detail, data_as_of


def _load_from_quant_bot(
    root: Path, mode: str, *, warnings: list[str], errors: list[str]
) -> tuple[dict[str, Any], str, str]:
    """Run the adapter; on failure fall back to sample.

    Returns (data, source, mode). Mutates the passed-in `warnings` / `errors`
    lists so the caller can reflect the outcome in dist/_status.json.

    - AdapterError → known/expected failure → warning, sample-fallback, ok=true
    - Anything else → real bug → error, sample-fallback, ok=false
    """
    from adapters.quant_bot import AdapterError, build_dashboard  # local import

    try:
        data = build_dashboard(root, mode=mode)  # type: ignore[arg-type]
        return data, "quant-bot", str(data.get("mode", mode))
    except AdapterError as e:
        msg = f"adapter failed ({e}); falling back to sample.json"
        log.warning(msg)
        warnings.append(str(e))
        sample, _ = _load_sample()
        return sample, "sample-fallback", str(sample.get("mode", "LIVE"))
    except Exception as e:
        # Real bug — record an error and (optionally) fail loudly. Strict mode
        # is opt-in so the scheduled cron stays green by default while CI on a
        # PR can flip TOMCASH_STRICT=1 to surface regressions.
        msg = f"adapter raised unexpectedly ({e!r}); falling back to sample.json"
        log.error(msg)
        errors.append(repr(e))
        sample, _ = _load_sample()
        return sample, "sample-fallback", str(sample.get("mode", "LIVE"))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Build TomCash dashboard")
    parser.add_argument("--from", dest="source", choices=["sample", "quant-bot"],
                        default="sample", help="Data source (default: sample)")
    parser.add_argument("--root", type=Path, help="quant-bot project root (required if --from quant-bot)")
    parser.add_argument("--mode", choices=["AUTO", "LIVE", "DAY0"], default="AUTO",
                        help="Mode hint for quant-bot adapter (default: AUTO)")
    parser.add_argument("--day0", action="store_true",
                        help="(legacy) shortcut for --from sample using day0_sample.json")
    parser.add_argument("--data", type=Path, help="Custom data file path (bypasses --from)")
    parser.add_argument("--out", type=Path, default=DIST / "index.html", help="Output path")
    parser.add_argument("--also-demo", action="store_true",
                        help="Additionally build dist/demo.html from data/sample.json")
    parser.add_argument("--no-minify", dest="minify", action="store_false",
                        default=True,
                        help="Skip the HTML whitespace minify pass (debug aid)")
    parser.add_argument("--inline-fonts", dest="inline_fonts", action="store_true",
                        default=True,
                        help="Self-host fonts: download woff2 to dist/fonts/ "
                             "and rewrite <link>s (default ON)")
    parser.add_argument("--no-inline-fonts", dest="inline_fonts", action="store_false",
                        help="Disable font self-hosting; keep Google Fonts CDN")
    parser.add_argument("--publisher", default="local",
                        help="Identifies which deploy system built this "
                             "(github-pages | cloudflare-pages | local). "
                             "Embedded in dist/_status.json so monitors can "
                             "tell which pipeline produced the artifact.")
    args = parser.parse_args()

    warnings: list[str] = []
    errors: list[str] = []

    # Self-host fonts when requested. On any failure (no network, etc.) we
    # fall back to Google Fonts CDN so the build still succeeds.
    local_fonts = False
    if args.inline_fonts:
        out_dir = args.out.parent if args.out else DIST
        ok, font_warnings = _fetch_fonts(out_dir)
        warnings.extend(font_warnings)
        local_fonts = ok
        if not ok:
            log.warning("font self-hosting disabled for this build; using Google Fonts CDN")

    # Resolve data + source label + a human-readable source_detail for the
    # provenance bar. data_as_of is the timestamp of the *underlying* file
    # (mtime) — distinct from generated_at (assembly time).
    source_detail: str
    data_as_of_for_prov: str | None = None
    if args.data:
        if not args.data.exists():
            sys.exit(f"Data not found: {args.data}")
        data: dict[str, Any] = json.loads(args.data.read_text())
        source_label = args.data.name
        mode_label = str(data.get("mode", "LIVE"))
        source_detail = str(args.data)
        data_as_of_for_prov = datetime.fromtimestamp(
            args.data.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    elif args.day0:
        if not DAY0_DATA.exists():
            sys.exit(f"Data not found: {DAY0_DATA}")
        data = json.loads(DAY0_DATA.read_text())
        source_label = "day0_sample"
        mode_label = "DAY0"
        source_detail = "data/day0_sample.json"
        data_as_of_for_prov = datetime.fromtimestamp(
            DAY0_DATA.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    elif args.source == "quant-bot":
        if not args.root:
            sys.exit("--from quant-bot requires --root <path>")
        data, source_label, mode_label = _load_from_quant_bot(
            args.root, args.mode, warnings=warnings, errors=errors,
        )
        if source_label == "quant-bot":
            source_detail, data_as_of_for_prov = _quant_bot_source_detail(
                args.root, mode_label
            )
        else:
            # Adapter fell back; we're now reading data/sample.json.
            source_detail = "data/sample.json"
            data_as_of_for_prov = datetime.fromtimestamp(
                DEFAULT_DATA.stat().st_mtime, tz=timezone.utc
            ).isoformat()
    else:
        data, source_label = _load_sample()
        mode_label = str(data.get("mode", "LIVE"))
        source_detail = "data/sample.json"
        data_as_of_for_prov = datetime.fromtimestamp(
            DEFAULT_DATA.stat().st_mtime, tz=timezone.utc
        ).isoformat()

    # Attach provenance for the renderer. Stays None-safe: the template only
    # renders the bar when DATA._provenance is present.
    data["_provenance"] = _provenance_for(
        source=source_label,
        source_detail=source_detail,
        data_as_of=data_as_of_for_prov,
        variant="real",
    )

    # Default index.html → "real" variant (whatever the adapter / configured
    # source produced — currently lab/DAY0 since quant-bot hasn't traded yet).
    # demo.html → "demo" variant (fabricated $181 showcase from sample.json).
    _render(data, args.out, source_label, mode_label, variant="real",
            minify=args.minify, local_fonts=local_fonts)

    if args.also_demo:
        demo_data, _ = _load_sample()
        demo_data["_provenance"] = _provenance_for(
            source="sample",
            source_detail="data/sample.json",
            data_as_of=datetime.fromtimestamp(
                DEFAULT_DATA.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            variant="demo",
        )
        _render(demo_data, DIST / "demo.html", "sample (demo)",
                str(demo_data.get("mode", "LIVE")), variant="demo",
                minify=args.minify, local_fonts=local_fonts)

    # Always emit a fresh status file. ok=False means an unexpected exception
    # was caught and swallowed — consumers (monitor, CI assertion) should
    # treat that as a build incident even though the dashboard still rendered
    # from sample-fallback.
    ok = not errors
    out_dir = args.out.parent if args.out else DIST
    data_as_of = data.get("as_of") or data.get("as_of_date") or data.get("generated_at")
    _write_status(
        out_dir,
        ok=ok,
        source=source_label,
        mode=mode_label,
        data_as_of=data_as_of,
        warnings=warnings,
        errors=errors,
        publisher=args.publisher,
    )

    # Strict mode lets PR checks opt into "fail on unexpected exception"
    # without breaking the scheduled cron.
    if errors and os.environ.get("TOMCASH_STRICT") == "1":
        log.error("TOMCASH_STRICT=1 and build had errors; exiting non-zero.")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
