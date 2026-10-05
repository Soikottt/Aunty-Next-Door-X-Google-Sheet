import ipaddress
import socket
import logging
import json
import os
import re
from pathlib import Path
import textwrap
import uuid
import time
import urllib.request
import urllib.parse
import html

import streamlit as st
import streamlit.components.v1 as components
from groq import Groq
from pydub import AudioSegment
import gspread
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("aunty_next_door")


# ============================================================
# CONFIG HELPERS
# ============================================================

def get_secret(name, default=""):
    """Read a setting from environment variables first, then Streamlit Secrets."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        value = st.secrets.get(name, "")
        if value:
            return str(value)
    except Exception:
        pass
    return default


def _get_int_setting(name, default):
    try:
        return int(get_secret(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


MAX_AUDIO_MB = _get_int_setting("MAX_AUDIO_MB", 25)  # Groq Whisper rejects files above its size limit
SHEET_NAME = "Ringba to Sheet QC"
WORKSHEET_NAME = "Sheet1"


# ============================================================
# SHEET HELPERS
# ============================================================

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
                    btn.style.boxShadow = '0 4px 12px rgba(255, 77, 77, 0.25)';
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

def _discover_groq_keys():
    """Primary first, then secondary, then any other Groq key found in Secrets/env (tertiary, etc.)."""
    found = []

    def add(value, require_prefix=False):
        value = str(value or "").strip()
        if value and value not in found and (not require_prefix or value.startswith("gsk_")):
            found.append(value)

    add(get_secret("Aunty_NEXT_DOOR_API_PRIMARY") or get_secret("GROQ_API_KEY"))
    add(get_secret("GROQ_API_KEY_SECONDARY_2") or get_secret("GROQ_SECONDARY_API_KEY"))

    names = set(os.environ.keys())
    try:
        names |= set(st.secrets.keys())
    except Exception:
        pass
    for name in sorted(names):
        upper = name.upper()
        if ("GROQ" in upper and "KEY" in upper) or upper.startswith("AUNTY_NEXT_DOOR_API"):
            add(get_secret(name), require_prefix=True)
    return found


GROQ_API_KEYS = _discover_groq_keys()
GROQ_API_KEY = GROQ_API_KEYS[0] if GROQ_API_KEYS else ""
GROQ_SECONDARY_API_KEY = GROQ_API_KEYS[1] if len(GROQ_API_KEYS) > 1 else ""

if not GROQ_API_KEY:
    st.warning("Primary Groq API key is not configured in Streamlit Secrets.")


# ============================================================
# ACTIVE SESSION STORAGE
# ============================================================

DEFAULT_SESSION = {
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

class GroqRateLimitError(RuntimeError):
    """Raised when every Groq key is rate limited. wait_seconds = shortest wait before a retry can work."""

    def __init__(self, wait_seconds, message="Groq rate limit reached on all API keys."):
        super().__init__(message)
        self.wait_seconds = float(wait_seconds)


_WAIT_PART_RE = re.compile(r"([\d.]+)(h|ms|m|s)")


def _rate_limit_wait(exc):
    """Seconds to wait if exc is a Groq rate-limit (429) error, else None."""
    text = str(exc)
    if getattr(exc, "status_code", None) != 429 and "rate_limit_exceeded" not in text and "Error code: 429" not in text:
        return None

    match = re.search(r"try again in\s+([0-9hms.]+)", text, re.I)
    if not match:
        return 300.0

    unit_seconds = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    total = sum(float(num) * unit_seconds[unit] for num, unit in _WAIT_PART_RE.findall(match.group(1)))
    return total or 300.0


def _short_error(exc, limit=160):
    if _rate_limit_wait(exc) is not None:
        return "rate limit (429)"
    text = " ".join(str(exc).split())
    return text[:limit] + ("..." if len(text) > limit else "")


def transcribe_groq_whisper(audio_file_path):
    if not GROQ_API_KEYS:
        raise RuntimeError("Groq API key not found. Configure Aunty_NEXT_DOOR_API_PRIMARY.")

    with open(audio_file_path, "rb") as file:
        audio_bytes = file.read()
    file_name = os.path.basename(audio_file_path)

    transcription = None
    errors, waits = [], []
    for key in GROQ_API_KEYS:
        try:
            transcription = Groq(api_key=key).audio.transcriptions.create(
                file=(file_name, audio_bytes),
                model="whisper-large-v3-turbo",
                response_format="verbose_json",
                language="en",
            )
            break
        except Exception as exc:
            wait = _rate_limit_wait(exc)
            if wait is not None:
                waits.append(wait)
            errors.append(_short_error(exc))

    if transcription is None:
        if waits and len(waits) == len(errors):
            raise GroqRateLimitError(min(waits), "Groq Whisper rate limit reached on all API keys.")
        raise RuntimeError("Whisper transcription failed: " + " | ".join(errors))

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


# ------------------------------------------------------------
# AI CALL ANALYSIS (token-optimised)
# ------------------------------------------------------------
# Token budget per call is roughly: ~450 instruction tokens + ~250 schema tokens
# + transcript (capped) + <=800 output tokens.  Typical total: 2,000-4,500 tokens.

GROQ_SUMMARY_MODEL = "openai/gpt-oss-20b"
# 0 (default) = always send the FULL transcript, however long. Set e.g. 9000 in Secrets to cap very long calls.
MAX_TRANSCRIPT_CHARS = _get_int_setting("MAX_TRANSCRIPT_CHARS", 0)
ANALYSIS_MAX_OUTPUT_TOKENS = _get_int_setting("ANALYSIS_MAX_OUTPUT_TOKENS", 800)
MIN_WORDS_FOR_AI = 12   # shorter transcripts are handled without calling the AI at all
# Set USE_FULL_QC_QUESTIONS=1 to send your long original QC question text instead of the compact guidance.
USE_FULL_QC_QUESTIONS = get_secret("USE_FULL_QC_QUESTIONS", "0").strip().lower() in {"1", "true", "yes"}

ANALYSIS_GUIDANCE = {
    "Rehab & Addiction Treatment": (
        "Reason for calling; rehab/treatment service wanted; location or ZIP; insurance ONLY if the CALLER clearly "
        "states it (name the provider, or say they have none); appointment only if clearly scheduled; "
        "what the agent provided; how the call ended."
    ),
    "Dumpster & Porta Potty Services": (
        "Dumpster or porta potty; size/capacity; residential/commercial/event use; price discussed; "
        "delivery address/date; phone given; booking confirmed."
    ),
    "Pest Control & Home Services": (
        "Specific pest/home service; homeowner or renter; service area; quote vs service vs wrong number; "
        "appointment scheduled; how the call ended; agent handling."
    ),
    "Insurance (Health / Auto / Home)": (
        "Coverage type (health/Medicare/auto/home); current coverage; new policy, quote or existing policy; "
        "qualification (age, state); wrong number; ending (transfer/quote/callback)."
    ),
    "Debt Relief & Financial Services": (
        "Type of debt help; total debt amount; secured vs unsecured; wrong number/spam; "
        "transferred/enrolled/consultation; how the call ended."
    ),
}
DEFAULT_ANALYSIS_GUIDANCE = (
    "Why they called; service or information sought; qualification and location; price, quote, appointment "
    "or transfer discussed; relevance (wrong number/unrelated); caller objections; agent mistakes; how it ended."
)


def get_analysis_guidance(campaign_name):
    if USE_FULL_QC_QUESTIONS:
        return get_qc_questions(campaign_name)
    return ANALYSIS_GUIDANCE.get(campaign_name, DEFAULT_ANALYSIS_GUIDANCE)


_BASE_ANALYSIS_PROPERTIES = {
    "long_summary": {"type": "string"},
    "main_topic": {"type": "string"},
    "call_type": {
        "type": "string",
        "enum": ["QUALIFIED", "NON-QUALIFIED", "WRONG NUMBER", "SPAM / ROBOT", "INFORMATION ONLY", "SILENT / NO RESPONSE", "OTHER"],
    },
    "qualification_status": {"type": "string", "enum": ["QUALIFIED", "NON-QUALIFIED", "NOT CLEAR"]},
    "caller_intent": {"type": "string"},
    "why_called": {"type": "string"},
    "service_requested": {"type": "string"},
    "insurance": {"type": "string"},
    "location": {"type": "string"},
    "outcome": {"type": "string"},
    "spam_robot": {"type": "boolean"},
    "spam_confidence": {"type": "integer"},
    "qc_issue": {"type": "string"},
    "relevant_intent": {"type": "boolean"},
    "qualification_info_present": {"type": "boolean"},
    "location_or_eligibility_present": {"type": "boolean"},
    "clear_outcome": {"type": "boolean"},
    "two_way_conversation": {"type": "boolean"},
    "major_qc_issue": {"type": "boolean"},
}


def _build_schema(include_speakers):
    properties = dict(_BASE_ANALYSIS_PROPERTIES)
    if include_speakers:
        properties["speaker_labels"] = {"type": "string"}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


def _shorten_transcript(text):
    """Keep the opening and the ending of very long calls; that is where QC facts usually are."""
    text = text.strip()
    if MAX_TRANSCRIPT_CHARS <= 0 or len(text) <= MAX_TRANSCRIPT_CHARS:
        return text
    head = int(MAX_TRANSCRIPT_CHARS * 0.65)
    tail = MAX_TRANSCRIPT_CHARS - head
    return f"{text[:head]} [...middle of call omitted...] {text[-tail:]}"


def _analysis_prompt(transcript_text, campaign_name, numbered_segments):
    speaker_rule = ""
    if numbered_segments:
        speaker_rule = (
            "\n- speaker_labels: one comma-separated letter per numbered line, in order "
            "(A=Agent, C=Caller, U=unsure), e.g. \"A,C,C,A\". Exactly one letter per line."
        )

    return f"""Call QC analyst for a pay-per-call network.
