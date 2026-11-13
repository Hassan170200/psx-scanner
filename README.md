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
