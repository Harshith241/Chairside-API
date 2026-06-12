import os
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import httpx
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Chairside Booking API")

# --- Config (defensive) ---
def env(key: str, default=None, required=False):
    val = os.environ.get(key, default)
    if required and not val:
        print(f"WARNING: env var {key} is missing")
    return val

CAL_API_KEY = env("CAL_API_KEY", required=True)
CAL_EVENT_TYPE_ID_RAW = env("CAL_EVENT_TYPE_ID", default="0")
CAL_EVENT_TYPE_ID = int(CAL_EVENT_TYPE_ID_RAW) if CAL_EVENT_TYPE_ID_RAW.isdigit() else 0
CAL_BASE = "https://api.cal.com/v2"

CAL_HEADERS_SLOTS = {
    "Authorization": f"Bearer {CAL_API_KEY}",
    "cal-api-version": "2024-09-04",
    "Content-Type": "application/json",
}
CAL_HEADERS_BOOKINGS = {
    "Authorization": f"Bearer {CAL_API_KEY}",
    "cal-api-version": "2024-08-13",
    "Content-Type": "application/json",
}

SUPABASE_URL = env("SUPABASE_URL", required=True)
SUPABASE_SERVICE_KEY = env("SUPABASE_SERVICE_KEY", required=True)
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY) if SUPABASE_URL and SUPABASE_SERVICE_KEY else None

TWILIO_SID = env("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = env("TWILIO_AUTH_TOKEN")
TWILIO_FROM = env("TWILIO_FROM_NUMBER")


# --- Helpers ---
def get_practice(practice_id: str):
    """Look up a practice. For v1, fall back to name match if not a UUID."""
    try:
        res = supabase.table("practices").select("*").eq("id", practice_id).execute()
        if res.data:
            return res.data[0]
    except Exception:
        pass  # not a valid UUID, fall through to name match

    # Fallback: match by name (case-insensitive)
    res = supabase.table("practices").select("*").ilike("name", f"%{practice_id}%").execute()
    return res.data[0] if res.data else None


async def send_sms(to: str, body: str):
    """Send an SMS via Twilio REST API."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    async with httpx.AsyncClient() as client:
        await client.post(
            url,
            data={"To": to, "From": TWILIO_FROM, "Body": body},
            auth=(TWILIO_SID, TWILIO_TOKEN),
        )


# --- Models ---
class AvailabilityRequest(BaseModel):
    practice_id: str
    preference: str
    days_ahead: Optional[int] = 14


class BookRequest(BaseModel):
    practice_id: str
    slot_iso: str
    patient_name: str
    patient_phone: str
    service: str
    is_new_patient: Optional[bool] = True


class HandoffRequest(BaseModel):
    practice_id: str
    patient_name: Optional[str] = "Unknown"
    patient_phone: str
    topic: str
    is_urgent: Optional[bool] = False

class RetellFunctionCall(BaseModel):
    call: Optional[dict] = None
    name: Optional[str] = None
    args: dict


# --- Endpoints ---

def _format_slots_for_speech(slots):
    """Turn ISO timestamps into natural language for the voice agent."""
    if not slots:
        return "I don't have any open slots in that range."
    out = []
    for iso in slots:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        out.append(dt.strftime("%A, %B %-d at %-I:%M %p"))
    return "; ".join(out)



@app.post("/api/availability")
async def availability(req: RetellFunctionCall):
    practice_id = req.args.get("practice_id", "sunset")
    preference = req.args.get("preference", "any time")
    days_ahead = req.args.get("days_ahead", 14)

    start = datetime.now(timezone.utc)
    end = start + timedelta(days=days_ahead)

    params = {
        "eventTypeId": CAL_EVENT_TYPE_ID,
        "start": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{CAL_BASE}/slots", headers=CAL_HEADERS_SLOTS, params=params)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Cal.com error: {r.text}")

    data = r.json()
    slots_by_date = data.get("data", {})
    flat = []
    for date, slots in slots_by_date.items():
        for s in slots:
            flat.append(s.get("start"))

    top3 = flat[:3]

    return {
        "available": len(top3) > 0,
        "slots": top3,
        "spoken": _format_slots_for_speech(top3),
    }


@app.post("/api/book")
async def book(req: RetellFunctionCall):
    practice_id = req.args.get("practice_id", "sunset")
    slot_iso = req.args.get("slot_iso")
    patient_name = req.args.get("patient_name", "Patient")
    patient_phone = req.args.get("patient_phone", "")
    service = req.args.get("service", "appointment")
    is_new_patient = req.args.get("is_new_patient", True)

    practice = get_practice(practice_id)

    payload = {
        "eventTypeId": CAL_EVENT_TYPE_ID,
        "start": slot_iso,
        "attendee": {
            "name": patient_name,
            "email": f"{patient_phone.replace('+', '')}@chairside.ai",
            "phoneNumber": patient_phone,
            "timeZone": "America/Phoenix",
        },
        "metadata": {"service": service, "new_patient": str(is_new_patient)},
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(f"{CAL_BASE}/bookings", headers=CAL_HEADERS_BOOKINGS, json=payload)

    if r.status_code not in (200, 201):
        print(f"Cal.com booking failed: {r.status_code} - {r.text}")
        return {
            "success": False,
            "reason": "slot_unavailable",
            "message": "That slot is no longer available. Please offer fresh times.",
        }

    booking = r.json().get("data", {})
    cal_booking_id = booking.get("uid") or booking.get("id")

    if practice:
        supabase.table("bookings").insert({
            "practice_id": practice["id"],
            "cal_booking_id": str(cal_booking_id),
            "patient_name": patient_name,
            "patient_phone": patient_phone,
            "appointment_datetime": slot_iso,
            "service": service,
            "confirmation_sms_sent": True,
        }).execute()

    dt = datetime.fromisoformat(slot_iso.replace("Z", "+00:00"))
    when = dt.strftime("%A, %B %-d at %-I:%M %p")
    pname = practice["name"] if practice else "our office"
    await send_sms(
        patient_phone,
        f"You're booked at {pname} for {when}. Reply STOP to opt out.",
    )

    return {"success": True, "booking_id": str(cal_booking_id), "when": when}


@app.post("/api/handoff")
async def handoff(req: RetellFunctionCall):
    practice_id = req.args.get("practice_id", "sunset")
    patient_name = req.args.get("patient_name", "Unknown")
    patient_phone = req.args.get("patient_phone", "")
    topic = req.args.get("topic", "general inquiry")
    is_urgent = req.args.get("is_urgent", False)

    practice = get_practice(practice_id)

    if practice:
        supabase.table("handoffs").insert({
            "practice_id": practice["id"],
            "patient_name": patient_name,
            "patient_phone": patient_phone,
            "topic": topic,
            "owner_notified": True,
        }).execute()

        urgency = "URGENT — " if is_urgent else ""
        await send_sms(
            practice["owner_sms"],
            f"{urgency}Caller {patient_name} ({patient_phone}) asked about: {topic}. Please call back.",
        )

    return {"success": True, "message": "The team will call you back shortly."}