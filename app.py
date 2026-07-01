"""
=============================================================================
AI Communication Platform - Twilio Phone Call & SMS Backend (Gloify Assistant)
=============================================================================
A FastAPI backend that enables natural-language voice conversations with an
AI assistant for Gloify. This system supports both inbound calls (answering
customers) and outbound calls (calling customers).

Architecture (all in one file):
  - Models      : Pydantic schemas for User, Log, and request payloads
  - Database    : Lightweight JSON file-based persistence (no SQL needed)
  - TwilioService: Wrapper around the Twilio REST client for calls & SMS
  - Orchestrator : Parses natural-language commands and dispatches actions
  - API Routes  : FastAPI endpoints exposed under /api
  - App entry   : FastAPI app creation, CORS, router registration, uvicorn

Environment variables required (put in a .env file):
  TWILIO_ACCOUNT_SID   - Your Twilio account SID
  TWILIO_AUTH_TOKEN    - Your Twilio auth token
  TWILIO_PHONE_NUMBER  - Your Twilio "from" phone number (E.164 format)
  PORT                 - (optional) HTTP port, default 8000
  HOST                 - (optional) bind host, default 0.0.0.0

Run:
  pip install fastapi uvicorn twilio python-dotenv pydantic
  python phonecall_twilio_app.py
=============================================================================
"""

# ---------------------------------------------------------------------------
# Standard library & third-party imports
# ---------------------------------------------------------------------------
import json
import os
import uuid
import asyncio
import base64
import audioop
from datetime import datetime
from typing import Any, Dict, List, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from openai import OpenAI
import websockets

# Load .env file values into os.environ before anything else
load_dotenv()


# =============================================================================
# SECTION 1 – PYDANTIC MODELS
# These define the shape of data flowing through the API and stored on disk.
# =============================================================================

class User(BaseModel):
    """A registered contact that can receive calls or SMS messages."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    username: str          # Human-readable name used in commands ("Call Alice")
    phone_number: str      # E.164 format, e.g. "+14155552671"


class UserCreate(BaseModel):
    """Payload for POST /api/users – only username + phone are required."""
    username: str
    phone_number: str


class UserUpdate(BaseModel):
    """Payload for PUT /api/users/{id} – all fields optional (partial update)."""
    username: Optional[str] = None
    phone_number: Optional[str] = None


class CommunicationLog(BaseModel):
    """A record of every call or SMS attempted by the platform."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.now)
    recipient_name: str    # Username of the target user
    recipient_phone: str   # Phone number called/texted
    action: str            # "call" or "sms"
    message: str           # The text spoken or sent
    status: str            # "success" or "failed"
    sid: Optional[str] = None  # Twilio message/call SID for tracking


class CommandRequest(BaseModel):
    """Payload for POST /api/process-command – the natural-language instruction."""
    command: str           # e.g. "Call Alice and tell her dinner is ready"


# =============================================================================
# SECTION 2 – JSON FILE DATABASE
# Stores Users and Logs as plain JSON files in a /data directory.
# Simple and portable – no database server required.
# =============================================================================

# Resolve paths relative to this script's location so the app can be run
# from any working directory.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(_BASE_DIR, "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
LOGS_FILE  = os.path.join(DATA_DIR, "logs.json")


def _ensure_data_files() -> None:
    """Create the data directory and empty JSON files if they don't exist yet."""
    os.makedirs(DATA_DIR, exist_ok=True)
    for path in (USERS_FILE, LOGS_FILE):
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump([], f)


def get_users() -> List[User]:
    """Read all users from disk and return them as a list of User objects."""
    _ensure_data_files()
    with open(USERS_FILE, "r") as f:
        return [User(**u) for u in json.load(f)]


def save_users(users: List[User]) -> None:
    """Persist the full list of User objects to disk (overwrites the file)."""
    _ensure_data_files()
    with open(USERS_FILE, "w") as f:
        json.dump([u.model_dump() for u in users], f, indent=4)


