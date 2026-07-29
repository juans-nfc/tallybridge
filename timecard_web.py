#!/usr/bin/env python3
"""
Web UI for the timecard file converter.

Two-step by design: you upload a day's packing-line .txt, review exactly what
was read out of it (totals, per-packer breakdown, anything that looks off),
and only then save the payroll workbook. Also lets you convert whatever is
sitting in the incoming folder, so you can drive the whole pipeline by hand
before turning the automatic watcher on.

Uses the same convert() as the headless service, so output is identical.

Run:
    python3 timecard_web.py                 # http://<server>:8080
    python3 timecard_web.py --port 9090 --host 127.0.0.1

Intended for a trusted internal network only — there is no login.
"""

import argparse
import os
import shutil
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from flask import (Flask, flash, redirect, render_template_string, request,
                   send_from_directory, url_for)
from markupsafe import escape
from werkzeug.utils import secure_filename

from timecard_converter import (DEFAULT_OUTPUT_DIR, DEFAULT_TEMPLATE,
                                DEFAULT_WATCH_DIR, FILE_PATTERNS, PACKAGE_CODES,
                                convert, find_candidates, parse_txt,
                                unmapped_codes)

APP_NAME = "TallyBridge"


class PrefixMiddleware:
    """Let the app live under a subpath behind nginx, e.g. /tallybridge.

    Honours the X-Forwarded-Prefix header the proxy sends, falling back to the
    prefix given on the command line. Without this, url_for() would generate
    links rooted at / and every form on the page would 404 behind the proxy.
    """

    def __init__(self, wsgi_app, prefix=""):
        self.wsgi_app = wsgi_app
        self.prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""

    def __call__(self, environ, start_response):
        forwarded = environ.get("HTTP_X_FORWARDED_PREFIX", "")
        prefix = "/" + forwarded.strip("/") if forwarded.strip("/") else self.prefix
        if prefix:
            environ["SCRIPT_NAME"] = prefix
            path = environ.get("PATH_INFO", "")
            if path.startswith(prefix):
                environ["PATH_INFO"] = path[len(prefix):] or "/"
        proto = environ.get("HTTP_X_FORWARDED_PROTO")
        if proto:
            environ["wsgi.url_scheme"] = proto
        return self.wsgi_app(environ, start_response)


app = Flask(__name__)
app.secret_key = "tallybridge-local-ui"  # only used for flash messages
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

CFG = {
    "template": DEFAULT_TEMPLATE,
    "output_dir": DEFAULT_OUTPUT_DIR,
    "watch_dir": DEFAULT_WATCH_DIR,
    "staging_dir": Path("/data/staging"),
}

# SIMS package code carrying the main piece count, used for the headline total.
PIECE_CODE = "555"

PREVIEW_ROW_LIMIT = 400  # cap the on-screen table; totals always cover the whole file


# ---------------------------------------------------------------------------
# Analysis for the preview screen
# ---------------------------------------------------------------------------

