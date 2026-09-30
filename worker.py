import os
import time
import json
import urllib.request
from pathlib import Path
from groq import Groq
import gspread
from google.oauth2.service_account import Credentials

# ============================================================
# CONFIGURATION & CONSTANTS
# ============================================================

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

GROQ_SUMMARY_MODEL = "openai/gpt-oss-20b"

# ============================================================
# CAMPAIGN & QC PROMPTS CONFIGURATION
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

DEFAULT_QC_QUESTIONS = """
Write a short, natural, human-written QC note for the call using simple English. Only include information that is clearly understood from the transcript. Focus on why the caller contacted the business, what service they wanted, and how the call ended.
"""

QC_SYSTEM_PROMPT = """
You are a Call QC analyst for a pay-per-call affiliate network.
Your task is to summarize a call transcript for QC review based on the campaign-specific QC questions provided below.

CAMPAIGN:
{campaign_name}

QC QUESTIONS:
{qc_questions}

STRICT RULES:
- Include only information clearly supported by the transcript.
- If a QC question is not answered or the information is unclear, skip it completely.
- Never write "not mentioned", "not provided", or "unknown".
- Do not use bullet points, numbered lists, or headings.
- Write ONE short, flat paragraph only in plain text (no markdown, no asterisks).

CALL TRANSCRIPT:
{call_transcript}
"""

SHORT_TOPIC_PROMPT = """
MAIN TOPIC SUMMARY:
Write ONE short sentence describing the caller's true main topic and reason for calling based on the summary below.

SUMMARY:
{call_transcript}
"""


# ============================================================
# HELPER & PROCESSING FUNCTIONS
# ============================================================

def get_campaign_category(raw_campaign_text):
    text = raw_campaign_text.lower()
    if "health" in text or "u65" in text:
        return "U65 Health Insurance"
    elif "rehab" in text:
        return "Rehab Services"
    elif "dumpster" in text:
        return "Dumpster & Porta Potty Services"
    else:
        return raw_campaign_text.strip() if raw_campaign_text else "General Customer Inquiry"

def format_seconds_to_hms(total_seconds_str):
    try:
        total_seconds = int(float(str(total_seconds_str).strip()))
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    except Exception:
        return str(total_seconds_str)

def get_groq_api_keys():
    primary = os.getenv("Aunty_NEXT_DOOR_API_PRIMARY", "").strip()
    secondary = os.getenv("GROQ_API_KEY_SECONDARY_2", "").strip()
    return primary, secondary

def transcribe_groq_whisper(audio_file_path):
    primary_key, _ = get_groq_api_keys()
    if not primary_key:
        raise RuntimeError("Primary Groq API key not found in environment variables.")

    client = Groq(api_key=primary_key)

    with open(audio_file_path, "rb") as file:
        transcription = client.audio.transcriptions.create(
            file=(os.path.basename(audio_file_path), file.read()),
            model="whisper-large-v3-turbo",
            response_format="verbose_json",
            language="en",
        )

    raw_text_segments = []
    segments = getattr(transcription, "segments", None)
    if segments:
        for seg in segments:
            text_value = seg.get("text", "").strip() if isinstance(seg, dict) else seg.text.strip()
            if text_value:
                raw_text_segments.append(text_value)
    else:
        full_text = getattr(transcription, "text", "").strip()
        if full_text:
            raw_text_segments.append(full_text)

    return raw_text_segments

