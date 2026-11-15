#!/usr/bin/env python3
"""Hosted console for the AI Job Search framework.

This is an additive, optional layer. It does not change the Claude Code
workflow, the skills, or the portal CLIs; it puts a browser in front of the
repo's own deterministic tools so you can exercise them without a terminal.

Everything the API returns is computed by the repository's real code:

  * POST /api/guard   -> tools/posting_guard.py   (`guard()`, the integrity
                        gate that strips hidden/invisible content out of a
                        fetched posting before an agent reads it)
  * POST /api/jobkey  -> tools/job_key.py         (`make_key()`, the canonical
                        dedup key for a posting)
  * GET  /api/catalog -> parsed from .claude/commands/*.md,
                        .claude/skills/*/SKILL.md and .agents/skills/*/SKILL.md

Stdlib only: no pip install, no network egress, no build step. Bind to
0.0.0.0 so a container or a preview proxy can reach it.

Usage:
  python3 webapp/server.py [--port 8000] [--host 0.0.0.0]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
STATIC = Path(__file__).resolve().parent / "static"

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import job_key  # noqa: E402  (repo tool, put on sys.path above)
import posting_guard  # noqa: E402

# A posting is text; anything past this is not a posting. Keeps a single
# request from pinning a worker on a pathological payload.
MAX_BODY_BYTES = 512 * 1024
MAX_POSTING_CHARS = 200_000


# --------------------------------------------------------------------------
# Catalog: read the framework's own metadata instead of hard-coding it
# --------------------------------------------------------------------------

def _frontmatter(text: str) -> dict[str, str]:
    """Minimal YAML-frontmatter reader for `key: value` and folded `key: >`."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    out: dict[str, str] = {}
    current: str | None = None
    for line in block.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s+\S", line) and current:
            out[current] = (out[current] + " " + line.strip()).strip()
            continue
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value in (">", "|", ">-", "|-"):
            value = ""
        out[key] = value.strip("\"'")
        current = key
    return out


def _first_sentence(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    if not text:
        return ""
    # Trigger-phrase lists make frontmatter descriptions unreadable in a card;
    # cut at the first real sentence boundary.
    match = re.match(r"(.{40,}?[.!?])(?:\s|$)", text)
    sentence = match.group(1) if match else text
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip(" ,;:") + "…"
    return sentence


@lru_cache(maxsize=1)
def catalog() -> dict:
    commands = []
    for path in sorted((ROOT / ".claude" / "commands").glob("*.md")):
        heading = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                heading = line[2:].strip()
                break
        # "# /apply - Drafter-Reviewer Job Application Workflow"
        match = re.match(r"^(/\S+)\s*[-–]\s*(.+)$", heading)
        name, summary = (match.group(1), match.group(2)) if match else (path.stem, heading)
        commands.append({"name": name, "summary": summary, "file": str(path.relative_to(ROOT))})

    def skill_entry(path: Path, kind: str) -> dict | None:
        meta = _frontmatter(path.read_text(encoding="utf-8"))
        if not meta.get("name"):
            return None
        entry = {
            "name": meta["name"],
            "description": _first_sentence(meta.get("description", "")),
            "kind": kind,
            "file": str(path.relative_to(ROOT)),
        }
        if meta.get("version"):
            entry["version"] = meta["version"]
        return entry

    skills = [
        e
        for p in sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))
        if (e := skill_entry(p, "skill"))
    ]
    portals = [
        e
        for p in sorted((ROOT / ".agents" / "skills").glob("*/SKILL.md"))
        if (e := skill_entry(p, "portal"))
    ]

    for portal in portals:
        manifest = ROOT / portal["file"].replace("SKILL.md", "cli/package.json")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            portal["version"] = str(data.get("version", portal.get("version", "")))
        except (OSError, ValueError):
            pass

    return {
        "commands": commands,
        "skills": skills,
        "portals": portals,
        "counts": {
            "commands": len(commands),
            "skills": len(skills),
            "portals": len(portals),
        },
    }


