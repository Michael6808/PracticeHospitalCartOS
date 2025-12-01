# StreamLitDE.py — Full app with background delivery listener & back buttons above inputs
import streamlit as st
import pandas as pd
import serial
import time
import io
from datetime import datetime, timedelta
from streamlit_autorefresh import st_autorefresh
from openpyxl import Workbook

# ---------------- CONFIG ----------------
PICO_SERIAL_PORT = "COM9"
PICO_BAUDRATE = 115200
NUM_SERVOS = 5
AUTO_RELOCK_SECONDS = 20
OUT_WARNING_MINUTES = 5
REFRESH_INTERVAL_SEC = 3    # how often page auto-refreshes (seconds)
AUTHORIZED_CODES = ["1111", "2222"]
MAIN_PASSCODE = "1234"
DELIVERY_PREFIX = "C"       # Delivery scanner prefix

# ---------------- AUTOREFRESH ----------------
st_autorefresh(interval=REFRESH_INTERVAL_SEC * 1000, key="global_refresh")

# ---------------- SESSION STATE SETUP ----------------
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if "df" not in st.session_state:
    st.session_state.df = pd.DataFrame(columns=[
        "Drug", "Amount", "Barcode", "Actively Out", "Wasted",
        "Delivered", "Needs Waste", "Cabinet", "Section", "Last Dispensed Time", "Assigned Patient"
    ])

if "patients" not in st.session_state:
    st.session_state.patients = {
        "PATIENT123": {"Name": "John Doe", "Drugs": ["Morphine", "Aspirin"]},
        "PATIENT456": {"Name": "Jane Smith", "Drugs": ["Ibuprofen"]},
    }

# UI state
if "menu" not in st.session_state:
    st.session_state.menu = None
if "menu_stack" not in st.session_state:
    st.session_state.menu_stack = []

# delivery state
if "awaiting_drug_scan" not in st.session_state:
    st.session_state.awaiting_drug_scan = False
if "current_patient" not in st.session_state:
    st.session_state.current_patient = None

# pico / cabinets
if "pico" not in st.session_state:
    st.session_state.pico = None
if "pico_connected" not in st.session_state:
    st.session_state.pico_connected = False
if "cabinet_locked" not in st.session_state:
    st.session_state.cabinet_locked = {i + 1: True for i in range(NUM_SERVOS)}
if "unlock_expiries" not in st.session_state:
    st.session_state.unlock_expiries = {}

# last dispensed times (for alerts)
if "last_dispensed" not in st.session_state:
    st.session_state.last_dispensed = {}  # drug -> datetime


# ---------------- HELPERS ----------------
def show_dataframe(df):
    try:
        st.dataframe(df, width="stretch")
    except TypeError:
        st.dataframe(df, use_container_width=True)


def enter_menu(name):
    st.session_state.menu_stack.append(st.session_state.menu)
    st.session_state.menu = name
    st.rerun()


def go_back():
    if st.session_state.menu_stack:
        st.session_state.menu = st.session_state.menu_stack.pop()
    else:
        st.session_state.menu = None
    st.rerun()


def reset_main():
    st.session_state.menu = None
    st.session_state.menu_stack = []
    st.rerun()


# ---------------- PICO / SERIAL ----------------
def get_pico():
    pico = st.session_state.pico
    try:
        if pico is not None and pico.is_open:
            return pico
    except Exception:
        st.session_state.pico = None
        st.session_state.pico_connected = False

    try:
        pico = serial.Serial(PICO_SERIAL_PORT, PICO_BAUDRATE, timeout=1)
        time.sleep(0.1)
        try:
            pico.write(b"CONNECT\n")
        except Exception:
            pass
        st.session_state.pico = pico
        st.session_state.pico_connected = True
        return pico
    except Exception:
        st.session_state.pico = None
        st.session_state.pico_connected = False
        return None


def send_servo_command(cmd: str) -> bool:
    pico = get_pico()
    if pico is None:
        return False
    try:
        pico.write((cmd + "\n").encode())
        return True
    except Exception:
        st.session_state.pico = None
        st.session_state.pico_connected = False
        return False