def generate_summaries_groq(full_transcript, campaign_name):
    primary_key, secondary_key = get_groq_api_keys()
    client_primary = Groq(api_key=primary_key)
    client_secondary = Groq(api_key=secondary_key) if secondary_key else client_primary
    
    qc_questions = CAMPAIGN_QC_QUESTIONS.get(campaign_name, DEFAULT_QC_QUESTIONS)

    full_prompt = QC_SYSTEM_PROMPT.format(
        campaign_name=campaign_name,
        qc_questions=qc_questions,
        call_transcript=full_transcript
    )

    detailed_summary = "Failed to generate summary."
    short_topic = "Failed to generate topic."

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
        try:
            response_detailed = client_secondary.chat.completions.create(
                model=GROQ_SUMMARY_MODEL,
                messages=[{"role": "user", "content": full_prompt}],
                temperature=0.2,
                max_tokens=450,
                reasoning_effort="low"
            )
            detailed_summary = response_detailed.choices[0].message.content.replace("*", "").strip()
        except Exception as secondary_e:
            return "Topic generation failed.", f"Model Error: {str(secondary_e)}"

    topic_prompt = SHORT_TOPIC_PROMPT.format(call_transcript=detailed_summary)

    try:
        response_topic = client_primary.chat.completions.create(
            model=GROQ_SUMMARY_MODEL,
            messages=[{"role": "user", "content": topic_prompt}],
            temperature=0.2,
            max_tokens=50,
            reasoning_effort="low"
        )
        short_topic = response_topic.choices[0].message.content.replace("*", "").strip()
    except Exception:
        try:
            response_topic = client_secondary.chat.completions.create(
                model=GROQ_SUMMARY_MODEL,
                messages=[{"role": "user", "content": topic_prompt}],
                temperature=0.2,
                max_tokens=50,
                reasoning_effort="low"
            )
            short_topic = response_topic.choices[0].message.content.replace("*", "").strip()
        except Exception:
            short_topic = "Topic generation failed."

    return short_topic, detailed_summary

def load_audio_url_to_path(url):
    url = url.strip()
    if not url:
        raise ValueError("URL is required.")

    filename = url.split("/")[-1].split("?")[0] or "web_audio.mp3"
    safe_name = f"{int(time.time())}_{filename}"
    path = UPLOAD_DIR / safe_name

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as response:
        with open(path, "wb") as out_file:
            out_file.write(response.read())

    return str(path)

def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    # Check if the secret is provided in GitHub Actions environment variables
    if os.environ.get("GCP_SERVICE_ACCOUNT_JSON"):
        creds_dict = json.loads(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return gspread.authorize(creds)
    elif os.path.exists("service_account.json"):
        return gspread.service_account(filename="service_account.json")
    else:
        raise FileNotFoundError("Google service account credentials not found in env or file!")


# ============================================================
# SINGLE-RUN SHEET WORKER EXECUTION
# ============================================================

def run_sheet_sync_once():
    sheet_names = ["Ringba to Sheet QC"]
    print("Starting single-run Google Sheets sync worker...")

    try:
        gc = get_gspread_client()
        
        for sheet_name in sheet_names:
            sheet = gc.open(sheet_name)
            worksheet = sheet.worksheet("Sheet1")
            rows = worksheet.get_all_values()
             
            for index, row in enumerate(rows[1:], start=2):
                raw_duration = row[5].strip() if len(row) > 5 else ""
                raw_campaign = row[3].strip() if len(row) > 3 else ""
                recording_url = row[8].strip() if len(row) > 8 else ""
                existing_main_topic = row[7].strip() if len(row) > 7 else ""

                if raw_duration and not ":" in raw_duration and not "[" in raw_duration:
                    formatted_dur = format_seconds_to_hms(raw_duration)
                    worksheet.update_cell(index, 6, formatted_dur)
                    time.sleep(1)

                if raw_campaign and recording_url and not existing_main_topic:
                    try:
                        print(f"Processing row {index} in {sheet_name}...")
                        campaign_name = get_campaign_category(raw_campaign)
                        audio_path = load_audio_url_to_path(recording_url)
                        
                        raw_text_segments = transcribe_groq_whisper(audio_path)
                        full_transcript_str = " ".join(raw_text_segments)
                        
                        main_topic, detailed_summary = generate_summaries_groq(full_transcript_str, campaign_name)
                        
                        worksheet.update_cell(index, 7, detailed_summary)
                        worksheet.update_cell(index, 8, main_topic)
                        print(f"Successfully updated row {index}!")
                        
                        if os.path.exists(audio_path):
                            os.remove(audio_path)
                            
                        time.sleep(2)
                    except Exception as err:
                        print(f"Error processing row {index}: {str(err)}")
                        worksheet.update_cell(index, 7, f"Error: {str(err)}")

    except Exception as e:
        print(f"Global worker error: {str(e)}")

    print("Worker sync run complete. Exiting cleanly.")


if __name__ == "__main__":
    print("Initializing standalone worker service...")
    run_sheet_sync_once()
    print("Worker task completed successfully.")
