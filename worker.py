import os
import time
import gspread
from google.oauth2.service_account import Credentials

# Import your core audio loading, transcription, and summarization helper functions
# (Make sure these functions are defined in or imported from your helper modules)
from helper import load_audio_url, transcribe_groq_whisper, generate_summaries_groq

def get_campaign_category(raw_campaign_text):
    """Accurately maps specific row campaign names to clean umbrella categories."""
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
    """Converts raw seconds into a clean H:MM:SS text string."""
    try:
        total_seconds = int(float(str(total_seconds_str).strip()))
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    except Exception:
        return str(total_seconds_str)

def get_gspread_client():
    """Initializes and returns a authorized gspread client safely for production."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    # 1. Check if service account info is provided via environment variables (e.g., Render / Heroku / Docker)
    if os.environ.get("GCP_SERVICE_ACCOUNT_JSON"):
        import json
        creds_dict = json.loads(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return gspread.authorize(creds)
    
    # 2. Fallback to local JSON file key for local development testing
    elif os.path.exists("service_account.json"):
        return gspread.service_account(filename="service_account.json")
    
    else:
        raise FileNotFoundError("Google Cloud Service Account credentials not found! Set GCP_SERVICE_ACCOUNT_JSON env var or provide service_account.json.")

def background_sheet_watcher():
    """Continuously watches multiple Google Sheets for new rows in the background every 15 minutes."""
    sheet_names = ["Ringba to Sheet QC"]
    print("Starting background Google Sheets worker loop...")

    while True:
        try:
            gc = get_gspread_client()
            
            for sheet_name in sheet_names:
                try:
                    sheet = gc.open(sheet_name)
                    worksheet = sheet.worksheet("Sheet1")
                    rows = worksheet.get_all_values()
                     
                    for index, row in enumerate(rows[1:], start=2):
                        raw_duration = row[5].strip() if len(row) > 5 else ""
                        raw_campaign = row[3].strip() if len(row) > 3 else ""
                        recording_url = row[8].strip() if len(row) > 8 else ""
                        existing_main_topic = row[7].strip() if len(row) > 7 else ""

                        # 1. Format raw duration seconds if missing proper format
                        if raw_duration and not ":" in raw_duration and not "[" in raw_duration:
                            formatted_dur = format_seconds_to_hms(raw_duration)
                            worksheet.update_cell(index, 6, formatted_dur)
                            time.sleep(1)

                        # 2. Process transcription and summaries if campaign & recording exist but topic is empty
                        if raw_campaign and recording_url and not existing_main_topic:
                            try:
                                print(f"Processing row {index} in {sheet_name}...")
                                campaign_name = get_campaign_category(raw_campaign)

                                # Download/load audio and get file path
                                file_path = load_audio_url(recording_url)
                                _, raw_text_segments = transcribe_groq_whisper(file_path)
                                full_transcript_str = " ".join(raw_text_segments)
                                
                                main_topic, detailed_summary = generate_summaries_groq(full_transcript_str, campaign_name)
                                
                                worksheet.update_cell(index, 7, detailed_summary)
                                worksheet.update_cell(index, 8, main_topic)
                                print(f"Successfully updated row {index}!")
                                time.sleep(5)
                                
                            except Exception as err:
                                print(f"Error processing row {index}: {str(err)}")
                                worksheet.update_cell(index, 7, f"Error: {str(err)}")

                except Exception as sheet_err:
                    print(f"Error accessing sheet '{sheet_name}': {sheet_err}")

        except Exception as e:
            print(f"Global worker error: {e}")

        # Refresh automatically every 15 minutes (900 seconds)
        print("Worker cycle complete. Sleeping for 15 minutes...")
        time.sleep(900)

if __name__ == "__main__":
    background_sheet_watcher()
