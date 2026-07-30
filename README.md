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
| `tallybridge-fetcher` | Copies files off another server's share (Option B only) | Only with `--auto` |

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
| `./deploy.sh --smb` | Print the Samba config for the drop folder |
| `./deploy.sh --install-smb` | Install it, validate it, restart Samba |

### Settings — `.env`

`deploy.sh` writes this on first run. It is gitignored, so each server keeps
its own. Edit and re-run `./deploy.sh` to apply.

```
TB_DATA=/srv/tallybridge                    # host folder for the data folders
TB_PORT=8087                                # host port the UI listens on
TB_PREFIX=/tallybridge                      # subpath behind nginx
PUBLIC_URL=https://tools.northernfruit.com  # used for the summary and --check
TB_SHARE=/mnt/payroll/STAMPER               # the STAMPER folder on the mounted share
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

## Getting files onto the server

### Option A — Windows shares on this server (recommended)

Two SMB shares, both password protected:

| Share | Folder | Access | Who |
|---|---|---|---|
| `\\tools\incoming` | `$TB_DATA/incoming` | read/write | the line — drops the day's `.txt` |
| `\\tools\converted` | `$TB_DATA/converted` | **read only** | payroll — imports the workbooks |

```bash
sudo apt install -y samba
./deploy.sh --install-smb
```

That appends whichever sections are missing to `/etc/samba/smb.conf` — backing
the file up first, validating with `testparm`, restoring the backup if Samba
objects — then restarts Samba. Re-running is a no-op. `./deploy.sh --smb`
prints the config without installing it.

Then create the two accounts (the script tells you which already exist):

```bash
sudo useradd -M -s /usr/sbin/nologin packline   # no home directory, no shell
sudo smbpasswd -a packline && sudo smbpasswd -e packline

sudo useradd -M -s /usr/sbin/nologin payroll
sudo smbpasswd -a payroll && sudo smbpasswd -e payroll
```

Map them from Windows:

```
net use S: \\tools\incoming  /user:packline /persistent:yes
net use P: \\tools\converted /user:payroll  /persistent:yes
```

If the server is firewalled, allow SMB from the plant LAN only:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 445 proto tcp
```

Notes worth knowing:

- **`converted` is deliberately read only.** Paycom only needs to read the
  workbooks, and a read-only share means an import can't move, lock, or delete
  one by accident. Removing files after import is done from the web page, which
  logs who did it.
- **`force user = root`** means files land owned by root, which is what the
  containers run as — so the watcher can always read a dropped file and move it
  into `processed/`. Without it a file saved by one user could be unmovable.
- **Files vanish from `incoming` a few seconds after saving.** That's the
  watcher archiving the source into `processed/`, not a lost file. Tell whoever
  uses the share, or they will save it twice.
- **Re-saving the same filename converts it again**, overwriting the workbook.
  Harmless in itself — just don't import the same day into Paycom twice.
- Windows scratch files (`Thumbs.db`, `desktop.ini`, `~$...`) are vetoed by
  Samba and ignored by the watcher, which only takes `NF-*.txt` / `IL-*.txt`.
- Names and accounts are overridable:
  `SMB_USER=nfline SMB_USER_OUT=hr SMB_SHARE_OUT=workbooks ./deploy.sh --install-smb`.
  One account can serve both — set `SMB_USER_OUT` to the same name.

### Option B — pull from an existing share

If the files already land on another server's share, `share_fetch.py` can copy
them across instead: mount that share on this host, set `TB_SHARE` in `.env` to
the folder the lines write into, and `./deploy.sh --auto` starts a
`tallybridge-fetcher` container alongside the watcher. It copies new
`NF-*.txt` / `IL-*.txt` into the incoming folder and moves each source into a
`processed` subfolder on the share, keeping a state file
(`$TB_DATA/share-fetch-state.json`) so nothing is ever copied — or counted —
twice.

The script also runs standalone for cron or a systemd timer:

```bash
./share_fetch.py --source /mnt/somewhere --dest /srv/tallybridge/incoming \
    --archive /mnt/somewhere/processed --once
```

Mounting an IBM i (AS/400) NetServer share needs `vers=2.0` — its NetServer
tops out at SMB dialect 2.002 — and a real user profile, since guest is
refused. Option A avoids all of that.

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

**Badge numbers are zero-padded to 7 digits** on the way out, because that is
the width Paycom expects: SIMS writes `0303`, the workbook carries `0000303`.
A badge that is already 7 digits is left alone; one that is longer, or not
numeric, passes through untouched and is flagged on the preview rather than
mangled.

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
| 3 — packer ID (`0303`) | A — Employee ID, **zero-padded to 7 digits** (`0000303`) |
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