def get_user_by_name(username: str) -> Optional[User]:
    """Case-insensitive lookup; returns None if the username is not registered."""
    for user in get_users():
        if user.username.lower() == username.lower():
            return user
    return None


def add_log(log: CommunicationLog) -> None:
    """Append a single CommunicationLog entry to the logs file."""
    _ensure_data_files()
    with open(LOGS_FILE, "r") as f:
        logs = json.load(f)
    logs.append(log.model_dump(mode="json"))
    with open(LOGS_FILE, "w") as f:
        json.dump(logs, f, indent=4)


def get_logs() -> List[Dict[str, Any]]:
    """Return all communication logs as raw dicts (ready to serialize as JSON)."""
    _ensure_data_files()
    with open(LOGS_FILE, "r") as f:
        return json.load(f)


# =============================================================================
# SECTION 3 – AI & VOICE CHATBOT CONFIGURATION
# =============================================================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_ACCESS_TOKEN = os.getenv("GEMINI_ACCESS_TOKEN")
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
GEMINI_LIVE_WS_URL = os.getenv(
    "GEMINI_LIVE_WS_URL",
    "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent",
)
if not GEMINI_API_KEY and not GEMINI_ACCESS_TOKEN:
    print("WARNING: GEMINI_API_KEY or GEMINI_ACCESS_TOKEN not found in .env file. Realtime voice will fail without it.")

# Gemini Live voice configuration
# Gemini Live voices: Kore, Puck, Charon, Fenrir, Aoede, Leda, Orus, Zephyr
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Kore")

# Note: ElevenLabs TTS configuration and fallback logic have been completely removed.
# The app now exclusively uses Google Gemini's native low-latency audio stream,
# which is much more reliable and faster for real-time voice calls.

client = OpenAI(
    api_key=GEMINI_API_KEY,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)


COMPANY_CONTEXT = """ Vridhi Home Finance """

# --- Variables ---
customer_name = "Mr. Rajkumar Sharma"
loan_id = "HL7842"
emi_amount = "₹24,500"
due_date = "5th July"
bounce_fee = "₹750"

# --- 1. EMI Reminder ---
SYSTEM_PROMPT = f"""You are Vridhi Digital Mitra, a female AI voice assistant calling on behalf of Vridhi Home Finance.
 
YOUR ONLY PURPOSE: Remind {customer_name} about their upcoming EMI and confirm they will pay on time. Nothing else.

IMPORTANT - Start the conversation in Hinglish.

If the customer replies in another language (Hindi, Marathi, Tamil, Telugu, English, etc.), immediately continue the conversation in that language.

Always respond in the language most recently used by the customer unless they switch again.

GUARDRAIL: You do not answer general questions, give financial advice, discuss anything unrelated to this EMI reminder, or engage in casual conversation. If asked anything outside this scope, say: "Main sirf aapke EMI reminder ke liye call kar rahi hoon — iske alawa main help nahi kar sakti."
 
LANGUAGE: 
- Start in Hinglish (Hindi + English, Roman script).
- Detect the customer's language from their first reply and switch immediately.
- Supported: Hinglish, Hindi, English, Telugu, Tamil, Kannada, Marathi.
- Stay in their language for the full call.
- You are female — always use female grammar: IN ALL LANGUAGES
 
CALL FLOW:
1. Greet warmly, say you are Vridhi Digital Mitra calling from Vridhi Home Finance, confirm you are speaking with {customer_name}.
2. Inform: EMI of {emi_amount} on Loan ID {loan_id} is due on {due_date}.
3. Ask if they are aware and if payment is arranged.
4. If yes — confirm, thank them, close the call.
5. If no or unsure — ask the reason, say "noted,", ask if they would like a callback from the team, then close.
 
HARD RULES:
- Never quote any figure not listed above.
- Never ask for OTP, PIN, password, or card details.
- Never threaten or pressure.
- Keep every response short — this is a voice call.
"""

