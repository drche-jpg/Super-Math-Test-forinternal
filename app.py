"""
MathComp — Competition Exam Platform
Single-file Streamlit app. No subfolders, no import issues.
"""
import sys, os, json, random, time, base64, re
from pathlib import Path
from typing import Optional

import streamlit as st

# ── Firebase / Firestore (optional — only active if credentials are set) ──────
_FB_ENABLED = False
_fb_db       = None

def _init_firebase():
    """Initialise Firebase once. Returns Firestore client or None."""
    global _FB_ENABLED, _fb_db
    if _fb_db is not None:
        return _fb_db
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore as _fs

        # Read credentials from Streamlit secrets
        try:
            fb_cfg = {
                "type":                        "service_account",
                "project_id":                  st.secrets["FIREBASE_PROJECT_ID"],
                "private_key_id":              st.secrets.get("FIREBASE_PRIVATE_KEY_ID", ""),
                "private_key":                 st.secrets["FIREBASE_PRIVATE_KEY"].replace("\\n", "\n"),
                "client_email":                st.secrets["FIREBASE_CLIENT_EMAIL"],
                "client_id":                   st.secrets.get("FIREBASE_CLIENT_ID", ""),
                "auth_uri":                    "https://accounts.google.com/o/oauth2/auth",
                "token_uri":                   "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_x509_cert_url":        st.secrets.get("FIREBASE_CERT_URL", ""),
            }
        except Exception:
            return None  # Secrets not set — use local JSON fallback

        if not firebase_admin._apps:
            cred = credentials.Certificate(fb_cfg)
            firebase_admin.initialize_app(cred)

        _fb_db = _fs.client()
        _FB_ENABLED = True
        return _fb_db
    except Exception:
        return None


def _fb() -> object | None:
    """Return Firestore client if Firebase is configured, else None."""
    return _init_firebase()

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MathComp — Competition Exam Platform",
    page_icon="✦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — DATABASE
# ══════════════════════════════════════════════════════════════════════════════
DB_PATH          = Path(__file__).parent / "data" / "questions.json"
RECORDS_PATH     = Path(__file__).parent / "data" / "records.json"
CUSTOM_COMP_PATH = Path(__file__).parent / "data" / "competitions.json"
COMP_DB_DIR      = Path(__file__).parent / "data" / "competitions"


# ── Per-competition file helpers ──────────────────────────────────────────────
def _comp_file(comp_code: str) -> Path:
    """Return the path to a per-competition question file."""
    return COMP_DB_DIR / f"{comp_code}.json"


def _load_comp_db(comp_code: str) -> list:
    """Load questions — Firestore first, then local JSON fallback."""
    db = _fb()
    if db:
        try:
            docs = db.collection("questions").where("comp", "==", comp_code).stream()
            qs = []
            for doc in docs:
                q = doc.to_dict()
                q["_doc_id"] = doc.id
                q.setdefault("comp", comp_code)
                qs.append(q)
            if qs:
                return qs
        except Exception:
            pass
    # Fallback: local JSON file
    path = _comp_file(comp_code)
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for q in data:
                    q.setdefault("comp", comp_code)
                return data
        except Exception:
            pass
    return []


def _save_comp_db(comp_code: str, questions: list) -> None:
    """Save/update a list of questions — used for bulk updates and local fallback."""
    db = _fb()
    if db:
        try:
            batch = db.batch()
            count = 0
            for q in questions:
                doc_id = q.get("_doc_id") or f"{comp_code}_{q.get('id', 0)}"
                q_data = {k: v for k, v in q.items()
                          if not k.startswith("_")}
                # Truncate oversized figures
                if q_data.get("figure") and len(str(q_data["figure"])) > 900000:
                    q_data["figure"] = None
                ref = db.collection("questions").document(doc_id)
                batch.set(ref, q_data)
                count += 1
                # Firestore batch limit is 500
                if count >= 490:
                    batch.commit()
                    batch = db.batch()
                    count = 0
            if count > 0:
                batch.commit()
            return
        except Exception as e:
            print(f"_save_comp_db Firestore error: {e}")
    # Fallback: local JSON
    COMP_DB_DIR.mkdir(parents=True, exist_ok=True)
    path = _comp_file(comp_code)
    q_list = [{k: v for k, v in q.items() if not k.startswith("_")}
              for q in questions]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(q_list, f, ensure_ascii=False, indent=2)


def list_comp_files() -> list[str]:
    """Return sorted list of competition codes — local files + Firestore."""
    codes = set()
    # Local JSON files
    if COMP_DB_DIR.exists():
        for p in COMP_DB_DIR.glob("*.json"):
            codes.add(p.stem)
    # Firestore — find all unique comp codes in the questions collection
    db = _fb()
    if db:
        try:
            docs = db.collection("questions").stream()
            for doc in docs:
                data = doc.to_dict()
                c = data.get("comp", "")
                if c:
                    codes.add(c)
        except Exception:
            pass
    # Always include built-in competitions that have local files
    return sorted(codes)


def db_add_to_comp(comp_code: str, q: dict) -> int:
    """Add a question — Firestore direct write, then local JSON fallback."""
    q["comp"] = comp_code
    q.setdefault("figure", None)
    q.setdefault("question_type", "mcq5")
    q.setdefault("correct_answer", "")
    q.setdefault("image", None)

    db = _fb()
    if db:
        try:
            # Get next ID from Firestore count
            existing = db.collection("questions").where("comp", "==", comp_code).stream()
            max_id = 0
            for doc in existing:
                d = doc.to_dict()
                max_id = max(max_id, d.get("id", 0))
            new_id = max_id + 1
            q["id"] = new_id

            # Remove internal keys not meant for storage
            q_store = {k: v for k, v in q.items() if not k.startswith("_")}
            # Handle figure — Firestore has 1MB doc limit, store base64 inline is fine
            # but very large images should be truncated
            if q_store.get("figure") and len(str(q_store.get("figure",""))) > 900000:
                q_store["figure"] = None  # too large for Firestore doc, skip

            doc_id = f"{comp_code}_{new_id}"
            db.collection("questions").document(doc_id).set(q_store)
            return new_id
        except Exception as e:
            # Fall through to local JSON
            import traceback
            print(f"Firestore write failed: {e}")

    # Fallback: local JSON file
    qs = _load_comp_db(comp_code)
    new_id = max((x.get("id", 0) for x in qs), default=0) + 1
    q["id"] = new_id
    q_local = {k: v for k, v in q.items() if not k.startswith("_")}
    qs.append(q_local)
    _save_comp_db(comp_code, qs)
    return new_id


def db_update_in_comp(comp_code: str, qid: int, updates: dict) -> bool:
    """Update a question in a specific competition's file."""
    qs = _load_comp_db(comp_code)
    for i, q in enumerate(qs):
        if q.get("id") == qid:
            qs[i].update(updates)
            _save_comp_db(comp_code, qs)
            return True
    return False


def db_delete_from_comp(comp_code: str, qid: int) -> bool:
    """Delete a question from Firestore or local JSON."""
    db = _fb()
    if db:
        try:
            docs = (db.collection("questions")
                    .where("comp", "==", comp_code)
                    .where("id", "==", qid)
                    .stream())
            deleted = False
            for doc in docs:
                doc.reference.delete()
                deleted = True
            if deleted:
                return True
        except Exception:
            pass
    # Fallback local JSON
    qs = _load_comp_db(comp_code)
    new_qs = [q for q in qs if q.get("id") != qid]
    if len(new_qs) < len(qs):
        _save_comp_db(comp_code, new_qs)
        return True
    return False


# ── Custom competition helpers ─────────────────────────────────────────────────
def _load_custom_comps() -> list:
    """Load custom competitions — Firestore first, then local JSON fallback."""
    db = _fb()
    if db:
        try:
            docs = db.collection("competitions").stream()
            comps = [dict(d.to_dict(), _doc_id=d.id) for d in docs]
            if comps:
                return comps
        except Exception:
            pass
    # Fallback: local JSON
    if CUSTOM_COMP_PATH.exists():
        try:
            with open(CUSTOM_COMP_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    CUSTOM_COMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    return []


def _save_custom_comps(comps: list) -> None:
    """Save custom competitions — Firestore first, then local JSON fallback."""
    db = _fb()
    if db:
        try:
            for comp in comps:
                doc_id = comp.get("_doc_id") or comp.get("code", "unknown")
                data = {k: v for k, v in comp.items() if k != "_doc_id"}
                db.collection("competitions").document(doc_id).set(data)
            return
        except Exception:
            pass
    # Fallback: local JSON
    CUSTOM_COMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CUSTOM_COMP_PATH, "w", encoding="utf-8") as f:
        json.dump(comps, f, ensure_ascii=False, indent=2)


def get_all_competitions() -> dict:
    """Return built-in + custom competitions as {code: name} dict."""
    out = dict(COMPETITIONS)
    for c in _load_custom_comps():
        out[c["code"]] = c["name"]
    return out


def get_competition_settings(code: str) -> dict:
    """Return settings dict for a competition code."""
    for c in _load_custom_comps():
        if c["code"] == code:
            return c
    # Built-in defaults
    return {
        "code": code,
        "name": COMPETITIONS.get(code, code),
        "show_score": True,
        "show_solution": True,
        "show_analysis": True,
    }


