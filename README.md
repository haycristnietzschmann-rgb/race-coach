# Race Coach

A personal training dashboard: your real Garmin data, a weekly workout list
you schedule yourself, automatic detection of what you actually did, and a
Claude-powered daily coaching brief — installable as an app on your phone
and Mac.

## How it's actually built (read this first)

This app has two independent parts, and it's important to understand what
each one does:

**`frontend/index.html`** — a single, self-contained file with everything:
Overview, Training, Race Coach, Journal, Race Archive, Ask Coach, Settings.
It currently runs on:
- A **real Garmin data snapshot** pulled once and baked into the file
  (sleep, HRV, training status, recent activities, HR zones — all real
  numbers from your account as of when it was generated)
- **Your browser's local storage** for everything you create going forward:
  settings, sport on/off toggles, scheduled workouts and their
  done/missed status, journal entries, race archive entries

This means the app is fully usable and persists your choices right now,
**without needing the backend running at all.** The trade-off: the Garmin
snapshot itself doesn't refresh automatically — it's a point-in-time pull,
not a live connection.

**`backend/`** — a separate FastAPI service that *does* have live Garmin
access, plus Claude-generated coaching briefs, a morning report, and push
notification scaffolding. It's ready to run, but the frontend doesn't call
it for live data yet — wiring that up (replacing the baked snapshot with
real `fetch()` calls to this backend) is the natural next step once you've
confirmed the UI/UX is exactly what you want. Until then, refreshing the
Garmin snapshot means asking me to re-pull your data and regenerate the file.

## Project structure

```
race-coach/
├── backend/
│   ├── main.py              FastAPI app: dashboard, overview, training,
│   │                        morning-report, push subscription endpoints
│   ├── garmin_client.py     Wraps the unofficial garminconnect library
│   ├── coach.py             Daily race-focused coaching brief (Claude)
│   ├── morning_report.py    Broader daily briefing (Claude)
│   ├── push.py              Web push notification helper
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── index.html            The whole app — this is what you deploy/install
    ├── manifest.json         PWA manifest
    ├── service-worker.js     Offline shell + push notification handler
    ├── icon-192.png
    └── icon-512.png
```

## What's inside index.html

- **Overview** — Recovery/Sleep/Strain/Training Status rings (tap any for a
  detail breakdown + coach note), HRV/body battery/resting HR/VO2max/FTP,
  a red-flag alert banner, a Fitness & Fatigue (CTL/ATL/form) chart, a
  Sleep Bank chart (need vs actual, cumulative deficit), and trend charts
  switchable Week/Month/3-Month
- **Training** — Monday-Sunday calendar (expandable to a full month view),
  clickable real activities with full stats (pace/speed/HR/elevation),
  real HR-zone distribution, and a Bike/Run/Swim volume breakdown chart
- **Race Coach** — real race info pulled for Port de Palma Triathlon, a
  weekly workout *list* (not a fixed schedule) you assign to whichever day
  you want, a coach suggestion that's explicitly optional, volume progress
  bars with a strain target, a Weekly Review (completed/missed/upcoming),
  automatic detection that crosses off workouts when a matching real
  Garmin activity is found, missed-workout flagging, and a race switcher
  (Palma race plan or General Fitness mode with no race)
- **Journal** — daily tags (alcohol, travel, poor sleep, stress, etc.) plus
  notes, saved locally
- **Race Archive** — log past races manually; empty until you add one
- **Ask Coach** — chat interface (currently a handful of canned demo
  answers; wiring this to the live Claude API is a backend integration step)
- **Settings** — sport on/off toggles (persist even when disabled), units
  (km/mi), notification preferences, manual refresh

## Known limitations, stated plainly

- **The Garmin snapshot is static.** It won't update on its own. Ask me to
  regenerate it, or do the backend integration below to make it live.
- **HR zones and activity details are real for mid-August 2026 only** —
  the two weeks I had real Garmin data for when building this. Other weeks
  fall back to clearly-synthetic placeholder data.
- **The Fitness & Fatigue chart is modeled**, not exact — Garmin doesn't
  expose daily training-load history through this API, so the 42-day shape
  is calibrated to land on your real 7-day/28-day load figures but the
  day-by-day values in between are estimated.
- **Ask Coach doesn't call Claude yet** — it's a UI demo with canned
  responses to the three suggestion chips.
