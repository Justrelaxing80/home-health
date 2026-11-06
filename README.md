# Home Health

A household health dashboard for ZimaOS. Each family member gets a profile and
links their own **Garmin Connect**, **Renpho**, and **Apple Health** data; the
app charts it all on one page. Switch people from the profile menu in the
top-right corner. Each profile also has **Supplements** and **Peptides** tabs.

**Shows:** weight, body fat, visceral fat (Renpho/Apple) · steps vs. goal, sleep and
sleep stages, sleep score, resting HR, HRV, Body Battery, stress,
recent activities (Garmin/Apple). When Garmin and Apple both report a day, Garmin's
numbers are used and Apple fills the gaps.

**Daily summary:** a short paragraph at the top of the dashboard comparing the
latest sleep, resting HR, HRV, steps, Body Battery/stress, weight/body fat and
workouts with your own recent averages, plus today's supplement and peptide
progress. It is generated from stored readings only (no AI, no estimates); each
number names the day it came from, and metrics with no recent data are left out.

**Supplements tab:** a daily check-off list (several servings a day, specific
weekdays), a 14-day consistency grid, per-supplement and overall streaks, and a
one-tap list of the 25 most common supplements (protein, creatine, D3, B12,
fish oil, magnesium, ...) plus your own, and optional ntfy reminders at set
times (grouped into one notification, skipped if already checked off). Streaks count days where every scheduled
serving was checked off; today never breaks a streak until it's over. The
dashboard shows the supplement and peptide streaks.

