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
