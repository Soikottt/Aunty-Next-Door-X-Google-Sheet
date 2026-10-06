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

The transcript is generated by Vosk speech-to-text and may contain grammar mistakes, repeated words, missing words, or incorrect word recognition. Understand the conversation using context and correct obvious transcription errors only when the intended meaning is clear. Never invent or assume information.

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


def _analysis_prompt(full_transcript, campaign_name, timeline_data=None):
    """Build a compact analysis prompt. Keep the transcript, but avoid sending
    timestamps, speaker-label instructions, or the old long JSON schema."""
    campaign_guidance = {
        "Rehab & Addiction Treatment": (
            "Focus on treatment/service requested, caller reason, insurance, location, "
            "qualification, and outcome. Medicaid/Medicare/state/government/public insurance "
            "is NON-QUALIFIED for this network rule; private/commercial insurance is a positive signal."
        ),
        "Dumpster & Porta Potty Services": (
            "Focus on dumpster/porta-potty service, size/type, use, price, location, delivery date, and booking."
        ),
        "Pest Control & Home Services": (
            "Focus on the home/pest problem, service requested, homeowner/renter status, location, quote, appointment, and outcome."
        ),
        "Insurance (Health / Auto / Home)": (
            "Focus on coverage type, current policy situation, quote/new policy/existing policy, eligibility, and outcome."
        ),
        "Debt Relief & Financial Services": (
            "Focus on debt/financial need, debt amount/type when stated, requested help, qualification, transfer/consultation, and outcome."
        ),
    }.get(
        campaign_name,
        "Focus on the caller's reason, requested service/information, important qualification details, location/eligibility, and outcome."
    )

    return f"""
You are a pay-per-call call QC analyst. Analyze the complete transcript and return ONLY one valid JSON object.

CAMPAIGN: {campaign_name}
CAMPAIGN FOCUS: {campaign_guidance}

Return exactly these keys:
long_summary, main_topic, call_type, qualification_status, caller_intent, why_called,
service_requested, insurance, location, outcome, spam_robot, spam_confidence,
qc_issue, spam_reason, qualification_reason

Allowed call_type values: QUALIFIED, NON-QUALIFIED, WRONG NUMBER, SPAM / ROBOT, INFORMATION ONLY, SILENT / NO RESPONSE, OTHER.
Allowed qualification_status values: QUALIFIED, NON-QUALIFIED, NOT CLEAR.
Use empty strings for unknown string values. spam_robot must be true/false. spam_confidence must be 0-100.

RULES:
- Use only facts supported by the transcript. Never guess or invent.
- An agent's question is NOT the caller's answer. Insurance must come from the caller's own statement/response.
- Write a natural 2-8 sentence long_summary and a 5-25 word main_topic. Cover the caller's reason for calling, what they asked for, key details they stated, what the agent said or offered, any objections, and how the call ended.
- If the caller is unrelated, explain what they actually wanted.
- If the call is a wrong number, describe what they were trying to reach when clear.
- If the call is silent/no-response, do not invent caller intent.
- If spam/robot, describe what the call was promoting or asking the recipient to do.
- Detect spam semantically: scripted/repeated language, press-0/press-9 instructions, automated marketing, fake verification, SEO/Google listing solicitations, insurance/debt marketing robots, and similar behavior.
- If the caller mentions Yelp or Yellow Pages in any context, mark the call as spam.
- A normal irrelevant or non-qualified caller is not automatically spam.
- For Rehab, Medicaid/Medicare/state/government/public insurance means NON-QUALIFIED. Private/commercial plans such as BCBS, Aetna, Cigna, UnitedHealthcare, Humana, PPO/HMO/EPO/POS are positive qualification signals when clearly stated by the caller.
- qualification_reason should briefly explain why the qualification status was chosen.
- qc_issue should contain only a real QC issue when supported; otherwise empty.

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


def apply_deterministic_spam_rules(analysis, full_transcript):
    """Apply high-confidence network spam rules after AI analysis.

    This is intentionally conservative: directory names such as Yelp or Yellow Pages
    are only treated as spam when the transcript also shows business-listing,
    advertising, verification, optimization, or similar solicitation behavior.
    """
    text = str(full_transcript or "").lower()

    directory_terms = ["yelp", "yellow pages", "yp.com"]
    solicitation_terms = [
        "business listing", "listing", "advertising", "advertise", "marketing",
        "promote", "promotion", "verification", "verify your listing",
        "update your listing", "claim your listing", "optimize", "optimization",
        "profile", "visibility", "search ranking", "ranking", "lead generation",
        "featured listing", "paid listing", "upgrade", "sales",
    ]

    directory_hit = any(term in text for term in directory_terms)
    solicitation_hit = any(term in text for term in solicitation_terms)

    # A directory name + business solicitation is a strong spam signal.
    if directory_hit and solicitation_hit:
        analysis["spam_robot"] = True
        analysis["spam_confidence"] = max(int(analysis.get("spam_confidence", 0) or 0), 97)
        current_reason = str(analysis.get("spam_reason", "") or "").strip()
        directory_name = "Yelp" if "yelp" in text else "Yellow Pages"
        forced_reason = f"{directory_name} business-listing/advertising solicitation"
        analysis["spam_reason"] = forced_reason if not current_reason else f"{forced_reason}; {current_reason}"
        analysis["call_type"] = "SPAM / ROBOT"
        if not str(analysis.get("qc_issue", "") or "").strip():
            analysis["qc_issue"] = "Business directory advertising or listing solicitation"

    return analysis


def _derive_analysis_flags(analysis, full_transcript, timeline_data=None):
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
        analysis["spam_confidence"] = max(0, min(100, int(analysis.get("spam_confidence", 0) or 0)))
    except (TypeError, ValueError):
        analysis["spam_confidence"] = 0

    for key in [
        "long_summary", "main_topic", "caller_intent", "why_called", "service_requested",
        "insurance", "location", "outcome", "qc_issue", "spam_reason", "qualification_reason"
    ]:
        analysis[key] = str(analysis.get(key, "") or "").replace("*", "").strip()

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
    if not full_transcript.strip():
        raise RuntimeError("No transcription text was available for AI analysis.")

    if not GROQ_API_KEYS:
        raise RuntimeError("No Groq API keys are configured.")

    ai_transcript = _trim_for_token_limit(full_transcript, campaign_name, timeline_data)
    prompt = _analysis_prompt(ai_transcript, campaign_name, timeline_data)

    analysis = _with_key_fallback(
        lambda c, lbl: _call_structured_analysis(c, prompt, lbl), "ANALYSIS"
    )
    analysis = apply_deterministic_spam_rules(analysis, full_transcript)

    return _derive_analysis_flags(analysis, full_transcript, timeline_data)


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
    if not full_transcript.strip():
        raise RuntimeError("No transcription text was available for AI summary.")

    if not GROQ_API_KEYS:
        raise RuntimeError("No Groq API keys are configured.")

    campaign_family = get_campaign_category(campaign_name)
    prompt = f"""
