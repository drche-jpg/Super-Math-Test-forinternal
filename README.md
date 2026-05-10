# MathComp ✦ — Competition Exam Platform

แพลตฟอร์มสอบแข่งขันคณิตศาสตร์ออนไลน์ พัฒนาโดย Dr.Che / Math Mission Thailand

---

## โครงสร้างไฟล์

```
├── app.py                    ← แอปหลัก (ไฟล์เดียว)
├── requirements.txt          ← Python dependencies
├── Procfile                  ← สำหรับ Railway deployment
├── .gitignore
└── .streamlit/
    ├── config.toml           ← Streamlit theme & settings
    └── secrets.toml          ← 🔒 LOCAL เท่านั้น ห้าม commit
```

---

## วิธี Deploy บน Streamlit Cloud

### 1. Push ขึ้น GitHub
```bash
git add app.py requirements.txt .streamlit/config.toml .gitignore Procfile README.md
git commit -m "Phase 0: model switch, secrets, anti-cheat, class PIN"
git push origin main
```

> ⚠️ อย่า push `.streamlit/secrets.toml` — อยู่ใน .gitignore แล้ว

### 2. ตั้งค่า Secrets บน Streamlit Cloud
ไปที่ [share.streamlit.io](https://share.streamlit.io) → App → ⋮ → Settings → Secrets

ใส่ค่าเหล่านี้ (แทนที่ด้วยค่าจริง):

```toml
ANTHROPIC_API_KEY  = "sk-ant-api03-..."
ADMIN_EMAIL        = "your@email.com"
ADMIN_PASSWORD     = "sha256-hash-of-your-password"

FIREBASE_PROJECT_ID    = "your-project-id"
FIREBASE_CLIENT_EMAIL  = "firebase-adminsdk-...@....iam.gserviceaccount.com"
FIREBASE_PRIVATE_KEY   = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
```

### 3. ตั้งค่า Admin Password ครั้งแรก
```bash
python3 -c "import hashlib; print(hashlib.sha256(b'YOUR_PASSWORD_HERE').hexdigest())"
```
นำ hash ที่ได้ใส่เป็นค่า `ADMIN_PASSWORD` ใน Secrets

---

## วิธี Deploy บน Railway (แนะนำสำหรับจัดสอบจริง)

Railway ไม่มี cold start — เหมาะกับนักเรียนหลายคนเข้าพร้อมกัน

1. ไปที่ [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
2. เลือก repo นี้
3. ตั้ง **Start Command**: `streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true`
4. ไปที่ **Variables** tab → เพิ่มทุก key จาก secrets.toml (ไม่ใส่ quotes)
5. Deploy

ราคา ~$5/เดือน

---

## Class PIN — วิธีใช้งาน

1. Admin → Settings → Class PIN Manager → กด "Generate PIN"
2. แชร์ PIN ให้นักเรียนก่อนสอบ
3. นักเรียนใส่ PIN ในหน้า "Tell us about yourself"
4. Admin → Student Records → filter ด้วย Class PIN เพื่อดูผลเฉพาะกลุ่ม

---

## Anti-Cheat Features (Phase 0)

- Tab switch detection + warning banner
- Window focus loss detection
- Right-click disabled during exam
- Copy/paste shortcuts blocked (Ctrl+C, Ctrl+V, Ctrl+U)
- F12 (DevTools) blocked

---

## AI Model

ใช้ `claude-sonnet-4-6` — ประหยัด ~80% เทียบกับ Opus โดยคุณภาพใกล้เคียงกัน
