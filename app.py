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
# CONFIG HELPER
# ============================================================

class ConfigHelper:
    """Centralized configuration manager for secrets, models, and application paths."""
    
    @staticmethod
    def secret(name, default=""):
        try:
            value = st.secrets.get(name, default)
            return value if value is not None else default
        except Exception:
            return default

    @classmethod
    def load_api_keys(cls):
        return {
            "GROQ_API_KEY": cls.secret("Aunty_NEXT_DOOR_API_PRIMARY") or cls.secret("GROQ_API_KEY"),
            "GROQ_SECONDARY_API_KEY": cls.secret("GROQ_API_KEY_SECONDARY_2") or cls.secret("GROQ_SECONDARY_API_KEY"),
            "GROQ_API_KEY_3": cls.secret("GROQ_API_KEY_3"),
            "GROQ_API_KEY_4": cls.secret("GROQ_API_KEY_4"),
            "GEMINI_API_KEY": cls.secret("GEMINI_API_KEY"),
            "CEREBRAS_API_KEY": cls.secret("CEREBRAS_API_KEY"),
            "OPENROUTER_API_KEY": cls.secret("OPENROUTER_API_KEY"),
            "MISTRAL_API_KEY": cls.secret("MISTRAL_API_KEY"),
            "TOGETHER_API_KEY": cls.secret("TOGETHER_API_KEY"),
            "COHERE_API_KEY": cls.secret("COHERE_API_KEY"),
        }

    @classmethod
    def get_provider_models(cls):
        summary_model = "openai/gpt-oss-20b"
        return {
            "Groq-1": summary_model,
            "Groq-2": summary_model,
            "Groq-3": summary_model,
            "Groq-4": summary_model,
            "Gemini": "gemini-2.5-flash",
            "Cerebras": "gpt-oss-120b",
            "OpenRouter": "openai/gpt-oss-20b:free",
            "Mistral": "mistral-small-latest",
            "Together": "openai/gpt-oss-20b",
            "Cohere": "command-a-03-2025",
        }


# ============================================================
# SHEET HELPER
# ============================================================

class SheetHelper:
    """Encapsulates Google Sheets connection, metadata retrieval, formatting, and batch sync."""

    @staticmethod
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

    @staticmethod
    def _background_is_special(background):
        if not background:
            return False

        color = background.get("rgbColor", background)
        if not isinstance(color, dict):
            return False

        r = 1.0 if color.get("red") is None else float(color.get("red", 1.0))
        g = 1.0 if color.get("green") is None else float(color.get("green", 1.0))
        b = 1.0 if color.get("blue") is None else float(color.get("blue", 1.0))

        return not (r >= 0.97 and g >= 0.97 and b >= 0.97)

    @classmethod
    def get_existing_special_columns(cls, worksheet, start_row, end_row):
        """Read user-entered background colors so existing VOIP/special colors survive score coloring."""
        if end_row < start_row:
            return {}

        try:
            metadata = worksheet.spreadsheet.fetch_sheet_metadata(
                params={
                    "includeGridData": True,
                    "ranges": [f"'{worksheet.title}'!A{start_row}:L{end_row}"],
                    "fields": "sheets/data/startRow,sheets/data/rowData/values/userEnteredFormat/backgroundColor,sheets/data/rowData/values/userEnteredFormat/backgroundColorStyle",
                }
            )
        except Exception:
            return {}

        special_by_row = {}

        for sheet_data in metadata.get("sheets", []):
            for grid_data in sheet_data.get("data", []):
                base_row = int(grid_data.get("startRow", start_row - 1)) + 1
                for offset, row_data in enumerate(grid_data.get("rowData", [])):
                    row_number = base_row + offset
                    special_columns = set()

                    for col_number, cell in enumerate(row_data.get("values", []), start=1):
                        user_format = cell.get("userEnteredFormat", {})
                        background = user_format.get("backgroundColor")
                        background_style = user_format.get("backgroundColorStyle")
                        if cls._background_is_special(background) or cls._background_is_special(background_style):
                            special_columns.add(col_number)

                    special_by_row[row_number] = special_columns

        return special_by_row

    @staticmethod
    def _column_letter(number):
        result = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            result = chr(65 + remainder) + result
        return result

    @classmethod
    def _contiguous_ranges_for_row(cls, row_number, columns):
        if not columns:
            return []

        columns = sorted(set(columns))
        ranges = []
        start_col = previous_col = columns[0]

        for col in columns[1:]:
            if col == previous_col + 1:
                previous_col = col
                continue

            ranges.append(f"{cls._column_letter(start_col)}{row_number}:{cls._column_letter(previous_col)}{row_number}")
            start_col = previous_col = col

        ranges.append(f"{cls._column_letter(start_col)}{row_number}:{cls._column_letter(previous_col)}{row_number}")
        return ranges

    @classmethod
    def apply_row_score_color(cls, worksheet, row_number, score, analysis, special_columns=None):
        """Color A:L by score while preserving existing special colors, except high-confidence spam is fully red."""
        special_columns = set(special_columns or [])
        color = get_score_color(score, analysis)

        high_confidence_spam = analysis.get("spam_robot") and int(analysis.get("spam_confidence", 0) or 0) >= 90

        if high_confidence_spam:
            worksheet.format(
                f"A{row_number}:L{row_number}",
                {"backgroundColor": SCORE_COLORS["spam"]}
            )
            return

        columns_to_color = [col for col in range(1, 13) if col not in special_columns and col != 12]
        ranges = cls._contiguous_ranges_for_row(row_number, columns_to_color)
        ranges.append(f"L{row_number}")

        if ranges:
            worksheet.format(ranges, {"backgroundColor": color})

    @classmethod
    def sync_google_sheet_batch(cls, default_campaign_name="", transcribe_callback=None, analyze_callback=None, score_callback=None, qc_report_callback=None):
        """Process Ringba recordings from the shared Google Sheet."""
        try:
            gc = cls.get_google_client()
            sheet = gc.open("Ringba to Sheet QC")
            worksheet = sheet.worksheet("Sheet1")

            rows = worksheet.get_all_values()
            processed_count = 0

            if len(rows) > 1:
                special_color_map = cls.get_existing_special_columns(worksheet, 2, len(rows))
            else:
                special_color_map = {}

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

                    main_topic = analysis["main_topic"]
                    detailed_summary = analysis["long_summary"]
                    qc_report = build_qc_report(analysis)
                    score = calculate_call_quality_score(analysis)

                    worksheet.update_cell(index, 7, detailed_summary)  # G
                    worksheet.update_cell(index, 8, main_topic)        # H
                    worksheet.update_cell(index, 11, qc_report)        # K
                    worksheet.update_cell(index, 12, score)             # L

                    cls.apply_row_score_color(
                        worksheet,
                        index,
                        score,
                        analysis,
                        special_color_map.get(index, set()),
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

