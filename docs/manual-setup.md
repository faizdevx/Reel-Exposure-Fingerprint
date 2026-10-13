# Manual steps to bring the project up (state of 2026-10-07)

This is the list of things only you can do by hand to get the project running and checked up to the current phase: M1 (skeleton, auth, consent, sessions) plus the parts of M2 built so far (extension shell, Instagram adapter, exposure tracking). Everything that can be checked automatically has been: see "What was already verified" at the end.

Commands are for **PowerShell** on Windows, run from the repository root (`D:\insta_recom_exten\reel-exposure-fingerprint`) unless a step says otherwise.

---

## 0. What works at this phase, and what does not yet

Works:

- Server: login, invites, extension tokens, consent, monitoring sessions, event ingest (`POST /api/v1/events`), `/healthz`.
- Extension: popup (consent, token, Start/Stop, badge), service worker (session rules, queue, upload every 15 s or 10 events with backoff and retry), content script (in-page marker, Reel detection, one event per viewing, dwell, pagehide flush).
- Dashboard: shell and health status only.

Not built yet (so do not expect it to work):

- Nothing processes the stored events yet: each new event gets a `jobs` row (`kind = 'exposure'`) that no worker picks up until M3.
- **No dashboard login or token screens.** You create your user and extension token with the commands in step 4.
- No worker, no topics, no fingerprints, no deletion endpoints, no frame capture (`frame_count` is always 0).

---

## 1. Prerequisites (one time)

| Tool | Check | Notes |
|---|---|---|
| Docker Desktop, running | `docker info` | Runs Postgres, the API and the dashboard |
| Node.js 24 and npm | `node -v` | Builds the extension |
| Google Chrome (desktop) | | The only supported browser |
| `uv` (optional) | `uv --version` | Only to run the server tests yourself |

Drive C: is low on space, so keep package caches on D: in every new shell before `npm` or `uv`:

```powershell
$env:npm_config_cache = "D:\insta_recom_exten\.cache\npm"
$env:UV_CACHE_DIR = "D:\insta_recom_exten\.cache\uv"
```

---

## 2. Create your `.env` (one time)

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `POSTGRES_PASSWORD` to a strong value of your choice. Leave the rest as is. Never commit `.env` (it is gitignored).

---

## 3. Start the server stack

```powershell
docker compose up -d --build
docker compose ps
curl.exe http://127.0.0.1:8000/healthz
```

Expected: `db` is `healthy`, `api` and `dashboard` are `Up`, and the health check prints `{"status":"ok"}`. The API applies the database migrations itself on start.

The dashboard is at <http://127.0.0.1:8080>. It should show the server as healthy.

If `api` keeps restarting: `docker compose logs api`. The usual cause is a `POSTGRES_PASSWORD` that changed after the database volume was created; see Troubleshooting.

---

## 4. Create your user and an extension token

There are no dashboard screens for this yet, so use the API directly.

**4.1 Create an invite code** (single use, valid 72 hours):

```powershell
$invite = (docker compose exec -T api python -m app.manage create-invite).Trim()
```

**4.2 Register, log in, and create a token.** Change the email and password (at least 12 characters) to your own:

```powershell
$api = "http://127.0.0.1:8000/api/v1"
$csrf = @{ "X-Requested-With" = "ref-dashboard" }
$email = "you@example.com"
$password = "choose-a-long-password"

Invoke-RestMethod -Method Post "$api/auth/register" -Headers $csrf -ContentType "application/json" `
  -Body (@{ email = $email; password = $password; invite_code = $invite } | ConvertTo-Json)

Invoke-RestMethod -Method Post "$api/auth/login" -Headers $csrf -ContentType "application/json" `
  -Body (@{ email = $email; password = $password } | ConvertTo-Json) -SessionVariable web

$t = Invoke-RestMethod -Method Post "$api/extension-tokens" -Headers $csrf -ContentType "application/json" `
  -Body (@{ label = "my chrome" } | ConvertTo-Json) -WebSession $web