def summarize(rows, source_name):
    """Build totals, a per-packer breakdown, and review notes for one file."""
    packers = sorted({r.emp_id for r in rows})
    date_codes = sorted({r.date_code for r in rows})
    code_counts = Counter(r.sims_code for r in rows)

    units_by_code = defaultdict(int)
    per_packer = defaultdict(dict)
    bad_units = []

    for r in rows:
        try:
            val = int(r.units)
        except (TypeError, ValueError):
            bad_units.append(r)
            continue
        units_by_code[r.sims_code] += val
        per_packer[r.emp_id][r.sims_code] = \
            per_packer[r.emp_id].get(r.sims_code, 0) + val

    # --- review notes -----------------------------------------------------
    notes = []
    prefixes = tuple(p.split("*")[0].upper() for p in FILE_PATTERNS)

    if not source_name.upper().startswith(prefixes):
        notes.append(
            f"File name doesn't start with {' or '.join(prefixes)} — "
            "it converts fine here, but the automatic watcher only picks up "
            "those prefixes.")

    if len(date_codes) > 1:
        notes.append(f"{len(date_codes)} different date codes in one file "
                     f"({', '.join(date_codes)}) — normally a file covers a single day.")

    unmapped = unmapped_codes(rows)
    if unmapped:
        notes.append(
            f"No Paycom equivalent for package code(s) {', '.join(unmapped)} — "
            "those rows keep the SIMS code and payroll will likely reject them. "
            "Add the code to PACKAGE_CODES in timecard_converter.py.")

    if bad_units:
        notes.append(f"{len(bad_units)} row(s) have a non-numeric piece count "
                     f"(e.g. packer {bad_units[0].emp_id}: {bad_units[0].units!r}) — "
                     "they import as text exactly as they appear.")

    dupes = [k for k, n in
             Counter((r.emp_id, r.date_code, r.sims_code) for r in rows).items() if n > 1]
    if dupes:
        notes.append(f"{len(dupes)} packer/package-code combination(s) appear more than "
                     f"once (first: packer {dupes[0][0]}, code {dupes[0][2]}) — "
                     "payroll receives both rows.")

    if PIECE_CODE in code_counts:
        missing = [p for p in packers if PIECE_CODE not in per_packer.get(p, {})]
        if missing:
            piece_label = PACKAGE_CODES.get(PIECE_CODE, (PIECE_CODE, ""))[0]
            notes.append(f"{len(missing)} packer(s) have no {piece_label} row: "
                         f"{', '.join(missing[:8])}{' and more' if len(missing) > 8 else ''}.")

    zero = [p for p in packers if per_packer.get(p, {}).get(PIECE_CODE) == 0]
    if zero:
        notes.append(f"{len(zero)} packer(s) show zero pieces on "
                     f"{PACKAGE_CODES.get(PIECE_CODE, (PIECE_CODE, ''))[0]}: "
                     f"{', '.join(zero[:8])}{' and more' if len(zero) > 8 else ''}.")

    # --- code translation table (SIMS -> Paycom, with totals) -------------
    codes_in_order = sorted(code_counts)
    code_rows = []
    for c in codes_in_order:
        paycom, desc = PACKAGE_CODES.get(c, (c, ""))
        code_rows.append({
            "sims": c,
            "paycom": paycom,
            "desc": desc,
            "mapped": c in PACKAGE_CODES,
            "rows": code_counts[c],
            "units": units_by_code.get(c, 0),
        })

    packer_rows = [
        {"emp": p,
         "cells": [per_packer.get(p, {}).get(c) for c in codes_in_order],
         "total": sum(per_packer.get(p, {}).values())}
        for p in packers
    ]

    return {
        "source": source_name,
        "row_count": len(rows),
        "packer_count": len(packers),
        "date_codes": date_codes,
        "codes_in_order": codes_in_order,
        "code_rows": code_rows,
        "grand_total": sum(units_by_code.values()),
        "piece_total": units_by_code.get(PIECE_CODE, 0),
        "piece_label": PACKAGE_CODES.get(PIECE_CODE, (PIECE_CODE, ""))[0],
        "unmapped": unmapped,
        "packer_rows": packer_rows,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

BASE_CSS = """
  :root {
    --ink: #22301F; --paper: #F7F5EE; --leaf: #33682E; --leaf-dark: #274F23;
    --apple: #B3402C; --amber: #8A6414; --stem: #77694B; --mist: #E4E0D2;
    --card: #FFFFFF;
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--paper); color: var(--ink); min-height: 100vh;
    font: 16px/1.55 "Segoe UI", system-ui, -apple-system, Arial, sans-serif; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 0 20px 64px; }
  header { background: var(--leaf); color: #F4F2E7;
    border-bottom: 6px solid var(--leaf-dark); margin-bottom: 30px; }
  .head-inner { max-width: 860px; margin: 0 auto; padding: 24px 20px 18px;
    display: flex; justify-content: space-between; align-items: flex-end;
    gap: 16px; flex-wrap: wrap; }
  h1 { font-family: "Arial Narrow", "Segoe UI", Arial, sans-serif; font-weight: 800;
    font-size: clamp(24px, 4.5vw, 34px); letter-spacing: .14em; text-transform: uppercase; }
  .lines { margin-top: 8px; display: flex; gap: 10px; flex-wrap: wrap; }
  .tag { border: 1.5px solid rgba(244,242,231,.65); padding: 2px 10px; font-size: 12px;
    letter-spacing: .18em; text-transform: uppercase; }
  header a { color: #F4F2E7; font-size: 14px; text-decoration: none;
    border-bottom: 1px solid rgba(244,242,231,.5); }
  .card { background: var(--card); border: 1px solid var(--mist); border-radius: 8px;
    padding: 22px 24px; margin-bottom: 20px; }
  h2 { font-size: 13px; letter-spacing: .16em; text-transform: uppercase;
    color: var(--stem); margin-bottom: 14px; font-weight: 700; }
  p.hint { color: var(--stem); font-size: 14px; margin-top: 10px; }
  input[type=file] { width: 100%; padding: 12px; border: 1.5px dashed var(--mist);
    border-radius: 6px; background: var(--paper); font-size: 14px; }
  .row { display: flex; align-items: center; gap: 14px; margin-top: 14px; flex-wrap: wrap; }
  button { background: var(--leaf); color: #F4F2E7; border: 0; border-radius: 6px;
    padding: 10px 22px; font-size: 15px; font-weight: 600; cursor: pointer; }
  button:hover { background: var(--leaf-dark); }
  button.quiet { background: var(--card); color: var(--leaf); border: 1.5px solid var(--leaf); }
  button.quiet:hover { background: var(--paper); }
  button.plain { background: none; color: var(--stem); border: 0; text-decoration: underline;
    padding: 10px 4px; font-weight: 400; }
  button[disabled] { opacity: .45; cursor: default; }
  button:focus-visible, input:focus-visible, a:focus-visible { outline: 3px solid var(--leaf);
    outline-offset: 2px; }
  .flash { border-radius: 6px; padding: 12px 16px; margin-bottom: 20px; font-size: 15px; }
  .flash.ok { background: #EAF2E6; border: 1px solid #BED4B5; }
  .flash.err { background: #F7E9E6; border: 1px solid #E0B7AE; color: var(--apple); }
  .flash a { color: inherit; font-weight: 700; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; color: var(--stem); font-weight: 600; font-size: 12px;
    letter-spacing: .1em; text-transform: uppercase; padding: 0 10px 8px 0; }
  td { padding: 7px 10px 7px 0; border-top: 1px solid var(--mist); }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums;
    font-family: Consolas, "Courier New", monospace; }
  a.dl { color: var(--leaf); font-weight: 600; text-decoration: none; }
  a.dl:hover { text-decoration: underline; }
  .empty { color: var(--stem); font-size: 14px; padding: 6px 0; }
  td.code, th.code { font-family: Consolas, "Courier New", monospace; }
"""

INDEX_PAGE = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TallyBridge</title><style>""" + BASE_CSS + """</style></head>
<body>
<header><div class="head-inner">
  <div>
    <h1>TallyBridge</h1>
    <div class="lines"><span class="tag">Northern Fruit &mdash; NF</span>
      <span class="tag">Ice Lakes &mdash; IL</span></div>
  </div>
</div></header>
<div class="wrap">

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for cat, msg in messages %}<div class="flash {{ cat }}">{{ msg | safe }}</div>{% endfor %}
  {% endwith %}

  <div class="card">
    <h2>Check a file</h2>
    <form method="post" action="{{ url_for('preview_upload') }}" enctype="multipart/form-data">
      <input type="file" name="txtfile" accept=".txt,text/plain" required>
      <div class="row"><button type="submit">Read the file</button></div>
    </form>
    <p class="hint">Pick a day's packing-line file, for example NF-Y2026M02D05.txt.
       You'll see the piece counts and anything worth a second look before the
       workbook gets written.</p>
  </div>

  <div class="card">
    <h2>Incoming folder</h2>
    {% if pending %}
      <p>{{ pending | length }} file{{ '' if pending|length == 1 else 's' }} waiting:
         {{ pending | join(', ') }}</p>
    {% else %}
      <p class="empty">No files waiting in {{ watch_dir }}.</p>
    {% endif %}
    <form method="post" action="{{ url_for('process_incoming') }}" class="row">
      <button type="submit" class="quiet" {{ '' if pending else 'disabled' }}>
        Convert waiting files</button>
    </form>
    <p class="hint">Converts everything in the folder at once, no preview, and archives
       the sources &mdash; the same thing the automatic service does. Use it to test the
       folder hand-off, or to catch up if the service was stopped.</p>
  </div>

  <div class="card">
    <h2>Package code mapping</h2>
    <table>
      <tr><th>SIMS</th><th>Paycom</th><th>Pack style</th></tr>
      {% for sims, pair in mapping %}
      <tr><td class="code">{{ sims }}</td><td class="code"><strong>{{ pair[0] }}</strong></td>
        <td>{{ pair[1] }}</td></tr>
      {% endfor %}
    </table>
    <p class="hint">Column F of every workbook carries the Paycom code. To add a pack
       style, edit PACKAGE_CODES in timecard_converter.py and rebuild.</p>
  </div>

  <div class="card">
    <h2>Converted files</h2>
    {% if recent %}
    <table>
      <tr><th>File</th><th>Created</th><th class="num">Size</th><th></th></tr>
      {% for f in recent %}
      <tr><td>{{ f.name }}</td><td>{{ f.when }}</td><td class="num">{{ f.size }}</td>
        <td><a class="dl" href="{{ url_for('download', filename=f.name) }}">Download</a></td></tr>
      {% endfor %}
    </table>
    {% else %}
      <p class="empty">Nothing converted yet. Saved workbooks show up here to download.</p>
    {% endif %}
  </div>

