import os
import time
from datetime import datetime, timezone

import requests
import streamlit as st
import pandas as pd

# ── Configuration ─────────────────────────────────────────────────────────────
API_BASE = os.getenv("API_BASE", "http://web:8000")        # docker-compose service name
REFRESH_SECONDS = 5
DEFAULT_STORE = "ST1008"

SEVERITY_COLOR = {
    "CRITICAL": "🔴",
    "WARN":     "🟡",
    "INFO":     "🔵",
}

# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Apex Retail — Store Intelligence",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .metric-card {
        background: #1e1e2e;
        border-radius: 12px;
        padding: 16px 20px;
        margin-bottom: 8px;
    }
    .anomaly-card {
        border-left: 4px solid;
        padding: 10px 16px;
        margin-bottom: 8px;
        border-radius: 0 8px 8px 0;
    }
    .critical { border-color: #ff4b4b; background: #2a1a1a; }
    .warn     { border-color: #ffa500; background: #2a2000; }
    .info     { border-color: #4b9aff; background: #001a2a; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🛍️ Apex Retail")
    st.subheader("Store Intelligence")
    st.divider()

    store_id = st.text_input("Store ID", value=DEFAULT_STORE)
    window_hours = st.slider("Window (hours)", min_value=1, max_value=48, value=24)
    refresh_rate = st.selectbox("Refresh rate", [3, 5, 10, 30], index=1, format_func=lambda x: f"{x}s")

    st.divider()
    auto_refresh = st.toggle("Auto-refresh", value=True)
    if st.button("🔄 Refresh now"):
        st.rerun()

    st.divider()
    st.caption(f"API: `{API_BASE}`")
    st.caption(f"Last check: {datetime.now(tz=timezone.utc).strftime('%H:%M:%S')} UTC")


# ── API helpers ───────────────────────────────────────────────────────────────
def fetch(path: str, params: dict | None = None) -> dict | None:
    try:
        r = requests.get(f"{API_BASE}{path}", params=params or {}, timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        st.error(f"❌ Cannot reach API at `{API_BASE}`. Is the `web` service running?")
        return None
    except requests.exceptions.HTTPError as e:
        st.warning(f"API returned {e.response.status_code} for `{path}`")
        return None
    except Exception as e:
        st.warning(f"Unexpected error fetching `{path}`: {e}")
        return None


# ── Main dashboard ────────────────────────────────────────────────────────────
st.title("📊 Live Store Intelligence")
st.caption(f"Store: **{store_id}** · {window_hours}h window · refreshing every {refresh_rate}s")

params = {"window_hours": window_hours}

# ── Row 1: Health banner ──────────────────────────────────────────────────────
health = fetch("/health")
if health:
    stores_map = {s["store_id"]: s for s in health.get("stores", [])}
    feed_status = stores_map.get(store_id, {})
    db_ok = health.get("db_connected", False)

    col_h1, col_h2, col_h3 = st.columns(3)
    with col_h1:
        icon = "✅" if health.get("status") == "healthy" else "⚠️"
        st.metric("API Status", f"{icon} {health.get('status', 'unknown').capitalize()}")
    with col_h2:
        db_icon = "✅" if db_ok else "❌"
        st.metric("Database", f"{db_icon} {'Connected' if db_ok else 'Disconnected'}")
    with col_h3:
        feed_icon = "✅" if feed_status.get("status") == "OK" else ("⚠️" if feed_status else "—")
        lag = feed_status.get("lag_seconds")
        lag_str = f"{lag:.0f}s lag" if lag is not None else "no data yet"
        st.metric("Feed Status", f"{feed_icon} {feed_status.get('status', 'NO_DATA')}", delta=lag_str)

st.divider()

# ── Row 2: Core KPIs ──────────────────────────────────────────────────────────
metrics = fetch(f"/stores/{store_id}/metrics", params)

if metrics:
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("👥 Unique Visitors", metrics["unique_visitors"])
    with c2:
        conv_pct = f"{metrics['conversion_rate'] * 100:.1f}%"
        st.metric("💳 Conversion Rate", conv_pct)
    with c3:
        dwell_s = metrics["avg_dwell_ms"] / 1000
        st.metric("⏱️ Avg Dwell", f"{dwell_s:.0f}s")
    with c4:
        st.metric("🧾 Queue Depth", metrics["queue_depth"])
    with c5:
        ab_pct = f"{metrics['abandonment_rate'] * 100:.1f}%"
        st.metric("🚪 Abandonment", ab_pct)

    conf_label = "✅ High confidence" if metrics.get("data_confidence") else "⚠️ Low data (<20 sessions)"
    st.caption(conf_label)
else:
    st.info(f"No metrics available for store `{store_id}` yet. Ingest some events first.")

st.divider()

# ── Row 3: Funnel + Heatmap (side by side) ────────────────────────────────────
col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("🔽 Conversion Funnel")
    funnel = fetch(f"/stores/{store_id}/funnel", params)

    if funnel and funnel.get("stages"):

        stages = funnel["stages"]
        df = pd.DataFrame(stages)

        # Horizontal bar chart
        st.bar_chart(
            df.set_index("stage")["count"],
            color="#4b9aff",
            use_container_width=True,
            horizontal=True,
        )

        # Table with drop-off
        df["drop_off_pct"] = df["drop_off_pct"].apply(lambda x: f"{x:.1f}%")
        st.dataframe(
            df[["stage", "count", "drop_off_pct"]].rename(columns={
                "stage": "Stage",
                "count": "Visitors",
                "drop_off_pct": "Drop-off",
            }),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No funnel data yet.")

with col_right:
    st.subheader("🔥 Zone Heatmap")
    heatmap = fetch(f"/stores/{store_id}/heatmap", params)

    if heatmap and heatmap.get("zones"):

        zones = heatmap["zones"]
        df_h = pd.DataFrame(zones)
        df_h = df_h.sort_values("normalised", ascending=False)

        # Render heatmap as a styled table with color bars
        st.dataframe(
            df_h[["zone_id", "visit_count", "avg_dwell_ms", "normalised"]].rename(columns={
                "zone_id":      "Zone",
                "visit_count":  "Visits",
                "avg_dwell_ms": "Avg Dwell (ms)",
                "normalised":   "Heat (0–100)",
            }),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Heat (0–100)": st.column_config.ProgressColumn(
                    "Heat (0–100)",
                    min_value=0,
                    max_value=100,
                    format="%.1f",
                ),
            },
        )

        conf_msg = (
            "✅ Data confidence: high (≥20 sessions)"
            if heatmap.get("data_confidence")
            else "⚠️ Data confidence: low (<20 sessions)"
        )
        st.caption(conf_msg)
    else:
        st.info("No heatmap data yet.")

st.divider()

# ── Row 4: Zone dwell breakdown ───────────────────────────────────────────────
if metrics and metrics.get("zone_dwells"):
    st.subheader("📍 Zone Dwell Breakdown")

    df_dwell = pd.DataFrame(metrics["zone_dwells"])
    df_dwell["avg_dwell_s"] = (df_dwell["avg_dwell_ms"] / 1000).round(1)
    df_dwell = df_dwell.sort_values("avg_dwell_s", ascending=False)

    st.bar_chart(
        df_dwell.set_index("zone_id")["avg_dwell_s"],
        color="#a855f7",
        use_container_width=True,
    )

    st.divider()

# ── Row 5: Active Anomalies ───────────────────────────────────────────────────
st.subheader("🚨 Active Anomalies")
anomalies_data = fetch(f"/stores/{store_id}/anomalies")

if anomalies_data:
    anomalies = anomalies_data.get("anomalies", [])
    if anomalies:
        for a in anomalies:
            sev = a["severity"].lower()
            icon = SEVERITY_COLOR.get(a["severity"], "⚪")
            with st.container():
                st.markdown(f"""
<div class="anomaly-card {sev}">
<strong>{icon} {a['anomaly_type']}</strong> — <em>{a['severity']}</em><br/>
{a['description']}<br/>
<small>💡 {a['suggested_action']}</small><br/>
<small>Detected at: {a.get('detected_at', '—')}</small>
</div>
""", unsafe_allow_html=True)
    else:
        st.success("✅ No active anomalies detected.")

st.divider()

# ── Footer: Raw event window info ─────────────────────────────────────────────
if metrics:
    st.caption(
        f"Window: `{metrics.get('window_start', '—')}` → `{metrics.get('window_end', '—')}`"
    )

# ── Auto-refresh loop ─────────────────────────────────────────────────────────
if auto_refresh:
    time.sleep(refresh_rate)
    st.rerun()