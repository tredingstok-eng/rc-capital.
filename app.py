# RC Capital — Private Fund Management Platform
from __future__ import annotations
import streamlit as st
import pandas as pd
import requests
import xml.etree.ElementTree as ET
import time
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — edit these values before deploying
# ═══════════════════════════════════════════════════════════════════════════════
IBKR_TOKEN    = "837126977366730658372732"
IBKR_QUERY_ID = "1492787"
ADMIN_PIN     = "0000"
TAX_RATE      = 0.25
MANAGER_NAME  = "raphael cohen"   # Exempt from 25% capital gains tax
DEBUG         = True               # Set to False before going live

# Google Sheets — direct export URL (sheet must be "Anyone with link can view")
# Column layout (positional): 0=Name, 1=PIN, 2=Deposit($), 3=Share(%)
GOOGLE_SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/1AuspdxTTFAoAYqgU0bpGko6-Z1PUcI3Zs7kF-ixjVC0/export?format=csv&gid=1570318828"

FLEX_REQUEST_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
FLEX_GET_URL     = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="RC Capital",
    page_icon="💎",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL CSS — dark theme + Hebrew RTL
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
html, body, [class*="css"] {
    font-family: 'Segoe UI', 'Arial Hebrew', Arial, sans-serif;
    direction: rtl;
    text-align: right;
}
.stApp {
    background: linear-gradient(160deg, #07101e 0%, #0c1a2e 100%);
}
[data-testid="stSidebar"] {
    background: rgba(255,255,255,0.03);
    direction: rtl;
}
[data-testid="metric-container"] {
    direction: rtl;
    text-align: right;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,215,0,0.2);
    border-radius: 12px;
    padding: 16px;
}
.rc-card {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,215,0,0.25);
    border-radius: 14px;
    padding: 22px 26px;
    margin-bottom: 14px;
}
.rc-title  { color: #90CAF9; font-size: 0.9rem; margin-bottom: 4px; }
.rc-value  { color: #ffffff; font-size: 2rem; font-weight: 700; }
.rc-label  { color: #FFD700; font-size: 1.1rem; font-weight: 600; }
.rc-pos    { color: #00E676; }
.rc-neg    { color: #FF5252; }
.rc-gold   { color: #FFD700; }
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION STATE DEFAULTS
# ═══════════════════════════════════════════════════════════════════════════════
_defaults = {
    "authenticated": False,
    "is_admin":      False,
    "user_row":      None,   # dict — authenticated user's sheet row
    "nav":           None,   # float — current master portfolio NAV
    "nav_source":    None,   # "ibkr" | "manual"
    "nav_ts":        None,   # timestamp string
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ═══════════════════════════════════════════════════════════════════════════════
# DATA & API HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def load_users() -> pd.DataFrame:
    from io import StringIO
    cb  = int(time.time())
    url = f"{GOOGLE_SHEET_CSV_URL}&cb={cb}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            raise PermissionError(
                "הגיליון לא נגיש (404). "
                "פתח את Google Sheets ← Share ← שנה ל-'Anyone with the link' (Viewer) ← Done"
            )
        resp.raise_for_status()
        # Force UTF-8 — Google Sheets exports in UTF-8 regardless of headers
        df = pd.read_csv(StringIO(resp.content.decode("utf-8")), dtype=str)
    except PermissionError:
        raise
    except Exception as e:
        raise ConnectionError(f"שגיאה בטעינת הגיליון: {e}")

    # Strip apostrophes Google Sheets adds when cells are formatted as text (e.g. '1919 → 1919)
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip("'").str.strip()

    for col in df.columns[2:]:
        df[col] = (df[col].astype(str)
                   .str.replace("$", "", regex=False)
                   .str.replace("%", "", regex=False)
                   .str.replace(",", "", regex=False)
                   .str.strip())
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def ibkr_send_request() -> str | None:
    """Phase 1 — ask IBKR to generate the report. Returns reference code or '1019'."""
    try:
        r    = requests.get(FLEX_REQUEST_URL,
                            params={"t": IBKR_TOKEN, "q": IBKR_QUERY_ID, "v": "3"},
                            timeout=30)
        root = ET.fromstring(r.text)
        if root.findtext("Status") == "Success":
            return root.findtext("ReferenceCode")
        if root.findtext("ErrorCode") == "1019":
            return "1019"
        st.error(f"IBKR: {root.findtext('ErrorMessage', 'שגיאה לא ידועה')}")
    except Exception as e:
        st.error(f"בקשת IBKR נכשלה: {e}")
    return None


def ibkr_get_nav(ref_code: str) -> float | None:
    """Phase 2 — download and parse the report, return NAV total."""
    try:
        cb   = int(time.time())
        r    = requests.get(f"{FLEX_GET_URL}?cb={cb}",
                            params={"t": IBKR_TOKEN, "q": ref_code, "v": "3"},
                            timeout=30)
        root = ET.fromstring(r.text)

        # Still processing
        for node in root.iter("FlexStatementResponse"):
            if node.findtext("ErrorCode") in ("1019", "1020"):
                return None

        # Use the most recent EquitySummaryByReportDateInBase entry
        nodes = root.findall(".//EquitySummaryByReportDateInBase")
        for node in reversed(nodes):
            total = node.get("total")
            if total:
                return float(total)
    except Exception as e:
        st.error(f"ניתוח IBKR נכשל: {e}")
    return None


def calc_net(nav: float, share: float, deposit: float, is_manager: bool) -> dict:
    gross = nav * (share / 100.0)
    pnl   = gross - deposit
    tax   = (pnl * TAX_RATE) if (pnl > 0 and not is_manager) else 0.0
    return {"gross": gross, "pnl": pnl, "tax": tax, "net": gross - tax}


def usd(v: float) -> str:
    return f"${v:,.2f}"

# ═══════════════════════════════════════════════════════════════════════════════
# SHARED — NAV CONTROL PANEL (admin only, shown on admin page)
# ═══════════════════════════════════════════════════════════════════════════════
def nav_control_panel():
    st.markdown("### 🛰️ עדכון NAV")
    col_ibkr, col_manual = st.columns(2)

    with col_ibkr:
        if st.button("📡 משוך מ-IBKR", use_container_width=True):
            with st.spinner("שולח בקשה ל-IBKR..."):
                ref = ibkr_send_request()

            if ref == "1019":
                st.warning("⏳ IBKR מגביל בקשות. יש להמתין 3 דקות ולנסות שוב.")
            elif ref:
                with st.spinner("ממתין לעיבוד הדוח (5 שניות)..."):
                    time.sleep(5)
                nav = ibkr_get_nav(ref)
                if nav:
                    st.session_state.nav       = nav
                    st.session_state.nav_source = "ibkr"
                    st.session_state.nav_ts     = datetime.now().strftime("%H:%M:%S")
                    st.success(f"✅ NAV עודכן: {usd(nav)}")
                    st.rerun()
                else:
                    st.error("לא ניתן לחלץ NAV. נסה שוב בעוד מספר דקות.")

    with col_manual:
        with st.expander("✏️ עדכון ידני (Override)"):
            manual = st.number_input(
                "NAV נוכחי ($)",
                min_value=0.0,
                value=float(st.session_state.nav or 0),
                step=100.0,
                format="%.2f",
            )
            if st.button("אשר עדכון ידני", use_container_width=True):
                st.session_state.nav        = manual
                st.session_state.nav_source = "manual"
                st.session_state.nav_ts     = datetime.now().strftime("%H:%M:%S")
                st.success(f"✅ NAV עודכן ידנית: {usd(manual)}")
                st.rerun()

    if st.session_state.nav:
        src = "IBKR" if st.session_state.nav_source == "ibkr" else "עדכון ידני"
        st.caption(f"NAV פעיל: **{usd(st.session_state.nav)}** | מקור: {src} | {st.session_state.nav_ts}")

# ═══════════════════════════════════════════════════════════════════════════════
# LOGIN SCREEN
# ═══════════════════════════════════════════════════════════════════════════════
def page_login():
    _, mid, _ = st.columns([1.2, 1, 1.2])
    with mid:
        st.markdown("""
        <div style='text-align:center; padding-top:60px;'>
            <h1 style='color:#FFD700; font-size:3rem; letter-spacing:6px; margin-bottom:0;'>💎 RC</h1>
            <h2 style='color:#FFD700; font-size:2rem; letter-spacing:6px; margin-top:0;'>CAPITAL</h2>
            <p style='color:#90CAF9; margin-bottom:30px;'>פלטפורמת ניהול השקעות פרטיות</p>
        </div>
        """, unsafe_allow_html=True)

        # st.form lets Enter key submit the login
        with st.form("login_form"):
            pin = st.text_input("קוד כניסה (PIN)", type="password",
                                placeholder="הזן קוד גישה", label_visibility="collapsed")
            submitted = st.form_submit_button("כניסה →", use_container_width=True,
                                              type="primary")
        if submitted:
            _do_login(pin.strip())


def _do_login(pin: str):
    if pin == ADMIN_PIN:
        st.session_state.authenticated = True
        st.session_state.is_admin      = True
        st.rerun()
        return

    try:
        df    = load_users()

        if DEBUG:
            with st.expander("🐛 Debug — login"):
                st.write("PIN entered:", repr(pin))
                st.write("PIN column values:", df.iloc[:, 1].tolist())
                st.write("Row count:", len(df))
                st.dataframe(df)

        # PIN is always column index 1 (positional, regardless of header name)
        match = df[df.iloc[:, 1].astype(str).str.strip() == pin]
        if match.empty:
            st.error("קוד PIN שגוי. נסה שנית.")
            return
        row = match.iloc[0]
        st.session_state.authenticated = True
        st.session_state.is_admin      = False
        st.session_state.user_row      = {
            "name":    str(row.iloc[0]),          # col 0 — Name
            "deposit": float(row.iloc[2]),         # col 2 — Initial deposit ($)
            "share":   float(row.iloc[3]),         # col 3 — Share (%)
        }
        st.rerun()
    except Exception as e:
        st.error(f"שגיאה בטעינת נתוני משתמשים: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — COMMAND CENTER
# ═══════════════════════════════════════════════════════════════════════════════
def page_admin():
    st.markdown("""
    <h1 style='color:#FFD700; text-align:center; letter-spacing:3px;'>🏦 RC Capital — מרכז פיקוד</h1>
    <p style='color:#90CAF9; text-align:center; margin-top:-8px;'>Admin Command Center</p>
    """, unsafe_allow_html=True)

    nav_control_panel()
    st.markdown("---")

    if not st.session_state.nav:
        st.info("יש לעדכן NAV על מנת לראות חישובים.")
        _logout_btn()
        return

    nav = st.session_state.nav

    try:
        df = load_users()
    except Exception as e:
        st.error(f"שגיאה בטעינת משקיעים: {e}")
        _logout_btn()
        return

    st.markdown(f"### 📊 כל המשקיעים | NAV הכולל: {usd(nav)}")

    rows = []
    for _, row in df.iterrows():
        try:
            name    = str(row.iloc[0])
            deposit = float(row.iloc[2])
            share   = float(row.iloc[3])
            is_mgr  = name.strip().lower() == MANAGER_NAME.lower()
            c       = calc_net(nav, share, deposit, is_mgr)
            rows.append({
                "שם":             name,
                "נתח (%)":        f"{share:.2f}%",
                "הפקדה":          usd(deposit),
                "שווי ברוטו":     usd(c["gross"]),
                "רווח / הפסד":    usd(c["pnl"]),
                "מס (25%)":       usd(c["tax"]),
                "שווי נטו":       usd(c["net"]),
                "_net_raw":        c["net"],
                "_gross_raw":      c["gross"],
                "_share_raw":      share,
            })
        except Exception:
            continue

    if not rows:
        st.warning("אין נתוני משקיעים.")
        _logout_btn()
        return

    display = pd.DataFrame(rows).drop(columns=["_net_raw", "_gross_raw", "_share_raw"])
    st.dataframe(display, use_container_width=True, hide_index=True)

    # Bar chart — gross vs net per investor
    names   = [r["שם"] for r in rows]
    grosses = [r["_gross_raw"] for r in rows]
    nets    = [r["_net_raw"]   for r in rows]

    fig_bar = go.Figure()
    fig_bar.add_trace(go.Bar(name="שווי ברוטו", x=names, y=grosses, marker_color="#4FC3F7"))
    fig_bar.add_trace(go.Bar(name="שווי נטו",   x=names, y=nets,    marker_color="#FFD700"))
    fig_bar.update_layout(
        title="השוואת שווי ברוטו vs. נטו לכל משקיע",
        barmode="group",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0.2)",
        font=dict(color="white"),
        legend=dict(orientation="h", y=1.1),
    )
    st.plotly_chart(fig_bar, use_container_width=True)

    # Pie chart — share distribution
    shares = [r["_share_raw"] for r in rows]
    fig_pie = go.Figure(go.Pie(
        labels=names,
        values=shares,
        hole=0.45,
        marker=dict(colors=px.colors.qualitative.Bold),
        textinfo="label+percent",
    ))
    fig_pie.update_layout(
        title="חלוקת נתחים בפורטפוליו",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="white"),
    )
    st.plotly_chart(fig_pie, use_container_width=True)

    _logout_btn()

# ═══════════════════════════════════════════════════════════════════════════════
# USER — PERSONAL DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════
def page_user():
    row     = st.session_state.user_row
    name    = str(row.get("name",    "משקיע"))
    share   = float(row.get("share",   0))
    deposit = float(row.get("deposit", 0))
    is_mgr  = name.strip().lower() == MANAGER_NAME.lower()

    st.markdown(f"""
    <h1 style='color:#FFD700; text-align:center; letter-spacing:3px;'>💎 RC Capital</h1>
    <h3 style='color:#90CAF9; text-align:center; margin-top:-8px;'>שלום, {name} 👋</h3>
    """, unsafe_allow_html=True)

    # Fund manager can also manually update NAV
    if is_mgr:
        nav_control_panel()
        st.markdown("---")

    if not st.session_state.nav:
        st.info("ממתין לעדכון NAV מהמנהל. נסה שוב מאוחר יותר.")
        _logout_btn()
        return

    nav = st.session_state.nav
    c   = calc_net(nav, share, deposit, is_mgr)

    # ── Top KPI strip ──────────────────────────────────────────────────────────
    k1, k2, k3, k4 = st.columns(4)
    pnl_label = "📈 רווח" if c["pnl"] >= 0 else "📉 הפסד"
    k1.metric("💰 שווי נטו",          usd(c["net"]))
    k2.metric(pnl_label,               usd(c["pnl"]),
              delta=f"{c['pnl']/deposit*100:.1f}%" if deposit else None)
    k3.metric("🏦 שווי ברוטו",        usd(c["gross"]))
    k4.metric("🧾 מס מוערך",          usd(c["tax"]))

    st.markdown("---")

    # ── Info cards ─────────────────────────────────────────────────────────────
    col_l, col_r = st.columns(2)
    with col_l:
        st.markdown(f"""
        <div class="rc-card">
            <div class="rc-title">NAV כולל — פורטפוליו מאסטר (IBKR)</div>
            <div class="rc-value">{usd(nav)}</div>
        </div>
        <div class="rc-card">
            <div class="rc-title">הפקדה ראשונית</div>
            <div class="rc-value">{usd(deposit)}</div>
        </div>
        """, unsafe_allow_html=True)
    with col_r:
        pnl_cls = "rc-pos" if c["pnl"] >= 0 else "rc-neg"
        st.markdown(f"""
        <div class="rc-card">
            <div class="rc-title">נתח שלך בפורטפוליו</div>
            <div class="rc-value">{share:.2f}%</div>
        </div>
        <div class="rc-card">
            <div class="rc-title">רווח / הפסד נוכחי</div>
            <div class="rc-value"><span class="{pnl_cls}">{usd(c["pnl"])}</span></div>
        </div>
        """, unsafe_allow_html=True)

    # ── Gauge chart ────────────────────────────────────────────────────────────
    gauge_max = max(nav * 1.1, c["net"] * 1.2, deposit * 1.2)
    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=c["net"],
        delta={"reference": deposit, "valueformat": ",.0f", "prefix": "$",
               "increasing": {"color": "#00E676"}, "decreasing": {"color": "#FF5252"}},
        number={"prefix": "$", "valueformat": ",.2f", "font": {"color": "#FFD700", "size": 36}},
        title={"text": "שווי נטו שלך", "font": {"color": "white", "size": 18}},
        gauge={
            "axis": {"range": [0, gauge_max], "tickcolor": "white",
                     "tickformat": "$,.0f"},
            "bar":  {"color": "#FFD700", "thickness": 0.25},
            "bgcolor": "rgba(0,0,0,0)",
            "steps": [
                {"range": [0,       deposit],   "color": "rgba(255,82,82,0.2)"},
                {"range": [deposit, gauge_max],  "color": "rgba(0,230,118,0.1)"},
            ],
            "threshold": {
                "line": {"color": "#4FC3F7", "width": 3},
                "thickness": 0.8,
                "value": deposit,
            },
        },
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="white"),
        height=320,
        margin=dict(t=60, b=20),
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── Tax breakdown ──────────────────────────────────────────────────────────
    if is_mgr:
        st.info("כמנהל הקרן, חישוב מס רווחי הון אינו חל עליך.")
    elif c["pnl"] > 0:
        st.markdown(f"""
        <div class="rc-card">
            <div class="rc-label">📋 פירוט מס רווחי הון (25%)</div>
            <br>
            <table style="width:100%; color:#ccc; direction:rtl;">
                <tr><td>שווי ברוטו</td><td style="text-align:left">{usd(c['gross'])}</td></tr>
                <tr><td>הפקדה ראשונית</td><td style="text-align:left">− {usd(deposit)}</td></tr>
                <tr><td>רווח חייב במס</td><td style="text-align:left">{usd(c['pnl'])}</td></tr>
                <tr><td>מס (× 25%)</td>
                    <td style="text-align:left; color:#FF5252;">− {usd(c['tax'])}</td></tr>
                <tr style="border-top:1px solid #FFD700;">
                    <td style="color:#FFD700; font-weight:bold; padding-top:8px;">שווי נטו לאחר מס</td>
                    <td style="text-align:left; color:#FFD700; font-weight:bold; padding-top:8px;">{usd(c['net'])}</td>
                </tr>
            </table>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.info("אין רווח כרגע — לא חל מס על הפסד.")

    src = "IBKR" if st.session_state.nav_source == "ibkr" else "עדכון ידני"
    st.caption(f"עדכון אחרון: {st.session_state.nav_ts} | מקור: {src}")

    _logout_btn()

# ═══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════
def _logout_btn():
    with st.sidebar:
        if st.button("🚪 התנתקות", use_container_width=True):
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════════
if not st.session_state.authenticated:
    page_login()
elif st.session_state.is_admin:
    page_admin()
else:
    page_user()
