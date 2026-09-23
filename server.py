# -*- coding: utf-8 -*-
"""
server.py
السيرفر الخلفي (Backend) المتكامل بنظام التخزين التبادلي المزدوج وحماية الـ API Key.
"""

import os
import io
import json
import time
import secrets
from fastapi import FastAPI, Request, UploadFile, File, Form, Security, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security.api_key import APIKeyHeader

import cloud_engine
import media_tools
from PIL import Image

app = FastAPI()

# --- إعدادات نظام التخزين التبادلي المزدوج (Fail-Safe) ---
PRIMARY_DB_FOLDER = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "data")
BACKUP_DB_FOLDER = "data"

os.makedirs(PRIMARY_DB_FOLDER, exist_ok=True)
os.makedirs(BACKUP_DB_FOLDER, exist_ok=True)

def load_json_fail_safe(filename, default):
    primary_path = os.path.join(PRIMARY_DB_FOLDER, filename)
    backup_path = os.path.join(BACKUP_DB_FOLDER, filename)
    
    try:
        if os.path.exists(primary_path):
            with open(primary_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"⚠️ مشكلة في التخزين الأساسي، جاري القراءة من السيرفر المحلي: {e}")
        
    if os.path.exists(backup_path):
        try:
            with open(backup_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
            
    return default

def save_json_fail_safe(filename, data):
    primary_path = os.path.join(PRIMARY_DB_FOLDER, filename)
    backup_path = os.path.join(BACKUP_DB_FOLDER, filename)
    
    try:
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"⚠️ فشل الحفظ المحلي: {e}")

    try:
        with open(primary_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"⚠️ فشل الحفظ في التخزين الدائم: {e}")

USERS_FILE = "users_db.json"
CHATS_FILE = "chats_db.json"
VIP_CODES_FILE = "vip_codes.json"

FREE_WINDOW_SECONDS = 3 * 60 * 60
LOCK_DURATION_SECONDS = 3 * 60 * 60

session_file_context = {}
session_image_context = {}

# --- نظام مصادقة وإنشاء الـ API Key الخاص بك ---
API_KEY_NAME = "x-api-key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

@app.post("/api/generate_my_api_key")
async def generate_my_api_key(username: str):
    users = load_json_fail_safe(USERS_FILE, {})
    if username not in users:
        return JSONResponse({"ok": False, "error": "المستخدم غير موجود."}, status_code=404)
    
    raw_key = secrets.token_hex(24)
    my_api_key = f"sk_assist_{raw_key}"
    
    users[username]["my_api_key"] = my_api_key
    save_json_fail_safe(USERS_FILE, users)
    
    return {"ok": True, "api_key": my_api_key}

async def verify_my_api_key(api_key: str = Security(api_key_header)):
    if not api_key:
        raise HTTPException(status_code=401, detail="API Key is missing!")
        
    users = load_json_fail_safe(USERS_FILE, {})
    for username, data in users.items():
        if isinstance(data, dict) and data.get("my_api_key") == api_key:
            return username
            
    raise HTTPException(status_code=403, detail="Invalid or expired API Key")

# --- مسار المحادثة المحمي بالـ API Key الجديد ---
@app.post("/api/chat/ask")
async def ask_ai(
    message: str = Form(...), 
    chat_id: str = Form(...),
    username: str = Depends(verify_my_api_key)
):
    response = cloud_engine.generate_gemini_response(user_message=message)
    return {
        "ok": True, 
        "response": response, 
        "user": username
    }

# --- نظام حساب الاستهلاك والمميزات ---

def ensure_usage_fields(record):
    changed = False
    if "window_start" not in record:
        record["window_start"] = time.time()
        changed = True
    if "is_vip" not in record:
        record["is_vip"] = False
        changed = True
    if "total_allowed_seconds" not in record:
        record["total_allowed_seconds"] = FREE_WINDOW_SECONDS
        changed = True
    return changed

def compute_usage_status(record):
    now = time.time()
    elapsed = now - record["window_start"]
    locked = False
    remaining_lock = 0

    if not record["is_vip"] and elapsed >= record["total_allowed_seconds"]:
        time_since_end = elapsed - record["total_allowed_seconds"]
        if time_since_end < LOCK_DURATION_SECONDS:
            locked = True
            remaining_lock = LOCK_DURATION_SECONDS - time_since_end
        else:
            record["window_start"] = now
            record["total_allowed_seconds"] = FREE_WINDOW_SECONDS
            elapsed = 0

    remaining_free = max(0, record["total_allowed_seconds"] - elapsed)
    return locked, remaining_lock, remaining_free