You are a fast call-summary assistant for a pay-per-call network.

CAMPAIGN:
{campaign_family}

Create only two outputs from the transcript: a short Main Topic and a factual Long Summary.

MAIN TOPIC:
- One short sentence, normally 5-25 words.
- State what the caller was actually calling about.
- If unrelated, describe what they actually wanted.
- If spam/robot, describe the actual subject of the spam, such as a Google listing or SEO solicitation.

LONG SUMMARY:
- Write 2-8 short, natural sentences.
- Include the caller's actual reason for calling, requested service, important information clearly provided, and the actual outcome when supported.
- Use only facts clearly supported by the transcript.
- Never invent insurance, location, qualification, appointments, transfers, or outcomes.
- An agent's question is NOT the caller's answer. Only treat information as caller-provided when the caller clearly states or confirms it.
- If the caller did not respond, do not invent a reason for the call.
- If the call is clearly an automated solicitation, summarize what it was promoting or asking the recipient to do. Yelp business-listing/advertising/verification/optimization calls and Yellow Pages business-listing/advertising/verification calls should be treated as spam when they are soliciting the business rather than acting as a normal consumer.
- No bullets, headings, markdown, filler, or comments about the quality of the conversation.
- Keep the wording simple and human.

SPAM / ROBOT RULES:
- Do NOT use exact phrase matching only.
- Use semantic similarity, conversation behavior, repeated scripted language, press-0/press-9 instructions, automated promotional language, fake verification claims, marketing solicitations, synthetic/automated behavior, and known spam patterns together.
- Known reference patterns include Google listing/SEO solicitations, fake business verification, insurance sales robots, debt/loan marketing robots, and repeated press-0/press-9 scripts.
- Similar wording must be recognized even when the exact words differ.
- A normal caller who is simply irrelevant or non-qualified is NOT automatically spam.
- Set spam_robot=true only when the transcript gives strong evidence of an automated/spam call.
- spam_confidence must reflect the strength of the evidence from 0-100.