# # --- 2. EMI Bounced / Failed Payment ---
# emi_bounced_prompt = f"""You are Vridhi Digital Mitra, a digital assistant calling from Vridhi Home Finance because the customer's EMI payment failed.

# - Greet, identify yourself and Vridhi Home Finance, confirm you're speaking to {customer_name}.
# - Hinglish, Roman script, respectful "aap" — a bit more direct than a reminder call, never rude or threatening.
# - Inform them the EMI of {emi_amount} (Loan ID {loan_id}) due on {due_date} didn't go through, ask the reason (salary delay, bank issue, forgot, dispute, etc.), acknowledge it ("noted, samajh gaya"), then continue the conversation naturally from there.
# - Ask when they can pay, confirm the date back to them.
# - Mention the bounce charge of {bounce_fee} applies as per the loan agreement.
# - Never ask for OTP, PIN, or card details.
# - Close politely, thank them.
# """

# # --- 3. Basic Support ---
# basic_support_prompt = f"""You are Vridhi Digital Mitra, a digital assistant for Vridhi Home Finance handling a basic query.

# - Greet, identify yourself and Vridhi Home Finance, confirm you're speaking to {customer_name}.
# - Hinglish, Roman script, respectful "aap," helpful tone.
# - Can help with: EMI due date ({due_date}), how to pay, callback for a statement, logging a complaint or contact update against Loan ID {loan_id}.
# - Cannot help with: live balance/outstanding, restructuring, disputes, anything needing account access.
# - Never ask for OTP, PIN, or card details.
# - Close politely, thank them.
# """
# Simple in-memory storage to keep track of conversations during a call
call_history = {}


# =============================================================================
# SECTION 4 – TWILIO SERVICE
# Wraps the Twilio REST client to send SMS and trigger outbound phone calls.
# If credentials are missing, it returns mock SIDs so the app still runs in
# dev/test mode without a real Twilio account.
# =============================================================================

class TwilioService:
    """Handles all outbound communication via the Twilio API."""

    def __init__(self) -> None:
        self.account_sid  = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token   = os.getenv("TWILIO_AUTH_TOKEN")
        self.from_number  = os.getenv("TWILIO_PHONE_NUMBER")
        
        # BASE_URL is the public ngrok/pinggy address used to link Twilio to this script.
        self.base_url     = os.getenv("BASE_URL")

        if self.account_sid and self.auth_token:
            # Real Twilio client – will make actual API calls
            self.client = Client(self.account_sid, self.auth_token)
        else:
            # Fallback for local development without credentials
            self.client = None
            print("WARNING: Twilio credentials not set. Running in mock mode.")

    def send_sms(self, to_number: str, message: str) -> Optional[str]:
        """
        Send an SMS to *to_number* with *message* as the body.
        Returns the Twilio message SID on success, None on failure.
        Returns a fake SID when running in mock mode.
        """
        if not self.client:
            return "MOCK_SID_SMS"  # Mock mode: pretend it worked

        try:
            msg = self.client.messages.create(
                body=message,
                from_=self.from_number,
                to=to_number,
            )
            return msg.sid
        except Exception as e:
            print(f"Error sending SMS: {e}")
            return None

    def make_call(self, to_number: str, message: str) -> Optional[str]:
        """
        Trigger an outbound phone call to *to_number*.
        Instead of a static message, this connects the person who answers 
        directly to the AI voice logic via the /voice webhook.
        Returns the Twilio call SID on success, None on failure.
        """
        if not self.client:
            return "MOCK_SID_CALL"

        if not self.base_url:
            print("ERROR: BASE_URL not set in .env. Outbound AI calls will not work.")
            return None

        try:
            call = self.client.calls.create(
                url=f"{self.base_url}/voice",
                from_=self.from_number,
                to=to_number,
            )
            return call.sid
        except Exception as e:
            print(f"Error making call: {e}")
            return None


