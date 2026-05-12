import os
import json
import re
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
 
# ===== ตั้งค่า =====
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
DATA_FILE = "meetings.json"
REMIND_BEFORE_MIN = 30  # แจ้งเตือนล่วงหน้ากี่นาที
 
app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
 
# ===== จัดการไฟล์ JSON =====
def load_meetings():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []
 
def save_meetings(meetings):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(meetings, f, ensure_ascii=False, indent=2)
 
# ===== แปลงข้อความเป็นนัดหมาย =====
THAI_MONTHS = {
    "ม.ค.": 1, "มกราคม": 1, "มค": 1,
    "ก.พ.": 2, "กุมภาพันธ์": 2, "กพ": 2,
    "มี.ค.": 3, "มีนาคม": 3, "มีค": 3,
    "เม.ย.": 4, "เมษายน": 4, "เมย": 4,
    "พ.ค.": 5, "พฤษภาคม": 5, "พค": 5,
    "มิ.ย.": 6, "มิถุนายน": 6, "มิย": 6,
    "ก.ค.": 7, "กรกฎาคม": 7, "กค": 7,
    "ส.ค.": 8, "สิงหาคม": 8, "สค": 8,
    "ก.ย.": 9, "กันยายน": 9, "กย": 9,
    "ต.ค.": 10, "ตุลาคม": 10, "ตค": 10,
    "พ.ย.": 11, "พฤศจิกายน": 11, "พย": 11,
    "ธ.ค.": 12, "ธันวาคม": 12, "ธค": 12,
}
 