def unlock_cabinet(cabinet_num: int):
    if not (1 <= cabinet_num <= NUM_SERVOS):
        return False
    ok = send_servo_command(f"UNLOCK{cabinet_num}")
    if ok:
        st.session_state.cabinet_locked[cabinet_num] = False
        st.session_state.unlock_expiries[cabinet_num] = datetime.now() + timedelta(seconds=AUTO_RELOCK_SECONDS)
    return ok


def lock_cabinet(cabinet_num: int):
    if not (1 <= cabinet_num <= NUM_SERVOS):
        return False
    ok = send_servo_command(f"LOCK{cabinet_num}")
    if ok:
        st.session_state.cabinet_locked[cabinet_num] = True
        st.session_state.unlock_expiries.pop(cabinet_num, None)
    return ok


def unlock_all_cabinets():
    any_ok = False
    for i in range(1, NUM_SERVOS + 1):
        ok = send_servo_command(f"UNLOCK{i}")
        if ok:
            any_ok = True
            st.session_state.cabinet_locked[i] = False
            st.session_state.unlock_expiries[i] = datetime.now() + timedelta(seconds=AUTO_RELOCK_SECONDS)
    return any_ok


# ---------------- ALERTS & AUTO-RELOCK ----------------
def show_out_alerts_in_app():
    now = datetime.now()
    messages = []
    for drug, ts in st.session_state.last_dispensed.items():
        if ts and now - ts > timedelta(minutes=OUT_WARNING_MINUTES):
            minutes = int((now - ts).total_seconds() // 60)
            messages.append(f"🚨 **{drug}** — out for {minutes} minutes")
    if messages:
        st.markdown(
            "<div style='background:#fff3cd;padding:12px;border-radius:8px;'>"
            "<h4 style='color:#8a3b00;'>Outstanding: Drugs out > 5 minutes</h4>"
            + "<br>".join(messages) +
            "</div>",
            unsafe_allow_html=True,
        )


def check_and_relock_expired():
    now = datetime.now()
    expired = [cab for cab, exp in st.session_state.unlock_expiries.items() if exp is not None and now >= exp]
    for cab in expired:
        if not st.session_state.cabinet_locked.get(cab, True):
            ok = send_servo_command(f"LOCK{cab}")
            if ok:
                st.session_state.cabinet_locked[cab] = True
        st.session_state.unlock_expiries.pop(cab, None)


# ---------------- CSV / TEMPLATE HELPERS ----------------
def make_inventory_template_bytes():
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory_Template"
    ws.append(["Drug", "Barcode", "Needs_Waste", "Cabinet", "Section", "Amount"])
    ws.append(["Morphine", "M123", True, 1, 1, 10])
    ws.append(["Ativan", "A456", False, 2, 1, 20])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio


def make_patient_template_bytes():
    wb = Workbook()
    ws = wb.active
    ws.title = "Patients_Template"
    ws.append(["Patient", "Drug"])
    ws.append(["PAT001", "Morphine"])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio


def process_inventory_file(uploaded):
    try:
        if uploaded.name.lower().endswith(".xlsx"):
            new_df = pd.read_excel(uploaded)
        else:
            new_df = pd.read_csv(uploaded)
    except Exception as e:
        st.error(f"Could not read file: {e}")
        return

    required = {"Drug", "Barcode", "Needs_Waste", "Cabinet", "Amount"}
    if not required.issubset(set(new_df.columns)):
        st.error(f"Inventory file must contain columns: {', '.join(sorted(required))}")
        return

    df = st.session_state.df.copy()
    for _, r in new_df.iterrows():
        drug = str(r["Drug"])
        barcode = str(r["Barcode"])
        needs_waste = bool(r["Needs_Waste"])
        cabinet = int(r["Cabinet"]) if pd.notnull(r["Cabinet"]) else 1
        section = int(r.get("Section", 1)) if pd.notnull(r.get("Section", 1)) else 1
        amount = int(r["Amount"]) if pd.notnull(r["Amount"]) else 0

        if barcode in df["Barcode"].values:
            idx = df.index[df["Barcode"] == barcode][0]
            df.loc[idx, "Amount"] = int(df.loc[idx, "Amount"]) + amount
            df.loc[idx, "Needs Waste"] = needs_waste
            df.loc[idx, "Cabinet"] = cabinet
            df.loc[idx, "Section"] = section
        elif drug in df["Drug"].values:
            idx = df.index[df["Drug"] == drug][0]
            df.loc[idx, "Amount"] = int(df.loc[idx, "Amount"]) + amount
            df.loc[idx, "Barcode"] = barcode or df.loc[idx, "Barcode"]
            df.loc[idx, "Needs Waste"] = needs_waste
            df.loc[idx, "Cabinet"] = cabinet
            df.loc[idx, "Section"] = section
        else:
            new_row = {
                "Drug": drug, "Amount": int(amount), "Barcode": barcode,
                "Actively Out": 0, "Wasted": 0, "Delivered": 0,
                "Needs Waste": needs_waste, "Cabinet": cabinet, "Section": section,
                "Last Dispensed Time": None, "Assigned Patient": None
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

    st.session_state.df = df
    st.success("Inventory merged — unlocking all cabinets for restock.")
    unlock_all_cabinets()


def process_patients_file(uploaded):
    try:
        if uploaded.name.lower().endswith(".xlsx"):
            p_df = pd.read_excel(uploaded)
        else:
            p_df = pd.read_csv(uploaded)
    except Exception as e:
        st.error(f"Could not read patients file: {e}")
        return

    required = {"Patient", "Drug"}
    if not required.issubset(set(p_df.columns)):
        st.error("Patient file must contain columns: Patient, Drug")
        return

    for _, r in p_df.iterrows():
        pid = str(r["Patient"])
        drug = str(r["Drug"])
        if pid in st.session_state.patients:
            existing = set(st.session_state.patients[pid]["Drugs"])
            existing.add(drug)
            st.session_state.patients[pid]["Drugs"] = list(existing)
        else:
            st.session_state.patients[pid] = {"Name": pid, "Drugs": [drug]}
    st.success("Patient dictionary updated.")


# ---------------- BARCODE HANDLERS ----------------
def handle_cart_scan(scan: str, context: str = None):
    if not scan:
        return
    s = scan.strip()
    df = st.session_state.df

    # add_existing context
    if context == "add_existing":
        if s in df["Barcode"].values:
            idx = df.index[df["Barcode"] == s][0]
            df.loc[idx, "Amount"] = int(df.loc[idx, "Amount"]) + 1
            st.session_state.df = df
            try:
                unlock_cabinet(int(df.loc[idx, "Cabinet"]))
            except Exception:
                pass
            reset_main()
        else:
            st.error("Barcode not found; consider Add New.")
        return

    # dispense context
    if context == "dispense":
        if s in df["Barcode"].values:
            idx = df.index[df["Barcode"] == s][0]
            if int(df.loc[idx, "Amount"]) > 0:
                df.loc[idx, "Amount"] = int(df.loc[idx, "Amount"]) - 1
                df.loc[idx, "Actively Out"] = int(df.loc[idx, "Actively Out"]) + 1
                df.loc[idx, "Last Dispensed Time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                st.session_state.df = df
                st.session_state.last_dispensed[df.loc[idx, "Drug"]] = datetime.now()
                try:
                    unlock_cabinet(int(df.loc[idx, "Cabinet"]))
                except Exception:
                    pass
                reset_main()
            else:
                st.warning("No stock available to dispense.")
        else:
            st.error("Barcode not found.")
        return

    # return context
    if context == "return":
        if s in df["Barcode"].values:
            idx = df.index[df["Barcode"] == s][0]
            if int(df.loc[idx, "Actively Out"]) > 0:
                df.loc[idx, "Actively Out"] = int(df.loc[idx, "Actively Out"]) - 1
                df.loc[idx, "Amount"] = int(df.loc[idx, "Amount"]) + 1
                df.loc[idx, "Last Dispensed Time"] = None
                st.session_state.df = df
                try:
                    lock_cabinet(int(df.loc[idx, "Cabinet"]))
                except Exception:
                    pass
                reset_main()
            else:
                st.warning("No actively out units to return.")
        else:
            st.error("Barcode not found.")
        return

    # waste context
    if context == "waste":
        c1 = st.session_state.get("waste_code1", "")
        c2 = st.session_state.get("waste_code2", "")
        if c1 not in AUTHORIZED_CODES or c2 not in AUTHORIZED_CODES or c1 == c2:
            st.error("Invalid or duplicate authorization codes.")
            return
        if s in df["Barcode"].values:
            idx = df.index[df["Barcode"] == s][0]
            if int(df.loc[idx, "Actively Out"]) > 0 and df.loc[idx, "Needs Waste"]:
                df.loc[idx, "Actively Out"] = int(df.loc[idx, "Actively Out"]) - 1
                df.loc[idx, "Wasted"] = int(df.loc[idx, "Wasted"]) + 1
                if int(df.loc[idx, "Actively Out"]) == 0:
                    df.loc[idx, "Last Dispensed Time"] = None
                st.session_state.df = df
                reset_main()
            else:
                st.info("Nothing to waste or waste not required.")
        else:
            st.error("Barcode not found.")
        return

    # generic quick dispense
    if s in df["Barcode"].values:
        idx = df.index[df["Barcode"] == s][0]
        if int(df.loc[idx, "Amount"]) > 0:
            df.loc[idx, "Amount"] = int(df.loc[idx, "Amount"]) - 1
            df.loc[idx, "Actively Out"] = int(df.loc[idx, "Actively Out"]) + 1
            df.loc[idx, "Last Dispensed Time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st.session_state.df = df
            st.session_state.last_dispensed[df.loc[idx, "Drug"]] = datetime.now()
            try:
                unlock_cabinet(int(df.loc[idx, "Cabinet"]))
            except Exception:
                pass
        else:
            st.warning("No stock to dispense.")
    else:
        st.error("Barcode not found in inventory.")


def handle_delivery_scan(raw_scan: str):
    """Background delivery: patient scan (C<id>) then drug scan (C<barcode>). Silent on success."""
    if not raw_scan:
        return
    s = raw_scan.strip()
    if s.upper().startswith(DELIVERY_PREFIX):
        code = s[len(DELIVERY_PREFIX):]
    else:
        code = s

    df = st.session_state.df

    # If another panel is active, ignore (we only listen on main)
    if st.session_state.menu is not None:
        return

    # If not awaiting drug, treat as patient id
    if not st.session_state.awaiting_drug_scan:
        if code in st.session_state.patients:
            st.session_state.current_patient = code
            st.session_state.awaiting_drug_scan = True
            # silent wait — do not show success UI
            return
        else:
            st.warning(f"Delivery scan: unknown patient ID '{code}'")
            return
    else:
        # expecting drug scan
        patient_id = st.session_state.current_patient
        if code in df["Barcode"].values:
            idx = df.index[df["Barcode"] == code][0]
            drug_name = df.loc[idx, "Drug"]
            if drug_name in st.session_state.patients.get(patient_id, {}).get("Drugs", []):
                # mark delivered
                if int(df.loc[idx, "Actively Out"]) > 0:
                    df.loc[idx, "Actively Out"] = int(df.loc[idx, "Actively Out"]) - 1
                df.loc[idx, "Delivered"] = int(df.loc[idx, "Delivered"] if pd.notna(df.loc[idx, "Delivered"]) else 0) + 1
                df.loc[idx, "Last Dispensed Time"] = None
                df.loc[idx, "Assigned Patient"] = patient_id
                st.session_state.df = df
                # success silent
            else:
                st.warning(f"Delivery warning: {drug_name} not prescribed for {patient_id}")
        else:
            st.warning("Delivery scan: drug barcode not found.")
        # reset delivery state
        st.session_state.awaiting_drug_scan = False
        st.session_state.current_patient = None


# ---------------- UI LAYOUT ----------------
st.set_page_config(page_title="Hospital Cart OS", layout="wide")
st.title("🏥 Hospital Cart OS — Background Delivery & Back Buttons")

# top controls: Pico connection
cols = st.columns([8, 1, 1])
with cols[1]:
    if st.session_state.pico_connected:
        if st.button("Disconnect Pico"):
            try:
                if st.session_state.pico and st.session_state.pico.is_open:
                    st.session_state.pico.close()
            except Exception:
                pass
            st.session_state.pico = None
            st.session_state.pico_connected = False
            st.success("Pico disconnected.")
    else:
        if st.button("Connect Pico"):
            pico = get_pico()
            if st.session_state.pico_connected:
                st.success("Pico connected.")
            else:
                st.error("Could not connect to Pico. Check COM port and firmware.")

with cols[2]:
    st.write(" ")  # spacer

st.write("Delivery scanner runs in the background on the main screen. Scan patient (prefix 'C') then drug. Only warnings/errors show if something is wrong.")

# Authentication
if not st.session_state.authenticated:
    pw = st.text_input("Enter passcode", type="password")
    if st.button("Unlock"):
        if pw == MAIN_PASSCODE:
            st.session_state.authenticated = True
            st.success("Access granted")
            reset_main()
        else:
            st.error("Incorrect passcode")
    st.stop()

# periodic checks
check_and_relock_expired()
show_out_alerts_in_app()

# patient list visible
with st.expander("🧾 Patient List (always visible)", expanded=True):
    rows = []
    for pid, info in st.session_state.patients.items():
        rows.append({"Patient ID": pid, "Name": info.get("Name", pid), "Drugs": ", ".join(info.get("Drugs", []))})
    show_dataframe(pd.DataFrame(rows))

# CSV / Template in expander
with st.expander("📁 Upload / Download Templates"):
    c1, c2 = st.columns(2)
    with c1:
        inv_upload = st.file_uploader("Upload Inventory (CSV or XLSX)", type=["csv", "xlsx"], key="inv_upload")
        if inv_upload is not None:
            process_inventory_file(inv_upload)
    with c2:
        pat_upload = st.file_uploader("Upload Patients (CSV or XLSX)", type=["csv", "xlsx"], key="pat_upload")
        if pat_upload is not None:
            process_patients_file(pat_upload)

    t1, t2 = st.columns(2)
    with t1:
        if st.button("Download Inventory Excel Template"):
            b = make_inventory_template_bytes()
            st.download_button("Download Inventory_Template.xlsx", data=b, file_name="Inventory_Template.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with t2:
        if st.button("Download Patients Excel Template"):
            b2 = make_patient_template_bytes()
            st.download_button("Download Patients_Template.xlsx", data=b2, file_name="Patients_Template.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.divider()

# Main menu
if st.session_state.menu is None:
    c1, c2, c3, c4 = st.columns([1,1,1,1])
    with c1:
        if st.button("➕ Add Items"):
            enter_menu("add_menu")
    with c2:
        if st.button("📦 Dispense"):
            enter_menu("dispense")
    with c3:
        if st.button("↩️ Return"):
            enter_menu("return")
    with c4:
        if st.button("🗑️ Waste"):
            enter_menu("waste")

# ---------- Panels (Back button above first field) ----------

# Add menu selection
if st.session_state.menu == "add_menu":
    st.subheader("➕ Add Items")
    if st.button("Back"):
        go_back()
    a1, a2 = st.columns(2)
    with a1:
        if st.button("🆕 Add New Drug"):
            enter_menu("add_new")
    with a2:
        if st.button("🔁 Add Existing (scan)"):
            enter_menu("add_existing")

# Add New
if st.session_state.menu == "add_new":
    st.subheader("Add New Drug")
    # Back button above inputs
    if st.button("Back"):
        go_back()
    with st.form("form_add_new", clear_on_submit=True):
        drug = st.text_input("Drug Name")
        barcode = st.text_input("Barcode (scan or type)")
        amount = st.number_input("Amount", min_value=1, value=1)
        needs_waste = st.checkbox("Needs Waste after use?")
        cabinet = st.number_input(f"Cabinet (1–{NUM_SERVOS})", min_value=1, max_value=NUM_SERVOS, value=1)
        section = st.number_input("Section", min_value=1, value=1)
        submitted = st.form_submit_button("Submit")
        if submitted:
            df = st.session_state.df.copy()
            if barcode in df["Barcode"].values:
                idx = df.index[df["Barcode"] == barcode][0]
                df.loc[idx, "Amount"] = int(df.loc[idx, "Amount"]) + int(amount)
                st.session_state.df = df
                try:
                    unlock_cabinet(int(df.loc[idx, "Cabinet"]))
                except Exception:
                    pass
                reset_main()
            else:
                new_row = {
                    "Drug": drug, "Amount": int(amount), "Barcode": barcode,
                    "Actively Out": 0, "Wasted": 0, "Delivered": 0,
                    "Needs Waste": bool(needs_waste), "Cabinet": int(cabinet), "Section": int(section),
                    "Last Dispensed Time": None, "Assigned Patient": None
                }
                st.session_state.df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                try:
                    unlock_cabinet(int(cabinet))
                except Exception:
                    pass
                reset_main()

# Add Existing
if st.session_state.menu == "add_existing":
    st.subheader("Add to Existing Drug (scan)")
    if st.button("Back"):
        go_back()
    st.write("Scan existing barcode with cart scanner to add 1 unit.")
    scan_add = st.text_input("Scan barcode to add (cart scanner)", key="scan_add")
    if scan_add:
        handle_cart_scan(scan_add.strip(), context="add_existing")
        st.session_state.scan_add = ""

# Dispense
if st.session_state.menu == "dispense":
    st.subheader("Dispense")
    if st.button("Back"):
        go_back()
    disp_drug = st.text_input("Drug name (optional for validation)", key="dispense_drug_name")
    disp_patient = st.text_input("Patient ID (optional)", key="dispense_patient_id")
    st.write("Now scan the drug barcode with the cart scanner.")
    scan_disp = st.text_input("Scan drug barcode (cart scanner)", key="scan_disp")
    if scan_disp:
        handle_cart_scan(scan_disp.strip(), context="dispense")
        st.session_state.scan_disp = ""

# Return
if st.session_state.menu == "return":
    st.subheader("Return to Stock")
    if st.button("Back"):
        go_back()
    st.write("Scan barcode (cart scanner) to return an actively-out item to stock.")
    scan_ret = st.text_input("Scan barcode to return", key="scan_return")
    if scan_ret:
        handle_cart_scan(scan_ret.strip(), context="return")
        st.session_state.scan_return = ""

# Waste
if st.session_state.menu == "waste":
    st.subheader("Waste (requires 2 authorization codes)")
    if st.button("Back"):
        go_back()
    c1 = st.text_input("First authorization code", type="password", key="waste_code1")
    c2 = st.text_input("Second authorization code", type="password", key="waste_code2")
    st.write("Scan barcode (cart scanner) to waste 1 actively-out unit.")
    scan_w = st.text_input("Scan to waste", key="scan_waste")
    # store codes in session for handler
    st.session_state.waste_code1 = c1
    st.session_state.waste_code2 = c2
    if scan_w:
        handle_cart_scan(scan_w.strip(), context="waste")
        st.session_state.scan_waste = ""

# Background delivery listener: active only when no menu is open.
# A hidden input will accept scans from the delivery scanner when on main screen.
if st.session_state.menu is None:
    # This input is intentionally label-hidden so it doesn't clutter UI.
    delivery_scan = st.text_input("", key="delivery_hidden_input", label_visibility="collapsed",
                                  placeholder="(delivery scanner — scan patient then drug)")
    if delivery_scan:
        # process and then clear
        handle_delivery_scan(delivery_scan.strip())
        st.session_state.delivery_hidden_input = ""

# Manual cabinet controls
st.markdown("---")
st.subheader("Cabinet Manual Control")
if not st.session_state.pico_connected:
    st.warning("Pico not connected — cabinets cannot be controlled from here.")
cols = st.columns(NUM_SERVOS)
for i in range(NUM_SERVOS):
    cab = i + 1
    with cols[i]:
        locked = st.session_state.cabinet_locked.get(cab, True)
        st.write(f"Cabinet {cab}")
        st.write(f"Status: {'Locked' if locked else 'Unlocked'}")
        if st.button(f"Unlock {cab}", key=f"manual_unlock_{cab}"):
            unlock_cabinet(cab)
        if st.button(f"Lock {cab}", key=f"manual_lock_{cab}"):
            lock_cabinet(cab)

# Inventory display & final checks
st.markdown("---")
st.subheader("Inventory")
if st.session_state.df.empty:
    st.info("No inventory. Add items or upload template.")
else:
    show_dataframe(st.session_state.df)

check_and_relock_expired()
show_out_alerts_in_app()