def extract_text_from_bytes(filename, data: bytes):
    name = filename.lower()
    try:
        if name.endswith((".txt", ".md")):
            return data.decode("utf-8", errors="ignore")
        elif name.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = ""
            for page in reader.pages:
                text += (page.extract_text() or "") + "\n"
            return text.strip()
        elif name.endswith(".docx"):
            from docx import Document
            doc = Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs)
        elif name.endswith(".csv"):
            import pandas as pd
            df = pd.read_csv(io.BytesIO(data))
            return df.to_string()
        elif name.endswith((".xlsx", ".xls")):
            import pandas as pd
            df = pd.read_excel(io.BytesIO(data))
            return df.to_string()
        else:
            return None
    except Exception as e:
        return f"⚠️ تعذرت قراءة الملف: {e}"

# --- مسارات المصادقة والمستخدمين والـ VIP ---

@app.post("/api/signup")
async def signup(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    question = body.get("question", "").strip()
    answer = body.get("answer", "").strip()

    if not username or not password or not question or not answer:
        return JSONResponse({"ok": False, "error": "لازم تملى كل الحقول."}, status_code=400)

    users = load_json_fail_safe(USERS_FILE, {})
    if username in users:
        return JSONResponse({"ok": False, "error": "اسم المستخدم محجوز بالفعل."}, status_code=400)

    users[username] = {"password": password, "question": question, "answer": answer.lower()}
    save_json_fail_safe(USERS_FILE, users)
    return {"ok": True}

@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()

    users = load_json_fail_safe(USERS_FILE, {})
    record = users.get(username)
    if record and record.get("password") == password:
        return {"ok": True, "username": username}
    return JSONResponse({"ok": False, "error": "اسم المستخدم أو كلمة المرور غلط."}, status_code=401)

@app.post("/api/get_security_question")
async def get_security_question(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()

    users = load_json_fail_safe(USERS_FILE, {})
    record = users.get(username)
    if not record or not isinstance(record, dict) or "question" not in record:
        return JSONResponse({"ok": False, "error": "مفيش حساب بالاسم ده أو مفيش سؤال أمان متسجل ليه."}, status_code=404)
    return {"ok": True, "question": record["question"]}

@app.post("/api/reset_password")
async def reset_password(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    answer = body.get("answer", "").strip().lower()
    new_password = body.get("new_password", "").strip()

    users = load_json_fail_safe(USERS_FILE, {})
    record = users.get(username)
    if not record or not isinstance(record, dict):
        return JSONResponse({"ok": False, "error": "حساب غير موجود."}, status_code=404)

    if record.get("answer", "") != answer:
        return JSONResponse({"ok": False, "error": "الإجابة غلط."}, status_code=400)

    if not new_password:
        return JSONResponse({"ok": False, "error": "اكتب كلمة مرور جديدة."}, status_code=400)

    record["password"] = new_password
    users[username] = record
    save_json_fail_safe(USERS_FILE, users)
    return {"ok": True}

@app.get("/api/usage/{username}")
async def get_usage(username: str):
    users = load_json_fail_safe(USERS_FILE, {})
    record = users.get(username)
    if not record:
        return JSONResponse({"error": "user not found"}, status_code=404)
    if not isinstance(record, dict):
        record = {"password": record}

    ensure_usage_fields(record)
    locked, remaining_lock, remaining_free = compute_usage_status(record)

    users[username] = record
    save_json_fail_safe(USERS_FILE, users)

    return {
        "locked": locked,
        "remaining_lock_seconds": remaining_lock,
        "remaining_free_seconds": remaining_free,
        "is_vip": record["is_vip"],
    }

@app.post("/api/redeem_code")
async def redeem_code(request: Request):
    body = await request.json()
    username = body.get("username")
    code = body.get("code", "").strip()

    codes = load_json_fail_safe(VIP_CODES_FILE, [])
    matched = next((c for c in codes if c.get("code") == code), None)
    if not matched:
        return JSONResponse({"ok": False, "error": "الكود غلط أو مستخدم قبل كده."}, status_code=400)

    codes.remove(matched)
    save_json_fail_safe(VIP_CODES_FILE, codes)

    users = load_json_fail_safe(USERS_FILE, {})
    record = users.get(username, {})
    if not isinstance(record, dict):
        record = {"password": record}
    ensure_usage_fields(record)