# PSX Live Scanner

Live PSX prices, rule-based buy setups, and a full trade plan (shares, stop-loss, target, net profit after costs and tax) for each one. It refreshes automatically every 60 seconds.

## Put it online (free, ~15 minutes, one time)

1. **Make a GitHub account** at github.com/signup (free).
2. **Create a repository**: click **+** at the top right, then **New repository**. Name it `psx-scanner`, choose **Public**, and click **Create repository**.
3. **Upload the files**: on the new repo page, click **uploading an existing file**. Drag in `app.py`, `psx_client.py`, `signals.py` and `requirements.txt`, then click **Commit changes**.
4. **Deploy**: go to share.streamlit.io and sign in with GitHub. Click **Create app**, then choose deploy from a GitHub repo:
   - Repository: `your-username/psx-scanner`
   - Branch: `main`
   - Main file: `app.py`
   - Under Advanced settings, pick Python 3.12.
   - Click **Deploy**. The first build takes 2–3 minutes.
5. You get a link like `psx-scanner-xxxx.streamlit.app`. Bookmark it, or use your phone's **Add to Home Screen**.

On the free tier, the app goes to sleep after a few days without visitors. Open the link and click the wake-up button. It's back in about 30 seconds.

## ntfy alerts (phone push notifications, free)

The app scans every minute in the background and sends a push notification to your phone:
- 🟢 a new buy setup, with the buy price, stop, target and number of shares
- 🔴 your stop-loss is hit (sell). Sent as **urgent**, so it rings loudly.
- ✅ your target is hit (sell)
- 🔵 you're up 1× your risk (move your stop to breakeven)
- ⏰ 15 minutes before the close, while you still hold a position

**One-time setup (about 2 minutes, no account needed):**
1. Install the **ntfy** app from the Play Store or App Store.
2. Open the app, tap **+** (Subscribe to topic) and enter the private channel name shown in the website's sidebar under **Phone alerts**, for example `psx-hassan-3f9a1c2b7d`. Leave the server as ntfy.sh. Keep this name private: anyone who knows it can read your alerts.
3. In Streamlit Cloud: **Manage app → ⋮ → Settings → Secrets**, add:
   ```
   NTFY_TOPIC = "psx-hassan-3f9a1c2b7d"
   ```
   Save. The app restarts.
4. In the sidebar, click **Send test push**. It should arrive within seconds.

**Daily use:** open the site once each morning, before the market opens, to wake it up. After that you can close it and alerts keep coming. When you buy, press **I bought this** on the setup card (or add the position manually) so the app watches your stop and target. Press **I sold** after you exit.

Your open position is stored on the server. If the app restarts (after an update or a long sleep), add your position again.

## WhatsApp alerts (optional)

Uses CallMeBot, a free gateway that can only message your own WhatsApp number. It can be slow to activate.

1. Save **+34 644 97 54 14** in your phone contacts (any name, e.g. "CallMeBot"). If that number doesn't reply, check callmebot.com for the current one.
2. On WhatsApp, send it exactly: `I allow callmebot to send me messages`
3. Within ~2 minutes it replies with your **APIKEY**. If not, try again after 24 hours.
4. In Secrets, add:
   ```
   WHATSAPP_PHONE = "+923001234567"
   WHATSAPP_APIKEY = "123456"
   ```
   Use your own number with +92 and no leading 0.
5. In the sidebar, click **Send test WhatsApp**.

When ntfy and WhatsApp are both set up, every alert goes to both.

## Broker calls panel

Shows trade calls from your broker's WhatsApp channels with a sense check: live status (waiting, in buy zone, target or stop hit), what happened since the call (from PSX daily history), a position size that keeps one stop-out near your chosen max loss, profit and loss after KTrade costs and tax, and warning flags.

- **Data:** a secret GitHub gist (`calls.json`) that Claude updates hourly on market days by reading the WhatsApp channels in your Chrome. Your computer must be awake with Chrome open for those updates.
- **Connect it once:** in Streamlit Secrets add
  ```
  BROKER_CALLS_URL = "https://gist.githubusercontent.com/<you>/<gist-id>/raw/calls.json"
  ```
- **Update now** reloads the file and rechecks prices and history straight away.
- **Paste a call:** copy a broker message from WhatsApp and paste it to add it instantly.
- Push alerts: a new call appears, the price enters the buy zone, TP1 is hit, or the stop is hit.