**Barcode scanning:** tap **Scan barcode** on the Supplements tab and point the
phone at a bottle. The product's name, brand, photo, serving size and (when
listed) per-serving nutrition come from [Open Food Facts](https://world.openfoodfacts.org)
and, to fill gaps, UPCitemdb's free lookup (100 a day). The sheet says which
source each product came from. Nothing is guessed, so check it against your label.
The barcode is saved with the supplement: scanning that bottle again checks off
today's next serving. Unknown barcodes can be added by hand and are recognized
from then on. Live camera scanning needs a secure page (https, or localhost). Over
plain `http://` (for example a Tailscale IP) the scanner uses the phone's camera
in photo mode instead, which works the same way with one extra tap. To get live
scanning remotely, turn on HTTPS for the Tailscale address (Tailscale Serve).

**Peptides tab:** today's dose checklist (log / skip / undo), compounds with
dosing schedules (daily, specific weekdays, every N days, as needed; several
times a day), vial tracking (reconstitution, mcg/mL, units to draw on a U-100
syringe, amount left, discard date), dose log, 30-day adherence, injection-site
rotation, a reconstitution calculator, and push reminders through
[ntfy](https://ntfy.sh). For personal tracking only, not medical advice.

## Install on ZimaOS

The image is built automatically by GitHub Actions on every push to `main` and
published as `ghcr.io/justrelaxing80/home-health:latest` (amd64 + arm64).

1. In ZimaOS open **App Store → Custom Install** (the ＋ / "Install a customized app" button).
2. Choose **Import**, paste the contents of [`docker-compose.yml`](docker-compose.yml), and install.
3. Open the **Home Health** tile, or `http://<zima-ip>:9085`.

Data (SQLite database + encryption key) lives in `/DATA/AppData/home-health`.
Back that folder up and you've backed up everything.

**Updating:** push changes to `main`, wait for the *Build and publish image*
action to finish, then update/restart the app in ZimaOS so it pulls the new `latest`.

**Without the App Store:** on the box, run `docker compose up -d` in a folder
containing `docker-compose.yml`.

**Settings** (in `docker-compose.yml`): `TZ` (set this to your timezone),
`SYNC_INTERVAL_MINUTES` (default 60; 0 = manual only), `GARMIN_BACKFILL_DAYS`
(history pulled on first Garmin link, default 30). The web port is the left side
of `9085:8080`.

## First run

1. Click **Add a demo profile** to see the dashboard filled with sample data
   (delete it later from its Settings page).
2. **Add profile** for each person, then in their **Settings**:
   - **Garmin (recommended: Personal API key)**: on Garmin Connect, create a key at
     [connect.garmin.com/app/settings/personalApiToken](https://connect.garmin.com/app/settings/personalApiToken)
     (it starts with `GCDC_`, is shown only once, and lasts up to a year), then
     paste it under *Or link with a Personal API key*. This is Garmin's official
     personal API: no password and no verification codes. When the key expires,
     the card says "needs re-link"; create a new key and paste it.
   - **Garmin (alternative)**: enter their Garmin Connect email/password. If the
     account has two-step verification, Garmin sends a code and the page asks for it.
   - **Scale via Garmin**: if the Renpho app is connected to Garmin Connect
     (Renpho app → Me → Third-party apps → Garmin), the Garmin link also imports
     weigh-ins (weight, BMI, body fat, water, visceral fat, metabolic age; one
     year on the first sync). Duplicates with the Renpho link/CSV are dropped.
   - **Renpho**: enter their Renpho app email/password. Or skip the cloud link and
     use **Import Renpho CSV** with a file exported from the Renpho app.
   - **Apple Health**: Apple has no web login; data has to come from the iPhone.
     Either click **Create sync URL** and paste it into the iOS app
     *Health Auto Export* (REST API automation, JSON, aggregated by day), or upload
     the `export.zip` from *Health → profile → Export All Health Data*. The phone
     must be able to reach the server (home Wi-Fi, or a VPN like Tailscale).
3. **Peptides → Reminders**: install the ntfy app on the phone, subscribe to the
   topic shown, and turn reminders on. A notification is sent when each
   scheduled dose comes due. Pick a hard-to-guess topic, because anyone who knows
   a topic on the public ntfy.sh server can read it (or tick *Hide peptide names*).

## Things to know

- **Garmin's Personal API key link is official**
  ([docs](https://developer.garmin.com/gc-developer-program/personal/personal-api/)).
  The Garmin password login and the Renpho link are unofficial: they use the
  community libraries [`garminconnect`](https://github.com/cyberjunky/python-garminconnect)
  and [`renpho-api`](https://github.com/danvaneijck/renpho-api). Either company
  can change things and break sync. When that happens, the dashboard shows a
  banner; updating the library version in `requirements.txt` and rebuilding is
  usually the fix. Renpho CSV import keeps working regardless.
- **Garmin MFA:** the session normally refreshes itself. If it ever fully expires,
  the dashboard says "needs re-link" and you log in again (with a code if MFA is on).
- **Renpho family members:** a profile reads the measurements of the Renpho
  account it's linked to. Give each person their own Renpho login, or use CSV import.
- **No login screen.** Anyone who can reach the page can see every profile. Keep
  it on your home network and don't port-forward it to the internet.
- **Access from outside the home:** use [Tailscale](https://tailscale.com) (free
  for personal use). Install the Tailscale app from the ZimaOS App Store and sign
  in, then install Tailscale on your phone/laptop with the same account and open
  `http://<zima's tailscale IP or name>:9085`. Only your own signed-in devices can
  reach it, and the Apple Health sync URL keeps working away from home if you use
  that address in Health Auto Export. Passwords
  and tokens are encrypted at rest with a key stored in the data folder.

## Development

```sh
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows
DATA_DIR=./data .venv/Scripts/python -m uvicorn app.main:app --port 9085 --reload
```

Layout: `app/main.py` (API), `garmin.py` / `scale.py` / `apple.py` (integrations),
`peptides.py` (schedules, vial math, ntfy reminders), `supplements.py`,
`streak.py`, `analysis.py` (daily summary),
`sync.py` (scheduler), `db.py` (SQLite), `static/` (dashboard, no build step;
Chart.js 4.4.1 is vendored in `static/vendor`).