- **Local storage settings won't survive** if this file is ever opened
  inside an embedded/sandboxed viewer that blocks browser storage — it'll
  show a visible warning if that happens. Opening it directly in a real
  browser (which is how you've been testing it) works fine.

---

## Step 1 - Replace your local files

If you already have an older copy of this project on your Mac, the
cleanest approach is a full replace:

```bash
cd ~/Downloads
rm -rf race-coach
```

Then download this new `race-coach.zip`, unzip it into `~/Downloads`, and
copy your real `.env` (with your Garmin/Anthropic credentials) into
`race-coach/backend/.env` - that file isn't included in the zip since it
has your secrets.

## Step 2 - Test the frontend locally one more time

```bash
cd ~/Downloads/race-coach/frontend
python3 -m http.server 5500
```

Open `http://localhost:5500` (not the `file://` version, and use an
Incognito window the first time to dodge any leftover service-worker
cache from before). Click through every tab, schedule a workout, toggle a
sport off, add a journal entry - confirm it all works and persists after
a refresh.

## Step 3 - Deploy the frontend

Since `index.html` is fully self-contained (no build step, no bundler),
any static host works. Pick one:

**Netlify (easiest)**
1. Go to app.netlify.com/drop
2. Drag the `frontend/` folder onto the page
3. You'll get a live URL like `https://random-name.netlify.app` instantly

**GitHub Pages**
1. Push this repo to GitHub
2. Repo Settings -> Pages -> set source to the `frontend/` folder
3. Your site publishes at `https://yourusername.github.io/race-coach`

**Render Static Site**
1. Push to GitHub, then in Render: New -> Static Site -> point at the repo,
   root directory `frontend`, no build command needed

## Step 4 - Deploy the backend (optional, for live data + push later)

1. Push to GitHub if you haven't
2. Render -> New -> Web Service -> root directory `backend`
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Add your `.env` variables in Render's dashboard (Garmin email/password,
   Anthropic API key, race details)
6. You'll get a backend URL like `https://race-coach-api.onrender.com`

Once deployed, tighten CORS in `main.py` - change `allow_origins=["*"]` to
your actual frontend URL from Step 3.

## Step 5 - Install it as an app

- **iPhone:** open your deployed frontend URL in Safari -> Share -> "Add to
  Home Screen"
- **Mac:** open it in Safari or Chrome -> "Add to Dock" / install icon in
  the address bar

It now behaves like a real app - own icon, launches full-screen.

## Step 6 - Set a spend cap (do this before anything else touches Claude)

Go to console.anthropic.com/settings/limits and set a monthly cap. This
protects you regardless of what happens later.

## What's next, when you're ready

The frontend and backend are now wired together — go to Settings, paste your
deployed backend URL into "Live data connection," hit Connect, and the whole
app switches to live Garmin data, a real Claude-generated morning report,
and a live Ask Coach chat. Leave it blank and it falls back to the offline
snapshot automatically, so the app never breaks if the backend is down.

**To actually go live:**
1. Deploy the backend (Step 4 above)
2. Open your deployed frontend, go to Settings → paste the backend URL → Connect
3. That's it — Overview, Training, and Ask Coach all start pulling real data

One thing to set up for push notifications specifically: generate VAPID keys
(see `push.py`'s docstring) and add them to your backend's `.env` — the
subscribe flow is wired up in the frontend already, it just needs those keys
present server-side to actually deliver anything.

## Fixing the "Cloudflare bot challenge" login error

If your deployed backend fails to log into Garmin with an error mentioning
"Cloudflare bot challenge," that's Garmin blocking the login attempt because
it's coming from a datacenter server (Render), not a normal home connection.
This is a known limitation of unofficial Garmin API access, not a bug.

**The fix — generate a resumable session locally, then hand it to Render:**

```bash
cd ~/Downloads/race-coach/backend
source venv/bin/activate
python3 generate_garmin_tokens.py
```

This logs in from your Mac (which Garmin doesn't block) and prints a long
base64 string. Copy that whole string, then:

1. Go to Render → your service → Settings → Environment Variables
2. Add a new one: key `GARMIN_TOKENS_B64`, value = the string you copied
3. Save, then trigger a manual redeploy

The backend will now resume that saved session instead of attempting a
fresh password login, which sidesteps the Cloudflare block entirely.
Sessions typically last for months; if it ever needs refreshing, just
re-run the script and update the Render variable.

