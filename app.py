# Aunty Next DOOR - Call Transcription & Google Sheets Sync
# Updated: multi-provider AI fallback, structured QC, deterministic scoring,
# Q-column token tracking, Yelp/Yellow Pages spam rule, and safe provider skipping.

import sqlite3
import hashlib
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import textwrap
import uuid
import time
import urllib.request
import html
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re

import streamlit as st
import streamlit.components.v1 as components
from groq import Groq
from pydub import AudioSegment
import gspread
import requests

# ============================================================
# DATABASE & AUTHENTICATION
# ============================================================
DB_PATH = Path("users.db")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reset_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL,
        code TEXT NOT NULL, expires_at DATETIME NOT NULL)""")
    c.execute("PRAGMA table_info(users)")
    if "is_admin" not in [x[1] for x in c.fetchall()]:
        c.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO users (name,email,password_hash,is_admin) VALUES (?,?,?,1)",
                  ("System Admin", "admin@domain.com", hash_password("admin123")))
    conn.commit(); conn.close()

def register_user(name, email, password, is_admin=0):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    try:
        c.execute("INSERT INTO users (name,email,password_hash,is_admin) VALUES (?,?,?,?)",
                  (name, email.lower().strip(), hash_password(password), is_admin))
        conn.commit(); return True, "Account created successfully! Please log in."
    except sqlite3.IntegrityError:
        return False, "An account with this email already exists."
    finally: conn.close()

def authenticate_user(email, password):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT id,name,email,is_admin FROM users WHERE lower(email)=? AND password_hash=?",
              (email.lower().strip(), hash_password(password)))
    u = c.fetchone(); conn.close()
    return {"id":u[0],"name":u[1],"email":u[2],"is_admin":bool(u[3])} if u else None

def update_user_profile(user_id, new_name, new_email, new_password=""):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    try:
        if new_password.strip():
            c.execute("UPDATE users SET name=?,email=?,password_hash=? WHERE id=?",
                      (new_name,new_email.lower().strip(),hash_password(new_password),user_id))
        else:
            c.execute("UPDATE users SET name=?,email=? WHERE id=?",
                      (new_name,new_email.lower().strip(),user_id))
        conn.commit(); return True, "Profile updated successfully!"
    except sqlite3.IntegrityError:
        return False, "Email address is already in use by another account."
    finally: conn.close()

def get_all_users():
    conn=sqlite3.connect(DB_PATH); c=conn.cursor(); c.execute("SELECT id,name,email,is_admin FROM users ORDER BY id ASC")
    rows=c.fetchall(); conn.close()
    return [{"id":x[0],"name":x[1],"email":x[2],"is_admin":bool(x[3])} for x in rows]

def admin_toggle_role(user_id, make_admin):
    conn=sqlite3.connect(DB_PATH); conn.execute("UPDATE users SET is_admin=? WHERE id=?",(1 if make_admin else 0,user_id)); conn.commit(); conn.close()

def admin_reset_password(user_id, new_password):
    conn=sqlite3.connect(DB_PATH); conn.execute("UPDATE users SET password_hash=? WHERE id=?",(hash_password(new_password),user_id)); conn.commit(); conn.close()

def admin_delete_user(user_id):
    conn=sqlite3.connect(DB_PATH); conn.execute("DELETE FROM users WHERE id=?",(user_id,)); conn.commit(); conn.close()

def send_reset_code_email(email, code):
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print(f"[TESTING MODE] Reset Code for {email}: {code}")
        return True, f"Demo Mode: Credentials not configured. Your code is: {code}"
    try:
        msg=MIMEMultipart(); msg['From']=SMTP_EMAIL; msg['To']=email; msg['Subject']="Password Reset Code - Aunty Next DOOR"
        msg.attach(MIMEText(f"Hello,\n\nYour 6-digit verification code is: {code}\n\nThis code will expire in 15 minutes.\n\nRegards,\nAunty Next DOOR Team",'plain'))
        server=smtplib.SMTP(SMTP_SERVER,SMTP_PORT); server.starttls(); server.login(SMTP_EMAIL,SMTP_PASSWORD); server.send_message(msg); server.quit()
        return True, f"Reset code sent to {email}. Please check your inbox."
    except Exception as e: return False, f"Failed to send email: {e}"

def generate_reset_code(email):
    email=email.lower().strip(); conn=sqlite3.connect(DB_PATH); c=conn.cursor(); c.execute("SELECT id FROM users WHERE lower(email)=?",(email,))
    if not c.fetchone(): conn.close(); return False,"No account found with that email address."
    code=f"{random.randint(100000,999999)}"; expires=datetime.now()+timedelta(minutes=15)
    c.execute("DELETE FROM reset_tokens WHERE lower(email)=?",(email,)); c.execute("INSERT INTO reset_tokens(email,code,expires_at) VALUES(?,?,?)",(email,code,expires)); conn.commit(); conn.close()
    return send_reset_code_email(email,code)

def reset_password_with_code(email, code, new_password):
    email=email.lower().strip(); conn=sqlite3.connect(DB_PATH); c=conn.cursor(); c.execute("SELECT expires_at FROM reset_tokens WHERE lower(email)=? AND code=?",(email,code.strip())); r=c.fetchone()
    if not r: conn.close(); return False,"Invalid verification code or email address."
    try: expires=datetime.fromisoformat(r[0])
    except Exception: expires=datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S")
    if datetime.now()>expires:
        c.execute("DELETE FROM reset_tokens WHERE lower(email)=?",(email,)); conn.commit(); conn.close(); return False,"Verification code has expired. Please request a new one."
    c.execute("UPDATE users SET password_hash=? WHERE lower(email)=?",(hash_password(new_password),email)); c.execute("DELETE FROM reset_tokens WHERE lower(email)=?",(email,)); conn.commit(); conn.close()
    return True,"Password reset successfully! You can now log in with your new password."

init_db()

def render_html(content): st.html(textwrap.dedent(content))

def render_copy_icon_button(text_to_copy, button_id):
    escaped=json.dumps(text_to_copy)
    components.html(f'''<div style="display:flex;justify-content:center;width:100%;margin-top:6px"><button id="{button_id}" onclick="copyText_{button_id}()" style="background:#ff4d4d;color:#fff;border:0;border-radius:8px;padding:0 16px;height:34px;cursor:pointer;font-size:13px;font-weight:700;width:100%">Copy text</button></div><script>function copyText_{button_id}(){{const t={escaped};navigator.clipboard.writeText(t).then(()=>{{const b=document.getElementById('{button_id}');b.innerText='â Copied!';b.style.background='#10b981';setTimeout(()=>{{b.innerText='Copy text';b.style.background='#ff4d4d'}},2000)}})}}</script>''',height=42)

st.set_page_config(page_title="Aunty Next DOOR â¢ Transcriber", page_icon="ðï¸", layout="wide", initial_sidebar_state="expanded")
UPLOAD_DIR=Path("uploads"); UPLOAD_DIR.mkdir(exist_ok=True)

# ============================================================
# SECRETS / API CONFIG
# ============================================================
def secret(name, default=""):
    try:
        value=st.secrets.get(name, default)
        return value if value is not None else default
    except Exception: return default

# New preferred secret names + backward-compatible old names.
GROQ_API_KEY=secret("Aunty_NEXT_DOOR_API_PRIMARY") or secret("GROQ_API_KEY")
GROQ_SECONDARY_API_KEY=secret("GROQ_API_KEY_SECONDARY_2") or secret("GROQ_SECONDARY_API_KEY")
GROQ_API_KEY_3=secret("GROQ_API_KEY_3")
GROQ_API_KEY_4=secret("GROQ_API_KEY_4")
GEMINI_API_KEY=secret("GEMINI_API_KEY")
CEREBRAS_API_KEY=secret("CEREBRAS_API_KEY")
OPENROUTER_API_KEY=secret("OPENROUTER_API_KEY")
MISTRAL_API_KEY=secret("MISTRAL_API_KEY")
TOGETHER_API_KEY=secret("TOGETHER_API_KEY")
COHERE_API_KEY=secret("COHERE_API_KEY")

GROQ_TRANSCRIPTION_MODEL="whisper-large-v3-turbo"
GROQ_SUMMARY_MODEL="openai/gpt-oss-20b"
# Models are centralized here so they can be changed without touching provider logic.
PROVIDER_MODELS={
    "Groq-1":GROQ_SUMMARY_MODEL,
    "Groq-2":GROQ_SUMMARY_MODEL,
    "Groq-3":GROQ_SUMMARY_MODEL,
    "Groq-4":GROQ_SUMMARY_MODEL
    "Gemini":"gemini-2.5-flash",
    "Cerebras":"gpt-oss-120b",
    "OpenRouter":"openai/gpt-oss-20b:free",
    "Mistral":"mistral-small-latest",
    "Together":"openai/gpt-oss-20b",
    "Cohere":"command-a-03-2025",
}

if not GROQ_API_KEY: st.warning("GROQ_API_KEY is not configured in Streamlit Secrets. Transcription requires Groq.")

DEFAULT_SESSION={"logged_in_user":None,"current_view":"transcriber","theme_mode":"dark","source_name":"No file loaded","file_path":None,"duration_sec":0,"est_proc_sec":0,"source_type":"Awaiting input","transcript":[],"full_text":"","short_topic":"","detailed_summary":"","transcribed":False,"status":"Ready for audio","elapsed":0,"last_analysis_provider":"","last_analysis_usage":{},"last_analysis_error":""}
for k,v in DEFAULT_SESSION.items():
    if k not in st.session_state: st.session_state[k]=v

# ============================================================
# CAMPAIGN RULES / QC QUESTIONS
# ============================================================
CAMPAIGN_QC_QUESTIONS={
"Rehab & Addiction Treatment":"""Focus on why the caller called, the treatment or rehab service requested, location, insurance/payment information from the caller, qualification, important qualification reason, appointment/scheduling, advice/referral/next step, and how the call ended. For insurance, use the caller's own response only. Never treat an agent question as the caller's answer.""",
"Dumpster & Porta Potty Services":"""Focus on dumpster or porta potty service, size/capacity, residential/commercial/event/construction use, pricing, service location, delivery date/time, booking, and how the call ended. Include only clearly stated details.""",
"Pest Control & Home Services":"""Focus on the specific home/pest problem, homeowner/renter status, service area, quote/service request, appointment or inspection, relevance/wrong number, handling issues, and outcome.""",
"Insurance (Health / Auto / Home)":"""Focus on insurance type, current policy situation, quote/new policy/existing policy intent, location/eligibility, qualifying information, transfer/quote/callback, wrong number/unrelated inquiry, and outcome.""",
"Debt Relief & Financial Services":"""Focus on debt-relief or financial service requested, debt amount if stated, unsecured/secured debt if stated, qualification, spam/wrong number/unrelated inquiry, transfer/enrollment/consultation, and outcome.""",
}
DEFAULT_QC_QUESTIONS="""Focus on why the caller called, service/product requested, qualification information, location/eligibility, quote/appointment/transfer/booking/next step, relevance, wrong number, caller objection, agent handling issue, spam/robot behavior, and outcome. Never guess missing information."""

def get_campaign_category(raw_campaign):
    raw=(raw_campaign or "").strip(); low=raw.lower()
    if any(x in low for x in ["rehab","addiction","mental health","treatment"]): return "Rehab & Addiction Treatment"
    if any(x in low for x in ["dumpster","porta potty","portable toilet"]): return "Dumpster & Porta Potty Services"
    if any(x in low for x in ["pest","roof","moving","home service","locksmith","plumbing","hvac"]): return "Pest Control & Home Services"
    if any(x in low for x in ["insurance","medicare","auto insurance","home insurance"]): return "Insurance (Health / Auto / Home)"
    if any(x in low for x in ["debt","financial","loan","mca"]): return "Debt Relief & Financial Services"
    return raw or "General Customer Inquiry"

def get_qc_questions(campaign_name):
    return CAMPAIGN_QC_QUESTIONS.get(campaign_name, DEFAULT_QC_QUESTIONS)

# ============================================================
# STRUCTURED AI ANALYSIS
# ============================================================
ANALYSIS_SCHEMA={
    "type":"object",
    "additionalProperties":False,
    "properties":{
        "long_summary":{"type":"string"},"main_topic":{"type":"string"},
        "call_type":{"type":"string","enum":["QUALIFIED","NON-QUALIFIED","INFO ONLY","WRONG NUMBER","SILENT","SPAM / ROBOT","OTHER"]},
        "qualification_status":{"type":"string","enum":["QUALIFIED","NON-QUALIFIED","NOT CLEAR","NOT APPLICABLE"]},
        "caller_intent":{"type":"string"},"why_called":{"type":"string"},"service_requested":{"type":"string"},
        "insurance":{"type":"string"},"location":{"type":"string"},"outcome":{"type":"string"},
        "spam_robot":{"type":"boolean"},"spam_confidence":{"type":"integer","minimum":0,"maximum":100},
        "spam_reason":{"type":"string"},"qc_issue":{"type":"string"},"qualification_reason":{"type":"string"}
    },
    "required":["long_summary","main_topic","call_type","qualification_status","caller_intent","why_called","service_requested","insurance","location","outcome","spam_robot","spam_confidence","spam_reason","qc_issue","qualification_reason"]
}

ANALYSIS_INSTRUCTIONS="""
You are the QC analyst for a pay-per-call network. Read the entire transcript and return ONLY valid JSON matching the schema.
Accuracy is more important than brevity. Use only facts supported by the transcript. The transcript may have speech-to-text errors; correct obvious errors only when the intended meaning is clear.