# Module-level singleton – imported and used by the Orchestrator
twilio_service = TwilioService()


# =============================================================================
# SECTION 4 – ORCHESTRATOR
# Parses a free-text command, identifies the target user and action type,
# then dispatches to the appropriate Twilio handler.
#
# Parsing strategy:
#   1. Scan the command for any registered username (case-insensitive).
#   2. If "message", "sms", or "text" is present → SMS; otherwise → call.
#   3. Pass the entire command as the spoken/sent message.
# =============================================================================

class Orchestrator:
    """Interprets natural-language commands and dispatches calls or SMS."""

    def __init__(self) -> None:
        # Map action names to their handler coroutines
        self.tools = {
            "call":   self._handle_call,
            "sms":    self._handle_sms,
            "status": self._handle_status,
        }

    async def process_command(self, command: str) -> Dict[str, Any]:
        """
        Entry point for the /api/process-command endpoint.
        Returns a dict with at least {"success": bool, "message": str}.
        """
        command_lower = command.lower()

        # Step 1 – Find a registered user mentioned in the command
        target_user = None
        for user in get_users():
            if user.username.lower() in command_lower:
                target_user = user
                break

        if not target_user:
            return {
                "success": False,
                "message": "Could not identify a registered recipient in your command.",
            }

        # Step 2 – Decide whether this is a call or an SMS
        action = "sms" if any(kw in command_lower for kw in ["message", "sms", "text"]) else "call"

        # Step 3 – Use the full command as the message content
        return await self.tools[action](target_user, command)

    # ------------------------------------------------------------------
    # Private action handlers
    # ------------------------------------------------------------------

    async def _handle_call(self, user: User, message: str) -> Dict[str, Any]:
        """Trigger an outbound call and log the result."""
        sid    = twilio_service.make_call(user.phone_number, message)
        status = "success" if sid else "failed"

        log = CommunicationLog(
            recipient_name=user.username,
            recipient_phone=user.phone_number,
            action="call",
            message=message,
            status=status,
            sid=sid,
        )
        add_log(log)

        return {
            "success": status == "success",
            "message": f"Call triggered for {user.username}.",
            "sid":     sid,
            "details": log.model_dump(mode="json"),
        }

    async def _handle_sms(self, user: User, message: str) -> Dict[str, Any]:
        """Send an SMS and log the result."""
        sid    = twilio_service.send_sms(user.phone_number, message)
        status = "success" if sid else "failed"

        log = CommunicationLog(
            recipient_name=user.username,
            recipient_phone=user.phone_number,
            action="sms",
            message=message,
            status=status,
            sid=sid,
        )
        add_log(log)

        return {
            "success": status == "success",
            "message": f"SMS sent to {user.username}.",
            "sid":     sid,
            "details": log.model_dump(mode="json"),
        }

    async def _handle_status(self, user: User, message: str) -> Dict[str, Any]:
        """Return basic registration info for the identified user (no Twilio call)."""
        return {
            "success": True,
            "message": f"User {user.username} is registered with number {user.phone_number}.",
        }


# Module-level singleton used by the API routes
orchestrator = Orchestrator()


# =============================================================================
# SECTION 5 – FASTAPI APPLICATION & ROUTES
# =============================================================================

app = FastAPI(title="AI Communication Platform")

# Allow all origins in development. In production, replace "*" with your
# frontend's actual domain (e.g. "https://myapp.example.com").
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Root health-check endpoint
# ------------------------------------------------------------------

@app.get("/")
async def root():
    """Simple liveness check – returns a welcome message."""
    return {"message": "Welcome to AI Communication Platform API"}


# ------------------------------------------------------------------
# User management endpoints  (prefix: /api/users)
# ------------------------------------------------------------------

@app.get("/api/users", response_model=List[User])
async def get_users_endpoint():
    """Return all registered users."""
    return get_users()


