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
import re
from typing import Optional


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
    """Map real Ringba campaign names to the closest QC campaign.
    The keywords for each campaign are edited in the app (Campaign Rules page)."""
    raw = (raw_campaign_text or "").strip()
    text = raw.lower()

    if not raw:
        return "General Customer Inquiry"

    campaigns = (globals().get("QC_CONFIG") or {}).get("campaigns") or {}

    # Exact campaign name first.
    for name in campaigns:
        if name.strip().lower() == text:
            return name

    # Then the name keywords, lowest "match priority" first.
    ordered = sorted(campaigns.items(), key=lambda item: item[1].get("match_priority", 100))
    for name, camp in ordered:
        for keyword in camp.get("name_keywords") or []:
            keyword = str(keyword).strip().lower()
            if keyword and keyword in text:
                return name

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
# Primary preferred secret: Aunty_NEXT_DOOR_API_PRIMARY
# Secondary preferred secret: GROQ_API_KEY_SECONDARY_2
# Old secret names are also supported for compatibility.

try:
    GROQ_API_KEY = (
        st.secrets.get("Aunty_NEXT_DOOR_API_PRIMARY", "")
        or st.secrets.get("GROQ_API_KEY", "")
    )
    GROQ_SECONDARY_API_KEY = (
        st.secrets.get("GROQ_API_KEY_SECONDARY_2", "")
        or st.secrets.get("GROQ_SECONDARY_API_KEY", "")
    )
    GROQ_API_KEY_3 = st.secrets.get("GROQ_API_KEY_3", "")
    GROQ_API_KEY_4 = st.secrets.get("GROQ_API_KEY_4", "")
except Exception:
    GROQ_API_KEY = ""
    GROQ_SECONDARY_API_KEY = ""
    GROQ_API_KEY_3 = ""
    GROQ_API_KEY_4 = ""

GROQ_API_KEYS = [k for k in [
    GROQ_API_KEY, GROQ_SECONDARY_API_KEY, GROQ_API_KEY_3, GROQ_API_KEY_4
] if k]

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

If the agent gives a greeting or speaks and there is no caller response, do not create a reason for the call or assume the caller disconnected. Simply describe the lack of response.

If the caller gives a response but the agent does not continue or answer, do not assume that the call was completed, transferred, scheduled, or resolved.

If the transcript contains unclear, garbled, or incomplete speech, do not convert it into a definite response. Only describe the response if the intended meaning is reasonably clear.

Do not use "dead air" when one side clearly responded. Use "did not respond" when only one side failed to respond.

Do not repeat the same response issue multiple times. Mention it once in the most natural place in the QC summary.

Keep the summary short, factual, natural, and conversational, like a human-written QC note.

Write ONE short paragraph only. No bullets, headings, or sections.
"""


# ============================================================
# MAIN QC SYSTEM PROMPT
# ============================================================

QC_SYSTEM_PROMPT = """
You are a Call QC analyst for a pay-per-call affiliate network.

Your task is to summarize a call transcript for QC review based on the campaign-specific QC questions provided below.

The transcript is generated by Vosk speech-to-text / whisper and may contain grammar mistakes, repeated words, missing words, or incorrect word recognition. Understand the conversation using context and correct obvious transcription errors only when the intended meaning is clear. Never invent or assume information.

CAMPAIGN:
{campaign_name}

QC QUESTIONS:
{qc_questions}

STRICT RULES:
Read the entire transcript before summarizing.
Check the transcript against the QC questions for this campaign.
Include only information clearly supported by the transcript.
If a QC question is not answered or the information is unclear, skip it completely.
Never write "not mentioned", "not provided", "unknown", or similar phrases for missing information.
Never guess or assume missing information.
Keep campaign-specific details that are relevant to QC and qualification.
Mention important issues such as wrong number, unrelated inquiry, disqualification, agent mistake, caller objection, fake/coached behavior, or other QC concerns when clearly supported by the call.
Include how the call ended when this information is available.
Do not include unnecessary personal information such as the caller's name, phone number, or address.
Do not repeat information.
Do not mention the QC questions in the summary.
Do not use bullet points or numbered lists.
Do not use headings or separate sections.
Write ONE short, flat paragraph only.
Use simple, clear, professional English.
Focus on facts relevant to the campaign and QC.
Do not make a payment, credit, or rejection decision unless the QC questions specifically ask for it.
Do not add any information that is not supported by the transcript.

UNIVERSAL RESPONSE RULES:
These rules apply to EVERY campaign, including campaigns with campaign-specific QC questions and campaigns using the default QC questions.

Always pay attention to whether both sides actually responded to each other.

An agent's question is NOT the caller's answer.

If the agent speaks or asks a question and the caller does not respond, clearly describe that situation when it is relevant to the QC outcome.

Use natural wording such as:
"The agent spoke, but the caller did not respond."
"The agent asked a question, but the caller did not respond."

If the caller speaks or asks a question and the agent does not respond, clearly describe that situation when it is relevant to the QC outcome.

If there is no voice or text on recording, fill the summary box as auto fielled with this text - no voice.

Use natural wording such as:
"The caller responded, but the agent did not respond."

If neither side responds and the transcript clearly indicates silence, no meaningful speech, or dead air, write:
"The call had dead air with no response from either side."

If the agent gives a greeting or speaks and there is no caller response, do not create a reason for the call and do not assume the caller disconnected. Simply describe the lack of response.

If the caller gives a response but the agent does not continue or answer, do not assume that the call was completed, transferred, scheduled, or resolved.

If the transcript contains unclear, garbled, or incomplete speech, do not convert it into a definite response. Only describe the response if the intended meaning is reasonably clear.

Do not use "dead air" when one side clearly responded.
Use "did not respond" when only one side failed to respond.

Do not repeat the same response issue multiple times. Mention it once in the most natural place in the QC summary.

If the transcript contains only a greeting, attempted greeting, or very short exchange without a meaningful conversation, summarize only what actually happened. Do not invent a caller intent or campaign qualification.

If the call ends because one side stops responding, describe the actual response pattern when it is relevant. Do not assume the reason for the call ending.

DO NOT use any Markdown formatting or asterisks (* or **). Output PLAIN TEXT ONLY.

OUTPUT:
Return ONLY the final QC summary paragraph.

CALL TRANSCRIPT:
{call_transcript}
"""


# ============================================================
# SHORT TOPIC PROMPT
# ============================================================

SHORT_TOPIC_PROMPT = """
MAIN TOPIC SUMMARY:

Write ONE short sentence describing the caller's true main topic and reason for calling based on the summary below.

SUMMARY:
{call_transcript}
"""


# ============================================================
# QC QUESTION SELECTOR
# ============================================================

def get_qc_questions(campaign_name):
    if not campaign_name:
        return DEFAULT_QC_QUESTIONS

    if campaign_name in CAMPAIGN_QC_QUESTIONS:
        return CAMPAIGN_QC_QUESTIONS[campaign_name]

    campaign_name_clean = campaign_name.strip().lower()

    for campaign, questions in CAMPAIGN_QC_QUESTIONS.items():
        if campaign.strip().lower() == campaign_name_clean:
            return questions

    return DEFAULT_QC_QUESTIONS


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def format_time(seconds):
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02d}:{secs:02d}"

def transcribe_groq_whisper(audio_file_path):
    if not GROQ_API_KEY:
        raise RuntimeError("Groq API key not found. Configure Aunty_NEXT_DOOR_API_PRIMARY.")

    client = Groq(api_key=GROQ_API_KEY)

    with open(audio_file_path, "rb") as file:
        transcription = client.audio.transcriptions.create(
            file=(os.path.basename(audio_file_path), file.read()),
            model="whisper-large-v3-turbo",
            response_format="verbose_json",
            language="en",
        )

    timeline_data = []
    raw_text_segments = []

    segments = getattr(transcription, "segments", None)
    if segments:
        for seg in segments:
            start_t = seg.get("start", 0.0) if isinstance(seg, dict) else seg.start
            end_t = seg.get("end", 0.0) if isinstance(seg, dict) else seg.end
            text_value = seg.get("text", "").strip() if isinstance(seg, dict) else seg.text.strip()
            if text_value:
                timeline_data.append({
                    "time": f"{format_time(start_t)} – {format_time(end_t)}",
                    "speaker": "Unknown",
                    "line": text_value,
                })
                raw_text_segments.append(text_value)
    else:
        full_text = getattr(transcription, "text", "").strip()
        if full_text:
            timeline_data.append({"time": "00:00 – 00:00", "speaker": "Unknown", "line": full_text})
            raw_text_segments.append(full_text)

    return timeline_data, raw_text_segments


GROQ_SUMMARY_MODEL = "openai/gpt-oss-20b"


# ============================================================
# SUMMARY STYLE (short, relevant, no filler)
# ============================================================

SUMMARY_STYLE_RULES = """
SUMMARY STYLE (applies to long_summary and main_topic):
- long_summary = ONE short plain-text paragraph of 2 to 4 short sentences (about 35-80 words). Never more than 4 sentences.
- Order: why the caller called -> only the campaign-relevant details the caller clearly stated -> what the agent actually provided or did -> how the call ended.
- Include ONLY facts that are clearly stated in the transcript AND relevant to the campaign QC questions below. Leave out everything else.
- Do NOT include: greetings or small talk, the caller's name, phone number or address, company or agent names, repeated information, opinions or comments about the quality of the call, statements about what was missing, guesses, or filler.
- Never write "not mentioned", "not provided", "not stated", "unknown", "no clear resolution", "no confirmed appointment", "no referral", or similar. If something is unclear, leave it out silently.
- Never say the same fact twice. Combine related facts into one sentence.
- An agent's question is NOT the caller's answer. Treat something as caller information only when the caller clearly said or confirmed it.
- Use simple natural wording such as "The caller was looking for...", "The caller asked about...", "The agent provided...", "The call ended after...".
- If the call is spam / robot / solicitation (for example Yelp, Yellow Pages, Google listing, SEO, marketing), say in one or two sentences what it was promoting. Do not describe it as a normal caller.
- If the caller wanted a different service than this campaign, say what the caller actually wanted in one sentence.
- If only one side spoke, say so in one short sentence (for example "The agent spoke, but the caller did not respond."). If neither side responded, write "The call had dead air with no response from either side."
- Plain text only. No bullets, headings, markdown, asterisks, or bold, even if the QC questions below mention bold text.
- main_topic = ONE sentence of 5-18 words stating the caller's real reason for calling. No filler.
"""


def build_summary_instructions(campaign_name):
    """Style rules + the campaign-specific QC questions (these decide what belongs in the summary)."""
    family = get_campaign_category(campaign_name) if campaign_name else campaign_name
    qc_questions = get_qc_questions(family)
    return (
        SUMMARY_STYLE_RULES
        + "\nCAMPAIGN QC QUESTIONS (use them only to decide what belongs in the summary; never mention them):\n"
        + qc_questions.strip()
        + "\n"
    )


_BANNED_SUMMARY_RE = re.compile(
    r"\b(?:not|never|wasn't|was not|were not|weren't|isn't|is not|no)\s+(?:clearly\s+|further\s+|any\s+)?"
    r"(?:mentioned|provided|stated|specified|discussed|given|disclosed|details?|information)\b"
    r"|\bunknown\b|\bno clear (?:resolution|outcome|details?)\b|\bno confirmed appointment\b"
    r"|\bno referral\b|\bnot clear\b|\bthe transcript\b|\bas an ai\b|\bqc questions?\b"
    r"|\b(?:said|says|greeted|greeting)\s+(?:hello|hi|hey|good (?:morning|afternoon|evening))\b"
    r"|\bthanked (?:the )?(?:caller|agent)\b",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")


def clean_summary_text(text, max_sentences=4, max_chars=700):
    """Final safety net: strip markdown, filler/'not mentioned' sentences, phone numbers,
    duplicate sentences, and cap the length."""
    raw = str(text or "")
    raw = re.sub(r"[*`#>]+", "", raw)
    raw = re.sub(r"^\s*(?:[-\u2022]|\d+[.)])\s+", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = _PHONE_RE.sub("a phone number", raw)
    if not raw:
        return ""

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", raw) if s.strip()]
    kept, seen = [], set()
    for sentence in sentences:
        if _BANNED_SUMMARY_RE.search(sentence):
            continue
        key = re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        kept.append(sentence)
        if len(kept) >= max_sentences:
            break

    result = " ".join(kept) if kept else " ".join(sentences[:2])
    if len(result) > max_chars:
        result = result[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return result


def clean_main_topic(text, max_words=25):
    """One short sentence, no markdown."""
    raw = re.sub(r"[*`#>]+", "", str(text or ""))
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return ""
    first = re.split(r"(?<=[.!?])\s+", raw)[0].strip()
    words = first.split()
    if len(words) > max_words:
        first = " ".join(words[:max_words]).rstrip(",;:") + "..."
    return first




def _analysis_prompt(full_transcript, campaign_name, timeline_data=None):
    """Build a compact analysis prompt. Keep the transcript, but avoid sending
    timestamps, speaker-label instructions, or the old long JSON schema."""
    campaign_guidance = get_campaign_focus(campaign_name)

    service_categories = ", ".join(CAMPAIGN_QC_QUESTIONS.keys())
    summary_instructions = build_summary_instructions(campaign_name)
    yelp_rule = (
        "- ANY mention of Yelp or Yellow Pages, in any context, means spam_robot=true, spam_confidence=99, call_type=SPAM / ROBOT."
        if SPAM_YELP_YELLOW_ENABLED
        else "- A caller mentioning Yelp or Yellow Pages is not automatically spam."
    )

    return f"""
You are a pay-per-call call QC analyst. Analyze the complete transcript and return ONLY one valid JSON object.

CAMPAIGN: {campaign_name}
CAMPAIGN FOCUS: {campaign_guidance}

Return exactly these keys:
long_summary, main_topic, call_type, qualification_status, caller_intent, why_called,
service_requested, insurance, location, outcome, spam_robot, spam_confidence,
qc_issue, spam_reason, qualification_reason, call_type_reason,
detected_service_category, matches_campaign, appointment_set

detected_service_category = the service the CALLER actually asked for, judged only from the caller's own words (NOT from the campaign name). Choose exactly one of: {service_categories}, OTHER, UNCLEAR.
matches_campaign = true if the caller's requested service belongs to this campaign's service; false if the caller wanted a different service (example: campaign is Dumpster but the caller is asking about rehab/addiction treatment); "unclear" if the caller never said what they wanted. This is about SERVICE TYPE only, never about insurance or eligibility.
appointment_set = true only if an appointment, booking, delivery, consultation, or warm transfer was clearly confirmed during the call; otherwise false.

Allowed call_type values: QUALIFIED, NON-QUALIFIED, WRONG NUMBER, SPAM / ROBOT, INFORMATION ONLY, SILENT / NO RESPONSE, OTHER.
Allowed qualification_status values: QUALIFIED, NON-QUALIFIED, NOT CLEAR.
Use empty strings for unknown string values. spam_robot must be true/false. spam_confidence must be 0-100.

RULES:
- Use only facts supported by the transcript. Never guess or invent.
- call_type_reason must be one short sentence (5-15 words) explaining why you chose that call_type, e.g. "Private insurance and seeking detox".
- An agent's question is NOT the caller's answer. Insurance must come from the caller's own statement/response.
- long_summary and main_topic MUST follow the SUMMARY STYLE and the campaign QC questions given after these rules. Keep them short and free of unnecessary detail.
- If the caller is unrelated, explain what they actually wanted.
- If the call is a wrong number, describe what they were trying to reach when clear.
- If the call is silent/no-response, do not invent caller intent.
- If spam/robot, describe what the call was promoting or asking the recipient to do.
- Detect spam semantically: scripted/repeated language, press-0/press-9 instructions, automated marketing, fake verification, SEO/Google listing solicitations, insurance/debt marketing robots, and similar behavior.
{yelp_rule}
- A normal irrelevant or non-qualified caller is not automatically spam.
- For Rehab, Medicaid/Medicare/state/government/marketplace/public insurance means NON-QUALIFIED. Treat private/commercial insurance as a positive qualification signal when clearly stated by the caller, including statements such as “private insurance,” “commercial insurance,” or insurance through the caller’s, parent’s, spouse’s, or another family member’s employer. PPO, HMO, EPO, and POS are also positive signals when clearly stated. Do not assume the provider or plan type if it is not stated.
- qualification_reason should briefly explain why the qualification status was chosen.
- qc_issue should contain only a real QC issue when supported; otherwise empty.
- If the caller is asking for a different service than this campaign's service, set matches_campaign=false and call_type=WRONG NUMBER.
- Use QUALIFIED or NON-QUALIFIED ONLY when the caller is asking for this campaign's service. Information-only, unclear, or off-campaign callers must use INFORMATION ONLY, WRONG NUMBER, or OTHER.

{summary_instructions}

