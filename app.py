import sqlite3
import hashlib
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import textwrap
import tempfile
import uuid
import time
import urllib.request
import html
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import streamlit as st
import streamlit.components.v1 as components
from groq import Groq
from pydub import AudioSegment
import gspread  
import threading
import zipfile
import requests



# ============================================================
# GOOGLE DRIVE MODEL AUTO-DOWNLOAD & EXTRACTION
# ============================================================

MODEL_DIR = "model"  # Target directory for your model
ZIP_PATH = "model.zip"
FILE_ID = "1TdPphBAbdnx8uKDDZyeDIP_udGyOhzWC"

def download_file_from_google_drive(file_id, destination):
    URL = "https://docs.google.com/uc?export=download"
    session = requests.Session()
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = session.get(URL, params={'id': file_id}, headers=headers, stream=True)
    
    token = get_confirm_token(response)
    if token:
        params = {'id': file_id, 'confirm': token}
        response = session.get(URL, params=params, headers=headers, stream=True)
        
    save_response_content(response, destination)

def get_confirm_token(response):
    for key, value in response.cookies.items():
        if key.startswith('download_warning'):
            return value
    return None

def save_response_content(response, destination):
    CHUNK_SIZE = 32768
    with open(destination, "wb") as f:
        for chunk in response.iter_content(CHUNK_SIZE):
            if chunk:
                f.write(chunk)

# Check and extract on first boot if missing
if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR, exist_ok=True)
    try:
        with st.spinner("Downloading model from Google Drive..."):
            download_file_from_google_drive(FILE_ID, ZIP_PATH)
        
        with st.spinner("Extracting model files..."):
            with zipfile.ZipFile(ZIP_PATH, 'r') as zip_ref:
                zip_ref.extractall(MODEL_DIR)
                
        if os.path.exists(ZIP_PATH):
            os.remove(ZIP_PATH)
        st.success("Model setup complete!")
    except Exception as e:
        st.error(f"Failed to auto-download/extract model: {str(e)}")


def get_campaign_category(raw_campaign_text):
    """Map real Ringba campaign names to the closest QC campaign family."""
    raw = (raw_campaign_text or "").strip()
    text = raw.lower()

    if not raw:
        return "General Customer Inquiry"

    if any(k in text for k in ["rehab", "addiction", "mental health", "substance", "treatment"]):
        return "Rehab & Addiction Treatment"

    if any(k in text for k in ["dumpster", "porta potty", "portable toilet"]):
        return "Dumpster & Porta Potty Services"

    if any(k in text for k in ["pest", "roofing", "roof", "home service", "plumbing", "hvac", "moving"]):
        return "Pest Control & Home Services"

    if any(k in text for k in ["insurance", "medicare", "medicaid", "auto insurance", "home insurance", "final expense", "health insurance"]):
        return "Insurance (Health / Auto / Home)"

    if any(k in text for k in ["debt", "debt relief", "settlement", "financial", "loan", "mca"]):
        return "Debt Relief & Financial Services"

    return raw

def format_seconds_to_hms(total_seconds_str):
    """Converts raw seconds into a clean H:MM:SS text string."""
    try:
        total_seconds = int(float(str(total_seconds_str).strip()))
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    except Exception:
        return str(total_seconds_str)

# ============================================================
# DATABASE & AUTHENTICATION SETUP (SQLite)
# ============================================================