$t.token | Set-Clipboard
"Token copied to the clipboard. Token id: $($t.id)"
```

The token is shown only once (the server keeps only its hash). It is now on your clipboard; you paste it into the extension in step 6. Do not save it in a file or share it.

To revoke it later: `Invoke-RestMethod -Method Delete "$api/extension-tokens/$($t.id)" -Headers $csrf -WebSession $web`

---

## 5. Build and load the extension

```powershell
cd extension
npm ci
npm run build
cd ..
```

Then in Chrome:

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and pick the folder `extension\dist`.
4. Pin "Reel Exposure Fingerprint" to the toolbar.

Chrome will say the extension can "read and change your data on www.instagram.com" and on 127.0.0.1 and localhost. That is expected (ADR-021, ADR-022): instagram.com is needed to see the tab address and to run the content script; the local hosts are the REF server.

**After every rebuild:** click the reload icon on the extension card, then **reload any open Instagram tab**. Chrome does not inject the new content script into tabs that were already open.

If you still have the M0 probe extension (`spikes/extension-probe`) loaded, remove or disable it so the two do not both show markers.

---

## 6. First run in the popup

Open the extension popup:

1. Paste the token into **Paste token** and click **Save token**. Expected: "Extension token saved." and the field is cleared.
2. Read the consent text, tick **I have read this and I agree**, click **Give consent**. Expected: the consent text is replaced by "Consent given <date>".
3. Open <https://www.instagram.com/reels/> in the active tab and log in to Instagram normally if needed. (The extension never sees your Instagram login.)
4. With that tab active, open the popup and click **Start monitoring**. Expected:
   - popup: red **● MONITORING** badge and "Monitoring is on.";
   - toolbar icon: red `REC` badge;
   - on the page: a red pill at the bottom left, "Reel Exposure Fingerprint: monitoring is on".
5. Click **Stop monitoring**. The badge, `REC` and the pill disappear.

Check the server recorded it:

```powershell
docker compose exec -T db psql -U ref -d ref -c "select started_at, ended_at, status, stop_reason from sessions order by started_at desc limit 5;"
```

Expected: a row with `status = stopped`, `stop_reason = user`. **This is the M1 exit check** (start and stop from the popup, row in the database).

---

## 7. Manual acceptance checks for this phase

None of these has been run in a real browser yet. Please run them and note the results (see step 9). Expected results are given for each.

### 7.1 Session rules (spec F2)

| Do | Expected `stop_reason` in `sessions` |
|---|---|
| Start, then press Stop | `user` |
| Start, then navigate to the Instagram home feed and wait 60 s | `left_surface` (the pill disappears at once; the session ends after 60 s) |
| Start, go to the home feed, come back to Reels within 60 s | session keeps running |
| Start, then close the Reels tab | `tab_closed` |
| Start, then quit Chrome completely and reopen it | `browser_restart` |
| Start, then press **Revoke consent** in the popup | `consent_revoked`; Start is disabled until you consent again |
| Start and leave it for 60 minutes | `time_cap` |

### 7.2 Consent gate (spec F1)

1. Revoke consent in the popup.
2. Open DevTools on the extension's service worker: `chrome://extensions` → the extension card → **service worker** link → **Network** tab.
3. Scroll some Reels.

Expected: no request leaves the extension, and Start is disabled.

### 7.3 Content script is idle off the Reels surface (spec F3)

With a session running, open the Instagram home feed, a profile, Explore and a single post (`/p/...`) or single reel (`/reel/...`). Expected: no pill on any of them. Only `https://www.instagram.com/reels/...` shows it, and only in the tab where you pressed Start.

### 7.4 Reel detection and dwell (spec F4, F6)

Events now reach the database. To check them:

1. Start a session on `/reels/`.
2. Scroll **20 Reels by hand**, keeping your own count. Watch some for 5+ s, skip some in under 1 s, pause one for about 5 s (click the video), and switch to another tab for about 5 s on one.
3. Press Stop, wait 30 s, then look at what arrived:

```powershell
docker compose exec -T db psql -U ref -d ref -c "select reel_shortcode, dwell_ms, video_duration_ms, creator_handle, field_status->>'caption' as caption, hashtags from reel_exposures order by observed_at desc limit 25;"
docker compose exec -T db psql -U ref -d ref -c "select count(*), count(distinct event_id) from reel_exposures; select kind, count(*) from jobs group by kind;"
```