TRANSCRIPT:
{full_transcript}
"""


def _parse_json_object(content):
    """Parse JSON returned by JSON mode, with a small fallback for accidental fences."""
    raw = str(content or "{}").strip()
    if raw.startswith("```"):
        raw = raw.replace("```json", "", 1).replace("```", "", 1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def _log_groq_token_usage(response, label="GROQ"):
    usage = getattr(response, "usage", None)
    if not usage:
        return
    print("========== GROQ TOKEN USAGE ==========")
    print(f"Request:      {label}")
    print(f"Input tokens:  {getattr(usage, 'prompt_tokens', 0)}")
    print(f"Output tokens: {getattr(usage, 'completion_tokens', 0)}")
    print(f"Total tokens:  {getattr(usage, 'total_tokens', 0)}")
    print("======================================")


def _call_structured_analysis(client, prompt, label="GROQ ANALYSIS"):
    response = client.chat.completions.create(
        model=GROQ_SUMMARY_MODEL,
        messages=[
            {
                "role": "system",
                "content": "Return only one valid JSON object using exactly the requested keys. Do not add commentary."
            },
            {"role": "user", "content": prompt}
        ],
        temperature=0.1,
        max_tokens=900,
        reasoning_effort="low",
        reasoning_format="hidden",
        response_format={"type": "json_object"}
    )
    _log_groq_token_usage(response, label)
    content = response.choices[0].message.content or "{}"
    return _parse_json_object(content)


# ------------------------------------------------------------
# SPAM RULES (strict)
# ------------------------------------------------------------
# Any mention of Yelp or Yellow Pages = SPAM, no other condition needed.
# Set True to treat ANY mention of the word "yellow" as spam as well.
STRICT_YELLOW_ANY = False
SPAM_YELP_YELLOW_ENABLED = True   # editable in the app (Campaign Rules -> Spam)
EXTRA_SPAM_RES = []               # extra always-spam words, editable in the app

_YELP_RE = re.compile(r"\by\W{0,2}e\W{0,2}l\W{0,2}p\b|\byelp\w*", re.IGNORECASE)
_YELLOW_DIRECTORY_RE = re.compile(
    r"\byellow\s*[-.]?\s*(?:pag\w*|book)\b"
    r"|\byellowpag\w*"
    r"|\byp\s*(?:dot\s*)?(?:\.\s*)?com\b"
    r"|\byellow\b(?:\W+\w+){0,3}?\W+(?:listing|listings|directory|ads?|advertis\w*|business|profile)\b",
    re.IGNORECASE,
)
_ANY_YELLOW_RE = re.compile(r"\byellow\b", re.IGNORECASE)

# Strong robot / solicitation patterns: one hit is enough.
_STRONG_SPAM_RES = [
    (re.compile(r"\bautomated (?:message|call|voice|system)\b|\bthis is a recorded message\b|\bpre-?recorded (?:message|call)\b", re.I),
     "Automated / recorded message"),
    (re.compile(r"\bpress (?:the )?(?:number |key )?(?:zero|one|two|three|four|five|six|seven|eight|nine|[0-9]|star|pound)\b", re.I),
     "Press-a-key instruction"),
    (re.compile(r"\bgoogle\s+(?:my\s+)?(?:business|listing|maps?|profile)\b.{0,120}?\b(?:verify|verification|claim|update|rank\w*|first page|suspend\w*|remov\w*|expire\w*)\b", re.I | re.S),
     "Google listing solicitation"),
]

# Softer solicitation phrases: 3 different kinds in one call = spam.
_MEDIUM_SPAM_PATTERNS = {
    "SEO": r"\bseo\b|search engine optimi[sz]ation",
    "Google listing": r"\bgoogle\s+(?:listing|business|maps?|profile)\b",
    "Google first page": r"\bfirst page of google\b|\btop of google\b",
    "Business listing": r"\bbusiness listing\b|\bclaim your\b|\bverify your business\b|\byour business profile\b",
    "Website/marketing": r"\bonline presence\b|\bwebsite traffic\b|\bweb design\b|\bdigital marketing\b|\bsocial media (?:marketing|management)\b",
    "Advertising": r"\badvertis\w*\b|\blead generation\b",
    "Warranty": r"\bextended (?:car )?warranty\b|\bvehicle warranty\b",
    "Rates/loans": r"\blower your (?:interest|rate|monthly)\b|\bcredit card rates\b|\bstudent loan forgiveness\b",
    "Robo promo": r"\blimited time offer\b|\bspecial offer\b|\bthis is not a sales call\b|\byou(?:'ve| have) been (?:selected|chosen|pre-?approved)\b",
}
_MEDIUM_SPAM_RES = [(re.compile(p, re.I), label) for label, p in _MEDIUM_SPAM_PATTERNS.items()]


def detect_spam_signals(full_transcript):
    """Return (confidence 0-100, reason, label). 0 means no deterministic spam signal."""
    text = str(full_transcript or "")
    if not text.strip():
        return 0, "", ""

    if SPAM_YELP_YELLOW_ENABLED and _YELP_RE.search(text):
        return 99, "Yelp mentioned on the call (strict network spam rule)", "Yelp"
    if SPAM_YELP_YELLOW_ENABLED and (_YELLOW_DIRECTORY_RE.search(text) or (STRICT_YELLOW_ANY and _ANY_YELLOW_RE.search(text))):
        return 99, "Yellow Pages mentioned on the call (strict network spam rule)", "Yellow Pages"

    for rx in EXTRA_SPAM_RES:
        hit = rx.search(text)
        if hit:
            return 99, f"Blocked spam word '{hit.group(0)}' mentioned on the call", "Blocked word"

    for rx, label in _STRONG_SPAM_RES:
        if rx.search(text):
            return 92, f"{label} detected in transcript", label

    hits = [label for rx, label in _MEDIUM_SPAM_RES if rx.search(text)]
    if len(hits) >= 3:
        return 90, "Multiple solicitation phrases: " + ", ".join(hits[:4]), "Solicitation"

    return 0, "", ""


def apply_deterministic_spam_rules(analysis, full_transcript):
    """Strict spam rules applied to the FULL transcript after the AI analysis.

    - Any Yelp / Yellow Pages mention = SPAM (confidence 99).
    - Robot patterns (automated message, press-a-key, Google listing scripts) = SPAM.
    - 3+ different solicitation phrases = SPAM.
    """
    confidence, reason, label = detect_spam_signals(full_transcript)
    if not confidence:
        return analysis

    analysis["spam_robot"] = True
    analysis["spam_confidence"] = max(_safe_int(analysis.get("spam_confidence")), confidence)
    current_reason = str(analysis.get("spam_reason", "") or "").strip()
    analysis["spam_reason"] = reason if not current_reason else f"{reason}; {current_reason}"
    analysis["call_type"] = "SPAM / ROBOT"
    analysis["qualification_status"] = "NOT CLEAR"
    if not str(analysis.get("qc_issue", "") or "").strip():
        analysis["qc_issue"] = f"{label} spam / solicitation call"
    return analysis


# ============================================================
# NO-VOICE DETECTION, CAMPAIGN MATCHING & BUSINESS RULES
# ============================================================

NO_VOICE_MIN_WORDS = 3                      # fewer words than this = "no voice"
MIN_AUDIO_SECONDS_FOR_TRANSCRIPTION = 1.0   # shorter audio is not sent to Whisper
NO_VOICE_TOPIC = "No voice"
NO_VOICE_SUMMARY = "No voice"

# Whisper often "hallucinates" these phrases on silent audio.
_SILENCE_HALLUCINATION_HINTS = (
    "thanks for watching", "thank you for watching", "please subscribe",
    "like and subscribe", "subtitles by", "amara org",
)

ALLOWED_CALL_TYPES = {
    "QUALIFIED", "NON-QUALIFIED", "WRONG NUMBER", "SPAM / ROBOT",
    "INFORMATION ONLY", "SILENT / NO RESPONSE", "OTHER",
}
ALLOWED_QUAL_STATUSES = {"QUALIFIED", "NON-QUALIFIED", "NOT CLEAR"}


def is_no_voice_transcript(text):
    """True when the recording produced no usable text (empty, 1-2 words,
    or a typical Whisper silence hallucination)."""
    cleaned = re.sub(r"[^a-z0-9' ]+", " ", str(text or "").lower())
    words = cleaned.split()
    if not words:
        return True
    normalized = " ".join(words)
    if len(words) <= 8 and any(hint in normalized for hint in _SILENCE_HALLUCINATION_HINTS):
        return True
    return len(words) < NO_VOICE_MIN_WORDS


def _is_short_audio_error(exc):
    """Groq rejects extremely short / empty audio. Treat that as No Voice."""
    msg = str(exc).lower()
    return any(k in msg for k in ("too short", "minimum audio length", "empty"))


def _to_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def _parse_match_flag(value):
    """True / False / None (unclear)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "yes", "1"}:
            return True
        if v in {"false", "no", "0"}:
            return False
    return None


def get_campaign_family(campaign_name):
    """Return the known QC campaign family for a campaign name, or None."""
    family = get_campaign_category(campaign_name)
    return family if family in CAMPAIGN_QC_QUESTIONS else None


def _match_known_family(text):
    """Map the AI's detected_service_category to a known campaign family, or None."""
    value = str(text or "").strip()
    if not value or value.upper() in {"OTHER", "UNCLEAR"}:
        return None
    for family in CAMPAIGN_QC_QUESTIONS:
        if family.lower() == value.lower():
            return family
    return get_campaign_family(value)


def build_no_voice_analysis(campaign_name=""):
    """Default analysis for recordings with no text. Score is fixed at 30."""
    analysis = {
        "long_summary": NO_VOICE_SUMMARY,
        "main_topic": NO_VOICE_TOPIC,
        "call_type": "SILENT / NO RESPONSE",
        "qualification_status": "NOT CLEAR",
        "caller_intent": "",
        "why_called": "",
        "service_requested": "",
        "insurance": "",
        "location": "",
        "outcome": "",
        "spam_robot": False,
        "spam_confidence": 0,
        "qc_issue": "No voice detected in recording",
        "spam_reason": "",
        "qualification_reason": "",
        "call_type_reason": "No speech or text in recording",
        "detected_service_category": "UNCLEAR",
        "matches_campaign": None,
        "appointment_set": False,
        "no_voice": True,
        "campaign_category": campaign_name,
    }
    return _derive_analysis_flags(analysis, "", timeline_data=[])


def apply_campaign_rules(analysis, campaign_name, full_transcript="", timeline_data=None):
    """Deterministic business rules applied AFTER the AI analysis, using the FULL transcript.

    1. Campaign vs service mismatch (e.g. Rehab caller on a Dumpster campaign)
       -> WRONG NUMBER. The transcript keywords decide first, the AI decides when the
       transcript is not clear enough.
    2. call_type / qualification_status must agree with each other.
    3. Rehab + government/state insurance (caller's own words) -> NON-QUALIFIED.
    """
    family = get_campaign_family(campaign_name)

    call_type = str(analysis.get("call_type", "OTHER") or "OTHER").strip().upper()
    if call_type not in ALLOWED_CALL_TYPES:
        call_type = "OTHER"
    status = str(analysis.get("qualification_status", "NOT CLEAR") or "NOT CLEAR").strip().upper()
    if status not in ALLOWED_QUAL_STATUSES:
        status = "NOT CLEAR"
    spam = _to_bool(analysis.get("spam_robot")) or call_type == "SPAM / ROBOT"

    # Insurance read from the caller's own words in the transcript.
    if full_transcript:
        analysis["transcript_insurance_status"] = detect_transcript_insurance(full_transcript, timeline_data)

    detected = _match_known_family(analysis.get("detected_service_category"))
    matches = _parse_match_flag(analysis.get("matches_campaign"))

    verdict, transcript_family = detect_transcript_family(full_transcript, family)
    analysis["transcript_family"] = transcript_family or ""
    if verdict == "other":
        detected = transcript_family
    elif verdict == "campaign":
        detected = family

    # ---- 1. Campaign / service mismatch -> WRONG NUMBER ----
    mismatch = False
    if not spam and call_type != "SILENT / NO RESPONSE":
        if verdict == "other":
            mismatch = True
        elif verdict == "campaign":
            mismatch = False
        elif family and detected:
            mismatch = detected != family
        else:
            mismatch = matches is False

    if mismatch:
        wanted = (
            detected
            or str(analysis.get("service_requested") or "").strip()
            or "a different service"
        )
        analysis["call_type"] = "WRONG NUMBER"
        analysis["qualification_status"] = "NON-QUALIFIED"
        analysis["campaign_mismatch"] = True
        analysis["detected_service_category"] = detected or analysis.get("detected_service_category", "")
        analysis["call_type_reason"] = f"Caller wanted {wanted}, but call came in on {campaign_name}"
        issue = f"Campaign/service mismatch: caller wanted {wanted}"
        existing = str(analysis.get("qc_issue") or "").strip()
        analysis["qc_issue"] = issue if not existing else f"{issue}; {existing}"
        return analysis

    analysis["campaign_mismatch"] = False

    # ---- 2. Keep call_type and qualification_status consistent ----
    if call_type == "QUALIFIED" and status == "NON-QUALIFIED":
        call_type = "NON-QUALIFIED"
    elif call_type == "NON-QUALIFIED" and status == "QUALIFIED":
        status = "NON-QUALIFIED"
    elif call_type not in {"QUALIFIED", "NON-QUALIFIED", "WRONG NUMBER"} and status == "QUALIFIED":
        status = "NOT CLEAR"

    # ---- 3. Rehab: government / state insurance is never qualified ----
    if (
        campaign_flag(family, "block_government_insurance")
        and call_type == "QUALIFIED"
        and get_rehab_insurance_status(analysis) == "YELLOW"
    ):
        call_type = "NON-QUALIFIED"
        status = "NON-QUALIFIED"
        analysis["call_type_reason"] = "Government/state insurance is not qualified for Rehab"

    analysis["call_type"] = call_type
    analysis["qualification_status"] = status
    return analysis


def _derive_analysis_flags(analysis, full_transcript, timeline_data=None, campaign_name=""):
    """Derive scoring/report flags in Python instead of spending AI tokens on them."""
    call_type = str(analysis.get("call_type", "OTHER") or "OTHER").strip().upper()
    allowed_call_types = {
        "QUALIFIED", "NON-QUALIFIED", "WRONG NUMBER", "SPAM / ROBOT",
        "INFORMATION ONLY", "SILENT / NO RESPONSE", "OTHER"
    }
    if call_type not in allowed_call_types:
        call_type = "OTHER"
    analysis["call_type"] = call_type

    qualification_status = str(analysis.get("qualification_status", "NOT CLEAR") or "NOT CLEAR").strip().upper()
    if qualification_status not in {"QUALIFIED", "NON-QUALIFIED", "NOT CLEAR"}:
        qualification_status = "NOT CLEAR"
    analysis["qualification_status"] = qualification_status

    spam_value = analysis.get("spam_robot", False)
    if isinstance(spam_value, str):
        analysis["spam_robot"] = spam_value.strip().lower() in {"true", "yes", "1"}
    else:
        analysis["spam_robot"] = bool(spam_value)
    try:
        analysis["spam_confidence"] = max(0, min(100, _safe_int(analysis.get("spam_confidence"))))
    except (TypeError, ValueError):
        analysis["spam_confidence"] = 0

    analysis["appointment_set"] = _to_bool(analysis.get("appointment_set", False))
    analysis["no_voice"] = _to_bool(analysis.get("no_voice", False))
    analysis["campaign_mismatch"] = _to_bool(analysis.get("campaign_mismatch", False))
    analysis["matches_campaign"] = _parse_match_flag(analysis.get("matches_campaign"))
    analysis["detected_service_category"] = str(
        analysis.get("detected_service_category", "") or ""
    ).replace("*", "").strip()

    for key in [
        "long_summary", "main_topic", "caller_intent", "why_called", "service_requested",
        "insurance", "location", "outcome", "qc_issue", "spam_reason", "qualification_reason",
        "call_type_reason"
    ]:
        analysis[key] = str(analysis.get(key, "") or "").replace("*", "").strip()

    # Keep the summary short and free of filler / "not mentioned" sentences.
    analysis["long_summary"] = clean_summary_text(analysis["long_summary"]) or (
        "No summary could be generated from the transcript."
    )
    analysis["main_topic"] = clean_main_topic(analysis["main_topic"])

    meaningful_reason = bool(analysis["caller_intent"] or analysis["why_called"] or analysis["service_requested"])
    analysis["relevant_intent"] = meaningful_reason and call_type not in {
        "WRONG NUMBER", "SPAM / ROBOT", "SILENT / NO RESPONSE"
    }

    analysis["qualification_info_present"] = bool(
        analysis["qualification_reason"] or analysis["insurance"] or
        qualification_status in {"QUALIFIED", "NON-QUALIFIED"}
    )
    analysis["location_or_eligibility_present"] = bool(
        analysis["location"] or analysis["insurance"]
    )
    analysis["clear_outcome"] = bool(analysis["outcome"])

    # Whisper does not diarize speakers. Use a conservative transcript-based signal
    # rather than asking the model to label every segment.
    segment_count = len([x for x in (timeline_data or []) if str(x.get("line", "")).strip()])
    if call_type == "SILENT / NO RESPONSE":
        two_way = False
    elif timeline_data is not None:
        two_way = segment_count >= 2 and meaningful_reason
    else:
        two_way = meaningful_reason and len(full_transcript.split()) >= 20
    analysis["two_way_conversation"] = bool(two_way)

    # Scoring facts are re-measured on the FULL transcript (not the summary).
    analysis = _apply_transcript_signals(analysis, full_transcript, timeline_data, campaign_name)

    analysis["major_qc_issue"] = bool(
        analysis["spam_robot"] or
        call_type in {"WRONG NUMBER", "SILENT / NO RESPONSE"} or
        analysis["qc_issue"]
    )
    return analysis

TPM_TARGET = 7400
ANALYSIS_MAX_OUT = 900      # same as max_tokens in _call_structured_analysis
CHARS_PER_TOKEN = 3.5       # conservative estimate, no extra library needed

def _trim_for_token_limit(transcript, campaign_name, timeline_data=None):
    """Return the transcript unchanged if it fits; otherwise keep start + end."""
    overhead_chars = len(_analysis_prompt("", campaign_name, timeline_data)) + 100
    budget_tokens = TPM_TARGET - ANALYSIS_MAX_OUT - int(overhead_chars / CHARS_PER_TOKEN)
    max_chars = int(budget_tokens * CHARS_PER_TOKEN)

    if len(transcript) <= max_chars:
        return transcript  # short call: untouched

    head = int(max_chars * 0.45)
    tail = max_chars - head
    return transcript[:head] + " ... [middle of call omitted] ... " + transcript[-tail:]

def _with_key_fallback(call_fn, label):
    """Try each configured Groq key in order until one succeeds."""
    if not GROQ_API_KEYS:
        raise RuntimeError("No Groq API keys are configured.")
    errors = []
    for i, key in enumerate(GROQ_API_KEYS, start=1):
        try:
            return call_fn(Groq(api_key=key), f"{label} KEY {i}")
        except Exception as exc:
            errors.append(f"Key {i}: {exc}")
            if "413" in str(exc):
                break  # request too large: every key would fail the same way
    raise RuntimeError("All Groq accounts failed. " + " | ".join(errors))