@app.post("/api/users", response_model=User)
async def create_user(user_in: UserCreate):
    """
    Register a new user.
    Returns 400 if the username is already taken (case-insensitive).
    """
    users = get_users()

    # Reject duplicate usernames to keep command parsing unambiguous
    if any(u.username.lower() == user_in.username.lower() for u in users):
        raise HTTPException(status_code=400, detail="Username already exists")

    new_user = User(username=user_in.username, phone_number=user_in.phone_number)
    users.append(new_user)
    save_users(users)
    return new_user


@app.put("/api/users/{user_id}", response_model=User)
async def update_user(user_id: str, user_in: UserUpdate):
    """
    Update an existing user's username and/or phone number.
    Returns 404 if the user_id is not found.
    """
    users = get_users()
    for i, u in enumerate(users):
        if u.id == user_id:
            updated_user = u.model_copy(update=user_in.model_dump(exclude_unset=True))
            users[i] = updated_user
            save_users(users)
            return updated_user
    raise HTTPException(status_code=404, detail="User not found")


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str):
    """Remove a user from the registry (does not delete their logs)."""
    users = get_users()
    save_users([u for u in users if u.id != user_id])
    return {"message": "User deleted"}


# ------------------------------------------------------------------
# Communication endpoints  (prefix: /api)
# ------------------------------------------------------------------

@app.post("/api/process-command")
async def process_command(request: CommandRequest):
    """
    Accept a natural-language command and execute the implied action.gs

    Examples:
      {"command": "Call Alice"}
        → triggers an outbound call to Alice's registered number

      {"command": "Text Bob: meeting at 3pm"}
        → sends an SMS to Bob

    Returns a result dict including success status, Twilio SID, and a log entry.
    """
    return await orchestrator.process_command(request.command)


@app.get("/api/logs")
async def get_logs_endpoint():
    """Return all communication logs (calls and SMS) in reverse-insertion order."""
    return get_logs()


# ------------------------------------------------------------------
# Voice Chatbot Endpoints (Inbound Calls)
# ------------------------------------------------------------------

@app.api_route("/voice", methods=["GET", "POST"])
async def voice_webhook(request: Request):
    """Connect the incoming call to the WebSocket Media Stream."""
    response = VoiceResponse()

    # Identify the host dynamically from the request or env
    host = os.getenv("BASE_URL", "").replace("https://", "").replace("http://", "")
    if not host:
        host = request.headers.get("host", "localhost:8000")

    print(f"📲 /voice hit — connecting media stream to wss://{host}/media-stream")

    # Say a brief greeting first so the caller knows the call connected
    response.say("Please wait while I connect you.", voice="Polly.Aditi")

    # Connect a BIDIRECTIONAL media stream
    connect = Connect()
    stream = Stream(url=f"wss://{host}/media-stream")
    stream.parameter(name="direction", value="both")
    connect.append(stream)
    response.append(connect)

    # Pause keeps the call alive while the stream is active
    response.pause(length=60)

    twiml_str = str(response)
    print(f"TwiML: {twiml_str}")
    return Response(content=twiml_str, media_type="application/xml")