Events still waiting to be sent are visible in the service worker DevTools (as in 7.2) → **Application** → **IndexedDB** → `ref-queue` → `events`; after a successful upload that store is empty.

Expected:

- one row per Reel you scrolled past (each Reel you viewed again later counts again), the two counts equal (no duplicates), and one `exposure` job per row;
- `reel_shortcode` matches the code in the Reel's address;
- `dwell_ms` roughly matches how long you actually watched, without the paused time and the hidden-tab time;
- fields that could not be read are `{"value": null, "status": "unavailable"}`, never blank or made up.

The current Reel's event is only written when you scroll past it or leave the page. The Reel on screen when you press Stop is not recorded (known limit, ADR-022). The M2 exit check asks for 100 hand-scrolled Reels giving at least 95 rows and 0 duplicates; do that run once 20 look right.

### 7.4a Two-minute network outage (spec F8, N5; M2 exit)

1. Start a session and scroll a few Reels.
2. Open the service worker DevTools (as in 7.2) → **Network** → set throttling to **Offline**. This cuts only the extension's uploads; Instagram keeps working in its own tab. (Alternatively stop the API with `docker compose stop api`, and `docker compose start api` afterwards.)
3. Keep scrolling by hand for **2 minutes**, counting the Reels. A few failed `POST /api/v1/events` requests appear in the service worker's Network tab, spaced further and further apart (backoff), not one per Reel.
4. Set throttling back to **No throttling**, keep scrolling for 2 more minutes, press Stop and wait about 2 minutes.
5. Run the two `psql` queries from 7.4.

Expected: every Reel you scrolled is in `reel_exposures`, the two counts are equal, and the session row has `stop_reason = user`. Also try pressing Stop while still offline: the session stays `active` in the database until you are back online; then the events arrive first and the stop after them (ADR-023).

Browser restart: repeat with the API stopped, close Chrome completely during the outage, start the API and then Chrome again, and wait a minute. The events arrive and the session ends with `browser_restart`.

### 7.5 Adapter self-test on the live page (spec N8)

The self-test exists but is not shown in the popup yet (M7). To run it by hand you would need the M0 probe or a debug build; skip this until it is surfaced, but **report** if 7.4 shows many `unavailable` handles or captions, or wrong shortcodes. The adapter's selectors were written against synthetic pages, not real Instagram (see `extension/test/fixtures/README.md`).

### 7.6 Things only a real browser can confirm

- The `http://127.0.0.1/*` host permission covers port 8000. If the popup says "Cannot reach the server" while `curl.exe http://127.0.0.1:8000/healthz` works, this is the cause: report it.
- Scrolling feels smooth with monitoring on (spec N2). Optionally record DevTools → Performance while scrolling 10 Reels.

---

## 8. Finish M0 (still open)

M0 is the go/no-go milestone and it is not finished. `docs/m0-feasibility-report.md` does not exist yet because it needs your browser runs.

1. Follow `spikes/README.md` exactly: load `spikes/extension-probe`, do runs 1 to 8 by hand, export the log and frames into `spikes/data/` (gitignored), and run the summary script it gives.
2. Send me the summary output (not the raw data, which contains third-party Reel content). I will write `docs/m0-feasibility-report.md` with the GO / NO-GO per component.
3. Remove the probe extension when done.

If you save any Reels-page HTML to replace the synthetic adapter fixtures, scrub real handles, captions and codes before it goes anywhere near git.

---

## 9. Decisions only you can make

Recorded in `docs/implementation-roadmap.md` §10 and `docs/architecture-decisions.md`:

| Id | Decision | Needed by |
|---|---|---|
| D-1 | Self-only or cohort (ethics paperwork if cohort) | now (M0) |
| D-2 | Deadline | now (M0) |
| D-3 | Research claim: exposure, engagement, or the gap | before M3 |
| D-4 | Taxonomy source: hand-written or adapted | start of M3 |
| D-5 | Supply the missing spec section 1 ("1.3" out-of-scope list) | now (M0) |
| D-6 | Accept or reject each Proposed ADR (002, 005 mechanism, 007, 008, 010–016, rest of 014, 022, 023) | M0 exit |
| D-7 | Acceptable extension permissions if frame method B needs `<all_urls>` | M0 exit |

**ADR-022 and ADR-023 are implemented and need your yes or no:**