def generate_call_analysis_groq(full_transcript, campaign_name, timeline_data=None):
    # No text in the recording -> "No Voice" (fixed score). No AI tokens are spent.
    if is_no_voice_transcript(full_transcript):
        return build_no_voice_analysis(campaign_name)

    if not GROQ_API_KEYS:
        raise RuntimeError("No Groq API keys are configured.")

    ai_transcript = _trim_for_token_limit(full_transcript, campaign_name, timeline_data)
    prompt = _analysis_prompt(ai_transcript, campaign_name, timeline_data)

    analysis = _with_key_fallback(
        lambda c, lbl: _call_structured_analysis(c, prompt, lbl), "ANALYSIS"
    )

    # Rules below run on the FULL transcript (not the trimmed AI copy and not the summary).
    analysis = apply_deterministic_spam_rules(analysis, full_transcript)
    analysis = apply_campaign_rules(analysis, campaign_name, full_transcript, timeline_data)
    analysis["campaign_category"] = campaign_name

    return _derive_analysis_flags(analysis, full_transcript, timeline_data, campaign_name)


def _call_fast_summary(client, prompt, label="GROQ FAST SUMMARY"):
    """Small manual summary request using JSON mode without a JSON schema."""
    response = client.chat.completions.create(
        model=GROQ_SUMMARY_MODEL,
        messages=[
            {
                "role": "system",
                "content": "Return only one valid JSON object with keys main_topic and long_summary. Do not add commentary."
            },
            {"role": "user", "content": prompt}
        ],
        temperature=0.1,
        max_tokens=750,
        reasoning_effort="low",
        reasoning_format="hidden",
        response_format={"type": "json_object"}
    )
    _log_groq_token_usage(response, label)
    return _parse_json_object(response.choices[0].message.content or "{}")


def generate_fast_summary_groq(full_transcript, campaign_name):
    """Fast manual summary: only Main Topic + Long Summary. Full QC stays separate."""
    if is_no_voice_transcript(full_transcript):
        return NO_VOICE_TOPIC, NO_VOICE_SUMMARY

    if not GROQ_API_KEYS:
        raise RuntimeError("No Groq API keys are configured.")

    campaign_family = get_campaign_category(campaign_name)
    summary_instructions = build_summary_instructions(campaign_family)

    spam_hint = ""
    spam_confidence, spam_reason, _ = detect_spam_signals(full_transcript)
    if spam_confidence:
        spam_hint = (
            f"\nNOTE: This is a spam / solicitation call ({spam_reason}). "
            "Say what it was promoting. Do not describe it as a normal caller.\n"
        )

    prompt = f"""
You are a call QC note writer for a pay-per-call network.

CAMPAIGN:
{campaign_family}

Return ONLY one valid JSON object with exactly two keys: main_topic and long_summary.
{spam_hint}
{summary_instructions}

Read the whole transcript before writing. Use only facts clearly supported by the transcript.

TRANSCRIPT:
{full_transcript}
"""

    result = _with_key_fallback(
        lambda c, lbl: _call_fast_summary(c, prompt, lbl), "FAST SUMMARY"
    )

    main_topic = clean_main_topic(result.get("main_topic", ""))
    long_summary = clean_summary_text(result.get("long_summary", ""))

    if not main_topic:
        main_topic = "No clear main topic identified."
    if not long_summary:
        long_summary = "No summary could be generated from the transcript."

    return main_topic, long_summary


def generate_summaries_groq(full_transcript, campaign_name, timeline_data=None):
    """Compatibility wrapper for the existing UI; uses the fast summary path."""
    return generate_fast_summary_groq(full_transcript, campaign_name)


def _clean_report_value(value, default="None"):
    value = str(value or "").replace("|", "/").replace("\n", " ").strip()
    return value if value else default


# --------------------------------------------------------
# INSURANCE KEYWORDS
# --------------------------------------------------------

GOVERNMENT_TERMS = [
    "medicaid", "medicare", "medi-cal", "medi cal",
    "state insurance", "state-funded", "state funded", "state plan", "state program",
    "government insurance", "government-funded", "government funded",
    "government plan", "government program",
    "public insurance", "public plan", "public health insurance",
    "county insurance", "county-funded", "county funded",
    "chip", "marketplace",
]

PRIVATE_TERMS = [
    "private", "employer", "employee", "commercial",
    "company insurance", "company plan", "group insurance", "group plan",
    "insurance through work",
    "ppo", "hmo", "pos", "epo",
    "blue cross", "blue shield", "bcbs", "aetna", "cigna", "humana",
    "united healthcare", "unitedhealthcare", "anthem", "kaiser",
]


def _compile(terms):
    # Word-boundary matching: "chip" won't match "chipotle", "pos" won't match "position".
    pattern = r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


GOVERNMENT_RE = _compile(GOVERNMENT_TERMS)
PRIVATE_RE = _compile(PRIVATE_TERMS)

INSURANCE_KEYS = ("insurance",)


def _safe_int(value, default=0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


# "I don't have Medicaid", "not on Medicare", "no medicaid" must NOT count as government insurance.
GOVERNMENT_NEGATED_RE = re.compile(
    r"\b(?:no|not|don[\u2019']t have|do not have|doesn[\u2019']t have|does not have|never had|without)"
    r"\s+(?:\w+\s+){0,2}?(?:" + "|".join(re.escape(t) for t in GOVERNMENT_TERMS) + r")\b",
    re.IGNORECASE,
)


# ============================================================
# TRANSCRIPT-BASED SIGNALS
# Scoring facts come from the FULL TRANSCRIPT (not the summary).
# The AI fields are only a fallback / second opinion.
# ============================================================

FAMILY_KEYWORDS = {
    "Rehab & Addiction Treatment": [
        r"\brehab\w*", r"\bdetox\w*", r"\baddict\w*", r"\balcohol\w*", r"\bdrugs?\b",
        r"\bopioids?\b", r"\bheroin\b", r"\bfentanyl\b", r"\bmeth\b|\bmethamphetamine\b",
        r"\bcocaine\b", r"\bsober\w*", r"\bwithdrawals?\b", r"\binpatient\b|\boutpatient\b",
        r"\btreatment (?:center|program|facility|facilities)s?\b", r"\bsubstance\b",
        r"\brelapse\w*", r"\bresidential treatment\b", r"\bmental health\b",
    ],
    "Dumpster & Porta Potty Services": [
        r"\bdumpsters?\b", r"\broll[\s-]?off\b",
        r"\bporta[\s-]?(?:potty|potties|john)\b|\bport[\s-]?a[\s-]?potty\b",
        r"\bportable (?:toilet|restroom|bathroom)s?\b",
        r"\b(?:10|15|20|30|40)[\s-]*(?:yard|yd)s?\b", r"\bdebris\b", r"\bjunk removal\b", r"\bdemolition\b",
    ],
    "Pest Control & Home Services": [
        r"\bpest\w*", r"\btermites?\b", r"\bcockroach\w*|\broach(?:es)?\b", r"\bbed ?bugs?\b",
        r"\brodents?\b|\bmice\b|\brats?\b", r"\bexterminat\w*", r"\broof(?:ing|er|ers)?\b",
        r"\bplumb\w*", r"\bhvac\b|\bair conditioning\b|\bfurnace\b",
        r"\bmov(?:ing company|ers)\b", r"\bants\b",
    ],
    "Insurance (Health / Auto / Home)": [
        r"\bauto insurance\b|\bcar insurance\b", r"\bhome(?:owners?)? insurance\b",
        r"\blife insurance\b", r"\bfinal expense\b|\bburial\b", r"\binsurance quote\b",
        r"\binsurance (?:policy|agent|broker)\b", r"\bmedicare (?:advantage|supplement|part)\b",
        r"\bopen enrollment\b", r"\bhealth insurance plan\b",
    ],
    "Debt Relief & Financial Services": [
        r"\bdebts?\b", r"\bdebt (?:relief|settlement)\b|\bconsolidat\w*", r"\bcredit cards?\b",
        r"\bcreditors?\b|\bcollections?\b", r"\bbankruptcy\b", r"\bmerchant cash\b|\bmca\b",
        r"\bpersonal loans?\b|\bloan\b",
    ],
}
_FAMILY_KEY_RES = {
    family: [re.compile(p, re.IGNORECASE) for p in patterns]
    for family, patterns in FAMILY_KEYWORDS.items()
}


def score_transcript_families(full_transcript):
    """How strongly the transcript points to each campaign family (repeat mentions count up to 2x)."""
    text = str(full_transcript or "")
    scores = {}
    for family, regexes in _FAMILY_KEY_RES.items():
        scores[family] = sum(min(len(rx.findall(text)), 2) for rx in regexes)
    return scores


def detect_transcript_family(full_transcript, campaign_family):
    """Compare what the caller talks about vs the campaign.

    Returns ("other", family)    -> transcript clearly points to a DIFFERENT service
            ("campaign", family) -> transcript clearly matches the campaign service
            (None, None)         -> not clear enough, let the AI decide
    """
    if not full_transcript or not campaign_family or campaign_family not in _FAMILY_KEY_RES:
        return None, None

    scores = score_transcript_families(full_transcript)
    campaign_hits = scores.get(campaign_family, 0)
    others = {k: v for k, v in scores.items() if k != campaign_family}
    best_family, best_hits = max(others.items(), key=lambda kv: kv[1])

    if best_hits >= 3 and best_hits >= 2 * campaign_hits + 1:
        return "other", best_family
    if campaign_hits >= 2 and campaign_hits >= best_hits:
        return "campaign", campaign_family
    return None, None


# ---- Insurance from the caller's own words ----
_GOV_ALT = "|".join(re.escape(t) for t in GOVERNMENT_TERMS)
_NEG_PREFIX = (
    r"(?:no|not|don[\u2019']t have|do not have|doesn[\u2019']t have|does not have|never had|without)"
)
PRIVATE_NEGATED_RE = re.compile(
    r"\b" + _NEG_PREFIX + r"\s+(?:\w+\s+){0,2}?(?:" + "|".join(re.escape(t) for t in PRIVATE_TERMS) + r")\b",
    re.IGNORECASE,
)
_CALLER_PREFIX = (
    r"i have|i[\u2019']ve got|i got|i am on|i[\u2019']m on|i am with|i[\u2019']m with|"
    r"i am covered by|i[\u2019']m covered by|i am under|i[\u2019']m under|my insurance is|"
    r"my insurance would be|my insurance|my coverage is|insurance is|i use|i get|i receive|"
    r"mine is|it[\u2019']s|it is|that[\u2019']s|that is|through|just|yes|yeah|yep|uh|um"
)
_CALLER_GOV_RE = re.compile(
    r"\b(?:" + _CALLER_PREFIX + r")\W+(?:\w+\W+){0,3}?(?:" + _GOV_ALT + r")\b", re.IGNORECASE
)
_CALLER_PRIV_RE = re.compile(
    r"\b(?:" + _CALLER_PREFIX + r")\W+(?:\w+\W+){0,3}?(?:" + "|".join(re.escape(t) for t in PRIVATE_TERMS) + r")\b",
    re.IGNORECASE,
)
_EMPLOYER_RE = re.compile(
    r"\b(?:through|from|with|by)\s+(?:my|his|her|their|our)\s+"
    r"(?:job|work|employer|company|parents?|mom|mother|dad|father|husband|wife|spouse|boyfriend|girlfriend)\b"
    r"|\b(?:private|commercial|employer|company|group)\s+(?:health\s+)?(?:insurance|plan|coverage)\b",
    re.IGNORECASE,
)


def detect_transcript_insurance(full_transcript, timeline_data=None):
    """Read insurance type from what the CALLER says. Returns "YELLOW" (government/state),
    "GREEN" (private/commercial) or None.

    Whisper has no speaker labels, so this ignores sentences that end with "?" (agent questions)
    and ignores negated mentions ("I don't have Medicaid"). Government wins if both appear."""
    if timeline_data:
        units = [str(x.get("line", "")) for x in timeline_data]
    else:
        units = [str(full_transcript or "")]

    sentences = []
    for unit in units:
        sentences.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", unit) if s.strip())

    gov = priv = False
    for sentence in sentences:
        if sentence.endswith("?"):
            continue
        cleaned = GOVERNMENT_NEGATED_RE.sub(" ", sentence)
        cleaned = PRIVATE_NEGATED_RE.sub(" ", cleaned)
        short_answer = len(re.findall(r"[A-Za-z']+", cleaned)) <= 4

        if _CALLER_GOV_RE.search(cleaned) or (short_answer and GOVERNMENT_RE.search(cleaned)):
            gov = True
        if (
            _CALLER_PRIV_RE.search(cleaned)
            or _EMPLOYER_RE.search(cleaned)
            or (short_answer and PRIVATE_RE.search(cleaned))
        ):
            priv = True

    if gov:
        return "YELLOW"
    if priv:
        return "GREEN"
    return None


# ---- Other transcript facts used by the score ----
_US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
    "hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
    "michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    "new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    "rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
    "west virginia|wisconsin|wyoming"
)
_ZIP_RE = re.compile(r"(?<![\d-])\d{5}(?![\d-])")
_STATE_RE = re.compile(r"\b(?:" + _US_STATES + r")\b", re.IGNORECASE)
_LOCATION_PHRASE_RE = re.compile(
    r"\b(?:i live in|i[\u2019']m in|i am in|i[\u2019']m located in|calling from|located in|my zip(?: code)?|zip code|my address)\b",
    re.IGNORECASE,
)

_QUAL_INFO_RES = {
    "Rehab & Addiction Treatment": re.compile(
        r"\binsurance\b|\bcoverage\b|\bmedicaid\b|\bmedicare\b|\bblue cross\b|\baetna\b|\bcigna\b|\bhumana\b|"
        r"\bunited ?healthcare\b|\bself[\s-]?pay\b|\bcash pay\b|\bout of pocket\b|\byears old\b",
        re.IGNORECASE),
    "Dumpster & Porta Potty Services": re.compile(
        r"\b(?:10|15|20|30|40)[\s-]*(?:yard|yd)s?\b|\bdeliver\w*\b|\baddress\b|\bresidential\b|\bcommercial\b|\bconstruction\b",
        re.IGNORECASE),
    "Pest Control & Home Services": re.compile(
        r"\bhome ?owner\b|\bi own\b|\brent(?:er|ing)?\b|\blandlord\b|\bsquare (?:feet|foot)\b|\bsq\.? ?ft\b",
        re.IGNORECASE),
    "Insurance (Health / Auto / Home)": re.compile(
        r"\byears old\b|\bage\b|\bdate of birth\b|\bcurrent(?:ly)? (?:insured|policy|coverage)\b|\bzip\b",
        re.IGNORECASE),
    "Debt Relief & Financial Services": re.compile(
        r"\$\s?\d|\b\d[\d,]*\s?(?:thousand|k|dollars)\b|\bcredit cards?\b|\bunsecured\b|\bi owe\b",
        re.IGNORECASE),
}
_GENERIC_QUAL_INFO_RE = re.compile(r"\binsurance\b|\bzip\b|\bage\b|\$\s?\d|\byears old\b", re.IGNORECASE)

_OUTCOME_RE = re.compile(
    r"\btransfer\w*\b|\bconnect(?:ing)? you\b|\bput you through\b|\bhold\b|\bcall you back\b|\bcalling you back\b|"
    r"\bgive you a call\b|\breach out\b|\bappointment\b|\bschedul\w*\b|\bbook(?:ed|ing)?\b|\bsend you\b|\btext you\b|"
    r"\bemail you\b|\bfollow up\b|\bthank you for calling\b|\bhave a (?:good|great|nice) (?:day|one)\b|"
    r"\bgoodbye\b|\bbye\b|\btake care\b|\bconfirmation\b",
    re.IGNORECASE,
)
_TRANSFER_RE = re.compile(
    r"\btransfer(?:r)?ing you\b|\bconnect(?:ing)? you (?:with|to)\b|\bput you through to\b|\bwarm transfer\b|\blet me transfer you\b",
    re.IGNORECASE,
)
_APPT_HIT_RE = re.compile(
    r"\bappointment\b|\bschedul\w*\b|\bbooked\b|\bbooking\b|\badmission\b|\bintake\b|\bconsultation\b|\breservation\b",
    re.IGNORECASE,
)
_APPT_STRONG_RE = re.compile(
    r"\b(?:appointment|scheduled|booked|booking|delivery|consultation|intake|admission)\b[^.?!]{0,60}?"
    r"\b(?:confirmed?|set|tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.))\b"
    r"|\b(?:i[\u2019']ve|i have|we[\u2019']ve|we have|you[\u2019']re|you are|you[\u2019']ve|you have)\s+(?:been\s+)?(?:scheduled|booked|set up)\b",
    re.IGNORECASE,
)
_AGENT_MARKER_RE = re.compile(
    r"\bthank you for calling\b|\bhow (?:can|may) i (?:help|assist)\b|\bcan i (?:get|have)\b|\bmay i have\b|"
    r"\bwhat(?:[\u2019']s| is) your\b|\bare you looking\b|\bdo you have\b|\blet me (?:transfer|connect|check)\b|"
    r"\bi can help\b|\bone moment\b|\bhold on\b|\bplease hold\b",
    re.IGNORECASE,
)
_CALLER_MARKER_RE = re.compile(
    r"\bi need\b|\bi[\u2019']m looking\b|\bi am looking\b|\bi want\b|\bi[\u2019']d like\b|\bi would like\b|"
    r"\blooking for\b|\bmy (?:son|daughter|husband|wife|brother|sister|mother|father|mom|dad|friend)\b|"
    r"\bhow much\b|\bcan you\b|\bi have\b|\bi don[\u2019']t\b|\byes\b|\byeah\b|\bokay\b",
    re.IGNORECASE,
)


