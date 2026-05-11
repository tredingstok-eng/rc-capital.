import streamlit as st
import pandas as pd
import requests
import xml.etree.ElementTree as ET
import time
import plotly.graph_objects as go
from datetime import datetime

# ─── CONFIGURATION ──────────────────────────────────────────────────────────
# Move these to .streamlit/secrets.toml for production deployment
IBKR_TOKEN      = "837126977366730658372732"
IBKR_QUERY_ID   = "1492787"
ADMIN_PIN       = "0000"
TAX_RATE        = 0.25
IBKR_WAIT_SECS  = 12  # seconds to wait between SendRequest and GetStatement

# Google Sheet → File → Share → Publish to web → CSV format → paste URL below
# Expected columns: name | pin | share_pct | initial_capital | is_manager
GOOGLE_SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/1AuspdxTTFAoAYqgU0bpGko6-Z1PUcI3Zs7kF-ixjVC0/edit?gid=1570318828#gid=1570318828"

IBKR_SEND_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
IBKR_GET_URL  = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"

# ─── PAGE CONFIG ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="RC Capital | לוח בקרה",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─── GLOBAL STYLES ───────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@300;400;500;700;900&display=swap');

html, body, [class*="css"], .stApp {
    font-family: 'Heebo', sans-serif !important;
    direction: rtl;
    background: #060d18;
    color: #e6edf3;
}

h1, h2, h3, h4 { font-family: 'Heebo', sans-serif !important; }