Campaign: {campaign_name}
Focus on: {get_analysis_guidance(campaign_name)}

The transcript is machine speech-to-text (errors possible, no speaker labels). Infer Agent vs Caller from context. An agent's question is never the caller's answer. Use only facts clearly stated; never guess. Use "" for anything not clearly discussed.

Return JSON:
- long_summary: 2-4 plain sentences, no markdown: real reason for calling, service wanted, key qualification facts, insurance only if the caller stated it, and the outcome. No names, phone numbers or addresses.
- main_topic: one sentence (5-15 words): what the caller actually called about; say so if unrelated.
- call_type / qualification_status: QUALIFIED only if campaign requirements are clearly met; NON-QUALIFIED if relevant but fails them; NOT CLEAR if unknown.
- caller_intent, why_called, service_requested, insurance, location, outcome: max 12 words each.
- spam_robot: true only with strong evidence (press-0/9 scripts, Google listing/SEO pitch, fake verification, marketing robot). An irrelevant human is not spam. spam_confidence 0-100.
- qc_issue: a real issue (wrong number, agent mistake, objection, silence, solicitation) or "".
- Booleans: relevant_intent, qualification_info_present, location_or_eligibility_present, clear_outcome, two_way_conversation, major_qc_issue.{speaker_rule}

TRANSCRIPT:
{transcript_text}"""


def _call_structured_analysis(client, prompt, schema, max_tokens=None):
    response = client.chat.completions.create(
        model=GROQ_SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": "Return only the structured call-analysis object. Do not add commentary."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=max_tokens or ANALYSIS_MAX_OUTPUT_TOKENS,
        reasoning_effort="low",
        reasoning_format="hidden",
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "call_qc_analysis", "strict": True, "schema": schema},
        },
    )
    content = response.choices[0].message.content or "{}"
    return json.loads(content)


def _trivial_analysis(text):
    """Calls with (almost) no speech do not need an AI call at all."""
    text = text.strip()
    if text:
        summary = f'The call had almost no speech. The transcript only shows: "{text}".'
    else:
        summary = "The call had dead air with no response from either side."
    return {
        "long_summary": summary,
        "main_topic": "No meaningful conversation on the call",
        "call_type": "SILENT / NO RESPONSE",
        "qualification_status": "NOT CLEAR",
        "caller_intent": "", "why_called": "", "service_requested": "",
        "insurance": "", "location": "", "outcome": "",
        "spam_robot": False, "spam_confidence": 0,
        "qc_issue": "Very little or no speech on the call",
        "relevant_intent": False, "qualification_info_present": False,
        "location_or_eligibility_present": False, "clear_outcome": False,
        "two_way_conversation": False, "major_qc_issue": True,
    }


def generate_call_analysis_groq(full_transcript, campaign_name, timeline_data=None, include_speakers=False):
    """include_speakers=True labels each transcript line Agent/Caller (UI only; costs a few extra tokens)."""
    if not full_transcript.strip():
        raise RuntimeError("No transcription text was available for AI analysis.")

    if not GROQ_API_KEYS:
        raise RuntimeError("Primary Groq API key is not configured.")

    if len(full_transcript.split()) < MIN_WORDS_FOR_AI:
        return _trivial_analysis(full_transcript)

    numbered = False
    if include_speakers and timeline_data:
        lines = [item.get("line", "").strip() for item in timeline_data if item.get("line", "").strip()]
        numbered_text = "\n".join(f"{i}: {line}" for i, line in enumerate(lines, start=1))
        if (MAX_TRANSCRIPT_CHARS <= 0 or len(numbered_text) <= MAX_TRANSCRIPT_CHARS) and len(lines) == len(timeline_data):
            transcript_text = numbered_text
            numbered = True
    if not numbered:
        transcript_text = _shorten_transcript(full_transcript)

    prompt = _analysis_prompt(transcript_text, campaign_name, numbered)
    schema = _build_schema(numbered)
    output_limit = ANALYSIS_MAX_OUTPUT_TOKENS + (3 * len(timeline_data) if numbered else 0)

    analysis = None
    errors, waits = [], []
    for key in GROQ_API_KEYS:
        try:
            analysis = _call_structured_analysis(Groq(api_key=key), prompt, schema, output_limit)
            break
        except Exception as exc:
            wait = _rate_limit_wait(exc)
            if wait is not None:
                waits.append(wait)
            errors.append(_short_error(exc))

    if analysis is None:
        if waits and len(waits) == len(errors):
            raise GroqRateLimitError(min(waits), "Groq daily/minute token limit reached on all API keys.")
        raise RuntimeError("All Groq analysis API keys failed: " + " | ".join(errors))

    if numbered and timeline_data is not None:
        labels = [p.strip().upper() for p in str(analysis.get("speaker_labels", "")).split(",") if p.strip()]
        names = {"A": "Agent", "C": "Caller"}
        if len(labels) == len(timeline_data):
            for item, label in zip(timeline_data, labels):
                item["speaker"] = names.get(label[:1], "Unknown")
    analysis.pop("speaker_labels", None)

    for field in ("main_topic", "long_summary", "qc_issue"):
        analysis[field] = str(analysis.get(field, "")).replace("*", "").strip()
    return analysis


def generate_summaries_groq(full_transcript, campaign_name, timeline_data=None):
    """Compatibility wrapper for the existing UI: returns (main_topic, long_summary)."""
    analysis = generate_call_analysis_groq(
        full_transcript,
        get_campaign_category(campaign_name),
        timeline_data=timeline_data,
        include_speakers=True,
    )
    return analysis["main_topic"], analysis["long_summary"]


def build_qc_report(analysis):
    parts = [
        f"Call Type: {analysis.get('call_type', 'OTHER')}",
        f"Caller Intent: {analysis.get('caller_intent', '').strip()}",
        f"Why They Called: {analysis.get('why_called', '').strip()}",
        f"Treatment/Service Interest: {analysis.get('service_requested', '').strip()}",
    ]

    if analysis.get("insurance", "").strip():
        parts.append(f"Insurance: {analysis['insurance'].strip()}")
    if analysis.get("location", "").strip():
        parts.append(f"Location: {analysis['location'].strip()}")
    if analysis.get("outcome", "").strip():
        parts.append(f"Outcome: {analysis['outcome'].strip()}")

    parts.extend([
        f"Spam/Robot: {'YES' if analysis.get('spam_robot') else 'NO'}",
        f"Spam Confidence: {int(analysis.get('spam_confidence', 0))}%",
        f"QC Issue: {analysis.get('qc_issue', '').strip() or 'None'}",
    ])

    return " | ".join(part for part in parts if part.split(": ", 1)[-1].strip())


def calculate_call_quality_score(analysis):
    """Deterministic 0-100 score. AI supplies facts; Python supplies the score."""
    score = 0

    qualification_status = analysis.get("qualification_status", "NOT CLEAR")
    if qualification_status == "QUALIFIED":
        score += 25
    elif qualification_status == "NOT CLEAR" and analysis.get("relevant_intent"):
        score += 10

    if analysis.get("service_requested", "").strip():
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

    if analysis.get("spam_robot") and spam_confidence >= 90:
        return 5
    if analysis.get("spam_robot"):
        return min(score, 25)
    if call_type == "WRONG NUMBER":
        return min(score, 30)
    if call_type == "SILENT / NO RESPONSE":
        return min(score, 15)
    if call_type in {"NON-QUALIFIED", "INFORMATION ONLY"}:
        return min(score, 60)

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


def _background_is_special(background):
    if not background:
        return False

    color = background.get("rgbColor", background)
    if not isinstance(color, dict):
        return False

    r = 1.0 if color.get("red") is None else float(color.get("red", 1.0))
    g = 1.0 if color.get("green") is None else float(color.get("green", 1.0))
    b = 1.0 if color.get("blue") is None else float(color.get("blue", 1.0))

    # Treat plain/near-white as the normal background.
    return not (r >= 0.97 and g >= 0.97 and b >= 0.97)


def get_existing_special_columns(worksheet, start_row, end_row):
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
                    if _background_is_special(background) or _background_is_special(background_style):
                        special_columns.add(col_number)

                special_by_row[row_number] = special_columns

    return special_by_row


def _column_letter(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _contiguous_ranges_for_row(row_number, columns):
    if not columns:
        return []

    columns = sorted(set(columns))
    ranges = []
    start_col = previous_col = columns[0]

    for col in columns[1:]:
        if col == previous_col + 1:
            previous_col = col
            continue

        ranges.append(f"{_column_letter(start_col)}{row_number}:{_column_letter(previous_col)}{row_number}")
        start_col = previous_col = col

    ranges.append(f"{_column_letter(start_col)}{row_number}:{_column_letter(previous_col)}{row_number}")
    return ranges


def apply_row_score_color(worksheet, row_number, score, analysis, special_columns=None):
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

    # L (score) always gets the score color, even if it previously had a color.
    columns_to_color = [col for col in range(1, 13) if col not in special_columns and col != 12]
    ranges = _contiguous_ranges_for_row(row_number, columns_to_color)
    ranges.append(f"L{row_number}")

    if ranges:
        worksheet.format(ranges, {"backgroundColor": color})

ALLOWED_AUDIO_EXTS = {".mp3", ".wav"}
UPLOAD_MAX_AGE_SEC = 6 * 3600


def cleanup_old_uploads(max_age_sec=UPLOAD_MAX_AGE_SEC):
    """Delete old audio files so the uploads folder cannot fill the disk."""
    cutoff = time.time() - max_age_sec
    try:
        for file in UPLOAD_DIR.iterdir():
            try:
                if file.is_file() and file.stat().st_mtime < cutoff:
                    file.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _host_is_public(hostname):
    """True only if every address the hostname resolves to is a public IP."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def validate_audio_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only http(s) recording URLs are supported.")
    if not _host_is_public(parsed.hostname):
        raise ValueError("That URL points to a private or unreachable address.")