</div></body></html>
"""

PREVIEW_PAGE = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ s.source }} &mdash; TallyBridge</title><style>""" + BASE_CSS + """
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 2px; background: var(--mist); border: 1px solid var(--mist); border-radius: 8px;
    overflow: hidden; margin-bottom: 20px; }
  .stat { background: var(--card); padding: 16px 18px; }
  .stat .k { font-size: 11px; letter-spacing: .14em; text-transform: uppercase;
    color: var(--stem); font-weight: 700; }
  .stat .v { font-family: Consolas, "Courier New", monospace; font-size: 26px;
    font-variant-numeric: tabular-nums; margin-top: 4px; }
  .notes { background: #FBF4E2; border: 1px solid #E6D3A3; border-left: 5px solid var(--amber);
    border-radius: 6px; padding: 16px 20px; margin-bottom: 20px; }
  .notes h3 { font-size: 13px; letter-spacing: .12em; text-transform: uppercase;
    color: var(--amber); margin-bottom: 8px; }
  .notes li { font-size: 14px; margin: 5px 0 0 4px; }
  .clean { background: #EAF2E6; border: 1px solid #BED4B5; border-left: 5px solid var(--leaf);
    border-radius: 6px; padding: 14px 20px; margin-bottom: 20px; font-size: 15px; }
  .scroll { max-height: 460px; overflow: auto; border: 1px solid var(--mist); border-radius: 6px; }
  .scroll table { font-size: 13px; }
  .scroll th { position: sticky; top: 0; background: var(--card); padding: 10px 12px 8px;
    box-shadow: inset 0 -1px 0 var(--mist); }
  .scroll td { padding: 6px 12px; }
  .actions { display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    border-top: 1px solid var(--mist); padding-top: 18px; margin-top: 18px; }
  .filerow { display: flex; justify-content: space-between; align-items: baseline;
    gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
  .filerow .src { font-family: Consolas, "Courier New", monospace; font-size: 18px; }
  .filerow .dest { color: var(--stem); font-size: 14px; }
  td.code, th.code { font-family: Consolas, "Courier New", monospace; }
  td.arrow { color: var(--stem); padding-right: 0; width: 18px; }
  td.was { color: var(--stem); font-size: 12.5px; }
  tr.unmapped td { background: #FBF4E2; }
  tr.unmapped td.arrow { color: var(--amber); font-weight: 700; }
</style></head>
<body>
<header><div class="head-inner">
  <div><h1>TallyBridge</h1><div class="lines"><span class="tag">Check before saving</span></div></div>
  <a href="{{ url_for('index') }}">Start over</a>
</div></header>
<div class="wrap">

  <div class="filerow">
    <span class="src">{{ s.source }}</span>
    <span class="dest">will be saved as {{ out_name }}</span>
  </div>

  <div class="stats">
    <div class="stat"><div class="k">Rows</div><div class="v">{{ s.row_count }}</div></div>
    <div class="stat"><div class="k">Packers</div><div class="v">{{ s.packer_count }}</div></div>
    <div class="stat"><div class="k">Date code</div>
      <div class="v" style="font-size:18px">{{ s.date_codes | join(', ') }}</div></div>
    <div class="stat"><div class="k">Pieces ({{ s.piece_label }})</div>
      <div class="v">{{ "{:,}".format(s.piece_total) }}</div></div>
  </div>

  {% if s.notes %}
  <div class="notes">
    <h3>Worth a look</h3>
    <ul>{% for n in s.notes %}<li>{{ n }}</li>{% endfor %}</ul>
  </div>
  {% else %}
  <div class="clean">Nothing unusual found &mdash; {{ s.row_count }} rows across
     {{ s.packer_count }} packers, all earning codes recognized.</div>
  {% endif %}

  <div class="card">
    <h2>Package codes translated for Paycom</h2>
    <table>
      <tr><th>SIMS</th><th></th><th>Paycom</th><th>Pack style</th>
        <th class="num">Rows</th><th class="num">Units</th></tr>
      {% for c in s.code_rows %}
      <tr{% if not c.mapped %} class="unmapped"{% endif %}>
        <td class="code">{{ c.sims }}</td>
        <td class="arrow">{% if c.mapped %}&rarr;{% else %}&#9888;{% endif %}</td>
        <td class="code"><strong>{{ c.paycom }}</strong></td>
        <td>{{ c.desc if c.mapped else 'no Paycom equivalent' }}</td>
        <td class="num">{{ c.rows }}</td>
        <td class="num">{{ "{:,}".format(c.units) }}</td></tr>
      {% endfor %}
      <tr><td colspan="4"><strong>All codes</strong></td>
        <td class="num">{{ s.row_count }}</td>
        <td class="num"><strong>{{ "{:,}".format(s.grand_total) }}</strong></td></tr>
    </table>
    <p class="hint">Column F of the workbook gets the Paycom code, not the SIMS code.
       Compare the unit totals against the line's own day-end numbers &mdash; if they
       match, the file read correctly.</p>
  </div>

  <div class="card">
    <h2>By packer</h2>
    <div class="scroll"><table>
      <tr><th>Employee ID</th>
        {% for c in s.code_rows %}<th class="num">{{ c.paycom }}</th>{% endfor %}
        <th class="num">Total</th></tr>
      {% for p in s.packer_rows %}
      <tr><td>{{ p.emp }}</td>
        {% for v in p.cells %}<td class="num">{{ "{:,}".format(v) if v is not none else "&mdash;" | safe }}</td>{% endfor %}
        <td class="num">{{ "{:,}".format(p.total) }}</td></tr>
      {% endfor %}
    </table></div>
  </div>

  <div class="card">
    <h2>Exactly what goes in the workbook</h2>
    <div class="scroll"><table>
      <tr><th>A &mdash; Employee ID</th><th>C &mdash; Date</th>
        <th>F &mdash; Earning Code</th><th class="num">I &mdash; Allocation</th>
        <th>N &mdash; Units</th><th>from SIMS</th></tr>
      {% for r in rows %}
      <tr{% if not r.mapped %} class="unmapped"{% endif %}>
        <td>{{ r.emp_id }}</td><td>{{ r.date_code }}</td>
        <td class="code"><strong>{{ r.paycom_code }}</strong></td>
        <td class="num">{{ r.alloc }}</td><td>{{ r.units }}</td>
        <td class="was">{{ r.sims_code }}{% if r.description %} &middot; {{ r.description }}{% endif %}</td></tr>
      {% endfor %}
    </table></div>
    {% if truncated %}
    <p class="hint">Showing the first {{ rows | length }} of {{ s.row_count }} rows.
       All {{ s.row_count }} are saved.</p>
    {% endif %}

    <div class="actions">
      <form method="post" action="{{ url_for('save', token=token) }}">
        <button type="submit">Save workbook</button>
      </form>
      <form method="post" action="{{ url_for('save', token=token) }}">
        <input type="hidden" name="csv" value="on">
        <button type="submit" class="quiet">Save workbook + import CSV</button>
      </form>
      <form method="post" action="{{ url_for('discard', token=token) }}">
        <button type="submit" class="plain">Discard</button>
      </form>
    </div>
  </div>

</div></body></html>
"""