/* KPI Cards */
.kpi-card {
    background: linear-gradient(145deg, #0f1f38 0%, #0c1a2e 100%);
    border: 1px solid #1d3557;
    border-radius: 14px;
    padding: 26px 22px;
    text-align: center;
    height: 100%;
}
.kpi-label { font-size: 0.78rem; color: #607b96; margin-bottom: 8px; letter-spacing: 0.05em; text-transform: uppercase; }
.kpi-value { font-size: 2rem; font-weight: 700; line-height: 1.1; }
.kpi-sub   { font-size: 0.75rem; color: #607b96; margin-top: 6px; }

/* Colour utilities */
.green { color: #3ddc97 !important; }
.red   { color: #ff5c5c !important; }
.gold  { color: #ffc857 !important; }
.white { color: #e6edf3 !important; }
.muted { color: #607b96 !important; }

/* Login centre */
.login-wrap { max-width: 380px; margin: 70px auto 0; text-align: center; }

/* Inputs */
.stTextInput > div > div > input {
    background: #0a1525 !important;
    border: 1px solid #1d3557 !important;
    color: #e6edf3 !important;
    border-radius: 10px !important;
    text-align: center;
    font-family: 'Heebo', sans-serif;
    font-size: 1.1rem;
    letter-spacing: 0.25em;
}
.stTextInput > div > div > input:focus { border-color: #1d6fa4 !important; box-shadow: none !important; }

/* Buttons */
.stButton > button {
    width: 100%;
    background: linear-gradient(135deg, #1d6fa4, #1450a3);
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    font-family: 'Heebo', sans-serif !important;
    font-size: 1rem !important;
    font-weight: 600 !important;
    padding: 12px !important;
    transition: opacity .2s;
}
.stButton > button:hover { opacity: 0.88; }

/* Divider */
hr { border-color: #1d3557 !important; }

/* DataFrame */
.stDataFrame { border: 1px solid #1d3557; border-radius: 10px; overflow: hidden; }
thead th { background: #0f1f38 !important; color: #8899b0 !important; font-family: 'Heebo', sans-serif; }

/* Number input */
.stNumberInput input { background: #0a1525 !important; color: #e6edf3 !important; border: 1px solid #1d3557 !important; }

/* Expander */
.streamlit-expanderHeader { background: #0f1f38 !important; border: 1px solid #1d3557 !important; border-radius: 10px !important; color: #e6edf3 !important; }
.streamlit-expanderContent { background: #0c1a2e !important; border: 1px solid #1d3557 !important; border-top: none !important; border-radius: 0 0 10px 10px !important; }

/* Alert boxes */
.stAlert { border-radius: 10px !important; }
</style>
""", unsafe_allow_html=True)


# ─── SESSION STATE INIT ──────────────────────────────────────────────────────
def _init_state():
    defaults = {
        "authenticated": False,
        "user_row":      None,
        "is_admin":      False,
        "nav_override":  None,
        "cached_nav":    None,
        "nav_timestamp": None,
        "ibkr_error":    None,
        "ibkr_loading":  False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()


# ─── DATA LOADERS ────────────────────────────────────────────────────────────
@st.cache_data(ttl=120)
def load_users() -> pd.DataFrame:
    bust = int(time.time())
    sep = "&" if "?" in GOOGLE_SHEET_CSV_URL else "?"
    url = f"{GOOGLE_SHEET_CSV_URL}{sep}cb={bust}"
    df = pd.read_csv(url)
    df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")
    df["pin"] = df["pin"].astype(str).str.strip()
    df["share_pct"] = pd.to_numeric(df["share_pct"], errors="coerce").fillna(0)
    df["initial_capital"] = pd.to_numeric(df["initial_capital"], errors="coerce").fillna(0)
    df["is_manager"] = df.get("is_manager", pd.Series(False, index=df.index)) \
                         .astype(str).str.lower().isin(["true", "1", "yes"])
    return df


def fetch_ibkr_nav() -> tuple:
    """Two-step IBKR Flex Web Service call. Returns (nav: float|None, error: str|None)."""
    try:
        # Step 1 – Send request
        r1 = requests.get(
            IBKR_SEND_URL,
            params={"t": IBKR_TOKEN, "q": IBKR_QUERY_ID, "v": "3"},
            timeout=15,
        )
        root1 = ET.fromstring(r1.text)

        status = root1.findtext("Status", "")
        if status == "Warn":
            code = root1.findtext("ErrorCode", "")
            msg  = root1.findtext("ErrorMessage", "Unknown error")
            if code == "1019":
                return None, "⏳ IBKR: יותר מדי בקשות. ממתין 3 דקות בין בקשות."
            return None, f"IBKR: {msg} (קוד {code})"

        ref_code = root1.findtext("ReferenceCode", "").strip()
        if not ref_code:
            return None, "לא התקבל קוד אסמכתא מ-IBKR."

        # Step 2 – Wait then fetch statement
        time.sleep(IBKR_WAIT_SECS)

        r2 = requests.get(
            IBKR_GET_URL,
            params={"t": IBKR_TOKEN, "q": ref_code, "v": "3"},
            timeout=30,
        )
        root2 = ET.fromstring(r2.text)

        # Still generating
        if root2.tag == "FlexStatementOperationMessage":
            err_msg = root2.findtext("ErrorMessage", "")
            return None, f"IBKR עדיין מעבד את הדוח: {err_msg}"

        # Find EquitySummaryByReportDateInBase → grab most recent total
        nodes = root2.findall(".//EquitySummaryByReportDateInBase")
        if not nodes:
            return None, "לא נמצא נתון NAV (EquitySummaryByReportDateInBase) בדוח IBKR."

        # Last entry = most recent date
        node = nodes[-1]
        total_str = node.get("total", "").replace(",", "").strip()
        if not total_str:
            return None, "שדה 'total' ריק בדוח IBKR. בדוק את הגדרות ה-Flex Query."

        return float(total_str), None

    except requests.Timeout:
        return None, "IBKR לא הגיב בזמן (timeout)."
    except ET.ParseError as exc:
        return None, f"שגיאת XML בפענוח תשובת IBKR: {exc}"
    except Exception as exc:
        return None, f"שגיאה בלתי צפויה: {exc}"


# ─── CALCULATION ENGINE ──────────────────────────────────────────────────────
def calculate(nav: float, share_pct: float, initial_capital: float, is_manager: bool) -> dict:
    gross = nav * (share_pct / 100.0)
    pnl   = gross - initial_capital
    tax   = (pnl * TAX_RATE) if (pnl > 0 and not is_manager) else 0.0
    net   = gross - tax
    roi   = ((net - initial_capital) / initial_capital * 100) if initial_capital else 0.0
    return {
        "gross":    gross,
        "pnl":      pnl,
        "tax":      tax,
        "net":      net,
        "net_pnl":  net - initial_capital,
        "roi":      roi,
    }


# ─── NAV RESOLVER ────────────────────────────────────────────────────────────
def get_effective_nav():
    """Returns (nav: float|None, source_label: str)."""
    if st.session_state.nav_override is not None:
        return st.session_state.nav_override, "עדכון ידני (מנהל)"
    if st.session_state.cached_nav is not None:
        ts = st.session_state.nav_timestamp
        ts_str = ts.strftime("%d/%m %H:%M") if ts else "—"
        return st.session_state.cached_nav, f"IBKR | {ts_str}"
    return None, "לא זמין"


# ─── HELPER: KPI CARD HTML ────────────────────────────────────────────────────
def kpi(label: str, value: str, sub: str = "", colour: str = "white") -> str:
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    return f"""
    <div class="kpi-card">
        <div class="kpi-label">{label}</div>
        <div class="kpi-value {colour}">{value}</div>
        {sub_html}
    </div>"""


# ─── LOGIN SCREEN ─────────────────────────────────────────────────────────────
def show_login():
    st.markdown("""
    <div class="login-wrap">
        <div style="font-size:3rem;">💼</div>
        <h1 style="font-size:2.4rem; font-weight:900; margin:8px 0 4px;">RC Capital</h1>
        <p class="muted" style="font-size:1rem; margin-bottom:36px;">ניהול תיק השקעות | לוח בקרה</p>
    </div>
    """, unsafe_allow_html=True)

    col_l, col_c, col_r = st.columns([1, 1.1, 1])
    with col_c:
        with st.form("login"):
            pin = st.text_input("קוד גישה", type="password", placeholder="••••", max_chars=20,
                                label_visibility="collapsed")
            ok = st.form_submit_button("כניסה →")

        if ok:
            print(pin.strip())
            pin = pin.strip()
            if pin == ADMIN_PIN:
                st.session_state.authenticated = True
                st.session_state.is_admin = True
                st.rerun()
            elif pin:
                try:
                    users = load_users()
                    match = users[users["pin"] == pin]
                    if not match.empty:
                        st.session_state.authenticated = True
                        st.session_state.is_admin = False
                        st.session_state.user_row = match.iloc[0].to_dict()
                        st.rerun()
                    else:
                        st.error("קוד גישה שגוי — נסה שנית.")
                except Exception as exc:
                    st.error(f"שגיאה בטעינת נתוני משתמשים: {exc}")


# ─── ADMIN COMMAND CENTER ─────────────────────────────────────────────────────
def show_admin():
    nav, nav_source = get_effective_nav()

    # ── Header ────────────────────────────────────────────────────────────────
    col_title, col_logout = st.columns([6, 1])
    with col_title:
        st.markdown("## 🏛️ Command Center — RC Capital")
    with col_logout:
        if st.button("התנתק"):
            st.session_state.clear()
            st.rerun()

    st.markdown("---")

    # ── Control Panel ─────────────────────────────────────────────────────────
    with st.expander("⚙️ פאנל ניהול", expanded=True):
        ctrl_left, ctrl_mid, ctrl_right = st.columns([2, 2, 1])

        with ctrl_left:
            st.markdown("**📝 עדכון NAV ידני (Admin Override)**")
            manual_val = float(st.session_state.nav_override) if st.session_state.nav_override else 0.0
            manual_nav = st.number_input("הזן NAV ($)", min_value=0.0, step=100.0,
                                         value=manual_val, format="%.2f", key="manual_nav_input")
            btn_a, btn_b = st.columns(2)
            with btn_a:
                if st.button("✅ החל עדכון"):
                    if manual_nav > 0:
                        st.session_state.nav_override = manual_nav
                        st.rerun()
                    else:
                        st.warning("הזן ערך גדול מ-0.")
            with btn_b:
                if st.button("🗑️ נקה Override"):
                    st.session_state.nav_override = None
                    st.rerun()

        with ctrl_mid:
            st.markdown("**📡 רענון מ-IBKR**")
            st.markdown(f"<p class='muted' style='font-size:0.8rem;'>TOKEN: ...{IBKR_TOKEN[-6:]} | Query ID: {IBKR_QUERY_ID}</p>",
                        unsafe_allow_html=True)
            if st.button("🔄 משוך נתונים מ-IBKR"):
                with st.spinner("שולח בקשה ל-IBKR... (עשוי לקחת ~15 שניות)"):
                    fetched_nav, err = fetch_ibkr_nav()
                if err:
                    st.session_state.ibkr_error = err
                    st.error(err)
                else:
                    st.session_state.cached_nav    = fetched_nav
                    st.session_state.nav_timestamp = datetime.now()
                    st.session_state.ibkr_error    = None
                    st.session_state.nav_override   = None
                    st.success(f"✅ NAV עודכן מ-IBKR: ${fetched_nav:,.2f}")
                    st.rerun()

            if st.session_state.ibkr_error:
                st.warning(st.session_state.ibkr_error)

        with ctrl_right:
            st.markdown("**מצב נוכחי**")
            nav_status = f"${nav:,.2f}" if nav else "—"
            st.markdown(f"""
            <div class="kpi-card" style="padding:16px;">
                <div class="kpi-label">NAV פעיל</div>
                <div class="kpi-value {'green' if nav else 'muted'}" style="font-size:1.3rem;">{nav_status}</div>
                <div class="kpi-sub">{nav_source}</div>
            </div>""", unsafe_allow_html=True)

    st.markdown("---")

    if nav is None:
        st.info("ℹ️ יש לטעון NAV מ-IBKR או להזין עדכון ידני בפאנל מעלה.")
        return

    # ── Load users ────────────────────────────────────────────────────────────
    try:
        users = load_users()
    except Exception as exc:
        st.error(f"שגיאה בטעינת נתוני משתמשים: {exc}")
        return

    # ── Summary KPIs ──────────────────────────────────────────────────────────
    calcs = [calculate(nav, r["share_pct"], r["initial_capital"], r["is_manager"])
             for _, r in users.iterrows()]

    total_initial = users["initial_capital"].sum()
    total_net     = sum(c["net"] for c in calcs)
    total_pnl     = total_net - total_initial
    total_tax     = sum(c["tax"] for c in calcs)
    overall_roi   = (total_pnl / total_initial * 100) if total_initial else 0

    pnl_color = "green" if total_pnl >= 0 else "red"
    pnl_sign  = "+" if total_pnl >= 0 else ""

    c1, c2, c3, c4 = st.columns(4)
    with c1: st.markdown(kpi("NAV מאסטר", f"${nav:,.2f}", nav_source), unsafe_allow_html=True)
    with c2: st.markdown(kpi("סה״כ הון מושקע", f"${total_initial:,.0f}"), unsafe_allow_html=True)
    with c3: st.markdown(kpi("סה״כ שווי נטו", f"${total_net:,.2f}"), unsafe_allow_html=True)
    with c4: st.markdown(kpi("רווח/הפסד כולל",
                              f"{pnl_sign}${abs(total_pnl):,.2f}",
                              f"{pnl_sign}{overall_roi:.2f}% | מס: ${total_tax:,.2f}",
                              pnl_color), unsafe_allow_html=True)

    st.markdown("---")

    # ── Users table ───────────────────────────────────────────────────────────
    st.markdown("### 📊 סקירת כל המשתמשים")

    rows = []
    for (_, u), c in zip(users.iterrows(), calcs):
        pnl_disp = f"${c['pnl']:+,.2f}"
        rows.append({
            "שם":             u["name"],
            "חלק %":          f"{u['share_pct']:.2f}%",
            "הון ראשוני $":   f"${u['initial_capital']:,.0f}",
            "ברוטו $":        f"${c['gross']:,.2f}",
            "רווח/הפסד $":    pnl_disp,
            "מס (25%) $":     f"${c['tax']:,.2f}" if c['tax'] > 0 else "—",
            "שווי נטו $":     f"${c['net']:,.2f}",
            "תשואה %":        f"{c['roi']:+.2f}%",
            "סוג":            "מנהל קרן" if u["is_manager"] else "משקיע",
        })

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ── Bar chart – net value per user ────────────────────────────────────────
    names    = [r["שם"] for r in rows]
    nets     = [c["net"] for c in calcs]
    initials = [u["initial_capital"] for _, u in users.iterrows()]
    colours  = ["#3ddc97" if n >= i else "#ff5c5c" for n, i in zip(nets, initials)]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="הון ראשוני",
        x=names, y=initials,
        marker_color="#1d3557",
        text=[f"${v:,.0f}" for v in initials],
        textposition="inside",
    ))
    fig.add_trace(go.Bar(
        name="שווי נטו",
        x=names, y=nets,
        marker_color=colours,
        text=[f"${v:,.0f}" for v in nets],
        textposition="outside",
    ))
    fig.update_layout(
        barmode="overlay",
        paper_bgcolor="#060d18",
        plot_bgcolor="#0f1f38",
        font=dict(color="#e6edf3", family="Heebo"),
        legend=dict(bgcolor="#0f1f38", bordercolor="#1d3557"),
        margin=dict(t=20, b=20, l=10, r=10),
        height=320,
        xaxis=dict(gridcolor="#1d3557"),
        yaxis=dict(gridcolor="#1d3557", tickprefix="$"),
    )
    st.plotly_chart(fig, use_container_width=True)


# ─── USER DASHBOARD ──────────────────────────────────────────────────────────
def show_user():
    u               = st.session_state.user_row
    name            = u.get("name", "משתמש")
    share_pct       = float(u.get("share_pct", 0))
    initial_capital = float(u.get("initial_capital", 0))
    is_manager      = bool(u.get("is_manager", False))

    # Header
    col_h, col_logout = st.columns([5, 1])
    with col_h:
        st.markdown(f"""
        <h2 style="font-size:1.7rem; font-weight:700; margin-bottom:4px;">שלום, {name} 👋</h2>
        <p class="muted" style="margin:0;">RC Capital | לוח בקרה אישי</p>""",
        unsafe_allow_html=True)
    with col_logout:
        if st.button("התנתק"):
            st.session_state.clear()
            st.rerun()

    st.markdown("---")

    nav, nav_source = get_effective_nav()

    if nav is None:
        st.info("🔄 הנתונים יהיו זמינים בקרוב. אנא נסה שוב מאוחר יותר.")
        return

    c = calculate(nav, share_pct, initial_capital, is_manager)
    net      = c["net"]
    net_pnl  = c["net_pnl"]
    roi      = c["roi"]
    gross    = c["gross"]
    tax      = c["tax"]

    net_color = "green" if net_pnl >= 0 else "red"
    pnl_sign  = "+" if net_pnl >= 0 else ""

    # Hero KPI
    st.markdown(f"""
    <div class="kpi-card" style="max-width:480px; margin:0 auto 28px auto; padding:36px 28px;">
        <div class="kpi-label" style="font-size:0.85rem;">שווי נטו שלך</div>
        <div class="kpi-value {net_color}" style="font-size:3rem;">${net:,.2f}</div>
        <div class="kpi-sub {net_color}" style="font-size:0.85rem;">
            {pnl_sign}${abs(net_pnl):,.2f} &nbsp;|&nbsp; {pnl_sign}{roi:.2f}%
        </div>
        <div class="kpi-sub" style="margin-top:6px;">{nav_source}</div>
    </div>""", unsafe_allow_html=True)

    # Secondary KPIs
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(kpi("הון ראשוני", f"${initial_capital:,.0f}"), unsafe_allow_html=True)
    with c2:
        st.markdown(kpi("פוזיציה ברוטו", f"${gross:,.2f}", f"חלק: {share_pct:.2f}%"), unsafe_allow_html=True)
    with c3:
        if is_manager:
            tax_val, tax_col, tax_sub = "לא חל", "muted", "מנהל קרן"
        elif tax > 0:
            tax_val, tax_col, tax_sub = f"${tax:,.2f}", "gold", "25% על רווח"
        else:
            tax_val, tax_col, tax_sub = "אין", "green", "אין רווח חייב"
        st.markdown(kpi("מס רווח הון", tax_val, tax_sub, tax_col), unsafe_allow_html=True)

    st.markdown("---")

    # Waterfall breakdown chart
    st.markdown("### 📈 פירוט חישוב השווי")

    measures = ["absolute", "relative", "relative", "total"]
    x_labels = ["הון ראשוני", "רווח ברוטו", "מס", "שווי נטו"]
    y_vals   = [initial_capital, c["pnl"], -tax, None]

    fig = go.Figure(go.Waterfall(
        orientation="v",
        measure=measures,
        x=x_labels,
        y=y_vals,
        connector={"line": {"color": "#1d3557", "width": 1}},
        increasing={"marker": {"color": "#3ddc97"}},
        decreasing={"marker": {"color": "#ff5c5c"}},
        totals={"marker": {"color": "#1d6fa4"}},
        texttemplate="$%{y:,.0f}",
        textposition="outside",
        textfont={"color": "#e6edf3", "size": 13},
    ))
    fig.update_layout(
        paper_bgcolor="#060d18",
        plot_bgcolor="#0f1f38",
        font=dict(color="#e6edf3", family="Heebo"),
        showlegend=False,
        margin=dict(t=30, b=10, l=10, r=10),
        height=340,
        xaxis=dict(gridcolor="#1d3557"),
        yaxis=dict(gridcolor="#1d3557", tickprefix="$"),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Disclosure
    if is_manager:
        note = "כמנהל הקרן, חישוב המס אינו חל עליך."
    elif tax > 0:
        note = f"הערה: נוכה מס רווח הון ישראלי בשיעור 25% על הרווח (${c['pnl']:,.2f}) = ${tax:,.2f}."
    else:
        note = "אין רווח חייב במס — שווי ברוטו שווה לשווי נטו."

    st.markdown(f"<p class='muted' style='font-size:0.78rem; text-align:center;'>{note}</p>",
                unsafe_allow_html=True)


# ─── ROUTER ──────────────────────────────────────────────────────────────────
if not st.session_state.authenticated:
    show_login()
elif st.session_state.is_admin:
    show_admin()
else:
    show_user()
