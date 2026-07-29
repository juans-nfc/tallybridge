# TallyBridge

Bridges the daily packing-line piece counts from SIMS across to Paycom.

Reads the tab-delimited export from each line (`NF-*.txt` for Northern Fruit,
`IL-*.txt` for Ice Lakes), translates SIMS package codes into Paycom earning
codes, and writes the "Time and Attendance Timecard Import" workbook payroll
expects.

Runs as two containers off one image:

| Container | Job | Starts |
|---|---|---|
| `tallybridge-web` | The page staff convert and check files on | Always |
| `tallybridge-watcher` | Automatic pickup from the incoming folder | Only with `--auto` |

The watcher sits behind a compose profile, so a plain deploy starts only the
UI. Nothing converts unattended until you ask for it.

---

## Deploying

On the server, first time:

```bash
git clone <your-repo-url> /opt/tallybridge
cd /opt/tallybridge
./deploy.sh
```

Every time after:

```bash
cd /opt/tallybridge
./deploy.sh --pull
```

`deploy.sh` is safe to re-run. It creates `.env` on first run, makes the data
folders, builds the image, starts the containers, and confirms the UI answers
on its port.

| Command | What it does |
|---|---|
| `./deploy.sh` | Build and start the UI |
| `./deploy.sh --pull` | `git pull` first, then build and start |
| `./deploy.sh --auto` | Also start the automatic folder watcher |
| `./deploy.sh --no-build` | Restart without rebuilding |
| `./deploy.sh --status` | What's running |
| `./deploy.sh --logs` | Follow the logs |
| `./deploy.sh --stop` | Stop everything |
| `./deploy.sh --nginx` | Print the nginx block for the subpath |

### Settings — `.env`

`deploy.sh` writes this on first run. It is gitignored, so each server keeps
its own. Edit and re-run `./deploy.sh` to apply.

```
TB_DATA=/srv/tallybridge                    # host folder for the data folders
TB_PORT=8087                                # host port the UI listens on
TB_PREFIX=/tallybridge                      # subpath behind nginx
PUBLIC_URL=https://tools.northernfruit.com  # only used for the summary output
```

Change `TB_PORT` if 8087 collides with something else on the box.

### nginx

The app is subpath-aware — it reads `X-Forwarded-Prefix`, so links and forms
resolve correctly under `/tallybridge`. Run `./deploy.sh --nginx` to print the
block for your `tools.northernfruit.com` server config, add it, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Then it's live at **https://tools.northernfruit.com/tallybridge/**

The page has no login of its own — it inherits whatever protects
`tools.northernfruit.com`. If that host is reachable from outside, put auth in
front of this location block.

---

## Using it

### Convert and check a day (the normal path)

1. **Check a file** — pick a day's `.txt`, click **Read the file**. Nothing is
   written yet.
2. **Review.** You get row and packer counts, the date code, the SIMS→Paycom
   translation with totals per pack style, a per-packer breakdown, and the
   exact columns headed for the workbook. Two things to confirm: the piece
   total against the line's own day-end number, and that every SIMS code
   became the Paycom code payroll expects.
3. **Read the "Worth a look" box** if it appears. It flags package codes with
   no Paycom equivalent, more than one date code in a file, non-numeric piece
   counts, repeated packer/code combinations, packers with no piece row, and
   file names the watcher wouldn't pick up. Notes for your judgment, not
   errors — the file still converts.
4. **Save workbook** (or **Save workbook + import CSV**). It lands in
   `$TB_DATA/converted` with a download link on the page.

### Testing the folder hand-off

**Convert waiting files** processes everything in `$TB_DATA/incoming` at once,
no preview, archiving sources to `processed/` and `failed/` — exactly what the
watcher will do. Drop a file in and click it to test the automatic path while
still controlling when it runs.

### Turning on automation

```bash
./deploy.sh --auto
docker compose logs -f watcher     # expect: Watching /data/incoming ...
```

Copy a file into `$TB_DATA/incoming`; within about 45 seconds the log shows it
converted and archived. The delay is deliberate — the watcher waits for the
file size to stop changing so it never reads a half-copied file. Then point
the lines (or a scheduled copy job) at that folder.

Unparseable files go to `$TB_DATA/failed` with the reason logged, and never
stop the service. To turn automation back off without touching the UI:

```bash
docker compose stop watcher
```

---

## Package code translation

The line writes SIMS package codes; Paycom needs its own earning codes. Column
F is translated on every conversion:

| SIMS | Paycom | Pack style | | SIMS | Paycom | Pack style |
|---|---|---|---|---|---|---|
| 500 | E50 | Euro 1/2 Box | | 545 | H45 | Half Carton |
| 505 | L05 | Loose Boxes | | 555 | H55 | Heavy Pack |
| 510 | T10 | Top Pad | | 560 | C60 | Clam |
| 520 | SS2 | Special | | 565 | E65 | Euro Tray |
| 525 | B25 | Bags | | 570 | T70 | Top Pad (HC) |
| 530 | E30 | Euro Bags | | | | |
| 535 | C35 | Cell Pack | | | | |
| 540 | S40 | Sleeve Bags | | | | |

The same table is on the UI home page for staff, and
`python3 timecard_converter.py codes` prints it.

**Adding a pack style:** edit `PACKAGE_CODES` at the top of
`timecard_converter.py`, commit, then `./deploy.sh --pull` on the server.

**A code that isn't in the table** passes through with its SIMS value, logs a
warning, and is highlighted on the preview — so one new pack style can't
silently block a day's payroll. To refuse such files outright instead, add
`--strict-codes` to the watcher's `command:` in `docker-compose.yml`; the file
is then quarantined to `failed/` with the reason logged.

---

## Column mapping

| Source .txt field (tab-separated) | Template column |
|---|---|
| 1 — date/batch code (`0205261P1`) | C — Date |
| 2 — line number | *(not imported)* |
| 3 — packer ID (`0303`) | A — Employee ID (text, leading zeros kept) |
| 4 — SIMS package code (`510`) | F — Earning Code, **translated to Paycom** (`T10`) |
| 5 — allocation (`10100121`) | I — Labor Allocation Code (number) |
| 6 — pieces (`0001`) | N — Units (text, leading zeros kept) |

## Reference

| Setting | Where |
|---|---|
| Data folder, port, subpath | `.env` (see above) |
| SIMS → Paycom codes | `PACKAGE_CODES` in `timecard_converter.py` |
| File patterns (`NF-*.txt`, `IL-*.txt`) | `FILE_PATTERNS` in `timecard_converter.py` |
| Watcher poll interval (15 s) | `POLL_SECONDS` in `timecard_converter.py` |
| Headline piece count code | `PIECE_CODE` in `timecard_web.py` |
| Refuse unmapped codes | add `--strict-codes` to the watcher `command:` |
| Watcher writes the CSV too | add `--csv` to the watcher `command:` |

Data folders under `$TB_DATA`: `incoming`, `converted`, `processed`, `failed`,
and `staging` (uploads waiting between preview and save, cleaned up after 12
hours if nobody saves them).

### Command line, without the UI

```bash
docker compose run --rm web python3 timecard_converter.py convert /data/incoming/NF-Y2026M02D05.txt --output-dir /data/converted
docker compose run --rm web python3 timecard_converter.py codes
```