CRITICAL:
- Never invent facts.
- Never turn an agent's question into the caller's answer.
- Do not guess Agent vs Caller when speaker identity is unavailable. Do not output speaker labels or speaker segments.
- If the caller does not clearly answer a question, leave that field as "Not clear" rather than assuming.
- Insurance must come from the caller's own statement/answer, not an agent's question. If the caller clearly says no insurance, say that.
- long_summary must be ONE natural, factual paragraph. Include decision-relevant intent, why called, service, qualification details/reason, insurance, location, outcome and clear QC/spam facts when present. Do not use bullets or headings.
- main_topic should be a meaningful short topic, NOT simply "SPAM / ROBOT". For example, a Google listing solicitation should have a topic such as "Google listing / SEO solicitation".
- Do not include unnecessary personal information such as caller name, phone number or full address.
- If a field is not supported, use "Not clear" (or "Not mentioned" where natural).
- spam_robot should reflect clear spam/robot behavior supported by the call. Deterministic Python rules will also be applied after AI.
- spam_confidence is your confidence in the spam assessment, 0-100.
- Do not calculate a quality score. Python will calculate the final score.
- Do not identify speakers.

CAMPAIGN: {campaign}
CAMPAIGN QC FOCUS: {qc}

TRANSCRIPT:
{transcript}
"""

def clean_ai_text(s):
    if not s: return ""
    s=str(s).strip()
    s=re.sub(r"^```(?:json|text)?\s*|\s*```$", "", s, flags=re.I|re.S)
    return s.replace("**","").replace("*","").strip()

def parse_json_response(content):
    content=clean_ai_text(content)
    try: return json.loads(content)
    except Exception: pass
    m=re.search(r"\{.*\}",content,re.S)
    if m:
        try: return json.loads(m.group(0))
        except Exception: pass
    raise ValueError("AI returned invalid JSON.")

def normalize_analysis(data):
    defaults={"long_summary":"","main_topic":"General customer inquiry","call_type":"OTHER","qualification_status":"NOT CLEAR","caller_intent":"Not clear","why_called":"Not clear","service_requested":"Not clear","insurance":"Not mentioned","location":"Not mentioned","outcome":"Not clear","spam_robot":False,"spam_confidence":0,"spam_reason":"","qc_issue":"","qualification_reason":"Not clear"}
    out=defaults.copy(); out.update(data or {})
    if out["call_type"] not in [x for x in ["QUALIFIED","NON-QUALIFIED","INFO ONLY","WRONG NUMBER","SILENT","SPAM / ROBOT","OTHER"]]: out["call_type"]="OTHER"
    if out["qualification_status"] not in ["QUALIFIED","NON-QUALIFIED","NOT CLEAR","NOT APPLICABLE"]: out["qualification_status"]="NOT CLEAR"
    try: out["spam_confidence"]=max(0,min(100,int(out.get("spam_confidence",0))))
    except Exception: out["spam_confidence"]=0
    out["spam_robot"]=bool(out.get("spam_robot",False))
    return out

def usage_from_obj(usage):
    """Normalize token usage from Groq/Pydantic/dict/OpenAI-compatible APIs."""
    if usage is None:
        return {"input": None, "output": None, "total": None}

    # Groq's SDK returns a Pydantic CompletionUsage object. Convert it first.
    if not isinstance(usage, dict):
        try:
            if hasattr(usage, "model_dump"):
                usage = usage.model_dump()
        except Exception:
            pass
        if not isinstance(usage, dict):
            try:
                if hasattr(usage, "dict"):
                    usage = usage.dict()
            except Exception:
                pass
        if not isinstance(usage, dict):
            try:
                usage = vars(usage)
            except Exception:
                usage = {}

    if not isinstance(usage, dict):
        usage = {}

    def get_value(*names):
        for name in names:
            value = usage.get(name)
            if value is not None:
                return value
        return None

    input_tokens = get_value("prompt_tokens", "input_tokens", "promptTokenCount")
    output_tokens = get_value("completion_tokens", "output_tokens", "candidatesTokenCount")
    total_tokens = get_value("total_tokens", "totalTokenCount")

    # Some providers omit total even though input/output are present.
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        try:
            total_tokens = int(input_tokens) + int(output_tokens)
        except Exception:
            total_tokens = None

    return {"input": input_tokens, "output": output_tokens, "total": total_tokens}

def usage_text(provider, model, usage):
    u=usage_from_obj(usage)
    def v(x):
        if x is None:
            return "N/A"
        try:
            return f"{int(x):,}"
        except Exception:
            return str(x)
    return f"{provider} | {model} | In: {v(u['input'])} | Out: {v(u['output'])} | Total: {v(u['total'])}"

def provider_specs():
    # Missing keys are intentionally skipped.
    return [
        ("Groq-1", "groq", GROQ_API_KEY, GROQ_SUMMARY_MODEL),
        ("Groq-2", "groq", GROQ_SECONDARY_API_KEY, GROQ_SUMMARY_MODEL),
        ("Groq-3", "groq", GROQ_API_KEY_3, GROQ_SUMMARY_MODEL),
        ("Gemini", "gemini", GEMINI_API_KEY, PROVIDER_MODELS["Gemini"]),
        ("Cerebras", "openai_compat", CEREBRAS_API_KEY, PROVIDER_MODELS["Cerebras"]),
        ("OpenRouter", "openai_compat", OPENROUTER_API_KEY, PROVIDER_MODELS["OpenRouter"]),
        ("Mistral", "openai_compat", MISTRAL_API_KEY, PROVIDER_MODELS["Mistral"]),
        ("Together", "openai_compat", TOGETHER_API_KEY, PROVIDER_MODELS["Together"]),
        ("Cohere", "cohere", COHERE_API_KEY, PROVIDER_MODELS["Cohere"]),
    ]

def openai_compat_analysis(api_key, model, prompt, provider_name):
    base_urls={
        "Cerebras":"https://api.cerebras.ai/v1/chat/completions",
        "OpenRouter":"https://openrouter.ai/api/v1/chat/completions",
        "Mistral":"https://api.mistral.ai/v1/chat/completions",
        "Together":"https://api.together.xyz/v1/chat/completions",
    }
    headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"}
    if provider_name=="OpenRouter": headers.update({"HTTP-Referer":"https://streamlit.io","X-Title":"Aunty Next DOOR"})
    payload={"model":model,"messages":[{"role":"system","content":"Return only valid JSON. Follow the requested schema exactly."},{"role":"user","content":prompt}],"temperature":0.1,"max_tokens":1600,"response_format":{"type":"json_object"}}
    r=requests.post(base_urls[provider_name],headers=headers,json=payload,timeout=120)
    r.raise_for_status(); obj=r.json(); content=obj["choices"][0]["message"]["content"]
    return parse_json_response(content), obj.get("usage",{})

def gemini_analysis(api_key, model, prompt):
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload={"systemInstruction":{"parts":[{"text":"You are a careful pay-per-call QC analyst. Return only valid JSON matching the requested schema."}]},"contents":[{"role":"user","parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.1,"maxOutputTokens":1600,"responseMimeType":"application/json","responseSchema":ANALYSIS_SCHEMA}}
    r=requests.post(url,json=payload,timeout=120); r.raise_for_status(); obj=r.json()
    content="".join(p.get("text","") for p in obj.get("candidates",[{}])[0].get("content",{}).get("parts",[]))
    return parse_json_response(content), obj.get("usageMetadata",{})

def cohere_analysis(api_key, model, prompt):
    url="https://api.cohere.com/v2/chat"
    headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"}
    payload={"model":model,"messages":[{"role":"user","content":prompt}],"temperature":0.1,"max_tokens":1600,"response_format":{"type":"json_object"}}
    r=requests.post(url,headers=headers,json=payload,timeout=120); r.raise_for_status(); obj=r.json()
    content=obj.get("message",{}).get("content","")
    if isinstance(content,list): content="".join(x.get("text","") if isinstance(x,dict) else str(x) for x in content)
    return parse_json_response(content), obj.get("usage",{})

def groq_analysis(api_key, model, prompt):
    client=Groq(api_key=api_key)
    # json_object is used for portability with gpt-oss-20b; Python validates the final JSON.
    resp=client.chat.completions.create(
        model=model,
        messages=[
            {"role":"system","content":"Return only valid JSON matching the requested fields."},
            {"role":"user","content":prompt}
        ],
        temperature=0.1,
        max_tokens=1600,
        reasoning_effort="low",
        response_format={"type":"json_object"}
    )
    content=resp.choices[0].message.content or ""

    # Groq SDK normally exposes resp.usage directly. Keep fallbacks for SDK/version differences.
    usage=getattr(resp,"usage",None)
    if usage is None:
        try:
            usage=resp.model_dump().get("usage")
        except Exception:
            pass
    if usage is None:
        try:
            usage=resp.dict().get("usage")
        except Exception:
            pass

    return parse_json_response(content), usage

def run_provider(name, kind, key, model, prompt):
    if kind=="groq": return groq_analysis(key,model,prompt)
    if kind=="gemini": return gemini_analysis(key,model,prompt)
    if kind=="cohere": return cohere_analysis(key,model,prompt)
    return openai_compat_analysis(key,model,prompt,name)

# ============================================================
# DETERMINISTIC SPAM + SCORING
# ============================================================
def apply_deterministic_spam_rules(analysis, transcript):
    text=(transcript or "").lower()
    a=normalize_analysis(analysis)
    # Latest business rule: ANY Yelp or Yellow Pages mention is spam.
    if "yelp" in text or "yellow pages" in text:
        a["spam_robot"]=True; a["spam_confidence"]=100
        a["spam_reason"]="Caller mentioned Yelp or Yellow Pages."
        if not a.get("main_topic") or a["main_topic"].lower() in {"spam / robot","spam","other"}:
            a["main_topic"]="Yelp / Yellow Pages inquiry"
        a["call_type"]="SPAM / ROBOT"
        return a
    patterns=[
        (r"google\s+(business|listing|maps)|google listing|seo|search engine optimization|listing verification|business verification|directory verification",95,"Google listing / SEO / verification solicitation."),
        (r"press\s*[09]|press zero|press nine",90,"Automated or scripted press-key behavior."),
        (r"debt relief marketing|loan marketing|insurance sales|marketing call|telemarketing",90,"Marketing or solicitation behavior."),
        (r"optimize your listing|verify your business|claim your listing",95,"Business directory/listing solicitation."),
    ]
    for pat,conf,reason in patterns:
        if re.search(pat,text,re.I):
            a["spam_robot"]=True; a["spam_confidence"]=max(int(a.get("spam_confidence",0)),conf); a["spam_reason"]=reason
            if a["call_type"]=="OTHER": a["call_type"]="SPAM / ROBOT"
            break
    return a

def deterministic_flags(a, transcript):
    text=(transcript or "").strip().lower()
    words=re.findall(r"\b\w+\b",text)
    silent=(len(words)<3)
    wrong= a.get("call_type")=="WRONG NUMBER" or "wrong number" in text
    nonqual=a.get("qualification_status")=="NON-QUALIFIED" or a.get("call_type")=="NON-QUALIFIED"
    info=a.get("call_type")=="INFO ONLY"
    return silent,wrong,nonqual,info

def calculate_quality_score(a, transcript, campaign):
    score=0; reasons=[]
    if a.get("qualification_status")=="QUALIFIED": score+=25; reasons.append("Qualified")
    if a.get("service_requested") not in ["", "Not clear", "Not mentioned"]: score+=15; reasons.append("Service requested")
    # Qualification information is represented by a concrete reason/status.
    if a.get("qualification_reason") not in ["", "Not clear", "Not mentioned"] or a.get("qualification_status") in ["QUALIFIED","NON-QUALIFIED"]:
        score+=15; reasons.append("Qualification information")
    if a.get("location") not in ["", "Not clear", "Not mentioned"]: score+=10; reasons.append("Location/eligibility")
    if a.get("outcome") not in ["", "Not clear", "Not mentioned"]: score+=10; reasons.append("Clear outcome")
    if len(re.findall(r"\b\w+\b",transcript or ""))>=12: score+=10; reasons.append("Two-way/meaningful conversation")
    if not a.get("spam_robot"): score+=5; reasons.append("No spam")
    if not a.get("qc_issue"): score+=10; reasons.append("No major QC issue")
    score=min(100,score)
    silent,wrong,nonqual,info=deterministic_flags(a,transcript)
    if a.get("spam_robot") and int(a.get("spam_confidence",0))>=90: score=5
    elif a.get("spam_robot"): score=min(score,25)
    if wrong: score=min(score,30)
    if silent: score=min(score,15)
    if nonqual or info: score=min(score,60)
    if campaign=="Rehab & Addiction Treatment":
        ins=(a.get("insurance") or "").lower()
        if any(x in ins for x in ["medicaid","medicare","government","state insurance","public insurance"]): score=min(score,60)
    return int(max(0,min(100,score))), reasons

def decision_signal(score,a):
    if a.get("spam_robot") and int(a.get("spam_confidence",0))>=90: return "REJECT / SPAM"
    if score>=90:return "KEEP / HIGH VALUE"
    if score>=80:return "KEEP / GOOD"
    if score>=60:return "REVIEW"
    if score>=40:return "LOW QUALITY / REVIEW"
    return "REJECT / INVESTIGATE"

def score_color(score):
    if score>=95:return "#15803d"
    if score>=80:return "#86efac"
    if score>=60:return "#fde047"
    if score>=40:return "#fb923c"
    return "#f87171"

def get_score_basis(a, reasons, score):
    basis=", ".join(reasons) if reasons else "No positive quality factors"
    if a.get("spam_robot") and a.get("spam_confidence",0)>=90: basis+="; high-confidence spam cap"
    if a.get("qualification_status")=="NON-QUALIFIED": basis+="; non-qualified cap"
    if a.get("call_type")=="WRONG NUMBER": basis+="; wrong-number cap"
    if a.get("call_type")=="SILENT": basis+="; silent-call cap"
    if a.get("call_type")=="INFO ONLY": basis+="; info-only cap"
    return basis

def build_k_report(a,score,reasons,decision):
    fields=[
        ("Call Type",a.get("call_type")),("Caller Intent",a.get("caller_intent")),("Why Called",a.get("why_called")),
        ("Service Interest",a.get("service_requested")),("Qualification",a.get("qualification_status")),
        ("Qualification Reason",a.get("qualification_reason")),("Insurance",a.get("insurance")),("Location",a.get("location")),
        ("Outcome",a.get("outcome")),("Spam/Robot",str(a.get("spam_robot"))),
        ("Spam Confidence",str(a.get("spam_confidence"))),("Spam Reason",a.get("spam_reason") or "None"),
        ("QC Issue",a.get("qc_issue") or "None"),("Decision Signal",decision),
        ("Score Basis",get_score_basis(a,reasons,score)),("Quality Score",str(score))]
    return "\n".join(f"{k}: {v or 'Not clear'}" for k,v in fields)

def generate_call_analysis(full_transcript,campaign_name):
    if not full_transcript.strip(): raise ValueError("Transcript is empty.")
    prompt=ANALYSIS_INSTRUCTIONS.format(campaign=campaign_name,qc=get_qc_questions(campaign_name),transcript=full_transcript)
    errors=[]
    for name,kind,key,model in provider_specs():
        if not key: continue
        try:
            raw,usage=run_provider(name,kind,key,model,prompt)
            a=normalize_analysis(raw)
            a=apply_deterministic_spam_rules(a,full_transcript)
            a["main_topic"]=clean_ai_text(a.get("main_topic")) or "General customer inquiry"
            a["long_summary"]=clean_ai_text(a.get("long_summary")) or "No clear summary available."
            a["long_summary"]=a["long_summary"].replace("\n"," ").strip()
            a["provider"]=name; a["model"]=model; a["usage"]=usage_from_obj(usage)
            st.session_state.last_analysis_provider=name; st.session_state.last_analysis_usage=a["usage"]; st.session_state.last_analysis_error=""
            return a
        except Exception as e:
            errors.append(f"{name}: {str(e)[:180]}")
            continue
    raise RuntimeError("All configured AI analysis providers failed or were unavailable. " + " | ".join(errors[-3:]))

# ============================================================
# TRANSCRIPTION / AUDIO
# ============================================================
def format_time(seconds):
    seconds=max(0,float(seconds or 0)); return f"{int(seconds//60):02d}:{int(seconds%60):02d}"

def transcribe_groq_whisper(audio_file_path):
    if not GROQ_API_KEY: raise RuntimeError("Groq API key not found for Whisper transcription.")
    client=Groq(api_key=GROQ_API_KEY)
    with open(audio_file_path,"rb") as f:
        tr=client.audio.transcriptions.create(file=(os.path.basename(audio_file_path),f.read()),model=GROQ_TRANSCRIPTION_MODEL,response_format="verbose_json",language="en")
    timeline=[]; texts=[]; segments=getattr(tr,"segments",None)
    if segments:
        for seg in segments:
            start=seg.get("start",0.0) if isinstance(seg,dict) else seg.start
            end=seg.get("end",0.0) if isinstance(seg,dict) else seg.end
            txt=(seg.get("text","") if isinstance(seg,dict) else seg.text).strip()
            if txt:
                timeline.append({"time":f"{format_time(start)} â {format_time(end)}","speaker":"Speaker","line":txt}); texts.append(txt)
    else:
        txt=getattr(tr,"text","").strip()
        if txt: timeline.append({"time":"00:00 â 00:00","speaker":"Speaker","line":txt}); texts.append(txt)
    return timeline,texts

def save_uploaded_audio(uploaded_file):
    ext=Path(uploaded_file.name).suffix.lower()
    if ext not in {".mp3",".wav"}: raise ValueError("Only MP3 and WAV are supported.")
    path=UPLOAD_DIR/f"{uuid.uuid4().hex}{ext}"; path.write_bytes(uploaded_file.getbuffer())
    try: duration=len(AudioSegment.from_file(path))/1000
    except Exception: duration=60
    st.session_state.update(source_name=uploaded_file.name,file_path=str(path),duration_sec=duration,est_proc_sec=max(1,round(duration*.008,1)),source_type="Uploaded file",transcribed=False,transcript=[],full_text="",short_topic="No transcription available yet.",detailed_summary="No summary generated yet.",status="Audio loaded and ready for processing.")

def load_audio_url(url):
    url=url.strip();
    if not url: raise ValueError("URL is required.")
    filename=url.split("/")[-1].split("?")[0] or "web_audio.mp3"; ext=Path(filename).suffix.lower(); ext=ext if ext in {".mp3",".wav"} else ".mp3"; path=UPLOAD_DIR/f"{uuid.uuid4().hex}{ext}"
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req,timeout=120) as r: path.write_bytes(r.read())
    duration=len(AudioSegment.from_file(path))/1000
    st.session_state.update(source_name=filename,file_path=str(path),duration_sec=duration,est_proc_sec=max(1,round(duration*.008,1)),source_type="Recording URL",transcribed=False,transcript=[],full_text="",short_topic="No transcription available yet.",detailed_summary="No summary generated yet.",status="Recording link loaded and ready.")

# ============================================================
# GOOGLE SHEETS SYNC
# ============================================================
def get_gspread_client():
    if os.path.exists("service_account.json"): return gspread.service_account(filename="service_account.json")
    return gspread.service_account_from_dict(dict(st.secrets["gcp_service_account"]))

def apply_row_score_color(worksheet,row,score):
    # Final score color is authoritative. Existing VoIP/pink logic does not override it.
    color=score_color(score)
    rgb={"#15803d":(0.08,0.50,0.24),"#86efac":(0.53,0.93,0.67),"#fde047":(0.99,0.87,0.28),"#fb923c":(0.98,0.57,0.24),"#f87171":(0.97,0.44,0.44)}.get(color,(0.97,0.44,0.44))
    try:
        worksheet.format(f"A{row}:L{row}",{"backgroundColor":{"red":rgb[0],"green":rgb[1],"blue":rgb[2]}})
    except Exception: pass

def update_q_cell(ws,row,value):
    try: ws.update_cell(row,17,value)
    except Exception: pass

def sync_google_sheet_batch(default_campaign_name=""):
    sheet_names=["Ringba to Sheet QC"]; total=0; status=st.empty()
    try:
        gc=get_gspread_client()
        for sheet_name in sheet_names:
            try:
                status.text(f"Checking sheet: {sheet_name}..."); ws=gc.open(sheet_name).worksheet("Sheet1"); rows=ws.get_all_values()
                if len(rows)<2: continue
                for index,row in enumerate(rows[1:],start=2):
                    raw_campaign=row[3].strip() if len(row)>3 else ""; raw_duration=row[5].strip() if len(row)>5 else ""; existing_h=row[7].strip() if len(row)>7 else ""; rec=row[8].strip() if len(row)>8 else ""
                    if raw_duration and ":" not in raw_duration and raw_duration.isdigit():
                        sec=int(raw_duration); ws.update_cell(index,6,f"{sec//3600}:{(sec%3600)//60:02d}:{sec%60:02d}"); time.sleep(.3)
                    if not (rec.startswith("http") and not existing_h): continue
                    try:
                        status.text(f"Row {index}: downloading audio..."); r=requests.get(rec,headers={"User-Agent":"Mozilla/5.0"},timeout=30); r.raise_for_status(); temp=f"temp_downloaded_audio_{uuid.uuid4().hex}.mp3"; Path(temp).write_bytes(r.content)
                        status.text(f"Row {index}: transcribing..."); _,texts=transcribe_groq_whisper(temp); transcript=" ".join(texts)
                        status.text(f"Row {index}: AI QC analysis..."); campaign=get_campaign_category(raw_campaign) if raw_campaign else (default_campaign_name or "General Customer Inquiry"); a=generate_call_analysis(transcript,campaign)
                        score,reasons=calculate_quality_score(a,transcript,campaign); decision=decision_signal(score,a); k=build_k_report(a,score,reasons,decision)
                        ws.update_cell(index,7,a["long_summary"]); ws.update_cell(index,8,a["main_topic"]); ws.update_cell(index,11,k); ws.update_cell(index,12,score)
                        update_q_cell(ws,index,usage_text(a["provider"],a["model"],a["usage"])); apply_row_score_color(ws,index,score)
                        total+=1; time.sleep(1)
                        try: os.remove(temp)
                        except Exception: pass
                    except Exception as row_err:
                        # Do not overwrite J. K gets the processing error and Q records failure status.
                        try: ws.update_cell(index,11,f"Processing Error: {str(row_err)[:500]}"); update_q_cell(ws,index,"AI analysis failed: all configured providers unavailable")
                        except Exception: pass
            except Exception: continue
        status.empty(); return True,f"Successfully processed {total} new call records!"
    except Exception as e:
        status.empty(); return False,f"Google Sheets connection error: {e}"

# ============================================================
# THEME
# ============================================================
theme_vars="""
--bg:#0b111e;--panel:#111a2e;--border:#1a2942;--text:#e2e8f0;--title-color:#fff;--muted:#64748b;--sidebar-bg:#080d1a;--card-bg:#101828;--input-bg:#090e17;--btn-bg:#172439;--stamp-bg:#132440;--stamp-text:#3b82f6;--blue-accent:#2563eb;--card-shadow:0 10px 30px rgba(0,0,0,.3);
""" if st.session_state.theme_mode=="dark" else """
--bg:#f8fafc;--panel:#fff;--border:#cbd5e1;--text:#0f172a;--title-color:#0f172a;--muted:#475569;--sidebar-bg:#fff;--card-bg:#fff;--input-bg:#f1f5f9;--btn-bg:#e2e8f0;--stamp-bg:#eff6ff;--stamp-text:#1d4ed8;--blue-accent:#2563eb;--card-shadow:0 4px 12px rgba(0,0,0,.05);
"""
render_html(f"""<style>
:root{{{theme_vars}--blue:#3b82f6;--green:#10b981;--radius:8px}}header[data-testid="stHeader"]{{background:transparent!important}}.stApp{{background:var(--bg)!important;color:var(--text)!important}}.block-container{{max-width:1700px;padding-top:18px;padding-bottom:20px}}section[data-testid="stSidebar"]{{background:var(--sidebar-bg)!important;border-right:1px solid var(--border)!important}}section[data-testid="stSidebar"]>div{{padding-top:14px}}div[data-testid="stRadio"] label{{color:var(--text)!important;font-weight:800!important;font-size:13px!important}}.brand{{display:flex;align-items:center;gap:10px;font-weight:900;letter-spacing:.05em;color:var(--text);font-size:16px;padding:0 4px 18px}}.brand-mark{{width:24px;height:24px;border-radius:6px;background:linear-gradient(135deg,#2563eb,#1d4ed8);display:grid;place-items:center}}.sidebar-divider{{height:1px;background:var(--border);margin:12px 0}}.sidebar-label{{font-size:11px;font-weight:800;color:var(--muted);margin:10px 4px 6px;text-transform:uppercase;letter-spacing:.05em}}.sidebar-user{{margin-top:15px;padding-top:12px;border-top:1px solid var(--border);display:flex;align-items:center;gap:10px;color:var(--text);font-size:13px;font-weight:700}}.avatar{{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;background:var(--input-bg);color:var(--muted);font-weight:800;font-size:11px;border:1px solid var(--border)}}.page-title{{display:flex;align-items:center;gap:10px;margin:4px 0 2px;font-size:26px;color:var(--title-color);font-weight:800}}.online{{font-size:11px;color:#10b981;background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.25);padding:2px 8px;border-radius:999px;font-weight:600}}.subtitle{{color:var(--muted);font-size:13px;margin:0 0 16px}}.dopp-card,.transcript-container{{background:var(--card-bg);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--card-shadow);overflow:hidden}}.card-title{{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:700;color:var(--title-color)}}.card-dot{{width:8px;height:8px;border-radius:50%;background:#2563eb}}div[data-baseweb="select"]>div{{background:var(--card-bg)!important;color:var(--text)!important;border-color:var(--border)!important;font-weight:700!important}}div.stButton>button{{background:var(--btn-bg)!important;color:var(--text)!important;border:1px solid var(--border)!important;border-radius:6px;font-size:13px;font-weight:600;min-height:38px;height:38px}}div.stButton>button[kind="primary"]{{background:#2563eb!important;color:#fff!important;border:0!important;font-weight:700!important}}.stTextInput input{{background:var(--card-bg)!important;border:1px solid var(--border)!important;color:var(--text)!important;border-radius:6px!important;font-size:13px!important}}.meta-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:10px}}.meta-item{{background:var(--input-bg);border:1px solid var(--border);border-radius:6px;padding:8px 10px}}.meta-key{{color:var(--muted);font-size:10px;text-transform:uppercase;font-weight:600}}.meta-value{{color:var(--title-color);font-size:12px;margin-top:3px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.status-box{{margin-top:8px;padding:8px 12px;border-radius:6px;background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.3);color:#059669;font-size:12px;display:flex;align-items:center;gap:8px;font-weight:700}}.summary-card{{background:var(--card-bg);border:1px solid var(--border);border-radius:var(--radius);padding:16px;box-shadow:var(--card-shadow);min-height:130px}}.summary-icon{{width:28px;height:28px;border-radius:6px;background:rgba(37,99,235,.15);color:#2563eb;display:grid;place-items:center;margin-bottom:8px;font-size:14px}}.summary-title{{margin:0;font-size:15px;font-weight:700;color:var(--title-color)}}.topic-text{{margin:10px 0 0;color:var(--text);font-size:14px;font-weight:600;line-height:1.5}}.summary-text{{margin:10px 0 0;color:var(--text);font-size:13px;line-height:1.6}}.transcript-header{{padding:14px 16px;border-bottom:1px solid var(--border)}}.transcript-scroll{{max-height:560px;overflow-y:auto;padding:6px 12px}}.transcript-line{{display:grid;grid-template-columns:85px 65px minmax(0,1fr);gap:10px;padding:12px 0;border-bottom:1px solid var(--border);align-items:start}}.transcript-stamp{{display:inline-flex;justify-content:center;background:var(--stamp-bg);color:var(--stamp-text);border-radius:4px;padding:3px 6px;font-size:11px;font-weight:700}}.transcript-speaker{{font-size:12px;color:var(--muted);padding-top:2px;font-weight:600}}.transcript-text{{font-size:13px;line-height:1.5;color:var(--text)}}
</style>""")

# ============================================================
# AUTH SCREEN
# ============================================================
if not st.session_state.logged_in_user:
    st.markdown("## Welcome to **Aunty Next DOOR**"); st.markdown("Please log in, create a public account, or reset your password.")
    t1,t2,t3=st.tabs(["ð Login","ð Sign Up","ð Forgot Password"])
    with t1:
        st.subheader("Login to your account"); e=st.text_input("Email Address",key="login_email"); p=st.text_input("Password",type="password",key="login_pass")
        if st.button("Log In",type="primary",use_container_width=True):
            u=authenticate_user(e,p)
            if u: st.session_state.logged_in_user=u; st.session_state.current_view="transcriber"; st.success(f"Welcome back, {u['name']}!"); time.sleep(.3); st.rerun()
            else: st.error("Invalid email or password.")
    with t2:
        st.subheader("Create a public account"); n=st.text_input("Full Name",key="signup_name"); e=st.text_input("Email Address",key="signup_email"); p=st.text_input("Create Password",type="password",key="signup_pass")
        if st.button("Create Account",type="primary",use_container_width=True):
            if n and e and p:
                ok,msg=register_user(n,e,p); st.success(msg) if ok else st.error(msg)
            else: st.warning("Please fill out all fields.")
    with t3:
        st.subheader("Reset Password via Email"); c1,c2=st.columns(2)
        with c1:
            st.markdown("##### 1. Request Reset Code"); e=st.text_input("Registered Email Address",key="reset_req_email")
            if st.button("Send Verification Code",use_container_width=True):
                if e.strip():
                    with st.spinner("Generating and sending code..."): ok,msg=generate_reset_code(e); st.success(msg) if ok else st.error(msg)
                else: st.warning("Please enter your email address.")
        with c2:
            st.markdown("##### 2. Enter Code & Set New Password"); e=st.text_input("Email Address",key="reset_email"); code=st.text_input("6-Digit Code",key="reset_code"); np=st.text_input("New Password",type="password",key="reset_new_pass")
            if st.button("Update Password",type="primary",use_container_width=True):
                if e.strip() and code.strip() and np.strip(): ok,msg=reset_password_with_code(e,code,np); st.success(msg) if ok else st.error(msg)
                else: st.warning("Please fill in all reset fields.")
    st.stop()

# ============================================================
# SIDEBAR
# ============================================================
current_user=st.session_state.logged_in_user
with st.sidebar:
    render_html('<div class="brand"><div class="brand-mark"></div><span>Aunty Next DOOR</span></div>')
    theme=st.radio("Theme Mode",["Dark","Light"],index=0 if st.session_state.theme_mode=="dark" else 1,horizontal=True,label_visibility="collapsed")
    if theme.lower()!=st.session_state.theme_mode: st.session_state.theme_mode=theme.lower(); st.rerun()
    if st.button("ðï¸  Transcriber",use_container_width=True,type="primary" if st.session_state.current_view=="transcriber" else "secondary"): st.session_state.current_view="transcriber"; st.rerun()
    if current_user.get("is_admin"):
        if st.button("ð¡ï¸  Admin Panel",use_container_width=True,type="primary" if st.session_state.current_view=="admin" else "secondary"): st.session_state.current_view="admin"; st.rerun()
    render_html('<div class="sidebar-divider"></div><div class="sidebar-label">Campaign</div>')
    selected_campaign=st.selectbox("Campaign",list(CAMPAIGN_QC_QUESTIONS.keys()),index=0,label_visibility="collapsed")
    render_html('<div class="sidebar-divider"></div><div class="sidebar-label">Google Sheets Automation</div>')
    if st.button("ð Sync & Process Sheet",use_container_width=True):
        with st.spinner("Scanning sheet and processing recordings..."):
            ok,msg=sync_google_sheet_batch(selected_campaign); st.success(msg) if ok else st.error(msg)
    render_html('<div class="sidebar-divider"></div><div class="sidebar-label">User Account Profile</div>')
    with st.form("profile_update_form"):
        nn=st.text_input("Name",value=current_user["name"]); ne=st.text_input("Email",value=current_user["email"]); np=st.text_input("New Password (optional)",type="password",placeholder="Leave blank to keep current")
        if st.form_submit_button("Save Profile Changes",use_container_width=True,type="primary"):
            ok,msg=update_user_profile(current_user["id"],nn,ne,np)
            if ok: st.session_state.logged_in_user.update(name=nn,email=ne); st.success(msg); time.sleep(.3); st.rerun()
            else: st.error(msg)
    if st.button("Logout",use_container_width=True): st.session_state.logged_in_user=None; st.rerun()
    initials="".join(x[0].upper() for x in current_user['name'].split()[:2]) or "U"; badge=" <span style='font-size:10px;color:#10b981'>(Admin)</span>" if current_user.get('is_admin') else ""
    render_html(f'<div class="sidebar-user"><div class="avatar">{initials}</div><div style="flex:1">{html.escape(current_user["name"])}{badge}</div></div>')

# ============================================================
# ADMIN
# ============================================================
if st.session_state.current_view=="admin":
    if not current_user.get("is_admin"): st.error("Access denied. Admin permissions required."); st.stop()
    render_html('<div class="page-title">Admin Panel <span class="online">â Management</span></div><p class="subtitle">Manage system users, grant/revoke permissions, and reset user credentials.</p>')
    a1,a2=st.tabs(["ð¥ Manage Users","â Create New User"])
    with a1:
        users=get_all_users(); st.subheader(f"Registered Accounts ({len(users)})")
        for u in users:
            with st.expander(f"{u['name']} ({u['email']}) {'â [ADMIN]' if u['is_admin'] else ''}"):
                c1,c2,c3=st.columns([1.5,1.5,1])
                with c1:
                    adm=st.checkbox("Admin Role",value=u["is_admin"],key=f"role_{u['id']}")
                    if adm!=u["is_admin"]: admin_toggle_role(u["id"],adm); st.rerun()
                with c2:
                    pw=st.text_input("Reset Password",key=f"pwd_{u['id']}",type="password")
                    if st.button("Update Password",key=f"btn_pwd_{u['id']}"):
                        if pw.strip(): admin_reset_password(u["id"],pw); st.success("Password updated!")
                        else: st.warning("Enter a valid password.")
                with c3:
                    if u["id"]!=current_user["id"]:
                        if st.button("ðï¸ Delete Account",key=f"del_{u['id']}"): admin_delete_user(u["id"]); st.rerun()
                    else: st.caption("Cannot delete self")
    with a2:
        st.subheader("Add a New User Account")
        with st.form("admin_create_user"):
            n=st.text_input("Full Name"); e=st.text_input("Email Address"); p=st.text_input("Password",type="password"); adm=st.checkbox("Grant Admin Privileges")
            if st.form_submit_button("Create User",type="primary"):
                if n and e and p:
                    ok,msg=register_user(n,e,p,1 if adm else 0); st.success(msg) if ok else st.error(msg)
                else: st.warning("Please fill out all fields.")
    st.stop()

# ============================================================
# MAIN TRANSCRIBER
# ============================================================
render_html('<div class="page-title">Transcriber <span class="online">â Online</span></div><p class="subtitle">Convert audio to text and get AI-powered summaries and insights.</p>')
left,right=st.columns([1.05,.95],gap="medium")
with left:
    c1,c2=st.columns(2)
    with c1:
        render_html(f'<div class="summary-card"><div class="summary-icon">â</div><h3 class="summary-title">Main topic</h3><p class="topic-text">{html.escape(st.session_state.short_topic or "No transcription available yet.")}</p></div>')
        if st.session_state.transcribed and st.session_state.short_topic: render_copy_icon_button(st.session_state.short_topic,"btn_copy_topic")
    with c2:
        render_html(f'<div class="summary-card"><div class="summary-icon">â¤</div><h3 class="summary-title">AI call summary</h3><p class="summary-text">{html.escape(st.session_state.detailed_summary or "No summary generated yet.")}</p></div>')
        if st.session_state.transcribed and st.session_state.detailed_summary: render_copy_icon_button(st.session_state.detailed_summary,"btn_copy_summary")
    render_html("<div style='height:6px'></div>")
    if "source_mode" not in st.session_state: st.session_state.source_mode="url"
    u1,u2=st.columns(2)
    with u1:
        if st.button("ð Paste recording URL",use_container_width=True,type="primary" if st.session_state.source_mode=="url" else "secondary"): st.session_state.source_mode="url"; st.rerun()
    with u2:
        if st.button("â¥ Upload MP3 / WAV",use_container_width=True,type="primary" if st.session_state.source_mode=="upload" else "secondary"): st.session_state.source_mode="upload"; st.rerun()
    if st.session_state.source_mode=="url":
        with st.form("url_form",clear_on_submit=False):
            url=st.text_input("Recording URL",placeholder="https://example.com/recording.mp3",label_visibility="collapsed")
            if st.form_submit_button("Load Audio",type="primary",use_container_width=True):
                if not url.strip(): st.error("Paste a recording URL first.")
                else:
                    with st.spinner("Downloading audio from link..."):
                        try: load_audio_url(url); st.success("Recording link loaded and ready.")
                        except Exception as e: st.error(f"Error loading URL: {e}")
    else:
        upload=st.file_uploader("Drop your audio here",type=["mp3","wav"],label_visibility="collapsed")
        if upload is not None and st.session_state.get("last_uploaded_name")!=upload.name:
            try: save_uploaded_audio(upload); st.session_state.last_uploaded_name=upload.name; st.success("Audio loaded successfully.")
            except Exception as e: st.error(f"Error loading audio: {e}")
    valid=bool(st.session_state.file_path and os.path.exists(st.session_state.file_path))
    if st.button("ðï¸ Transcribe audio",type="primary",use_container_width=True,disabled=not valid):
        progress=st.progress(0,text="Preparing audio..."); status=st.empty(); start=time.time()
        try:
            status.info("Processing Groq Whisper..."); progress.progress(20,text="Sending audio to Groq Whisper..."); timeline,texts=transcribe_groq_whisper(st.session_state.file_path)
            progress.progress(65,text="Running AI QC analysis..."); transcript=" ".join(texts); a=generate_call_analysis(transcript,selected_campaign); elapsed=round(time.time()-start,1)
            st.session_state.transcript=timeline; st.session_state.full_text=transcript; st.session_state.short_topic=a["main_topic"]; st.session_state.detailed_summary=a["long_summary"]; st.session_state.transcribed=True; st.session_state.status=f"Transcription completed ({elapsed}s)"; st.session_state.elapsed=elapsed
            progress.progress(100,text="Completed"); status.success(f"Done in {elapsed}s â¢ AI: {a['provider']}"); time.sleep(.4); st.rerun()
        except Exception as e:
            progress.empty(); status.error(f"Processing failed: {e}")
    if valid:
        try:
            with open(st.session_state.file_path,"rb") as af: st.audio(af.read(),format="audio/mp3")
        except Exception: pass
    render_html(f'<div class="dopp-card"><div class="meta-grid"><div class="meta-item"><div class="meta-key">Duration</div><div class="meta-value">{format_time(st.session_state.duration_sec)}</div></div><div class="meta-item"><div class="meta-key">Est. Time</div><div class="meta-value">~{st.session_state.est_proc_sec}s</div></div><div class="meta-item"><div class="meta-key">Source</div><div class="meta-value">{html.escape(st.session_state.source_type)}</div></div><div class="meta-item"><div class="meta-key">Campaign</div><div class="meta-value" style="color:#2563eb">{html.escape(selected_campaign)}</div></div></div></div><div class="status-box"><span>â</span><span>{html.escape(st.session_state.status)}</span><span style="margin-left:auto">{"100%" if st.session_state.transcribed else "0%"}</span></div>')
with right:
    render_html('<div class="transcript-container"><div class="transcript-header"><div class="card-title"><span class="card-dot"></span> Timeline Transcript</div></div></div>')
    if st.session_state.transcribed and st.session_state.full_text: render_copy_icon_button(st.session_state.full_text,"btn_copy_transcript")
    q=st.text_input("Search transcript",placeholder="Search transcript...",label_visibility="collapsed"); data=st.session_state.transcript
    if not data: render_html('<div class="transcript-container"><div class="transcript-scroll"><div style="padding:40px 20px;text-align:center;color:var(--muted);font-size:13px">No transcript processed yet.</div></div></div>')
    else:
        q=q.strip().lower(); rows=[]
        for item in data:
            ts=item.get("time",""); sp=item.get("speaker","Speaker"); line=item.get("line","")
            if q and q not in f"{ts} {sp} {line}".lower(): continue
            rows.append(f'<div class="transcript-line"><span class="transcript-stamp">{html.escape(ts)}</span><span class="transcript-speaker">{html.escape(sp)}</span><div class="transcript-text">{html.escape(line)}</div></div>')
        render_html('<div class="transcript-container"><div class="transcript-scroll">'+(''.join(rows) if rows else '<div style="padding:40px 20px;text-align:center;color:var(--muted);font-size:13px">No matching transcript found.</div>')+'</div></div>')