def download_audio(url):
    """Download a recording to uploads/. Pure function: no Streamlit session state, safe for threads.

    Returns (Path, original_filename). The caller is responsible for deleting the file.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("URL is required.")

    validate_audio_url(url)
    cleanup_old_uploads()

    filename = url.split("/")[-1].split("?")[0] or "web_audio.mp3"
    ext = Path(filename).suffix.lower()
    path = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext if ext in ALLOWED_AUDIO_EXTS else '.mp3'}"

    max_bytes = MAX_AUDIO_MB * 1024 * 1024
    too_big = f"Recording is larger than the {MAX_AUDIO_MB} MB limit."

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            final_host = urllib.parse.urlparse(response.geturl()).hostname
            if final_host and not _host_is_public(final_host):
                raise ValueError("The recording redirected to a private address.")

            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ValueError(too_big)

            total = 0
            with open(path, "wb") as out_file:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(too_big)
                    out_file.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise

    return path, filename


def save_uploaded_audio(uploaded_file):
    ext = Path(uploaded_file.name).suffix.lower()
    if ext not in ALLOWED_AUDIO_EXTS:
        raise ValueError("Only MP3 and WAV are supported.")

    if uploaded_file.size > MAX_AUDIO_MB * 1024 * 1024:
        raise ValueError(f"File is larger than the {MAX_AUDIO_MB} MB limit.")

    cleanup_old_uploads()
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
    """Interactive (UI) loader: downloads the recording and stores it in the user's session."""
    path, filename = download_audio(url)

    try:
        sound = AudioSegment.from_file(path)
        duration_sec = len(sound) / 1000.0
    except Exception:
        path.unlink(missing_ok=True)
        raise

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