DB_PATH = Path("users.db")

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            expires_at DATETIME NOT NULL
        )
    """)
    conn.commit()

    c.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in c.fetchall()]
    if "is_admin" not in columns:
        c.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        conn.commit()

    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        admin_pwd = hash_password("admin123")
        c.execute(
            "INSERT INTO users (name, email, password_hash, is_admin) VALUES (?, ?, ?, 1)",
            ("System Admin", "admin@domain.com", admin_pwd)
        )
        conn.commit()

    conn.close()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(name: str, email: str, password: str, is_admin: int = 0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        pwd_hash = hash_password(password)
        c.execute(
            "INSERT INTO users (name, email, password_hash, is_admin) VALUES (?, ?, ?, ?)",
            (name, email.lower().strip(), pwd_hash, is_admin)
        )
        conn.commit()
        return True, "Account created successfully! Please log in."
    except sqlite3.IntegrityError:
        return False, "An account with this email already exists."
    finally:
        conn.close()

def authenticate_user(email: str, password: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    pwd_hash = hash_password(password)
    c.execute(
        "SELECT id, name, email, is_admin FROM users WHERE lower(email) = ? AND password_hash = ?",
        (email.lower().strip(), pwd_hash)
    )
    user = c.fetchone()
    conn.close()
    if user:
        return {"id": user[0], "name": user[1], "email": user[2], "is_admin": bool(user[3])}
    return None

def update_user_profile(user_id: int, new_name: str, new_email: str, new_password: str = ""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        if new_password.strip():
            pwd_hash = hash_password(new_password)
            c.execute("UPDATE users SET name = ?, email = ?, password_hash = ? WHERE id = ?", (new_name, new_email.lower().strip(), pwd_hash, user_id))
        else:
            c.execute("UPDATE users SET name = ?, email = ? WHERE id = ?", (new_name, new_email.lower().strip(), user_id))
        conn.commit()
        return True, "Profile updated successfully!"
    except sqlite3.IntegrityError:
        return False, "Email address is already in use by another account."
    finally:
        conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, email, is_admin FROM users ORDER BY id ASC")
    users = c.fetchall()
    conn.close()
    return [{"id": u[0], "name": u[1], "email": u[2], "is_admin": bool(u[3])} for u in users]

def admin_toggle_role(user_id: int, make_admin: bool):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_admin = ? WHERE id = ?", (1 if make_admin else 0, user_id))
    conn.commit()
    conn.close()

def admin_reset_password(user_id: int, new_password: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    pwd_hash = hash_password(new_password)
    c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pwd_hash, user_id))
    conn.commit()
    conn.close()

def admin_delete_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

def send_reset_code_email(email: str, code: str):
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        return True, f"Demo Mode: Credentials not configured. Your code is: {code}"

    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_EMAIL
        msg['To'] = email
        msg['Subject'] = "Password Reset Code - Aunty Next DOOR"

        body = f"""
        Hello,

        We received a request to reset your password for your Aunty Next DOOR account.

        Your 6-digit verification code is: {code}

        This code will expire in 15 minutes. If you did not request a password reset, please ignore this email.

        Regards,
        Aunty Next DOOR Team
        """
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True, f"Reset code sent to {email}. Please check your inbox."
    except Exception as e:
        return False, f"Failed to send email: {str(e)}"

def generate_reset_code(email: str):
    email_clean = email.lower().strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE lower(email) = ?", (email_clean,))
    user = c.fetchone()
    
    if not user:
        conn.close()
        return False, "No account found with that email address."

    code = f"{random.randint(100000, 999999)}"
    expires_at = datetime.now() + timedelta(minutes=15)

    c.execute("DELETE FROM reset_tokens WHERE lower(email) = ?", (email_clean,))
    c.execute("INSERT INTO reset_tokens (email, code, expires_at) VALUES (?, ?, ?)", (email_clean, code, expires_at))
    conn.commit()
    conn.close()

    return send_reset_code_email(email_clean, code)

def reset_password_with_code(email: str, code: str, new_password: str):
    email_clean = email.lower().strip()
    code_clean = code.strip()
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT expires_at FROM reset_tokens WHERE lower(email) = ? AND code = ?", (email_clean, code_clean))
    record = c.fetchone()

    if not record:
        conn.close()
        return False, "Invalid verification code or email address."

    expires_at = datetime.strptime(record[0], "%Y-%m-%d %H:%M:%S.%f") if "." in record[0] else datetime.strptime(record[0], "%Y-%m-%d %H:%M:%S")
    
    if datetime.now() > expires_at:
        c.execute("DELETE FROM reset_tokens WHERE lower(email) = ?", (email_clean,))
        conn.commit()
        conn.close()
        return False, "Verification code has expired. Please request a new one."

    pwd_hash = hash_password(new_password)
    c.execute("UPDATE users SET password_hash = ? WHERE lower(email) = ?", (pwd_hash, email_clean))
    c.execute("DELETE FROM reset_tokens WHERE lower(email) = ?", (email_clean,))
    conn.commit()
    conn.close()

    return True, "Password reset successfully! You can now log in with your new password."

init_db()


def render_html(content):
    st.html(textwrap.dedent(content))


# ============================================================
# HELPER: JS CLIPBOARD COPY BUTTON
# ============================================================

def render_copy_icon_button(text_to_copy, button_id):
    escaped_text = json.dumps(text_to_copy)
    button_html = f"""
    <div style="display: flex; justify-content: center; width: 100%; margin-top: 6px;">
        <button id="{button_id}" onclick="copyText_{button_id}()" title="Copy text" style="
            background: #ff4d4d;
            color: #ffffff;
            border: none;
            border-radius: 8px;
            padding: 0 16px;
            min-height: 34px;
            height: 34px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 700;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            box-sizing: border-box;
            box-shadow: 0 4px 12px rgba(255, 77, 77, 0.25);
        ">Copy text</button>
    </div>
    <script>
        function copyText_{button_id}() {{
            const text = {escaped_text};
            navigator.clipboard.writeText(text).then(function() {{
                const btn = document.getElementById('{button_id}');
                btn.innerText = '✓ Copied!';
                btn.style.background = '#10b981';
                btn.style.boxShadow = '0 4px 12px rgba(16, 185, 129, 0.25)';
                setTimeout(function() {{
                    btn.innerText = 'Copy text';
                    btn.style.background = '#ff4d4d';
                    btn.style.boxShadow = '0 4px 12px rgba(255, 77, 77, 0.25);';
                }}, 2000);
            }}).catch(function(err) {{
                console.error('Copy error: ', err);
            }});
        }}
    </script>
    """
    components.html(button_html, height=42)


# ============================================================
# STREAMLIT CONFIG
# ============================================================

st.set_page_config(
    page_title="Aunty Next DOOR • Transcriber",
    page_icon="🎙️",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# UPLOAD CONFIGURATION
# ============================================================

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


# ============================================================
# GROQ API KEYS (STREAMLIT SECRETS ONLY)
# ============================================================
# API keys are intentionally NOT stored in this source file.
# Primary preferred secret: GROQ_API_KEY
# Secondary preferred secret: GROQ_API_KEY_SECONDARY_2
# Third preferred secret: GROQ_API_KEY_3
# Old secret names are also supported for compatibility.

try:
    GROQ_API_KEY = (
        st.secrets.get("GROQ_API_KEY", "")
        or st.secrets.get("GROQ_API_KEY", "")
    )
    GROQ_SECONDARY_API_KEY = (
        st.secrets.get("GROQ_API_KEY_SECONDARY_2", "")
        or st.secrets.get("GROQ_SECONDARY_API_KEY", "")
    )
    GROQ_THIRD_API_KEY = (
        st.secrets.get("GROQ_API_KEY_3", "")
    )
except Exception:
    GROQ_API_KEY = ""
    GROQ_SECONDARY_API_KEY = ""
    GROQ_THIRD_API_KEY = ""

if not GROQ_API_KEY:
    st.warning("Primary Groq API key is not configured in Streamlit Secrets.")


# ============================================================
# ACTIVE SESSION STORAGE
# ============================================================

DEFAULT_SESSION = {
    "logged_in_user": None,
    "current_view": "transcriber",
    "theme_mode": "dark",
    "source_name": "No file loaded",
    "file_path": None,
    "duration_sec": 0,
    "est_proc_sec": 0,
    "source_type": "Awaiting input",
    "transcript": [],
    "full_text": "",
    "short_topic": "",
    "detailed_summary": "",
    "transcribed": False,
    "status": "Ready for audio",
    "elapsed": 0,
}

for key, value in DEFAULT_SESSION.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# CAMPAIGN QUESTION SETS & PROMPTS
# ============================================================

CAMPAIGN_QC_QUESTIONS = {
    "Rehab & Addiction Treatment": """