1. Content script injected on all of `https://www.instagram.com/*` (idle except on `/reels/*`) rather than matched to `/reels/*` only.
2. `POST /events` body shape `{"events": [...]}` and its per-event answer (`stored`, `duplicate`, or `rejected` with a reason).
3. Two fields added to the event payload, `video_duration_status` and `caption_truncated`, stored inside `field_status` (no new column).
4. Events of a stopped session are refused, so the extension sends queued events **before** telling the server a session stopped. While offline, the server's session stays open until the events are in.
5. Events the server rejects for good are dropped from the queue and counted; a batch refused as malformed is split to find the one bad event.
6. One `exposure` job per new event, not one per new shortcode as spec M2 words it.

---

## 10. Save the work in git

Nothing has been committed yet, so CI has never run. When you are happy, either ask me to commit (I will use a branch and Conventional Commits), or do it yourself:

```powershell
git status          # check that no .env, spikes/data, dist or node_modules is listed
git switch -c m2/extension-tracking
git add .
git commit -m "feat: M1 foundation, auth, consent/sessions, extension shell, adapter, exposure tracking"
```

Push to GitHub and check that the **CI** workflow passes (server, extension, dashboard, compose jobs).

---

## 11. Running the tests yourself (optional)

```powershell
# Extension (313 tests) and dashboard (6 tests)
cd extension; npm ci; npm run lint; npm run typecheck; npm test; cd ..
cd dashboard; npm ci; npm run lint; npm run typecheck; npm test; cd ..

# Server (174 tests; 161 need a throwaway pgvector database)
docker run -d --rm --name ref-testdb -e POSTGRES_PASSWORD=testpw -e POSTGRES_DB=ref_test -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
cd server
$env:TEST_DATABASE_URL = "postgresql+psycopg://postgres:testpw@127.0.0.1:55432/ref_test"
uv run ruff check . ../tests; uv run mypy; uv run pytest
cd ..
docker stop ref-testdb
```

Without `TEST_DATABASE_URL` the 161 database tests are reported as **skipped**, not passed.

---

## 12. Stopping and cleaning up

```powershell
docker compose down        # stop; your data stays in the db-data volume
docker compose down -v     # stop AND delete all data (users, sessions, consents): irreversible
```

To remove the extension: `chrome://extensions` → **Remove**. That also deletes its local queue and stored token.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `api` restarts, logs show `password authentication failed` | The `db-data` volume was created with an older `POSTGRES_PASSWORD`. Put the old one back in `.env`, or wipe the data with `docker compose down -v` (deletes everything) |
| Popup: "Cannot reach the server." | The stack is not running (`docker compose ps`), or the host-permission issue in 7.6 |
| Popup: "The server rejected the extension token. Save a new one." | Token revoked or the database was wiped: repeat step 4 |
| Popup: "Open instagram.com/reels in this tab, then press Start." | The active tab is not on `https://www.instagram.com/reels/...` (a single `/reel/...` page does not count yet) |
| No pill on the Reels page during a session | The tab was open before the extension was (re)loaded: reload the tab |
| Port 8000 or 8080 already in use | Stop the other program, or change the left-hand port in `docker-compose.yml` (the extension expects the API on 8000; see `extension/src/config.ts`) |

---

## What was already verified (2026-10-07)

- Extension: eslint, tsc, 346 Vitest tests, build. Dashboard: eslint, tsc, 6 tests, build. Server: ruff, ruff format, mypy strict, 247 pytest tests against a real pgvector database.
- Simulated 2 minute outage (`extension/test/delivery.test.ts`): no event lost and none duplicated, including Stop pressed offline and a browser restart.
- `docker compose up --build` of the whole stack; migrations applied; `/healthz` on 8000 and through the dashboard on 8080.
- The exact commands in step 4 (invite, register, login, token), then with the token: start without consent (403), consent, start, stop (`user` in the database), revoke (204).
- `POST /api/v1/events` on a live compose stack: 401 without a token, 403 without consent, 3 events stored, the same 3 replayed as `duplicate` (3 rows, 3 jobs), 413 over 256 KB, 422 without echoing values, `session_inactive` after Stop.
- Not verified: anything in Chrome (steps 5 to 7).
