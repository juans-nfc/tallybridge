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
| `tallybridge-fetcher` | Copies new files off the payroll share | Only with `--auto` |

The watcher sits behind a compose profile, so a plain deploy starts only the
UI. Nothing converts unattended until you ask for it.

---

## Deploying

On the server, first time:

```bash
git clone <your-repo-url> /var/www/html/tallybridge
cd /var/www/html/tallybridge
./deploy.sh
./deploy.sh --install-nginx
```

Every time after:

```bash
cd /var/www/html/tallybridge
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
| `./deploy.sh --install-nginx` | Install that block, test it, reload nginx |
| `./deploy.sh --check` | Confirm the public URL answers |

### Settings — `.env`

`deploy.sh` writes this on first run. It is gitignored, so each server keeps
its own. Edit and re-run `./deploy.sh` to apply.

```
TB_DATA=/srv/tallybridge                    # host folder for the data folders
TB_PORT=8087                                # host port the UI listens on
TB_PREFIX=/tallybridge                      # subpath behind nginx
PUBLIC_URL=https://tools.northernfruit.com  # used for the summary and --check
TB_SHARE=/mnt/stamper                       # where the payroll share is mounted
TB_AUTH=auto                                # auto | on | off — M365 SSO gate
```

`TB_PORT=8087` was picked because it doesn't collide with anything on
tools.northernfruit.com (3000, 3100, 4180, 8000, 8080, 8081, 8090, 8091, 8850
are in use).

### nginx

`./deploy.sh --install-nginx` does the whole job:

1. Writes the proxy config to `/etc/nginx/snippets/tallybridge.conf`.
2. Finds the server block for `PUBLIC_URL` — specifically the **TLS** block,
   since the hostname also appears in the `:80` redirect block where a
   `location` would never be reached.
3. Backs the file up, adds one line (`include snippets/tallybridge.conf;`),
   runs `nginx -t`, and **restores the backup if nginx objects**.
4. Reloads nginx and checks the public URL responds.

Re-running is safe — if the include line is already there, nothing is edited.
To see the config without installing it: `./deploy.sh --nginx`.

If nginx runs in a container or behind Nginx Proxy Manager, the script says so
and prints what to enter there instead (forward target, location, the
`X-Forwarded-Prefix` header, and the 25 MB body limit).

**Why the header matters:** the proxy strips the `/tallybridge` prefix, so the
app reads `X-Forwarded-Prefix` to build its links. Without it every form and
download on the page would point at the wrong path.

### M365 sign-in

`TB_AUTH=auto` (the default) gates TallyBridge behind the same oauth2-proxy
SSO your other apps use, if the script finds it configured in nginx — matching
how `/scalehouse` and `/fta` are protected. Since these files are payroll data,
leave it on.

When SSO is active the page shows who is signed in, and every save is logged
with their address:

```
2026-07-29 16:05:41  NF-Y2026M02D05.xlsx saved from NF-Y2026M02D05.txt by jsmith@northernfruit.com
```

Set `TB_AUTH=off` for an ungated location, or `on` to require the gate even if
auto-detection misses it.

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
5. **After payroll has imported it,** tick the file under *Converted files* and
   click **Delete selected** to keep the list clean. The tick-box in the header
   row selects everything. Deletion is permanent, asks for confirmation first,
   and is logged with the signed-in user — it only ever removes `.xlsx`/`.csv`
   files inside the converted folder, never source files or anything else.

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

## Pulling files off the payroll share

The lines write to `\\192.168.1.10\payroll\STAMPER\`. Mount that share on the
Docker host once, and the `tallybridge-fetcher` container copies new files into
the incoming folder and moves each source into `STAMPER\processed\` on the share
so it is never picked up twice.

### 1. Mount the share on the host

```bash
sudo apt install -y cifs-utils
sudo mkdir -p /mnt/stamper
```

Put the credentials in a root-only file so they stay out of `/etc/fstab` and
out of `ps`:

```bash
sudo tee /etc/tallybridge-smb.cred >/dev/null <<'EOF'
username=SERVICEACCOUNT
password=THEPASSWORD
domain=NORTHERNFRUIT
EOF
sudo chmod 600 /etc/tallybridge-smb.cred
```

Add the mount to `/etc/fstab` (one line):

```
//192.168.1.10/payroll/STAMPER  /mnt/stamper  cifs  credentials=/etc/tallybridge-smb.cred,vers=3.0,uid=0,gid=0,file_mode=0660,dir_mode=0770,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=30  0  0
```

`_netdev` and `nofail` matter: they stop a boot hanging if the file server is
unreachable. Then mount and confirm:

```bash
sudo systemctl daemon-reload
sudo mount -a
mountpoint /mnt/stamper && ls /mnt/stamper
```

The service account needs **write** access, since the fetcher moves handled
files into `processed\`. Read-only also works — files are still converted — but
each one has to be tidied off the share by hand.

### 2. Start the fetcher

It shares the `auto` profile with the watcher, so:

```bash
./deploy.sh --auto
docker compose logs -f fetcher
```

Expect `Watching share /share for NF-*.txt, IL-*.txt (every 60s)`. Drop a test
file on the share and it appears in `converted/` about a minute later.

If the share is mounted somewhere other than `/mnt/stamper`, set `TB_SHARE` in
`.env` and re-run `./deploy.sh --auto`.

### How it avoids double-counting

Duplicate piece counts reaching payroll is the failure worth engineering
against, so:

- Files are copied under a temporary name and renamed into place, so the
  converter never sees a partially copied file.
- A file is only touched once its size has stopped changing — a file still
  being written by the line is left for the next pass.
- Every handled file is recorded in `$TB_DATA/share-fetch-state.json` by name,
  size and modification time. If the archive move fails (read-only share,
  permissions), the file is **not** copied again; the log says it needs tidying
  by hand instead.
- A same-named file that comes back with different content counts as new.
- An archive name clash keeps both copies (`NF-...-20260730134814.txt`).
- An unmounted share is reported and retried, never mistaken for "no files".

### Doing it without Docker

If you'd rather run the copy as a plain system job:

```bash
sudo cp share_fetch.py /usr/local/bin/
/usr/local/bin/share_fetch.py --source /mnt/stamper --dest /srv/tallybridge/incoming \
    --archive /mnt/stamper/processed --once
```

Put that in a cron entry or a systemd timer — `--once` does a single pass and
exits, so it is safe to run every minute. Two passes can't collide on the same
file thanks to the state file.

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

## Source file layouts

The lines produce two shapes and TallyBridge detects which it's handed:

- **Tab-delimited** — six tab-separated fields per line.
- **Space-aligned fixed width** — columns padded with spaces, CRLF or LF
  endings. Field boundaries are worked out from the file itself (any column
  blank on every row is a separator), so a change in padding doesn't break it.

**Rows with no packer ID** appear in some exports — the line writes a code and
a piece count with the Employee ID column blank. Those rows are left out of the
workbook, because Paycom can't import a blank Employee ID, and the count and
units are reported both in the preview and the log so the day still reconciles
against the line's own totals. If payroll would rather see them posted against
a house employee number, that's a small change to `convert()`.

## Column mapping

| Source .txt field | Template column |
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