# --------------------------------------------------------------------------
# API handlers
# --------------------------------------------------------------------------

def run_guard(payload: dict) -> dict:
    raw = payload.get("raw", "")
    if not isinstance(raw, str):
        raise ValueError("'raw' must be a string")
    if not raw.strip():
        raise ValueError("nothing to scan - paste a posting first")
    if len(raw) > MAX_POSTING_CHARS:
        raise ValueError(f"posting is {len(raw):,} chars; the limit is {MAX_POSTING_CHARS:,}")

    mode = payload.get("mode", "auto")
    if mode not in ("auto", "html", "text"):
        raise ValueError("'mode' must be one of: auto, html, text")

    cleaned, findings = posting_guard.guard(raw, mode)
    flags = {
        "instruction_like": len(findings.get("instruction_like", [])),
        "hidden_blocks": len(findings.get("hidden_blocks", [])),
        "hidden_urls": len(findings.get("hidden_urls", [])),
        "homoglyph_runs": len(findings.get("homoglyph_runs", [])),
        "invisible_text": len(findings.get("invisible_text", [])),
        "invisible_flagged": findings.get("invisible_flagged", 0),
    }
    return {
        "verdict": findings.get("verdict", ""),
        "flagged": sum(flags[k] for k in ("instruction_like", "hidden_urls", "homoglyph_runs", "invisible_text", "invisible_flagged")) > 0,
        "flags": flags,
        "findings": findings,
        "cleaned": cleaned,
        "stats": {
            "input_chars": len(raw),
            "cleaned_chars": len(cleaned),
            "removed_chars": max(0, len(raw) - len(cleaned)),
            "mode": mode,
        },
    }


def run_jobkey(payload: dict) -> dict:
    company = str(payload.get("company", "")).strip()
    title = str(payload.get("title", "")).strip()
    url = str(payload.get("url", "")).strip()
    if not company and not title:
        raise ValueError("give at least a company or a title")
    key = job_key.make_key(company, title, url)
    return {
        "key": key,
        "canonical": job_key.is_canonical(key),
        "legacy_shape": job_key.is_legacy_shape(key),
        "length": len(key),
    }


ROUTES = {
    "/api/guard": run_guard,
    "/api/jobkey": run_jobkey,
}


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "AIJobSearchConsole/1.0"

    def log_message(self, fmt: str, *args) -> None:  # quieter, structured-ish logs
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _serve_static(self, rel: str) -> None:
        target = (STATIC / rel).resolve()
        # Static files live in exactly one directory; refuse to walk out of it.
        if not target.is_file() or STATIC.resolve() not in target.parents and target != STATIC.resolve():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(target.suffix, "application/octet-stream")
        self._send(HTTPStatus.OK, target.read_bytes(), ctype)

    # -- verbs ------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True, "service": "ai-job-search-console"})
            return
        if path == "/api/catalog":
            try:
                self._json(HTTPStatus.OK, catalog())
            except Exception:
                traceback.print_exc()
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "could not read the framework catalog")
            return
        if path == "/api/jobkey":
            # Convenience GET mirror for curl/deep links.
            try:
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                self._json(HTTPStatus.OK, run_jobkey(query))
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        handler = ROUTES.get(path)
        if handler is None:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "bad Content-Length")
            return
        if length <= 0:
            self._error(HTTPStatus.BAD_REQUEST, "empty request body")
            return
        if length > MAX_BODY_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "body must be JSON")
            return
        if not isinstance(payload, dict):
            self._error(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
            return

        try:
            self._json(HTTPStatus.OK, handler(payload))
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "the tool failed on that input")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hosted console for the AI Job Search framework.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    args = parser.parse_args(argv)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    counts = catalog()["counts"]
    print(
        f"AI Job Search console on http://{args.host}:{args.port}  "
        f"({counts['commands']} commands, {counts['portals']} portal CLIs)",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