@app.websocket("/media-stream")
async def media_stream_endpoint(websocket: WebSocket):
    """WebSocket endpoint to handle bidirectional audio streaming with Twilio."""
    await websocket.accept()
    print("📞 Incoming Twilio Media Stream Connected")

    stream_sid = None
    call_sid = None
    gemini_ws = None
    input_audio_state = None
    output_audio_state = None

    # Note: ElevenLabs variables (elevenlabs_ws, elevenlabs_receive_task, tts_text_buffer, etc.)
    # have been removed. Gemini's native audio is played directly without text buffering or fallback.

    def _gemini_ws_url() -> str:
        base = GEMINI_LIVE_WS_URL.rstrip("/")
        if GEMINI_ACCESS_TOKEN:
            return f"{base}?access_token={GEMINI_ACCESS_TOKEN}"
        return f"{base}?key={GEMINI_API_KEY}"

    async def send_audio_to_twilio(raw_ulaw: bytes):
        """Stream raw ulaw audio back to the caller via Twilio media events."""
        nonlocal stream_sid
        CHUNK = 640  # 640 bytes = 80ms of 8kHz mulaw (good balance)

        for i in range(0, len(raw_ulaw), CHUNK):
            chunk = raw_ulaw[i : i + CHUNK]
            media_event = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": base64.b64encode(chunk).decode("utf-8")},
            }
            try:
                await websocket.send_text(json.dumps(media_event))
            except Exception:
                break

    async def send_mark():
        nonlocal stream_sid
        if not stream_sid:
            return
        mark_event = {
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {"name": "playback_done"},
        }
        try:
            await websocket.send_text(json.dumps(mark_event))
        except Exception:
            pass

    async def connect_gemini():
        nonlocal gemini_ws
        if gemini_ws is not None:
            return

        url = _gemini_ws_url()
        try:
            import socket
            gemini_ws = await websockets.connect(url, family=socket.AF_INET)
            print(
                f"✅ Connected to Gemini Live | voice={GEMINI_VOICE}"
            )
            
            # Native audio Live models (e.g. gemini-3.1-flash-live-preview) only support
            # AUDIO modality.
            await gemini_ws.send(
                json.dumps(
                    {
                        "setup": {
                            "model": f"models/{GEMINI_LIVE_MODEL}",
                            "generation_config": {
                                "response_modalities": ["AUDIO"],
                                "speech_config": {
                                    "voice_config": {
                                        "prebuilt_voice_config": {
                                            "voice_name": GEMINI_VOICE,
                                        }
                                    }
                                },
                            },
                            "output_audio_transcription": {},
                            "system_instruction": {
                                "parts": [{"text": SYSTEM_PROMPT}]
                            },
                        }
                    }
                )
            )

            await gemini_ws.send(
                json.dumps(
                    {
                        "realtimeInput": {
                            "text": "Start the conversation by greeting the customer in English. After the customer responds, reply in whatever language they use, and continue using that language unless they switch again. "
                        }
                    }
                )
            )
        except Exception as e:
            print(f"❌ Failed to connect to Gemini Live websocket: {e}")
            await websocket.close()
            raise

    def append_transcription(buffer: str, chunk: str) -> str:
        """Append Gemini transcription chunk (handles delta or cumulative text)."""
        if not chunk:
            return buffer
        if buffer and chunk.startswith(buffer):
            return chunk
        if buffer and buffer.endswith(chunk):
            return buffer
        return buffer + chunk

    async def play_gemini_audio(inline_data: dict):
        """Stream Gemini native audio directly to Twilio (PCM → 8k μ-law)."""
        nonlocal output_audio_state
        audio_data_b64 = inline_data.get("data")
        if not audio_data_b64:
            return
        mime = inline_data.get("mimeType") or inline_data.get("mime_type") or ""
        sample_rate = 24000 if "24000" in mime else 16000
        try:
            audio_bytes = base64.b64decode(audio_data_b64)
            audio_8k, output_audio_state = audioop.ratecv(
                audio_bytes, 2, 1, sample_rate, 8000, output_audio_state,
            )
            ulaw_audio = audioop.lin2ulaw(audio_8k, 2)
            # Send the resampled audio directly to Twilio Media Stream
            await send_audio_to_twilio(ulaw_audio)
        except Exception as e:
            print(f"Gemini audio playback error: {e}")

    # Note: ElevenLabs TTS connection, streaming receiver, playback control,
    # and fallback helper functions have been fully deprecated and removed.
    # The agent now stream-decodes Gemini's native voice packets directly.

    async def receive_from_twilio():
        nonlocal stream_sid, call_sid,input_audio_state
        try:
            while True:
                message = await websocket.receive_text()
                data = json.loads(message)

                if data["event"] == "start":
                    stream_sid = data["start"]["streamSid"]
                    call_sid = data["start"]["callSid"]
                    print(f"▶️  Stream Started | SID: {stream_sid} | Call: {call_sid}")

                    if call_sid not in call_history:
                        call_history[call_sid] = [{"role": "system", "content": SYSTEM_PROMPT}]

                    await connect_gemini()

                elif data["event"] == "media":
                    payload = data["media"]["payload"]
                    pcm_mulaw = base64.b64decode(payload)
                    try:
                        pcm_linear_8k = audioop.ulaw2lin(pcm_mulaw, 2)
                        pcm_linear_16k, input_audio_state = audioop.ratecv(
                            pcm_linear_8k,
                            2,
                            1,
                            8000,
                            16000,
                            input_audio_state,
                        )
                        encoded_audio = base64.b64encode(pcm_linear_16k).decode("utf-8")
                    except Exception as e:
                        print(f"Audio conversion error: {e}")
                        continue

                    if gemini_ws is not None:
                        try:
                            await gemini_ws.send(
                                json.dumps(
                                    {
                                        "realtime_input": {
                                            "audio": {
                                                "data": encoded_audio,
                                                "mime_type": "audio/pcm;rate=16000",
                                            }
                                        }
                                    }
                                )
                            )
                        except Exception as e:
                            print(f"Gemini send error: {e}")

                elif data["event"] == "stop":
                    print("⏹️  Stream Stopped")
                    break

        except WebSocketDisconnect:
            print("📴 Twilio disconnected")
        except Exception as e:
            print(f"Twilio Receive Error: {e}")

    async def receive_from_gemini():
        """Stream Gemini native audio directly to the caller."""
        nonlocal gemini_ws
        try:
            while True:
                if gemini_ws is None:
                    await asyncio.sleep(0.1)
                    continue

                message = await gemini_ws.recv()
                msg = json.loads(message)

                if "serverContent" in msg:
                    server_content = msg["serverContent"]

                    if "outputTranscription" in server_content:
                        t = server_content["outputTranscription"].get("text", "")
                        if t:
                            print(f"🤖 Gemini (transcription): {t}")

                    if "modelTurn" in server_content and "parts" in server_content["modelTurn"]:
                        for part in server_content["modelTurn"]["parts"]:
                            text = part.get("text")
                            if text:
                                print(f"🤖 Gemini (text): {text}")
                            inline_data = part.get("inlineData")
                            if inline_data and inline_data.get("data"):
                                await play_gemini_audio(inline_data)

                    if server_content.get("turnComplete"):
                        print("✅ Gemini turn complete")
                        await send_mark()

                    if server_content.get("interrupted"):
                        print("🔇 Interrupted by caller")
                        if stream_sid:
                            try:
                                await websocket.send_text(json.dumps({
                                    "event": "clear",
                                    "streamSid": stream_sid,
                                }))
                            except Exception:
                                pass

                    if "inputTranscription" in server_content:
                        print(f"🗣️  Caller: {server_content['inputTranscription'].get('text', '')}")

                if "toolCall" in msg:
                    print(f"Gemini tool call event ignored: {msg.get('toolCall')}")

        except websockets.exceptions.ConnectionClosed:
            print("Gemini websocket closed")
        except Exception as e:
            print(f"Gemini Receive Error: {e}")

    task1 = asyncio.create_task(receive_from_twilio())
    task2 = asyncio.create_task(receive_from_gemini())

    await asyncio.gather(task1, task2)

    try:
        if gemini_ws is not None:
            await gemini_ws.close()
    except Exception:
        pass

    print("🔌 Media stream session ended")


# =============================================================================
# SECTION 6 – ENTRY POINT
# Run with:  python phonecall_twilio_app.py
# Or via uvicorn directly:  uvicorn phonecall_twilio_app:app --reload
# =============================================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run("app:app", host=host, port=port, reload=True)
    
