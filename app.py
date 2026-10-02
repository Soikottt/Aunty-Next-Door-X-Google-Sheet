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
from streamlit_cookies_controller import CookieController


# ============================================================
# DATABASE & AUTHENTICATION SETUP (SQLite)
# ============================================================

DB_PATH = Path("users.db")

# SMTP Configuration (Set environment variables or st.secrets for actual email delivery)
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")  # Sender Email
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")  # Sender App Password

def init_db():
    """Create users and reset_tokens tables if they don't exist and run auto-migration."""
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

    # Migration check: Ensure 'is_admin' column exists if DB existed prior
    c.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in c.fetchall()]
    if "is_admin" not in columns:
        c.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        conn.commit()

    # Make sure at least one default admin exists if user table is fresh
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
    """Hash password using SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(name: str, email: str, password: str, is_admin: int = 0):
    """Register a new public or admin user."""
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
    """Authenticate logging in user."""
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
    """Update user information in database."""
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

# ============================================================
# ADMIN PANEL DATABASE HELPERS
# ============================================================

def get_all_users():
    """Retrieve all registered users for admin panel."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, email, is_admin FROM users ORDER BY id ASC")
    users = c.fetchall()
    conn.close()
    return [{"id": u[0], "name": u[1], "email": u[2], "is_admin": bool(u[3])} for u in users]

def admin_toggle_role(user_id: int, make_admin: bool):
    """Grant or revoke admin access for a user."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_admin = ? WHERE id = ?", (1 if make_admin else 0, user_id))
    conn.commit()
    conn.close()

def admin_reset_password(user_id: int, new_password: str):
    """Force reset a user password from admin panel."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    pwd_hash = hash_password(new_password)
    c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pwd_hash, user_id))
    conn.commit()
    conn.close()

def admin_delete_user(user_id: int):
    """Delete user from database."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

def send_reset_code_email(email: str, code: str):
    """Sends the reset code via SMTP email."""
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print(f"[TESTING MODE] Reset Code for {email}: {code}")
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
    """Generates a 6-digit reset code, saves it to DB, and emails it."""
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
    """Verifies reset code and updates password."""
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

# Initialize DB
init_db()


def render_html(content):
    """Render HTML/CSS without Markdown's code-block indentation rules."""
    st.html(textwrap.dedent(content))


# ============================================================
# HELPER: JS CLIPBOARD COPY BUTTON
# ============================================================

def render_copy_icon_button(text_to_copy, button_id):
    """Renders a browser-native Javascript copy button matching standard full-width primary buttons."""
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

# Initialize Cookie Controller for Remember Me persistence
cookie_controller = CookieController()


# ============================================================
# UPLOAD CONFIGURATION
# ============================================================

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


# ============================================================
# GROQ API KEYS (SECURE ST.SECRETS RETRIEVAL)
# ============================================================

GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", "")
GROQ_SECONDARY_API_KEY = st.secrets.get("GROQ_SECONDARY_API_KEY", "")

if not GROQ_API_KEY:
    st.warning("GROQ_API_KEY is not configured in Streamlit Secrets.")


# ============================================================
# ACTIVE SESSION STORAGE & COOKIE RESTORATION
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

# Check browser cookies for persistent login if session is empty
if st.session_state["logged_in_user"] is None:
    saved_cookie_user = cookie_controller.get("logged_in_user")
    if saved_cookie_user:
        try:
            user_data = json.loads(saved_cookie_user)
            st.session_state["logged_in_user"] = user_data
        except Exception:
            pass


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

**INSURANCE EXTRACTION RULE:**

Always include the caller's clearly stated insurance name and type in the summary when discussed.

* Include the exact insurance provider or plan name, such as HealthPartners, Blue Cross, Aetna, or Medicaid.
* Identify the insurance type or source when clearly stated, such as private insurance, employer-sponsored, spouse's employer plan, Medicare, or Medicaid.
* If the caller says the insurance is through a spouse's employer, include that detail.
* Do not assume the insurance type if it is not confirmed.
* If insurance is discussed but the name or type is unclear, include only the confirmed information.
* Never omit clearly stated insurance details just to make the summary shorter.

**Example:** "The caller confirmed he has HealthPartners private insurance through his wife's employer."

Write ONE short paragraph only. No bullets, headings, or sections. Use **bold text** only for the most important treatment, Inusrance name if mentioned, service, action, or outcome.

Keep the final summary very short, factual, natural, conversational and included insurance name, like a human-written QC note.
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
Return only the final QC summary paragraph in simple, natural English, using basic words and correct grammar.

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
        raise RuntimeError("Groq API key not found.")

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
                    "speaker": "Speaker",
                    "line": text_value,
                })
                raw_text_segments.append(text_value)
    else:
        full_text = getattr(transcription, "text", "").strip()
        if full_text:
            timeline_data.append({"time": "00:00 – 00:00", "speaker": "Speaker", "line": full_text})
            raw_text_segments.append(full_text)

    return timeline_data, raw_text_segments

GROQ_SUMMARY_MODEL = "openai/gpt-oss-20b"


def generate_summaries_groq(full_transcript, campaign_name):
    client_primary = Groq(api_key=GROQ_API_KEY)
    client_secondary = Groq(api_key=GROQ_SECONDARY_API_KEY) if GROQ_SECONDARY_API_KEY else client_primary
    
    if campaign_name in CAMPAIGN_QC_QUESTIONS:
        qc_questions = CAMPAIGN_QC_QUESTIONS[campaign_name]
    else:
        qc_questions = "Verify the main intent of the call, customer questions, and final outcome."

    full_prompt = QC_SYSTEM_PROMPT.format(
        campaign_name=campaign_name,
        qc_questions=qc_questions,
        call_transcript=full_transcript
    )

    detailed_summary = "Failed to generate summary."
    short_topic = "Failed to generate topic."

    # --- STEP 1: DETAILED SUMMARY WITH FAILOVER ---
    try:
        response_detailed = client_primary.chat.completions.create(
            model=GROQ_SUMMARY_MODEL,
            messages=[{"role": "user", "content": full_prompt}],
            temperature=0.2,
            max_tokens=450,
            reasoning_effort="low"
        )
        detailed_summary = response_detailed.choices[0].message.content.replace("*", "").strip()
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "rate_limit" in error_str.lower():
            try:
                response_detailed = client_secondary.chat.completions.create(
                    model=GROQ_SUMMARY_MODEL,
                    messages=[{"role": "user", "content": full_prompt}],
                    temperature=0.2,
                    max_tokens=450,
                    reasoning_effort="low"
                )
                detailed_summary = response_detailed.choices[0].message.content.replace("*", "").strip()
            except Exception as e2:
                detailed_summary = f"Error generating summary (Rate Limit): {str(e2)}"
        else:
            detailed_summary = f"Error generating summary: {str(e)}"

    # --- STEP 2: SHORT TOPIC SUMMARY ---
    topic_prompt = SHORT_TOPIC_PROMPT.format(call_transcript=detailed_summary)
    try:
        response_topic = client_primary.chat.completions.create(
            model=GROQ_SUMMARY_MODEL,
            messages=[{"role": "user", "content": topic_prompt}],
            temperature=0.2,
            max_tokens=60,
            reasoning_effort="low"
        )
        short_topic = response_topic.choices[0].message.content.replace("*", "").strip()
    except Exception:
        short_topic = detailed_summary[:100] + "..."

    return detailed_summary, short_topic