# ---------------------------------------------------------------------------
# Staging helpers
# ---------------------------------------------------------------------------

def staging_slot(token):
    """Return the per-upload staging directory for a token, or None if the
    token isn't a bare hex id (guards against path traversal)."""
    if not token or not token.isalnum() or len(token) != 32:
        return None
    return CFG["staging_dir"] / token


def staged_path(token):
    """Return the staged .txt for a token, or None.

    The file keeps its original name inside a token-named folder, so the
    workbook that convert() produces is named after the source file.
    """
    slot = staging_slot(token)
    if not slot or not slot.is_dir():
        return None
    matches = list(slot.glob("*.txt"))
    return matches[0] if matches else None


def clear_slot(token):
    slot = staging_slot(token)
    if slot and slot.is_dir():
        shutil.rmtree(slot, ignore_errors=True)


def prune_staging(max_age_hours=12):
    """Drop staged uploads nobody came back to save."""
    if not CFG["staging_dir"].is_dir():
        return
    cutoff = time.time() - max_age_hours * 3600
    for slot in CFG["staging_dir"].iterdir():
        if slot.is_dir() and slot.stat().st_mtime < cutoff:
            shutil.rmtree(slot, ignore_errors=True)


def human_size(n):
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB"


def list_recent(limit=15):
    out = CFG["output_dir"]
    if not out.exists():
        return []
    files = sorted((p for p in out.iterdir() if p.suffix.lower() in (".xlsx", ".csv")),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [{"name": p.name,
             "when": time.strftime("%m/%d/%Y %H:%M", time.localtime(p.stat().st_mtime)),
             "size": human_size(p.stat().st_size)} for p in files]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    pending = sorted(p.name for p in find_candidates(CFG["watch_dir"])) \
        if CFG["watch_dir"].exists() else []
    return render_template_string(INDEX_PAGE, recent=list_recent(), pending=pending,
                                  watch_dir=CFG["watch_dir"],
                                  mapping=sorted(PACKAGE_CODES.items()))


@app.post("/preview")
def preview_upload():
    up = request.files.get("txtfile")
    if not up or not up.filename:
        flash("Choose a .txt file first.", "err")
        return redirect(url_for("index"))

    name = secure_filename(up.filename)
    if not name.lower().endswith(".txt"):
        flash(f"{name} isn't a .txt file. Pick the packing-line text file.", "err")
        return redirect(url_for("index"))

    CFG["staging_dir"].mkdir(parents=True, exist_ok=True)
    prune_staging()
    token = uuid.uuid4().hex
    slot = CFG["staging_dir"] / token
    slot.mkdir(parents=True, exist_ok=True)
    up.save(slot / name)

    try:
        parse_txt(slot / name)
    except Exception as exc:
        clear_slot(token)
        flash(f"Couldn't read {escape(name)}: {escape(str(exc))}", "err")
        return redirect(url_for("index"))

    return redirect(url_for("preview", token=token))


@app.get("/preview/<token>")
def preview(token):
    path = staged_path(token)
    if not path:
        flash("That file is no longer staged — upload it again.", "err")
        return redirect(url_for("index"))

    name = path.name
    try:
        rows = parse_txt(path)
    except Exception as exc:
        clear_slot(token)
        flash(f"Couldn't read {escape(name)}: {escape(str(exc))}", "err")
        return redirect(url_for("index"))

    return render_template_string(
        PREVIEW_PAGE, s=summarize(rows, name), token=token,
        rows=rows[:PREVIEW_ROW_LIMIT], truncated=len(rows) > PREVIEW_ROW_LIMIT,
        out_name=Path(name).stem + ".xlsx")


@app.post("/save/<token>")
def save(token):
    path = staged_path(token)
    if not path:
        flash("That file is no longer staged — upload it again.", "err")
        return redirect(url_for("index"))

    name = path.name
    want_csv = bool(request.form.get("csv"))
    try:
        out_path = convert(path, CFG["template"], CFG["output_dir"], also_csv=want_csv)
    except Exception as exc:
        flash(f"Couldn't save {escape(name)}: {escape(str(exc))}", "err")
        return redirect(url_for("preview", token=token))
    clear_slot(token)

    link = url_for("download", filename=out_path.name)
    extra = " Import CSV saved alongside it." if want_csv else ""
    flash(f"Saved {escape(out_path.name)} from {escape(name)}.{extra} "
          f"<a href='{link}'>Download {escape(out_path.name)}</a>", "ok")
    return redirect(url_for("index"))


@app.post("/discard/<token>")
def discard(token):
    path = staged_path(token)
    if path:
        name = path.name
        clear_slot(token)
        flash(f"Discarded {escape(name)}. Nothing was saved.", "ok")
    return redirect(url_for("index"))


@app.post("/process-incoming")
def process_incoming():
    watch_dir = CFG["watch_dir"]
    processed_dir = watch_dir.parent / "processed"
    failed_dir = watch_dir.parent / "failed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)

    ok, bad = [], []
    for txt_path in sorted(find_candidates(watch_dir)):
        try:
            convert(txt_path, CFG["template"], CFG["output_dir"])
        except Exception as exc:
            bad.append(f"{escape(txt_path.name)} ({escape(str(exc))})")
            shutil.move(str(txt_path), failed_dir / txt_path.name)
        else:
            ok.append(txt_path.name)
            shutil.move(str(txt_path), processed_dir / txt_path.name)

    if ok:
        flash(f"Converted {len(ok)} file{'' if len(ok) == 1 else 's'}: "
              f"{', '.join(str(escape(n)) for n in ok)}", "ok")
    if bad:
        flash(f"Failed, moved to the failed folder: {'; '.join(bad)}", "err")
    if not ok and not bad:
        flash("Nothing to convert — the incoming folder is empty.", "ok")
    return redirect(url_for("index"))


@app.get("/download/<path:filename>")
def download(filename):
    name = secure_filename(filename)
    if not name.lower().endswith((".xlsx", ".csv")):
        flash("Only converted .xlsx and .csv files can be downloaded.", "err")
        return redirect(url_for("index"))
    return send_from_directory(CFG["output_dir"], name, as_attachment=True)


@app.errorhandler(413)
def too_big(_e):
    flash("That file is larger than 25 MB — check you picked the right one.", "err")
    return redirect(url_for("index"))


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} web UI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--watch-dir", type=Path, default=DEFAULT_WATCH_DIR)
    parser.add_argument("--staging-dir", type=Path, default=None,
                        help="where uploads wait between preview and save "
                             "(default: <output-dir>/../staging)")
    parser.add_argument("--url-prefix", default=os.environ.get("URL_PREFIX", ""),
                        help="subpath this app is served under, e.g. /tallybridge "
                             "(also read from the URL_PREFIX env var; a proxy's "
                             "X-Forwarded-Prefix header overrides both)")
    args = parser.parse_args()

    CFG["template"] = args.template
    CFG["output_dir"] = args.output_dir
    CFG["watch_dir"] = args.watch_dir
    CFG["staging_dir"] = args.staging_dir or args.output_dir.parent / "staging"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    CFG["staging_dir"].mkdir(parents=True, exist_ok=True)

    app.wsgi_app = PrefixMiddleware(app.wsgi_app, args.url_prefix)
    where = f"http://{args.host}:{args.port}{'/' + args.url_prefix.strip('/') if args.url_prefix.strip('/') else ''}"

    try:
        from waitress import serve
        print(f"{APP_NAME} serving on {where}", flush=True)
        serve(app, host=args.host, port=args.port)
    except ImportError:
        app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
