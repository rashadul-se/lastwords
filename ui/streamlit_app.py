"""Presentation layer only. No SQL, no business rules."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from app.config import Settings
from app.container import build_service
from app.domain.models import VitalEvent, MedicationEvent

st.set_page_config(page_title="Elder Care Centre", page_icon="🏨", layout="wide")
CFG = Settings()
TZ = ZoneInfo(CFG.timezone)


@st.cache_resource(show_spinner="Connecting...")
def svc():
    return build_service()          # one shared service/pool for all sessions


def when_input(label: str, key: str) -> datetime:
    now = datetime.now(TZ)
    c1, c2 = st.columns(2)
    d = c1.date_input(f"{label} date", now.date(), key=f"{key}_d")
    t = c2.time_input(f"{label} time", now.time().replace(second=0, microsecond=0), key=f"{key}_t")
    return datetime.combine(d, t, tzinfo=TZ)


def guarded(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ValueError as e:
        st.error(str(e))
    except Exception as e:
        st.error(f"Could not complete (database unreachable?): {e}")


care = svc()
st.sidebar.title("🏨 Care Concierge")
page = st.sidebar.radio("Go to", ["Dashboard", "Residents", "Admissions", "Finance", "Record care", "Sync"])
offline = st.sidebar.toggle("Work offline", value=False)
s = care.status()
st.sidebar.caption(f"Postgres: {'🟢' if s['postgres'] else '🔴'}  Redis: {'🟢' if s['redis'] else '🔴'}  "
                   f"Pending: {s['pending_sync']}")
cur = CFG.currency

# ---------------------------------------------------------------- Dashboard
if page == "Dashboard":
    d = care.dashboard()
    cols = st.columns(6)
    for col, (lab, val) in zip(cols, [("Residents", d["total"]), ("In house", d["in_house"]),
                                      ("Red alerts", d["red"]), ("Amber", d["amber"]),
                                      (f"Income MTD ({cur})", f"{d['income']:,.0f}"),
                                      (f"Expense MTD ({cur})", f"{d['expense']:,.0f}")]):
        col.metric(lab, val)
    st.subheader("Nurse priority list")
    st.dataframe(pd.DataFrame(d["top"]), use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- Residents
elif page == "Residents":
    q = st.text_input("Search name or room")
    pg = st.number_input("Page", 0, 999, 0)
    st.dataframe(care.residents(q, int(pg)), use_container_width=True, hide_index=True)
    rid = st.number_input("Open resident id", 1, 10**6, 1)
    if st.button("View profile"):
        det = guarded(care.resident_detail, int(rid))
        if det:
            p = det["profile"]
            a, b, c, e = st.columns(4)
            a.metric("Status", p["status"]); b.metric("Points", p["points"]); c.metric("Tier", det["tier"])
            e.metric("Room", p["room"])
            st.caption(f"Check-in: {p['checkin_at']}   |   Check-out: {p['checkout_at']}")
            t1, t2, t3, t4 = st.tabs(["Insights", "Stays", "Points", "Medication"])
            with t1:
                for line in det["insights"]:
                    st.info(line)
                if not det["vitals"].empty:
                    st.line_chart(det["vitals"].set_index("ts")[["sleep_hrs", "mood", "temp"]])
            t2.dataframe(det["stays"], use_container_width=True, hide_index=True)
            t3.dataframe(det["points"], use_container_width=True, hide_index=True)
            t4.dataframe(det["meds"], use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- Admissions
elif page == "Admissions":
    t1, t2, t3, t4 = st.tabs(["Register resident", "Check-in", "Check-out", "Stay log"])
    with t1:
        with st.form("add"):
            c1, c2, c3 = st.columns(3)
            name = c1.text_input("Full name"); age = c2.number_input("Age", 50, 120, 75)
            room = c3.text_input("Room")
            cond = c1.text_input("Conditions", "None")
            diet = c2.selectbox("Diet", ["Regular", "Diabetic", "Soft", "Low-salt", "Vegetarian"])
            now_in = c3.checkbox("Check in immediately", True)
            when = when_input("Check-in", "reg")
            if st.form_submit_button("Register"):
                rid = guarded(care.add_resident, name, int(age), room, cond, diet, when if now_in else None)
                if rid:
                    st.success(f"Registered resident #{rid}" + (" and checked in (+50 welcome points)" if now_in else ""))
    with t2:
        with st.form("in"):
            rid = st.number_input("Resident id", 1, 10**6, 1, key="in_id")
            when = when_input("Check-in", "in")
            if st.form_submit_button("Check in"):
                r = guarded(care.check_in, int(rid), when, online=not offline)
                if r:
                    st.success(f"{r['status']} · +{r['points']} welcome points")
    with t3:
        with st.form("out"):
            rid = st.number_input("Resident id", 1, 10**6, 1, key="out_id")
            when = when_input("Check-out", "out")
            if st.form_submit_button("Check out"):
                r = guarded(care.check_out, int(rid), when, online=not offline)
                if r:
                    st.success(f"{r['status']} · {r['nights']} night(s) · +{r['points']} points")
    with t4:
        st.dataframe(guarded(care.recent_stays, 200), use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- Finance
elif page == "Finance":
    cats = care.categories()
    t1, t2, t3 = st.tabs(["Record transaction", "Statement & receipts", "Summary"])
    with t1:
        with st.form("tx"):
            c1, c2, c3 = st.columns(3)
            kind = c1.selectbox("Type", ["INCOME", "EXPENSE"])
            rid = c2.number_input("Resident id (0 = centre-wide)", 0, 10**6, 0)
            amt = c3.number_input(f"Amount ({cur})", 0.0, 10_000_000.0, 0.0, step=10.0)
            cat = c1.selectbox("Category", cats["INCOME"] + cats["EXPENSE"])
            desc = c2.text_input("Description")
            when = when_input("Transaction", "tx")
            if st.form_submit_button("Save & issue receipt"):
                e = guarded(care.record_transaction, int(rid) or None, kind, cat, desc, amt, when,
                            online=not offline)
                if e:
                    st.success(f"{e['status']} · receipt {e['receipt_no']}" +
                               (f" · +{e['points']} points" if e["points"] else ""))
                    txt = care.receipt_text(e)
                    st.code(txt)
                    st.download_button("Download receipt", txt, file_name=f"{e['receipt_no']}.txt")
    with t2:
        c1, c2, c3 = st.columns(3)
        rid = c1.number_input("Resident id (0 = all)", 0, 10**6, 0, key="st_id")
        start = c2.date_input("From", date.today() - timedelta(days=30))
        end = c3.date_input("To", date.today())
        if st.button("Load statement"):
            st.session_state["stmt"] = guarded(care.statement, int(rid) or None,
                                               datetime.combine(start, datetime.min.time(), tzinfo=TZ),
                                               datetime.combine(end, datetime.min.time(), tzinfo=TZ))
        stmt = st.session_state.get("stmt")
        if stmt:
            a, b, c = st.columns(3)
            a.metric("Income", f"{stmt['income']:,.2f}"); b.metric("Expenditure", f"{stmt['expense']:,.2f}")
            c.metric("Net", f"{stmt['net']:,.2f}")
            tbl = stmt["table"]
            st.dataframe(tbl, use_container_width=True, hide_index=True)
            if not tbl.empty:
                pick = st.selectbox("Receipt", tbl["receipt_no"])
                txt = care.receipt_text(tbl[tbl.receipt_no == pick].iloc[0].to_dict())
                st.code(txt)
                st.download_button("Download receipt", txt, file_name=f"{pick}.txt", key="dl_stmt")
    with t3:
        c1, c2 = st.columns(2)
        start = c1.date_input("From", date.today().replace(day=1), key="s_from")
        end = c2.date_input("To", date.today(), key="s_to")
        df = guarded(care.finance_summary, datetime.combine(start, datetime.min.time(), tzinfo=TZ),
                     datetime.combine(end, datetime.min.time(), tzinfo=TZ))
        if df is not None and not df.empty:
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.bar_chart(df.pivot_table(index="category", columns="kind", values="total", fill_value=0))

# ---------------------------------------------------------------- Record care
elif page == "Record care":
    with st.form("vital"):
        rid = st.number_input("Resident id", 1, 10**6, 1)
        c1, c2, c3, c4 = st.columns(4)
        hr = c1.number_input("HR", 30, 220, 75); temp = c2.number_input("Temp C", 30.0, 43.0, 36.7)
        spo2 = c3.number_input("SpO2", 50, 100, 96); mood = c4.slider("Mood", 1, 5, 3)
        sleep = c1.number_input("Sleep h", 0.0, 24.0, 7.0); steps = c2.number_input("Steps", 0, 30000, 2500)
        fl = c3.number_input("Fluids ml", 0, 5000, 1500)
        if st.form_submit_button("Save vitals"):
            r = guarded(care.record_vital, VitalEvent(int(rid), hr, temp, spo2, sleep, steps, mood, fl),
                        online=not offline)
            if r:
                st.success(r)
    with st.form("med"):
        mrid = st.number_input("Resident id ", 1, 10**6, 1)
        drug = st.text_input("Drug", "Metformin 500mg")
        status = st.selectbox("Status", ["GIVEN", "REFUSED", "MISSED"])
        if st.form_submit_button("Log medication"):
            r = guarded(care.log_medication, MedicationEvent(int(mrid), drug, status), online=not offline)
            if r:
                st.success(r)

# ---------------------------------------------------------------- Sync
else:
    st.write(f"Pending offline events: **{s['pending_sync']}**")
    if st.button("Sync now", disabled=offline):
        st.success(f"Synced {care.sync()} events")