Write a short, natural, human-written QC note for the Rehab call using simple English. Keep the summary concise. Combine related information into one sentence when possible. Do not repeat the same action or information. Prefer 2 short sentences over one long sentence.

Use ONLY information that is clearly stated in the transcript. Never guess, assume, interpret, or fill in missing information from context. If speech is unclear or garbled, skip that information. 

Clarify each speaker’s perspective: Always identify who said the information. Do not treat the agent’s question or statement as the caller’s answer or information. When summarizing insurance, treatment, location, appointment, or other details, clearly use the information provided by the correct speaker. If the caller answers the agent’s question, use the caller’s answer. If only the agent mentions something and the caller does not confirm it, do not present it as the caller’s information.

IMPORTANT RULES:
An agent's question is NOT the caller's answer.
**Insurance Rule:** Always check the **caller’s response** when the agent asks about insurance.
If the **agent asks the caller about insurance** and the caller gives an answer, include the **insurance name or insurance status** in the summary.
If the caller says they **do not have insurance**, write **“The caller said they do not have insurance.”**
If the caller clearly names an insurance provider, such as **Medicaid, Medicare, Blue Cross Blue Shield, Aetna, Cigna, or other insurance**, include that exact insurance name.
Do not use the **agent’s question** as the caller’s answer. The insurance information must come from the **caller’s response or statement**. If the agent asks about insurance but the caller's response is completely unclear or unintelligible, skip the insurance information. If neither the agent nor the caller discusses insurance, skip it.

Never state that the caller has or does not have insurance unless the caller clearly says so.
If the caller does not clearly answer a question, skip it.
Do not turn unclear speech into a definite fact.
Do not create or assume information that is not clearly stated.
Do not mention missing information unless it is necessary to explain something that actually happened.
Do not say "no clear resolution," "no confirmed appointment," "no referral," or similar phrases unless the transcript clearly supports that statement.
Do not repeat the same information.