## Strategy: core + satellite (24 Nov 2026)

A backtest on real PSX daily prices (2022 to Nov 2026, KTrade costs, 15% tax on gains) found that simply
holding the KSE-100 beat every trading rule tested, in nearly every year. So the app now splits your money:

- **Core (default 80%)**: buy and hold the market. The CORE panel suggests the 8 biggest KSE-100 companies split by
  index weight (or a PSX-listed ETF), and shows the KSE-100's trend and returns. Add savings monthly; rebalance every
  3 months.
- **Satellite (the rest)**: swing trades held 2–5 days, signalled in the last 30 minutes of the day (SWING panel,
  push alerts 3:00–3:27 pm). Only the strongest stocks by 3-month return, only when the KSE-100 is in an uptrend.
  Breakout (new 20-day high on strong volume) or pullback (dip to the 10-day average after a breakout). Stop below
  the recent low (2–5%), target 2× the risk, breakeven at +1× risk, sold at the close of day 4.
- **BACKTEST panel**: runs the swing rules in the sidebar on ~3 years of real prices and compares them with holding
  the KSE-100, year by year. Change the rules in the sidebar to test ideas before risking money.
- **Intraday** signals are paper-only now (they lost money on paper 17–23 Nov): momentum breakouts in the first
  45 minutes only, VWAP bounce is watch-only, intraday buy alerts are off by default. Same-day sells are charged no
  commission (KTrade's intraday pricing).

The app loads daily price history for the KSE-100 stocks once a day (about two minutes, in the background).

## Paper trades (does the scanner actually make money?)

From the first market day the app logs every buy setup as a paper trade: bought at the setup's price with the
suggested share count, then sold at the target or stop (stop moved to breakeven at +1× risk), or 15 minutes
before the close. Results are after KTrade costs and capital gains tax. The PAPER panel shows the win rate,
average rupees per trade and a breakdown by signal type, and you can download it as CSV. A short summary is
pushed to your phone 10 minutes before the close.

**Journal (from 18 Nov).** Each paper trade also records what things looked like when it was opened: time,
the stock's move and volume vs normal, distance from VWAP, stop width, the rules version, and the market's mood
(average move of KSE-100 stocks and the share of stocks up). It also records the best and worst price reached
and when. Setups skipped as "Costs too high" are followed the same way, kept separate. The **What's working**
panel groups results by signal, entry time, market mood, volume, stop width and rules version; **Download
journal (CSV)** gives every field for deeper study. After a week or two, use it to write new rules (for example
"only momentum breakouts before 11:00 on up days"). The 11 trades from 17 Nov are kept in `paper_seed.json`.

Give it at least 20 closed trades before trusting the numbers. If the average per trade is negative after
costs, don't trade the signals with real money yet.

**Back it up (recommended, 2 minutes).** Streamlit Cloud wipes files when the app restarts, so connect a
GitHub token and the log is saved to a private gist in your account:

1. Open https://github.com/settings/tokens/new?scopes=gist&description=PSX%20scanner%20paper%20trades
2. Set **Expiration** to 90 days (or longer), leave only **gist** ticked, and press **Generate token**.
3. Copy the token (starts with `ghp_`). In Streamlit: **Manage app → ⋮ → Settings → Secrets**, add
   `GITHUB_TOKEN = "ghp_..."` on a new line and press **Save**.

The panel's footnote changes to "Backed up to a private gist" once it's connected.

## If it says "Couldn't reach the PSX data portal"

Streamlit's servers are outside Pakistan, and PSX may block them. If the error keeps happening during market hours, run the same app on your laptop instead:

1. Install Python 3.12 from python.org. On Windows, tick "Add Python to PATH".
2. Open a terminal in the folder with these files and run:
   ```
   pip install -r requirements.txt
   streamlit run app.py
   ```
3. It opens in your browser at http://localhost:8501.

## Good to know

- **VWAP** is built from the price snapshots the app takes while it is open. Keep the app open from the market open so VWAP is meaningful.
- "Volume vs normal" compares today's volume with the stock's 30-day average, adjusted for how much of the trading day has passed.
- Every input is in the sidebar: capital, position size, stop distance, target multiple, filters, broker costs and market hours.
- PSX publishes no official API. If PSX changes its website, only `psx_client.py` needs updating.

Not financial advice. Always confirm the price in your broker's app before placing an order.
