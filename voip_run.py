"""
One pass of the VOIP / line-type checker for GitHub Actions.

Same logic as cli_checker.py (read the phone number in Column E, write Line Type to M, Carrier to N,
Fake Number to O, highlight VOIP/Fake rows in pink) but it runs ONCE and exits, instead of looping forever.
GitHub Actions starts it together with the call QC sync.

Needs (GitHub secrets, never in the code)
  VOIP_ENV             your whole .env text (GOOGLE_SHEET_NAME=..., VERIPHONE_API_KEY_1=..., ...)
                       (or instead: VERIPHONE_API_KEYS with your keys comma separated:  key1,key2,key3)
  STREAMLIT_SECRETS    the same secret the QC scheduler uses (its [gcp_service_account] is reused)
Optional environment variables
  GCP_SERVICE_ACCOUNT_JSON  full service-account JSON, if you want a different account for this checker
  GOOGLE_SHEET_NAME         default "Ringba to Sheet QC"
  VOIP_MAX_ROWS_PER_RUN     default 80  (the rest are picked up on the next run)
  VOIP_WRITE_DELAY          default 1.2 seconds between sheet writes (keeps you under Google's write limit)
  VOIP_MAX_WAIT             default 30 seconds to wait for a capped key before stopping this run
"""
import json
import os
import sys
import time
from pathlib import Path



def _load_env_secret():
    """GitHub secret VOIP_ENV can hold your whole .env text (KEY=value lines). Loaded BEFORE veriphone is imported."""
    for line in os.environ.get("VOIP_ENV", "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_secret()

import gspread
from google.oauth2.service_account import Credentials

from veriphone import client, normalize_phone

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Ringba to Sheet QC")
MAX_ROWS = int(os.getenv("VOIP_MAX_ROWS_PER_RUN", "80"))
WRITE_DELAY = float(os.getenv("VOIP_WRITE_DELAY", "1.2"))
PINK = {"backgroundColor": {"red": 1.0, "green": 0.75, "blue": 0.8}}
CHECKED_TYPES = ("VOIP", "Mobile", "Landline", "Other")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]


def load_service_account_info():
    raw = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        return json.loads(raw)
    secrets_text = os.environ.get("STREAMLIT_SECRETS", "").strip()
    if secrets_text:
        try:
            import tomllib
        except ModuleNotFoundError:          # Python < 3.11
            import tomli as tomllib
        info = tomllib.loads(secrets_text).get("gcp_service_account")
        if info:
            return dict(info)
    path = Path("credentials.json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def main():
    if not client.key_count:
        print("No Veriphone keys found. Add the GitHub secret VERIPHONE_API_KEYS (comma separated).", flush=True)
        return 1
    info = load_service_account_info()
    if not info:
        print("No Google credentials found (gcp_service_account in STREAMLIT_SECRETS).", flush=True)
        return 1

    client.max_wait = int(os.getenv("VOIP_MAX_WAIT", "30"))
    print(f"Loaded {client.key_count} Veriphone key(s).", flush=True)

    gc = gspread.authorize(Credentials.from_service_account_info(info, scopes=SCOPES))
    sheet = gc.open(GOOGLE_SHEET_NAME).sheet1
    rows = sheet.get_all_values()

    todo = []
    for idx in range(1, len(rows)):
        row = list(rows[idx]) + [""] * (15 - len(rows[idx]))
        number = normalize_phone(row[4])                      # Column E
        if not number:
            continue
        # Skip rows already checked (Column M has a line type AND Column O has Yes/No)
        if row[12].strip() in CHECKED_TYPES and row[14].strip() in ("Yes", "No"):
            continue
        todo.append((idx, number))

    print(f"Numbers to check: {len(todo)} (this run: up to {MAX_ROWS})", flush=True)
    checked = voip_found = fake_found = 0

    for idx, number in todo[:MAX_ROWS]:
        result = client.lookup(number)
        if not result.ok:
            # Every key is capped/unavailable: do NOT write a fake result. The row stays empty
            # and is retried automatically on the next run.
            print(f"!! Stopped at row {idx + 1}: {result.error}", flush=True)
            break

        line_type, carrier, fake_number = result.as_tuple()
        r = idx + 1
        sheet.update(range_name=f"M{r}:O{r}", values=[[line_type, carrier, fake_number]])   # M, N, O
        if line_type == "VOIP" or fake_number == "Yes":
            sheet.format(f"A{r}:O{r}", PINK)
        checked += 1
        voip_found += line_type == "VOIP"
        fake_found += fake_number == "Yes"
        source = "cache" if result.cached else f"key {result.key_used}"
        print(f"Row {r}: {line_type} | {carrier} | Fake: {fake_number} ({source})", flush=True)
        time.sleep(WRITE_DELAY)

    print(f"Done. Checked: {checked} | VOIP: {voip_found} | Fake: {fake_found} | Left for next run: {max(0, len(todo) - checked)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
