import time
from app import sync_google_sheet_batch  # Imports your core processing function

print("==================================================")
print("🚀 Starting Standalone Google Sheet Worker...")
print("==================================================")

# List your campaigns or define how your script processes them
campaigns = [
    "Rehab & Addiction Treatment", 
    "Dumpster & Porta Potty Services"
]

while True:
    for campaign in campaigns:
        try:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking campaign: {campaign}...")
            success, message = sync_google_sheet_batch(campaign)
            
            if success and "processed 0" not in message:
                print(f"✅ Success: {message}")
            elif not success:
                print(f"⚠️ Error in batch: {message}")
                
        except Exception as e:
            print(f"❌ Exception in worker loop: {e}")
    
    # Wait 30 seconds before the next check to stay well clear of 429 rate limits
    time.sleep(30)