# ── Student record helpers ─────────────────────────────────────────────────────
def _load_records() -> list:
    """Load student records — Firestore first, then local JSON fallback."""
    db = _fb()
    if db:
        try:
            docs = db.collection("records").order_by(
                "timestamp", direction="DESCENDING"
            ).limit(1000).stream()
            return [dict(d.to_dict(), _doc_id=d.id) for d in docs]
        except Exception:
            pass
    # Fallback: local JSON
    if RECORDS_PATH.exists():
        try:
            with open(RECORDS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    return []


def _save_record(record: dict) -> None:
    """Save student record — Firestore direct add, then local JSON fallback."""
    # Remove internal keys
    record_clean = {k: v for k, v in record.items() if not k.startswith("_")}
    db = _fb()
    if db:
        try:
            db.collection("records").add(record_clean)
            return
        except Exception as e:
            print(f"_save_record Firestore error: {e}")
    # Fallback: local JSON
    records = []
    if RECORDS_PATH.exists():
        try:
            with open(RECORDS_PATH, encoding="utf-8") as f:
                records = json.load(f)
        except Exception:
            pass
    records.append(record)
    RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RECORDS_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

COMPETITIONS = {
    "AMC-MP":   "Australian MC – Middle Primary",
    "AMC-UP":   "Australian MC – Upper Primary",
    "AMC-JR":   "Australian MC – Junior",
    "AMC-INT":  "Australian MC – Intermediate",
    "AMC-SR":   "Australian MC – Senior",
    "AMC8":     "American MC – AMC 8",
    "AMC10":    "American MC – AMC 10",
    "AMC12":    "American MC – AMC 12",
    "AIME":     "AIME",
    "SANSU-KB": "Sansu Olympic – Kidbee",
    "SANSU-JR": "Sansu Olympic – Junior",
    "SANSU-SR": "Sansu Olympic – Senior",
    "POSN-R1":  "สอวน. รอบแรก",
    "MATH-PT":  "สมาคมคณิตศาสตร์ – ระดับประถม",
    "MATH-MT":  "สมาคมคณิตศาสตร์ – มัธยมต้น",
    "MATH-UP":  "สมาคมคณิตศาสตร์ – มัธยมปลาย",
}

TOPICS = ["Algebra", "Geometry", "Number Theory",
          "Combinatorics", "Word Problem", "Others"]

DIFF_LABELS = {"easy": "🟢 Easy", "intermediate": "🟡 Intermediate", "advanced": "🔴 Advanced"}


def _load_db() -> list:
    if DB_PATH.exists():
        try:
            with open(DB_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass  # corrupted file — fall through to seed
    # File missing or corrupted — write seed questions and return them
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _save_db(SEED_QUESTIONS)
    return SEED_QUESTIONS[:]


def _save_db(questions: list) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(questions, f, ensure_ascii=False, indent=2)


def db_get_all() -> list:
    """Return all questions — Firestore + local JSON files + legacy fallback."""
    seen_keys = set()
    all_qs    = []

    # 1. Firestore (highest priority — always up to date)
    db = _fb()
    if db:
        try:
            docs = db.collection("questions").stream()
            for doc in docs:
                q = doc.to_dict()
                q["_doc_id"] = doc.id
                q.setdefault("comp", "")
                key = (q["comp"], q.get("id"), doc.id)
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_qs.append(q)
        except Exception:
            pass

    # 2. Local per-competition JSON files (for competitions not yet in Firestore)
    if COMP_DB_DIR.exists():
        for p in sorted(COMP_DB_DIR.glob("*.json")):
            comp_code = p.stem
            try:
                with open(p, encoding="utf-8") as f:
                    qs = json.load(f)
                for q in qs:
                    q.setdefault("comp", comp_code)
                    key = (comp_code, q.get("id"), "local")
                    # Skip if already loaded from Firestore
                    fb_key = (comp_code, q.get("id"))
                    already = any(
                        (x.get("comp"), x.get("id")) == fb_key
                        for x in all_qs
                    )
                    if not already and key not in seen_keys:
                        seen_keys.add(key)
                        all_qs.append(q)
            except Exception:
                pass

    # 3. Legacy questions.json fallback
    try:
        legacy = _load_db()
        local_comps = {q.get("comp") for q in all_qs}
        for q in legacy:
            if q.get("comp") not in local_comps:
                key = ("legacy", q.get("id"))
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_qs.append(q)
    except Exception:
        pass

    return all_qs


def db_get_filtered(comp=None, difficulty=None, comp_list=None) -> list:
    """Filter questions — Firestore first, then local JSON fallback."""
    db = _fb()

    if comp_list:
        qs = []
        for c in comp_list:
            if db:
                try:
                    query = db.collection("questions").where("comp", "==", c)
                    if difficulty and difficulty != "mixed":
                        query = query.where("difficulty", "==", difficulty)
                    docs = query.stream()
                    fb_qs = []
                    for doc in docs:
                        q = doc.to_dict()
                        q["_doc_id"] = doc.id
                        q.setdefault("comp", c)
                        fb_qs.append(q)
                    if fb_qs:
                        qs.extend(fb_qs)
                        continue
                except Exception:
                    pass
            # Fallback local
            local_qs = _load_comp_db(c)
            if not local_qs:
                local_qs = [q for q in _load_db() if q.get("comp") == c]
            qs.extend(local_qs)
    elif comp:
        if db:
            try:
                query = db.collection("questions").where("comp", "==", comp)
                if difficulty and difficulty != "mixed":
                    query = query.where("difficulty", "==", difficulty)
                docs = query.stream()
                qs = []
                for doc in docs:
                    q = doc.to_dict()
                    q["_doc_id"] = doc.id
                    q.setdefault("comp", comp)
                    qs.append(q)
                if qs:
                    if difficulty and difficulty != "mixed":
                        return qs  # already filtered
                    return qs
            except Exception:
                pass
        # Fallback local
        qs = _load_comp_db(comp)
        if not qs:
            qs = [q for q in _load_db() if q.get("comp") == comp]
    else:
        qs = db_get_all()

    if difficulty and difficulty != "mixed":
        qs = [q for q in qs if q.get("difficulty") == difficulty]
    return qs


def db_add(q: dict) -> int:
    """Add question — routes to per-competition file if available."""
    comp_code = q.get("comp", "")
    q.setdefault("image", None)
    q.setdefault("figure", None)
    q.setdefault("topic", "Others")
    q.setdefault("question_type", "mcq5")
    q.setdefault("correct_answer", "")

    # Use per-competition file if the competition has one (or always create one)
    if comp_code:
        return db_add_to_comp(comp_code, q)

    # Fallback: legacy questions.json
    qs = _load_db()
    new_id = max((x.get("id", 0) for x in qs), default=0) + 1
    q["id"] = new_id
    qs.append(q)
    _save_db(qs)
    return new_id


def db_update(qid: int, updates: dict, comp_code: str = "") -> bool:
    """Update question — tries per-comp file first, then legacy."""
    if comp_code:
        if db_update_in_comp(comp_code, qid, updates):
            return True
    # Try all comp files
    for c in list_comp_files():
        if db_update_in_comp(c, qid, updates):
            return True
    # Fallback legacy
    qs = _load_db()
    for i, q in enumerate(qs):
        if q.get("id") == qid:
            qs[i].update(updates)
            _save_db(qs)
            return True
    return False


def db_delete(qid: int, comp_code: str = "") -> bool:
    """Delete question — tries per-comp file first, then legacy."""
    if comp_code:
        if db_delete_from_comp(comp_code, qid):
            return True
    for c in list_comp_files():
        if db_delete_from_comp(c, qid):
            return True
    qs = _load_db()
    new_qs = [q for q in qs if q.get("id") != qid]
    if len(new_qs) < len(qs):
        _save_db(new_qs)
        return True
    return False


def db_stats() -> dict:
    qs = _load_db()
    by_diff = {"easy": 0, "intermediate": 0, "advanced": 0}
    by_comp: dict = {}
    by_topic: dict = {}
    for q in qs:
        d = q.get("difficulty", "easy")
        by_diff[d] = by_diff.get(d, 0) + 1
        by_comp[q["comp"]] = by_comp.get(q["comp"], 0) + 1
        t = q.get("topic", "Others")
        by_topic[t] = by_topic.get(t, 0) + 1
    return {"total": len(qs), "by_diff": by_diff,
            "by_comp": by_comp, "by_topic": by_topic}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1B — STUDENT AUTHENTICATION
# ══════════════════════════════════════════════════════════════════════════════
# Uses Firebase Auth REST API (no extra SDK needed — just requests + secrets).
# Students register / login with email + password.
# Session key: st.session_state["student_auth"] = {uid, email, display_name}
# ─────────────────────────────────────────────────────────────────────────────

def _firebase_web_api_key() -> str:
    """Read Firebase Web API key from secrets (different from service-account key)."""
    try:
        return str(st.secrets.get("FIREBASE_WEB_API_KEY", ""))
    except Exception:
        return os.environ.get("FIREBASE_WEB_API_KEY", "")


def _auth_request(endpoint: str, payload: dict) -> dict:
    """POST to Firebase Auth REST API. Returns response dict."""
    import urllib.request, urllib.error
    api_key = _firebase_web_api_key()
    if not api_key:
        return {"error": {"message": "FIREBASE_WEB_API_KEY not set in secrets."}}
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:{endpoint}?key={api_key}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data,
                                   headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        return body
    except Exception as ex:
        return {"error": {"message": str(ex)}}


def _auth_signup(email: str, password: str, display_name: str) -> tuple[bool, str]:
    """Create a new student account. Returns (success, error_msg)."""
    res = _auth_request("signUp", {
        "email": email, "password": password, "returnSecureToken": True
    })
    if "error" in res:
        msg = res["error"].get("message", "Unknown error")
        friendly = {
            "EMAIL_EXISTS": "อีเมลนี้ถูกใช้แล้ว กรุณา login แทน",
            "INVALID_EMAIL": "รูปแบบอีเมลไม่ถูกต้อง",
            "WEAK_PASSWORD : Password should be at least 6 characters": "รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร",
            "FIREBASE_WEB_API_KEY not set in secrets.": "ยังไม่ได้ตั้งค่า FIREBASE_WEB_API_KEY — ติดต่อผู้ดูแลระบบ",
        }.get(msg, msg)
        return False, friendly
    # Update display name
    id_token = res.get("idToken", "")
    if id_token and display_name:
        _auth_request("update", {"idToken": id_token, "displayName": display_name,
                                  "returnSecureToken": False})
    st.session_state["student_auth"] = {
        "uid":          res.get("localId", ""),
        "email":        res.get("email", email),
        "display_name": display_name,
        "id_token":     id_token,
    }
    return True, ""


def _auth_login(email: str, password: str) -> tuple[bool, str]:
    """Login existing student. Returns (success, error_msg)."""
    res = _auth_request("signInWithPassword", {
        "email": email, "password": password, "returnSecureToken": True
    })
    if "error" in res:
        msg = res["error"].get("message", "Unknown error")
        friendly = {
            "EMAIL_NOT_FOUND":      "ไม่พบอีเมลนี้ในระบบ กรุณาสมัครก่อน",
            "INVALID_PASSWORD":     "รหัสผ่านไม่ถูกต้อง",
            "INVALID_LOGIN_CREDENTIALS": "อีเมลหรือรหัสผ่านไม่ถูกต้อง",
            "USER_DISABLED":        "บัญชีนี้ถูกระงับ ติดต่อผู้ดูแลระบบ",
            "TOO_MANY_ATTEMPTS_TRY_LATER": "พยายาม login มากเกินไป กรุณารอสักครู่",
            "FIREBASE_WEB_API_KEY not set in secrets.": "ยังไม่ได้ตั้งค่า FIREBASE_WEB_API_KEY — ติดต่อผู้ดูแลระบบ",
        }.get(msg, msg)
        return False, friendly
    st.session_state["student_auth"] = {
        "uid":          res.get("localId", ""),
        "email":        res.get("email", email),
        "display_name": res.get("displayName", email.split("@")[0]),
        "id_token":     res.get("idToken", ""),
    }
    return True, ""


def _auth_reset_password(email: str) -> tuple[bool, str]:
    """Send password-reset email."""
    res = _auth_request("sendOobCode", {"requestType": "PASSWORD_RESET", "email": email})
    if "error" in res:
        return False, res["error"].get("message", "Error")
    return True, ""


def _student_logged_in() -> bool:
    return bool(st.session_state.get("student_auth", {}).get("uid"))


def _student_info() -> dict:
    return st.session_state.get("student_auth", {})


def _render_student_login_page():
    """Full student login/register UI. Returns True if already logged in."""
    if _student_logged_in():
        return True

    # Check if Firebase Web API key is configured — if not, skip auth gracefully
    if not _firebase_web_api_key():
        return True   # allow anonymous access when auth not configured

    st.markdown(
        '<div style="max-width:440px;margin:3rem auto 0;">'
        '<div style="background:linear-gradient(135deg,#0D1B2A,#1A2F47);'
        'border-radius:16px;padding:2rem;text-align:center;margin-bottom:1.5rem;">'
        '<div style="font-family:Playfair Display,serif;font-size:1.8rem;'
        'font-weight:700;color:#F0D98A;">MathComp ✦</div>'
        '<div style="color:rgba(255,255,255,.55);font-size:.85rem;margin-top:.25rem;">'
        'เข้าสู่ระบบเพื่อดูประวัติและรับ Certificate</div></div>',
        unsafe_allow_html=True,
    )

    tab_login, tab_register = st.tabs(["🔑 เข้าสู่ระบบ", "📝 สมัครใหม่"])

    with tab_login:
        with st.form("student_login_form"):
            email_l = st.text_input("อีเมล", placeholder="yourname@email.com")
            pass_l  = st.text_input("รหัสผ่าน", type="password")
            col_btn, col_forgot = st.columns([2, 1])
            with col_btn:
                do_login = st.form_submit_button("เข้าสู่ระบบ", type="primary",
                                                  use_container_width=True)
            with col_forgot:
                do_reset = st.form_submit_button("ลืมรหัสผ่าน?", use_container_width=True)

        if do_login:
            if not email_l or not pass_l:
                st.error("กรุณากรอกอีเมลและรหัสผ่าน")
            else:
                ok, err = _auth_login(email_l.strip(), pass_l)
                if ok:
                    st.success("✓ เข้าสู่ระบบสำเร็จ!")
                    st.rerun()
                else:
                    st.error(f"❌ {err}")

        if do_reset:
            if not email_l:
                st.error("กรุณากรอกอีเมลก่อน")
            else:
                ok, err = _auth_reset_password(email_l.strip())
                if ok:
                    st.success("✉️ ส่งลิงก์รีเซ็ตรหัสผ่านไปที่อีเมลแล้ว")
                else:
                    st.error(f"❌ {err}")

        st.caption("ยังไม่มีบัญชี? คลิกแท็บ 'สมัครใหม่' ด้านบน")

    with tab_register:
        with st.form("student_register_form"):
            reg_name  = st.text_input("ชื่อ-นามสกุล *", placeholder="เช่น สมชาย ใจดี")
            reg_email = st.text_input("อีเมล *", placeholder="yourname@email.com")
            reg_pass  = st.text_input("รหัสผ่าน * (อย่างน้อย 6 ตัว)", type="password")
            reg_pass2 = st.text_input("ยืนยันรหัสผ่าน *", type="password")
            do_signup = st.form_submit_button("สมัครสมาชิก", type="primary",
                                               use_container_width=True)

        if do_signup:
            if not reg_name or not reg_email or not reg_pass:
                st.error("กรุณากรอกข้อมูลให้ครบ")
            elif len(reg_pass) < 6:
                st.error("รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร")
            elif reg_pass != reg_pass2:
                st.error("รหัสผ่านไม่ตรงกัน")
            else:
                ok, err = _auth_signup(reg_email.strip(), reg_pass, reg_name.strip())
                if ok:
                    st.success("✓ สมัครสมาชิกสำเร็จ! กำลังเข้าสู่ระบบ…")
                    st.rerun()
                else:
                    st.error(f"❌ {err}")

    st.markdown("</div>", unsafe_allow_html=True)
    st.stop()   # don't render the exam page until logged in


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — AI HELPERS
# ══════════════════════════════════════════════════════════════════════════════
# ── CONFIG stored in secrets ────────────────────────────────────────────────
# ADMIN_EMAIL is read from Streamlit secrets (key: ADMIN_EMAIL).
# Falls back to env var, then to a safe default that blocks all logins.
def _get_admin_email() -> str:
    try:
        val = st.secrets.get("ADMIN_EMAIL", "")
        if val:
            return str(val).strip().lower()
    except Exception:
        pass
    val = os.environ.get("ADMIN_EMAIL", "")
    if val:
        return val.strip().lower()
    return ""   # empty → no one can log in until secret is set

SECRETS_FILE  = Path(__file__).parent / ".streamlit" / "secrets.toml"


def _read_secrets() -> dict:
    """Parse .streamlit/secrets.toml as a flat dict (handles Streamlit Cloud too)."""
    result = {}
    # Try Streamlit secrets first (works on Cloud and locally)
    for key in ["ANTHROPIC_API_KEY", "ADMIN_PASSWORD", "ADMIN_EMAIL"]:
        try:
            val = st.secrets[key]
            if val:
                result[key] = str(val)
        except Exception:
            pass
    # Fallback: read file directly (for local dev)
    if SECRETS_FILE.exists():
        try:
            import re as _re
            text = SECRETS_FILE.read_text(encoding="utf-8")
            for line in text.splitlines():
                m = _re.match(r'^\s*([A-Z_]+)\s*=\s*"(.*)"', line)
                if m:
                    result.setdefault(m.group(1), m.group(2))
        except Exception:
            pass
    # Fallback: environment
    for key in ["ANTHROPIC_API_KEY", "ADMIN_PASSWORD", "ADMIN_EMAIL"]:
        if key not in result:
            val = os.environ.get(key, "")
            if val:
                result[key] = val
    return result


def _write_secret(key: str, value: str) -> bool:
    """Write a single secret to the local secrets.toml file."""
    try:
        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if SECRETS_FILE.exists():
            import re as _re
            for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
                m = _re.match(r'^\s*([A-Z_]+)\s*=\s*"(.*)"', line)
                if m:
                    existing[m.group(1)] = m.group(2)
        existing[key] = value
        lines = [f'{k} = "{v}"' for k, v in existing.items()]
        SECRETS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def _get_api_key() -> str:
    """Read API key — session state first, then permanent secrets."""
    if st.session_state.get("api_key"):
        return st.session_state["api_key"]
    return _read_secrets().get("ANTHROPIC_API_KEY", "")


def _get_admin_password() -> str:
    """Read admin password from permanent secrets."""
    return _read_secrets().get("ADMIN_PASSWORD", "")


def _ai_client():
    import anthropic
    key = _get_api_key()
    if not key:
        raise ValueError("No API key found. Add it in Admin -> Settings.")
    return anthropic.Anthropic(api_key=key)


# ── ADMIN AUTH ───────────────────────────────────────────────────────────────
import hashlib as _hashlib

def _hash(pw: str) -> str:
    return _hashlib.sha256(pw.encode()).hexdigest()


def _check_admin_login(email: str, password: str) -> tuple[bool, str]:
    """Returns (success, error_message)."""
    admin_email = _get_admin_email()
    if not admin_email:
        return False, "ADMIN_EMAIL not set in Streamlit secrets. Contact system administrator."
    if email.strip().lower() != admin_email:
        return False, "This email is not authorised as admin."
    stored = _get_admin_password()
    if not stored:
        return False, "No admin password set. Add ADMIN_PASSWORD to Streamlit secrets."
    if _hash(password) != stored:
        return False, "Incorrect password."
    return True, ""


def _admin_login_page():
    """Render the admin login form. Returns True if already logged in."""
    if st.session_state.get("admin_logged_in"):
        return True

    st.markdown(
        '''<div style="max-width:420px;margin:3rem auto;">
        <div style="background:linear-gradient(135deg,#0D1B2A,#1A2F47);border-radius:16px;
        padding:2rem 2rem 1.5rem;text-align:center;margin-bottom:1.5rem;">
        <div style="font-family:Playfair Display,serif;font-size:1.6rem;font-weight:700;
        color:#F0D98A;">MathComp ✦</div>
        <div style="color:rgba(255,255,255,.55);font-size:.85rem;margin-top:.3rem;">
        Admin Portal — Authorised Access Only</div></div>''',
        unsafe_allow_html=True,
    )

    with st.form("admin_login_form"):
        email = st.text_input("Email address", placeholder="Enter your email address",
                              autocomplete="email")
        password = st.text_input("Password", type="password",
                                 placeholder="Enter admin password")
        submitted = st.form_submit_button("🔐 Sign In", type="primary",
                                          use_container_width=True)

    if submitted:
        ok, err = _check_admin_login(email, password)
        if ok:
            st.session_state["admin_logged_in"] = True
            st.session_state["admin_email"] = email.strip().lower()
            st.rerun()
        else:
            st.error(f"❌ {err}")

    st.markdown(
        '''<div style="max-width:420px;margin:.75rem auto;font-size:12px;
        color:var(--color-text-tertiary);text-align:center;">
        Only the registered administrator can access this portal.</div>''',
        unsafe_allow_html=True,
    )
    return False


def ai_generate_solution(question_body: str, options: list, competition: str = "") -> dict:
    opts_text = "\n".join(f"  {chr(65+i)}. {o}" for i, o in enumerate(options))
    prompt = (
        f"You are an expert mathematics competition tutor for {competition or 'math competition'}.\n"
        f"Question:\n{question_body}\n\nOptions:\n{opts_text}\n\n"
        "Write a detailed solution using STRICT LaTeX formatting:\n"
        "- ALL math in $...$ delimiters e.g. $x^2+y^2$\n"
        "- Fractions: $\\frac{a}{b}$, roots: $\\sqrt{x}$, exponents: $x^{n}$\n"
        "- Key steps on their own line using $$...$$\n"
        "- Greek letters: $\\alpha$, $\\theta$, $\\pi$ etc.\n"
        "\n"
        "Respond ONLY in JSON (no markdown fences):\n"
        '{"correct_index":0,"difficulty":"easy|intermediate|advanced",'
        '"topic":"Algebra|Geometry|Number Theory|Combinatorics|Word Problem|Others",'
        '"solution":"step-by-step solution with ALL math in $LaTeX$"}'
    )
    try:
        resp = _ai_client().messages.create(
            model="claude-sonnet-4-6", max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        return {"correct_index": 0, "difficulty": "intermediate",
                "topic": "Others", "solution": f"⚠️ AI error: {e}"}


def ai_extract_image(image_bytes: bytes, mime: str = "image/png", competition: str = "") -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode()
    prompt = (
        f"You are an expert mathematics competition typesetter for {competition or 'a math competition'}.\n"
        "Read this question image carefully and extract ALL content with STRICT LaTeX formatting.\n"
        "\n"
        "=== MANDATORY LaTeX RULES ===\n"
        "- ALL math expressions MUST use LaTeX delimiters\n"
        "- Inline math: $...$ e.g. $x^2 + y^2 = z^2$\n"
        "- Fractions: $\\frac{a}{b}$ — NEVER write a/b\n"
        "- Exponents: $x^{2}$, $2^{10}$ — NEVER write x^2 outside $\n"
        "- Roots: $\\sqrt{x}$, $\\sqrt[3]{x}$\n"
        "- Operators: $\\times$, $\\div$, $\\pm$, $\\leq$, $\\geq$, $\\neq$\n"
        "- Greek: $\\alpha$, $\\beta$, $\\theta$, $\\pi$, $\\omega$\n"
        "- Trig/log: $\\sin$, $\\cos$, $\\tan$, $\\log$, $\\ln$\n"
        "- Geometry: $\\angle ABC$, $\\triangle ABC$, $\\overline{AB}$, $60^\\circ$\n"
        "- Binomial: $\\binom{n}{k}$\n"
        "- Summation: $\\sum_{i=1}^{n} i$\n"
        "- Sets: $\\mathbb{R}$, $\\mathbb{Z}$, $\\mathbb{N}$\n"
        "- Display equations: $$x = \\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}$$\n"
        "\n"
        "BAD: If x+y=10 and xy=21 find x^2+y^2\n"
        "GOOD: If $x+y=10$ and $xy=21$, find $x^2+y^2$\n"
        "BAD options: [1/2, 3/4, 5/6]\n"
        "GOOD options: [$\\frac{1}{2}$, $\\frac{3}{4}$, $\\frac{5}{6}$]\n"
        "\n"
        "Respond ONLY in valid JSON (absolutely no markdown, no extra text):\n"
        "{\n"
        '  "question": "Full question with ALL math in $LaTeX$",\n'
        '  "options": ["Each option with $LaTeX$ math"],\n'
        '  "correct_index": 0,\n'
        '  "difficulty": "easy|intermediate|advanced",\n'
        '  "topic": "Algebra|Geometry|Number Theory|Combinatorics|Word Problem|Others",\n'
        '  "solution": "Full solution — ALL math in $LaTeX$, key steps in $$LaTeX$$",\n'
        '  "question_type": "mcq5|mcq4|fill"\n'
        "}"
    )
    try:
        resp = _ai_client().messages.create(
            model="claude-sonnet-4-6", max_tokens=2000,
            messages=[{"role": "user", "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()
        result = json.loads(raw)
        # Flag if LaTeX appears missing (math chars outside $)
        q = result.get("question", "")
        plain_math = any(p in q for p in ['^', '_', '/']) and '$' not in q
        if plain_math:
            result["_latex_warning"] = True
        return result
    except Exception as e:
        return {"question": "⚠️ Could not extract.", "options": ["A", "B", "C", "D"],
                "correct_index": 0, "difficulty": "intermediate",
                "topic": "Others", "solution": f"⚠️ {e}", "question_type": "mcq5"}


def ai_analyse_performance(results: list) -> str:
    correct = sum(1 for r in results if r["is_correct"])
    total = len(results)
    summary = "\n".join(
        f"- Q{i+1} [{r['topic']}/{r['difficulty']}]: {'✓' if r['is_correct'] else '✗'}"
        for i, r in enumerate(results)
    )
    prompt = (
        f"Student completed {total}-question math competition mock exam.\n"
        f"Score: {correct}/{total}\nResults:\n{summary}\n\n"
        "Write a concise (≤200 words) personalised analysis in English. Include:\n"
        "1. Overall verdict\n2. Strongest topic\n3. Weakest topic + improvement tip\n"
        "4. Motivational closing line\nUse markdown."
    )
    try:
        resp = _ai_client().messages.create(
            model="claude-sonnet-4-6", max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"⚠️ Analysis unavailable: {e}"


def ai_generate_figure(description: str) -> str:
    """
    Ask Claude to generate an SVG figure from a text description.
    Returns raw SVG string ready to embed in HTML, or empty string on failure.
    """
    prompt = (
        "You are a mathematical figure illustrator. "
        "Generate a clean, accurate SVG figure for a math competition question.\n\n"
        f"Figure description: {description}\n\n"
        "Rules:\n"
        "- Output ONLY the raw SVG code — no markdown, no explanation, no backticks\n"
        "- Start with <svg ... > and end with </svg>\n"
        "- Use viewBox=\"0 0 400 300\" width=\"400\" height=\"300\"\n"
        "- Use clean lines, black strokes (#333), white or light grey fill\n"
        "- Add clear labels (angles, lengths, points) using <text> elements\n"
        "- Font: font-family=\"Arial\" font-size=\"13\"\n"
        "- Keep it simple and precise — this is for a math exam\n"
        "- Common figures: triangles, circles, rectangles, number lines, "
        "coordinate axes, 3D shapes (drawn in 2D perspective)\n"
    )
    try:
        resp = _ai_client().messages.create(
            model="claude-sonnet-4-6", max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        svg = resp.content[0].text.strip()
        # Clean up — remove any accidental markdown fences
        svg = re.sub(r"^```[a-z]*\n?", "", svg)
        svg = re.sub(r"\n?```$", "", svg)
        svg = svg.strip()
        if not svg.startswith("<svg"):
            return ""
        return svg
    except Exception as e:
        return f"<!-- SVG generation failed: {e} -->"


def _img_to_b64(img_bytes: bytes, mime: str = "image/png") -> str:
    """Convert image bytes to a data-URI string for storage."""
    b64 = base64.standard_b64encode(img_bytes).decode()
    return f"data:{mime};base64,{b64}"


def _render_figure(q: dict):
    """Display a question's figure — handles uploaded images and SVG strings."""
    fig = q.get("figure")
    if not fig:
        return
    if isinstance(fig, str) and fig.startswith("data:image"):
        # Uploaded image stored as data-URI
        st.markdown(
            f'''<div style="text-align:center;margin:.75rem 0;">
            <img src="{fig}" style="max-width:100%;max-height:320px;
            border:1px solid #DDD8CC;border-radius:8px;"/>
            </div>''',
            unsafe_allow_html=True,
        )
    elif isinstance(fig, str) and fig.strip().startswith("<svg"):
        # AI-generated SVG
        st.markdown(
            f'''<div style="text-align:center;margin:.75rem 0;
            background:#FAFAFA;border:1px solid #DDD8CC;border-radius:8px;padding:.5rem;">
            {fig}
            </div>''',
            unsafe_allow_html=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2b — PDF BATCH IMPORT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _pdf_to_page_images(pdf_bytes: bytes, dpi: int = 200) -> list[bytes]:
    """Convert PDF pages to PNG images at given DPI. Returns list of PNG bytes."""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    imgs = []
    for page in doc:
        pix = page.get_pixmap(matrix=mat, alpha=False)
        imgs.append(pix.tobytes("png"))
    doc.close()
    return imgs


def _whitespace_crop(page_png: bytes, min_gap: int = 20) -> list[bytes]:
    """
    Auto-detect question regions by finding horizontal whitespace gaps.
    Best for clean digital PDFs.
    Returns list of cropped PNG bytes.
    """
    try:
        import numpy as np
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(page_png)).convert("L")
        arr = np.array(img)
        h, w = arr.shape
        # Row is a gap if >98% pixels are very white
        row_gap = (arr > 248).sum(axis=1) > int(w * 0.98)
        # Find gap start positions (runs of ≥ min_gap gap rows)
        splits = [0]
        run = 0
        for y, g in enumerate(row_gap):
            if g:
                run += 1
            else:
                if run >= min_gap:
                    splits.append(y - run // 2)
                run = 0
        splits.append(h)
        splits = sorted(set(splits))
        # Remove splits that are too close together
        min_h = max(h // 25, 40)
        filtered = [splits[0]]
        for s in splits[1:]:
            if s - filtered[-1] >= min_h:
                filtered.append(s)
        if filtered[-1] < h:
            filtered.append(h)
        # Crop each region
        src_img = Image.open(_io.BytesIO(page_png))
        crops = []
        for i in range(len(filtered) - 1):
            c = src_img.crop((0, filtered[i], w, filtered[i + 1]))
            buf = _io.BytesIO()
            c.save(buf, "PNG")
            crops.append(buf.getvalue())
        return crops if len(crops) > 1 else [page_png]
    except Exception:
        return [page_png]


def _equal_crop(page_png: bytes, n: int, padding: int = 15) -> list[bytes]:
    """
    Divide page into n equal horizontal strips with padding.
    Best for scanned PDFs with consistent layout.
    """
    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(page_png))
        w, h = img.size
        strip = h // n
        crops = []
        for i in range(n):
            t = max(0, i * strip - padding)
            b = min(h, (i + 1) * strip + padding)
            c = img.crop((0, t, w, b))
            buf = _io.BytesIO()
            c.save(buf, "PNG")
            crops.append(buf.getvalue())
        return crops
    except Exception:
        return [page_png] * n


def _ai_count_questions_on_page(page_png: bytes) -> int:
    """Ask Claude how many questions are on this page. Returns count (1–15)."""
    try:
        b64 = base64.standard_b64encode(page_png).decode()
        resp = _ai_client().messages.create(
            model="claude-sonnet-4-6", max_tokens=20,
            messages=[{"role": "user", "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text",
                 "text": "Count the number of distinct math problems/questions on this page. "
                         "Reply with ONLY a single integer number, nothing else."},
            ]}],
        )
        return int(resp.content[0].text.strip())
    except Exception:
        return 1


def _ai_extract_one_question(crop_png: bytes, competition: str,
                              q_num: int, page_num: int) -> dict:
    """
    Extract a single question from a cropped image with strict LaTeX formatting.
    Returns a question dict ready for the question bank.
    """
    b64 = base64.standard_b64encode(crop_png).decode()
    prompt = (
        f"This is question #{q_num} from a {competition or 'math competition'} paper.\n"
        "Extract this question with STRICT LaTeX formatting rules:\n"
        "\n"
        "MANDATORY LaTeX RULES — no exceptions:\n"
        "• ALL math in $...$ delimiters: $x^2+y^2$, $\\frac{{a}}{{b}}$, $\\sqrt{{x}}$\n"
        "• NEVER write fractions as a/b — always $\\frac{{a}}{{b}}$\n"
        "• NEVER write exponents as x^2 outside $ — always $x^2$\n"
        "• Operators: $\\times$, $\\div$, $\\pm$, $\\leq$, $\\geq$, $\\neq$\n"
        "• Angles: $60^\\circ$, $\\angle ABC$\n"
        "• Triangle: $\\triangle ABC$, segment: $\\overline{{AB}}$\n"
        "• Greek: $\\alpha$, $\\beta$, $\\theta$, $\\pi$, $\\omega$\n"
        "• Trig: $\\sin x$, $\\cos x$, $\\tan x$\n"
        "• Log: $\\log_{{2}} n$, $\\ln x$\n"
        "• Binomial: $\\binom{{n}}{{k}}$\n"
        "• Sum/product: $\\sum_{{i=1}}^{{n}}$, $\\prod$\n"
        "• Display key steps: $$x = \\frac{{-b \\pm \\sqrt{{b^2-4ac}}}}{{2a}}$$\n"
        "\n"
        "If a figure/diagram is present in the image, include [FIGURE] in the question text.\n"
        "\n"
        "Respond ONLY in JSON (no markdown, no extra text):\n"
        "{\n"
        '  "question": "Full question text with ALL math in $LaTeX$",\n'
        '  "options": ["$\\\\frac{1}{2}$", ...],\n'
        '  "correct_index": 0,\n'
        '  "question_type": "mcq5|mcq4|fill",\n'
        '  "difficulty": "easy|intermediate|advanced",\n'
        '  "topic": "Algebra|Geometry|Number Theory|Combinatorics|Word Problem|Others",\n'
        '  "solution": "Step-by-step — ALL math in $LaTeX$, key steps in $$LaTeX$$",\n'
        '  "has_figure": false\n'
        "}"
    )
    try:
        resp = _ai_client().messages.create(
            model="claude-sonnet-4-6", max_tokens=2000,
            messages=[{"role": "user", "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw.strip())
        result["_page"]         = page_num
        result["_q_num"]        = q_num
        result["_crop_png"]     = crop_png   # store crop for preview
        result["question_number"] = q_num
        return result
    except Exception as e:
        return {
            "question":       f"⚠️ Could not extract Q{q_num} (page {page_num}): {e}",
            "options":        [], "correct_index": 0,
            "question_type":  "mcq5", "difficulty": "intermediate",
            "topic":          "Others", "solution": "",
            "_page":          page_num, "_q_num": q_num,
            "_crop_png":      crop_png, "question_number": q_num,
            "_error":         True, "has_figure": False,
        }


def _ai_extract_image(image_bytes: bytes, mime: str = "image/png",
                      competition: str = "") -> dict:
    """Alias kept for compatibility."""
    return _ai_extract_one_question(image_bytes, competition, 1, 1)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — GLOBAL CSS
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("""
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600;700&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{--gold:#C9A84C;--gold-light:#F0D98A;--navy:#0D1B2A;--navy-mid:#1A2F47;
  --cream:#FAF7F0;--cream-dark:#EDE8DC;--success:#2D7D4F;--danger:#C0392B;
  --warning:#B8860B;--info:#1A5F9E;--border:#DDD8CC;--radius:12px;}
html,body,[class*="css"]{font-family:'DM Sans',sans-serif!important;}
.block-container{padding-top:1.5rem!important;}
section[data-testid="stSidebar"]{background:var(--navy)!important;}
section[data-testid="stSidebar"] *{color:rgba(255,255,255,0.85)!important;}
div[data-testid="metric-container"]{background:#fff;border:1px solid var(--border);
  border-radius:var(--radius);padding:1rem!important;box-shadow:0 2px 8px rgba(13,27,42,0.06);}
.stButton>button{border-radius:9px!important;font-family:'DM Sans',sans-serif!important;font-weight:500!important;}
.stButton>button[kind="primary"]{background:var(--navy)!important;color:#fff!important;border:none!important;}
h1,h2,h3{font-family:'Playfair Display',serif!important;color:var(--navy)!important;}
.stTabs [data-baseweb="tab-list"]{background:var(--cream)!important;border-radius:10px!important;padding:4px!important;}
.stTabs [aria-selected="true"]{background:var(--navy)!important;color:#fff!important;}
div[data-testid="stAlert"]{border-radius:10px!important;}
.hero{background:linear-gradient(135deg,#0D1B2A 60%,#1A2F47);border-radius:16px;
  padding:2rem 2.5rem;margin-bottom:1.5rem;color:#fff;}
.hero h1{color:var(--gold-light)!important;margin:0 0 .4rem;font-size:2rem;}
.hero p{color:rgba(255,255,255,.65);margin:0;font-size:.95rem;}
.q-card{background:#fff;border:1px solid var(--border);border-radius:14px;
  padding:1.5rem;margin-bottom:1rem;box-shadow:0 2px 12px rgba(13,27,42,.06);}
.badge{display:inline-block;padding:3px 11px;border-radius:20px;font-size:12px;font-weight:600;}
.badge-easy{background:#E8F5EE;color:#2D7D4F;}
.badge-int{background:#FFF8E0;color:#B8860B;}
.badge-adv{background:#FDECEA;color:#C0392B;}
.badge-gold{background:rgba(201,168,76,.15);color:#8B6914;}
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — COMPETITION GROUPS
# ══════════════════════════════════════════════════════════════════════════════
COMP_GROUPS = {
    "🇦🇺 Australian Mathematics Competition": {
        "AMC-MP": "Middle Primary", "AMC-UP": "Upper Primary",
        "AMC-JR": "Junior", "AMC-INT": "Intermediate", "AMC-SR": "Senior",
    },
    "🇺🇸 American Mathematics Competition": {
        "AMC8": "AMC 8", "AMC10": "AMC 10", "AMC12": "AMC 12", "AIME": "AIME",
    },
    "🎌 Sansu Olympic": {
        "SANSU-KB": "Kidbee", "SANSU-JR": "Junior", "SANSU-SR": "Senior",
    },
    "🇹🇭 สอวน. รอบแรก": {"POSN-R1": "สอวน. รอบแรก"},
    "🇹🇭 สมาคมคณิตศาสตร์แห่งประเทศไทย": {
        "MATH-PT": "ระดับประถม", "MATH-MT": "มัธยมต้น", "MATH-UP": "มัธยมปลาย",
    },
}

DIFF_COLORS = {
    "easy": ("#2D7D4F", "#E8F5EE"),
    "intermediate": ("#B8860B", "#FFF8E0"),
    "advanced": ("#C0392B", "#FDECEA"),
}

# Grade-based practice mode — maps grade to typical competition levels
GRADE_LEVELS = {
    "Grade 1–3":   {
        "label": "Grade 1–3",   "age": "6–9 yrs",
        "comps": ["AMC-MP"],
        "icon": "🌱",
        "note": "Australian MC Middle Primary",
    },
    "Grade 4–5":   {
        "label": "Grade 4–5",   "age": "9–11 yrs",
        "comps": ["AMC-MP", "AMC-UP", "SANSU-KB", "SANSU-JR", "MATH-PT"],
        "icon": "🌿",
        "note": "Australian MC Upper Primary, Sansu Kidbee & Junior, สมาคม ประถม",
    },
    "Grade 6":     {
        "label": "Grade 6",     "age": "11–12 yrs",
        "comps": ["AMC-UP", "AMC-JR", "SANSU-JR", "MATH-PT"],
        "icon": "🌳",
        "note": "Australian MC Upper Primary & Junior, Sansu Junior, สมาคม ประถม",
    },
    "Grade 7–8":   {
        "label": "Grade 7–8",   "age": "12–14 yrs",
        "comps": ["AMC-JR", "AMC8", "AMC-INT", "SANSU-SR", "MATH-MT", "POSN-R1"],
        "icon": "⭐",
        "note": "Australian MC Junior & Int, AMC 8, Sansu Senior, สมาคม มัธยมต้น, สอวน.",
    },
    "Grade 9":     {
        "label": "Grade 9",     "age": "14–15 yrs",
        "comps": ["AMC8", "AMC10", "AMC-INT", "SANSU-SR", "MATH-MT", "POSN-R1"],
        "icon": "🔥",
        "note": "AMC 8 & 10, Australian MC Int, Sansu Senior, สมาคม มัธยมต้น, สอวน.",
    },
    "Grade 10–11": {
        "label": "Grade 10–11", "age": "15–17 yrs",
        "comps": ["AMC10", "AMC12", "AMC-SR", "MATH-MT", "MATH-UP", "POSN-R1"],
        "icon": "🚀",
        "note": "AMC 10 & 12, Australian MC Senior, สมาคม มัธยมต้น & ปลาย, สอวน.",
    },
    "Grade 12":    {
        "label": "Grade 12",    "age": "17–18 yrs",
        "comps": ["AMC12", "AMC-SR", "AIME", "MATH-UP", "POSN-R1"],
        "icon": "🏆",
        "note": "AMC 12, Australian MC Senior, AIME, สมาคม มัธยมปลาย, สอวน.",
    },
}


# Default number of questions per competition (official exam length)
COMP_DEFAULT_Q = {
    "AMC8":     25,
    "AMC10":    25,
    "AMC12":    25,
    "AMC-MP":   30,
    "AMC-UP":   30,
    "AMC-JR":   30,
    "AMC-INT":  30,
    "AMC-SR":   30,
    "AIME":     15,
    "SANSU-KB": 25,
    "SANSU-JR": 25,
    "SANSU-SR": 25,
    "POSN-R1":  30,
    "MATH-PT":  25,
    "MATH-MT":  25,
    "MATH-UP":  25,
}

def _default_q_count(mode: str, comp_code: str = "", grade_key: str = "") -> int:
    """Return the default number of questions for a given selection."""
    if mode == "competition" and comp_code in COMP_DEFAULT_Q:
        return COMP_DEFAULT_Q[comp_code]
    return 10   # fallback default for grade mode or unknown competitions


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — RADAR CHART
# ══════════════════════════════════════════════════════════════════════════════
def render_radar(scores_by_topic: dict):
    import plotly.graph_objects as go
    cats = TOPICS
    vals = []
    for t in cats:
        c, tot = scores_by_topic.get(t, (0, 0))
        vals.append(round(c / tot * 100, 1) if tot else 0)
    vals_c = vals + [vals[0]]
    cats_c = cats + [cats[0]]
    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(
        r=vals_c, theta=cats_c, fill="toself",
        fillcolor="rgba(201,168,76,0.18)",
        line=dict(color="#C9A84C", width=2.5),
        marker=dict(size=7, color="#0D1B2A"), name="Score %",
    ))
    fig.update_layout(
        polar=dict(
            bgcolor="rgba(250,247,240,0.6)",
            radialaxis=dict(visible=True, range=[0, 100], ticksuffix="%",
                            tickfont=dict(size=10), gridcolor="rgba(13,27,42,0.12)"),
            angularaxis=dict(tickfont=dict(size=12), gridcolor="rgba(13,27,42,0.1)"),
        ),
        showlegend=False, paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(t=30, b=30, l=50, r=50), height=380,
    )
    st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — STUDENT PAGE
# ══════════════════════════════════════════════════════════════════════════════
def _init_student():
    defaults = {
        "exam_step": "setup", "setup_step": 0,
        "exam_mode": None,          # "competition" | "grade"
        "selected_comp": None,
        "selected_grade": None,     # e.g. "Grade 7-8"
        "selected_diff": "mixed",
        "q_count": 10, "time_limit": 60, "show_sol": "after",
        "exam_questions": [], "exam_answers": {},
        "exam_start": None, "exam_current": 0, "exam_submitted": False,
        # Student identity
        "student_first_name": "",
        "student_last_name": "",
        "student_school": "",
        "student_class_pin": "",    # Optional 6-digit class PIN issued by teacher
        # Competition-level overrides (set when a custom comp is chosen)
        "comp_show_score": True,
        "comp_show_solution": True,
        "comp_show_analysis": True,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def page_student():
    _init_student()

    # ── Auth gate (soft — skips if FIREBASE_WEB_API_KEY not set) ──────────
    _render_student_login_page()

    # ── Pre-fill name from Firebase Auth if not already set ────────────────
    if _student_logged_in():
        info = _student_info()
        display = info.get("display_name", "")
        if display and not st.session_state.get("student_first_name"):
            parts = display.strip().split(" ", 1)
            st.session_state["student_first_name"] = parts[0]
            st.session_state["student_last_name"]  = parts[1] if len(parts) > 1 else ""
        if info.get("email") and not st.session_state.get("student_school"):
            pass  # could pre-fill school too if stored in profile later

    # ── Top nav bar for logged-in students ─────────────────────────────────
    if _student_logged_in():
        info = _student_info()
        nav1, nav2, nav3 = st.columns([4, 2, 1])
        with nav1:
            st.markdown(
                f'<div style="font-size:13px;color:#888;padding:.4rem 0;">'
                f'👋 สวัสดี <strong>{info.get("display_name") or info.get("email","")}</strong></div>',
                unsafe_allow_html=True,
            )
        with nav2:
            if st.button("📈 ประวัติของฉัน", use_container_width=True):
                st.session_state["show_progress"] = True
                st.rerun()
        with nav3:
            if st.button("ออก", use_container_width=True):
                st.session_state.pop("student_auth", None)
                st.rerun()

    # ── Progress dashboard page ────────────────────────────────────────────
    if st.session_state.get("show_progress"):
        _page_student_progress()
        return

    st.markdown(
        '<div class="hero"><h1>Practice Like a Champion ✦</h1>'
        '<p>Choose by competition series or by your school grade — then pick your difficulty and start practising.</p></div>',
        unsafe_allow_html=True,
    )

    step = st.session_state.exam_step
    if step == "setup":
        _student_setup()
    elif step == "exam":
        _student_exam()
    elif step == "results":
        _student_results()


# ── Setup helpers ─────────────────────────────────────────────────────────────
def _step_indicator(steps, current):
    si = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:1.5rem;">'
    for i, lbl in enumerate(steps, 1):
        done   = i < current
        active = i == current
        bg   = "#2D7D4F" if done else ("#0D1B2A" if active else "#EDE8DC")
        col  = "#fff" if (done or active) else "#888"
        tcol = "#0D1B2A" if (done or active) else "#888"
        num  = "✓" if done else str(i)
        si += (f'<div style="width:26px;height:26px;border-radius:50%;background:{bg};'
               f'display:flex;align-items:center;justify-content:center;font-size:12px;'
               f'font-weight:700;color:{col};">{num}</div>'
               f'<span style="font-size:14px;font-weight:{"600" if active else "400"};color:{tcol};">{lbl}</span>')
        if i < len(steps):
            si += '<div style="flex:1;height:1px;background:#DDD8CC;margin:0 8px;"></div>'
    si += "</div>"
    st.markdown(si, unsafe_allow_html=True)


def _student_setup():
    s    = st.session_state.setup_step
    mode = st.session_state.exam_mode

    # Step 0 — student identity
    if s == 0:
        _step_indicator(["Your Details", "Choose Mode", "Choose Details", "Exam Settings"], 1)
        st.markdown("### Tell us about yourself")
        st.markdown("This helps your teacher track your progress.")
        st.markdown("<br>", unsafe_allow_html=True)

        id_col1, id_col2 = st.columns(2)
        with id_col1:
            fn = st.text_input("First Name *", value=st.session_state.student_first_name,
                               placeholder="e.g. Somchai")
        with id_col2:
            ln = st.text_input("Last Name *", value=st.session_state.student_last_name,
                               placeholder="e.g. Jaidee")
        school = st.text_input("School (optional)", value=st.session_state.student_school,
                               placeholder="e.g. Triam Udom Suksa School")

        # Class PIN — optional, issued by teacher for class-level tracking
        pin_col1, pin_col2 = st.columns([1, 2])
        with pin_col1:
            class_pin = st.text_input(
                "Class PIN (optional)",
                value=st.session_state.student_class_pin,
                placeholder="e.g. 123456",
                max_chars=10,
                help="6-digit code given by your teacher. Leave blank if not applicable.",
            )

        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("Continue →", type="primary", disabled=(not fn.strip() or not ln.strip())):
            st.session_state.student_first_name = fn.strip()
            st.session_state.student_last_name  = ln.strip()
            st.session_state.student_school     = school.strip()
            st.session_state.student_class_pin  = class_pin.strip()
            st.session_state.setup_step = 1
            st.rerun()
        if not fn.strip() or not ln.strip():
            st.caption("* First name and last name are required.")

    # Step 1 — choose mode
    elif s == 1:
        _step_indicator(["Your Details", "Choose Mode", "Choose Details", "Exam Settings"], 2)
        st.markdown("### How would you like to practise?")
        st.markdown("<br>", unsafe_allow_html=True)
        mc1, mc2 = st.columns(2, gap="large")
        with mc1:
            st.markdown(
                """<div style="background:#fff;border:2px solid #DDD8CC;border-radius:14px;
                padding:1.5rem;text-align:center;min-height:180px;display:flex;flex-direction:column;
                align-items:center;justify-content:center;gap:.6rem;">
                <div style="font-size:2.5rem;">🏆</div>
                <div style="font-size:1rem;font-weight:600;color:#0D1B2A;">By Competition</div>
                <div style="font-size:12px;color:#888;line-height:1.5;">
                AMC, AIME, Sansu Olympic,<br>สอวน., สมาคมคณิตศาสตร์ and more</div>
                </div>""",
                unsafe_allow_html=True,
            )
            if st.button("📋 Select Competition", type="primary",
                         use_container_width=True, key="mode_comp"):
                st.session_state.exam_mode = "competition"
                st.session_state.setup_step = 2
                st.rerun()
        with mc2:
            st.markdown(
                """<div style="background:#fff;border:2px solid #DDD8CC;border-radius:14px;
                padding:1.5rem;text-align:center;min-height:180px;display:flex;flex-direction:column;
                align-items:center;justify-content:center;gap:.6rem;">
                <div style="font-size:2.5rem;">🎒</div>
                <div style="font-size:1rem;font-weight:600;color:#0D1B2A;">By Grade Level</div>
                <div style="font-size:12px;color:#888;line-height:1.5;">
                Select your school grade and<br>choose Easy / Intermediate / Advanced</div>
                </div>""",
                unsafe_allow_html=True,
            )
            if st.button("🎒 Select Grade Level", type="primary",
                         use_container_width=True, key="mode_grade"):
                st.session_state.exam_mode = "grade"
                st.session_state.setup_step = 2
                st.rerun()

    # Step 2 — choose competition or grade
    elif s == 2:
        if mode == "competition":
            _step_indicator(["Your Details", "Choose Mode", "Choose Competition", "Exam Settings"], 3)
            _setup_competition()
        else:
            _step_indicator(["Your Details", "Choose Mode", "Choose Grade", "Exam Settings"], 3)
            _setup_grade()

    # Step 3 — settings
    elif s == 3:
        labels = (["Your Details", "Choose Mode", "Choose Competition", "Exam Settings"]
                  if mode == "competition"
                  else ["Your Details", "Choose Mode", "Choose Grade", "Exam Settings"])
        _step_indicator(labels, 4)
        _setup_settings()



def _setup_grade():
    """Grade-based practice selector."""
    st.markdown("### Select Your Grade Level")
    st.markdown("Questions are automatically matched to competitions appropriate for your grade.")
    st.markdown("")

    chosen_grade = st.session_state.get("selected_grade")

    # Grade tiles
    cols = st.columns(3)
    grades = list(GRADE_LEVELS.items())
    for idx, (key, info) in enumerate(grades):
        with cols[idx % 3]:
            is_sel = chosen_grade == key
            border = "2px solid #C9A84C" if is_sel else "1.5px solid #DDD8CC"
            bg     = "rgba(201,168,76,0.12)" if is_sel else "#fff"
            fw     = "600" if is_sel else "400"
            check  = "✓ " if is_sel else ""
            st.markdown(
                f'''<div style="border:{border};border-radius:12px;padding:1rem;
                background:{bg};text-align:center;margin-bottom:4px;">
                <div style="font-size:1.8rem;">{info["icon"]}</div>
                <div style="font-size:14px;font-weight:{fw};color:#0D1B2A;margin-top:.3rem;">
                {check}{info["label"]}</div>
                <div style="font-size:11px;color:#888;margin-top:.2rem;">{info["age"]}</div>
                </div>''',
                unsafe_allow_html=True,
            )
            if st.button(
                "✓ Selected" if is_sel else "Select",
                key=f"grade_{key}", use_container_width=True
            ):
                st.session_state.selected_grade = key
                st.rerun()

    if chosen_grade:
        info = GRADE_LEVELS[chosen_grade]
        st.success(f"✓ **{chosen_grade}** selected")
        st.markdown(
            f'<div style="font-size:12px;color:#555;margin-top:-.5rem;padding:.4rem .9rem;'
            f'background:#f5f5f5;border-radius:6px;">'
            f'📚 Covers: {info.get("note","")}</div>',
            unsafe_allow_html=True,
        )
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("← Back", key="grade_back"):
                st.session_state.setup_step = 1
                st.session_state.exam_mode = None
                st.rerun()
        with c2:
            if st.button("Continue to Settings →", type="primary", key="grade_continue"):
                st.session_state.q_count = 10  # grade mode default
                st.session_state.setup_step = 3
                st.rerun()
    else:
        st.info("☝️ Select your grade level above to continue.")
        if st.button("← Back", key="grade_back_none"):
            st.session_state.setup_step = 1
            st.session_state.exam_mode = None
            st.rerun()


def _setup_competition():
    st.markdown("### Choose a Competition")
    chosen = st.session_state.selected_comp

    for group_name, comps in COMP_GROUPS.items():
        st.markdown(f"**{group_name}**")
        cols = st.columns(len(comps))
        for i, (code, label) in enumerate(comps.items()):
            with cols[i]:
                is_sel = chosen == code
                border = "2px solid #C9A84C" if is_sel else "1.5px solid #DDD8CC"
                bg = "rgba(201,168,76,0.12)" if is_sel else "#fff"
                icon = "✓ " if is_sel else ""
                st.markdown(
                    f'<div style="border:{border};border-radius:9px;padding:8px 10px;'
                    f'background:{bg};text-align:center;font-size:13px;'
                    f'font-weight:{"600" if is_sel else "400"};margin-bottom:4px;">{icon}{label}</div>',
                    unsafe_allow_html=True,
                )
                if st.button("✓ Selected" if is_sel else "Select",
                             key=f"comp_{code}", use_container_width=True):
                    st.session_state.selected_comp = code
                    st.rerun()
        st.markdown("---")

    if chosen:
        cname = next((v for g in COMP_GROUPS.values()
                      for k, v in g.items() if k == chosen), chosen)
        st.success(f"✓ Selected: **{cname}**")
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("← Back", key="comp_back"):
                st.session_state.setup_step = 1
                st.session_state.exam_mode = None
                st.rerun()
        with c2:
            if st.button("Continue to Settings →", type="primary", key="comp_continue"):
                # Load competition-level display settings
                cs = get_competition_settings(chosen)
                st.session_state.comp_show_score    = cs.get("show_score", True)
                st.session_state.comp_show_solution = cs.get("show_solution", True)
                st.session_state.comp_show_analysis = cs.get("show_analysis", True)
                # Set default question count for this competition
                st.session_state.q_count = _default_q_count("competition", comp_code=chosen)
                st.session_state.setup_step = 3
                st.rerun()
    else:
        st.info("☝️ Select a competition above to continue.")
        if st.button("← Back", key="comp_back_none"):
            st.session_state.setup_step = 1
            st.session_state.exam_mode = None
            st.rerun()


def _setup_settings():
    mode = st.session_state.exam_mode

    # Determine label and filter parameters based on mode
    if mode == "competition":
        comp_code = st.session_state.selected_comp
        comp_name = next((v for g in COMP_GROUPS.values()
                          for k, v in g.items() if k == comp_code), comp_code)
        title = f"Settings for **{comp_name}**"
    else:
        grade_key  = st.session_state.selected_grade
        grade_info = GRADE_LEVELS.get(grade_key, {})
        title = f"Settings for **{grade_key}** ({grade_info.get('age','')})"

    st.markdown(f"### {title}")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Difficulty Level**")
        # Both modes now support all four difficulty options including Mixed
        diff_map = {
            "🟢 Easy": "easy",
            "🟡 Intermediate": "intermediate",
            "🔴 Advanced": "advanced",
            "🎲 Mixed (all levels)": "mixed",
        }
        default_idx = 0 if mode == "grade" else 3
        diff_sel = st.radio("Difficulty", list(diff_map.keys()), index=default_idx,
                            label_visibility="collapsed")
        st.session_state.selected_diff = diff_map[diff_sel]

    with col2:
        # Show default for this competition
        mode_now = st.session_state.exam_mode
        comp_now = st.session_state.get("selected_comp","")
        default_q = _default_q_count(mode_now, comp_code=comp_now)
        all_comp_q = get_all_competitions()

        st.markdown("**Number of Questions**")
        if mode_now == "competition" and comp_now in COMP_DEFAULT_Q:
            st.markdown(
                f'''<div style="font-size:12px;color:#888;margin-bottom:4px;">
                Default for {all_comp_q.get(comp_now, comp_now)}: <strong>{default_q} questions</strong>
                </div>''',
                unsafe_allow_html=True,
            )
        # Ensure q_count doesn't exceed available questions cap
        avail_preview = db_get_filtered(
            comp=comp_now if mode_now=="competition" else None,
            comp_list=GRADE_LEVELS.get(
                st.session_state.get("selected_grade",""), {}
            ).get("comps", []) if mode_now=="grade" else None,
        )
        avail_count = len(avail_preview)
        max_q = max(avail_count, default_q, 5)

        # If q_count is still at the uninitialised default (10) or 0,
        # snap it to the competition default so the slider pre-fills correctly
        if st.session_state.q_count <= 10 and default_q > 10:
            st.session_state.q_count = min(default_q, avail_count) if avail_count > 0 else default_q

        current_q = max(5, min(st.session_state.q_count, max(max_q, 50)))

        # Warn if DB has fewer questions than the official default
        if avail_count == 0:
            st.error(
                f"⚠️ No questions found for **{all_comp_q.get(comp_now, comp_now)}** "
                f"in the database.\n\n"
                f"Please make sure `data/competitions/{comp_now}.json` is uploaded to GitHub."
            )
        elif avail_count < default_q:
            st.warning(
                f"⚠️ Only **{avail_count} questions** available for "
                f"{all_comp_q.get(comp_now, comp_now)} "
                f"(official exam has {default_q}). "
                f"Add more questions via Admin → Add Question."
            )

        st.session_state.q_count = st.slider(
            "Questions", 5, max(max_q, 50), current_q, 1,
            label_visibility="collapsed",
            help=f"Official default: {default_q} · Available in DB: {avail_count}"
        )
        st.markdown("**Time Limit**")
        tl_map = {"No limit": 0, "30 min": 30, "60 min": 60, "90 min": 90, "2 hr": 120}
        tl_sel = st.selectbox("Time", list(tl_map.keys()), index=2,
                              label_visibility="collapsed")
        st.session_state.time_limit = tl_map[tl_sel]
        comp_allows_sol = st.session_state.get("comp_show_solution", True)
        st.markdown("**Show Solution**")
        if not comp_allows_sol:
            st.markdown(
                '<div style="background:#FFF0F0;border:.5px solid #E8A0A0;border-radius:7px;'
                'padding:.4rem .8rem;font-size:12px;color:#8B1A1A;">🔒 Solutions hidden by competition settings</div>',
                unsafe_allow_html=True,
            )
            st.session_state.show_sol = "never"
        else:
            sol_map = {"After each question": "each",
                       "After submitting exam": "after", "Don't show": "never"}
            sol_sel = st.selectbox("Solution", list(sol_map.keys()), index=1,
                                   label_visibility="collapsed")
            st.session_state.show_sol = sol_map[sol_sel]

    st.markdown("---")
    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("← Back"):
            st.session_state.setup_step = 2
            st.rerun()
    with c2:
        # Filter questions based on mode
        if mode == "competition":
            avail = db_get_filtered(comp=comp_code,
                                    difficulty=st.session_state.selected_diff)
        else:
            grade_comps = GRADE_LEVELS.get(grade_key, {}).get("comps", [])
            avail = db_get_filtered(comp_list=grade_comps,
                                    difficulty=st.session_state.selected_diff)

        n = min(st.session_state.q_count, len(avail))
        if n == 0:
            st.warning("⚠️ No questions found for this selection. "
                       "Try a different difficulty or ask your teacher to add more questions.")
        else:
            st.info(f"📚 {len(avail)} questions available — will use {n}.")
            if st.button(f"🚀 Start Exam ({n} questions)", type="primary"):
                random.shuffle(avail)
                st.session_state.exam_questions = avail[:n]
                st.session_state.exam_answers = {}
                st.session_state.exam_current = 0
                st.session_state.exam_start = time.time()
                st.session_state.exam_submitted = False
                st.session_state.exam_step = "exam"
                st.rerun()


# ── Exam ──────────────────────────────────────────────────────────────────────
_ANTICHEAT_JS = """
<script>
(function(){
  if(window._mathcompAC) return;
  window._mathcompAC = true;

  var _warns = parseInt(sessionStorage.getItem('mc_warns') || '0');
  var _banner = document.getElementById('mc-ac-banner');

  function showBanner(msg){
    _warns++;
    sessionStorage.setItem('mc_warns', _warns);
    var b = document.getElementById('mc-ac-banner');
    if(!b) return;
    b.textContent = '⚠️ ' + msg + ' (warning ' + _warns + '/3)';
    b.style.display = 'block';
    setTimeout(function(){ b.style.display='none'; }, 5000);
  }

  // Tab / window visibility change
  document.addEventListener('visibilitychange', function(){
    if(document.visibilityState === 'hidden'){
      showBanner('Tab switch detected — please stay on the exam page');
    }
  });

  // Window blur (alt-tab, clicking outside browser)
  window.addEventListener('blur', function(){
    showBanner('Window focus lost — please return to the exam');
  });

  // Right-click disabled
  document.addEventListener('contextmenu', function(e){ e.preventDefault(); });

  // Common copy-paste shortcuts disabled during exam
  document.addEventListener('keydown', function(e){
    if((e.ctrlKey || e.metaKey) && ['c','v','u','s','p'].indexOf(e.key.toLowerCase()) >= 0){
      e.preventDefault();
      showBanner('Keyboard shortcut blocked during exam');
    }
    // F12 / DevTools
    if(e.key === 'F12'){ e.preventDefault(); }
  });
})();
</script>
<div id="mc-ac-banner" style="display:none;position:fixed;top:0;left:0;width:100%;
  background:#7B1515;color:#fff;text-align:center;padding:10px 16px;
  font-size:14px;font-weight:600;z-index:9999;letter-spacing:.01em;"></div>
"""

def _student_exam():
    # Inject anti-cheat JS on every render (Streamlit rerenders on each interaction)
    import streamlit.components.v1 as _components
    _components.html(_ANTICHEAT_JS, height=0, scrolling=False)

    qs = st.session_state.exam_questions
    cur = st.session_state.exam_current
    total = len(qs)
    q = qs[cur]
    qid = q["id"]
    answers = st.session_state.exam_answers
    show_sol = st.session_state.show_sol

    elapsed = int(time.time() - st.session_state.exam_start)
    tl = st.session_state.time_limit
    remaining = max(0, tl * 60 - elapsed) if tl else None

    # Header
    h1, h2, h3 = st.columns([3, 4, 2])
    with h1:
        mode = st.session_state.get("exam_mode", "competition")
        if mode == "competition":
            comp_name = next(
                (v for g in COMP_GROUPS.values()
                 for k, v in g.items()
                 if k == st.session_state.selected_comp),
                st.session_state.selected_comp or "Exam",
            )
        else:
            grade_key = st.session_state.get("selected_grade", "")
            grade_info = GRADE_LEVELS.get(grade_key, {})
            comp_name = f"{grade_info.get('icon','🎒')} {grade_key}"
        st.markdown(f"**{comp_name}**")
    with h2:
        pct = int((cur + 1) / total * 100)
        st.markdown(
            f'<div style="background:#0D1B2A;border-radius:8px;padding:6px 12px;">'
            f'<div style="font-size:12px;color:rgba(255,255,255,.5);">Question {cur+1} of {total}</div>'
            f'<div style="height:4px;background:rgba(255,255,255,.15);border-radius:2px;margin:4px 0;">'
            f'<div style="width:{pct}%;height:100%;background:#C9A84C;border-radius:2px;"></div></div></div>',
            unsafe_allow_html=True,
        )
    with h3:
        if remaining is not None:
            mm, ss = divmod(remaining, 60)
            color = "#E74C3C" if remaining < 120 else "#F0D98A"
            st.markdown(
                f'<div style="text-align:right;font-family:monospace;font-size:1.3rem;'
                f'color:{color};font-weight:600;">⏱ {mm:02d}:{ss:02d}</div>',
                unsafe_allow_html=True,
            )
            if remaining == 0:
                st.session_state.exam_step = "results"
                st.rerun()
        else:
            mm, ss = divmod(elapsed, 60)
            st.markdown(
                f'<div style="text-align:right;font-family:monospace;'
                f'font-size:1.1rem;color:#888;">⏱ {mm:02d}:{ss:02d}</div>',
                unsafe_allow_html=True,
            )

    # Question navigator dots
    dot_html = '<div style="display:flex;flex-wrap:wrap;gap:5px;margin:0.5rem 0 1rem;">'
    for i, _q in enumerate(qs):
        _qid = _q["id"]
        if i == cur:
            s = "background:#0D1B2A;color:#fff;border-color:#0D1B2A;"
        elif _qid in answers:
            s = "background:#E8F5EE;color:#2D7D4F;border-color:#2D7D4F;"
        else:
            s = "background:#fff;color:#888;border-color:#DDD8CC;"
        dot_html += (f'<div style="width:29px;height:29px;border:1.5px solid;border-radius:6px;'
                     f'display:flex;align-items:center;justify-content:center;'
                     f'font-size:11px;font-weight:600;{s}">{i+1}</div>')
    dot_html += "</div>"
    st.markdown(dot_html, unsafe_allow_html=True)

    # Question card
    diff = q.get("difficulty", "easy")
    diff_icon = {"easy": "🟢", "intermediate": "🟡", "advanced": "🔴"}.get(diff, "")
    st.markdown(
        f'<div class="q-card">'
        f'<div style="display:flex;gap:8px;margin-bottom:.8rem;">'
        f'<span class="badge badge-{"int" if diff=="intermediate" else diff[:3]}">'
        f'{diff_icon} {diff.title()}</span>'
        f'<span class="badge badge-gold">{q.get("topic","Others")}</span></div>',
        unsafe_allow_html=True,
    )

    # Legacy image field
    if q.get("image"):
        try:
            st.image(q["image"], use_column_width=True)
        except Exception:
            pass

    # New figure field (uploaded image data-URI or AI SVG)
    _render_figure(q)

    st.markdown(f"**Q{cur+1}.** {q['body']}")

    already   = answers.get(qid)
    revealed  = (show_sol == "each") and (already is not None)
    qtype     = q.get("question_type", "mcq5")   # mcq5 | mcq4 | fill
    opts      = q.get("options", [])
    letters   = ["A", "B", "C", "D", "E"]

    # ── Multiple-choice (4 or 5 options) ──────────────────────────────────
    if qtype in ("mcq5", "mcq4", ""):
        correct_idx = q.get("correct", 0)

        if revealed:
            for i, opt in enumerate(opts):
                lbl = f"{letters[i]}. {opt}"
                if i == correct_idx:
                    st.success(f"✓ {lbl}  ← Correct answer")
                elif i == already:
                    st.error(f"✗ {lbl}  ← Your answer")
                else:
                    st.markdown(f"&emsp;{lbl}")
            st.markdown("---")
            st.markdown("**📖 Solution**")
            st.markdown(q.get("solution", "No solution available."))
        else:
            radio_opts  = [f"{letters[i]}.  {opt}" for i, opt in enumerate(opts)]
            radio_index = already if already is not None else None
            selected = st.radio(
                "Choose your answer:",
                options=list(range(len(radio_opts))),
                format_func=lambda i: radio_opts[i],
                index=radio_index,
                key=f"radio_{qid}",
                label_visibility="visible",
            )
            if selected != already:
                st.session_state.exam_answers[qid] = selected
                st.rerun()

    # ── Fill-in-the-blank (numeric answer) ────────────────────────────────
    elif qtype == "fill":
        correct_ans = str(q.get("correct_answer", "")).strip()

        if revealed:
            given_ans = str(already).strip() if already is not None else "—"
            is_right  = given_ans == correct_ans
            if is_right:
                st.success(f"✓ Your answer: **{given_ans}** — Correct!")
            else:
                st.error(f"✗ Your answer: **{given_ans}**")
                st.success(f"✓ Correct answer: **{correct_ans}**")
            st.markdown("---")
            st.markdown("**📖 Solution**")
            st.markdown(q.get("solution", "No solution available."))
        else:
            st.markdown(
                '''<div style="background:#FFF8E0;border:.5px solid #E8C84A;
                border-radius:8px;padding:.6rem .9rem;font-size:13px;
                color:#7A5800;margin-bottom:.75rem;">
                ✏️ <strong>Fill-in answer</strong> — type your numerical answer below
                </div>''',
                unsafe_allow_html=True,
            )
            # Use text_input so fractions like "1/3" and integers both work
            fill_val = st.text_input(
                "Your answer:",
                value=str(already) if already is not None else "",
                key=f"fill_{qid}",
                placeholder="e.g.  42   or   1/3   or   3.14",
            )
            if st.button("✅ Confirm Answer", key=f"fill_confirm_{qid}", type="primary"):
                if fill_val.strip():
                    st.session_state.exam_answers[qid] = fill_val.strip()
                    st.rerun()

            if already is not None:
                st.markdown(
                    f'<div style="font-size:12px;color:#2D7D4F;margin-top:.3rem;">'
                    f'✓ Saved: <strong>{already}</strong> — you can change it before submitting.</div>',
                    unsafe_allow_html=True,
                )

    st.markdown('</div>', unsafe_allow_html=True)

    # Navigation
    nc1, nc2, nc3, nc4 = st.columns([1, 1, 2, 2])
    with nc1:
        if st.button("← Prev", disabled=(cur == 0)):
            st.session_state.exam_current -= 1
            st.rerun()
    with nc2:
        if st.button("Skip →"):
            if cur < total - 1:
                st.session_state.exam_current += 1
            st.rerun()
    with nc3:
        if cur < total - 1:
            if st.button("Next →", type="primary", use_container_width=True):
                st.session_state.exam_current += 1
                st.rerun()
    with nc4:
        answered = len(answers)
        if st.button(f"✅ Submit ({answered}/{total} answered)",
                     type="primary", use_container_width=True):
            st.session_state.exam_step = "results"
            st.rerun()


# ── Results ───────────────────────────────────────────────────────────────────
def _student_results():
    qs        = st.session_state.exam_questions
    answers   = st.session_state.exam_answers
    total     = len(qs)
    show_sol  = st.session_state.show_sol
    show_score    = st.session_state.get("comp_show_score", True)
    show_analysis = st.session_state.get("comp_show_analysis", True)

    correct_count = wrong_count = skipped_count = 0
    topic_scores = {t: [0, 0] for t in TOPICS}
    results_for_ai = []

    for q in qs:
        qid   = q["id"]
        qtype = q.get("question_type", "mcq5")
        topic = q.get("topic", "Others")
        if topic not in topic_scores:
            topic_scores[topic] = [0, 0]
        topic_scores[topic][1] += 1
        given = answers.get(qid)

        # Determine correctness based on question type
        if qtype == "fill":
            correct_ans = str(q.get("correct_answer", "")).strip()
            is_correct  = (given is not None) and (str(given).strip() == correct_ans)
        else:
            is_correct = given == q.get("correct", 0)

        if given is None:
            skipped_count += 1
        elif is_correct:
            correct_count += 1
            topic_scores[topic][0] += 1
        else:
            wrong_count += 1
        results_for_ai.append({
            "question_body": q["body"][:80], "topic": topic,
            "difficulty": q.get("difficulty", "easy"),
            "is_correct": is_correct,
        })

    pct = round(correct_count / total * 100, 1) if total else 0
    elapsed = int(time.time() - st.session_state.exam_start)
    mm, ss = divmod(elapsed, 60)
    grade = "Excellent! 🏆" if pct >= 80 else ("Good Work! 👍" if pct >= 60 else "Keep Practising! 💪")
    grade_color = "#2D7D4F" if pct >= 80 else ("#B8860B" if pct >= 60 else "#C0392B")

    # ── Save student record ────────────────────────────────────────────────
    if not st.session_state.get("_record_saved"):
        mode      = st.session_state.get("exam_mode", "competition")
        comp_code = st.session_state.get("selected_comp", "")
        grade_sel = st.session_state.get("selected_grade", "")
        record = {
            "timestamp":    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "first_name":   st.session_state.get("student_first_name", ""),
            "last_name":    st.session_state.get("student_last_name", ""),
            "school":       st.session_state.get("student_school", ""),
            "class_pin":    st.session_state.get("student_class_pin", ""),
            "student_uid":   _student_info().get("uid", ""),
            "student_email": _student_info().get("email", ""),
            "mode":         mode,
            "competition":  comp_code if mode == "competition" else grade_sel,
            "difficulty":   st.session_state.get("selected_diff", "mixed"),
            "total_q":      total,
            "correct":      correct_count,
            "wrong":        wrong_count,
            "skipped":      skipped_count,
            "score_pct":    pct,
            "time_sec":     elapsed,
            "topic_scores": {t: {"correct": v[0], "total": v[1]}
                             for t, v in topic_scores.items()},
            "answers": {
                str(qid): {
                    "given":   str(answers.get(qid, "")),
                    "correct": str(q.get("correct_answer") if q.get("question_type") == "fill"
                                   else q.get("correct", "")),
                    "is_correct": results_for_ai[i]["is_correct"],
                    "topic":   q.get("topic", "Others"),
                    "difficulty": q.get("difficulty", "easy"),
                }
                for i, q in enumerate(qs)
                for qid in [q["id"]]
            },
        }
        _save_record(record)
        st.session_state["_record_saved"] = True

    # Score banner — gated by competition settings
    if show_score:
        st.markdown(
            f'<div style="background:linear-gradient(135deg,#0D1B2A 60%,#1A2F47);'
            f'border-radius:16px;padding:2rem;text-align:center;margin-bottom:1.5rem;">'
            f'<div style="font-family:Playfair Display,serif;font-size:1.5rem;'
            f'font-weight:700;color:{grade_color};margin-bottom:.3rem;">{grade}</div>'
            f'<div style="font-family:monospace;font-size:3rem;font-weight:700;'
            f'color:#F0D98A;line-height:1;">{pct}%</div>'
            f'<div style="color:rgba(255,255,255,.6);margin-top:.5rem;">'
            f'{correct_count}/{total} correct · Time: {mm:02d}:{ss:02d}</div></div>',
            unsafe_allow_html=True,
        )
        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("✅ Correct", correct_count)
        mc2.metric("❌ Wrong", wrong_count)
        mc3.metric("⏭ Skipped", skipped_count)
        mc4.metric("⏱ Time", f"{mm}m {ss}s")
    else:
        st.markdown(
            f'''<div style="background:linear-gradient(135deg,#0D1B2A,#1A2F47);
            border-radius:16px;padding:2rem;text-align:center;margin-bottom:1.5rem;">
            <div style="font-size:1.3rem;font-weight:600;color:#F0D98A;">
            ✓ Exam Submitted Successfully</div>
            <div style="color:rgba(255,255,255,.5);margin-top:.5rem;font-size:.9rem;">
            Time: {mm:02d}:{ss:02d} · Results will be shared by your teacher.</div>
            </div>''',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # Radar + AI analysis — gated
    if not show_score:
        st.info("📋 Your answers have been recorded. Your teacher will share results with you.")
        st.markdown("---")
        if st.button("🔄 Start New Exam", type="primary"):
            for k in ["exam_step","setup_step","exam_mode","selected_comp","selected_grade",
                      "selected_diff","exam_questions","exam_answers","exam_current",
                      "exam_submitted","exam_start","_record_saved","student_first_name",
                      "student_last_name","student_school","comp_show_score",
                      "comp_show_solution","comp_show_analysis"]:
                st.session_state.pop(k, None)
            st.rerun()
        return  # stop rendering if score is hidden

    ra_col, ai_col = st.columns(2)
    with ra_col:
        st.markdown("### 📊 Topic Performance")
        radar_data = {t: (topic_scores[t][0], topic_scores[t][1]) for t in TOPICS}
        render_radar(radar_data)
        for t in TOPICS:
            c, tot = topic_scores[t]
            if tot > 0:
                bar_pct = int(c / tot * 100)
                bar_color = "#2D7D4F" if bar_pct >= 70 else "#B8860B" if bar_pct >= 40 else "#C0392B"
                st.markdown(
                    f'<div style="margin-bottom:6px;">'
                    f'<div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:2px;">'
                    f'<span>{t}</span>'
                    f'<span style="color:{bar_color};font-weight:600;">{c}/{tot} ({bar_pct}%)</span></div>'
                    f'<div style="background:#EDE8DC;border-radius:4px;height:6px;">'
                    f'<div style="background:{bar_color};width:{bar_pct}%;height:100%;border-radius:4px;"></div>'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )

    with ai_col:
        st.markdown("### 🤖 AI Performance Analysis")
        if not show_analysis:
            st.info("🔒 AI analysis is not available for this competition.")
        elif _get_api_key():
            with st.spinner("Generating personalised analysis…"):
                analysis = ai_analyse_performance(results_for_ai)
            st.markdown(analysis)
        else:
            best = max(topic_scores, key=lambda t: topic_scores[t][0] / max(topic_scores[t][1], 1))
            worst = min(
                (t for t in topic_scores if topic_scores[t][1] > 0),
                key=lambda t: topic_scores[t][0] / max(topic_scores[t][1], 1),
                default="N/A",
            )
            st.info("💡 Add your Anthropic API key in **Admin → Settings** to unlock AI analysis.")
            st.markdown(f"""
**Overall:** You scored **{pct}%** ({correct_count}/{total}).

**Strongest area:** {best}

**Needs work:** {worst} — review core concepts and practise more problems in this area.
            """)

    st.markdown("---")
    st.markdown("### 📝 Question Review")
    letters = ["A", "B", "C", "D", "E"]
    for idx, q in enumerate(qs):
        qid   = q["id"]
        qtype = q.get("question_type", "mcq5")
        given = answers.get(qid)

        # Determine correctness
        if qtype == "fill":
            correct_ans = str(q.get("correct_answer", "")).strip()
            is_correct  = (given is not None) and (str(given).strip() == correct_ans)
        else:
            correct_idx = q.get("correct", 0)
            is_correct  = given == correct_idx

        is_skipped = given is None
        icon      = "✅" if is_correct else ("⏭" if is_skipped else "❌")
        diff_icon = {"easy":"🟢","intermediate":"🟡","advanced":"🔴"}.get(q.get("difficulty",""),"")
        type_tag  = " · ✏️ Fill-in" if qtype == "fill" else ""

        with st.expander(
            f"{icon} Q{idx+1}  ·  {diff_icon} {q.get('difficulty','').title()}  ·  {q.get('topic','Others')}{type_tag}"
        ):
            _render_figure(q)
            st.markdown(q["body"])

            if qtype == "fill":
                given_str = str(given).strip() if given is not None else "—"
                if is_correct:
                    st.success(f"✓ Your answer: **{given_str}** — Correct!")
                elif is_skipped:
                    st.warning(f"⏭ Skipped — Correct answer: **{correct_ans}**")
                else:
                    st.error(f"✗ Your answer: **{given_str}**")
                    st.success(f"✓ Correct answer: **{correct_ans}**")
            else:
                for i, opt in enumerate(q.get("options", [])):
                    lbl = f"{letters[i]}. {opt}"
                    if i == correct_idx:
                        st.success(f"✓ {lbl}  ← Correct answer")
                    elif i == given:
                        st.error(f"✗ {lbl}  ← Your answer")
                    else:
                        st.markdown(f"&emsp;{lbl}")

            if show_sol != "never":
                st.markdown("---")
                st.markdown("**📖 Solution**")
                st.markdown(q.get("solution", "No solution provided."))

    st.markdown("---")

    # ── Certificate download ───────────────────────────────────────────────
    if show_score:
        cert_record = {
            "first_name":  st.session_state.get("student_first_name", ""),
            "last_name":   st.session_state.get("student_last_name", ""),
            "competition": st.session_state.get("selected_comp") or st.session_state.get("selected_grade",""),
            "score_pct":   pct,
            "correct":     correct_count,
            "total_q":     total,
            "time_sec":    elapsed,
            "timestamp":   time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        }
        _show_certificate_button(cert_record)
        st.markdown("---")

    if st.button("🔄 Start New Exam", type="primary"):
        for k in ["exam_step", "setup_step", "exam_mode",
                  "selected_comp", "selected_grade", "selected_diff",
                  "exam_questions", "exam_answers", "exam_current",
                  "exam_submitted", "exam_start", "_record_saved",
                  "student_first_name", "student_last_name", "student_school",
                  "comp_show_score", "comp_show_solution", "comp_show_analysis"]:
            st.session_state.pop(k, None)
        st.rerun()




# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6B — CERTIFICATE GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def _generate_certificate_pdf(
    student_name: str,
    competition: str,
    score_pct: int,
    correct: int,
    total: int,
    time_str: str,
    timestamp: str,
    cert_id: str,
) -> bytes:
    """
    Generate a PDF certificate using reportlab.
    Returns bytes of the PDF file.
    """
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER
        import io as _io

        buf = _io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=landscape(A4),
            topMargin=1.5*cm, bottomMargin=1.5*cm,
            leftMargin=2*cm, rightMargin=2*cm,
        )

        W, H = landscape(A4)
        navy   = colors.HexColor("#0D1B2A")
        gold   = colors.HexColor("#C9A84C")
        gold2  = colors.HexColor("#F0D98A")
        cream  = colors.HexColor("#FAF8F3")
        gray   = colors.HexColor("#888888")

        styles = getSampleStyleSheet()
        def sty(name, **kw):
            s = ParagraphStyle(name, parent=styles["Normal"], **kw)
            return s

        header_sty  = sty("hdr",  fontName="Helvetica-Bold",   fontSize=11,
                          textColor=gray, alignment=TA_CENTER, spaceAfter=4)
        title_sty   = sty("ttl",  fontName="Helvetica-Bold",   fontSize=36,
                          textColor=gold,  alignment=TA_CENTER, spaceAfter=6)
        sub_sty     = sty("sub",  fontName="Helvetica",         fontSize=13,
                          textColor=navy,  alignment=TA_CENTER, spaceAfter=4)
        name_sty    = sty("nm",   fontName="Helvetica-Bold",   fontSize=28,
                          textColor=navy,  alignment=TA_CENTER, spaceAfter=6)
        body_sty    = sty("bd",   fontName="Helvetica",         fontSize=12,
                          textColor=navy,  alignment=TA_CENTER, spaceAfter=4)
        score_sty   = sty("sc",   fontName="Helvetica-Bold",   fontSize=22,
                          textColor=gold,  alignment=TA_CENTER, spaceAfter=4)
        small_sty   = sty("sm",   fontName="Helvetica",         fontSize=9,
                          textColor=gray,  alignment=TA_CENTER, spaceAfter=2)

        grade = (
            "Distinction ✦" if score_pct >= 80 else
            "Merit"          if score_pct >= 60 else
            "Participation"
        )

        story = [
            Paragraph("MATH MISSION THAILAND", header_sty),
            HRFlowable(width="80%", thickness=1, color=gold, spaceAfter=8),
            Paragraph("Certificate of Achievement", title_sty),
            Spacer(1, 0.3*cm),
            Paragraph("This is to certify that", sub_sty),
            Spacer(1, 0.2*cm),
            Paragraph(student_name, name_sty),
            HRFlowable(width="50%", thickness=0.5, color=gold, spaceAfter=8),
            Paragraph(f"has successfully completed the", body_sty),
            Paragraph(f"<b>{competition}</b>", sty("comp", fontName="Helvetica-Bold",
                      fontSize=16, textColor=navy, alignment=TA_CENTER, spaceAfter=4)),
            Spacer(1, 0.3*cm),
            Paragraph(f"with a score of", body_sty),
            Paragraph(f"{score_pct}% &nbsp;·&nbsp; {correct}/{total} correct &nbsp;·&nbsp; Time: {time_str}", score_sty),
            Paragraph(f"Grade: <b>{grade}</b>", body_sty),
            Spacer(1, 0.5*cm),
            HRFlowable(width="80%", thickness=1, color=gold, spaceAfter=8),
            Paragraph(f"Date: {timestamp[:10]}    |    Certificate ID: {cert_id}", small_sty),
            Paragraph("Verify at: mathcomp.streamlit.app/?verify=" + cert_id, small_sty),
        ]

        doc.build(story)
        return buf.getvalue()

    except ImportError:
        # reportlab not installed — return a minimal placeholder
        return b"%PDF placeholder - install reportlab"
    except Exception as e:
        return f"%PDF error: {e}".encode()


def _cert_id(record: dict) -> str:
    """Deterministic short certificate ID from record fields."""
    import hashlib
    raw = f"{record.get('first_name','')}{record.get('last_name','')}{record.get('timestamp','')}{record.get('score_pct','')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12].upper()


def _show_certificate_button(record: dict):
    """Render download button for certificate if score qualifies (≥ 50%)."""
    pct = record.get("score_pct", 0)
    if pct < 50:
        st.info("📋 Certificate available when score ≥ 50%")
        return

    student_name = f"{record.get('first_name','')} {record.get('last_name','')}".strip()
    competition  = record.get("competition", "Math Competition")
    correct      = record.get("correct", 0)
    total        = record.get("total_q", 0)
    elapsed      = record.get("time_sec", 0)
    mm, ss       = divmod(int(elapsed), 60)
    time_str     = f"{mm:02d}:{ss:02d}"
    timestamp    = record.get("timestamp", "")
    cert_id      = _cert_id(record)

    pdf_bytes = _generate_certificate_pdf(
        student_name=student_name,
        competition=competition,
        score_pct=pct,
        correct=correct,
        total=total,
        time_str=time_str,
        timestamp=timestamp,
        cert_id=cert_id,
    )

    grade = "Distinction ✦" if pct >= 80 else "Merit" if pct >= 60 else "Participation"
    grade_color = "#C9A84C" if pct >= 80 else "#2D7D4F" if pct >= 60 else "#888"

    st.markdown(
        f'<div style="background:linear-gradient(135deg,#0D1B2A,#1A2F47);'
        f'border-radius:12px;padding:1.2rem 1.5rem;margin:1rem 0;'
        f'display:flex;align-items:center;justify-content:space-between;">'
        f'<div>'
        f'<div style="color:#F0D98A;font-weight:700;font-size:1rem;">🏅 Certificate Ready</div>'
        f'<div style="color:rgba(255,255,255,.6);font-size:.85rem;margin-top:2px;">'
        f'Grade: <span style="color:{grade_color};font-weight:600;">{grade}</span>'
        f'  ·  ID: <code style="color:rgba(255,255,255,.5);">{cert_id}</code></div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )
    st.download_button(
        label="⬇️ Download Certificate (PDF)",
        data=pdf_bytes,
        file_name=f"MathComp_Certificate_{cert_id}.pdf",
        mime="application/pdf",
        use_container_width=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6C — STUDENT PROGRESS DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

def _load_student_records(uid: str = "", email: str = "", display_name: str = "") -> list:
    """Load records for one student — matched by UID, email, or display name."""
    all_records = _load_records()
    if not uid and not email and not display_name:
        return []

    # Split display_name into first/last for legacy record matching
    first, last = "", ""
    if display_name:
        parts = display_name.strip().split(" ", 1)
        first = parts[0].lower()
        last  = parts[1].lower() if len(parts) > 1 else ""

    result = []
    for r in all_records:
        # Match by UID (most reliable)
        if uid and r.get("student_uid") == uid:
            result.append(r)
            continue
        # Match by email
        if email and r.get("student_email", "").lower() == email.lower():
            result.append(r)
            continue
        # Match by name (for records created before auth was added)
        if first and last:
            r_first = r.get("first_name", "").lower()
            r_last  = r.get("last_name", "").lower()
            if r_first == first and r_last == last:
                result.append(r)
                continue
    return result


def _page_student_progress():
    """Student's personal progress dashboard — shown when they click 'My History'."""
    info = _student_info()
    if not info.get("uid"):
        st.info("กรุณาเข้าสู่ระบบก่อนเพื่อดูประวัติการสอบ")
        return

    st.markdown(
        f'<div style="background:linear-gradient(135deg,#0D1B2A,#1A2F47);'
        f'border-radius:14px;padding:1.5rem 2rem;margin-bottom:1.5rem;">'
        f'<h2 style="color:#F0D98A;margin:0;font-family:Playfair Display,serif;">📈 ประวัติการสอบ</h2>'
        f'<p style="color:rgba(255,255,255,.55);margin:.3rem 0 0;font-size:.9rem;">'
        f'{info.get("display_name","") or info.get("email","")}</p></div>',
        unsafe_allow_html=True,
    )

    records = _load_student_records(
        uid=info.get("uid", ""),
        email=info.get("email", ""),
        display_name=info.get("display_name", ""),
    )

    if not records:
        st.info("ยังไม่มีประวัติการสอบ — ไปทำข้อสอบก่อนแล้วกลับมาดูที่นี่")
        if st.button("🎓 ไปหน้าข้อสอบ"):
            st.session_state["show_progress"] = False
            st.rerun()
        return

    # ── Summary metrics ────────────────────────────────────────────────────
    scores   = [r.get("score_pct", 0) for r in records]
    avg_sc   = round(sum(scores) / len(scores), 1)
    best_sc  = max(scores)
    total_ex = len(records)

    m1, m2, m3 = st.columns(3)
    m1.metric("📝 ข้อสอบทั้งหมด", total_ex)
    m2.metric("📊 คะแนนเฉลี่ย", f"{avg_sc}%")
    m3.metric("🏆 คะแนนสูงสุด", f"{best_sc}%")

    st.markdown("---")

    # ── Score trend chart ──────────────────────────────────────────────────
    if len(records) >= 2:
        try:
            import plotly.graph_objects as go
            dates  = [r.get("timestamp","")[:10] for r in reversed(records)]
            scrs   = [r.get("score_pct", 0) for r in reversed(records)]
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=dates, y=scrs, mode="lines+markers",
                line=dict(color="#C9A84C", width=2),
                marker=dict(size=8, color="#F0D98A"),
                name="Score %",
            ))
            fig.update_layout(
                title="แนวโน้มคะแนน", height=260,
                xaxis_title="วันที่", yaxis_title="Score %",
                yaxis=dict(range=[0, 105]),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#0D1B2A"),
                margin=dict(l=30, r=30, t=40, b=30),
            )
            st.plotly_chart(fig, use_container_width=True)
        except Exception:
            pass

    # ── Exam history list ──────────────────────────────────────────────────
    st.markdown("### 📋 รายการการสอบ")
    for r in records:
        pct  = r.get("score_pct", 0)
        comp = r.get("competition", "—")
        ts   = r.get("timestamp", "")[:16]
        grade_col = "#C9A84C" if pct >= 80 else "#2D7D4F" if pct >= 60 else "#C0392B" if pct >= 50 else "#888"

        with st.expander(f"**{comp}** — {pct}% — {ts}"):
            c1, c2, c3 = st.columns(3)
            c1.metric("คะแนน", f"{pct}%")
            c2.metric("ถูก/ทั้งหมด", f"{r.get('correct',0)}/{r.get('total_q',0)}")
            elapsed = r.get("time_sec", 0)
            mm2, ss2 = divmod(int(elapsed), 60)
            c3.metric("เวลา", f"{mm2:02d}:{ss2:02d}")

            # Topic scores
            ts_dict = r.get("topic_scores", {})
            if ts_dict:
                st.markdown("**ผลตามหัวข้อ:**")
                for topic, val in ts_dict.items():
                    if isinstance(val, dict):
                        c_cnt = val.get("correct", 0)
                        t_cnt = val.get("total", 0)
                    else:
                        continue
                    if t_cnt > 0:
                        bar_pct = int(c_cnt / t_cnt * 100)
                        bar_col = "#2D7D4F" if bar_pct >= 70 else "#B8860B" if bar_pct >= 40 else "#C0392B"
                        st.markdown(
                            f'<div style="margin-bottom:5px;">'
                            f'<div style="display:flex;justify-content:space-between;font-size:12px;">'
                            f'<span>{topic}</span>'
                            f'<span style="color:{bar_col};font-weight:600;">{c_cnt}/{t_cnt}</span></div>'
                            f'<div style="background:#EDE8DC;border-radius:3px;height:5px;">'
                            f'<div style="background:{bar_col};width:{bar_pct}%;height:100%;border-radius:3px;"></div>'
                            f'</div></div>',
                            unsafe_allow_html=True,
                        )

            st.markdown("---")
            _show_certificate_button(r)

    st.markdown("---")
    if st.button("🎓 กลับหน้าข้อสอบ", type="primary"):
        st.session_state["show_progress"] = False
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — ADMIN PAGE
# ══════════════════════════════════════════════════════════════════════════════
def page_admin():
    # ── Auth gate ──────────────────────────────────────────────────────────
    if not _admin_login_page():
        return   # login form shown; stop here

    # ── Logged-in header ───────────────────────────────────────────────────
    hc1, hc2 = st.columns([5, 1])
    with hc1:
        st.markdown(
            '<div style="background:linear-gradient(135deg,#0D1B2A,#1A2F47);'
            'border-radius:14px;padding:1.5rem 2rem;margin-bottom:1.5rem;">'
            '<h2 style="color:#F0D98A;margin:0;font-family:Playfair Display,serif;">⚙️ Admin Portal</h2>'
            '<p style="color:rgba(255,255,255,.55);margin:.3rem 0 0;font-size:.9rem;">'
            f'Signed in as {_get_admin_email() or "admin"}</p></div>',
            unsafe_allow_html=True,
        )
    with hc2:
        st.markdown("<div style='margin-top:1.1rem;'>", unsafe_allow_html=True)
        if st.button("🔓 Sign Out", use_container_width=True):
            st.session_state["admin_logged_in"] = False
            st.session_state.pop("admin_email", None)
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    tabs = st.tabs(["📚 Question Bank", "➕ Add Question",
                    "📤 Upload & AI", "📄 PDF Batch Import",
                    "🏆 Competitions", "👥 Student Records",
                    "📊 Statistics", "⚙️ Settings"])
    with tabs[0]: _admin_qbank()
    with tabs[1]: _admin_add()
    with tabs[2]: _admin_upload()
    with tabs[3]: _admin_pdf_import()
    with tabs[4]: _admin_competitions()
    with tabs[5]: _admin_records()
    with tabs[6]: _admin_stats()
    with tabs[7]: _admin_settings()


def _diff_badge_text(d):
    return {"easy": "🟢 Easy", "intermediate": "🟡 Intermediate", "advanced": "🔴 Advanced"}.get(d, d)


def _admin_qbank():
    st.markdown("#### 📚 Question Bank")

    # ── Per-competition file overview ──────────────────────────────────────
    comp_files = list_comp_files()
    if comp_files:
        file_stats = []
        for c in comp_files:
            qs_c = _load_comp_db(c)
            comp_name = get_all_competitions().get(c, c)
            file_stats.append(f"**{c}** ({len(qs_c)}q)")
        st.markdown(
            f'<div style="background:#FAF7F0;border:.5px solid #DDD8CC;border-radius:8px;'
            f'padding:.5rem .9rem;font-size:12px;color:#555;margin-bottom:.75rem;">'
            f'📁 Database files: {" · ".join(file_stats)}'
            f'</div>',
            unsafe_allow_html=True,
        )

    all_qs = db_get_all()

    fc1, fc2, fc3 = st.columns([3, 2, 2])
    with fc1:
        search = st.text_input("🔍 Search questions", placeholder="Type to search…",
                               label_visibility="collapsed")
    with fc2:
        comp_f = st.selectbox("Competition",
                              ["All"] + list(get_all_competitions().keys()),
                              format_func=lambda k: "All Competitions" if k == "All" else get_all_competitions().get(k, k),
                              label_visibility="collapsed")
    with fc3:
        diff_f = st.selectbox("Difficulty",
                              ["All", "easy", "intermediate", "advanced"],
                              format_func=lambda k: "All Difficulties" if k == "All" else _diff_badge_text(k),
                              label_visibility="collapsed")

    qs = all_qs
    if comp_f != "All":
        qs = [q for q in qs if q["comp"] == comp_f]
    if diff_f != "All":
        qs = [q for q in qs if q["difficulty"] == diff_f]
    if search:
        qs = [q for q in qs if search.lower() in q["body"].lower()]

    st.markdown(f'<div style="font-size:13px;color:#888;margin-bottom:.5rem;">Showing <strong>{len(qs)}</strong> of {len(all_qs)} questions</div>', unsafe_allow_html=True)

    if not qs:
        st.info("No questions match your filters.")
        return

    letters = ["A", "B", "C", "D", "E"]
    for q in qs:
        qid = q["id"]
        cname = COMPETITIONS.get(q["comp"], q["comp"])
        diff_icon = {"easy": "🟢", "intermediate": "🟡", "advanced": "🔴"}.get(q["difficulty"], "")
        preview = q["body"][:70] + "…" if len(q["body"]) > 70 else q["body"]
        qt_icon = {"mcq5": "📋5", "mcq4": "📋4", "fill": "✏️"}.get(q.get("question_type","mcq5"), "📋5")
        with st.expander(f"#{qid} · {cname} · {diff_icon} {q['difficulty'].title()} · {q.get('topic','Others')} · {qt_icon} — {preview}"):
            _render_figure(q)
            st.markdown(q["body"])
            if q.get("question_type") == "fill":
                st.info(f"✏️ Fill-in question — Correct answer: **{q.get('correct_answer','?')}**")
            else:
                st.markdown("**Options:**")
                for i, opt in enumerate(q.get("options", [])):
                    mark = "✅" if i == q.get("correct", 0) else "  "
                    st.markdown(f"{mark} **{letters[i]}.** {opt}")
            st.markdown("---")
            st.markdown("**📖 Solution:**")
            st.markdown(q.get("solution", "No solution."))
            st.markdown("---")
            st.markdown("**✏️ Edit**")
            with st.form(key=f"edit_{q.get('comp','x')}_{qid}"):
                new_body = st.text_area("Question text", q["body"], height=80)
                ec1, ec2 = st.columns(2)
                with ec1:
                    new_diff = st.selectbox("Difficulty", ["easy", "intermediate", "advanced"],
                                            index=["easy","intermediate","advanced"].index(q["difficulty"]),
                                            format_func=_diff_badge_text)
                with ec2:
                    new_topic = st.selectbox("Topic", TOPICS,
                                             index=TOPICS.index(q.get("topic","Others"))
                                             if q.get("topic") in TOPICS else 0)
                # Fill-in answer edit
                if q.get("question_type") == "fill":
                    new_correct_ans = st.text_input(
                        "Correct answer (fill-in)",
                        value=str(q.get("correct_answer", "")),
                    )
                else:
                    new_correct_ans = q.get("correct_answer", "")
                new_sol = st.text_area("Solution", q.get("solution", ""), height=120)

                # Figure management inside edit form
                st.markdown("**🖼️ Figure**")
                if q.get("figure"):
                    st.markdown("Current figure attached. Upload new one to replace, or remove below.")
                    remove_fig = st.checkbox("🗑️ Remove figure", key=f"rm_fig_{q.get('comp','x')}_{qid}")
                else:
                    remove_fig = False
                    st.markdown("No figure. Upload one below (optional).")
                new_fig_file = st.file_uploader(
                    "Upload / replace figure",
                    type=["png","jpg","jpeg","gif","svg"],
                    key=f"fig_upload_{q.get('comp','x')}_{qid}",
                )

                ea, eb = st.columns(2)
                with ea:
                    saved = st.form_submit_button("💾 Save", type="primary")
                with eb:
                    deleted = st.form_submit_button("🗑️ Delete")
            if saved:
                upd = {
                    "body": new_body, "difficulty": new_diff,
                    "topic": new_topic, "solution": new_sol,
                    "correct_answer": new_correct_ans,
                }
                if remove_fig:
                    upd["figure"] = None
                elif new_fig_file:
                    fig_b = new_fig_file.read()
                    upd["figure"] = _img_to_b64(fig_b, new_fig_file.type or "image/png")
                db_update(qid, upd, comp_code=q.get("comp",""))
                st.success("✓ Updated!")
                st.rerun()
            if deleted:
                db_delete(qid, comp_code=q.get("comp",""))
                st.warning("Deleted.")
                st.rerun()


def _admin_add():
    st.markdown("#### ➕ Add Question Manually")

    # ── Database file info ─────────────────────────────────────────────────
    comp_files = list_comp_files()
    all_comps_available = get_all_competitions()
    st.markdown("**📁 Competition Database Files**")
    st.markdown(
        f'<div style="background:#E8F5EE;border:.5px solid #9FE1CB;border-radius:8px;'
        f'padding:.5rem .9rem;font-size:12px;color:#0F6E56;margin-bottom:.75rem;">'
        f'✓ {len(comp_files)} competition database files active: '
        f'{", ".join(comp_files[:8])}{"..." if len(comp_files)>8 else ""}'
        f'<br>Questions are saved to <strong>data/competitions/[COMP_CODE].json</strong>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Question type selector (outside form so it drives the UI) ──────────
    qtype = st.radio(
        "**Question Type**",
        ["📋 Multiple Choice — 5 options (A–E)",
         "📋 Multiple Choice — 4 options (A–D)",
         "✏️ Fill-in-the-blank (numeric answer)"],
        horizontal=True,
        label_visibility="visible",
    )
    qtype_code = "mcq5" if "5 options" in qtype else ("mcq4" if "4 options" in qtype else "fill")
    num_opts   = 5 if qtype_code == "mcq5" else (4 if qtype_code == "mcq4" else 0)

    if qtype_code == "fill":
        st.info("✏️ **Fill-in:** Students type their numeric answer directly. "
                "You set the exact correct answer (e.g. 42, 1/3, 3.14). "
                "Used for AIME (0–999), สอวน., and other free-response competitions.")
    st.markdown("---")

    with st.form("add_form"):
        ac1, ac2, ac3 = st.columns(3)
        with ac1:
            comp = st.selectbox("Competition", list(get_all_competitions().keys()),
                                format_func=lambda k: get_all_competitions().get(k, k))
        with ac2:
            year = st.number_input("Year", 1990, 2030, 2024)
        with ac3:
            topic = st.selectbox("Topic", TOPICS)

        body = st.text_area("Question Text", height=100,
                            placeholder="Enter question. Use $...$ for math.")

        letters = ["A", "B", "C", "D", "E"]

        # ── MCQ options ────────────────────────────────────────────────────
        if qtype_code in ("mcq5", "mcq4"):
            st.markdown(f"**Answer Options ({num_opts} choices) — select the correct one**")
            opts = []
            opt_cols = st.columns(num_opts)
            for i in range(num_opts):
                with opt_cols[i]:
                    opts.append(st.text_input(f"Option {letters[i]}", key=f"add_opt_{i}"))
            correct_input = st.radio(
                "Correct answer", list(range(num_opts)),
                format_func=lambda i: letters[i], horizontal=True,
            )
            correct_answer_fill = ""   # unused for MCQ

        # ── Fill-in answer ─────────────────────────────────────────────────
        else:
            opts = []
            correct_input = 0          # unused for fill
            st.markdown("**Correct Answer** (exact value student must type)")
            correct_answer_fill = st.text_input(
                "Correct answer", placeholder="e.g.  42  or  1/3  or  3.14",
                help="Must match exactly what student types — case-insensitive for text, exact for numbers."
            )
            st.markdown("*Tip: For AIME answers are integers 0–999. "
                        "For สอวน. answers may be integers or simple fractions.*")

        diff     = st.select_slider("Difficulty", ["easy", "intermediate", "advanced"],
                                    value="intermediate", format_func=_diff_badge_text)
        solution = st.text_area("Detailed Solution", height=150)

        b1, b2 = st.columns(2)
        with b1:
            ai_gen  = st.form_submit_button("🤖 Generate Solution with AI")
        with b2:
            save_btn = st.form_submit_button("💾 Save Question", type="primary")

    # ── Figure section (outside form — uses session state) ─────────────────
    st.markdown("---")
    st.markdown("**🖼️ Figure / Diagram** *(optional)*")

    fig_tab1, fig_tab2 = st.tabs(["📁 Upload Image", "🤖 AI Auto-Draw"])

    with fig_tab1:
        fig_col1, fig_col2 = st.columns([1, 1])
        with fig_col1:
            uploaded_fig = st.file_uploader(
                "Upload figure (PNG, JPG, SVG)",
                type=["png", "jpg", "jpeg", "gif", "svg"],
                key="add_fig_upload",
                help="Max 5MB. The image will be embedded in the question.",
            )
            if uploaded_fig:
                fig_bytes = uploaded_fig.read()
                mime_type = uploaded_fig.type or "image/png"
                st.session_state["_add_figure"] = _img_to_b64(fig_bytes, mime_type)
                st.success("✓ Figure uploaded and ready.")
        with fig_col2:
            if st.session_state.get("_add_figure", "").startswith("data:image"):
                st.markdown("**Preview:**")
                st.markdown(
                    f'''<img src="{st.session_state["_add_figure"]}"
                    style="max-width:100%;max-height:220px;border:1px solid #DDD8CC;border-radius:8px;"/>''',
                    unsafe_allow_html=True,
                )
                if st.button("🗑️ Remove figure", key="remove_fig_upload"):
                    st.session_state.pop("_add_figure", None)
                    st.rerun()

    with fig_tab2:
        st.markdown("Describe the figure and AI will draw it as an SVG diagram.")
        st.markdown(
            '''<div style="background:#FFF8E0;border:.5px solid #E8C84A;border-radius:8px;
            padding:.5rem .8rem;font-size:12px;color:#7A5800;margin-bottom:.5rem;">
            💡 <strong>Tips:</strong> "Right triangle with legs 3 and 4, hypotenuse 5, labelled" ·
            "Circle with centre O, radius 5, chord AB" ·
            "Rectangle ABCD with diagonal AC" ·
            "Number line from -3 to 3 with point at 1.5" ·
            "Coordinate axes with parabola y=x²"
            </div>''',
            unsafe_allow_html=True,
        )
        fig_desc = st.text_area(
            "Figure description",
            placeholder="e.g. Triangle ABC with angle A = 90°, AB = 6 cm, AC = 8 cm. Label all sides.",
            height=80,
            key="fig_desc_input",
        )
        ai_fig_col1, ai_fig_col2 = st.columns([1, 2])
        with ai_fig_col1:
            if st.button("🎨 Generate Figure with AI", type="primary",
                         key="gen_fig_btn", use_container_width=True):
                if not fig_desc.strip():
                    st.warning("Enter a figure description first.")
                elif not _get_api_key():
                    st.error("⚠️ Set your Anthropic API key in Settings first.")
                else:
                    with st.spinner("Drawing figure…"):
                        svg = ai_generate_figure(fig_desc)
                    if svg and svg.startswith("<svg"):
                        st.session_state["_add_figure"] = svg
                        st.session_state["_add_figure_svg"] = True
                        st.success("✓ Figure generated!")
                        st.rerun()
                    else:
                        st.error("Could not generate figure. Try a more specific description.")
        with ai_fig_col2:
            if (st.session_state.get("_add_figure", "").strip().startswith("<svg")
                    and st.session_state.get("_add_figure_svg")):
                st.markdown("**Preview:**")
                st.markdown(
                    f'''<div style="background:#FAFAFA;border:1px solid #DDD8CC;
                    border-radius:8px;padding:.5rem;text-align:center;">
                    {st.session_state["_add_figure"]}
                    </div>''',
                    unsafe_allow_html=True,
                )
                if st.button("🔄 Regenerate", key="regen_fig"):
                    st.session_state.pop("_add_figure", None)
                    st.session_state.pop("_add_figure_svg", None)
                    st.rerun()

    st.markdown("---")

    # ── AI generation ──────────────────────────────────────────────────────
    if ai_gen:
        if not body.strip():
            st.warning("Enter the question text first.")
        else:
            with st.spinner("AI is analysing…"):
                result = ai_generate_solution(
                    body,
                    [o for o in opts if o] if qtype_code != "fill" else [],
                    comp,
                )
            st.session_state["_ai_result"] = result
            if qtype_code != "fill":
                ci = result.get("correct_index", 0)
                ci_label = letters[ci] if ci < num_opts else "?"
                st.success(f"✓ AI: **{result['difficulty'].title()}** · "
                           f"**{result['topic']}** · Correct: {ci_label}")
            else:
                st.success(f"✓ AI: **{result['difficulty'].title()}** · **{result['topic']}**")
            st.markdown("**AI Solution:**")
            st.markdown(result.get("solution", ""))

    # ── Save ───────────────────────────────────────────────────────────────
    if save_btn:
        if not body.strip():
            st.error("Question text is required.")
        elif qtype_code != "fill" and len([o for o in opts if o.strip()]) < 2:
            st.error("Provide at least 2 options.")
        elif qtype_code == "fill" and not correct_answer_fill.strip():
            st.error("Provide the correct answer for fill-in questions.")
        else:
            ai_r      = st.session_state.pop("_ai_result", {})
            _ai_diff  = ai_r.get("difficulty", diff)
            _ai_diff  = _ai_diff if _ai_diff in ["easy","intermediate","advanced"] else diff
            _ai_topic = ai_r.get("topic", topic)
            _ai_topic = _ai_topic if _ai_topic in TOPICS else topic
            _ai_sol   = ai_r.get("solution", solution) or solution
            if _ai_sol.startswith("⚠️"):
                _ai_sol = solution

            # Grab any figure stored in session state
            _figure = st.session_state.pop("_add_figure", None)
            st.session_state.pop("_add_figure_svg", None)

            if qtype_code == "fill":
                q_data = {
                    "comp": comp, "year": int(year), "body": body,
                    "question_type": "fill",
                    "options": [],
                    "correct": 0,
                    "correct_answer": correct_answer_fill.strip(),
                    "difficulty": _ai_diff,
                    "topic": _ai_topic,
                    "solution": _ai_sol,
                    "figure": _figure,
                    "image": None,
                }
            else:
                clean_opts = [o for o in opts if o.strip()]
                try:
                    _ai_correct = int(ai_r.get("correct_index", correct_input))
                    if _ai_correct < 0 or _ai_correct >= len(clean_opts):
                        _ai_correct = correct_input
                except Exception:
                    _ai_correct = correct_input
                q_data = {
                    "comp": comp, "year": int(year), "body": body,
                    "question_type": qtype_code,
                    "options": clean_opts,
                    "correct": _ai_correct,
                    "correct_answer": "",
                    "difficulty": _ai_diff,
                    "topic": _ai_topic,
                    "solution": _ai_sol,
                    "figure": _figure,
                    "image": None,
                }

            new_id = db_add(q_data)
            has_fig = "with figure" if _figure else ""
            file_written = f"data/competitions/{comp}.json"
            st.success(
                f"✓ Question #{new_id} saved as **{qtype_code.upper()}** {has_fig}!\n\n"
                f"📁 Written to: `{file_written}`"
            )
            st.rerun()




def _admin_pdf_import():
    """
    PDF Batch Import — smart auto-crop with per-question image preview.
    Two modes:
      • Auto-detect: whitespace-based or equal-split crop
      • Manual: choose questions-per-page
    Each cropped image is sent to Claude with strict LaTeX prompt.
    Admin reviews, edits, and saves to question bank with crop image attached.
    """
    st.markdown("#### 📄 PDF Batch Import")
    st.markdown(
        "Upload a multi-page PDF exam paper. The app **auto-crops each question** "
        "into an individual image, sends each to AI for extraction with **strict LaTeX**, "
        "then lets you review and edit before saving to the question bank."
    )

    # ── Settings ─────────────────────────────────────────────────────────────
    st.markdown("---")
    c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
    with c1:
        pdf_file = st.file_uploader(
            "Upload exam PDF", type=["pdf"], key="pdf_upload_v2",
        )
    with c2:
        pdf_comp = st.selectbox(
            "Competition",
            [""] + list(get_all_competitions().keys()),
            format_func=lambda k: "Select…" if k == "" else get_all_competitions().get(k, k),
            key="pdf_comp_v2",
        )
    with c3:
        pdf_year = st.number_input("Year", 1990, 2030, 2024, key="pdf_year_v2")
    with c4:
        pdf_dpi = st.select_slider(
            "DPI quality",
            options=[150, 200, 250, 300],
            value=200,
            format_func=lambda x: f"{x} DPI",
        )

    crop_mode = st.radio(
        "**Crop mode**",
        ["🤖 Auto-detect gaps (digital PDF)",
         "📏 Equal split (specify questions per page)",
         "🧠 AI counts questions per page (slowest, most accurate)"],
        horizontal=True,
        key="pdf_crop_mode",
    )

    if "Equal split" in crop_mode:
        q_per_page = st.slider(
            "Questions per page", 1, 15, 3,
            help="How many questions appear on each page of the PDF.",
        )
    else:
        q_per_page = None

    # ── Extract button ────────────────────────────────────────────────────────
    if pdf_file and pdf_comp:
        pdf_bytes = pdf_file.read()
        try:
            import fitz as _fitz
            doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
            n_pages = len(doc)
            doc.close()
        except Exception:
            n_pages = "?"

        st.info(f"📄 **{n_pages} pages** · Competition: **{get_all_competitions().get(pdf_comp, pdf_comp)}**")

        if not _get_api_key():
            st.error("⚠️ Anthropic API key required. Add it in Admin → Settings.")
        elif st.button("🤖 Convert & Extract All Questions", type="primary",
                       use_container_width=True, key="pdf_go_v2"):

            with st.spinner("Converting PDF to images…"):
                try:
                    page_imgs = _pdf_to_page_images(pdf_bytes, dpi=pdf_dpi)
                except Exception as e:
                    st.error(f"PDF conversion failed: {e}")
                    page_imgs = []

            if page_imgs:
                all_crops  = []   # (crop_png, page_num)
                prog = st.progress(0, "Cropping pages…")

                for pi, pg_png in enumerate(page_imgs):
                    prog.progress((pi + 0.3) / len(page_imgs),
                                  f"Cropping page {pi+1}/{len(page_imgs)}…")

                    if "Auto-detect" in crop_mode:
                        crops = _whitespace_crop(pg_png)
                        if len(crops) <= 1:
                            # Fallback to equal split if no gaps found
                            crops = _equal_crop(pg_png, max(1, q_per_page or 3))
                    elif "Equal split" in crop_mode:
                        crops = _equal_crop(pg_png, q_per_page or 3)
                    else:  # AI counts
                        prog.progress((pi + 0.5) / len(page_imgs),
                                      f"AI counting questions on page {pi+1}…")
                        n_q = _ai_count_questions_on_page(pg_png)
                        crops = _equal_crop(pg_png, max(1, n_q))

                    for crop in crops:
                        all_crops.append((crop, pi + 1))

                prog.progress(1.0, f"Cropped into {len(all_crops)} question images")

                # Now send each crop to AI
                prog2 = st.progress(0, "AI extracting questions…")
                extracted = []
                for qi, (crop_png, page_n) in enumerate(all_crops):
                    prog2.progress(
                        (qi + 1) / len(all_crops),
                        f"AI reading question {qi+1}/{len(all_crops)}…"
                    )
                    result = _ai_extract_one_question(
                        crop_png, pdf_comp, qi + 1, page_n
                    )
                    extracted.append(result)

                prog2.empty()
                st.session_state["_pdf_v2_extracted"] = extracted
                st.session_state["_pdf_v2_comp"]      = pdf_comp
                st.session_state["_pdf_v2_year"]      = int(pdf_year)
                st.success(
                    f"✓ **{len(extracted)} questions** extracted! "
                    f"Review and edit below, then save to Question Bank."
                )
                st.rerun()

    elif pdf_file and not pdf_comp:
        st.warning("☝️ Select a competition first.")

    # ── Review panel ─────────────────────────────────────────────────────────
    extracted   = st.session_state.get("_pdf_v2_extracted", [])
    saved_comp  = st.session_state.get("_pdf_v2_comp", "")
    saved_year  = st.session_state.get("_pdf_v2_year", 2024)

    if not extracted:
        if not pdf_file:
            st.markdown(
                """<div style="background:#FAF7F0;border:2px dashed #DDD8CC;border-radius:14px;
                padding:3rem;text-align:center;margin-top:1rem;">
                <div style="font-size:3rem;">📄</div>
                <div style="font-weight:500;margin:.5rem 0;">Upload a PDF to begin</div>
                <div style="font-size:13px;color:#888;line-height:1.6;">
                Supports AMC, AIME, Sansu, Thai competition papers.<br>
                Each question is auto-cropped and its image is preserved in the question bank.
                </div></div>""",
                unsafe_allow_html=True,
            )
        return

    # Summary bar
    ok_count  = sum(1 for q in extracted if not q.get("_error"))
    err_count = sum(1 for q in extracted if q.get("_error"))
    st.markdown(
        f'''<div style="background:#0D1B2A;border-radius:10px;padding:.75rem 1.25rem;
        display:flex;gap:2rem;margin-bottom:1rem;">
        <span style="color:#F0D98A;font-weight:600;">{len(extracted)} questions extracted</span>
        <span style="color:#9FE1CB;">✓ {ok_count} ready</span>
        {f'<span style="color:#F08080;">⚠ {err_count} need attention</span>' if err_count else ""}
        </div>''',
        unsafe_allow_html=True,
    )

    letters = ["A", "B", "C", "D", "E"]
    edited_qs = []

    for idx, q in enumerate(extracted):
        is_err   = q.get("_error", False)
        q_num    = q.get("_q_num", idx + 1)
        page_n   = q.get("_page", "?")
        crop_png = q.get("_crop_png")
        has_fig  = q.get("has_figure", False) or "[FIGURE]" in q.get("question", "")
        icon     = "⚠️" if is_err else f"Q{q_num}"

        with st.expander(
            f"{icon}  page {page_n}  ·  {q.get('topic','?')}  ·  "
            f"{q.get('difficulty','?').title()}  ·  "
            f"{q.get('question','')[:55]}…",
            expanded=is_err,
        ):
            # ── Crop preview + figure upload ──────────────────────────────
            img_col, edit_col = st.columns([1, 2])

            with img_col:
                st.markdown("**📸 Cropped Image**")
                if crop_png:
                    st.image(crop_png, use_column_width=True,
                             caption=f"Page {page_n} · Q{q_num}")

                # Upload better/custom crop
                custom_fig = st.file_uploader(
                    "Replace with custom crop",
                    type=["png","jpg","jpeg"],
                    key=f"pdfv2_fig_{idx}",
                    help="Upload a more precise crop of this question if needed.",
                )
                use_crop_as_figure = st.checkbox(
                    "Save image with question",
                    value=has_fig,
                    key=f"pdfv2_use_fig_{idx}",
                    help="Attach this cropped image to the question in the question bank.",
                )

            with edit_col:
                # Question body
                st.markdown(
                    '''<div style="background:#FFF8E0;border:.5px solid #E8C84A;
                    border-radius:6px;padding:4px 8px;font-size:11px;color:#7A5800;margin-bottom:4px;">
                    ✏️ LaTeX: $x^2$  $\\frac{a}{b}$  $\\sqrt{x}$  $\\angle ABC$  $\\triangle$
                    </div>''',
                    unsafe_allow_html=True,
                )
                new_body = st.text_area(
                    "Question text",
                    value=q.get("question", ""),
                    height=90,
                    key=f"pdfv2_body_{idx}",
                )

                # Question type + correct answer
                qtype_map = {"mcq5":"MCQ 5 (A–E)","mcq4":"MCQ 4 (A–D)","fill":"Fill-in"}
                cur_qt = q.get("question_type","mcq5")
                if cur_qt not in qtype_map:
                    cur_qt = "mcq5"
                new_qt = st.selectbox(
                    "Type", list(qtype_map.keys()),
                    index=list(qtype_map.keys()).index(cur_qt),
                    format_func=lambda x: qtype_map[x],
                    key=f"pdfv2_qt_{idx}",
                )

                opts = q.get("options", [])
                new_opts = []
                fill_ans = ""
                if new_qt in ("mcq5","mcq4"):
                    n_opts = 5 if new_qt == "mcq5" else 4
                    oc = st.columns(n_opts)
                    for oi in range(n_opts):
                        with oc[oi]:
                            new_opts.append(
                                st.text_input(
                                    letters[oi],
                                    value=opts[oi] if oi < len(opts) else "",
                                    key=f"pdfv2_opt_{idx}_{oi}",
                                )
                            )
                    ci = q.get("correct_index", 0)
                    try:
                        ci = int(ci)
                        if ci >= n_opts: ci = 0
                    except Exception:
                        ci = 0
                    new_correct = st.radio(
                        "Correct", list(range(n_opts)), index=ci,
                        format_func=lambda i: letters[i],
                        horizontal=True, key=f"pdfv2_ci_{idx}",
                    )
                else:
                    new_correct = 0
                    fill_ans = st.text_input(
                        "Correct answer", key=f"pdfv2_fill_{idx}"
                    )

                dc1, dc2 = st.columns(2)
                with dc1:
                    diffs = ["easy","intermediate","advanced"]
                    cur_d = q.get("difficulty","intermediate")
                    if cur_d not in diffs: cur_d = "intermediate"
                    new_diff = st.selectbox(
                        "Difficulty", diffs,
                        index=diffs.index(cur_d),
                        format_func=_diff_badge_text,
                        key=f"pdfv2_diff_{idx}",
                    )
                with dc2:
                    cur_t = q.get("topic","Others")
                    if cur_t not in TOPICS: cur_t = "Others"
                    new_topic = st.selectbox(
                        "Topic", TOPICS,
                        index=TOPICS.index(cur_t),
                        key=f"pdfv2_topic_{idx}",
                    )

                new_sol = st.text_area(
                    "Solution (LaTeX)",
                    value=q.get("solution",""),
                    height=80,
                    key=f"pdfv2_sol_{idx}",
                )

            # Figure data
            if custom_fig:
                fig_data = _img_to_b64(custom_fig.read(),
                                       custom_fig.type or "image/png")
            elif use_crop_as_figure and crop_png:
                fig_data = _img_to_b64(crop_png, "image/png")
            else:
                fig_data = None

            save_this = st.checkbox(
                "✅ Include in Save All",
                value=not is_err,
                key=f"pdfv2_save_{idx}",
            )

            edited_qs.append({
                "comp":           saved_comp,
                "year":           saved_year,
                "body":           new_body,
                "options":        [o for o in new_opts if o.strip()],
                "correct":        new_correct,
                "correct_answer": fill_ans.strip(),
                "difficulty":     new_diff,
                "topic":          new_topic,
                "solution":       new_sol,
                "question_type":  new_qt,
                "figure":         fig_data,
                "image":          None,
                "_save":          save_this,
            })

    # ── Save All ──────────────────────────────────────────────────────────────
    st.markdown("---")
    n_sel = sum(1 for q in edited_qs if q.get("_save"))
    bc1, bc2 = st.columns([3, 1])
    with bc1:
        if st.button(
            f"💾 Save {n_sel} Selected Questions to Question Bank",
            type="primary", use_container_width=True, key="pdfv2_save_all",
            disabled=n_sel == 0,
        ):
            saved = errs = 0
            for q in edited_qs:
                if not q["_save"] or not q["body"].strip():
                    continue
                try:
                    q_data = {k: v for k, v in q.items() if not k.startswith("_")}
                    db_add(q_data)
                    saved += 1
                except Exception as e:
                    errs += 1
                    st.error(str(e))
            if saved:
                st.success(f"✓ {saved} questions saved to Question Bank!")
                st.session_state.pop("_pdf_v2_extracted", None)
                st.rerun()
    with bc2:
        if st.button("🗑️ Clear", use_container_width=True, key="pdfv2_clear"):
            st.session_state.pop("_pdf_v2_extracted", None)
            st.rerun()


def _admin_competitions():
    """Create and manage custom competitions with display settings."""
    st.markdown("#### 🏆 Competition Management")
    st.markdown(
        "Create custom competitions or modify built-in ones. "
        "Control whether students see their score, solution, and AI analysis."
    )

    all_comps = get_all_competitions()
    custom_comps = _load_custom_comps()
    custom_codes = {c["code"] for c in custom_comps}

    # ── Create new competition ─────────────────────────────────────────────
    st.markdown("---")
    st.markdown("**➕ Create New Competition**")
    # Get the app's base URL for link generation
    try:
        app_url = st.secrets.get("APP_URL", "").rstrip("/")
    except Exception:
        app_url = ""
    if not app_url:
        try:
            app_url = f"https://{st.context.headers.get('host','your-app.streamlit.app')}"
        except Exception:
            app_url = "https://your-app.streamlit.app"

    with st.form("new_comp_form"):
        nc1, nc2 = st.columns(2)
        with nc1:
            comp_name = st.text_input("Competition Name *",
                                      placeholder="e.g. Math Mission Thailand 2025")
        with nc2:
            comp_code = st.text_input("Short Code *",
                                      placeholder="e.g. MMT2025  (no spaces)")

        fa1, fa2, fa3 = st.columns(3)
        with fa1:
            q_count_fixed = st.number_input(
                "Fixed no. of questions",
                min_value=0, max_value=100, value=0,
                help="Set to 0 to let students choose. Set a number to lock it for all participants."
            )
        with fa2:
            time_limit_fixed = st.selectbox(
                "Fixed time limit",
                [0, 20, 30, 40, 45, 60, 75, 90, 120],
                index=0,
                format_func=lambda x: "Student chooses" if x == 0 else f"{x} min",
                help="Set a fixed time limit for this competition."
            )
        with fa3:
            diff_fixed = st.selectbox(
                "Fixed difficulty",
                ["student_choice", "easy", "intermediate", "advanced", "mixed"],
                format_func=lambda x: {
                    "student_choice": "Student chooses",
                    "easy": "🟢 Easy", "intermediate": "🟡 Intermediate",
                    "advanced": "🔴 Advanced", "mixed": "🎲 Mixed"
                }[x],
                help="Lock the difficulty for this competition."
            )

        st.markdown("**Visibility Settings for Students**")
        vc1, vc2, vc3 = st.columns(3)
        with vc1:
            show_score = st.checkbox("Show Score", value=True,
                                     help="Students see their percentage and correct/wrong count")
        with vc2:
            show_solution = st.checkbox("Show Solution", value=True,
                                        help="Students can view detailed solutions after exam")
        with vc3:
            show_analysis = st.checkbox("Show AI Analysis", value=True,
                                        help="Students receive personalised AI coaching report")
        create_btn = st.form_submit_button("✅ Create Competition", type="primary")

    if create_btn:
        comp_code_clean = re.sub(r"\s+", "", comp_code).upper()
        if not comp_name.strip():
            st.error("Competition name is required.")
        elif not comp_code_clean:
            st.error("Short code is required.")
        elif comp_code_clean in all_comps:
            st.error(f"Code '{comp_code_clean}' already exists. Choose a different code.")
        else:
            new_comp = {
                "code":            comp_code_clean,
                "name":            comp_name.strip(),
                "show_score":      show_score,
                "show_solution":   show_solution,
                "show_analysis":   show_analysis,
                "q_count_fixed":   int(q_count_fixed),
                "time_limit_fixed":int(time_limit_fixed),
                "diff_fixed":      diff_fixed,
            }
            custom_comps.append(new_comp)
            _save_custom_comps(custom_comps)
            direct_link = f"{app_url}/?exam={comp_code_clean}"
            st.success(f"✓ Competition **{comp_name.strip()}** ({comp_code_clean}) created!")
            st.info(f"🔗 Direct link for participants:\n\n`{direct_link}`")
            st.rerun()

    # ── Custom competitions list ───────────────────────────────────────────
    if custom_comps:
        st.markdown("---")
        st.markdown("**Your Custom Competitions**")
        for idx, cc in enumerate(custom_comps):
            code      = cc["code"]
            name      = cc["name"]
            link      = f"{app_url}/?exam={code}"
            q_fix     = cc.get("q_count_fixed", 0)
            t_fix     = cc.get("time_limit_fixed", 0)
            d_fix     = cc.get("diff_fixed", "student_choice")
            qs_in_db  = len(_load_comp_db(code))

            with st.expander(f"🏆 **{name}** `{code}` — {qs_in_db} questions in DB"):
                # Direct link
                st.markdown("**🔗 Direct Link for Participants**")
                st.markdown(
                    f'''<div style="background:#0D1B2A;border-radius:8px;padding:.75rem 1rem;
                    display:flex;align-items:center;justify-content:space-between;margin-bottom:.5rem;">
                    <span style="font-family:monospace;font-size:13px;color:#F0D98A;">{link}</span>
                    </div>''',
                    unsafe_allow_html=True,
                )
                st.code(link, language=None)
                st.caption("Share this link with participants. They go directly into this competition.")

                # Settings summary
                sc1, sc2, sc3, sc4, sc5 = st.columns(5)
                sc1.markdown(f"{'✅' if cc.get('show_score', True) else '🔒'} Score")
                sc2.markdown(f"{'✅' if cc.get('show_solution', True) else '🔒'} Solution")
                sc3.markdown(f"{'✅' if cc.get('show_analysis', True) else '🔒'} AI Analysis")
                sc4.markdown(f"📝 {q_fix if q_fix else 'Flexible'} Q")
                sc5.markdown(f"⏱ {f'{t_fix}min' if t_fix else 'Flexible'}")

                # DB status + export
                if qs_in_db == 0:
                    st.error(f"⚠️ No questions found. Add questions for `{code}` in Admin → Add Question.")
                else:
                    st.success(f"✓ {qs_in_db} questions ready in database.")
                    # Export this competition's question file
                    comp_qs_data = _load_comp_db(code)
                    if comp_qs_data:
                        import json as _json
                        export_bytes = _json.dumps(comp_qs_data,
                                                   ensure_ascii=False, indent=2).encode()
                        st.download_button(
                            label=f"📥 Download {code}.json (commit to GitHub)",
                            data=export_bytes,
                            file_name=f"{code}.json",
                            mime="application/json",
                            key=f"export_comp_{idx}",
                            help=(
                                f"Download this file and upload it to GitHub at "
                                f"data/competitions/{code}.json so questions survive redeploy."
                            ),
                            use_container_width=True,
                        )
                    st.info(
                        f"💡 **Important:** After adding questions on Streamlit Cloud, "
                        f"download `{code}.json` above and commit it to GitHub at "
                        f"`data/competitions/{code}.json` — otherwise questions disappear "
                        f"when the app restarts.",
                    )

                # Delete
                if st.button(f"🗑️ Delete competition", key=f"del_comp_{idx}",
                             type="secondary"):
                    custom_comps.pop(idx)
                    _save_custom_comps(custom_comps)
                    st.rerun()
    else:
        st.info("No custom competitions yet. Create one above.")

    # ── Built-in competition info ──────────────────────────────────────────
    st.markdown("---")
    st.markdown("**Built-in Competitions** *(score, solution, and AI analysis always shown)*")
    built_in_html = "".join(
        f'<span style="display:inline-block;background:#F0F0F0;border-radius:6px;'
        f'padding:3px 10px;margin:3px;font-size:12px;">{v}</span>'
        for k, v in COMPETITIONS.items()
    )
    st.markdown(f'<div>{built_in_html}</div>', unsafe_allow_html=True)


def _build_summary_csv(records: list) -> str:
    """Build a CSV string: one row per exam session (summary)."""
    import io, csv
    buf = io.StringIO()
    writer = csv.writer(buf)
    # Header
    header = [
        "Timestamp", "First Name", "Last Name", "School",
        "Competition", "Mode", "Difficulty",
        "Total Q", "Correct", "Wrong", "Skipped", "Score %", "Time (sec)",
        "Algebra C/T", "Geometry C/T", "Number Theory C/T",
        "Combinatorics C/T", "Word Problem C/T", "Others C/T",
    ]
    writer.writerow(header)
    for r in records:
        ts_dict = r.get("topic_scores", {})
        def ct(t):
            v = ts_dict.get(t, {})
            return f"{v.get('correct',0)}/{v.get('total',0)}"
        writer.writerow([
            r.get("timestamp",""), r.get("first_name",""), r.get("last_name",""),
            r.get("school",""), r.get("competition",""), r.get("mode",""),
            r.get("difficulty",""), r.get("total_q",""), r.get("correct",""),
            r.get("wrong",""), r.get("skipped",""), r.get("score_pct",""),
            r.get("time_sec",""),
            ct("Algebra"), ct("Geometry"), ct("Number Theory"),
            ct("Combinatorics"), ct("Word Problem"), ct("Others"),
        ])
    return buf.getvalue()


def _build_answers_csv(records: list, db_questions: list) -> str:
    """Build a CSV string: one row per question per student (detailed answers)."""
    import io, csv
    # Build question lookup
    q_map = {str(q["id"]): q for q in db_questions}
    buf = io.StringIO()
    writer = csv.writer(buf)
    header = [
        "Timestamp", "First Name", "Last Name", "School",
        "Competition", "Difficulty", "Score %",
        "Q#", "Question (truncated)", "Topic", "Q Difficulty",
        "Student Answer", "Correct Answer", "Result",
    ]
    writer.writerow(header)
    for r in records:
        ans_data = r.get("answers", {})
        for qid_str, ans in ans_data.items():
            q = q_map.get(qid_str, {})
            body = q.get("body", ans.get("topic",""))[:80]
            result = "Correct" if ans.get("is_correct") else "Wrong"
            writer.writerow([
                r.get("timestamp",""), r.get("first_name",""), r.get("last_name",""),
                r.get("school",""), r.get("competition",""), r.get("difficulty",""),
                r.get("score_pct",""),
                qid_str, body,
                ans.get("topic",""), ans.get("difficulty",""),
                ans.get("given",""), ans.get("correct",""), result,
            ])
    return buf.getvalue()


def _admin_records():
    """View all student exam records with full answer details and spreadsheet export."""
    st.markdown("#### 👥 Student Records")

    records = _load_records()
    all_qs  = db_get_all()

    if not records:
        st.info("No student exam records yet. Records are created when students complete exams.")
        return

    # ── Summary metrics ────────────────────────────────────────────────────
    total_exams     = len(records)
    unique_students = len({(r["first_name"], r["last_name"]) for r in records})
    avg_score       = round(sum(r.get("score_pct", 0) for r in records) / total_exams, 1)

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Exams", total_exams)
    m2.metric("Unique Students", unique_students)
    m3.metric("Average Score", f"{avg_score}%")

    st.markdown("---")

    # ── Filters ────────────────────────────────────────────────────────────
    f1, f2, f3, f4, f5 = st.columns([2, 2, 2, 2, 2])
    with f1:
        search_name = st.text_input("🔍 Name", placeholder="First or last name",
                                    label_visibility="collapsed")
    with f2:
        all_comps_in_records = sorted({r.get("competition","") for r in records if r.get("competition")})
        comp_filter = st.selectbox("Competition", ["All competitions"] + all_comps_in_records,
                                   label_visibility="collapsed")
    with f3:
        all_schools = sorted({r.get("school","") for r in records if r.get("school")})
        school_filter = st.selectbox("School", ["All schools"] + all_schools,
                                     label_visibility="collapsed")
    with f4:
        all_pins = sorted({r.get("class_pin","") for r in records if r.get("class_pin","")})
        pin_filter = st.selectbox("Class PIN", ["All classes"] + all_pins,
                                  label_visibility="collapsed",
                                  help="Filter by the class PIN issued to students")
    with f5:
        date_filter = st.selectbox("Date", ["All time", "Today", "This week", "This month"],
                                   label_visibility="collapsed")

    # Apply filters
    import datetime
    filtered = records[:]
    if search_name:
        low = search_name.lower()
        filtered = [r for r in filtered
                    if low in r.get("first_name","").lower()
                    or low in r.get("last_name","").lower()]
    if comp_filter != "All competitions":
        filtered = [r for r in filtered if r.get("competition") == comp_filter]
    if school_filter != "All schools":
        filtered = [r for r in filtered if r.get("school") == school_filter]
    if pin_filter != "All classes":
        filtered = [r for r in filtered if r.get("class_pin","") == pin_filter]
    if date_filter != "All time":
        today = datetime.date.today()
        def in_range(r):
            try:
                rd = datetime.date.fromisoformat(r.get("timestamp","")[:10])
                if date_filter == "Today":     return rd == today
                if date_filter == "This week": return (today - rd).days <= 7
                if date_filter == "This month":return rd.month == today.month and rd.year == today.year
            except Exception:
                return True
        filtered = [r for r in filtered if in_range(r)]

    st.markdown(f"Showing **{len(filtered)}** of {total_exams} records")

    # ── Export buttons ─────────────────────────────────────────────────────
    st.markdown("**📤 Export to Spreadsheet**")
    ex1, ex2, ex3 = st.columns(3)
    with ex1:
        csv_summary = _build_summary_csv(filtered)
        st.download_button(
            "📊 Summary Sheet (CSV)",
            data=csv_summary,
            file_name="mathcomp_summary.csv",
            mime="text/csv",
            help="One row per exam. Open in Excel or Google Sheets.",
            use_container_width=True,
        )
    with ex2:
        csv_answers = _build_answers_csv(filtered, all_qs)
        st.download_button(
            "📋 Full Answer Sheet (CSV)",
            data=csv_answers,
            file_name="mathcomp_answers.csv",
            mime="text/csv",
            help="One row per question per student — all answers visible.",
            use_container_width=True,
        )
    with ex3:
        json_data = json.dumps(filtered, ensure_ascii=False, indent=2)
        st.download_button(
            "🗂️ Raw JSON",
            data=json_data,
            file_name="mathcomp_records.json",
            mime="application/json",
            help="Complete data including all fields.",
            use_container_width=True,
        )

    st.markdown("---")

    # ── Records table ──────────────────────────────────────────────────────
    q_map = {str(q["id"]): q for q in all_qs}
    letters = ["A", "B", "C", "D", "E"]

    for r in reversed(filtered):  # newest first
        name      = f"{r.get('first_name','')} {r.get('last_name','')}".strip()
        school    = r.get("school","—") or "—"
        comp      = r.get("competition","—")
        score     = r.get("score_pct", 0)
        correct   = r.get("correct", 0)
        total_q   = r.get("total_q", 0)
        ts        = r.get("timestamp","")
        diff      = r.get("difficulty","—")
        t_sec     = r.get("time_sec", 0)
        mm2, ss2  = divmod(t_sec, 60)
        score_col = "#2D7D4F" if score >= 80 else ("#B8860B" if score >= 60 else "#C0392B")
        topic_sc  = r.get("topic_scores", {})
        ans_data  = r.get("answers", {})

        with st.expander(
            f"**{name}** · {comp} · {score}% ({correct}/{total_q}) · {ts[:10]}"
        ):
            # ── Header row ────────────────────────────────────────────────
            ec1, ec2, ec3, ec4 = st.columns(4)
            ec1.markdown(f"**School:** {school}")
            ec2.markdown(f"**Difficulty:** {diff.title()}")
            ec3.markdown(f"**Time:** {mm2}m {ss2}s")
            ec4.markdown(
                f'<div style="font-size:1.6rem;font-weight:700;color:{score_col};">'
                f'{score}%  <span style="font-size:13px;color:#888;">({correct}/{total_q})</span></div>',
                unsafe_allow_html=True,
            )

            # ── Topic breakdown ───────────────────────────────────────────
            if topic_sc:
                st.markdown("**Topic Performance:**")
                tc_cols = st.columns(len(topic_sc))
                for ti, (t_name, vals) in enumerate(topic_sc.items()):
                    tc = vals.get("correct", 0)
                    tt = vals.get("total", 0)
                    tpct = round(tc/tt*100) if tt else 0
                    bar_col = "#2D7D4F" if tpct>=70 else "#B8860B" if tpct>=40 else "#C0392B"
                    with tc_cols[ti]:
                        st.markdown(
                            f'<div style="text-align:center;padding:.3rem;background:#F8F8F8;'
                            f'border-radius:8px;">'
                            f'<div style="font-size:11px;color:#666;">{t_name[:8]}</div>'
                            f'<div style="font-size:1.1rem;font-weight:600;color:{bar_col};">'
                            f'{tc}/{tt}</div>'
                            f'<div style="font-size:10px;color:{bar_col};">{tpct}%</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

            st.markdown("---")

            # ── Full answer detail table ───────────────────────────────────
            if ans_data:
                st.markdown("**📋 All Answers:**")
                rows_html = ""
                for i, (qid_str, ans) in enumerate(ans_data.items()):
                    ic        = ans.get("is_correct", False)
                    q         = q_map.get(qid_str, {})
                    qtype     = q.get("question_type", "mcq5")
                    q_body    = q.get("body", "—")[:90] + ("…" if len(q.get("body","")) > 90 else "")
                    given_raw = ans.get("given", "—")
                    corr_raw  = ans.get("correct", "—")
                    t_name    = ans.get("topic","—")
                    d_name    = ans.get("difficulty","—")

                    # Resolve option letters for MCQ
                    if qtype in ("mcq5","mcq4",""):
                        opts = q.get("options",[])
                        try:
                            given_disp = f"{letters[int(given_raw)]}. {opts[int(given_raw)]}" if given_raw not in ("","—") else "—"
                        except Exception:
                            given_disp = given_raw
                        try:
                            corr_disp  = f"{letters[int(corr_raw)]}. {opts[int(corr_raw)]}" if corr_raw not in ("","—") else "—"
                        except Exception:
                            corr_disp = corr_raw
                    else:
                        given_disp = given_raw
                        corr_disp  = corr_raw

                    bg      = "#F0FFF4" if ic else "#FFF5F5"
                    result  = '<span style="color:#2D7D4F;font-weight:600;">✅ Correct</span>' if ic else '<span style="color:#C0392B;font-weight:600;">❌ Wrong</span>'
                    rows_html += (
                        f'<tr style="background:{bg};">'
                        f'<td style="padding:5px 8px;font-weight:600;">Q{i+1}</td>'
                        f'<td style="padding:5px 8px;font-size:11px;max-width:260px;">{q_body}</td>'
                        f'<td style="padding:5px 8px;font-size:11px;">{t_name}</td>'
                        f'<td style="padding:5px 8px;font-size:11px;">{d_name.title()}</td>'
                        f'<td style="padding:5px 8px;">{result}</td>'
                        f'<td style="padding:5px 8px;font-size:11px;color:#333;">{given_disp}</td>'
                        f'<td style="padding:5px 8px;font-size:11px;color:#2D7D4F;">{corr_disp}</td>'
                        f'</tr>'
                    )
                st.markdown(
                    f'<div style="overflow-x:auto;">'
                    f'<table style="font-size:12px;border-collapse:collapse;width:100%;border:1px solid #EEE;">'
                    f'<thead><tr style="background:#0D1B2A;color:#fff;">'
                    f'<th style="padding:6px 8px;">Q</th>'
                    f'<th style="padding:6px 8px;text-align:left;">Question</th>'
                    f'<th style="padding:6px 8px;">Topic</th>'
                    f'<th style="padding:6px 8px;">Difficulty</th>'
                    f'<th style="padding:6px 8px;">Result</th>'
                    f'<th style="padding:6px 8px;text-align:left;">Student Answer</th>'
                    f'<th style="padding:6px 8px;text-align:left;">Correct Answer</th>'
                    f'</tr></thead>'
                    f'<tbody>{rows_html}</tbody></table></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.info("No answer details recorded for this session.")


def _admin_upload():
    st.markdown("#### 📤 Upload Image & AI Extraction")
    st.markdown("Upload a photo of a competition question — AI will read, assess difficulty, and write a full solution.")

    uc1, uc2 = st.columns(2)
    with uc1:
        uploaded = st.file_uploader("Drop question image here",
                                    type=["png", "jpg", "jpeg", "webp"])
        comp = st.selectbox("Competition", [""] + list(get_all_competitions().keys()),
                            format_func=lambda k: "Auto-detect" if k == "" else get_all_competitions().get(k, k))
        year = st.number_input("Year", 1990, 2030, 2024, key="upload_year")

        if uploaded:
            img_bytes = uploaded.read()
            st.image(img_bytes, caption="Uploaded image", use_column_width=True)
            if st.button("🤖 Extract & Analyse with AI", type="primary"):
                if not _get_api_key():
                    st.error("⚠️ Set your Anthropic API key in Settings first.")
                else:
                    with st.spinner("Claude is reading the question… (15–30 seconds)"):
                        mime = f"image/{uploaded.type.split('/')[-1]}" if uploaded.type else "image/png"
                        result = ai_extract_image(img_bytes, mime, comp)
                    st.session_state["_upload_result"] = result
                    st.session_state["_upload_image"] = img_bytes
                    if result.get("_latex_warning"):
                        st.warning(
                            "⚠️ LaTeX may be missing in extracted text. "
                            "Please check the question and options below and "
                            "wrap any math expressions in $...$ before saving."
                        )
                    else:
                        st.success("✓ Extraction complete — LaTeX formatting applied!")

    with uc2:
        result = st.session_state.get("_upload_result")
        if result:
            st.markdown("**📋 Extracted Question** *(editable)*")
            st.markdown(
                '''<div style="background:#FFF8E0;border:.5px solid #E8C84A;border-radius:7px;
                padding:.4rem .8rem;font-size:12px;color:#7A5800;margin-bottom:.4rem;">
                ✏️ <strong>LaTeX required:</strong> All math must use <code>$...$</code> for inline
                and <code>$$...$$</code> for display equations.
                e.g. <code>$x^2+y^2$</code>, <code>$\\frac{a}{b}$</code>, <code>$\\sqrt{x}$</code>
                </div>''',
                unsafe_allow_html=True,
            )
            q_text = st.text_area("Question", result.get("question", ""), height=100)
            opts_raw = result.get("options", ["A", "B", "C", "D"])
            letters = ["A","B","C","D","E"]
            new_opts = []
            for i, o in enumerate(opts_raw[:5]):
                new_opts.append(st.text_input(f"Option {letters[i]}", o, key=f"up_opt_{i}"))
            _correct_raw = result.get("correct_index", 0)
            try:
                _correct_idx = int(_correct_raw)
                if _correct_idx < 0 or _correct_idx >= len(new_opts):
                    _correct_idx = 0
            except Exception:
                _correct_idx = 0
            correct_i = st.radio("Correct answer", list(range(len(new_opts))),
                                 index=_correct_idx,
                                 format_func=lambda i: letters[i], horizontal=True)
            r1, r2, r3 = st.columns(3)
            with r1:
                _diff_val = result.get("difficulty","intermediate")
                _diff_val = _diff_val if _diff_val in ["easy","intermediate","advanced"] else "intermediate"
                diff = st.selectbox("Difficulty", ["easy","intermediate","advanced"],
                                    index=["easy","intermediate","advanced"].index(_diff_val),
                                    format_func=_diff_badge_text)
            with r2:
                tai = result.get("topic","Others")
                topic = st.selectbox("Topic", TOPICS,
                                     index=TOPICS.index(tai) if tai in TOPICS else 0)
            with r3:
                up_qtype = st.selectbox(
                    "Question Type",
                    ["mcq5", "mcq4", "fill"],
                    format_func=lambda x: {
                        "mcq5": "MCQ 5 options (A–E)",
                        "mcq4": "MCQ 4 options (A–D)",
                        "fill": "Fill-in (numeric)",
                    }[x],
                )
            _sol_raw = result.get("solution","")
            _sol_val = _sol_raw if not _sol_raw.startswith("⚠️") else ""
            sol = st.text_area("Solution *(AI-generated, editable)*", _sol_val, height=180)
            if _sol_raw.startswith("⚠️"):
                st.warning("AI could not generate a solution — please type it manually below, or add Anthropic credits and try again.")
            # Fill-in correct answer field (shown if fill type selected)
            if up_qtype == "fill":
                fill_correct = st.text_input(
                    "Correct answer (fill-in)",
                    value="",
                    placeholder="e.g. 42 or 1/3",
                )
            else:
                fill_correct = ""

            if st.button("💾 Save to Question Bank", type="primary", use_container_width=True):
                new_id = db_add({
                    "comp": comp or "AMC10", "year": int(year), "body": q_text,
                    "question_type": up_qtype,
                    "options": [o for o in new_opts if o.strip()] if up_qtype != "fill" else [],
                    "correct": correct_i if up_qtype != "fill" else 0,
                    "correct_answer": fill_correct.strip() if up_qtype == "fill" else "",
                    "difficulty": diff, "topic": topic,
                    "solution": sol, "image": None,
                })
                st.success(f"✓ Question #{new_id} saved!")
                st.session_state.pop("_upload_result", None)
                st.session_state.pop("_upload_image", None)
                st.rerun()
        else:
            st.markdown(
                '<div style="background:#FAF7F0;border:2px dashed #DDD8CC;border-radius:14px;'
                'padding:3rem;text-align:center;">'
                '<div style="font-size:2.5rem;">🔍</div>'
                '<div style="font-weight:500;margin:.5rem 0;">Upload an image to begin</div>'
                '<div style="font-size:13px;color:#888;">AI will extract question text, '
                'assess difficulty, and write a detailed solution</div></div>',
                unsafe_allow_html=True,
            )


def _admin_stats():
    st.markdown("#### 📊 Statistics")
    import plotly.express as px
    s = db_stats()
    total = s["total"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Questions", total)
    m2.metric("🟢 Easy", s["by_diff"].get("easy", 0))
    m3.metric("🟡 Intermediate", s["by_diff"].get("intermediate", 0))
    m4.metric("🔴 Advanced", s["by_diff"].get("advanced", 0))
    if total == 0:
        st.info("No questions yet.")
        return
    st.markdown("---")
    sc1, sc2 = st.columns(2)
    with sc1:
        st.markdown("**By Competition**")
        comp_data = {COMPETITIONS.get(k, k): v for k, v in s["by_comp"].items()}
        fig = px.bar(x=list(comp_data.values()), y=list(comp_data.keys()),
                     orientation="h", color_discrete_sequence=["#C9A84C"],
                     labels={"x": "Questions", "y": ""})
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          margin=dict(t=10,b=10,l=10,r=10), height=350,
                          font=dict(family="DM Sans"))
        st.plotly_chart(fig, use_container_width=True)
    with sc2:
        st.markdown("**By Topic**")
        td = s.get("by_topic", {})
        if td:
            fig2 = px.pie(values=list(td.values()), names=list(td.keys()),
                          color_discrete_sequence=["#0D1B2A","#C9A84C","#2D7D4F","#B8860B","#1A5F9E","#888"],
                          hole=0.45)
            fig2.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                               margin=dict(t=10,b=10,l=10,r=10), height=350,
                               font=dict(family="DM Sans"))
            st.plotly_chart(fig2, use_container_width=True)
    st.markdown("**By Difficulty**")
    colors = {"easy":"#2D7D4F","intermediate":"#B8860B","advanced":"#C0392B"}
    for d, cnt in s["by_diff"].items():
        pct = int(cnt/total*100) if total else 0
        st.markdown(
            f'<div style="margin-bottom:8px;">'
            f'<div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:3px;">'
            f'<span>{_diff_badge_text(d)}</span>'
            f'<span style="color:{colors[d]};font-weight:600;">{cnt} ({pct}%)</span></div>'
            f'<div style="background:#EDE8DC;border-radius:4px;height:8px;">'
            f'<div style="background:{colors[d]};width:{pct}%;height:100%;border-radius:4px;"></div>'
            f'</div></div>', unsafe_allow_html=True)


def _admin_settings():
    st.markdown("#### ⚙️ Settings")

    # ── 0. FIREBASE STATUS ────────────────────────────────────────────────
    st.markdown("**🔥 Firebase Status**")
    db_check = _fb()
    if db_check:
        st.success("✅ Firebase Firestore connected — all data permanently stored in the cloud.")
        # Test write button
        if st.button("🔬 Test Firebase Write/Read", key="fb_test_btn"):
            try:
                test_ref = db_check.collection("_test").document("ping")
                test_ref.set({"ts": "ok"})
                back = test_ref.get().to_dict()
                test_ref.delete()
                if back.get("ts") == "ok":
                    st.success("✓ Write → Read → Delete all succeeded!")
                else:
                    st.error("Write/read mismatch.")
            except Exception as e:
                st.error(f"Firebase test failed: {e}")
    else:
        st.warning(
            "⚠️ Firebase not connected — using local JSON files (data may be lost on restart). "
            "Add Firebase credentials to Streamlit Cloud Secrets to enable permanent storage."
        )
        with st.expander("How to add Firebase credentials"):
            st.markdown("""
**Add these 3 keys to Streamlit Cloud → App Settings → Secrets:**
```toml
FIREBASE_PROJECT_ID     = "your-project-id"
FIREBASE_CLIENT_EMAIL   = "firebase-adminsdk-xxx@your-project.iam.gserviceaccount.com"
FIREBASE_PRIVATE_KEY    = "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----\n"
```
Get these values from the JSON file you downloaded from Firebase Console → Project Settings → Service Accounts → Generate new private key.
            """)
    st.markdown("---")

    # ── 0b. FIREBASE WEB API KEY (for Student Auth) ────────────────────────
    st.markdown("**👤 Student Authentication (Firebase Web API Key)**")
    web_key = _firebase_web_api_key()
    if web_key:
        st.success("✅ FIREBASE_WEB_API_KEY set — นักเรียนสามารถสมัครและ login ได้")
    else:
        st.warning("⚠️ ยังไม่ได้ตั้งค่า FIREBASE_WEB_API_KEY — นักเรียนจะข้าม login ไปได้เลย (anonymous mode)")
        with st.expander("วิธีเพิ่ม Firebase Web API Key"):
            st.markdown("""
**ขั้นตอน:**
1. Firebase Console → Project Settings → General
2. เลื่อนลงหา **"Your apps"** → Web app → Copy **"apiKey"**
3. เพิ่มใน Streamlit Cloud → App Settings → Secrets:
```toml
FIREBASE_WEB_API_KEY = "AIzaSy..."
```
4. Firebase Console → Authentication → Sign-in method → เปิด **Email/Password**
            """)

    st.markdown("---")

    # ── 1. API KEY ─────────────────────────────────────────────────────────
    st.markdown("**🔑 Anthropic API Key**")
    current_key = _get_api_key()
    if current_key:
        masked = current_key[:12] + "..." + current_key[-6:]
        st.success(f"✓ API key active: `{masked}` — AI features are enabled.")
    else:
        st.warning("⚠️ No API key set. Add one below to enable AI features.")

    with st.form("api_key_form"):
        new_key = st.text_input(
            "New API Key",
            type="password",
            placeholder="sk-ant-api03-...",
            help="Paste your Anthropic API key. It will be saved permanently.",
        )
        save_key = st.form_submit_button("💾 Save API Key Permanently", type="primary")

    if save_key:
        new_key = new_key.strip()
        if not new_key.startswith("sk-ant-"):
            st.error("❌ That doesn't look like a valid Anthropic key (should start with sk-ant-).")
        else:
            saved = _write_secret("ANTHROPIC_API_KEY", new_key)
            st.session_state["api_key"] = new_key
            if saved:
                st.success("✓ API key saved permanently to secrets.toml.")
            else:
                st.info("✓ API key saved for this session. On Streamlit Cloud, update the key in App Settings → Secrets too.")

    st.markdown("---")

    # ── 2. CHANGE PASSWORD ─────────────────────────────────────────────────
    st.markdown("**🔐 Change Admin Password**")
    st.markdown(f"Admin account: `{_get_admin_email() or '(not set — add ADMIN_EMAIL to secrets)'}`")

    with st.form("change_pw_form"):
        cur_pw  = st.text_input("Current password", type="password")
        new_pw  = st.text_input("New password", type="password",
                                help="Minimum 8 characters")
        conf_pw = st.text_input("Confirm new password", type="password")
        change_pw = st.form_submit_button("🔐 Change Password", type="primary")

    if change_pw:
        stored_pw = _get_admin_password()
        if not stored_pw:
            st.error("❌ No current password set. Add ADMIN_PASSWORD to Streamlit secrets first.")
        elif _hash(cur_pw) != stored_pw:
            st.error("❌ Current password is incorrect.")
        elif len(new_pw) < 8:
            st.error("❌ New password must be at least 8 characters.")
        elif new_pw != conf_pw:
            st.error("❌ New passwords do not match.")
        else:
            saved = _write_secret("ADMIN_PASSWORD", _hash(new_pw))
            if saved:
                st.success("✓ Password changed successfully.")
            else:
                st.warning(
                    "Password changed for this session only. "
                    "On Streamlit Cloud, update ADMIN_PASSWORD in App Settings → Secrets "
                    f"to: `{_hash(new_pw)}`"
                )
            st.session_state["admin_logged_in"] = False
            st.info("Please sign in again with your new password.")
            st.rerun()

    st.markdown("---")

    # ── 3. CLASS PIN MANAGER ───────────────────────────────────────────────
    st.markdown("**🔖 Class PIN Manager**")
    st.caption(
        "Give students a 6-digit PIN so you can filter their results by class. "
        "Generate one per class/batch and share it before the exam."
    )
    import secrets as _secrets
    pin_c1, pin_c2 = st.columns([2, 1])
    with pin_c1:
        custom_pin = st.text_input("Custom PIN (leave blank to auto-generate)",
                                   placeholder="e.g. 240101", max_chars=10,
                                   key="custom_pin_input")
    with pin_c2:
        st.markdown("<div style='margin-top:1.65rem;'>", unsafe_allow_html=True)
        if st.button("🎲 Generate PIN", use_container_width=True):
            st.session_state["_gen_pin"] = str(_secrets.randbelow(900000) + 100000)
        st.markdown("</div>", unsafe_allow_html=True)

    final_pin = custom_pin.strip() or st.session_state.get("_gen_pin", "")
    if final_pin:
        st.success(f"📌 Class PIN: **{final_pin}** — share this with students before they start")
        records = _load_records()
        pin_records = [r for r in records if str(r.get("class_pin","")) == final_pin]
        if pin_records:
            st.info(f"📊 {len(pin_records)} exam(s) submitted with this PIN so far.")

    st.markdown("---")

    # ── 4. DATABASE ────────────────────────────────────────────────────────
    st.markdown("**🗄️ Database**")
    all_qs = db_get_all()
    st.info(f"📚 {len(all_qs)} questions in database.")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("📥 Export questions.json"):
            data = json.dumps(all_qs, ensure_ascii=False, indent=2)
            st.download_button("⬇️ Download questions.json", data,
                               "questions.json", "application/json")
    with c2:
        uploaded_db = st.file_uploader("📤 Import questions.json",
                                       type=["json"], key="db_import")
        if uploaded_db:
            new_qs = json.loads(uploaded_db.read())
            if st.button("✅ Confirm Import"):
                _save_db(new_qs)
                st.success(f"✓ Imported {len(new_qs)} questions.")
                st.rerun()

    st.markdown("---")

    # ── 5. HOSTING & RELIABILITY NOTE ─────────────────────────────────────
    st.markdown("**🚀 Hosting & Reliability**")
    st.info(
        "**Current:** Streamlit Community Cloud (free) — cold start ~15-30 sec, "
        "limited concurrent users.\n\n"
        "**Recommended for live exams:** Migrate to [Railway](https://railway.app) or "
        "[Render](https://render.com) (~$5-10/month) for always-on hosting with no cold start. "
        "Use the same `app.py` — no code changes needed, just set the same Streamlit secrets "
        "as environment variables."
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — SEED DATA (fallback if no questions.json)
# ══════════════════════════════════════════════════════════════════════════════
SEED_QUESTIONS = [
    {"id":1,"comp":"AMC10","year":2023,"body":r"If $x+y=10$ and $xy=21$, what is $x^2+y^2$?","options":["37","58","79","100","121"],"correct":1,"difficulty":"easy","topic":"Algebra","solution":r"$x^2+y^2=(x+y)^2-2xy=100-42=\boxed{58}$","image":None},
    {"id":2,"comp":"AMC10","year":2023,"body":"A box has 3 red, 4 blue, and 5 green balls. Two drawn without replacement — probability both same colour?","options":["19/66","3/11","17/66","5/22","7/33"],"correct":0,"difficulty":"intermediate","topic":"Combinatorics","solution":r"$\frac{\binom{3}{2}+\binom{4}{2}+\binom{5}{2}}{\binom{12}{2}}=\frac{19}{66}$","image":None},
    {"id":3,"comp":"AMC8","year":2023,"body":"What is 25% of 80?","options":["15","18","20","22","25"],"correct":2,"difficulty":"easy","topic":"Number Theory","solution":r"$\frac{1}{4}\times80=\boxed{20}$","image":None},
    {"id":4,"comp":"AMC-JR","year":2023,"body":"Sum of three consecutive integers is 99. What is the largest?","options":["31","32","33","34","35"],"correct":3,"difficulty":"easy","topic":"Algebra","solution":r"$n+(n+1)+(n+2)=99\Rightarrow n=32$. Largest $=\boxed{34}$.","image":None},
    {"id":5,"comp":"POSN-R1","year":2023,"body":r"หา $\gcd(2^{100}-1,\;2^{75}-1)$","options":[r"$2^{25}-1$",r"$2^{50}-1$",r"$2^5-1$",r"$2^{75}-1$","$1$"],"correct":0,"difficulty":"advanced","topic":"Number Theory","solution":r"$\gcd(2^m-1,2^n-1)=2^{\gcd(m,n)}-1$. $\gcd(100,75)=25$. Answer: $2^{25}-1$.","image":None},
    {"id":6,"comp":"MATH-MT","year":2023,"body":r"ถ้า $\log_2 3=a$ จงหา $\log_8 9$","options":[r"$a/2$",r"$2a/3$",r"$a/3$",r"$3a/2$","$2a$"],"correct":1,"difficulty":"intermediate","topic":"Algebra","solution":r"$\log_8 9=\frac{2\log3}{3\log2}=\frac{2a}{3}$","image":None},
    {"id":7,"comp":"AIME","year":2023,"body":"Number of positive integers ≤1000 divisible by 7 but not 11?","options":["117","124","129","130","142"],"correct":3,"difficulty":"advanced","topic":"Number Theory","solution":r"$\lfloor1000/7\rfloor-\lfloor1000/77\rfloor=142-12=\boxed{130}$","image":None},
    {"id":8,"comp":"AMC12","year":2023,"body":r"Let $f(x)=x^3-3x$. How many real solutions does $f(f(x))=0$ have?","options":["3","6","7","8","9"],"correct":4,"difficulty":"advanced","topic":"Algebra","solution":r"Each of $f(x)=0,\pm\sqrt{3}$ has 3 roots. Total: $\boxed{9}$.","image":None},
    {"id":9,"comp":"SANSU-JR","year":2023,"body":"How many 3-digit numbers have all distinct digits summing to 9?","options":["44","48","52","56","60"],"correct":1,"difficulty":"intermediate","topic":"Combinatorics","solution":"Triples without 0: 18. With 0: 16 (×4 arrangements). Also check {9,0,...}: 14 more. Total 48.","image":None},
    {"id":10,"comp":"AMC-INT","year":2023,"body":r"If $\log_2 x=5$, what is $\log_2(x^3)$?","options":["8","10","12","15","25"],"correct":3,"difficulty":"intermediate","topic":"Algebra","solution":r"$3\log_2 x=3\times5=\boxed{15}$","image":None},
]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — ROUTING
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("""
    <div style="padding:.5rem 0 1.2rem;">
      <div style="font-family:'Playfair Display',serif;font-size:1.35rem;font-weight:700;color:#F0D98A;">MathComp ✦</div>
      <div style="font-size:12px;color:rgba(255,255,255,.4);margin-top:2px;">Competition Exam Platform</div>
    </div>
    """, unsafe_allow_html=True)

    page = st.radio("Navigate", ["🎓  Student Exam", "⚙️  Admin Portal"],
                    label_visibility="collapsed")

    # Copyright footer pinned to bottom of sidebar
    st.markdown("""
    <div style="position:fixed;bottom:0;left:0;width:230px;
                padding:.75rem 1rem;
                background:rgba(13,27,42,0.95);
                border-top:1px solid rgba(255,255,255,0.07);
                font-size:10.5px;color:rgba(255,255,255,0.35);
                line-height:1.6;">
      © 2024 Dr.Che<br>
      <span style="color:rgba(201,168,76,0.6);">Math Mission Thailand</span><br>
      All rights reserved.
    </div>
    """, unsafe_allow_html=True)

# ── Direct competition link handler ──────────────────────────────────────────
# URL: ?exam=MMT2025  → auto-launches student into that competition
def _handle_direct_link():
    """Check for ?exam=CODE in URL and pre-configure the student session."""
    try:
        params = st.query_params
        exam_code = params.get("exam", "")
    except Exception:
        exam_code = ""

    if not exam_code:
        return False

    # Validate the competition exists
    all_comps = get_all_competitions()
    if exam_code not in all_comps:
        st.error(f"❌ Competition code `{exam_code}` not found.")
        return False

    # Pre-fill session state for this competition
    cs = get_competition_settings(exam_code)
    st.session_state["direct_exam_code"]    = exam_code
    st.session_state["comp_show_score"]     = cs.get("show_score", True)
    st.session_state["comp_show_solution"]  = cs.get("show_solution", True)
    st.session_state["comp_show_analysis"]  = cs.get("show_analysis", True)
    st.session_state["_direct_q_fix"]       = cs.get("q_count_fixed", 0)
    st.session_state["_direct_t_fix"]       = cs.get("time_limit_fixed", 0)
    st.session_state["_direct_d_fix"]       = cs.get("diff_fixed", "student_choice")
    return True


def page_student_direct(exam_code: str):
    """
    Streamlined student flow for direct competition links.
    Skips mode/competition selection — goes straight to name entry then exam settings.
    """
    _init_student()

    comp_name = get_all_competitions().get(exam_code, exam_code)
    cs        = get_competition_settings(exam_code)

    # Pre-load competition settings
    st.session_state["selected_comp"]       = exam_code
    st.session_state["exam_mode"]           = "competition"
    st.session_state["comp_show_score"]     = cs.get("show_score", True)
    st.session_state["comp_show_solution"]  = cs.get("show_solution", True)
    st.session_state["comp_show_analysis"]  = cs.get("show_analysis", True)

    # Apply fixed settings from competition config
    q_fix = cs.get("q_count_fixed", 0)
    t_fix = cs.get("time_limit_fixed", 0)
    d_fix = cs.get("diff_fixed", "student_choice")
    if q_fix:
        st.session_state["q_count"] = q_fix
    if t_fix:
        st.session_state["time_limit"] = t_fix
    if d_fix and d_fix != "student_choice":
        st.session_state["selected_diff"] = d_fix

    # Hero banner with competition name
    st.markdown(
        f'''<div class="hero">
        <h1>{comp_name} ✦</h1>
        <p>Competition Exam — MathComp · Math Mission Thailand</p>
        </div>''',
        unsafe_allow_html=True,
    )

    step = st.session_state.exam_step

    # Step 0: identity (always shown first)
    if step == "setup" and st.session_state.setup_step == 0:
        _step_indicator(["Your Details", "Exam Settings", "Start"], 1)
        st.markdown("### Enter Your Details")
        id_col1, id_col2 = st.columns(2)
        with id_col1:
            fn = st.text_input("First Name *", value=st.session_state.student_first_name,
                               placeholder="e.g. Somchai")
        with id_col2:
            ln = st.text_input("Last Name *", value=st.session_state.student_last_name,
                               placeholder="e.g. Jaidee")
        school = st.text_input("School (optional)",
                               value=st.session_state.student_school)
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("Continue →", type="primary",
                     disabled=(not fn.strip() or not ln.strip())):
            st.session_state.student_first_name = fn.strip()
            st.session_state.student_last_name  = ln.strip()
            st.session_state.student_school     = school.strip()
            # Skip to settings (step 3) or jump straight to exam if all fixed
            if q_fix and t_fix and d_fix != "student_choice":
                # All settings are fixed — go straight to exam
                avail = db_get_filtered(comp=exam_code, difficulty=d_fix)
                n = min(q_fix, len(avail))
                if n > 0:
                    import random as _r, time as _t
                    _r.shuffle(avail)
                    st.session_state.exam_questions = avail[:n]
                    st.session_state.exam_answers   = {}
                    st.session_state.exam_current   = 0
                    st.session_state.exam_start     = _t.time()
                    st.session_state.exam_submitted = False
                    st.session_state.exam_step      = "exam"
                    st.rerun()
                else:
                    st.error("No questions available for this competition yet.")
            else:
                st.session_state.setup_step = 3  # jump to settings
                st.rerun()
        if not fn.strip() or not ln.strip():
            st.caption("* First name and last name are required.")

    elif step == "setup" and st.session_state.setup_step == 3:
        _step_indicator(["Your Details", "Exam Settings", "Start"], 2)
        st.markdown(f"### Exam Settings — {comp_name}")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Difficulty Level**")
            if d_fix and d_fix != "student_choice":
                diff_labels = {"easy":"🟢 Easy","intermediate":"🟡 Intermediate",
                               "advanced":"🔴 Advanced","mixed":"🎲 Mixed"}
                st.markdown(
                    f'''<div style="background:#FFF8E0;border:.5px solid #E8C84A;
                    border-radius:8px;padding:.5rem .9rem;font-size:13px;color:#7A5800;">
                    🔒 Fixed by organiser: <strong>{diff_labels.get(d_fix, d_fix)}</strong>
                    </div>''',
                    unsafe_allow_html=True,
                )
                st.session_state.selected_diff = d_fix
            else:
                diff_map = {"🟢 Easy":"easy","🟡 Intermediate":"intermediate",
                            "🔴 Advanced":"advanced","🎲 Mixed (all levels)":"mixed"}
                diff_sel = st.radio("Difficulty", list(diff_map.keys()), index=3,
                                    label_visibility="collapsed")
                st.session_state.selected_diff = diff_map[diff_sel]

        with col2:
            st.markdown("**Number of Questions**")
            avail = db_get_filtered(comp=exam_code,
                                    difficulty=st.session_state.selected_diff)
            if q_fix:
                st.markdown(
                    f'''<div style="background:#FFF8E0;border:.5px solid #E8C84A;
                    border-radius:8px;padding:.5rem .9rem;font-size:13px;color:#7A5800;">
                    🔒 Fixed by organiser: <strong>{q_fix} questions</strong>
                    </div>''',
                    unsafe_allow_html=True,
                )
                st.session_state.q_count = q_fix
            else:
                default_q = _default_q_count("competition", comp_code=exam_code)
                n_avail   = len(avail)
                if st.session_state.q_count <= 10 and default_q > 10:
                    st.session_state.q_count = min(default_q, n_avail) if n_avail else default_q
                st.session_state.q_count = st.slider(
                    "Questions", 5, max(n_avail, 50, 5),
                    min(st.session_state.q_count, max(n_avail, 5)), 1,
                    label_visibility="collapsed")

            st.markdown("**Time Limit**")
            if t_fix:
                st.markdown(
                    f'''<div style="background:#FFF8E0;border:.5px solid #E8C84A;
                    border-radius:8px;padding:.5rem .9rem;font-size:13px;color:#7A5800;">
                    🔒 Fixed by organiser: <strong>{t_fix} minutes</strong>
                    </div>''',
                    unsafe_allow_html=True,
                )
                st.session_state.time_limit = t_fix
            else:
                tl_map = {"No limit":0,"30 min":30,"60 min":60,"90 min":90,"2 hr":120}
                tl_sel = st.selectbox("Time", list(tl_map.keys()), index=2,
                                      label_visibility="collapsed")
                st.session_state.time_limit = tl_map[tl_sel]

        st.markdown("---")
        n = min(st.session_state.q_count, len(avail))
        if n == 0:
            st.error("⚠️ No questions available for this competition yet.")
        else:
            st.info(f"📚 {len(avail)} questions available — exam will use {n}.")
            if st.button(f"🚀 Start Exam ({n} questions)", type="primary"):
                import random as _r, time as _t
                _r.shuffle(avail)
                st.session_state.exam_questions = avail[:n]
                st.session_state.exam_answers   = {}
                st.session_state.exam_current   = 0
                st.session_state.exam_start     = _t.time()
                st.session_state.exam_submitted = False
                st.session_state.exam_step      = "exam"
                st.rerun()

    elif step == "exam":
        _student_exam()
    elif step == "results":
        _student_results()


# ── Main routing ──────────────────────────────────────────────────────────────
# Check for direct competition link first (?exam=CODE)
try:
    _exam_param = st.query_params.get("exam", "")
except Exception:
    _exam_param = ""

if _exam_param:
    # Direct link mode — hide sidebar navigation, show competition directly
    st.markdown("""
    <style>
    section[data-testid="stSidebar"]{display:none!important;}
    .block-container{padding-top:1rem!important;}
    </style>
    """, unsafe_allow_html=True)
    page_student_direct(_exam_param.upper())
elif "Student" in page:
    page_student()
else:
    page_admin()

# ── Global footer ─────────────────────────────────────────────────────────────
st.markdown("""
<div style="margin-top:3rem;padding:1.25rem 0 .5rem;
            border-top:.5px solid #DDD8CC;text-align:center;
            font-size:12px;color:#AAA;line-height:1.8;">
  © 2024 <strong style="color:#8B6914;">Dr.Che · Math Mission Thailand</strong>
  &nbsp;·&nbsp; All rights reserved
  &nbsp;·&nbsp; MathComp Competition Exam Platform
</div>
""", unsafe_allow_html=True)
