# Mine Remote Control — Phone Dashboard

Control the Mine agent on your PC from your phone. The site on Vercel is just
a static page; it talks **directly** to Firebase Realtime Database, and the
bridge running on your PC (`ai_agent_framework/bridge.py`) relays commands.

```
Phone (this site) ──HTTPS──> Firebase Realtime DB <──polling── PC (bridge.py)
```

## One-time setup (your manual part, ~10 minutes)

### 1. Create the Firebase database

1. Go to https://console.firebase.google.com → **Add project** (name: `mine-agent`, analytics off is fine).
2. Left menu → **Build → Realtime Database** → **Create Database**.
3. Choose a location (e.g. `us-central1`), start in **locked mode**.
4. Copy the **Database URL** shown at the top, e.g.
   `https://mine-agent-abc123.firebaseio.com` (or `...-region1.firebasedatabase.app`).
5. Get your access token: ⚙️ **Project Settings → Service accounts →
   Database secrets → Show**. This legacy database secret is the token used by
   the dashboard and bridge below (`YOUR-TOKEN-HERE`).
6. Open the **Rules** tab and paste, then **Publish**:

```json
{
  "rules": {
    ".read": false,
    ".write": false
  }
}
```

> Requests that pass the database secret as `?auth=<secret>` on every REST call
> (exactly what the dashboard and bridge send) get full access and bypass these
> rules; everyone else is denied. Keep the secret private.

### 2. Deploy the dashboard to Vercel

1. Push this repo to GitHub, import it at https://vercel.com/new, set the
   **Root Directory** to `webapp`, deploy. (Or run `vercel` from `webapp/`
   with the Vercel CLI.)
2. Edit `webapp/index.html` before/after deploy — set:

```js
const FB_BASE = "mine-agent-abc123.firebaseio.com";  // NO https://
``` 3. Your dashboard URL: `https://YOUR-SITE.vercel.app/#/YOUR-DATABASE-SECRET`
   — open it once on your phone and bookmark it.

### 3. Run the bridge on your PC

```cmd
cd ai_agent_framework
pip install requests          # already in requirements.txt

set MINE_FIREBASE_DB_URL=https://mine-agent-abc123.firebaseio.com
set MINE_BRIDGE_TOKEN=YOUR-DATABASE-SECRET
python bridge.py
```

Keep `bridge.py` running (it's a daemon). `main.py` stays free for local use.
Prefer desktop-mode tasks from the phone? `set MINE_BRIDGE_MODE=desktop`.

## Usage

On the phone: type a goal → **Run task**. You'll see the live annotated
screenshot, each step's action/result in the log, PC online/offline state,
and a **Stop** button that halts the running task within ~2 seconds.

## Data layout (Firebase)

```
commands/task    {goal, token}   written by phone, consumed+deleted by bridge
commands/stop    true            written by phone, consumed+deleted by bridge
status/current   {state, step, thought, last_action, heartbeat, ts}
status/log       push list of step events (trimmed to last ~60)
status/image     base64 PNG of the latest annotated screenshot
```

## Security notes

- The token is the only secret; anyone with it can control your PC's agent.
  Keep the dashboard URL private.
- Domain restrictions (`MINE_ALLOWED_DOMAINS`) and the confirmation gate apply
  to bridge tasks exactly like local ones — **but** high-risk confirmations
  cannot be answered remotely, so bridge tasks that hit the gate are declined
  (check the log for "declined by user").
- The screenshot stream means anyone with the token sees your screen. Again:
  keep the URL private.