QUALIFICATION RULES:
- Use QUALIFIED only when the campaign-specific qualification requirements are clearly met.
- Use NON-QUALIFIED when the caller is clearly relevant but fails or does not meet the campaign requirements.
- Use NOT CLEAR when the transcript does not provide enough information to determine qualification.

MISSING INFORMATION:
For string fields, use an empty string when the information was not clearly discussed. Do not invent values.

CALL TYPE:
- QUALIFIED: relevant and clearly qualified.
- NON-QUALIFIED: relevant but clearly not qualified.
- WRONG NUMBER: caller was trying to reach another person/business/service.
- SPAM / ROBOT: automated/scripted spam or marketing call.
- INFORMATION ONLY: caller wanted information but not the campaign service/action.
- SILENT / NO RESPONSE: no meaningful caller response or no meaningful two-way interaction.
- OTHER: anything else that does not fit the categories.

QC ISSUE:
Mention only a real issue supported by the transcript, such as automated solicitation, wrong number, agent handling problem, caller objection, silence, or a clear qualification problem. Otherwise use an empty string.

Read the entire transcript before deciding.

TRANSCRIPT:
{full_transcript}
"""

    result = _with_key_fallback(
        lambda c, lbl: _call_fast_summary(c, prompt, lbl), "FAST SUMMARY"
    )
    

    main_topic = str(result.get("main_topic", "")).replace("*", "").strip()
    long_summary = str(result.get("long_summary", "")).replace("*", "").strip()

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


def get_rehab_insurance_status(analysis):
    """Classify Rehab insurance for deterministic qualification scoring."""
    insurance = str(analysis.get("insurance", "") or "").strip().lower()
    if not insurance:
        return None

    government_terms = [
        "medicaid", "medicare", "state insurance", "state-funded",
        "state funded", "government insurance", "government-funded",
        "government funded", "government plan", "government program",
        "public insurance", "public plan", "chip", "marketplace",
    ]
    if any(term in insurance for term in government_terms):
        return "GOVERNMENT"

    private_terms = [
        "private", "employer", "employee", "commercial", "company insurance",
        "group insurance", "group plan", "ppo", "hmo", "pos", "epo",
        "insurance through work",
    ]
    if any(term in insurance for term in private_terms):
        return "PRIVATE"

    return None


def get_decision_signal(score, analysis):
    """Convert the numeric score into a practical operations signal."""
    spam = bool(analysis.get("spam_robot"))
    spam_conf = int(analysis.get("spam_confidence", 0) or 0)
    if spam and spam_conf >= 90:
        return "REJECT / SPAM"
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
    """Explain the deterministic score in compact terms for future decisions."""
    items = []
    if analysis.get("qualification_status") == "QUALIFIED":
        items.append("Qualification +25")
    elif analysis.get("qualification_status") == "NOT CLEAR" and analysis.get("relevant_intent"):
        items.append("Relevant but unclear +10")
    if str(analysis.get("service_requested", "")).strip():
        items.append("Service +15")
    if analysis.get("qualification_info_present"):
        items.append("QualInfo +15")
    if analysis.get("location_or_eligibility_present"):
        items.append("Location +10")
    if analysis.get("clear_outcome"):
        items.append("Outcome +10")
    if analysis.get("two_way_conversation"):
        items.append("2Way +10")
    if not analysis.get("spam_robot"):
        items.append("NoSpam +5")
    if not analysis.get("major_qc_issue"):
        items.append("NoMajorQC +10")
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

    if str(analysis.get("insurance", "")).strip():
        parts.append(f"Insurance: {_clean_report_value(analysis.get('insurance'))}")
    if str(analysis.get("location", "")).strip():
        parts.append(f"Location: {_clean_report_value(analysis.get('location'))}")
    if str(analysis.get("outcome", "")).strip():
        parts.append(f"Outcome: {_clean_report_value(analysis.get('outcome'))}")

    parts.extend([
        f"Spam/Robot: {'YES' if analysis.get('spam_robot') else 'NO'}",
        f"Spam Confidence: {int(analysis.get('spam_confidence', 0) or 0)}%",
        f"QC Issue: {_clean_report_value(analysis.get('qc_issue'))}",
    ])

    return " | ".join(parts)


def calculate_call_quality_score(analysis):
    """Deterministic 0-100 score. AI supplies facts; Python supplies the final score.
    Rehab special rules:
    - Government/state insurance = never qualified.
    - No confirmed appointment = never 100.
    - High-confidence spam overrides everything."""
    
    score = 0

    qualification_status = analysis.get("qualification_status", "NOT CLEAR")
    if qualification_status == "QUALIFIED":
        score += 25
    elif qualification_status == "NOT CLEAR" and analysis.get("relevant_intent"):
        score += 10

    if str(analysis.get("service_requested", "")).strip():
        score += 15
    if analysis.get("qualification_info_present"):
        score += 15
    if analysis.get("location_or_eligibility_present"):
        score += 10
    if analysis.get("clear_outcome"):
        score += 10
    if analysis.get("two_way_conversation"):
        score += 10
    if not analysis.get("spam_robot"):
        score += 5
    if not analysis.get("major_qc_issue"):
        score += 10

    call_type = analysis.get("call_type", "OTHER")
    spam_confidence = int(analysis.get("spam_confidence", 0) or 0)
    campaign_category = str(analysis.get("campaign_category", "") or "").strip()

    # Strong spam is always a very low-quality call.
    if analysis.get("spam_robot") and spam_confidence >= 90:
        return 5
    if analysis.get("spam_robot"):
        return min(score, 25)

    if call_type == "WRONG NUMBER":
        return min(score, 30)
    if call_type == "SILENT / NO RESPONSE":
        return min(score, 15)
    if call_type in {"NON-QUALIFIED", "INFORMATION ONLY"}:
        score = min(score, 60)

    # Business rule: Rehab calls using government/state insurance are not qualified.
    # Keep the score as the single final source of row color, so this produces a
    # yellow-range score rather than applying a separate row color.
   if campaign_category == "Rehab & Addiction Treatment":

        rehab_status = get_rehab_insurance_status(analysis)

        # Government/state insurance is NEVER qualified.
        if rehab_status == "YELLOW":
            score = min(score, 60)

        # Private/commercial insurance can qualify,
        # but appointment is still required for a perfect score.
        if not analysis.get("appointment_set", False):
            # A Rehab call without a confirmed appointment
            # can NEVER receive 100.
            score = min(score, 99)
    # --------------------------------------------------------
    # GOVERNMENT / STATE INSURANCE
    # --------------------------------------------------------

    government_terms = [
        "medicaid",
        "medicare",
        "state insurance",
        "state-funded",
        "state funded",
        "state plan",
        "state program",
        "government insurance",
        "government-funded",
        "government funded",
        "government plan",
        "government program",
        "public insurance",
        "public plan",
        "public health insurance",
        "chip",
        "medi-cal",
        "medical",
        "county insurance",
        "county-funded",
        "county funded",
    ]

    if any(term in insurance for term in government_terms):
        return "YELLOW"

    # --------------------------------------------------------
    # PRIVATE / COMMERCIAL INSURANCE
    # --------------------------------------------------------

    private_terms = [
        "private",
        "employer",
        "employee",
        "commercial",
        "company insurance",
        "company plan",
        "group insurance",
        "group plan",
        "ppo",
        "hmo",
        "pos",
        "epo",
    ]

    if any(term in insurance for term in private_terms):
        return "GREEN"
        
    return max(0, min(100, score))


SCORE_COLORS = {
    "excellent": {"red": 0.56, "green": 0.83, "blue": 0.60},
    "good": {"red": 0.75, "green": 0.93, "blue": 0.78},
    "yellow": {"red": 1.00, "green": 0.93, "blue": 0.60},
    "orange": {"red": 1.00, "green": 0.78, "blue": 0.50},
    "poor": {"red": 1.00, "green": 0.60, "blue": 0.60},
    "spam": {"red": 1.00, "green": 0.35, "blue": 0.35},
}


def get_score_color(score, analysis):
    if analysis.get("spam_robot") and int(analysis.get("spam_confidence", 0) or 0) >= 90:
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


def sync_google_sheet_batch(default_campaign_name=""):
    """Process Ringba recordings from the shared Google Sheet.

    Sheet layout:
    D = Campaign
    G = Long AI Summary
    H = Main Topic / processing flag
    I = Recording URL
    J = Existing carrier / VOIP data (untouched)
    K = AI QC Report
    L = Numeric quality score
    """
    try:
        gc = get_google_client()
        sheet = gc.open("Ringba to Sheet QC")
        worksheet = sheet.worksheet("Sheet1")

        rows = worksheet.get_all_values()
        processed_count = 0

        for index, row in enumerate(rows[1:], start=2):
            raw_campaign = row[3].strip() if len(row) > 3 else ""
            raw_duration = row[5].strip() if len(row) > 5 else ""
            existing_main_topic = row[7].strip() if len(row) > 7 else ""
            recording_url = row[8].strip() if len(row) > 8 else ""

            # Keep the existing duration formatting behavior.
            if raw_duration and ":" not in raw_duration and "[" not in raw_duration:
                try:
                    worksheet.update_cell(index, 6, format_seconds_to_hms(raw_duration))
                    time.sleep(0.3)
                except Exception:
                    pass

            # Existing trigger stays: recording exists + H is empty.
            if not recording_url or not recording_url.startswith("http") or existing_main_topic:
                continue

            try:
                campaign_to_use = get_campaign_category(raw_campaign) if raw_campaign else default_campaign_name
                if not campaign_to_use:
                    campaign_to_use = "General Customer Inquiry"

                load_audio_url(recording_url)
                timeline_data, raw_text_segments = transcribe_groq_whisper(st.session_state.file_path)
                full_transcript_str = " ".join(raw_text_segments).strip()

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

                worksheet.update_cell(index, 7, detailed_summary)  # G
                worksheet.update_cell(index, 8, main_topic)        # H
                worksheet.update_cell(index, 11, qc_report)        # K
                worksheet.update_cell(index, 12, score)             # L

                apply_row_score_color(
                    worksheet,
                    index,
                    score,
                    analysis,
                )

                processed_count += 1
                time.sleep(1)

            except Exception as e:
                error_text = f"Processing error: {str(e)}"
                try:
                    worksheet.update_cell(index, 7, error_text)
                    worksheet.update_cell(index, 11, error_text)
                except Exception:
                    pass

        return True, f"Successfully processed {processed_count} new recordings!"
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
            success, message = sync_google_sheet_batch(selected_campaign)
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