Focus only on clearly understood information, such as:
**Reason for calling**
**Treatment or Rehab service requested**
**Location or ZIP code**
**Insurance or payment information**, only if clearly stated
**Appointment or scheduling**, only if clearly discussed
**Important qualification information**
**What the agent provided or advised**
**Phone number, referral, or next step**
**How the call ended**

For treatment, use the exact service that is clearly understood. If only "rehab" or "treatment" is clear, use that instead of guessing a specific treatment.
For insurance, include it only when the caller clearly provides the insurance type or payment information.
For appointments, only mention an appointment if the caller clearly requested one or the agent clearly scheduled one. Do not assume an appointment was made or not made.
For the ending, briefly describe what actually happened, such as the agent providing a phone number, giving a referral, scheduling an appointment, the caller thanking the agent, or the call ended.

Use natural phrases such as:
"The caller was looking for..."
"The caller called about..."
"The caller wanted..."
"The caller asked about..."
"The agent provided..."
"The agent gave..."
"The call ended after..."

Avoid unnatural phrases such as:
"The caller identified themselves as..."
"The caller presented themselves as..."
"The caller indicated that..."
"The agent inquired about..."
"No clear details were provided..."
"No confirmed appointment was made..."

Write ONE short paragraph only. No bullets, headings, or sections. Use **bold text** only for the most important treatment, service, action, or outcome.

Keep the final summary very short, factual, natural, and conversational, like a human-written QC note.
""",

    "Dumpster & Porta Potty Services": """
For each question below, only include the answer if it is clearly mentioned or answered in the call. If the caller does not answer a question or the information is not mentioned, skip that question completely. Do not assume, guess, or add missing information.

What service is the caller looking for? Dumpster or Porta Potty.
What size or capacity does the caller need? For dumpsters, include the size such as 10-yard, 20-yard, 30-yard, or 40-yard. For Porta Potty, include the capacity or type if mentioned.
What is the service for? For example, residential, commercial, moving, construction, party, or event.
Did the caller ask about the price? If yes, include the price or pricing information discussed.
Did the caller provide or agree to provide their address or delivery location? Include it only if clearly mentioned.
Did the caller provide or agree to provide their phone number? Include it only if clearly mentioned.
What delivery date or time does the caller need? Mention if the request is urgent or if the requested delivery time was unavailable.
Was an appointment, booking, or delivery scheduled? If yes, include the confirmed date and time.
""",

    "Pest Control & Home Services": """
What specific home service or pest problem is the caller asking about?
Is the caller a homeowner or renter?
What service area or location did the caller mention?
Was an appointment or inspection scheduled?
Did the caller want a quote, service, or was it an unrelated inquiry or wrong number?
How did the call end?
Did the agent properly handle and qualify the caller?
""",

    "Insurance (Health / Auto / Home)": """
What type of insurance coverage is the caller seeking (Health, Medicare, Auto, Home)?
What is the caller's current insurance or policy situation?
Is the caller looking for a new policy, quote, or asking about an existing policy?
Is it a wrong number or unrelated inquiry?
Did the caller meet qualifying criteria (age, location, employment state)?
How did the call end (transferred, quote given, callback set)?
Did the agent properly handle and qualify the caller?
""",

    "Debt Relief & Financial Services": """
What type of debt relief or financial assistance is the caller asking for?
What total amount of debt did the caller state they have?
Is the debt unsecured (credit cards, personal loans) or secured?
Is it a wrong number, spam, or unrelated inquiry?
Was the caller transferred, enrolled, or scheduled for a consultation?
How did the call end?
Did the agent properly handle and qualify the caller?
""",
}


# ============================================================
# DEFAULT QC QUESTIONS
# ============================================================

DEFAULT_QC_QUESTIONS = """
Write a short, natural, human-written QC note for the call using simple English.

Only include information that is clearly understood from the transcript. Focus on the information that is relevant to the campaign and the quality of the call.

Focus on:
- Why the caller contacted the business or service
- What product, service, or information the caller was looking for
- Any important qualification information clearly discussed
- Location or eligibility information when clearly provided
- Any price, quote, appointment, transfer, booking, or next step that was actually discussed
- Whether the call was relevant, unrelated, a wrong number, or otherwise unsuitable
- Any clear caller objection or concern
- Any clear agent mistake or handling issue
- How the call ended

Do not guess, assume, or fill in missing information.

Always distinguish between the caller's information and the agent's statements or questions. An agent's question is NOT the caller's answer. If the caller does not clearly answer a question, skip that information.

If one person speaks and the other person does not respond, describe the actual situation naturally when relevant.

For example:
"The agent spoke, but the caller did not respond."
"The agent asked a question, but the caller did not respond."
"The caller responded, but the agent did not respond."

If neither side responds and the transcript clearly indicates silence, no meaningful speech, or dead air, describe it naturally as:
"The call had dead air with no response from either side."

If the agent gives a greeting or speaks and there is no caller response