def extract_transcript_signals(full_transcript, timeline_data, campaign_family):
    """Scoring facts measured directly on the full transcript."""
    text = str(full_transcript or "")
    words = re.findall(r"[A-Za-z0-9']+", text.lower())
    word_count = len(words)
    segment_count = len([x for x in (timeline_data or []) if str(x.get("line", "")).strip()])

    tail_words = max(40, int(word_count * 0.30))
    tail = " ".join(words[-tail_words:])

    qual_re = _QUAL_INFO_RES.get(campaign_family, _GENERIC_QUAL_INFO_RE)

    # Whisper has no speakers, so "two-way" = both caller-style and agent-style language
    # in a transcript that is long enough and split into several segments.
    question_count = text.count("?")
    caller_hit = bool(_CALLER_MARKER_RE.search(text))
    agent_hit = bool(_AGENT_MARKER_RE.search(text))
    enough_segments = segment_count >= 3 if timeline_data is not None else True
    two_way = (
        word_count >= 20
        and enough_segments
        and ((caller_hit and agent_hit) or (question_count >= 2 and word_count >= 40))
    )

    scores = score_transcript_families(text)
    family_hits = scores.get(campaign_family, 0) if campaign_family else 0

    service_keyword = ""
    if campaign_family in _FAMILY_KEY_RES:
        for rx in _FAMILY_KEY_RES[campaign_family]:
            match = rx.search(text)
            if match:
                service_keyword = match.group(0).lower().strip()
                break

    return {
        "word_count": word_count,
        "segment_count": segment_count,
        "two_way": bool(two_way),
        "location": bool(_ZIP_RE.search(text) or _STATE_RE.search(text) or _LOCATION_PHRASE_RE.search(text)),
        "qual_info": bool(qual_re.search(text)),
        "outcome": bool(_OUTCOME_RE.search(tail)),
        "appointment_hit": bool(_APPT_HIT_RE.search(text) or _TRANSFER_RE.search(text)),
        "appointment_strong": bool(_APPT_STRONG_RE.search(text) or _TRANSFER_RE.search(text)),
        "family_hits": family_hits,
        "service_keyword": service_keyword,
    }


def _apply_transcript_signals(analysis, full_transcript, timeline_data, campaign_name):
    """Let the transcript decide the score facts. AI fields only fill gaps."""
    if analysis.get("no_voice") or not str(full_transcript or "").strip():
        return analysis

    family = get_campaign_family(campaign_name)
    sig = extract_transcript_signals(full_transcript, timeline_data, family)
    call_type = analysis.get("call_type", "OTHER")

    # Appointment / booking / transfer: must be visible in the transcript.
    ai_appt = bool(analysis.get("appointment_set"))
    analysis["appointment_set"] = bool(sig["appointment_strong"] or (ai_appt and sig["appointment_hit"]))

    analysis["two_way_conversation"] = bool(sig["two_way"]) and call_type != "SILENT / NO RESPONSE"
    analysis["location_or_eligibility_present"] = bool(analysis.get("location_or_eligibility_present") or sig["location"])
    analysis["qualification_info_present"] = bool(analysis.get("qualification_info_present") or sig["qual_info"])
    analysis["clear_outcome"] = bool(analysis.get("clear_outcome") or sig["outcome"])

    if not str(analysis.get("service_requested", "")).strip() and sig["service_keyword"]:
        analysis["service_requested"] = sig["service_keyword"]

    if call_type not in {"WRONG NUMBER", "SPAM / ROBOT", "SILENT / NO RESPONSE"}:
        analysis["relevant_intent"] = bool(analysis.get("relevant_intent") or sig["family_hits"] >= 1)

    analysis["transcript_word_count"] = sig["word_count"]
    return analysis


def get_rehab_insurance_status(analysis):
    """Classify Rehab insurance for deterministic qualification scoring.
    Returns "YELLOW" (government/state), "GREEN" (private/commercial), or None."""
    # The caller's own words in the transcript come first.
    transcript_status = analysis.get("transcript_insurance_status")
    if transcript_status in {"YELLOW", "GREEN"}:
        return transcript_status

    insurance = " ".join(
        str(analysis.get(key) or "") for key in INSURANCE_KEYS
    ).strip()
    if not insurance:
        return None

    # Remove negated mentions first ("does not have Medicaid").
    cleaned = GOVERNMENT_NEGATED_RE.sub(" ", insurance)

    # Government is checked first, so it wins if both match.
    if GOVERNMENT_RE.search(cleaned):
        return "YELLOW"

    if PRIVATE_RE.search(cleaned):
        return "GREEN"

    return None


def get_decision_signal(score, analysis):
    """Convert the numeric score into a practical operations signal."""
    spam = bool(analysis.get("spam_robot"))
    spam_conf = _safe_int(analysis.get("spam_confidence"))
    if analysis.get("no_voice"):
        return "NO VOICE / CHECK RECORDING"
    if spam and spam_conf >= 90:
        return "REJECT / SPAM"
    if analysis.get("call_type") == "WRONG NUMBER":
        return "REJECT / WRONG NUMBER"
    if score >= 90:
        return "KEEP / HIGH VALUE"
    if score >= 80:
        return "KEEP / GOOD"
    if score >= 60:
        return "REVIEW"
    if score >= 40:
        return "LOW QUALITY / REVIEW"
    return "REJECT / INVESTIGATE"


def get_score_basis(analysis):
    """Explain the deterministic score in compact terms (single source: _score_breakdown)."""
    _, items = _score_breakdown(analysis)
    return ", ".join(items) if items else "No positive score factors"


def build_qc_report(analysis):
    """Simple QC report for column K."""
    parts = [
        f"Call Type: {_clean_report_value(analysis.get('call_type'), 'OTHER')}",
        f"Call Type Reason: {_clean_report_value(analysis.get('call_type_reason'))}",
        f"Caller Intent: {_clean_report_value(analysis.get('caller_intent'))}",
        f"Why They Called: {_clean_report_value(analysis.get('why_called'))}",
        f"Treatment/Service Interest: {_clean_report_value(analysis.get('service_requested'))}",
    ]

    if analysis.get("campaign_mismatch"):
        parts.append(
            f"Campaign Mismatch: YES (campaign: {_clean_report_value(analysis.get('campaign_category'))}, "
            f"caller wanted: {_clean_report_value(analysis.get('detected_service_category') or analysis.get('service_requested'))})"
        )
    if str(analysis.get("insurance", "")).strip():
        parts.append(f"Insurance: {_clean_report_value(analysis.get('insurance'))}")
    if str(analysis.get("location", "")).strip():
        parts.append(f"Location: {_clean_report_value(analysis.get('location'))}")
    if str(analysis.get("outcome", "")).strip():
        parts.append(f"Outcome: {_clean_report_value(analysis.get('outcome'))}")
    if analysis.get("appointment_set"):
        parts.append("Appointment/Booking: YES")

    parts.extend([
        f"Spam/Robot: {'YES' if analysis.get('spam_robot') else 'NO'}",
        f"Spam Confidence: {_safe_int(analysis.get('spam_confidence'))}%",
        f"QC Issue: {_clean_report_value(analysis.get('qc_issue'))}",
    ])

    return " | ".join(parts)


# ------------------------------------------------------------
# SCORING RULES
# These are the BUILT-IN DEFAULTS. You normally do NOT edit them here:
# change them in the app -> sidebar "Campaign Rules" -> "Scoring" (all campaigns)
# or inside a campaign's own "Score settings" (only that campaign).
# ------------------------------------------------------------
SCORE_WEIGHT_FIELDS = [
    # key, label, default, max, group, can be overridden per campaign
    ("q_base_clear",   "Base points (status QUALIFIED)",          60, 100, "Qualified calls", True),
    ("q_base_unclear", "Base points (status not clear)",          45, 100, "Qualified calls", True),
    ("q_service",      "+ Service requested",                     10, 100, "Qualified calls", True),
    ("q_location",     "+ Location / eligibility",                 8, 100, "Qualified calls", True),
    ("q_qualinfo",     "+ Qualification info",                     6, 100, "Qualified calls", True),
    ("q_outcome",      "+ Clear outcome",                          8, 100, "Qualified calls", True),
    ("q_twoway",       "+ Two-way conversation",                   4, 100, "Qualified calls", True),
    ("q_appointment",  "+ Appointment / booking / transfer",       4, 100, "Qualified calls", True),
    ("q_qc_penalty",   "- Penalty when a QC issue exists",        10, 100, "Qualified calls", True),

    ("nq_base",        "Base points",                             20, 100, "Non-qualified calls", True),
    ("nq_intent",      "+ Relevant intent",                        5, 100, "Non-qualified calls", True),
    ("nq_service",     "+ Service requested",                      8, 100, "Non-qualified calls", True),
    ("nq_qualinfo",    "+ Qualification info",                     7, 100, "Non-qualified calls", True),
    ("nq_location",    "+ Location",                               5, 100, "Non-qualified calls", True),
    ("nq_outcome",     "+ Clear outcome",                          5, 100, "Non-qualified calls", True),
    ("nq_twoway",      "+ Two-way conversation",                   5, 100, "Non-qualified calls", True),

    ("o_intent",       "+ Relevant intent",                       10, 49, "Other call types (max 49)", True),
    ("o_service",      "+ Service requested",                      8, 49, "Other call types (max 49)", True),
    ("o_twoway",       "+ Two-way conversation",                   8, 49, "Other call types (max 49)", True),
    ("o_outcome",      "+ Clear outcome",                          5, 49, "Other call types (max 49)", True),
    ("o_qualinfo",     "+ Qualification info",                     4, 49, "Other call types (max 49)", True),

    ("non_qualified_max",     "Non-qualified: highest score",                    60, 100, "Caps", True),
    ("qualified_unclear_max", "Qualified but status not clear: highest score",   79, 100, "Caps", True),
    ("appointment_max",       "Qualified without appointment/booking: highest",  95, 100, "Caps", True),
    ("government_cap",        "Government insurance: highest score",             60, 100, "Caps", True),

    ("no_voice_score",        "No voice / no text in recording (fixed)",         30, 49, "Hard rules (all campaigns, max 49)", False),
    ("silent_score",          "Silent / no response call type (fixed)",          30, 49, "Hard rules (all campaigns, max 49)", False),
    ("spam_confident_score",  "Confirmed spam, confidence 90+ (fixed)",           5, 49, "Hard rules (all campaigns, max 49)", False),
    ("spam_max_score",        "Other spam: highest score",                       25, 49, "Hard rules (all campaigns, max 49)", False),
    ("wrong_number_max",      "Wrong number / campaign mismatch: highest score", 30, 49, "Hard rules (all campaigns, max 49)", False),
    ("other_type_max",        "Any call type except Qualified / Non-qualified",  49, 49, "Hard rules (all campaigns, max 49)", False),
]
DEFAULT_WEIGHTS = {f[0]: f[2] for f in SCORE_WEIGHT_FIELDS}


def _qc_cfg():
    return globals().get("QC_CONFIG") or {}


def get_campaign_cfg(campaign_key):
    return (_qc_cfg().get("campaigns") or {}).get(str(campaign_key or "").strip(), {})


def campaign_flag(campaign_key, flag, default=False):
    """Per-campaign on/off rule (for example block_government_insurance)."""
    camp = get_campaign_cfg(campaign_key)
    return bool(camp.get(flag, default)) if camp else default


def get_effective_weights(campaign_key):
    """Global score numbers, replaced by the campaign's own values where it has overrides."""
    weights = dict(DEFAULT_WEIGHTS)
    weights.update((_qc_cfg().get("global") or {}).get("weights") or {})
    camp = get_campaign_cfg(campaign_key)
    for key, value in (camp.get("score_overrides") or {}).items():
        if key in weights:
            weights[key] = value
    return weights


def get_campaign_focus(campaign_key):
    camp = get_campaign_cfg(campaign_key)
    focus = str(camp.get("focus", "") or "").strip() if camp else ""
    return focus or (
        "Focus on the caller's reason, requested service/information, important "
        "qualification details, location/eligibility, and outcome."
    )


def _score_breakdown(analysis):
    """Return (final_score, list_of_reasons). AI supplies facts; Python supplies the score.
    All numbers come from the editable settings (Campaign Rules page).

    Tiers:
      QUALIFIED      base points + completeness bonuses, up to 100
      NON-QUALIFIED  base points + bonuses, capped (default 60)
      everything else (INFO ONLY / OTHER / WRONG NUMBER / SPAM / SILENT): always under 50
    Hard rules: no voice = fixed, silent = fixed, wrong number capped, spam capped.
    """
    call_type = str(analysis.get("call_type", "OTHER") or "OTHER").strip().upper()
    status = str(analysis.get("qualification_status", "NOT CLEAR") or "NOT CLEAR").strip().upper()
    spam_confidence = _safe_int(analysis.get("spam_confidence"))
    spam = bool(analysis.get("spam_robot")) or call_type == "SPAM / ROBOT"
    campaign_key = str(analysis.get("campaign_category", "") or "").strip()

    weights = get_effective_weights(campaign_key)

    def W(key):
        try:
            return int(weights.get(key, DEFAULT_WEIGHTS[key]))
        except (TypeError, ValueError):
            return int(DEFAULT_WEIGHTS[key])

    def under50(value):
        return min(49, value)

    # ---- Fixed-score rules ----
    if analysis.get("no_voice"):
        return under50(W("no_voice_score")), [f"No voice detected = {under50(W('no_voice_score'))}"]
    if spam and spam_confidence >= 90:
        return under50(W("spam_confident_score")), [f"Confirmed spam/robot = {under50(W('spam_confident_score'))}"]
    if call_type == "SILENT / NO RESPONSE":
        return under50(W("silent_score")), [f"Silent / no response = {under50(W('silent_score'))}"]

    score = 0
    items = []

    def add(points, label):
        nonlocal score
        score += points
        items.append(f"{label} +{points}")

    has_service = bool(str(analysis.get("service_requested", "")).strip())

    # ---- Tier points ----
    if call_type == "QUALIFIED" and status != "NON-QUALIFIED":
        tier = "QUALIFIED"
        if status == "QUALIFIED":
            add(W("q_base_clear"), "Qualified base")
        else:
            add(W("q_base_unclear"), "Qualified base (status not clear)")
        if has_service:
            add(W("q_service"), "Service")
        if analysis.get("location_or_eligibility_present"):
            add(W("q_location"), "Location/eligibility")
        if analysis.get("qualification_info_present"):
            add(W("q_qualinfo"), "Qualification info")
        if analysis.get("clear_outcome"):
            add(W("q_outcome"), "Outcome")
        if analysis.get("two_way_conversation"):
            add(W("q_twoway"), "2-way conversation")
        if analysis.get("appointment_set"):
            add(W("q_appointment"), "Appointment/booking")
        if str(analysis.get("qc_issue", "")).strip():
            score -= W("q_qc_penalty")
            items.append(f"QC issue -{W('q_qc_penalty')}")
    elif call_type in {"QUALIFIED", "NON-QUALIFIED"}:
        tier = "NON-QUALIFIED"
        add(W("nq_base"), "Non-qualified base")
        if analysis.get("relevant_intent"):
            add(W("nq_intent"), "Relevant intent")
        if has_service:
            add(W("nq_service"), "Service")
        if analysis.get("qualification_info_present"):
            add(W("nq_qualinfo"), "Qualification info")
        if analysis.get("location_or_eligibility_present"):
            add(W("nq_location"), "Location")
        if analysis.get("clear_outcome"):
            add(W("nq_outcome"), "Outcome")
        if analysis.get("two_way_conversation"):
            add(W("nq_twoway"), "2-way conversation")
    else:
        tier = "OTHER"
        if analysis.get("relevant_intent"):
            add(W("o_intent"), "Relevant intent")
        if has_service:
            add(W("o_service"), "Service")
        if analysis.get("two_way_conversation"):
            add(W("o_twoway"), "2-way conversation")
        if analysis.get("clear_outcome"):
            add(W("o_outcome"), "Outcome")
        if analysis.get("qualification_info_present"):
            add(W("o_qualinfo"), "Qualification info")

    # ---- Caps (lowest cap wins) ----
    caps = []
    if spam:
        caps.append((under50(W("spam_max_score")), "spam"))
    elif call_type == "WRONG NUMBER":
        caps.append((under50(W("wrong_number_max")), "wrong number"))
    elif tier == "OTHER":
        caps.append((under50(W("other_type_max")), f"call type {call_type}"))
    elif tier == "NON-QUALIFIED":
        caps.append((W("non_qualified_max"), "non-qualified"))
    elif status != "QUALIFIED":
        caps.append((W("qualified_unclear_max"), "qualification not clear"))

    # Campaign rule: a qualified call without an appointment/booking can never reach the top score.
    if tier == "QUALIFIED" and not analysis.get("appointment_set", False) \
            and campaign_flag(campaign_key, "appointment_cap_enabled", True):
        caps.append((W("appointment_max"), "no appointment/booking"))

    # Campaign rule: government / state insurance is never fully qualified.
    if campaign_flag(campaign_key, "block_government_insurance") \
            and get_rehab_insurance_status(analysis) == "YELLOW":
        caps.append((W("government_cap"), "government insurance"))

    final = max(0, score)
    for cap_value, cap_reason in caps:
        if final > cap_value:
            final = cap_value
            items.append(f"Capped at {cap_value} ({cap_reason})")

    return max(0, min(100, final)), items


def calculate_call_quality_score(analysis):
    """Deterministic 0-100 score. See _score_breakdown for the rules."""
    return _score_breakdown(analysis)[0]


SCORE_COLORS = {
    "excellent": {"red": 0.56, "green": 0.83, "blue": 0.60},
    "good": {"red": 0.75, "green": 0.93, "blue": 0.78},
    "yellow": {"red": 1.00, "green": 0.93, "blue": 0.60},
    "orange": {"red": 1.00, "green": 0.78, "blue": 0.50},
    "poor": {"red": 1.00, "green": 0.60, "blue": 0.60},
    "spam": {"red": 1.00, "green": 0.35, "blue": 0.35},
}


def get_score_color(score, analysis):
    if analysis.get("spam_robot") and _safe_int(analysis.get("spam_confidence")) >= 90:
        return SCORE_COLORS["spam"]
    if score >= 95:
        return SCORE_COLORS["excellent"]
    if score >= 80:
        return SCORE_COLORS["good"]
    if score >= 60:
        return SCORE_COLORS["yellow"]
    if score >= 40:
        return SCORE_COLORS["orange"]
    return SCORE_COLORS["poor"]