PROCESSING_MARKER = "⏳ Processing..."
ERROR_MARKER = "ERROR - clear this cell to retry"
MAX_ROW_ATTEMPTS = 3
WATCHER_INTERVAL_SEC = 30


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _process_sheet_locked(shared, fallback_campaign):
    """One pass over the sheet. Caller must hold shared['lock'].

    fallback_campaign: campaign to use for rows with a blank Campaign cell.
    None means rows without a campaign are skipped (background worker behaviour).
    """
    attempts = shared["attempts"]
    gc = get_google_client()
    worksheet = gc.open(SHEET_NAME).worksheet(WORKSHEET_NAME)
    rows = worksheet.get_all_values()

    if len(rows) < 2:
        return 0

    # 1) Duration cleanup (seconds -> H:MM:SS) in batched writes instead of one call per cell.
    duration_updates = []
    pending = []

    for index, row in enumerate(rows[1:], start=2):
        raw_campaign = row[3].strip() if len(row) > 3 else ""
        raw_duration = row[5].strip() if len(row) > 5 else ""
        existing_main_topic = row[7].strip() if len(row) > 7 else ""
        recording_url = row[8].strip() if len(row) > 8 else ""

        if raw_duration and ":" not in raw_duration and "[" not in raw_duration:
            formatted = format_seconds_to_hms(raw_duration)
            if formatted != raw_duration:
                duration_updates.append({"range": f"F{index}", "values": [[formatted]]})

        if not recording_url.startswith("http") or existing_main_topic:
            continue

        if raw_campaign:
            campaign_name = get_campaign_category(raw_campaign)
        elif fallback_campaign is not None:
            campaign_name = fallback_campaign or "General Customer Inquiry"
        else:
            continue

        pending.append((index, recording_url, campaign_name))

    for batch in _chunks(duration_updates, 200):
        try:
            worksheet.batch_update(batch, value_input_option="USER_ENTERED")
        except Exception as exc:
            logger.warning("Duration cleanup failed: %s", exc)

    if not pending:
        return 0

    special_color_map = get_existing_special_columns(worksheet, 2, len(rows))
    processed = 0

    for index, recording_url, campaign_name in pending:
        # Re-read the row: if someone sorted/inserted rows meanwhile, do not write into the wrong row.
        try:
            current = worksheet.row_values(index)
        except Exception as exc:
            logger.warning("Could not re-read row %s: %s", index, exc)
            continue

        current_topic = current[7].strip() if len(current) > 7 else ""
        current_url = current[8].strip() if len(current) > 8 else ""
        if current_url != recording_url or current_topic:
            continue

        # Claim the row so nothing else picks it up.
        try:
            worksheet.update_cell(index, 8, PROCESSING_MARKER)
        except Exception as exc:
            logger.warning("Could not claim row %s: %s", index, exc)
            continue

        audio_path = None
        try:
            audio_path, _ = download_audio(recording_url)
            timeline_data, raw_text_segments = transcribe_groq_whisper(str(audio_path))
            full_transcript_str = " ".join(raw_text_segments).strip()

            analysis = generate_call_analysis_groq(
                full_transcript_str,
                campaign_name,
                timeline_data=timeline_data,
            )
            score = calculate_call_quality_score(analysis)
            main_topic = analysis["main_topic"] or "No topic detected"

            worksheet.batch_update(
                [
                    {"range": f"G{index}:H{index}", "values": [[analysis["long_summary"], main_topic]]},
                    {"range": f"K{index}:L{index}", "values": [[build_qc_report(analysis), score]]},
                ],
                value_input_option="RAW",
            )
            attempts.pop(recording_url, None)
            processed += 1

            try:
                apply_row_score_color(worksheet, index, score, analysis, special_color_map.get(index, set()))
            except Exception as exc:
                logger.warning("Row %s processed but coloring failed: %s", index, exc)

        except GroqRateLimitError as rl:
            wait = min(max(rl.wait_seconds, 60), 6 * 3600) + 30
            shared["paused_until"] = time.time() + wait
            minutes = int(wait // 60)
            logger.warning("Groq limit reached; pausing the sheet worker for ~%s min", minutes)
            note = (
                f"Processing error: Groq token limit reached on all API keys. "
                f"Waiting - will retry automatically in about {minutes} min."
            )
            try:
                # Show the reason in G/K (like before). H stays empty so the row is retried automatically.
                worksheet.batch_update(
                    [
                        {"range": f"G{index}:H{index}", "values": [[note, ""]]},
                        {"range": f"K{index}", "values": [[note]]},
                    ],
                    value_input_option="RAW",
                )
            except Exception as write_exc:
                logger.warning("Could not write limit note for row %s: %s", index, write_exc)
            break
        except Exception as exc:
            logger.exception("Row %s failed", index)
            count = attempts.get(recording_url, 0) + 1
            attempts[recording_url] = count
            give_up = count >= MAX_ROW_ATTEMPTS
            if give_up:
                error_text = f"Processing error: {exc}"
            else:
                error_text = f"Processing error: {exc} (attempt {count} of {MAX_ROW_ATTEMPTS}, will retry automatically)"
            try:
                # The reason is always visible in G and K, so you can see why a row is not complete.
                # H gets the ERROR marker only after the last attempt; until then it is empty and the row is retried.
                worksheet.batch_update(
                    [
                        {"range": f"G{index}:H{index}", "values": [[error_text, ERROR_MARKER if give_up else ""]]},
                        {"range": f"K{index}", "values": [[error_text]]},
                    ],
                    value_input_option="RAW",
                )
                if give_up:
                    attempts.pop(recording_url, None)
            except Exception as write_exc:
                logger.warning("Could not record error for row %s: %s", index, write_exc)
        finally:
            if audio_path is not None:
                audio_path.unlink(missing_ok=True)

        time.sleep(1)

    return processed


def process_sheet_once(shared, fallback_campaign=None, blocking=False):
    """Run one pass if nobody else is. Returns (processed_count, state) with state 'ok', 'busy' or 'paused'."""
    if shared.get("paused_until", 0) > time.time():
        return 0, "paused"
    if not shared["lock"].acquire(blocking=blocking):
        return 0, "busy"
    try:
        return _process_sheet_locked(shared, fallback_campaign), "ok"
    finally:
        shared["lock"].release()


def _clear_stale_claims(shared):
    """If the app crashed mid-row, a 'Processing...' marker can be left behind. Clear them on startup."""
    with shared["lock"]:
        worksheet = get_google_client().open(SHEET_NAME).worksheet(WORKSHEET_NAME)
        column_h = worksheet.col_values(8)
        updates = [
            {"range": f"H{i}", "values": [[""]]}
            for i, value in enumerate(column_h, start=1)
            if value.strip() == PROCESSING_MARKER
        ]
        if updates:
            worksheet.batch_update(updates, value_input_option="RAW")
            logger.info("Cleared %s stale processing markers", len(updates))


def background_sheet_watcher(shared):
    """Continuously watches the Ringba sheet for new recordings."""
    time.sleep(5)

    try:
        _clear_stale_claims(shared)
    except Exception as exc:
        logger.warning("Startup cleanup skipped: %s", exc)

    while True:
        try:
            process_sheet_once(shared, fallback_campaign=None, blocking=False)
        except Exception as exc:
            logger.warning("Background watcher cycle failed: %s", exc)
        time.sleep(WATCHER_INTERVAL_SEC)


@st.cache_resource
def start_background_worker():
    """Starts exactly ONE watcher thread per server process (not one per browser session)."""
    shared = {"lock": threading.Lock(), "attempts": {}}
    thread = threading.Thread(
        target=background_sheet_watcher, args=(shared,), daemon=True, name="sheet-watcher"
    )
    thread.start()
    return shared


def sync_google_sheet_batch(default_campaign_name=""):
    """Process Ringba recordings from the shared Google Sheet (manual button).

    Sheet layout:
    D = Campaign
    G = Long AI Summary
    H = Main Topic / processing flag
    I = Recording URL
    J = Existing carrier / VOIP data (untouched)
    K = AI QC Report
    L = Numeric quality score
    """
    shared = start_background_worker()
    try:
        count, state = process_sheet_once(
            shared,
            fallback_campaign=default_campaign_name or "General Customer Inquiry",
            blocking=False,
        )
    except Exception as e:
        return False, f"Google Sheets error: {str(e)}"

    if state == "busy":
        return False, "The background worker is processing the sheet right now. Please try again in a minute."
    if state == "paused":
        minutes = max(1, int((shared.get("paused_until", 0) - time.time()) // 60))
        return False, f"Groq token limit reached on all API keys. Processing resumes automatically in about {minutes} min."
    return True, f"Successfully processed {count} new recordings!"


# Start the single background worker (all helper functions above are defined by this point).
start_background_worker()


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
# SIDEBAR & ROUTING
# ============================================================

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

# ============================================================
# MAIN TRANSCRIBER INTERFACE
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
            short_topic, detailed_summary = generate_summaries_groq(
                full_transcript_str,
                selected_campaign,
                timeline_data=timeline_data,
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
                st.audio(
                    audio_file.read(),
                    format="audio/wav" if st.session_state.file_path.lower().endswith(".wav") else "audio/mp3",
                )
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
