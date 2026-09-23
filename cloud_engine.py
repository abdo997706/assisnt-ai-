# -*- coding: utf-8 -*-
"""
cloud_engine.py
دعم الرد عن طريق خدمات سحابية سريعة (Grok من xAI، Gemini من Google).
المفاتيح بتتقرا من api_keys.json المجاور لهذا الملف - حطها هناك بنفسك.
يدعم أكتر من مفتاح Gemini احتياطي (GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3, ...)
لو مفتاح خلصت حصته، بيجرب تلقائي المفتاح اللي بعده.
"""

import json
import os
import re

try:
    import streamlit as st
except ImportError:
    st = None

KEYS_FILE = "api_keys.json"


def _load_keys():
    keys = {}
    if st is not None:
        try:
            if "grok_api_key" in st.secrets:
                keys["grok_api_key"] = st.secrets["grok_api_key"]
            if "gemini_api_key" in st.secrets:
                keys["gemini_api_key"] = st.secrets["gemini_api_key"]
        except Exception:
            pass

    if not keys.get("grok_api_key") and os.environ.get("GROK_API_KEY"):
        keys["grok_api_key"] = os.environ.get("GROK_API_KEY")
    if not keys.get("gemini_api_key") and os.environ.get("GEMINI_API_KEY"):
        keys["gemini_api_key"] = os.environ.get("GEMINI_API_KEY")

    if not keys and os.path.exists(KEYS_FILE):
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            keys = json.load(f)

    # مفاتيح Gemini الاحتياطية: GEMINI_API_KEY_2, GEMINI_API_KEY_3, ...
    gemini_keys = []
    if keys.get("gemini_api_key"):
        gemini_keys.append(keys["gemini_api_key"])
    i = 2
    while os.environ.get(f"GEMINI_API_KEY_{i}"):
        gemini_keys.append(os.environ.get(f"GEMINI_API_KEY_{i}"))
        i += 1
    # دعم مفاتيح احتياطية جوه ملف api_keys.json كمان (لو حد حاطهم يدوي)
    for extra_key in keys.get("gemini_api_keys_extra", []):
        if extra_key and extra_key not in gemini_keys:
            gemini_keys.append(extra_key)

    keys["gemini_api_keys"] = gemini_keys

    return keys


def generate_grok_response(user_message, chat_history=None, system_prompt=None, model="grok-4-fast-non-reasoning"):
    try:
        from openai import OpenAI
    except ImportError:
        return "⚠️ المكتبة الناقصة: شغّل `pip install openai` الأول."

    keys = _load_keys()
    api_key = keys.get("grok_api_key", "").strip()
    if not api_key or "ضع مفتاح" in api_key:
        return "⚠️ مفتاح Grok مش متسجّل. افتح ملف api_keys.json وحط المفتاح الحقيقي بتاعك."

    client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1", timeout=30.0)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if chat_history:
        for m in chat_history:
            if m.get("role") in ("user", "assistant"):
                messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(model=model, messages=messages, temperature=0.7)
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"⚠️ حصل خطأ من Grok: {e}"


def generate_gemini_response(user_message, chat_history=None, system_prompt=None, model="gemini-3.6-flash", image=None):
    """
    بيستخدم Gemini (Google) عن طريق مكتبة google-genai.
    image: اختياري - صورة (PIL.Image أو bytes) عشان Gemini يشوفها ويحللها فعليًا.
    بيجرب كل مفتاح Gemini متسجّل (GEMINI_API_KEY, GEMINI_API_KEY_2, ...) بالترتيب،
    ولو مفتاح وصل لحد الاستخدام المجاني (429/RESOURCE_EXHAUSTED)، بيتقفز للمفتاح اللي بعده تلقائيًا.
    وجوه كل مفتاح، بيجرب موديلات بديلة برضو لو الموديل الأساسي مش متاح.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return "⚠️ المكتبة الناقصة: شغّل `pip install google-genai` الأول."

    keys = _load_keys()
    api_keys = keys.get("gemini_api_keys") or []
    if not api_keys:
        return "⚠️ مفتاح Gemini مش متسجّل. افتح ملف api_keys.json وحط المفتاح الحقيقي بتاعك."

    convo = ""
    if chat_history:
        for m in chat_history:
            role_label = "User" if m.get("role") == "user" else "Assistant"
            convo += f"{role_label}: {m['content']}\n"
    convo += f"User: {user_message}\nAssistant:"

    config = types.GenerateContentConfig(system_instruction=system_prompt) if system_prompt else None
    contents = [image, convo] if image is not None else convo

    fallback_models = [model, "gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
    fallback_models = list(dict.fromkeys(fallback_models))

    last_error = None
    for api_key in api_keys:
        client = genai.Client(api_key=api_key.strip(), http_options=types.HttpOptions(timeout=90000))
        key_exhausted = False
        for m in fallback_models:
            try:
                response = client.models.generate_content(model=m, contents=contents, config=config)
                return response.text.strip()
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
                    key_exhausted = True
                    continue
                break
        # لو المفتاح خلص (مش الموديل بس)، كمّل للمفتاح اللي بعده
        if not key_exhausted:
            # الخطأ مش بسبب نفاد الحصة (زي مفتاح غلط)، مفيش داعي نكمل بمفاتيح تانية لنفس الخطأ
            continue

    return f"⚠️ حصل خطأ من Gemini (كل المفاتيح المتاحة اتجربت): {last_error}"


def suggest_filename(description):
    """بيطلب من Gemini اسم ملف قصير ومناسب بالإنجليزي بناءً على وصف المحتوى."""
    try:
        prompt = (
            "Suggest a short, clean, filesystem-safe filename in English for a file "
            "created from this request (lowercase, words separated by underscores, "
            "NO extension, max 4 words, reply with ONLY the filename and nothing else): "
            f"\"{description}\""
        )
        result = generate_gemini_response(prompt, model="gemini-3.6-flash")
        if not result or result.startswith("⚠️"):
            return None
        name = result.strip().splitlines()[0]
        name = re.sub(r"[^a-zA-Z0-9_\-]", "", name.replace(" ", "_"))
        name = name.strip("_-").lower()
        return name[:40] if name else None
    except Exception:
        return None