def apply_row_score_color(worksheet, row_number, score, analysis, special_columns=None):
    """Apply the final score color to the ENTIRE A:L row. Score is authoritative."""
    color = get_score_color(score, analysis)
    worksheet.format(
        f"A{row_number}:L{row_number}",
        {"backgroundColor": color}
    )


def save_uploaded_audio(uploaded_file):
    ext = Path(uploaded_file.name).suffix.lower()
    if ext not in {".mp3", ".wav"}:
        raise ValueError("Only MP3 and WAV are supported.")

    safe_name = f"{uuid.uuid4().hex}{ext}"
    path = UPLOAD_DIR / safe_name

    with open(path, "wb") as output:
        output.write(uploaded_file.getbuffer())

    try:
        sound = AudioSegment.from_file(path)
        duration_sec = len(sound) / 1000.0
    except Exception:
        duration_sec = 60.0

    st.session_state.source_name = uploaded_file.name
    st.session_state.file_path = str(path)
    st.session_state.duration_sec = duration_sec
    st.session_state.est_proc_sec = max(1.0, round(duration_sec * 0.008, 1))
    st.session_state.source_type = "Uploaded file"
    st.session_state.transcribed = False
    st.session_state.transcript = []
    st.session_state.full_text = ""
    st.session_state.short_topic = "No transcription available yet."
    st.session_state.detailed_summary = "No summary generated yet."
    st.session_state.status = "Audio loaded and ready for processing."

def load_audio_url(url):
    url = url.strip()
    if not url:
        raise ValueError("URL is required.")

    filename = url.split("/")[-1].split("?")[0] or "web_audio.mp3"
    ext = Path(filename).suffix.lower()
    safe_name = f"{uuid.uuid4().hex}{ext if ext in {'.mp3', '.wav'} else '.mp3'}"
    path = UPLOAD_DIR / safe_name

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as response:
        with open(path, "wb") as out_file:
            out_file.write(response.read())

    sound = AudioSegment.from_file(path)
    duration_sec = len(sound) / 1000.0

    st.session_state.source_name = filename
    st.session_state.file_path = str(path)
    st.session_state.duration_sec = duration_sec
    st.session_state.est_proc_sec = max(1.0, round(duration_sec * 0.008, 1))
    st.session_state.source_type = "Recording URL"
    st.session_state.transcribed = False
    st.session_state.transcript = []
    st.session_state.full_text = ""
    st.session_state.short_topic = "No transcription available yet."
    st.session_state.detailed_summary = "No summary generated yet."
    st.session_state.status = "Recording link loaded and ready."


def get_google_client():
    """Connect to Google Sheets using the local service account or Streamlit Secrets."""
    if os.path.exists("service_account.json"):
        return gspread.service_account(filename="service_account.json")

    try:
        return gspread.service_account_from_dict(dict(st.secrets["gcp_service_account"]))
    except Exception as exc:
        raise RuntimeError(
            "Google Sheets credentials not found. Add service_account.json or gcp_service_account to Streamlit Secrets."
        ) from exc


# ============================================================
# EDITABLE QC CONFIG
# Campaign QC note rules, keywords, scoring and spam settings are stored in
# qc_config.json (+ backup in the Google Sheet tab "QC_Config") and edited from the
# app: sidebar -> "Campaign Rules" (admin only). Everything below is read on every
# run, so saved changes apply to the next call processed without a restart.
# ============================================================
import copy

QC_CONFIG_PATH = Path("qc_config.json")
QC_CONFIG_PREVIOUS_PATH = Path("qc_config.previous.json")
QC_CONFIG_SHEET_NAME = "Ringba to Sheet QC"
QC_CONFIG_TAB = "QC_Config"

# Snapshot of the built-in (in-code) values, used for "Reset to defaults".
_BUILTIN_CAMPAIGN_QC = dict(CAMPAIGN_QC_QUESTIONS)
_BUILTIN_DEFAULT_QC = DEFAULT_QC_QUESTIONS
_BUILTIN_SUMMARY_STYLE = SUMMARY_STYLE_RULES
_BUILTIN_GOV_TERMS = list(GOVERNMENT_TERMS)
_BUILTIN_PRIVATE_TERMS = list(PRIVATE_TERMS)

_GENERIC_FOCUS = (
    "Focus on the caller's reason, requested service/information, important "
    "qualification details, location/eligibility, and outcome."
)

# Built-in details for the original campaigns (everything is editable in the app).
_BUILTIN_CAMPAIGN_DETAILS = {
    "Rehab & Addiction Treatment": {
        "match_priority": 10,
        "name_keywords": ["rehab", "addiction", "mental health", "substance", "treatment"],
        "focus": (
            "Focus on treatment/service requested, caller reason, insurance, location, "
            "qualification, and outcome. Medicaid/Medicare/state/government/public insurance "
            "is NON-QUALIFIED for this network rule; private/commercial insurance is a positive signal."
        ),
        "service_keywords": [
            "rehab*", "detox*", "addict*", "alcohol*", "drug", "opioid", "heroin", "fentanyl",
            "meth", "methamphetamine", "cocaine", "sober*", "withdrawal", "inpatient", "outpatient",
            "treatment center", "treatment program", "treatment facility", "substance", "relapse*",
            "residential treatment", "mental health",
        ],
        "qual_keywords": [
            "insurance", "coverage", "medicaid", "medicare", "blue cross", "aetna", "cigna",
            "humana", "united healthcare", "self pay", "cash pay", "out of pocket", "years old",
        ],
        "block_government_insurance": True,
    },
    "Dumpster & Porta Potty Services": {
        "match_priority": 20,
        "name_keywords": ["dumpster", "porta potty", "portable toilet"],
        "focus": "Focus on dumpster/porta-potty service, size/type, use, price, location, delivery date, and booking.",
        "service_keywords": [
            "dumpster", "roll off", "porta potty", "porta john", "port a potty", "portable toilet",
            "portable restroom", "portable bathroom", "10 yard", "15 yard", "20 yard", "30 yard",
            "40 yard", "debris", "junk removal", "demolition",
        ],
        "qual_keywords": [
            "10 yard", "15 yard", "20 yard", "30 yard", "40 yard", "deliver*", "address",
            "residential", "commercial", "construction",
        ],
    },
    "Pest Control & Home Services": {
        "match_priority": 30,
        "name_keywords": ["pest", "roofing", "roof", "home service", "plumbing", "hvac", "moving"],
        "focus": "Focus on the home/pest problem, service requested, homeowner/renter status, location, quote, appointment, and outcome.",
        "service_keywords": [
            "pest*", "termite", "cockroach*", "roach", "bed bug", "rodent", "mice", "rat",
            "exterminat*", "roof*", "plumb*", "hvac", "air conditioning", "furnace",
            "moving company", "mover", "ant",
        ],
        "qual_keywords": ["homeowner", "i own", "rent*", "landlord", "square feet", "sq ft"],
    },
    "Insurance (Health / Auto / Home)": {
        "match_priority": 40,
        "name_keywords": ["insurance", "medicare", "medicaid", "auto insurance", "home insurance", "final expense", "health insurance"],
        "focus": "Focus on coverage type, current policy situation, quote/new policy/existing policy, eligibility, and outcome.",
        "service_keywords": [
            "auto insurance", "car insurance", "home insurance", "homeowners insurance",
            "homeowner insurance", "life insurance", "final expense", "burial", "insurance quote",
            "insurance policy", "insurance agent", "insurance broker", "medicare advantage",
            "medicare supplement", "medicare part", "open enrollment", "health insurance plan",
        ],
        "qual_keywords": ["years old", "age", "date of birth", "current policy", "current coverage", "zip"],
    },
    "Debt Relief & Financial Services": {
        "match_priority": 50,
        "name_keywords": ["debt", "debt relief", "settlement", "financial", "loan", "mca"],
        "focus": "Focus on debt/financial need, debt amount/type when stated, requested help, qualification, transfer/consultation, and outcome.",
        "service_keywords": [
            "debt", "debt relief", "debt settlement", "consolidat*", "credit card", "creditor",
            "collection", "bankruptcy", "merchant cash", "mca", "personal loan", "loan",
        ],
        "qual_keywords": [r"regex:\$\s?\d", "thousand", "dollars", "credit card", "unsecured", "i owe"],
    },
}


def _blank_campaign():
    return {
        "match_priority": 100,
        "name_keywords": [],
        "qc_note_rules": _BUILTIN_DEFAULT_QC,
        "focus": _GENERIC_FOCUS,
        "service_keywords": [],
        "qual_keywords": [],
        "block_government_insurance": False,
        "appointment_cap_enabled": True,
        "score_overrides": {},
    }


def _default_qc_config():
    campaigns = {}
    for name, rules in _BUILTIN_CAMPAIGN_QC.items():
        camp = _blank_campaign()
        camp.update(copy.deepcopy(_BUILTIN_CAMPAIGN_DETAILS.get(name, {})))
        camp["qc_note_rules"] = rules
        if not camp["name_keywords"]:
            camp["name_keywords"] = [name.lower()]
        campaigns[name] = camp
    return {
        "meta": {"version": 2, "updated_at": "", "updated_by": ""},
        "global": {
            "weights": dict(DEFAULT_WEIGHTS),
            "spam": {"yelp_yellow_is_spam": True, "strict_yellow_any": False, "extra_terms": []},
            "voice": {"no_voice_min_words": 3, "min_audio_seconds": 1.0},
            "government_terms": list(_BUILTIN_GOV_TERMS),
            "private_terms": list(_BUILTIN_PRIVATE_TERMS),
            "summary_style_rules": _BUILTIN_SUMMARY_STYLE,
            "default_qc_questions": _BUILTIN_DEFAULT_QC,
        },
        "campaigns": campaigns,
    }


def _merge_config(saved, defaults):
    """Saved settings on top of the built-in defaults (new settings added later still get a value)."""
    cfg = copy.deepcopy(defaults)
    if not isinstance(saved, dict):
        return cfg
    cfg["meta"].update(saved.get("meta") or {})
    for key, value in (saved.get("global") or {}).items():
        if key in ("weights", "spam", "voice") and isinstance(value, dict):
            cfg["global"][key].update(value)
        else:
            cfg["global"][key] = value
    if isinstance(saved.get("campaigns"), dict) and saved["campaigns"]:
        cfg["campaigns"] = {}
        for name, camp in saved["campaigns"].items():
            merged = _blank_campaign()
            merged["qc_note_rules"] = cfg["global"].get("default_qc_questions", _BUILTIN_DEFAULT_QC)
            merged.update(camp or {})
            cfg["campaigns"][str(name).strip()] = merged
    return cfg


# ---- keyword helpers ----
def _keyword_to_regex(entry):
    """Plain word/phrase -> regex.  'rehab*' = starts with rehab.  'regex:...' = raw regex."""
    text = str(entry or "").strip()
    if not text:
        return None
    if text.lower().startswith("regex:"):
        return text[6:].strip() or None
    prefix_match = text.endswith("*")
    text = text.rstrip("*").strip()
    parts = [p for p in re.split(r"[\s-]+", text) if p]
    if not parts:
        return None
    body = r"[\s-]?".join(re.escape(p) for p in parts)
    if prefix_match:
        return r"\b" + body + r"\w*"
    return r"\b" + body + r"(?:s|es)?\b"


def _valid_patterns(entries):
    patterns = []
    for entry in entries or []:
        pattern = _keyword_to_regex(entry)
        if not pattern:
            continue
        try:
            re.compile(pattern)
        except re.error:
            continue
        patterns.append(pattern)
    return patterns


def _alt(terms):
    return "|".join(re.escape(str(t)) for t in terms if str(t).strip()) or "(?!x)x"


def _rebuild_insurance_regexes():
    global GOVERNMENT_RE, PRIVATE_RE, GOVERNMENT_NEGATED_RE, PRIVATE_NEGATED_RE
    global _GOV_ALT, _CALLER_GOV_RE, _CALLER_PRIV_RE
    GOVERNMENT_RE = _compile(GOVERNMENT_TERMS) if GOVERNMENT_TERMS else re.compile(r"(?!x)x")
    PRIVATE_RE = _compile(PRIVATE_TERMS) if PRIVATE_TERMS else re.compile(r"(?!x)x")
    _GOV_ALT = _alt(GOVERNMENT_TERMS)
    private_alt = _alt(PRIVATE_TERMS)
    GOVERNMENT_NEGATED_RE = re.compile(
        r"\b" + _NEG_PREFIX + r"\s+(?:\w+\s+){0,2}?(?:" + _GOV_ALT + r")\b", re.IGNORECASE)
    PRIVATE_NEGATED_RE = re.compile(
        r"\b" + _NEG_PREFIX + r"\s+(?:\w+\s+){0,2}?(?:" + private_alt + r")\b", re.IGNORECASE)
    _CALLER_GOV_RE = re.compile(
        r"\b(?:" + _CALLER_PREFIX + r")\W+(?:\w+\W+){0,3}?(?:" + _GOV_ALT + r")\b", re.IGNORECASE)
    _CALLER_PRIV_RE = re.compile(
        r"\b(?:" + _CALLER_PREFIX + r")\W+(?:\w+\W+){0,3}?(?:" + private_alt + r")\b", re.IGNORECASE)


def apply_qc_config(cfg):
    """Push the settings into the running app (rules, scoring, keywords, spam, summaries)."""
    global QC_CONFIG, NO_VOICE_MIN_WORDS, MIN_AUDIO_SECONDS_FOR_TRANSCRIPTION
    global STRICT_YELLOW_ANY, SPAM_YELP_YELLOW_ENABLED, EXTRA_SPAM_RES
    global SUMMARY_STYLE_RULES, DEFAULT_QC_QUESTIONS

    QC_CONFIG = cfg
    g = cfg["global"]

    NO_VOICE_MIN_WORDS = int(g["voice"].get("no_voice_min_words", 3))
    MIN_AUDIO_SECONDS_FOR_TRANSCRIPTION = float(g["voice"].get("min_audio_seconds", 1.0))
    STRICT_YELLOW_ANY = bool(g["spam"].get("strict_yellow_any", False))
    SPAM_YELP_YELLOW_ENABLED = bool(g["spam"].get("yelp_yellow_is_spam", True))
    EXTRA_SPAM_RES = []
    for pattern in _valid_patterns(g["spam"].get("extra_terms")):
        EXTRA_SPAM_RES.append(re.compile(pattern, re.IGNORECASE))

    SUMMARY_STYLE_RULES = g.get("summary_style_rules") or _BUILTIN_SUMMARY_STYLE
    DEFAULT_QC_QUESTIONS = g.get("default_qc_questions") or _BUILTIN_DEFAULT_QC

    CAMPAIGN_QC_QUESTIONS.clear()
    FAMILY_KEYWORDS.clear()
    _FAMILY_KEY_RES.clear()
    _QUAL_INFO_RES.clear()
    for name, camp in cfg["campaigns"].items():
        CAMPAIGN_QC_QUESTIONS[name] = camp.get("qc_note_rules") or DEFAULT_QC_QUESTIONS
        patterns = _valid_patterns(camp.get("service_keywords"))
        FAMILY_KEYWORDS[name] = patterns
        _FAMILY_KEY_RES[name] = [re.compile(p, re.IGNORECASE) for p in patterns]
        qual_patterns = _valid_patterns(camp.get("qual_keywords"))
        if qual_patterns:
            _QUAL_INFO_RES[name] = re.compile("|".join(f"(?:{p})" for p in qual_patterns), re.IGNORECASE)

    GOVERNMENT_TERMS[:] = [str(t).strip() for t in g.get("government_terms", []) if str(t).strip()]
    PRIVATE_TERMS[:] = [str(t).strip() for t in g.get("private_terms", []) if str(t).strip()]
    _rebuild_insurance_regexes()


def validate_qc_config(cfg):
    errors = []
    campaigns = cfg.get("campaigns") or {}
    if not campaigns:
        errors.append("At least one campaign is required.")
    seen = set()
    for name, camp in campaigns.items():
        if not str(name).strip():
            errors.append("A campaign has an empty name.")
            continue
        low = str(name).strip().lower()
        if low in seen:
            errors.append(f"Duplicate campaign name: {name}")
        seen.add(low)
        if not str(camp.get("qc_note_rules", "")).strip():
            errors.append(f"'{name}': QC note rules cannot be empty.")
        for field in ("service_keywords", "qual_keywords"):
            for entry in camp.get(field) or []:
                pattern = _keyword_to_regex(entry)
                if not pattern:
                    continue
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(f"'{name}' {field}: invalid pattern '{entry}' ({exc})")
    limits = {f[0]: f[3] for f in SCORE_WEIGHT_FIELDS}
    for key, value in (cfg["global"].get("weights") or {}).items():
        if key in limits and not (0 <= float(value) <= limits[key]):
            errors.append(f"Score '{key}' must be between 0 and {limits[key]}.")
    for name, camp in campaigns.items():
        for key, value in (camp.get("score_overrides") or {}).items():
            if key in limits and not (0 <= float(value) <= limits[key]):
                errors.append(f"'{name}': score '{key}' must be between 0 and {limits[key]}.")
    for entry in cfg["global"]["spam"].get("extra_terms") or []:
        pattern = _keyword_to_regex(entry)
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"Extra spam term '{entry}' is invalid ({exc})")
    return errors


# ---- storage: local file + Google Sheet backup ----
def _save_qc_config_to_sheet(cfg):
    try:
        sheet = get_google_client().open(QC_CONFIG_SHEET_NAME)
        try:
            ws = sheet.worksheet(QC_CONFIG_TAB)
        except Exception:
            ws = sheet.add_worksheet(title=QC_CONFIG_TAB, rows=60, cols=2)
        payload = json.dumps(cfg, ensure_ascii=False)
        chunks = [payload[i:i + 40000] for i in range(0, len(payload), 40000)] or [""]
        ws.clear()
        ws.update(values=[[chunk] for chunk in chunks], range_name="A1")
        return True, f"Backed up to Google Sheet tab '{QC_CONFIG_TAB}'."
    except Exception as exc:
        return False, f"Saved on the server, but the Google Sheet backup failed ({exc})."