def parse_meeting(text):
    """แปลงข้อความเช่น 'นัดประชุม Sprint 3 พค 10:00 ห้อง A' เป็น dict"""
    # หาเวลา HH:MM หรือ HH.MM (รับทั้ง : และ .)
    time_match = re.search(r"(\d{1,2})[:.](\d{2})", text)
    if not time_match:
        return None, "ไม่พบเวลา (เช่น 10:00 หรือ 10.00)"
    hour = int(time_match.group(1))
    minute = int(time_match.group(2))
 
    # หาวันที่
    day = None
    month = None
    year = datetime.now().year
 
    # ลองหารูปแบบ "3 พค" หรือ "3 พ.ค."
    for m_text, m_num in THAI_MONTHS.items():
        pattern = r"(\d{1,2})\s*" + re.escape(m_text)
        m = re.search(pattern, text)
        if m:
            day = int(m.group(1))
            month = m_num
            break
 
    # ถ้าไม่เจอ ลอง "3/5" หรือ "3-5"
    if day is None:
        date_match = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", text)
        if date_match:
            day = int(date_match.group(1))
            month = int(date_match.group(2))
            if date_match.group(3):
                y = int(date_match.group(3))
                year = y + 2000 if y < 100 else y
 
    if day is None or month is None:
        return None, "ไม่พบวันที่ (เช่น 3 พค หรือ 3/5)"
 
    # สร้าง datetime
    try:
        dt = datetime(year, month, day, hour, minute)
        # ถ้าวันที่ผ่านไปแล้วในปีนี้ ให้เป็นปีหน้า
        if dt < datetime.now():
            dt = dt.replace(year=year + 1)
    except ValueError as e:
        return None, f"วันที่ไม่ถูกต้อง: {e}"
 
    # หาห้อง (คำหลัง "ห้อง")
    room_match = re.search(r"ห้อง\s*(\S+)", text)
    room = room_match.group(1) if room_match else "-"
 
    # ลบคำว่า "นัดประชุม" และวันเวลาออก เอาที่เหลือเป็นเรื่อง
    topic = text
    topic = re.sub(r"^(นัด|นัดประชุม|ประชุม|เพิ่ม)", "", topic).strip()
    if time_match:
        topic = topic.replace(time_match.group(0), "")
    for m_text in THAI_MONTHS:
        topic = re.sub(r"\d{1,2}\s*" + re.escape(m_text), "", topic)
    topic = re.sub(r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?", "", topic)
    topic = re.sub(r"ห้อง\s*\S+", "", topic)
    topic = re.sub(r"\s+", " ", topic).strip()
    if not topic:
        topic = "ประชุม"
 
    return {
        "topic": topic,
        "datetime": dt.strftime("%Y-%m-%d %H:%M"),
        "room": room,
        "reminded": False,
    }, None
 
# ===== ตัวจับเวลาแจ้งเตือน =====
# เก็บ user_id ของคนใช้บอท (จะถูกอัปเดตเมื่อมีคนส่งข้อความเข้ามา)
def add_subscriber(user_id):
    subs_file = "subscribers.json"
    subs = []
    if os.path.exists(subs_file):
        try:
            with open(subs_file, "r", encoding="utf-8") as f:
                subs = json.load(f)
        except Exception:
            subs = []
    if user_id not in subs:
        subs.append(user_id)
        with open(subs_file, "w", encoding="utf-8") as f:
            json.dump(subs, f)
 
def get_subscribers():
    if not os.path.exists("subscribers.json"):
        return []
    try:
        with open("subscribers.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []
 
def reminder_loop():
    """เช็คทุก 1 นาทีว่ามีนัดที่ใกล้ถึงเวลาไหม"""
    while True:
        try:
            meetings = load_meetings()
            now = datetime.now()
            changed = False
            for m in meetings:
                if m.get("reminded"):
                    continue
                dt = datetime.strptime(m["datetime"], "%Y-%m-%d %H:%M")
                diff = (dt - now).total_seconds() / 60
                # ถ้าใกล้ถึงในระยะ 30 นาที (แต่ยังไม่ถึงเวลา)
                if 0 < diff <= REMIND_BEFORE_MIN:
                    msg = (
                        f"⏰ ใกล้ถึงเวลาประชุมแล้ว!\n"
                        f"📌 เรื่อง: {m['topic']}\n"
                        f"🕐 เวลา: {m['datetime']}\n"
                        f"📍 ห้อง: {m['room']}\n"
                        f"(อีก {int(diff)} นาที)"
                    )
                    for uid in get_subscribers():
                        try:
                            line_bot_api.push_message(uid, TextSendMessage(text=msg))
                        except Exception as e:
                            print(f"ส่งข้อความไม่สำเร็จ: {e}")
                    m["reminded"] = True
                    changed = True
            if changed:
                save_meetings(meetings)
        except Exception as e:
            print(f"reminder_loop error: {e}")
        time.sleep(60)
 
# ===== Webhook =====
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"
 
@app.route("/")
def index():
    return "Bot is running!"
 
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    add_subscriber(user_id)
    text = event.message.text.strip()
 
    # คำสั่งต่างๆ
    if text in ["ดู", "รายการ", "list", "นัดหมาย"]:
        meetings = load_meetings()
        upcoming = [m for m in meetings
                    if datetime.strptime(m["datetime"], "%Y-%m-%d %H:%M") >= datetime.now()]
        upcoming.sort(key=lambda x: x["datetime"])
        if not upcoming:
            reply = "ยังไม่มีนัดหมายที่กำลังจะมาถึง"
        else:
            lines = ["📋 รายการนัดหมาย:"]
            for i, m in enumerate(upcoming, 1):
                lines.append(f"{i}. {m['topic']} | {m['datetime']} | ห้อง {m['room']}")
            reply = "\n".join(lines)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return
 
    if text.startswith("ลบ"):
        # ลบนัด: "ลบ 1"
        num_match = re.search(r"\d+", text)
        if not num_match:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="พิมพ์ 'ลบ 1' (เลขลำดับ)"))
            return
        idx = int(num_match.group(0)) - 1
        meetings = load_meetings()
        upcoming = [m for m in meetings
                    if datetime.strptime(m["datetime"], "%Y-%m-%d %H:%M") >= datetime.now()]
        upcoming.sort(key=lambda x: x["datetime"])
        if 0 <= idx < len(upcoming):
            target = upcoming[idx]
            meetings.remove(target)
            save_meetings(meetings)
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(text=f"ลบแล้ว: {target['topic']}"))
        else:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(text="ไม่พบลำดับนี้"))
        return
 
    if text in ["ช่วย", "help", "วิธีใช้"]:
        help_text = (
            "📖 วิธีใช้บอท:\n"
            "• เพิ่มนัด: นัดประชุม Sprint 3 พค 10:00 ห้อง A\n"
            "• ดูรายการ: พิมพ์ 'ดู'\n"
            "• ลบนัด: พิมพ์ 'ลบ 1' (เลขลำดับจากคำสั่งดู)\n"
            "• วิธีใช้: พิมพ์ 'ช่วย'\n\n"
            "บอทจะเตือนล่วงหน้า 30 นาทีก่อนถึงเวลา"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))
        return
 
    # ลองแปลงเป็นนัด
    meeting, err = parse_meeting(text)
    if meeting:
        meetings = load_meetings()
        meetings.append(meeting)
        save_meetings(meetings)
        reply = (
            f"✅ บันทึกแล้ว!\n"
            f"📌 {meeting['topic']}\n"
            f"🕐 {meeting['datetime']}\n"
            f"📍 ห้อง {meeting['room']}\n"
            f"จะเตือนล่วงหน้า {REMIND_BEFORE_MIN} นาที"
        )
    else:
        reply = (
            f"❓ ไม่เข้าใจคำสั่ง ({err})\n\n"
            "ตัวอย่าง: นัดประชุม Sprint 3 พค 10:00 ห้อง A\n"
            "พิมพ์ 'ช่วย' เพื่อดูวิธีใช้"
        )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
 
# ===== เริ่มทำงาน =====
if __name__ == "__main__":
    # เริ่มตัวจับเวลาในเธรดแยก
    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    # ถูกรันโดย gunicorn
    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()
