# Home Health

A household health dashboard for ZimaOS. Each family member gets a profile and
links their own **Garmin Connect** and **Renpho** accounts; the app syncs every
hour and charts it all on one page. Switch people from the profile menu in the
top-right corner.

**Shows:** weight, body fat, visceral fat (Renpho) · steps vs. goal, sleep and
sleep stages, sleep score, resting HR, overnight HRV, Body Battery, stress,
recent activities (Garmin).

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
   - **Garmin**: enter their Garmin Connect email/password. If the account has
     two-step verification, Garmin sends a code and the page asks for it.
   - **Renpho**: enter their Renpho app email/password. Or skip the cloud link and
     use **Import Renpho CSV** with a file exported from the Renpho app.

## Things to know

- **Both Garmin and Renpho integrations are unofficial.** Garmin's official API
  is for approved businesses only, and Renpho has none, so this uses the
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
  it on your home network and don't port-forward it to the internet. Passwords
  and tokens are encrypted at rest with a key stored in the data folder.

## Development

```sh
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows
DATA_DIR=./data .venv/Scripts/python -m uvicorn app.main:app --port 9085 --reload
```

Layout: `app/main.py` (API), `garmin.py` / `scale.py` (integrations),
`sync.py` (scheduler), `db.py` (SQLite), `static/` (dashboard, no build step;
Chart.js 4.4.1 is vendored in `static/vendor`).