def _load_qc_config_from_sheet():
    try:
        ws = get_google_client().open(QC_CONFIG_SHEET_NAME).worksheet(QC_CONFIG_TAB)
        text = "".join(ws.col_values(1))
        return json.loads(text) if text.strip() else None
    except Exception:
        return None


def save_qc_config(cfg, updated_by=""):
    """Validate-free save: write file (keeping the previous version), back up to the sheet, apply."""
    cfg["meta"]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cfg["meta"]["updated_by"] = updated_by
    if QC_CONFIG_PATH.exists():
        try:
            QC_CONFIG_PREVIOUS_PATH.write_text(QC_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
    tmp_path = QC_CONFIG_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(QC_CONFIG_PATH)
    sheet_ok, sheet_msg = _save_qc_config_to_sheet(cfg)
    apply_qc_config(cfg)
    return sheet_ok, sheet_msg


def _bootstrap_qc_config():
    defaults = _default_qc_config()
    data = None
    if QC_CONFIG_PATH.exists():
        try:
            data = json.loads(QC_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = None
    if data is None:
        try:
            already_tried = st.session_state.get("qc_sheet_restore_tried", False)
            st.session_state["qc_sheet_restore_tried"] = True
        except Exception:
            already_tried = True
        if not already_tried:
            data = _load_qc_config_from_sheet()
            if data:
                try:
                    QC_CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
    return _merge_config(data, defaults) if data else defaults


QC_CONFIG = _bootstrap_qc_config()
apply_qc_config(QC_CONFIG)


# ============================================================
# CAMPAIGN RULES PAGE (admin)
# ============================================================
def _lines(text):
    out, seen = [], set()
    for line in str(text or "").splitlines():
        item = line.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def _flash(kind, message):
    st.session_state["cr_flash"] = (kind, message)


def _commit_qc_config(cfg, label="Saved"):
    errors = validate_qc_config(cfg)
    if errors:
        for err in errors:
            st.error(err)
        return False
    who = (st.session_state.get("logged_in_user") or {}).get("email", "")
    sheet_ok, sheet_msg = save_qc_config(cfg, updated_by=who)
    _flash("success" if sheet_ok else "warning", f"{label}. {sheet_msg}")
    return True


def _weight_inputs(cfg, ver, key_prefix, values, only_overridable=False, camp_name=None):
    """Number inputs grouped like the score table. Returns {key: value}."""
    if camp_name:
        base = get_effective_weights(camp_name)
    else:
        base = cfg["global"]["weights"]
    groups = []
    for field in SCORE_WEIGHT_FIELDS:
        if only_overridable and not field[5]:
            continue
        if field[4] not in groups:
            groups.append(field[4])
    result = {}
    for group in groups:
        st.markdown(f"**{group}**")
        cols = st.columns(3)
        group_fields = [f for f in SCORE_WEIGHT_FIELDS if f[4] == group and (f[5] or not only_overridable)]
        for i, (key, label, default, mx, _grp, _ov) in enumerate(group_fields):
            with cols[i % 3]:
                current = int(base.get(key, default))
                result[key] = st.number_input(
                    label, min_value=0, max_value=int(mx), value=min(current, int(mx)), step=1,
                    key=f"cr_{ver}_{key_prefix}_{key}",
                )
    return result


def _cr_campaign_editor(cfg, name, ver):
    camp = cfg["campaigns"][name]
    kp = re.sub(r"\W+", "_", name)
    global_weights = cfg["global"]["weights"]

    with st.form(f"cr_form_{ver}_{kp}"):
        st.markdown(f"### {name}")
        c1, c2 = st.columns(2)
        with c1:
            name_kw = st.text_area(
                "Campaign name keywords (one per line)",
                value="\n".join(camp.get("name_keywords", [])), height=150,
                help="If the campaign name from Ringba contains any of these words, this campaign's rules are used.",
                key=f"cr_{ver}_{kp}_namekw")
            priority = st.number_input(
                "Match priority (lower = checked first)", min_value=1, max_value=999,
                value=int(camp.get("match_priority", 100)), step=1, key=f"cr_{ver}_{kp}_prio",
                help="Use this when two campaigns could match the same Ringba campaign name.")
        with c2:
            focus = st.text_area(
                "Campaign focus (1-2 sentences for the AI analysis)",
                value=camp.get("focus", ""), height=150, key=f"cr_{ver}_{kp}_focus")

        qc_rules = st.text_area(
            "QC note rules - what the summary must include and avoid for this campaign",
            value=camp.get("qc_note_rules", ""), height=420, key=f"cr_{ver}_{kp}_qc",
            help="These rules decide what goes into the call summary for this campaign.")

        c3, c4 = st.columns(2)
        with c3:
            service_kw = st.text_area(
                "Service keywords (one per line)",
                value="\n".join(camp.get("service_keywords", [])), height=200, key=f"cr_{ver}_{kp}_svc",
                help="Words a caller says when they want THIS service. Used to catch wrong numbers "
                     "(for example a rehab caller on a dumpster campaign). End a word with * to match "
                     "any ending (rehab* = rehab, rehabilitation). Start a line with regex: for a raw pattern.")
        with c4:
            qual_kw = st.text_area(
                "Qualification keywords (one per line)",
                value="\n".join(camp.get("qual_keywords", [])), height=200, key=f"cr_{ver}_{kp}_qual",
                help="Words that show the caller gave qualification information (insurance, size, budget, age...). "
                     "Same format as service keywords.")

        c5, c6 = st.columns(2)
        with c5:
            block_gov = st.checkbox(
                "Government / state insurance is NOT qualified (blocks Qualified)",
                value=bool(camp.get("block_government_insurance", False)), key=f"cr_{ver}_{kp}_gov")
        with c6:
            appt_cap = st.checkbox(
                "Qualified calls without an appointment/booking cannot get the top score",
                value=bool(camp.get("appointment_cap_enabled", True)), key=f"cr_{ver}_{kp}_appt")

        st.markdown("#### Score settings for this campaign")
        st.caption("Pre-filled with the current values. Only numbers you change are saved as this campaign's own; "
                   "the rest keep following the global Scoring tab.")
        values = _weight_inputs(cfg, ver, kp, None, only_overridable=True, camp_name=name)

        submitted = st.form_submit_button("💾 Save campaign", type="primary")

    if submitted:
        camp["name_keywords"] = _lines(name_kw)
        camp["match_priority"] = int(priority)
        camp["focus"] = focus.strip()
        camp["qc_note_rules"] = qc_rules.strip()
        camp["service_keywords"] = _lines(service_kw)
        camp["qual_keywords"] = _lines(qual_kw)
        camp["block_government_insurance"] = bool(block_gov)
        camp["appointment_cap_enabled"] = bool(appt_cap)
        camp["score_overrides"] = {
            k: int(v) for k, v in values.items() if int(v) != int(global_weights.get(k, DEFAULT_WEIGHTS[k]))
        }
        if _commit_qc_config(cfg, f"Saved campaign '{name}'"):
            st.rerun()

    b1, b2, b3 = st.columns([1, 1, 2])
    with b1:
        if st.button("📄 Duplicate", key=f"cr_dup_{ver}_{kp}"):
            new_name, n = f"{name} (copy)", 2
            while new_name.lower() in {x.lower() for x in cfg["campaigns"]}:
                new_name, n = f"{name} (copy {n})", n + 1
            cfg["campaigns"][new_name] = copy.deepcopy(camp)
            cfg["campaigns"][new_name]["match_priority"] = 100
            cfg["campaigns"][new_name]["name_keywords"] = []
            if _commit_qc_config(cfg, f"Duplicated as '{new_name}'"):
                st.session_state["cr_pending_choice"] = new_name
                st.rerun()
    with b2:
        confirm = st.checkbox("Confirm delete", key=f"cr_delconfirm_{ver}_{kp}")
    with b3:
        if st.button("🗑 Delete campaign", key=f"cr_del_{ver}_{kp}"):
            if len(cfg["campaigns"]) <= 1:
                st.error("You need at least one campaign.")
            elif not confirm:
                st.warning("Tick 'Confirm delete' first.")
            else:
                del cfg["campaigns"][name]
                if _commit_qc_config(cfg, f"Deleted campaign '{name}'"):
                    st.rerun()


def _cr_add_campaign(cfg, ver):
    names = list(cfg["campaigns"].keys())
    st.markdown("### Add a new campaign")
    with st.form(f"cr_add_{ver}"):
        new_name = st.text_input("Campaign name", placeholder="e.g. Solar Leads", key=f"cr_{ver}_new_name")
        template = st.selectbox("Start from", ["Blank template"] + names, key=f"cr_{ver}_new_tpl",
                                help="Copy an existing campaign's rules and edit them, or start blank.")
        created = st.form_submit_button("➕ Create campaign", type="primary")
    if created:
        clean = new_name.strip()
        if not clean:
            st.error("Enter a campaign name.")
        elif clean.lower() in {n.lower() for n in names}:
            st.error("A campaign with this name already exists.")
        else:
            if template == "Blank template":
                camp = _blank_campaign()
                camp["qc_note_rules"] = cfg["global"].get("default_qc_questions", _BUILTIN_DEFAULT_QC)
            else:
                camp = copy.deepcopy(cfg["campaigns"][template])
                camp["match_priority"] = 100
            camp["name_keywords"] = [clean.lower()]
            cfg["campaigns"][clean] = camp
            if _commit_qc_config(cfg, f"Created campaign '{clean}'"):
                st.session_state["cr_pending_choice"] = clean
                st.rerun()
    st.caption("After creating it, open it from the list above to set its QC note rules, keywords and scores.")


def _cr_campaigns_tab(cfg, ver):
    names = list(cfg["campaigns"].keys())
    add_label = "➕ Add new campaign"
    pending = st.session_state.pop("cr_pending_choice", None)
    if pending in names:
        st.session_state["cr_campaign_choice"] = pending
    if st.session_state.get("cr_campaign_choice") not in names + [add_label]:
        st.session_state["cr_campaign_choice"] = names[0]
    choice = st.selectbox("Campaign to edit", names + [add_label], key="cr_campaign_choice")
    if choice == add_label:
        _cr_add_campaign(cfg, ver)
    else:
        _cr_campaign_editor(cfg, choice, ver)


def _cr_simulator(cfg, ver):
    st.markdown("#### 🧪 Score simulator (uses the saved settings)")
    s1, s2, s3 = st.columns(3)
    with s1:
        sim_campaign = st.selectbox("Campaign", list(cfg["campaigns"].keys()), key=f"sim_c_{ver}")
        sim_type = st.selectbox("Call type", sorted(ALLOWED_CALL_TYPES), index=sorted(ALLOWED_CALL_TYPES).index("QUALIFIED"), key=f"sim_t_{ver}")
        sim_status = st.selectbox("Qualification status", ["QUALIFIED", "NON-QUALIFIED", "NOT CLEAR"], key=f"sim_s_{ver}")
    with s2:
        sim_service = st.checkbox("Service requested", value=True, key=f"sim_svc_{ver}")
        sim_loc = st.checkbox("Location / eligibility", value=True, key=f"sim_loc_{ver}")
        sim_qual = st.checkbox("Qualification info", value=True, key=f"sim_q_{ver}")
        sim_out = st.checkbox("Clear outcome", value=True, key=f"sim_o_{ver}")
    with s3:
        sim_two = st.checkbox("Two-way conversation", value=True, key=f"sim_two_{ver}")
        sim_appt = st.checkbox("Appointment / booking / transfer", value=False, key=f"sim_a_{ver}")
        sim_issue = st.checkbox("QC issue", value=False, key=f"sim_i_{ver}")
        sim_gov = st.checkbox("Caller has government insurance", value=False, key=f"sim_g_{ver}")
        sim_novoice = st.checkbox("No voice in recording", value=False, key=f"sim_nv_{ver}")
    analysis = {
        "call_type": sim_type, "qualification_status": sim_status, "campaign_category": sim_campaign,
        "service_requested": "service" if sim_service else "",
        "location_or_eligibility_present": sim_loc, "qualification_info_present": sim_qual,
        "clear_outcome": sim_out, "two_way_conversation": sim_two, "appointment_set": sim_appt,
        "qc_issue": "issue" if sim_issue else "", "relevant_intent": True,
        "spam_robot": sim_type == "SPAM / ROBOT", "spam_confidence": 95 if sim_type == "SPAM / ROBOT" else 0,
        "no_voice": sim_novoice, "transcript_insurance_status": "YELLOW" if sim_gov else None,
    }
    score, items = _score_breakdown(analysis)
    st.metric("Score", score)
    st.caption(", ".join(items) if items else "No points")


def _cr_scoring_tab(cfg, ver):
    st.caption("These numbers apply to every campaign unless a campaign sets its own value "
               "(Campaigns tab -> Score settings). Qualified and Non-qualified calls can score above 49; "
               "every other call type is always kept under 50.")
    with st.form(f"cr_scoring_{ver}"):
        values = _weight_inputs(cfg, ver, "global", None, only_overridable=False)
        submitted = st.form_submit_button("💾 Save scoring", type="primary")
    if submitted:
        cfg["global"]["weights"].update({k: int(v) for k, v in values.items()})
        if _commit_qc_config(cfg, "Saved scoring"):
            st.rerun()
    st.divider()
    _cr_simulator(cfg, ver)


def _cr_spam_tab(cfg, ver):
    g = cfg["global"]
    with st.form(f"cr_spam_{ver}"):
        st.markdown("#### Spam rules")
        yelp = st.checkbox("Any mention of Yelp or Yellow Pages = spam", value=bool(g["spam"].get("yelp_yellow_is_spam", True)), key=f"cr_{ver}_yelp")
        strict_yellow = st.checkbox("Even the single word \"yellow\" = spam", value=bool(g["spam"].get("strict_yellow_any", False)), key=f"cr_{ver}_yellow")
        extra = st.text_area("Extra words that always mark a call as spam (one per line)", value="\n".join(g["spam"].get("extra_terms", [])), height=110, key=f"cr_{ver}_extra")

        st.markdown("#### No voice")
        v1, v2 = st.columns(2)
        with v1:
            min_words = st.number_input("Fewer words than this = no voice", min_value=1, max_value=20, value=int(g["voice"].get("no_voice_min_words", 3)), step=1, key=f"cr_{ver}_minwords")
        with v2:
            min_secs = st.number_input("Audio shorter than this (seconds) is not transcribed", min_value=0.0, max_value=30.0, value=float(g["voice"].get("min_audio_seconds", 1.0)), step=0.5, key=f"cr_{ver}_minsecs")

        st.markdown("#### Insurance words")
        i1, i2 = st.columns(2)
        with i1:
            gov = st.text_area("Government / state insurance words (one per line)", value="\n".join(g.get("government_terms", [])), height=200, key=f"cr_{ver}_gov")
        with i2:
            priv = st.text_area("Private / commercial insurance words (one per line)", value="\n".join(g.get("private_terms", [])), height=200, key=f"cr_{ver}_priv")

        st.markdown("#### Summary style (all campaigns)")
        style = st.text_area("Summary style rules", value=g.get("summary_style_rules", ""), height=320, key=f"cr_{ver}_style",
                             help="Short-summary rules used for every campaign. Each campaign's own QC note rules are added after these.")
        default_qc = st.text_area("QC note rules for campaigns with no rules of their own", value=g.get("default_qc_questions", ""), height=200, key=f"cr_{ver}_defqc")
        submitted = st.form_submit_button("💾 Save", type="primary")
    if submitted:
        g["spam"].update({"yelp_yellow_is_spam": bool(yelp), "strict_yellow_any": bool(strict_yellow), "extra_terms": _lines(extra)})
        g["voice"].update({"no_voice_min_words": int(min_words), "min_audio_seconds": float(min_secs)})
        g["government_terms"] = _lines(gov)
        g["private_terms"] = _lines(priv)
        g["summary_style_rules"] = style.strip() or _BUILTIN_SUMMARY_STYLE
        g["default_qc_questions"] = default_qc.strip() or _BUILTIN_DEFAULT_QC
        if _commit_qc_config(cfg, "Saved spam / voice / insurance settings"):
            st.rerun()


def _cr_backup_tab(cfg, ver):
    st.markdown("#### Download / import")
    st.download_button("⬇️ Download settings (JSON)", data=json.dumps(cfg, indent=2, ensure_ascii=False),
                       file_name="qc_config.json", mime="application/json", key=f"cr_dl_{ver}")
    uploaded = st.file_uploader("Import settings from a JSON file", type=["json"], key=f"cr_up_{ver}")
    if uploaded is not None and st.button("Import this file", key=f"cr_import_{ver}"):
        try:
            imported = _merge_config(json.loads(uploaded.getvalue().decode("utf-8")), _default_qc_config())
        except Exception as exc:
            st.error(f"Could not read the file: {exc}")
        else:
            if _commit_qc_config(imported, "Imported settings"):
                st.rerun()

    st.markdown("#### Undo / restore")
    r1, r2 = st.columns(2)
    with r1:
        if st.button("↩️ Restore previous version", key=f"cr_prev_{ver}"):
            if QC_CONFIG_PREVIOUS_PATH.exists():
                try:
                    previous = _merge_config(json.loads(QC_CONFIG_PREVIOUS_PATH.read_text(encoding="utf-8")), _default_qc_config())
                except Exception as exc:
                    st.error(f"Previous version unreadable: {exc}")
                else:
                    if _commit_qc_config(previous, "Restored previous version"):
                        st.rerun()
            else:
                st.info("No previous version saved yet.")
    with r2:
        if st.button("☁️ Restore from Google Sheet backup", key=f"cr_sheet_{ver}"):
            data = _load_qc_config_from_sheet()
            if not data:
                st.error("No backup found in the Google Sheet (or the sheet is not reachable).")
            elif _commit_qc_config(_merge_config(data, _default_qc_config()), "Restored from Google Sheet"):
                st.rerun()

    st.markdown("#### Reset")
    confirm = st.checkbox("I understand this replaces ALL campaigns and settings with the built-in defaults", key=f"cr_reset_ok_{ver}")
    if st.button("⚠️ Reset everything to built-in defaults", key=f"cr_reset_{ver}"):
        if not confirm:
            st.warning("Tick the confirmation first.")
        elif _commit_qc_config(_default_qc_config(), "Reset to built-in defaults"):
            st.rerun()


def render_campaign_rules_page(current_user):
    if not current_user.get("is_admin"):
        st.error("Access denied. Admin permissions required.")
        st.stop()

    render_html("""
        <div class="page-title">
            Campaign Rules &amp; Scoring
            <span class="online" style="background: rgba(37,99,235,0.12); border-color: rgba(37,99,235,0.25); color: #2563eb;">● Editable</span>
        </div>
        <p class="subtitle">
            Edit each campaign's QC note rules, keywords and scores, add new campaigns, and tune spam and voice settings.
            Saved changes apply to the next call processed.
        </p>
    """)

    flash = st.session_state.pop("cr_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])

    cfg = copy.deepcopy(QC_CONFIG)
    updated_at = cfg["meta"].get("updated_at") or ""
    ver = re.sub(r"\W+", "", updated_at) or "0"
    st.caption(
        f"Last saved: {updated_at or 'never (using built-in defaults)'}"
        + (f" by {cfg['meta'].get('updated_by')}" if cfg["meta"].get("updated_by") else "")
    )

    tabs = st.tabs(["📋 Campaigns", "🎯 Scoring", "🚫 Spam, Voice & Insurance", "💾 Backup & Reset"])
    with tabs[0]:
        _cr_campaigns_tab(cfg, ver)
    with tabs[1]:
        _cr_scoring_tab(cfg, ver)
    with tabs[2]:
        _cr_spam_tab(cfg, ver)
    with tabs[3]:
        _cr_backup_tab(cfg, ver)


# ============================================================
# SHEET SYNC WITH LIVE PROGRESS
# While a row is being worked on, its output cells show an icon:
#   ⏳ waiting   🔄 working now   (final value = done)   ⚠️ error
# The app shows the same state live (which column is being filled right now).
# ============================================================

PROCESSING_STALE_MINUTES = 10          # a 🔄/⏳ marker older than this is treated as a crashed run
_PROGRESS_ICONS = ("⏳", "🔄")
_SYNC_ICONS = {"waiting": "⏳", "working": "🔄", "done": "✅", "error": "⚠️"}
_SYNC_COLUMN_LABELS = [("G", "Summary"), ("H", "Main topic"), ("K", "QC report"), ("L", "Score")]


def _progress_stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _is_progress_marker(text):
    return str(text or "").strip().startswith(_PROGRESS_ICONS)


def _progress_marker_age_minutes(text):
    """Minutes since a '... since YYYY-MM-DD HH:MM:SS' marker was written (None if unreadable)."""
    match = re.search(r"since (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", str(text or ""))
    if not match:
        return None
    try:
        started = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (datetime.now() - started).total_seconds() / 60.0


def _row_work_state(recording_url, existing_main_topic):
    """'process' = needs work, 'busy' = another run is working on it, 'skip' = nothing to do."""
    if not recording_url or not recording_url.startswith("http"):
        return "skip"
    if not existing_main_topic:
        return "process"
    if _is_progress_marker(existing_main_topic):
        age = _progress_marker_age_minutes(existing_main_topic)
        if age is not None and age < PROCESSING_STALE_MINUTES:
            return "busy"
        return "process"      # stale or unreadable marker: the earlier run died, do it again
    return "skip"


def _write_row_cells(worksheet, row_number, g, h, k, l):
    """Write G:H and K:L of one row in a single API call (H = done flag, so it is written with the rest)."""
    worksheet.batch_update(
        [
            {"range": f"G{row_number}:H{row_number}", "values": [[g, h]]},
            {"range": f"K{row_number}:L{row_number}", "values": [[k, l]]},
        ],
        value_input_option="RAW",
    )


def make_sync_progress_callback(container):
    """Live progress panel for the app. Returns a callback for sync_google_sheet_batch()."""
    with container:
        bar_slot = st.empty()
        panel_slot = st.empty()
    log = []

    def chips(columns):
        html_out = ""
        for col, label in _SYNC_COLUMN_LABELS:
            state = columns.get(col, "waiting")
            emphasis = "font-weight:600;border-color:var(--blue-accent);" if state == "working" else ""
            html_out += (
                "<span style=\"display:inline-block;margin:2px 6px 2px 0;padding:4px 10px;border-radius:999px;"
                "border:1px solid var(--border);background:var(--panel);color:var(--text);font-size:13px;"
                f"{emphasis}\">{_SYNC_ICONS.get(state, '⏳')} Column {col} · {label}</span>"
            )
        return html_out

    def draw(title, subtitle, columns):
        log_html = "".join(
            f"<div style=\"color:var(--muted);font-size:12.5px;\">{html.escape(line)}</div>" for line in log[-6:]
        )
        panel_slot.markdown(
            "<div style=\"border:1px solid var(--border);border-radius:12px;padding:14px 16px;"
            "background:var(--card-bg);margin-bottom:10px;\">"
            f"<div style=\"font-weight:600;color:var(--text);\">{html.escape(title)}</div>"
            f"<div style=\"color:var(--muted);margin:4px 0 8px;\">{html.escape(subtitle)}</div>"
            f"<div>{chips(columns)}</div>{log_html}</div>",
            unsafe_allow_html=True,
        )

    def set_bar(fraction, text):
        try:
            bar_slot.progress(max(0.0, min(1.0, fraction)), text=text)
        except TypeError:
            bar_slot.progress(max(0.0, min(1.0, fraction)))

    def callback(event):
        kind = event.get("event")
        total = int(event.get("total") or 0)
        position = int(event.get("position") or 0)

        if kind == "start":
            if total == 0:
                bar_slot.empty()
                draw("Nothing to process", "No new recordings were found in the sheet.", {})
            else:
                set_bar(0.0, f"0 of {total} rows done")
                draw("Starting", f"{total} row(s) waiting to be processed.", {})
        elif kind == "row":
            set_bar((position - 1) / max(total, 1), f"Row {position} of {total} (sheet row {event.get('row')})")
            draw(
                f"Sheet row {event.get('row')} · {event.get('campaign', '')}",
                event.get("label", "Working..."),
                event.get("columns", {}),
            )
        elif kind == "row_done":
            log.append(f"Row {event.get('row')}: {event.get('call_type')} · score {event.get('score')}")
            set_bar(position / max(total, 1), f"{position} of {total} rows done")
            draw(f"Row {event.get('row')} finished", f"{event.get('call_type')} · score {event.get('score')}",
                 event.get("columns", {}))
        elif kind == "row_error":
            log.append(f"Row {event.get('row')}: error - {str(event.get('message', ''))[:90]}")
            set_bar(position / max(total, 1), f"{position} of {total} rows done")
            draw(f"Row {event.get('row')} failed", str(event.get("message", ""))[:160], event.get("columns", {}))
        elif kind == "finished":
            if total:
                set_bar(1.0, f"Finished: {event.get('processed', 0)} of {total} rows processed")
            all_done = {col: "done" for col, _ in _SYNC_COLUMN_LABELS} if event.get("processed") else {}
            draw("Finished", event.get("message", ""), all_done)

    return callback


def sync_google_sheet_batch(default_campaign_name="", progress_callback=None):
    """Process Ringba recordings from the shared Google Sheet with rate-limiting and timeouts."""
    def emit(**kwargs):
        if progress_callback:
            try:
                progress_callback(kwargs)
            except Exception:
                pass

    try:
        gc = get_google_client()
        sheet = gc.open("Ringba to Sheet QC")
        worksheet = sheet.worksheet("Sheet1")

        rows = worksheet.get_all_values()
        processed_count = 0
        busy_count = 0
        position = 0

        total_pending = sum(
            1 for row in rows[1:]
            if _row_work_state(
                row[8].strip() if len(row) > 8 else "",
                row[7].strip() if len(row) > 7 else "",
            ) == "process"
        )
        emit(event="start", total=total_pending)

        for index, row in enumerate(rows[1:], start=2):
            raw_campaign = row[3].strip() if len(row) > 3 else ""
            raw_duration = row[5].strip() if len(row) > 5 else ""
            existing_main_topic = row[7].strip() if len(row) > 7 else ""
            recording_url = row[8].strip() if len(row) > 8 else ""

            if raw_duration and ":" not in raw_duration and "[" not in raw_duration:
                try:
                    worksheet.update_cell(index, 6, format_seconds_to_hms(raw_duration))
                    time.sleep(0.3)
                except Exception:
                    pass

            work_state = _row_work_state(recording_url, existing_main_topic)
            if work_state == "busy":
                busy_count += 1
                continue
            if work_state != "process":
                continue

            position += 1
            marker = f"🔄 Processing since {_progress_stamp()}"
            campaign_to_use = "General Customer Inquiry"
            row_finished = False

            try:
                campaign_to_use = get_campaign_category(raw_campaign) if raw_campaign else default_campaign_name
                if not campaign_to_use:
                    campaign_to_use = "General Customer Inquiry"

                # ---- Step 1: download + transcribe with safety timeout ----
                emit(event="row", row=index, position=position, total=total_pending, campaign=campaign_to_use,
                     label="Downloading and transcribing the recording",
                     columns={"G": "working", "H": "working", "K": "waiting", "L": "waiting"})
                _write_row_cells(worksheet, index, "🔄 Transcribing audio…", marker, "⏳ Waiting", "⏳")

                # Safe download with timeout constraint to prevent hanging
                url = recording_url.strip()
                filename = url.split("/")[-1].split("?")[0] or "web_audio.mp3"
                ext = Path(filename).suffix.lower()
                safe_name = f"{uuid.uuid4().hex}{ext if ext in {'.mp3', '.wav'} else '.mp3'}"
                path = UPLOAD_DIR / safe_name

                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as response:
                    with open(path, "wb") as out_file:
                        out_file.write(response.read())

                sound = AudioSegment.from_file(path)
                duration_sec = len(sound) / 1000.0

                st.session_state.source_name = filename
                st.session_state.file_path = str(path)
                st.session_state.duration_sec = duration_sec

                timeline_data, raw_text_segments = [], []
                if duration_sec >= MIN_AUDIO_SECONDS_FOR_TRANSCRIPTION:
                    try:
                        timeline_data, raw_text_segments = transcribe_groq_whisper(st.session_state.file_path)
                    except Exception as whisper_exc:
                        if not _is_short_audio_error(whisper_exc):
                            raise
                full_transcript_str = " ".join(raw_text_segments).strip()

                # ---- Step 2: AI summary + analysis ----
                emit(event="row", row=index, position=position, total=total_pending, campaign=campaign_to_use,
                     label="Writing the summary and checking the call",
                     columns={"G": "working", "H": "working", "K": "working", "L": "waiting"})
                _write_row_cells(worksheet, index, "🔄 Writing summary…", marker, "🔄 Checking call…", "⏳")

                analysis = generate_call_analysis_groq(
                    full_transcript_str,
                    campaign_to_use,
                    timeline_data=timeline_data,
                )
                analysis["campaign_category"] = campaign_to_use

                main_topic = analysis["main_topic"]
                detailed_summary = analysis["long_summary"]
                score = calculate_call_quality_score(analysis)
                analysis["quality_score"] = score
                qc_report = build_qc_report(analysis)

                # ---- Step 3: save results ----
                emit(event="row", row=index, position=position, total=total_pending, campaign=campaign_to_use,
                     label="Saving results to the sheet",
                     columns={"G": "working", "H": "working", "K": "working", "L": "working"})
                _write_row_cells(worksheet, index, detailed_summary, main_topic, qc_report, score)

                apply_row_score_color(worksheet, index, score, analysis)

                row_finished = True
                processed_count += 1
                emit(event="row_done", row=index, position=position, total=total_pending, campaign=campaign_to_use,
                     call_type=analysis.get("call_type", ""), score=score,
                     columns={"G": "done", "H": "done", "K": "done", "L": "done"})
                
                # Rate-limit cushion: 2.5 second pause between rows to protect Groq quota
                time.sleep(2.5)

            except Exception as e:
                row_finished = True
                error_text = f"⚠️ Processing error: {str(e)}"
                try:
                    _write_row_cells(worksheet, index, error_text, "", error_text, "")
                except Exception:
                    pass
                emit(event="row_error", row=index, position=position, total=total_pending,
                     campaign=campaign_to_use, message=str(e),
                     columns={"G": "error", "H": "error", "K": "error", "L": "error"})
                time.sleep(1.5)
            finally:
                if not row_finished:
                    try:
                        _write_row_cells(worksheet, index, "", "", "", "")
                    except Exception:
                        pass

        message = f"Successfully processed {processed_count} new recordings!"
        if busy_count:
            message += f" {busy_count} row(s) are being processed by another run and were skipped."
        emit(event="finished", total=total_pending, processed=processed_count, message=message)
        return True, message
    except Exception as e:
        return False, f"Google Sheets error: {str(e)}"


# ============================================================
# THEME DYNAMIC INJECTION
# ============================================================

if st.session_state.theme_mode == "light":
    theme_vars = """
        --bg: #f8fafc;
        --panel: #ffffff;
        --border: #cbd5e1;
        --text: #0f172a;
        --title-color: #0f172a;
        --muted: #475569;
        --sidebar-bg: #ffffff;
        --card-bg: #ffffff;
        --input-bg: #f1f5f9;
        --btn-bg: #e2e8f0;
        --stamp-bg: #eff6ff;
        --stamp-text: #1d4ed8;
        --blue-accent: #2563eb;
        --card-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
    """
else:
    theme_vars = """
        --bg: #0b111e;
        --panel: #111a2e;
        --border: #1a2942;
        --text: #e2e8f0;
        --title-color: #ffffff;
        --muted: #64748b;
        --sidebar-bg: #080d1a;
        --card-bg: #101828;
        --input-bg: #090e17;
        --btn-bg: #172439;
        --stamp-bg: #132440;
        --stamp-text: #3b82f6;
        --blue-accent: #2563eb;
        --card-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
    """

render_html(f"""
<style>
:root {{
    {theme_vars}
    --blue: #3b82f6;
    --green: #10b981;
    --radius: 8px;
}}
header[data-testid="stHeader"] {{ background: transparent !important; }}
.stApp {{ background: var(--bg) !important; color: var(--text) !important; }}
.block-container {{ max-width: 1700px; padding-top: 18px; padding-bottom: 20px; }}
section[data-testid="stSidebar"] {{ background: var(--sidebar-bg) !important; border-right: 1px solid var(--border) !important; }}
section[data-testid="stSidebar"] > div {{ padding-top: 14px; }}
div[data-testid="stRadio"] label {{ color: var(--text) !important; font-weight: 800 !important; font-size: 13px !important; }}
.brand {{ display: flex; align-items: center; gap: 10px; font-weight: 900; letter-spacing: .05em; color: var(--text); font-size: 16px; padding: 0px 4px 18px; }}
.brand-mark {{ width: 24px; height: 24px; border-radius: 6px; background: linear-gradient(135deg, #2563eb, #1d4ed8); display: grid; place-items: center; box-shadow: 0 2px 8px rgba(37,99,235,0.4); }}
.sidebar-divider {{ height: 1px; background: var(--border); margin: 12px 0; }}
.sidebar-nav {{ color: var(--text); font-size: 13px; padding: 9px 12px; border-radius: 6px; margin-bottom: 3px; font-weight: 700 !important; cursor: pointer; }}
.sidebar-nav.active {{ background: rgba(37,99,235,0.12); color: #2563eb; font-weight: 800 !important; border-left: 3px solid #2563eb; }}
.sidebar-label {{ font-size: 11px; font-weight: 800 !important; color: var(--muted); margin: 10px 4px 6px; text-transform: uppercase; letter-spacing: .05em; }}
.sidebar-user {{ margin-top: 15px; padding-top: 12px; border-top: 1px solid var(--border); display: flex; align-items: center; gap: 10px; color: var(--text); font-size: 13px; font-weight: 700 !important; }}
.avatar {{ width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; background: var(--input-bg); color: var(--muted); font-weight: 800; font-size: 11px; border: 1px solid var(--border); }}
.page-title {{ display: flex; align-items: center; gap: 10px; margin: 4px 0 2px 0; font-size: 26px; color: var(--title-color); font-weight: 800; }}
.online {{ font-size: 11px; color: #10b981; background: rgba(16,185,129,0.12); border: 1px solid rgba(16,185,129,0.25); padding: 2px 8px; border-radius: 999px; font-weight: 600; }}
.subtitle {{ color: var(--muted); font-size: 13px; margin: 0 0 16px 0; }}
.dopp-card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--card-shadow); overflow: hidden; }}
.card-title {{ display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 700; color: var(--title-color); }}
.card-dot {{ width: 8px; height: 8px; border-radius: 50%; background: #2563eb; }}
div[data-baseweb="select"] > div {{ background-color: var(--card-bg) !important; color: var(--text) !important; border-color: var(--border) !important; font-weight: 700 !important; }}
div.stButton > button {{ background: var(--btn-bg) !important; color: var(--text) !important; border: 1px solid var(--border) !important; border-radius: 6px; font-size: 13px; font-weight: 600; min-height: 38px; height: 38px; }}
div.stButton > button[kind="primary"] {{ background: #2563eb !important; color: #ffffff !important; border: none !important; font-weight: 700 !important; }}
.stTextInput input {{ background: var(--card-bg) !important; border: 1px solid var(--border) !important; color: var(--text) !important; border-radius: 6px !important; font-size: 13px !important; }}
.meta-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 8px; padding: 10px; }}
.meta-item {{ background: var(--input-bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; }}
.meta-key {{ color: var(--muted); font-size: 10px; text-transform: uppercase; font-weight: 600; }}
.meta-value {{ color: var(--title-color); font-size: 12px; margin-top: 3px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.status-box {{ margin-top: 8px; padding: 8px 12px; border-radius: 6px; background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3); color: #059669; font-size: 12px; display: flex; align-items: center; gap: 8px; font-weight: 700; }}
.summary-card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; box-shadow: var(--card-shadow); min-height: 130px; }}
.summary-icon {{ width: 28px; height: 28px; border-radius: 6px; background: rgba(37,99,235,0.15); color: #2563eb; display: grid; place-items: center; margin-bottom: 8px; font-size: 14px; }}
.summary-title {{ margin: 0; font-size: 15px; font-weight: 700; color: var(--title-color); }}
.topic-text {{ margin: 10px 0 0; color: var(--text); font-size: 14px; font-weight: 600; line-height: 1.5; }}
.summary-text {{ margin: 10px 0 0; color: var(--text); font-size: 13px; line-height: 1.6; }}
.transcript-container {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--card-shadow); overflow: hidden; }}
.transcript-header {{ padding: 14px 16px; border-bottom: 1px solid var(--border); }}
.transcript-scroll {{ max-height: 560px; overflow-y: auto; padding: 6px 12px; }}
.transcript-line {{ display: grid; grid-template-columns: 85px 65px minmax(0,1fr); gap: 10px; padding: 12px 0; border-bottom: 1px solid var(--border); align-items: start; }}
.transcript-stamp {{ display: inline-flex; justify-content: center; background: var(--stamp-bg); color: var(--stamp-text); border-radius: 4px; padding: 3px 6px; font-size: 11px; font-weight: 700; }}
.transcript-speaker {{ font-size: 12px; color: var(--muted); padding-top: 2px; font-weight: 600; }}
.transcript-text {{ font-size: 13px; line-height: 1.5; color: var(--text); }}
</style>
""")


# ============================================================
# AUTHENTICATION SCREEN (LOGIN / SIGN UP / FORGOT PASSWORD)
# ============================================================

if not st.session_state.logged_in_user:
    st.markdown("## Welcome to **Aunty Next DOOR**")
    st.markdown("Please log in, create a public account, or reset your password.")

    auth_tab1, auth_tab2, auth_tab3 = st.tabs(["🔐 Login", "📝 Sign Up", "🔑 Forgot Password"])

    with auth_tab1:
        st.subheader("Login to your account")
        login_email = st.text_input("Email Address", key="login_email")
        login_password = st.text_input("Password", type="password", key="login_pass")
        if st.button("Log In", type="primary", use_container_width=True):
            user = authenticate_user(login_email, login_password)
            if user:
                st.session_state.logged_in_user = user
                st.session_state.current_view = "transcriber"
                st.success(f"Welcome back, {user['name']}!")
                time.sleep(0.5)
                st.rerun()
            else:
                st.error("Invalid email or password.")

    with auth_tab2:
        st.subheader("Create a public account")
        signup_name = st.text_input("Full Name", key="signup_name")
        signup_email = st.text_input("Email Address", key="signup_email")
        signup_password = st.text_input("Create Password", type="password", key="signup_pass")
        if st.button("Create Account", type="primary", use_container_width=True):
            if signup_name and signup_email and signup_password:
                ok, msg = register_user(signup_name, signup_email, signup_password)
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)
            else:
                st.warning("Please fill out all fields.")

    with auth_tab3:
        st.subheader("Reset Password via Email")
        
        step1, step2 = st.columns(2)
        
        with step1:
            st.markdown("##### 1. Request Reset Code")
            reset_req_email = st.text_input("Registered Email Address", key="reset_req_email")
            if st.button("Send Verification Code", use_container_width=True):
                if reset_req_email.strip():
                    with st.spinner("Generating and sending code..."):
                        ok, msg = generate_reset_code(reset_req_email)
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)
                else:
                    st.warning("Please enter your email address.")

        with step2:
            st.markdown("##### 2. Enter Code & Set New Password")
            reset_email = st.text_input("Email Address", key="reset_email")
            reset_code = st.text_input("6-Digit Code", key="reset_code")
            new_pass = st.text_input("New Password", type="password", key="reset_new_pass")
            
            if st.button("Update Password", type="primary", use_container_width=True):
                if reset_email.strip() and reset_code.strip() and new_pass.strip():
                    ok, msg = reset_password_with_code(reset_email, reset_code, new_pass)
                    if ok:
                        st.success(msg)
                    else:
                        st.error(msg)
                else:
                    st.warning("Please fill in all reset fields.")

    st.stop()


# ============================================================
# LOGGED IN SIDEBAR & ROUTING
# ============================================================

current_user = st.session_state.logged_in_user

# Live sync progress panel (shows at the top of the page while the sheet is processed).
sync_progress_container = st.container()

with st.sidebar:
    render_html("""
        <div class="brand">
            <div class="brand-mark"></div>
            <span>Aunty Next DOOR</span>
        </div>
    """)

    theme_choice = st.radio(
        "Theme Mode",
        ["Dark", "Light"],
        index=0 if st.session_state.theme_mode == "dark" else 1,
        horizontal=True,
        label_visibility="collapsed",
    )
    if theme_choice.lower() != st.session_state.theme_mode:
        st.session_state.theme_mode = theme_choice.lower()
        st.rerun()

    if st.button("🎙️ &nbsp; Transcriber", use_container_width=True, type="primary" if st.session_state.current_view == "transcriber" else "secondary"):
        st.session_state.current_view = "transcriber"
        st.rerun()

    if current_user.get("is_admin"):
        if st.button("🛡 &nbsp; Admin Panel", use_container_width=True, type="primary" if st.session_state.current_view == "admin" else "secondary"):
            st.session_state.current_view = "admin"
            st.rerun()

        if st.button("⚙️ &nbsp; Campaign Rules", use_container_width=True, type="primary" if st.session_state.current_view == "campaign_rules" else "secondary"):
            st.session_state.current_view = "campaign_rules"
            st.rerun()

    render_html("""
        <div class="sidebar-divider"></div>
        <div class="sidebar-label">Campaign</div>
    """)

    selected_campaign = st.selectbox(
        "Campaign",
        list(CAMPAIGN_QC_QUESTIONS.keys()),
        index=0,
        label_visibility="collapsed",
    )

    render_html("""
        <div class="sidebar-divider"></div>
        <div class="sidebar-label">Google Sheets Automation</div>
    """)
    if st.button("🔄 Sync & Process Sheet", use_container_width=True):
        with st.spinner("Scanning sheet and processing recordings..."):
            success, message = sync_google_sheet_batch(
                selected_campaign,
                progress_callback=make_sync_progress_callback(sync_progress_container),
            )
            if success:
                st.success(message)
            else:
                st.error(message)

    render_html("""
        <div class="sidebar-divider"></div>
        <div class="sidebar-label">User Account Profile</div>
    """)

    with st.form("profile_update_form"):
        new_name = st.text_input("Name", value=current_user["name"])
        new_email = st.text_input("Email", value=current_user["email"])
        new_pass = st.text_input("New Password (optional)", type="password", placeholder="Leave blank to keep current")

        save_profile = st.form_submit_button("Save Profile Changes", use_container_width=True, type="primary")

        if save_profile:
            ok, msg = update_user_profile(current_user["id"], new_name, new_email, new_pass)
            if ok:
                st.session_state.logged_in_user["name"] = new_name
                st.session_state.logged_in_user["email"] = new_email
                st.success(msg)
                time.sleep(0.5)
                st.rerun()
            else:
                st.error(msg)

    if st.button("Logout", use_container_width=True):
        st.session_state.logged_in_user = None
        st.rerun()

    initials = "".join([part[0].upper() for part in current_user['name'].split()[:2]]) or "U"
    admin_badge = " <span style='font-size:10px; color:#10b981;'>(Admin)</span>" if current_user.get("is_admin") else ""
    render_html(f"""
        <div class="sidebar-user">
            <div class="avatar">{initials}</div>
            <div style="flex:1">{html.escape(current_user['name'])}{admin_badge}</div>
        </div>
    """)


# ============================================================
# ADMIN PANEL VIEW
# ============================================================

if st.session_state.current_view == "admin":
    if not current_user.get("is_admin"):
        st.error("Access denied. Admin permissions required.")
        st.stop()

    render_html("""
        <div class="page-title">
            Admin Panel
            <span class="online" style="background: rgba(37,99,235,0.12); border-color: rgba(37,99,235,0.25); color: #2563eb;">● Management</span>
        </div>
        <p class="subtitle">
            Manage system users, grant/revoke permissions, and reset user credentials.
        </p>
    """)

    admin_tabs = st.tabs(["👥 Manage Users", "➕ Create New User"])

    with admin_tabs[0]:
        users_list = get_all_users()
        st.subheader(f"Registered Accounts ({len(users_list)})")

        for u in users_list:
            with st.expander(f"{u['name']} ({u['email']}) {'— [ADMIN]' if u['is_admin'] else ''}"):
                c1, c2, c3 = st.columns([1.5, 1.5, 1])
                
                with c1:
                    is_adm = st.checkbox("Admin Role", value=u["is_admin"], key=f"role_{u['id']}")
                    if is_adm != u["is_admin"]:
                        admin_toggle_role(u["id"], is_adm)
                        st.success("User role updated!")
                        time.sleep(0.4)
                        st.rerun()

                with c2:
                    new_user_pwd = st.text_input("Reset Password", key=f"pwd_{u['id']}", type="password", placeholder="New password")
                    if st.button("Update Password", key=f"btn_pwd_{u['id']}"):
                        if new_user_pwd.strip():
                            admin_reset_password(u["id"], new_user_pwd.strip())
                            st.success("Password updated!")
                        else:
                            st.warning("Enter a valid password.")

                with c3:
                    if u["id"] != current_user["id"]:
                        if st.button("🗑️ Delete Account", key=f"del_{u['id']}", type="secondary"):
                            admin_delete_user(u["id"])
                            st.success("User deleted!")
                            time.sleep(0.4)
                            st.rerun()
                    else:
                        st.caption("Cannot delete self")

    with admin_tabs[1]:
        st.subheader("Add a New User Account")
        with st.form("admin_create_user"):
            new_u_name = st.text_input("Full Name")
            new_u_email = st.text_input("Email Address")
            new_u_pass = st.text_input("Password", type="password")
            new_u_is_admin = st.checkbox("Grant Admin Privileges")
            
            if st.form_submit_button("Create User", type="primary"):
                if new_u_name and new_u_email and new_u_pass:
                    ok, msg = register_user(new_u_name, new_u_email, new_u_pass, 1 if new_u_is_admin else 0)
                    if ok:
                        st.success(msg)
                        time.sleep(0.5)
                        st.rerun()
                    else:
                        st.error(msg)
                else:
                    st.warning("Please fill out all fields.")

    st.stop()


# ============================================================
# CAMPAIGN RULES VIEW (ADMIN)
# ============================================================

if st.session_state.current_view == "campaign_rules":
    render_campaign_rules_page(current_user)
    st.stop()


# ============================================================
# MAIN TRANSCRIBER INTERFACE (PROTECTED)
# ============================================================

render_html("""
    <div class="page-title">
        Transcriber
        <span class="online">● Online</span>
    </div>
    <p class="subtitle">
        Convert audio to text and get AI-powered summaries and insights.
    </p>
""")

left_col, right_col = st.columns([1.05, 0.95], gap="medium")

with left_col:
    summary_col1, summary_col2 = st.columns(2)

    with summary_col1:
        render_html(f"""
            <div class="summary-card">
                <div class="summary-icon">◎</div>
                <h3 class="summary-title">Main topic</h3>
                <p class="topic-text">{html.escape(st.session_state.short_topic or "No transcription available yet.")}</p>
            </div>
        """)
        if st.session_state.transcribed and st.session_state.short_topic:
            render_copy_icon_button(st.session_state.short_topic, "btn_copy_topic")

    with summary_col2:
        render_html(f"""
            <div class="summary-card">
                <div class="summary-icon">▤</div>
                <h3 class="summary-title">AI call summary</h3>
                <p class="summary-text">{html.escape(st.session_state.detailed_summary or "No summary generated yet.")}</p>
            </div>
        """)
        if st.session_state.transcribed and st.session_state.detailed_summary:
            render_copy_icon_button(st.session_state.detailed_summary, "btn_copy_summary")

    render_html("<div style='height:6px'></div>")

    if "source_mode" not in st.session_state:
        st.session_state.source_mode = "url"

    tab_url, tab_upload = st.columns(2)
    with tab_url:
        if st.button("🔗 Paste recording URL", use_container_width=True, type=("primary" if st.session_state.source_mode == "url" else "secondary")):
            st.session_state.source_mode = "url"
            st.rerun()

    with tab_upload:
        if st.button("↥ Upload MP3 / WAV", use_container_width=True, type=("primary" if st.session_state.source_mode == "upload" else "secondary")):
            st.session_state.source_mode = "upload"
            st.rerun()

    if st.session_state.source_mode == "url":
        with st.form("url_form", clear_on_submit=False):
            url_value = st.text_input("Recording URL", placeholder="https://example.com/recording.mp3", label_visibility="collapsed")
            submitted = st.form_submit_button("Load Audio", type="primary", use_container_width=True)
            if submitted:
                if not url_value.strip():
                    st.error("Paste a recording URL first.")
                else:
                    with st.spinner("Downloading audio from link..."):
                        try:
                            load_audio_url(url_value)
                            st.success("Recording link loaded and ready.")
                        except Exception as e:
                            st.error(f"Error loading URL: {str(e)}")
    else:
        uploaded_file = st.file_uploader("Drop your audio here", type=["mp3", "wav"], label_visibility="collapsed")
        if uploaded_file is not None:
            if st.session_state.get("last_uploaded_name") != uploaded_file.name:
                try:
                    save_uploaded_audio(uploaded_file)
                    st.session_state["last_uploaded_name"] = uploaded_file.name
                    st.success("Audio loaded successfully.")
                except Exception as e:
                    st.error(f"Error loading audio: {str(e)}")

    valid_audio = st.session_state.file_path and os.path.exists(st.session_state.file_path)

    if st.button("🎙️ Transcribe audio", type="primary", use_container_width=True, disabled=not valid_audio):
        progress_bar = st.progress(0, text="Preparing audio...")
        status_placeholder = st.empty()
        try:
            start_clock = time.time()
            status_placeholder.info("Processing Groq Whisper...")
            progress_bar.progress(15, text="Sending audio to Groq Whisper...")

            timeline_data, raw_text_segments = transcribe_groq_whisper(st.session_state.file_path)
            progress_bar.progress(65, text="Generating AI summary...")

            full_transcript_str = " ".join(raw_text_segments)
            short_topic, detailed_summary = generate_fast_summary_groq(
                full_transcript_str,
                selected_campaign,
            )
            elapsed_time = round(time.time() - start_clock, 1)

            st.session_state.transcript = timeline_data
            st.session_state.full_text = full_transcript_str
            st.session_state.short_topic = short_topic
            st.session_state.detailed_summary = detailed_summary
            st.session_state.transcribed = True
            st.session_state.status = f"Transcription completed ({elapsed_time}s)"
            st.session_state.elapsed = elapsed_time

            progress_bar.progress(100, text="Completed")
            status_placeholder.success(f"Done in {elapsed_time}s")
            time.sleep(0.5)
            st.rerun()
        except Exception as e:
            progress_bar.empty()
            status_placeholder.error(f"Transcription failed: {str(e)}")

    if st.session_state.file_path and os.path.exists(st.session_state.file_path):
        try:
            with open(st.session_state.file_path, "rb") as audio_file:
                st.audio(audio_file.read(), format="audio/mp3")
        except Exception:
            pass

    render_html(f"""
        <div class="dopp-card">
            <div class="meta-grid">
                <div class="meta-item">
                    <div class="meta-key">Duration</div>
                    <div class="meta-value">{format_time(st.session_state.duration_sec)}</div>
                </div>
                <div class="meta-item">
                    <div class="meta-key">Est. Time</div>
                    <div class="meta-value">~{st.session_state.est_proc_sec}s</div>
                </div>
                <div class="meta-item">
                    <div class="meta-key">Source</div>
                    <div class="meta-value">{html.escape(st.session_state.source_type)}</div>
                </div>
                <div class="meta-item">
                    <div class="meta-key">Campaign</div>
                    <div class="meta-value" style="color:#2563eb">{html.escape(selected_campaign)}</div>
                </div>
            </div>
        </div>
        <div class="status-box">
            <span>✓</span>
            <span>{html.escape(st.session_state.status)}</span>
            <span style="margin-left:auto">{ "100%" if st.session_state.transcribed else "0%" }</span>
        </div>
    """)

with right_col:
    render_html("""
        <div class="transcript-container">
            <div class="transcript-header">
                <div class="card-title"><span class="card-dot"></span> Timeline Transcript</div>
            </div>
        </div>
    """)

    if st.session_state.transcribed and st.session_state.full_text:
        render_copy_icon_button(st.session_state.full_text, "btn_copy_transcript")

    search_query = st.text_input("Search transcript", placeholder="Search transcript...", label_visibility="collapsed")
    transcript_data = st.session_state.transcript

    if not transcript_data:
        render_html("""
            <div class="transcript-container">
                <div class="transcript-scroll">
                    <div style="padding:40px 20px; text-align:center; color:var(--muted); font-size:13px;">
                        No transcript processed yet.
                    </div>
                </div>
            </div>
        """)
    else:
        query = search_query.strip().lower()
        rows = []
        for item in transcript_data:
            timestamp = item.get("time", "")
            speaker = item.get("speaker", "Unknown")
            line_text = item.get("line", "")
            if query and query not in f"{timestamp} {speaker} {line_text}".lower():
                continue
            rows.append(f"""
                <div class="transcript-line">
                    <span class="transcript-stamp">{html.escape(timestamp)}</span>
                    <span class="transcript-speaker">{html.escape(speaker)}</span>
                    <div class="transcript-text">{html.escape(line_text)}</div>
                </div>
            """)

        if rows:
            render_html('<div class="transcript-container"><div class="transcript-scroll">' + "".join(rows) + "</div></div>")
        else:
            render_html("""
                <div class="transcript-container">
                    <div class="transcript-scroll">
                        <div style="padding:40px 20px; text-align:center; color:var(--muted); font-size:13px;">
                            No matching transcript found.
                        </div>
                    </div>
                </div>
            """)
