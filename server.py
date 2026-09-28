"""Adhan Connect — Backend API (FastAPI, Module 3 enriched).

Routers:
- /api/auth/*           — register, login, refresh, logout, forgot/reset password, verify-phone (mocked)
- /api/users/*          — me (GET/PUT), stats, family, children, subscription
- /api/mosques/*        — list, detail, follow, live-status, schedule, events
- /api/baf/*            — devices CRUD, pair, volume, mode, schedule
- /api/content/*        — coran, stories, recipes, arabic
- /api/subscriptions/*  — Stripe Checkout + Portal + cancel + current
- /api/webhooks/stripe  — Stripe webhook (idempotent)
- /api/admin/mosque/*   — stats, audience, revenue, content, events, live start/stop
- /api/admin/super/*    — overview, users, mosques, baf health, revenue, alerts
- /ws/live/{mosque_id}  — simulated live audio broadcast (looping MP3)

Bcrypt + JWT + Stripe + native FastAPI WebSocket. MongoDB via Motor.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import bcrypt
import httpx
import jwt
import stripe
from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field
from starlette.middleware.cors import CORSMiddleware

from quran_data import SURAHS_114, RECITERS, get_audio_url
from cards_pdf import make_single_pdf, make_full_alphabet_pdf, LETTERS_RICH
from fastapi.responses import Response  # noqa: E402  (Response for PDF stream)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# ─── Config ──────────────────────────────────────────────────────────────
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
JWT_SECRET = os.environ["JWT_SECRET_KEY"]
JWT_ALG = os.environ.get("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MIN = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "10080"))
REFRESH_EXPIRE_DAYS = int(os.environ.get("REFRESH_TOKEN_EXPIRE_DAYS", "30"))
STRIPE_KEY = os.environ.get("STRIPE_API_KEY", "")
STRIPE_WH_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_DOMAIN = os.environ.get("APP_DOMAIN", "")
MOBILE_SCHEME = os.environ.get("MOBILE_SCHEME", "adhanconnect")
ADHAN_STREAM_URL = os.environ.get(
    "ADHAN_STREAM_URL", "https://www.islamcan.com/audio/adhan/azan2.mp3"
)

stripe.api_key = STRIPE_KEY

# ─── App ────────────────────────────────────────────────────────────────
client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

app = FastAPI(title="Adhan Connect API")
api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("adhan")

# ─── Models ─────────────────────────────────────────────────────────────
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)
    first_name: str = Field(..., min_length=1, max_length=80)
    phone: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserPublic(BaseModel):
    id: str
    email: EmailStr
    first_name: str
    phone: Optional[str] = None
    subscription: Dict[str, Any] = Field(default_factory=lambda: {"plan": None, "status": None})
    streak_days: int = 0
    last_checkin: Optional[str] = None
    favorite_mosque_id: Optional[str] = None
    family_id: Optional[str] = None
    avatar: Optional[str] = None
    role: str = "user"


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserPublic


class RefreshBody(BaseModel):
    refresh_token: str


class UpdateMeBody(BaseModel):
    first_name: Optional[str] = None
    avatar: Optional[str] = None
    preferences: Optional[Dict[str, Any]] = None


class FamilyBody(BaseModel):
    name: Optional[str] = None
    members: Optional[List[Dict[str, Any]]] = None


class ChildBody(BaseModel):
    first_name: str
    age: int
    arabic_level: str = "Débutant"


class VerifyPhoneBody(BaseModel):
    phone: str
    code: str


class ForgotPasswordBody(BaseModel):
    email: EmailStr


class ResetPasswordBody(BaseModel):
    token: str
    new_password: str = Field(..., min_length=6)


class Mosque(BaseModel):
    id: str
    name: str
    city: str
    country: str
    image: str
    is_live: bool = False
    subscribers: int = 0
    imam: str
    description: str
    latitude: float
    longitude: float
    distance_km: float = 0.0


class Surah(BaseModel):
    id: str
    number: int
    name_ar: str
    name_translit: str
    name_fr: str
    verses: int
    audio_url: str
    reciter: str
    cover: str


class Recipe(BaseModel):
    id: str
    title: str
    category: str
    image: str
    prep_time: str
    blessed_ingredient: str
    hadith: str
    ingredients: List[str]
    steps: List[str]


class ProphetChapter(BaseModel):
    title: str
    illustration: str  # emoji ou URL
    text: str


class QuizQuestion(BaseModel):
    question: str
    choices: List[str]
    correct: int
    explanation: str


class Prophet(BaseModel):
    id: str
    name: str
    cover: str
    age_range: str
    duration: str
    is_new: bool
    audio_url: Optional[str] = ""
    summary: str
    # Champs étendus (Ulul Azm)
    name_ar: Optional[str] = ""
    name_en: Optional[str] = ""
    order: Optional[int] = None
    period: Optional[str] = ""
    quran_mentions: Optional[str] = ""
    chapters: Optional[List[ProphetChapter]] = None
    quiz: Optional[List[QuizQuestion]] = None
    quran_references: Optional[List[str]] = None
    sources: Optional[List[str]] = None


class PrayerTime(BaseModel):
    name: str
    time: str


class Product(BaseModel):
    id: str
    name: str
    price: str
    image: str
    context: str
    recommendation: str


class BAFDevice(BaseModel):
    id: str
    device_id: str
    name: str
    type: str = "main"
    online: bool = False
    firmware: str = "1.2.4"
    volume: int = 60
    mode: str = "live"
    last_seen: Optional[str] = None
    linked_mosque_id: Optional[str] = None


class PairBAFBody(BaseModel):
    qr_code: str
    name: Optional[str] = "BAF Salon"


class BAFVolumeBody(BaseModel):
    volume: int = Field(..., ge=0, le=100)


class BAFModeBody(BaseModel):
    mode: str  # live | story | coran | off | night


class BAFScheduleBody(BaseModel):
    type: str  # story | coran | adhan
    time: str  # HH:MM
    content_id: Optional[str] = None


class SubscriptionCreateBody(BaseModel):
    plan: str  # PREMIUM | FAMILLE | BAF_BUNDLE | BAF_FAMILLE


# ─── Auth helpers ───────────────────────────────────────────────────────
def hash_pw(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


def verify_pw(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except Exception:
        return False


def make_access_token(uid: str, role: str = "user") -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MIN)
    return jwt.encode({"sub": uid, "role": role, "exp": exp, "type": "access"}, JWT_SECRET, algorithm=JWT_ALG)


def make_refresh_token(uid: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(days=REFRESH_EXPIRE_DAYS)
    jti = secrets.token_urlsafe(16)
    return jwt.encode(
        {"sub": uid, "exp": exp, "type": "refresh", "jti": jti},
        JWT_SECRET, algorithm=JWT_ALG,
    )


def user_public(doc: dict) -> UserPublic:
    sub = doc.get("subscription")
    if isinstance(sub, str):
        sub = {"plan": sub, "status": "active", "stripe_id": None, "expires_at": None}
    elif not isinstance(sub, dict):
        sub = {"plan": None, "status": None}
    return UserPublic(
        id=doc["id"],
        email=doc["email"],
        first_name=doc["first_name"],
        phone=doc.get("phone"),
        subscription=sub,
        streak_days=doc.get("streak_days", 0),
        last_checkin=doc.get("last_checkin"),
        favorite_mosque_id=doc.get("favorite_mosque_id"),
        family_id=doc.get("family_id"),
        avatar=doc.get("avatar"),
        role=doc.get("role", "user"),
    )


async def get_current_user(authorization: Optional[str] = Header(default=None)) -> UserPublic:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        if payload.get("type") != "access":
            raise HTTPException(401, "Invalid token type")
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid token")
    doc = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
    if not doc:
        raise HTTPException(401, "User not found")
    return user_public(doc)


async def require_admin(user: UserPublic = Depends(get_current_user)) -> UserPublic:
    if user.role not in ("admin", "super_admin", "mosque_admin"):
        raise HTTPException(403, "Admin access required")
    return user


# =========================================================================
# AUTH
# =========================================================================
@api.post("/auth/register", response_model=TokenPair)
async def register(body: UserCreate):
    if await db.users.find_one({"email": body.email.lower()}):
        raise HTTPException(400, "Email déjà utilisé")
    uid = str(uuid.uuid4())
    today = datetime.now(timezone.utc).date().isoformat()
    doc = {
        "id": uid,
        "email": body.email.lower(),
        "phone": body.phone,
        "first_name": body.first_name.strip(),
        "hashed_password": hash_pw(body.password),
        "subscription": {"plan": None, "status": None, "stripe_id": None, "expires_at": None},
        "streak_days": 1,
        "last_checkin": today,
        "favorite_mosque_id": None,
        "family_id": None,
        "avatar": None,
        "preferences": {"language": "FR", "prayer_method": 2, "night_mode": False},
        "role": "user",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(doc)
    return TokenPair(
        access_token=make_access_token(uid),
        refresh_token=make_refresh_token(uid),
        user=user_public(doc),
    )


@api.post("/auth/login", response_model=TokenPair)
async def login(body: UserLogin):
    doc = await db.users.find_one({"email": body.email.lower()}, {"_id": 0})
    if not doc or not verify_pw(body.password, doc["hashed_password"]):
        raise HTTPException(401, "Email ou mot de passe incorrect")
    return TokenPair(
        access_token=make_access_token(doc["id"], doc.get("role", "user")),
        refresh_token=make_refresh_token(doc["id"]),
        user=user_public(doc),
    )


@api.post("/auth/refresh")
async def refresh(body: RefreshBody):
    try:
        payload = jwt.decode(body.refresh_token, JWT_SECRET, algorithms=[JWT_ALG])
        if payload.get("type") != "refresh":
            raise HTTPException(401, "Invalid token type")
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid refresh token")
    # Check revocation
    if await db.revoked_tokens.find_one({"jti": payload.get("jti")}):
        raise HTTPException(401, "Refresh token revoked")
    return {
        "access_token": make_access_token(payload["sub"]),
        "refresh_token": make_refresh_token(payload["sub"]),
        "token_type": "bearer",
    }


@api.post("/auth/logout")
async def logout(body: RefreshBody):
    try:
        payload = jwt.decode(body.refresh_token, JWT_SECRET, algorithms=[JWT_ALG], options={"verify_exp": False})
        if jti := payload.get("jti"):
            await db.revoked_tokens.update_one(
                {"jti": jti},
                {"$set": {"jti": jti, "revoked_at": datetime.now(timezone.utc).isoformat()}},
                upsert=True,
            )
    except jwt.PyJWTError:
        pass
    return {"ok": True}


@api.post("/auth/verify-phone")
async def verify_phone(body: VerifyPhoneBody):
    """MOCKED — Twilio integration deferred. Accepts code '0000' for demo."""
    if body.code == "0000":
        return {"ok": True, "verified": True, "mocked": True}
    raise HTTPException(400, "Code invalide (demo: utilisez 0000)")


@api.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordBody):
    """MOCKED — SendGrid/Resend deferred. Returns a reset token directly for testing."""
    doc = await db.users.find_one({"email": body.email.lower()})
    if not doc:
        return {"ok": True, "mocked": True}  # don't reveal existence
    token = secrets.token_urlsafe(24)
    await db.reset_tokens.update_one(
        {"user_id": doc["id"]},
        {"$set": {"user_id": doc["id"], "token": token, "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}},
        upsert=True,
    )
    return {"ok": True, "reset_token": token, "mocked": True}


@api.post("/auth/reset-password")
async def reset_password(body: ResetPasswordBody):
    doc = await db.reset_tokens.find_one({"token": body.token})
    if not doc:
        raise HTTPException(400, "Token invalide")
    expires = datetime.fromisoformat(doc["expires_at"])
    if expires < datetime.now(timezone.utc):
        raise HTTPException(400, "Token expiré")
    await db.users.update_one(
        {"id": doc["user_id"]},
        {"$set": {"hashed_password": hash_pw(body.new_password)}},
    )
    await db.reset_tokens.delete_one({"token": body.token})
    return {"ok": True}


@api.get("/auth/me", response_model=UserPublic)
async def auth_me(u: UserPublic = Depends(get_current_user)):
    return u


# =========================================================================
# USERS
# =========================================================================
@api.get("/users/me", response_model=UserPublic)
async def users_me(u: UserPublic = Depends(get_current_user)):
    return u


@api.put("/users/me", response_model=UserPublic)
async def update_me(body: UpdateMeBody, u: UserPublic = Depends(get_current_user)):
    update: Dict[str, Any] = {}
    if body.first_name is not None:
        update["first_name"] = body.first_name.strip()
    if body.avatar is not None:
        update["avatar"] = body.avatar
    if body.preferences is not None:
        update["preferences"] = body.preferences
    if update:
        await db.users.update_one({"id": u.id}, {"$set": update})
    doc = await db.users.find_one({"id": u.id}, {"_id": 0})
    return user_public(doc)


class StatsResponse(BaseModel):
    streak_days: int
    prayers_this_month: int
    prayers_target: int
    surahs_listened: int
    stories_listened: int
    level_badge: str


@api.get("/users/me/stats", response_model=StatsResponse)
@api.get("/stats", response_model=StatsResponse)  # legacy alias
async def my_stats(u: UserPublic = Depends(get_current_user)):
    return StatsResponse(
        streak_days=u.streak_days, prayers_this_month=87, prayers_target=150,
        surahs_listened=14, stories_listened=23, level_badge="Fidèle du Fajr ✦",
    )


@api.post("/users/me/family")
async def create_family(body: FamilyBody, u: UserPublic = Depends(get_current_user)):
    fid = str(uuid.uuid4())
    await db.families.insert_one({
        "id": fid, "name": body.name or f"Famille {u.first_name}",
        "members": body.members or [], "owner_id": u.id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    await db.users.update_one({"id": u.id}, {"$set": {"family_id": fid}})
    return {"id": fid, "name": body.name or f"Famille {u.first_name}", "members": body.members or []}


@api.put("/users/me/family")
async def update_family(body: FamilyBody, u: UserPublic = Depends(get_current_user)):
    if not u.family_id:
        raise HTTPException(404, "Aucune famille associée")
    update: Dict[str, Any] = {}
    if body.name is not None:
        update["name"] = body.name
    if body.members is not None:
        update["members"] = body.members
    await db.families.update_one({"id": u.family_id}, {"$set": update})
    fam = await db.families.find_one({"id": u.family_id}, {"_id": 0})
    return fam or {}


@api.post("/users/me/children")
async def add_child(body: ChildBody, u: UserPublic = Depends(get_current_user)):
    if not u.family_id:
        # Auto-create family
        fid = str(uuid.uuid4())
        await db.families.insert_one({
            "id": fid, "name": f"Famille {u.first_name}", "members": [], "owner_id": u.id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        await db.users.update_one({"id": u.id}, {"$set": {"family_id": fid}})
        family_id = fid
    else:
        family_id = u.family_id
    child = {
        "id": str(uuid.uuid4()),
        "first_name": body.first_name,
        "age": body.age,
        "arabic_level": body.arabic_level,
        "stories_listened": 0,
    }
    await db.families.update_one({"id": family_id}, {"$push": {"members": child}})
    return child


@api.get("/users/me/subscription")
async def my_subscription(u: UserPublic = Depends(get_current_user)):
    return u.subscription or {"plan": None, "status": None}


# Keep old streak endpoint for frontend compat
@api.post("/stats/streak/checkin", response_model=UserPublic)
async def streak_checkin(u: UserPublic = Depends(get_current_user)):
    today = datetime.now(timezone.utc).date()
    doc = await db.users.find_one({"id": u.id}, {"_id": 0})
    last = doc.get("last_checkin")
    streak = doc.get("streak_days", 0)
    if last != today.isoformat():
        yesterday = (today - timedelta(days=1)).isoformat()
        streak = streak + 1 if last == yesterday else 1
        await db.users.update_one(
            {"id": u.id},
            {"$set": {"streak_days": streak, "last_checkin": today.isoformat()}},
        )
    doc = await db.users.find_one({"id": u.id}, {"_id": 0})
    return user_public(doc)


# =========================================================================
# MOSQUES
# =========================================================================
@api.get("/mosques", response_model=List[Mosque])
async def list_mosques(
    city: Optional[str] = None,
    live: Optional[bool] = None,
    page: int = 1,
    page_size: int = 50,
):
    q: Dict[str, Any] = {}
    if city:
        q["city"] = {"$regex": city, "$options": "i"}
    if live is not None:
        q["is_live"] = live
    skip = max(0, (page - 1) * page_size)
    docs = await db.mosques.find(q, {"_id": 0}).skip(skip).limit(page_size).to_list(page_size)
    return [Mosque(**d) for d in docs]


@api.get("/mosques/{mid}", response_model=Mosque)
async def get_mosque(mid: str):
    doc = await db.mosques.find_one({"id": mid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Mosquée introuvable")
    return Mosque(**doc)


@api.post("/mosques/{mid}/follow", response_model=UserPublic)
async def follow_mosque(mid: str, u: UserPublic = Depends(get_current_user)):
    if not await db.mosques.find_one({"id": mid}):
        raise HTTPException(404, "Mosquée introuvable")
    await db.users.update_one({"id": u.id}, {"$set": {"favorite_mosque_id": mid}})
    await db.mosques.update_one({"id": mid}, {"$inc": {"subscribers": 1}})
    doc = await db.users.find_one({"id": u.id}, {"_id": 0})
    return user_public(doc)


@api.delete("/mosques/{mid}/follow", response_model=UserPublic)
async def unfollow_mosque(mid: str, u: UserPublic = Depends(get_current_user)):
    if u.favorite_mosque_id == mid:
        await db.users.update_one({"id": u.id}, {"$set": {"favorite_mosque_id": None}})
        await db.mosques.update_one({"id": mid}, {"$inc": {"subscribers": -1}})
    doc = await db.users.find_one({"id": u.id}, {"_id": 0})
    return user_public(doc)


@api.get("/mosques/{mid}/live-status")
async def mosque_live_status(mid: str):
    doc = await db.mosques.find_one({"id": mid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Mosquée introuvable")
    return {
        "isLive": doc.get("is_live", False),
        "viewers": doc.get("subscribers", 0) if doc.get("is_live") else 0,
        "startedAt": datetime.now(timezone.utc).isoformat() if doc.get("is_live") else None,
        "quality": "HD",
    }


@api.get("/mosques/{mid}/schedule")
async def mosque_schedule(mid: str):
    if not await db.mosques.find_one({"id": mid}):
        raise HTTPException(404, "Mosquée introuvable")
    # 7 jours statiques par défaut
    base = await get_prayer_times()
    days = []
    today = datetime.now(timezone.utc).date()
    for i in range(7):
        d = today + timedelta(days=i)
        days.append({"date": d.isoformat(), "prayers": [p.model_dump() for p in base]})
    return {"mosque_id": mid, "days": days}


@api.get("/mosques/{mid}/events")
async def mosque_events(mid: str):
    docs = await db.mosque_events.find({"mosque_id": mid}, {"_id": 0}).to_list(100)
    return docs


# =========================================================================
# CONTENT
# =========================================================================
@api.get("/surahs", response_model=List[Surah])
@api.get("/content/coran", response_model=List[Surah])
async def list_surahs():
    docs = await db.surahs.find({}, {"_id": 0}).sort("number", 1).to_list(200)
    return [Surah(**d) for d in docs]


@api.get("/content/coran/{sid}", response_model=Surah)
async def get_surah(sid: str):
    doc = await db.surahs.find_one({"id": sid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Sourate introuvable")
    return Surah(**doc)


# ─── Quran complete (114 surahs + audio + verses from Al-Quran Cloud) ──────
@api.get("/quran/surahs")
async def quran_list_surahs():
    """114 sourates avec audio URL pour le récitateur par défaut (Alafasy)."""
    return [
        {
            "id": f"surah-{s['n']:03d}",
            "number": s["n"],
            "name_ar": s["ar"],
            "name_translit": s["tr"],
            "name_fr": s["fr"],
            "verses": s["verses"],
            "type": s["type"],
            "audio_url": get_audio_url(s["n"], "ar.alafasy"),
        }
        for s in SURAHS_114
    ]


@api.get("/quran/reciters")
async def quran_list_reciters():
    return RECITERS


@api.get("/quran/surah/{number}")
async def quran_get_surah(number: int, reciter: str = "ar.alafasy", translation: str = "fr.hamidullah"):
    """
    Sourate complète : versets ar + traduction fr + audio URL.
    Cache en MongoDB après le 1er fetch pour rapidité offline.
    """
    if number < 1 or number > 114:
        raise HTTPException(400, "Numéro de sourate invalide (1-114)")
    # v2 = on invalide les anciens caches (qui contenaient la Basmala parasite)
    cache_key = f"surah-{number}-{translation}-v2"
    cached = await db.quran_cache.find_one({"_id": cache_key})
    if cached:
        verses = cached["verses"]
    else:
        # Fetch from Al-Quran Cloud — combine arabic + french in 1 call each
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                ar_r = await client.get(f"https://api.alquran.cloud/v1/surah/{number}/ar")
                fr_r = await client.get(f"https://api.alquran.cloud/v1/surah/{number}/{translation}")
                ar_data = ar_r.json()["data"]
                fr_data = fr_r.json()["data"]

            # 🔧 FIX : alquran.cloud préfixe la Basmala au verset 1 en arabe
            # pour TOUTES les sourates SAUF Al-Fatiha (1) et At-Tawbah (9).
            # On la retire pour que ça matche la traduction française.
            # Approche robuste : on compare en supprimant les diacritiques (harakat)
            import re as _re
            ARABIC_DIACRITICS = _re.compile(r'[\u064B-\u065F\u0670\u06D6-\u06ED\uFEFF]')

            def strip_diacritics(s: str) -> str:
                return ARABIC_DIACRITICS.sub('', s)

            BASMALA_BARE = "بسم الله الرحمن الرحيم"  # sans diacritiques

            def strip_basmala(text: str) -> str:
                # Compare les premiers caractères sans diacritiques
                bare = strip_diacritics(text).lstrip()
                if bare.startswith(BASMALA_BARE):
                    # Cherche la position après la basmala dans le texte original
                    # On scanne char par char, en sautant les diacritiques
                    target_len = len(BASMALA_BARE)
                    matched = 0
                    pos = 0
                    while pos < len(text) and matched < target_len:
                        c = text[pos]
                        if ARABIC_DIACRITICS.match(c) or c == '\ufeff':
                            pos += 1
                            continue
                        matched += 1
                        pos += 1
                    # Consomme tous les diacritiques/espaces résiduels après la Basmala
                    while pos < len(text) and (ARABIC_DIACRITICS.match(text[pos]) or text[pos] in ' \ufeff\u200f'):
                        pos += 1
                    return text[pos:].strip()
                return text

            verses = []
            for ar_v, fr_v in zip(ar_data["ayahs"], fr_data["ayahs"]):
                ar_text = ar_v["text"]
                # Ne supprimer la basmala que pour les autres sourates (pas 1 ni 9)
                if number not in (1, 9) and ar_v["numberInSurah"] == 1:
                    ar_text = strip_basmala(ar_text)
                verses.append({
                    "n": ar_v["numberInSurah"],
                    "ar": ar_text,
                    "fr": fr_v["text"],
                    "audio_ayah": f"https://cdn.islamic.network/quran/audio/128/ar.alafasy/{ar_v['number']}.mp3",
                })
            await db.quran_cache.insert_one({"_id": cache_key, "verses": verses})
        except Exception as e:  # noqa: BLE001
            log.warning("Quran fetch failed for surah %s: %s", number, e)
            raise HTTPException(502, "Impossible de charger la sourate. Réessayez.") from e

    meta = next((s for s in SURAHS_114 if s["n"] == number), None)
    if not meta:
        raise HTTPException(404, "Sourate introuvable")
    return {
        "number": number,
        "name_ar": meta["ar"],
        "name_translit": meta["tr"],
        "name_fr": meta["fr"],
        "type": meta["type"],
        "verses": verses,
        "audio_url": get_audio_url(number, reciter),
        "reciter": reciter,
    }


@api.get("/quran/timestamps")
async def quran_timestamps(surah: int = Query(...), reciter: str = Query("alafasy")):
    """
    Timestamps verset-par-verset depuis Quran.com v4 (chapter_recitations?segments=true).
    Mappe nos IDs internes vers les IDs Quran.com.
    Retour : { audio_url, timestamps: [{n, start_ms, end_ms}, ...] }
    """
    QURAN_COM_IDS = {
        "alafasy": 7, "abdulbasit": 1, "sudais": 3, "husary": 5,
        "minshawi": 4, "shaatree": 11, "maher": 9,
    }
    rid = QURAN_COM_IDS.get(reciter, 7)
    cache_key = f"ts2-{surah}-{reciter}"
    cached = await db.quran_cache.find_one({"_id": cache_key})
    if cached:
        return {"audio_url": cached.get("audio_url", ""), "timestamps": cached["timestamps"]}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                f"https://api.quran.com/api/v4/chapter_recitations/{rid}/{surah}",
                params={"segments": "true"},
            )
            data = r.json().get("audio_file", {})
        audio_url = data.get("audio_url", "")
        timestamps = []
        for af in data.get("timestamps", []):
            try:
                v = int(str(af["verse_key"]).split(":")[-1])
                timestamps.append({
                    "n": v,
                    "start_ms": int(af["timestamp_from"]),
                    "end_ms": int(af["timestamp_to"]),
                })
            except Exception:
                continue
        await db.quran_cache.insert_one({
            "_id": cache_key, "timestamps": timestamps, "audio_url": audio_url,
        })
        return {"audio_url": audio_url, "timestamps": timestamps}
    except Exception as e:  # noqa: BLE001
        log.warning("Quran timestamps fetch failed: %s", e)
        return {"audio_url": "", "timestamps": []}


# ─── Cards PDF — fiches d'apprentissage enfants ────────────────────────────
@api.get("/cards/all.pdf")
async def cards_all_pdf():
    """Génère un PDF complet des 28 fiches (une par page)."""
    pdf = make_full_alphabet_pdf(LETTERS_RICH)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="adhan-alphabet-arabe.pdf"'},
    )


@api.get("/cards/{tr}.pdf")
async def card_single_pdf(tr: str):
    """Génère un PDF d'une seule lettre (par translit)."""
    letter = next((l for l in LETTERS_RICH if l["tr"].lower() == tr.lower()), None)
    if not letter:
        raise HTTPException(404, f"Lettre '{tr}' introuvable")
    pdf = make_single_pdf(letter["ar"], letter["tr"], letter["word_ar"], letter["word_fr"])
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="adhan-{letter["tr"]}.pdf"'},
    )



@api.get("/recipes", response_model=List[Recipe])
@api.get("/content/recipes", response_model=List[Recipe])
async def list_recipes(category: Optional[str] = None):
    q: Dict[str, Any] = {}
    if category:
        q["category"] = category
    docs = await db.recipes.find(q, {"_id": 0}).to_list(200)
    return [Recipe(**d) for d in docs]


@api.get("/recipes/{rid}", response_model=Recipe)
@api.get("/content/recipes/{rid}", response_model=Recipe)
async def get_recipe(rid: str):
    doc = await db.recipes.find_one({"id": rid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Recette introuvable")
    return Recipe(**doc)


@api.get("/prophets", response_model=List[Prophet])
@api.get("/content/stories", response_model=List[Prophet])
async def list_prophets(age_range: Optional[str] = None):
    q: Dict[str, Any] = {}
    if age_range:
        q["age_range"] = age_range
    docs = await db.prophets.find(q, {"_id": 0}).to_list(100)
    return [Prophet(**d) for d in docs]


@api.get("/content/stories/{sid}", response_model=Prophet)
async def get_story(sid: str):
    doc = await db.prophets.find_one({"id": sid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Histoire introuvable")
    return Prophet(**doc)


ARABIC_LESSONS = [
    {"id": f"al-{i}", "type": "alphabet", "level": 1, "title": f"Lettre #{i+1}",
     "audio_url": "https://www.islamcan.com/audio/adhan/azan2.mp3"}
    for i in range(28)
]


@api.get("/content/arabic")
async def arabic_lessons(level: Optional[int] = None, type: Optional[str] = None):
    out = ARABIC_LESSONS
    if level is not None:
        out = [l for l in out if l["level"] == level]
    if type:
        out = [l for l in out if l["type"] == type]
    return out


@api.get("/content/arabic/{lid}")
async def arabic_lesson(lid: str):
    for l in ARABIC_LESSONS:
        if l["id"] == lid:
            return l
    raise HTTPException(404, "Leçon introuvable")


# =========================================================================
# PRAYER TIMES (Aladhan)
# =========================================================================
@api.get("/prayer-times", response_model=List[PrayerTime])
async def get_prayer_times(
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    method: int = 2,
):
    static = [
        PrayerTime(name="Fajr", time="06:12"),
        PrayerTime(name="Dhouhr", time="13:08"),
        PrayerTime(name="Asr", time="15:42"),
        PrayerTime(name="Maghrib", time="18:24"),
        PrayerTime(name="Isha", time="20:01"),
    ]
    if lat is None or lng is None:
        return static
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as cli:
            r = await cli.get(
                "https://api.aladhan.com/v1/timings",
                params={"latitude": lat, "longitude": lng, "method": method},
            )
            r.raise_for_status()
            t = r.json().get("data", {}).get("timings", {})
            mapping = {"Fajr": "Fajr", "Dhuhr": "Dhouhr", "Asr": "Asr", "Maghrib": "Maghrib", "Isha": "Isha"}
            out = [PrayerTime(name=v, time=t[k].split(" ")[0]) for k, v in mapping.items() if k in t]
            if len(out) == 5:
                return out
    except Exception as e:
        logger.warning("Aladhan failed: %s", e)
    return static


# =========================================================================
# BAF DEVICES
# =========================================================================
@api.get("/baf/devices", response_model=List[BAFDevice])
async def list_baf(u: UserPublic = Depends(get_current_user)):
    docs = await db.baf_devices.find({"owner_id": u.id}, {"_id": 0}).to_list(50)
    return [BAFDevice(**d) for d in docs]


@api.post("/baf/pair", response_model=BAFDevice)
async def pair_baf(body: PairBAFBody, u: UserPublic = Depends(get_current_user)):
    # MOCKED hardware pairing — accept any QR string, generate a device
    dev_id = body.qr_code if body.qr_code.startswith("BAF-") else f"BAF-{secrets.token_hex(4).upper()}"
    doc = {
        "id": str(uuid.uuid4()),
        "device_id": dev_id,
        "name": body.name or "BAF Salon",
        "type": "main",
        "online": True,
        "firmware": "1.2.4",
        "volume": 60,
        "mode": "live",
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "linked_mosque_id": u.favorite_mosque_id,
        "owner_id": u.id,
    }
    await db.baf_devices.insert_one(doc)
    return BAFDevice(**{k: v for k, v in doc.items() if k != "owner_id"})


@api.delete("/baf/{bid}")
async def remove_baf(bid: str, u: UserPublic = Depends(get_current_user)):
    res = await db.baf_devices.delete_one({"id": bid, "owner_id": u.id})
    if res.deleted_count == 0:
        raise HTTPException(404, "BAF introuvable")
    return {"ok": True}


@api.put("/baf/{bid}/volume", response_model=BAFDevice)
async def baf_volume(bid: str, body: BAFVolumeBody, u: UserPublic = Depends(get_current_user)):
    res = await db.baf_devices.find_one_and_update(
        {"id": bid, "owner_id": u.id}, {"$set": {"volume": body.volume}}, return_document=True,
    )
    if not res:
        raise HTTPException(404, "BAF introuvable")
    doc = await db.baf_devices.find_one({"id": bid}, {"_id": 0})
    return BAFDevice(**{k: v for k, v in doc.items() if k != "owner_id"})


@api.put("/baf/{bid}/mode", response_model=BAFDevice)
async def baf_mode(bid: str, body: BAFModeBody, u: UserPublic = Depends(get_current_user)):
    if body.mode not in {"live", "story", "coran", "off", "night"}:
        raise HTTPException(400, "Mode invalide")
    await db.baf_devices.update_one({"id": bid, "owner_id": u.id}, {"$set": {"mode": body.mode}})
    doc = await db.baf_devices.find_one({"id": bid, "owner_id": u.id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "BAF introuvable")
    return BAFDevice(**{k: v for k, v in doc.items() if k != "owner_id"})


@api.get("/baf/{bid}/status")
async def baf_status(bid: str, u: UserPublic = Depends(get_current_user)):
    doc = await db.baf_devices.find_one({"id": bid, "owner_id": u.id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "BAF introuvable")
    return {
        "online": doc.get("online", False),
        "firmware": doc.get("firmware"),
        "lastSeen": doc.get("last_seen"),
        "volume": doc.get("volume"),
        "mode": doc.get("mode"),
    }


@api.post("/baf/{bid}/schedule")
async def baf_schedule(bid: str, body: BAFScheduleBody, u: UserPublic = Depends(get_current_user)):
    item = {"id": str(uuid.uuid4()), **body.model_dump()}
    res = await db.baf_devices.update_one(
        {"id": bid, "owner_id": u.id}, {"$push": {"schedule": item}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "BAF introuvable")
    return item


# =========================================================================
# SHOP (existing)
# =========================================================================
@api.get("/shop/products", response_model=List[Product])
async def list_products(context: Optional[str] = None):
    q: Dict[str, Any] = {}
    if context:
        q["context"] = context
    docs = await db.products.find(q, {"_id": 0}).to_list(100)
    return [Product(**d) for d in docs]


# =========================================================================
# SUBSCRIPTIONS (Stripe)
# =========================================================================
PLANS = {
    "PREMIUM": {"name": "Premium", "monthly_eur": 5, "setup_eur": 0},
    "FAMILLE": {"name": "Famille", "monthly_eur": 8, "setup_eur": 0},
    "BAF_BUNDLE": {"name": "BAF Bundle", "monthly_eur": 5, "setup_eur": 394},
    "BAF_FAMILLE": {"name": "BAF Famille", "monthly_eur": 8, "setup_eur": 500},
}


def build_line_items(plan_code: str) -> List[Dict[str, Any]]:
    p = PLANS[plan_code]
    items: List[Dict[str, Any]] = [{
        "price_data": {
            "currency": "eur",
            "product_data": {"name": f"Adhan Connect — {p['name']}"},
            "recurring": {"interval": "month"},
            "unit_amount": p["monthly_eur"] * 100,
        },
        "quantity": 1,
    }]
    if p["setup_eur"] > 0:
        items.append({
            "price_data": {
                "currency": "eur",
                "product_data": {"name": f"Adhan Connect — {p['name']} (BAF setup)"},
                "unit_amount": p["setup_eur"] * 100,
            },
            "quantity": 1,
        })
    return items


@api.post("/subscriptions/create")
async def create_subscription(body: SubscriptionCreateBody, u: UserPublic = Depends(get_current_user)):
    if body.plan not in PLANS:
        raise HTTPException(400, f"Plan inconnu : {body.plan}")
    if not STRIPE_KEY or not STRIPE_KEY.startswith("sk_"):
        raise HTTPException(503, "Stripe non configuré")
    user_doc = await db.users.find_one({"id": u.id})
    success_url = f"{MOBILE_SCHEME}://subscribe/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{APP_DOMAIN}/subscribe/cancelled"
    # Demo fallback when a placeholder key is used (cannot actually call Stripe)
    if STRIPE_KEY == "sk_test_emergent" or len(STRIPE_KEY) < 30:
        return {
            "checkoutUrl": f"{APP_DOMAIN}/subscribe/demo?plan={body.plan}&user={u.id}",
            "sessionId": f"cs_demo_{secrets.token_hex(8)}",
            "demo_mode": True,
            "note": "Stripe key is a placeholder. Provide a real sk_test_… key to enable live Checkout.",
        }
    try:
        sess = stripe.checkout.Session.create(
            mode="subscription",
            line_items=build_line_items(body.plan),
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=u.id,
            customer=user_doc.get("stripe_customer_id") or None,
            customer_email=None if user_doc.get("stripe_customer_id") else u.email,
            subscription_data={"metadata": {"plan_code": body.plan, "user_id": u.id}},
            allow_promotion_codes=True,
        )
    except Exception as e:
        logger.exception("Stripe create session failed")
        raise HTTPException(500, f"Stripe error: {e}")
    return {"checkoutUrl": sess.url, "sessionId": sess.id}


@api.get("/subscriptions/current")
async def current_sub(u: UserPublic = Depends(get_current_user)):
    sub = u.subscription or {}
    return {
        "plan": sub.get("plan"),
        "status": sub.get("status"),
        "stripeId": sub.get("stripe_id"),
        "expiresAt": sub.get("expires_at"),
    }


@api.post("/subscriptions/cancel")
async def cancel_sub(u: UserPublic = Depends(get_current_user)):
    sub_id = (u.subscription or {}).get("stripe_id")
    if not sub_id:
        raise HTTPException(400, "Aucun abonnement actif")
    try:
        stripe_sub = stripe.Subscription.delete(sub_id)
    except Exception as e:
        raise HTTPException(500, f"Stripe error: {e}")
    await db.users.update_one(
        {"id": u.id},
        {"$set": {"subscription.status": stripe_sub.status, "subscription.expires_at": None}},
    )
    return {"status": stripe_sub.status}


@api.post("/subscriptions/portal")
async def portal_sub(u: UserPublic = Depends(get_current_user)):
    user_doc = await db.users.find_one({"id": u.id})
    cust_id = user_doc.get("stripe_customer_id")
    if not cust_id:
        raise HTTPException(400, "Aucun client Stripe associé")
    try:
        sess = stripe.billing_portal.Session.create(
            customer=cust_id, return_url=f"{APP_DOMAIN}/account",
        )
    except Exception as e:
        raise HTTPException(500, f"Stripe error: {e}")
    return {"portalUrl": sess.url}


@api.post("/webhooks/stripe")
async def stripe_webhook(request: Request, stripe_signature: Optional[str] = Header(default=None, alias="Stripe-Signature")):
    payload = await request.body()
    # Verify signature only if webhook secret configured
    if STRIPE_WH_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WH_SECRET)
        except Exception:
            raise HTTPException(400, "Invalid signature")
    else:
        import json
        event = json.loads(payload.decode())  # dev-mode, no verification

    eid = event.get("id")
    if eid and await db.stripe_events.find_one({"_id": eid}):
        return {"received": True, "duplicate": True}

    et = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    try:
        if et == "checkout.session.completed":
            user_id = obj.get("client_reference_id")
            cust = obj.get("customer")
            sub_id = obj.get("subscription")
            plan = ((obj.get("metadata") or {}).get("plan_code")) or None
            if user_id:
                upd: Dict[str, Any] = {"stripe_customer_id": cust}
                if sub_id:
                    upd.update({
                        "subscription.plan": plan,
                        "subscription.status": "active",
                        "subscription.stripe_id": sub_id,
                    })
                await db.users.update_one({"id": user_id}, {"$set": upd})
        elif et.startswith("customer.subscription."):
            sub_id = obj.get("id")
            status_ = obj.get("status")
            cpe = obj.get("current_period_end")
            expires_at = datetime.fromtimestamp(cpe, tz=timezone.utc).isoformat() if cpe else None
            await db.users.update_one(
                {"subscription.stripe_id": sub_id},
                {"$set": {"subscription.status": status_, "subscription.expires_at": expires_at}},
            )
    finally:
        if eid:
            await db.stripe_events.insert_one({"_id": eid, "processed_at": datetime.now(timezone.utc).isoformat()})

    return {"received": True}


# =========================================================================
# ADMIN — MOSQUE (mocked but realistic data)
# =========================================================================
admin_mosque = APIRouter(prefix="/api/admin/mosque", dependencies=[Depends(require_admin)])


@admin_mosque.get("/stats")
async def admin_mosque_stats(u: UserPublic = Depends(get_current_user)):
    total_followers = await db.mosques.aggregate([{"$group": {"_id": None, "n": {"$sum": "$subscribers"}}}]).to_list(1)
    n = total_followers[0]["n"] if total_followers else 0
    return {
        "totalFollowers": n,
        "liveViewers": 412,
        "monthlyPlays": 18243,
        "revenue": {"month_eur": 2840, "currency": "eur"},
    }


@admin_mosque.get("/audience")
async def admin_mosque_audience():
    return [
        {"city": "Paris", "subscribers": 924, "lat": 48.85, "lng": 2.35},
        {"city": "Lyon", "subscribers": 312, "lat": 45.76, "lng": 4.83},
        {"city": "Marseille", "subscribers": 287, "lat": 43.30, "lng": 5.40},
        {"city": "Bruxelles", "subscribers": 211, "lat": 50.84, "lng": 4.35},
        {"city": "Strasbourg", "subscribers": 178, "lat": 48.58, "lng": 7.75},
    ]


@admin_mosque.get("/revenue")
async def admin_mosque_revenue():
    today = datetime.now(timezone.utc).date()
    months = [(today.replace(day=1) - timedelta(days=30 * i)).isoformat()[:7] for i in range(6)][::-1]
    return [{"month": m, "eur": 1200 + i * 180} for i, m in enumerate(months)]


@admin_mosque.get("/content")
async def admin_mosque_content():
    return await db.surahs.find({}, {"_id": 0}).limit(20).to_list(20)


@admin_mosque.post("/events")
async def admin_mosque_create_event(payload: Dict[str, Any] = Body(...), u: UserPublic = Depends(get_current_user)):
    doc = {"id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(), **payload}
    await db.mosque_events.insert_one(doc)
    return {k: v for k, v in doc.items() if k != "_id"}


@admin_mosque.get("/live/start")
async def admin_mosque_live_start(mosque_id: str = Query(...)):
    if not await db.mosques.find_one({"id": mosque_id}):
        raise HTTPException(404, "Mosquée introuvable")
    token = secrets.token_urlsafe(24)
    await db.mosques.update_one({"id": mosque_id}, {"$set": {"is_live": True}})
    return {
        "broadcastToken": token,
        "wsEndpoint": f"/ws/broadcast/{mosque_id}?token={token}",
        "instructions": "Connectez votre boîtier imam à ce WebSocket pour démarrer la diffusion.",
    }


@admin_mosque.post("/live/stop")
async def admin_mosque_live_stop(payload: Dict[str, Any] = Body(...)):
    mid = payload.get("mosque_id")
    if not mid:
        raise HTTPException(400, "mosque_id requis")
    await db.mosques.update_one({"id": mid}, {"$set": {"is_live": False}})
    return {"ok": True}


# =========================================================================
# ADMIN — SUPER
# =========================================================================
admin_super = APIRouter(prefix="/api/admin/super", dependencies=[Depends(require_admin)])


@admin_super.get("/overview")
async def super_overview():
    users = await db.users.count_documents({})
    mosques = await db.mosques.count_documents({})
    bafs = await db.baf_devices.count_documents({})
    return {
        "users": users, "mosques": mosques, "baf_devices": bafs,
        "mrr_eur": 18420, "arr_eur": 18420 * 12, "churn_rate": 0.024,
    }


@admin_super.get("/mosques")
async def super_mosques():
    docs = await db.mosques.find({}, {"_id": 0}).to_list(200)
    return docs


@admin_super.get("/users")
async def super_users(q: Optional[str] = None, page: int = 1, page_size: int = 50):
    query: Dict[str, Any] = {}
    if q:
        query["$or"] = [{"email": {"$regex": q, "$options": "i"}}, {"first_name": {"$regex": q, "$options": "i"}}]
    skip = max(0, (page - 1) * page_size)
    docs = await db.users.find(query, {"_id": 0, "hashed_password": 0}).skip(skip).limit(page_size).to_list(page_size)
    total = await db.users.count_documents(query)
    return {"total": total, "page": page, "page_size": page_size, "items": docs}


@admin_super.get("/baf/health")
async def super_baf_health():
    docs = await db.baf_devices.find({}, {"_id": 0}).to_list(500)
    online = sum(1 for d in docs if d.get("online"))
    return {"total": len(docs), "online": online, "offline": len(docs) - online, "alerts": []}


@admin_super.get("/revenue")
async def super_revenue():
    return {
        "mrr_eur": 18420, "arr_eur": 18420 * 12,
        "active_subscriptions": 412, "churn_rate": 0.024,
        "by_plan": [
            {"plan": "PREMIUM", "count": 184, "mrr_eur": 920},
            {"plan": "FAMILLE", "count": 156, "mrr_eur": 1248},
            {"plan": "BAF_BUNDLE", "count": 52, "mrr_eur": 260},
            {"plan": "BAF_FAMILLE", "count": 20, "mrr_eur": 160},
        ],
    }


@admin_super.get("/alerts")
async def super_alerts():
    return [
        {"id": "alert-1", "severity": "critical", "title": "Mosquée hors-ligne", "detail": "Al-Rahman Paris déconnectée", "since": "12 min", "target": "msq-paris-gp"},
        {"id": "alert-2", "severity": "warning", "title": "Latence élevée", "detail": "Latence stream 450ms à Marseille", "since": "34 min", "target": "msq-marseille"},
        {"id": "alert-3", "severity": "info", "title": "Pic d'inscriptions", "detail": "+32% cette semaine", "since": "1h", "target": None},
        {"id": "alert-4", "severity": "warning", "title": "Firmware obsolète", "detail": "12 BAF en version 1.0.x", "since": "3h", "target": "fleet"},
        {"id": "alert-5", "severity": "resolved", "title": "BAF reconnecté", "detail": "BAF-A4F2C9 retour en ligne", "since": "1h", "target": "BAF-A4F2C9"},
        {"id": "alert-6", "severity": "critical", "title": "Webhook Stripe en échec", "detail": "3 retry consécutifs", "since": "8 min", "target": "stripe"},
        {"id": "alert-7", "severity": "info", "title": "Nouvelle mosquée vérifiée", "detail": "Mosquée Annour validée", "since": "2h", "target": "msq-annour"},
        {"id": "alert-8", "severity": "warning", "title": "Quota CDN", "detail": "85% de la bande passante mensuelle", "since": "5h", "target": "cdn"},
        {"id": "alert-9", "severity": "resolved", "title": "Pic CPU résolu", "detail": "Auto-scaling déclenché", "since": "6h", "target": "infra"},
        {"id": "alert-10", "severity": "info", "title": "Sauvegarde quotidienne OK", "detail": "Backup MongoDB Atlas 2.4 GB", "since": "8h", "target": "backup"},
        {"id": "alert-11", "severity": "warning", "title": "Taux d'échec auth", "detail": "12% sur les 30 dernières min", "since": "15 min", "target": "auth"},
        {"id": "alert-12", "severity": "critical", "title": "Stream coupé", "detail": "Diffusion Bruxelles interrompue", "since": "4 min", "target": "msq-bxl"},
        {"id": "alert-13", "severity": "resolved", "title": "Migration DB OK", "detail": "Index 2dsphere recréés", "since": "12h", "target": "db"},
        {"id": "alert-14", "severity": "info", "title": "Nouveau record DAU", "detail": "12 847 utilisateurs actifs aujourd'hui", "since": "30 min", "target": None},
        {"id": "alert-15", "severity": "warning", "title": "BAF firmware échec", "detail": "Mise à jour OTA bloquée sur 4 BAF", "since": "2h", "target": "firmware"},
    ]


@admin_super.get("/mrr-12months")
async def super_mrr_12months():
    """12 derniers mois de MRR, croissance réaliste de 15k → 241k €."""
    now = datetime.now(timezone.utc)
    base = [15000, 22000, 35000, 48000, 62000, 78000, 94000, 118000, 142000, 168000, 198000, 241455]
    out = []
    for i, mrr in enumerate(base):
        d = now.replace(day=1) - timedelta(days=30 * (11 - i))
        out.append({"month": d.strftime("%Y-%m"), "label": d.strftime("%b"), "mrr": mrr})
    return out


@admin_super.get("/dau-mau")
async def super_dau_mau():
    """7 derniers jours de DAU."""
    today = datetime.now(timezone.utc).date()
    base = [8420, 9210, 11280, 10840, 12150, 11920, 12847]
    return [{"day": (today - timedelta(days=6 - i)).isoformat(), "label": ["Lun","Mar","Mer","Jeu","Ven","Sam","Dim"][i], "users": v} for i, v in enumerate(base)]


@admin_super.get("/top-mosques")
async def super_top_mosques():
    docs = await db.mosques.find({}, {"_id": 0}).sort("subscribers", -1).limit(10).to_list(10)
    out = []
    for d in docs:
        out.append({
            "id": d["id"], "name": d["name"], "city": d["city"],
            "subscribers": d.get("subscribers", 0),
            "is_live": d.get("is_live", False),
            "mrr_eur": int(d.get("subscribers", 0) * 0.5),  # estimation 0.5€/abonné
        })
    return out


@admin_super.get("/latest-users")
async def super_latest_users():
    docs = await db.users.find({}, {"_id": 0, "hashed_password": 0}).sort("created_at", -1).limit(10).to_list(10)
    out = []
    for d in docs:
        sub = d.get("subscription") or {}
        plan = sub.get("plan") if isinstance(sub, dict) else sub
        out.append({
            "id": d["id"],
            "first_name": d.get("first_name", "?"),
            "email": d.get("email"),
            "plan": plan or "Gratuit",
            "created_at": d.get("created_at"),
        })
    return out


@admin_super.get("/france-cities")
async def super_france_cities():
    """Villes françaises avec nombre de BAF actifs (mocked pour la carte)."""
    return [
        {"city": "Paris", "lat": 48.8566, "lng": 2.3522, "baf": 342, "live_mosques": 4},
        {"city": "Lyon", "lat": 45.7640, "lng": 4.8357, "baf": 218, "live_mosques": 2},
        {"city": "Marseille", "lat": 43.2965, "lng": 5.3698, "baf": 287, "live_mosques": 3},
        {"city": "Toulouse", "lat": 43.6047, "lng": 1.4442, "baf": 124, "live_mosques": 1},
        {"city": "Nice", "lat": 43.7102, "lng": 7.2620, "baf": 89, "live_mosques": 1},
        {"city": "Nantes", "lat": 47.2184, "lng": -1.5536, "baf": 76, "live_mosques": 0},
        {"city": "Strasbourg", "lat": 48.5734, "lng": 7.7521, "baf": 142, "live_mosques": 1},
        {"city": "Bordeaux", "lat": 44.8378, "lng": -0.5792, "baf": 98, "live_mosques": 1},
        {"city": "Lille", "lat": 50.6292, "lng": 3.0573, "baf": 154, "live_mosques": 2},
        {"city": "Bruxelles", "lat": 50.8503, "lng": 4.3517, "baf": 167, "live_mosques": 1},
        {"city": "Rennes", "lat": 48.1173, "lng": -1.6778, "baf": 54, "live_mosques": 0},
        {"city": "Montpellier", "lat": 43.6108, "lng": 3.8767, "baf": 73, "live_mosques": 0},
    ]


@admin_mosque.get("/uploads")
async def admin_mosque_uploads():
    """MOCK — liste de contenus uploadés."""
    today = datetime.now(timezone.utc).date()
    return [
        {"id": "up-1", "filename": "Khoutba Vendredi 16 Mai", "size_mb": 48, "duration": "42:18", "status": "approved", "date": today.isoformat()},
        {"id": "up-2", "filename": "Récitation Ramadan Nuit 27", "size_mb": 12, "duration": "08:24", "status": "pending", "date": (today - timedelta(days=1)).isoformat()},
        {"id": "up-3", "filename": "Khoutba Aïd al-Fitr", "size_mb": 67, "duration": "58:02", "status": "rejected", "date": (today - timedelta(days=4)).isoformat()},
        {"id": "up-4", "filename": "Conférence Tafsir Surah Kahf", "size_mb": 95, "duration": "1:24:33", "status": "approved", "date": (today - timedelta(days=7)).isoformat()},
        {"id": "up-5", "filename": "Dou'a du Qiyyam", "size_mb": 18, "duration": "12:48", "status": "approved", "date": (today - timedelta(days=12)).isoformat()},
    ]


@admin_mosque.get("/live/current")
async def admin_mosque_live_current(mosque_id: str = Query("msq-paris-gp")):
    """Statut live courant d'une mosquée (pour la card Live Control)."""
    doc = await db.mosques.find_one({"id": mosque_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Mosquée introuvable")
    is_live = doc.get("is_live", False)
    return {
        "mosque_id": mosque_id,
        "mosque_name": doc.get("name"),
        "mosque_city": doc.get("city"),
        "isLive": is_live,
        "listeners": doc.get("subscribers", 0) if is_live else 0,
        "quality": "HD 96 kbps",
        "latency_ms": 187,
        "uptime": "99.8%",
        "started_at": datetime.now(timezone.utc).replace(hour=12, minute=34, second=0, microsecond=0).isoformat() if is_live else None,
        "next_prayer": "Maghrib",
        "next_prayer_at": "20:47",
    }


@admin_super.post("/content/moderate")
async def super_moderate(body: Dict[str, Any] = Body(...)):
    return {"ok": True, "content_id": body.get("content_id") or body.get("audio_id"), "decision": body.get("decision") or body.get("action", "approve")}


# ── Iteration 2 endpoints ───────────────────────────────────────────────────
@admin_super.get("/hardware/list")
async def super_hardware_list():
    """Detailed BAF parc with synthetic but realistic mock entries."""
    today = datetime.now(timezone.utc).date()
    devices = []
    cities = ["Paris", "Lyon", "Marseille", "Toulouse", "Nice", "Nantes", "Strasbourg", "Bordeaux", "Lille", "Bruxelles"]
    statuses = [("online", 0.78), ("offline", 0.14), ("warning", 0.08)]
    fws = ["2.1.4", "2.1.3", "2.0.8", "2.1.4", "2.1.4", "2.1.4", "1.9.2", "2.1.4"]
    for i in range(48):
        st = statuses[0][0] if i % 7 not in (3, 6) else (statuses[1][0] if i % 7 == 3 else statuses[2][0])
        devices.append({
            "id": f"baf-{i+1:04d}",
            "serial": f"BAF-{(0xA4F2C9 + i):06X}",
            "model": "BAF Salon" if i % 3 else ("BAF Enfants" if i % 5 else "BAF Mosquée"),
            "owner": f"Famille {['Benali','Khaldi','Hassan','Toumi','Ziani','Mahmoud','Saidi','Lemoine'][i%8]}",
            "city": cities[i % len(cities)],
            "firmware": fws[i % len(fws)],
            "status": st,
            "uptime_pct": round(99.8 if st == "online" else (78.2 if st == "warning" else 0), 1),
            "last_seen": (datetime.now(timezone.utc) - timedelta(minutes=(i*17) % 1440)).isoformat(),
            "linked_mosque": f"msq-{cities[i%len(cities)].lower()}-{(i%3)+1}",
        })
    return devices


@admin_super.get("/content/list")
async def super_content_list():
    """All audio contents across mosques with moderation status."""
    today = datetime.now(timezone.utc).date()
    titles = [
        ("Khoutba Vendredi 16 Mai", "Grande Mosquée de Paris", 48, "42:18", "pending"),
        ("Récitation Ramadan Nuit 27", "Mosquée El-Islah Marseille", 12, "08:24", "pending"),
        ("Khoutba Aïd al-Fitr", "Mosquée de Lyon", 67, "58:02", "approved"),
        ("Conférence Tafsir Surah Kahf", "Mosquée Annour Strasbourg", 95, "1:24:33", "approved"),
        ("Dou'a du Qiyyam", "Mosquée El-Islah Marseille", 18, "12:48", "approved"),
        ("Récitation Sourate Yasin", "Grande Mosquée de Bruxelles", 24, "18:42", "pending"),
        ("Khoutba Patience & Foi", "Mosquée Bilal Lille", 52, "44:30", "approved"),
        ("Tafsir Sourate Al-Mulk", "Mezquita M-30 Madrid", 78, "1:08:15", "rejected"),
        ("Histoire de Yusuf ﷺ", "Mosquée Annour Strasbourg", 41, "35:24", "approved"),
        ("Ramadan — Récitation Tarawih", "Mosquée de Lyon", 112, "1:52:08", "approved"),
        ("Khoutba Sincérité", "Mosquée Bilal Lille", 38, "32:14", "pending"),
        ("Sirah du Prophète ﷺ — Ép. 12", "Westermoskee Amsterdam", 89, "1:14:32", "approved"),
        ("Récitation Sourate Al-Rahman", "Mosquée El-Islah Marseille", 22, "16:48", "approved"),
        ("Khoutba Aïd al-Adha", "Mosquée Annour Strasbourg", 56, "47:22", "approved"),
        ("Cours de Tajwid — Ép. 4", "Grande Mosquée de Paris", 34, "28:50", "pending"),
        ("Dou'a du voyageur", "Sehitlik Moschee Berlin", 8, "06:12", "approved"),
    ]
    out = []
    for i, (t, mosque, mb, dur, status) in enumerate(titles):
        out.append({
            "id": f"audio-{i+1:03d}",
            "title": t,
            "mosque": mosque,
            "category": "Khoutba" if "Khoutba" in t else ("Récitation" if "Récitation" in t else ("Cours" if "Cours" in t or "Tafsir" in t or "Sirah" in t or "Histoire" in t else "Dou'a")),
            "size_mb": mb,
            "duration": dur,
            "status": status,
            "plays": 12_847 - (i * 537),
            "date": (today - timedelta(days=i*2)).isoformat(),
        })
    return out


@admin_super.get("/revenue/timeline")
async def super_revenue_timeline():
    """Monthly revenue breakdown by plan (12 months)."""
    base = [15000, 22000, 35000, 48000, 62000, 78000, 94000, 118000, 142000, 168000, 198000, 241455]
    now = datetime.now(timezone.utc)
    out = []
    for i, total in enumerate(base):
        d = now.replace(day=1) - timedelta(days=30 * (11 - i))
        # rough split: premium 40%, famille 40%, baf bundle 12%, baf famille 8%
        out.append({
            "month": d.strftime("%Y-%m"),
            "label": d.strftime("%b"),
            "premium": int(total * 0.40),
            "famille": int(total * 0.40),
            "baf_bundle": int(total * 0.12),
            "baf_famille": int(total * 0.08),
            "total": total,
        })
    return out


@admin_mosque.get("/subscribers")
async def admin_mosque_subscribers(mosque_id: str = Query("msq-paris-gp")):
    """List of subscribers for a mosque (mock + real users mix)."""
    real_users = await db.users.find({}, {"_id": 0, "hashed_password": 0}).limit(20).to_list(20)
    out = []
    for u in real_users:
        sub = u.get("subscription") or {}
        plan = sub.get("plan") if isinstance(sub, dict) else sub
        out.append({
            "id": u["id"],
            "first_name": u.get("first_name", "?"),
            "email": u.get("email", "—"),
            "plan": plan or "Gratuit",
            "city": "Paris",
            "since": (u.get("created_at") or datetime.now(timezone.utc).isoformat())[:10],
            "monthly_eur": {"PREMIUM": 5, "FAMILLE": 8, "BAF_BUNDLE": 5, "BAF_FAMILLE": 8}.get((plan or "").upper(), 0),
        })
    # Pad with mock
    fillers = [
        ("Ahmed", "ahmed.b@example.com", "Premium", "Paris", 5),
        ("Fatima", "fatima.k@example.com", "Famille", "Saint-Denis", 8),
        ("Yasmine", "yasmine@example.com", "Famille", "Pantin", 8),
        ("Omar", "omar.t@example.com", "BAF Bundle", "Aubervilliers", 5),
        ("Khaled", "khaled@example.com", "Famille", "Bobigny", 8),
        ("Leila", "leila.m@example.com", "Premium", "Montreuil", 5),
        ("Imane", "imane@example.com", "Famille", "Vincennes", 8),
        ("Yusuf", "yusuf.r@example.com", "BAF Famille", "Saint-Ouen", 8),
        ("Sarah", "sarah.h@example.com", "Premium", "Bagnolet", 5),
        ("Khalil", "khalil@example.com", "Famille", "Vitry", 8),
    ]
    today = datetime.now(timezone.utc).date()
    for i, (fn, em, pl, city, eur) in enumerate(fillers):
        out.append({
            "id": f"sub-{i+1:03d}", "first_name": fn, "email": em, "plan": pl,
            "city": city, "since": (today - timedelta(days=30 + i*7)).isoformat(),
            "monthly_eur": eur,
        })
    return out


# =========================================================================
# WEBSOCKET — Simulated live audio broadcast
# =========================================================================
_adhan_bytes: Optional[bytes] = None
_clients: Dict[str, List[WebSocket]] = {}


async def _load_adhan_bytes() -> bytes:
    global _adhan_bytes
    if _adhan_bytes is not None:
        return _adhan_bytes
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as cli:
            r = await cli.get(ADHAN_STREAM_URL)
            r.raise_for_status()
            _adhan_bytes = r.content
            logger.info("Adhan MP3 cached: %d bytes", len(_adhan_bytes))
    except Exception as e:
        logger.warning("Failed to fetch adhan mp3: %s", e)
        _adhan_bytes = b""
    return _adhan_bytes


@app.websocket("/ws/live/{mosque_id}")
async def ws_live(websocket: WebSocket, mosque_id: str):
    """Simulated live audio broadcast. Streams a looping MP3 in 4 KB chunks at 50 ms cadence.
    Sends a `ping` JSON every 5 s. Accepts `{type:'pong'}` from client.
    """
    await websocket.accept()
    await websocket.send_json({"type": "ready", "quality": "HD", "latency": 120, "mosque_id": mosque_id})

    mp3 = await _load_adhan_bytes()
    if not mp3:
        await websocket.send_json({"type": "error", "message": "Adhan stream unavailable"})
        await websocket.close()
        return

    _clients.setdefault(mosque_id, []).append(websocket)
    chunk_size = 4096
    offset = 0
    last_ping = asyncio.get_event_loop().time()
    try:
        while True:
            chunk = mp3[offset : offset + chunk_size]
            if not chunk:
                offset = 0  # loop
                continue
            offset += chunk_size
            await websocket.send_bytes(chunk)
            now = asyncio.get_event_loop().time()
            if now - last_ping > 5:
                await websocket.send_json({"type": "ping", "ts": int(now)})
                last_ping = now
            await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("WS error: %s", e)
    finally:
        _clients.get(mosque_id, []).remove(websocket) if websocket in _clients.get(mosque_id, []) else None


@api.get("/ws/stats")
async def ws_stats():
    return {"connected_by_mosque": {k: len(v) for k, v in _clients.items()}}


# =========================================================================
# ROOT
# =========================================================================
@api.get("/")
async def root():
    return {"name": "Adhan Connect API", "version": "1.3", "status": "ok"}


# =========================================================================
# SEED (idempotent)
# =========================================================================
async def seed_database() -> None:
    if await db.mosques.count_documents({}) == 0:
        logger.info("Seeding mosques…")
        await db.mosques.insert_many([
            {"id": "msq-paris-gp", "name": "Grande Mosquée de Paris", "city": "Paris", "country": "France",
             "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/26/Grande_Mosqu%C3%A9e_de_Paris_n02.jpg/1280px-Grande_Mosqu%C3%A9e_de_Paris_n02.jpg",
             "is_live": True, "subscribers": 2847, "imam": "Cheikh Hafiz Chems-Eddine",
             "description": "Joyau architectural au cœur de Paris depuis 1926, refuge spirituel pour des milliers de fidèles.",
             "latitude": 48.842, "longitude": 2.3553, "distance_km": 1.2},
            {"id": "msq-lyon", "name": "Mosquée de Lyon", "city": "Lyon", "country": "France",
             "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/00/Grande_Mosqu%C3%A9e_de_Lyon_-_2.jpg/1280px-Grande_Mosqu%C3%A9e_de_Lyon_-_2.jpg",
             "is_live": False, "subscribers": 1432, "imam": "Cheikh Kamel Kabtane",
             "description": "Sanctuaire moderne dans la capitale des Gaules.", "latitude": 45.746, "longitude": 4.8528, "distance_km": 462.3},
            {"id": "msq-marseille", "name": "Mosquée El-Islah", "city": "Marseille", "country": "France",
             "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4e/Mosqu%C3%A9e_de_Marseille.JPG/1280px-Mosqu%C3%A9e_de_Marseille.JPG",
             "is_live": True, "subscribers": 3921, "imam": "Cheikh Abdelhamid Bouzar",
             "description": "Plus grande mosquée du sud, communauté chaleureuse et fervente.", "latitude": 43.3047, "longitude": 5.3849, "distance_km": 775.5},
            {"id": "msq-bxl", "name": "Grande Mosquée de Bruxelles", "city": "Bruxelles", "country": "Belgique",
             "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0e/Brussels_Grand_Mosque_R01.jpg/1280px-Brussels_Grand_Mosque_R01.jpg",
             "is_live": False, "subscribers": 2104, "imam": "Cheikh Tamer Abou Saber",
             "description": "Cœur spirituel du Parc du Cinquantenaire.", "latitude": 50.8412, "longitude": 4.3923, "distance_km": 311.0},
            {"id": "msq-londres", "name": "London Central Mosque", "city": "Londres", "country": "Royaume-Uni",
             "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/45/Regent%27s_Park_Mosque_-_geograph.org.uk_-_1574014.jpg/1280px-Regent%27s_Park_Mosque_-_geograph.org.uk_-_1574014.jpg",
             "is_live": True, "subscribers": 5612, "imam": "Cheikh Khalifa Ezzat",
             "description": "Regent's Park, dôme doré emblématique.", "latitude": 51.531, "longitude": -0.1675, "distance_km": 344.0},
            {"id": "msq-berlin", "name": "Sehitlik Moschee", "city": "Berlin", "country": "Allemagne",
             "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/91/Sehitlik-Moschee_2.jpg/1280px-Sehitlik-Moschee_2.jpg",
             "is_live": False, "subscribers": 1876, "imam": "Cheikh Mehmet Tekin",
             "description": "Minarets élancés à Neukölln, élégance ottomane.", "latitude": 52.4795, "longitude": 13.4097, "distance_km": 878.0},
            {"id": "msq-madrid", "name": "Mezquita M-30", "city": "Madrid", "country": "Espagne",
             "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b8/Centro_Cultural_Isl%C3%A1mico_de_Madrid_%28M-30%29_03.jpg/1280px-Centro_Cultural_Isl%C3%A1mico_de_Madrid_%28M-30%29_03.jpg",
             "is_live": False, "subscribers": 2230, "imam": "Cheikh Riay Tatary",
             "description": "Centre culturel et lieu de prière au cœur de l'Espagne.", "latitude": 40.4358, "longitude": -3.6585, "distance_km": 1052.0},
            {"id": "msq-roma", "name": "Moschea di Roma", "city": "Rome", "country": "Italie",
             "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4f/Moschea_di_Roma_-_Esterno_4.JPG/1280px-Moschea_di_Roma_-_Esterno_4.JPG",
             "is_live": True, "subscribers": 1789, "imam": "Cheikh Sami Salem",
             "description": "Plus grande mosquée d'Europe occidentale par superficie.", "latitude": 41.9395, "longitude": 12.4823, "distance_km": 1107.0},
            {"id": "msq-amsterdam", "name": "Westermoskee", "city": "Amsterdam", "country": "Pays-Bas",
             "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/05/Westermoskee_Aya_Sofya_Amsterdam.jpg/1280px-Westermoskee_Aya_Sofya_Amsterdam.jpg",
             "is_live": False, "subscribers": 987, "imam": "Cheikh Yassin Elforkani",
             "description": "Architecture contemporaine, communauté turque dynamique.", "latitude": 52.3702, "longitude": 4.8952, "distance_km": 432.0},
            {"id": "msq-strasbourg", "name": "Grande Mosquée de Strasbourg", "city": "Strasbourg", "country": "France",
             "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/22/Grande_mosqu%C3%A9e_de_Strasbourg_juillet_2015-04.jpg/1280px-Grande_mosqu%C3%A9e_de_Strasbourg_juillet_2015-04.jpg",
             "is_live": False, "subscribers": 1654, "imam": "Cheikh Mohamed Latrèche",
             "description": "Inaugurée en 2012, symbole d'harmonie alsacienne.", "latitude": 48.5841, "longitude": 7.761, "distance_km": 397.0},
        ])
    else:
        # Migration : mise à jour des photos vers les vraies images Wikimedia
        real_photos = {
            "msq-paris-gp": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/26/Grande_Mosqu%C3%A9e_de_Paris_n02.jpg/1280px-Grande_Mosqu%C3%A9e_de_Paris_n02.jpg",
            "msq-lyon": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/00/Grande_Mosqu%C3%A9e_de_Lyon_-_2.jpg/1280px-Grande_Mosqu%C3%A9e_de_Lyon_-_2.jpg",
            "msq-marseille": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4e/Mosqu%C3%A9e_de_Marseille.JPG/1280px-Mosqu%C3%A9e_de_Marseille.JPG",
            "msq-bxl": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0e/Brussels_Grand_Mosque_R01.jpg/1280px-Brussels_Grand_Mosque_R01.jpg",
            "msq-londres": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/45/Regent%27s_Park_Mosque_-_geograph.org.uk_-_1574014.jpg/1280px-Regent%27s_Park_Mosque_-_geograph.org.uk_-_1574014.jpg",
            "msq-berlin": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/91/Sehitlik-Moschee_2.jpg/1280px-Sehitlik-Moschee_2.jpg",
            "msq-madrid": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b8/Centro_Cultural_Isl%C3%A1mico_de_Madrid_%28M-30%29_03.jpg/1280px-Centro_Cultural_Isl%C3%A1mico_de_Madrid_%28M-30%29_03.jpg",
            "msq-roma": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4f/Moschea_di_Roma_-_Esterno_4.JPG/1280px-Moschea_di_Roma_-_Esterno_4.JPG",
            "msq-amsterdam": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/05/Westermoskee_Aya_Sofya_Amsterdam.jpg/1280px-Westermoskee_Aya_Sofya_Amsterdam.jpg",
            "msq-strasbourg": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/22/Grande_mosqu%C3%A9e_de_Strasbourg_juillet_2015-04.jpg/1280px-Grande_mosqu%C3%A9e_de_Strasbourg_juillet_2015-04.jpg",
        }
        for mid, url in real_photos.items():
            await db.mosques.update_one({"id": mid}, {"$set": {"image": url}})
        logger.info("✦ Mosques real photos migration applied")

    if await db.surahs.count_documents({}) == 0:
        await db.surahs.insert_many([
            {"id": "s-1", "number": 1, "name_ar": "الفاتحة", "name_translit": "Al-Fatiha", "name_fr": "L'Ouverture",
             "verses": 7, "audio_url": "https://server8.mp3quran.net/afs/001.mp3", "reciter": "Sheikh Mishary Al-Afasy",
             "cover": "https://images.unsplash.com/photo-1564769625905-50e93615e769?crop=entropy&cs=srgb&fm=jpg&q=85&w=600"},
            {"id": "s-2", "number": 2, "name_ar": "البقرة", "name_translit": "Al-Baqarah", "name_fr": "La Vache",
             "verses": 286, "audio_url": "https://server8.mp3quran.net/afs/002.mp3", "reciter": "Sheikh Mishary Al-Afasy",
             "cover": "https://images.unsplash.com/photo-1542379653-b204b1448bba?crop=entropy&cs=srgb&fm=jpg&q=85&w=600"},
            {"id": "s-36", "number": 36, "name_ar": "يس", "name_translit": "Yâ-Sîn", "name_fr": "Yâ-Sîn",
             "verses": 83, "audio_url": "https://server8.mp3quran.net/afs/036.mp3", "reciter": "Sheikh Mishary Al-Afasy",
             "cover": "https://images.unsplash.com/photo-1591825729269-caeb344f6df2?crop=entropy&cs=srgb&fm=jpg&q=85&w=600"},
            {"id": "s-55", "number": 55, "name_ar": "الرحمن", "name_translit": "Ar-Rahman", "name_fr": "Le Tout Miséricordieux",
             "verses": 78, "audio_url": "https://server8.mp3quran.net/afs/055.mp3", "reciter": "Sheikh Mishary Al-Afasy",
             "cover": "https://images.unsplash.com/photo-1564769625905-50e93615e769?crop=entropy&cs=srgb&fm=jpg&q=85&w=600"},
            {"id": "s-67", "number": 67, "name_ar": "الملك", "name_translit": "Al-Mulk", "name_fr": "La Royauté",
             "verses": 30, "audio_url": "https://server8.mp3quran.net/afs/067.mp3", "reciter": "Sheikh Mishary Al-Afasy",
             "cover": "https://images.unsplash.com/photo-1542379653-b204b1448bba?crop=entropy&cs=srgb&fm=jpg&q=85&w=600"},
            {"id": "s-112", "number": 112, "name_ar": "الإخلاص", "name_translit": "Al-Ikhlâs", "name_fr": "Le Monothéisme Pur",
             "verses": 4, "audio_url": "https://server8.mp3quran.net/afs/112.mp3", "reciter": "Sheikh Mishary Al-Afasy",
             "cover": "https://images.unsplash.com/photo-1591825729269-caeb344f6df2?crop=entropy&cs=srgb&fm=jpg&q=85&w=600"},
        ])

    if await db.recipes.count_documents({}) == 0:
        await db.recipes.insert_many([
            {"id": "r-dates-milk", "title": "Dattes & Lait Sunna", "category": "Petit-déj Sunna",
             "image": "https://images.unsplash.com/photo-1629738601425-494c3d6ba3e2?crop=entropy&cs=srgb&fm=jpg&q=85&w=800",
             "prep_time": "5 min", "blessed_ingredient": "Dattes Ajwa",
             "hadith": "Le Prophète ﷺ a dit : « Quiconque mange sept dattes Ajwa le matin, ni poison ni magie ne lui nuiront ce jour-là. » — Bukhari",
             "ingredients": ["7 dattes Ajwa", "1 verre de lait tiède", "1 cuillère de miel pur"],
             "steps": ["Faire tiédir le lait.", "Dénoyauter les dattes.", "Verser le lait, ajouter le miel."]},
            {"id": "r-honey-tea", "title": "Tisane au Miel & Nigelle", "category": "Remèdes",
             "image": "https://images.unsplash.com/photo-1556910103-1c02745aae4d?crop=entropy&cs=srgb&fm=jpg&q=85&w=800",
             "prep_time": "10 min", "blessed_ingredient": "Graines de Nigelle",
             "hadith": "« Dans la graine de nigelle, il y a une guérison contre toute maladie sauf la mort. » — Bukhari",
             "ingredients": ["1 tasse d'eau chaude", "1 cuillère de miel", "1/2 cuillère de nigelle", "Citron"],
             "steps": ["Faire bouillir l'eau.", "Ajouter la nigelle.", "Incorporer miel et citron."]},
            {"id": "r-ramadan-harira", "title": "Harira de Ramadan", "category": "Ramadan",
             "image": "https://images.unsplash.com/photo-1547592180-85f173990554?crop=entropy&cs=srgb&fm=jpg&q=85&w=800",
             "prep_time": "1h", "blessed_ingredient": "Huile d'olive",
             "hadith": "« Mangez l'huile d'olive et frictionnez-vous avec, car elle provient d'un arbre béni. » — Tirmidhi",
             "ingredients": ["Viande agneau", "Oignon", "Tomates", "Pois chiches", "Lentilles", "Coriandre", "Huile d'olive"],
             "steps": ["Revenir la viande.", "Ajouter légumes et épices.", "Cuire 45 min."]},
            {"id": "r-figs-sweet", "title": "Dessert aux Figues", "category": "Desserts",
             "image": "https://images.unsplash.com/photo-1571115764595-644a1f56a55c?crop=entropy&cs=srgb&fm=jpg&q=85&w=800",
             "prep_time": "20 min", "blessed_ingredient": "Figues",
             "hadith": "Allah a juré par la figue et l'olive. — Sourate At-Tin",
             "ingredients": ["8 figues fraîches", "Miel", "Yaourt grec", "Pistaches", "Cannelle"],
             "steps": ["Couper les figues.", "Dresser sur yaourt.", "Arroser de miel."]},
            {"id": "r-grilled-fish", "title": "Poisson Grillé aux Épices", "category": "Plats",
             "image": "https://images.unsplash.com/photo-1535007813616-79dc02ba4021?crop=entropy&cs=srgb&fm=jpg&q=85&w=800",
             "prep_time": "30 min", "blessed_ingredient": "Olives",
             "hadith": "« Les meilleures viandes sont celles que vous apportez à votre famille. »",
             "ingredients": ["Filet de poisson", "Citron", "Cumin", "Paprika", "Huile d'olive", "Olives"],
             "steps": ["Mariner.", "Griller 12 min.", "Servir."]},
        ])

    # Prophètes : 5 Ulul Azm avec contenu complet (Nuh, Ibrahim, Musa, Issa, Muhammad)
    from prophets_data import PROPHETS_FULL
    if await db.prophets.count_documents({}) == 0:
        await db.prophets.insert_many(PROPHETS_FULL)
        logger.info("✦ 5 prophètes Ulul Azm semés (Nuh, Ibrahim, Musa, Issa, Muhammad)")
    else:
        # Migration : on remplace l'ancien contenu léger par les 5 Ulul Azm complets
        await db.prophets.delete_many({})
        await db.prophets.insert_many(PROPHETS_FULL)
        logger.info("✦ Migration prophètes Ulul Azm appliquée")

    if await db.products.count_documents({}) == 0:
        await db.products.insert_many([
            {"id": "prod-misbaha", "name": "Misbaha bois d'olivier", "price": "24,90 €",
             "image": "https://images.unsplash.com/photo-1591825729269-caeb344f6df2?crop=entropy&cs=srgb&fm=jpg&q=85&w=600",
             "context": "dhikr", "recommendation": "Un frère nous conseille un chapelet de Jérusalem."},
            {"id": "prod-dates", "name": "Dattes Ajwa Médine 500g", "price": "29,00 €",
             "image": "https://images.unsplash.com/photo-1629738601425-494c3d6ba3e2?crop=entropy&cs=srgb&fm=jpg&q=85&w=600",
             "context": "recipes", "recommendation": "Un frère nous conseille les Ajwa authentiques."},
            {"id": "prod-honey", "name": "Miel de Sidr du Yémen", "price": "45,00 €",
             "image": "https://images.unsplash.com/photo-1556910103-1c02745aae4d?crop=entropy&cs=srgb&fm=jpg&q=85&w=600",
             "context": "recipes", "recommendation": "Un frère nous conseille ce miel rare."},
            {"id": "prod-rug", "name": "Tapis de prière premium", "price": "59,00 €",
             "image": "https://images.unsplash.com/photo-1564769625905-50e93615e769?crop=entropy&cs=srgb&fm=jpg&q=85&w=600",
             "context": "prayer", "recommendation": "Un frère nous conseille ce tapis velours."},
            {"id": "prod-attar", "name": "Parfum Attar Oud", "price": "39,00 €",
             "image": "https://images.unsplash.com/photo-1583245146398-3cab7d6e9c66?crop=entropy&cs=srgb&fm=jpg&q=85&w=600",
             "context": "sunna", "recommendation": "Un frère nous conseille cet attar sans alcool."},
            {"id": "prod-olive", "name": "Huile d'olive bio extra vierge", "price": "18,90 €",
             "image": "https://images.unsplash.com/photo-1547592180-85f173990554?crop=entropy&cs=srgb&fm=jpg&q=85&w=600",
             "context": "recipes", "recommendation": "Un frère nous conseille une huile bénie."},
        ])

    # Demo user
    if not await db.users.find_one({"email": "demo@adhan.com"}):
        await db.users.insert_one({
            "id": str(uuid.uuid4()), "email": "demo@adhan.com", "first_name": "Yusuf", "phone": "+33600000000",
            "hashed_password": hash_pw("Adhan2025!"),
            "subscription": {"plan": "FAMILLE", "status": "active", "stripe_id": None, "expires_at": None},
            "streak_days": 23, "last_checkin": datetime.now(timezone.utc).date().isoformat(),
            "favorite_mosque_id": "msq-paris-gp", "family_id": None, "avatar": None,
            "preferences": {"language": "FR", "prayer_method": 2, "night_mode": False},
            "role": "user", "created_at": datetime.now(timezone.utc).isoformat(),
        })

    # Admin user
    if not await db.users.find_one({"email": "admin@adhan.com"}):
        await db.users.insert_one({
            "id": str(uuid.uuid4()), "email": "admin@adhan.com", "first_name": "Admin",
            "hashed_password": hash_pw("Admin2025!"),
            "subscription": {"plan": "FAMILLE", "status": "active", "stripe_id": None, "expires_at": None},
            "streak_days": 365, "last_checkin": datetime.now(timezone.utc).date().isoformat(),
            "favorite_mosque_id": "msq-paris-gp", "family_id": None,
            "preferences": {"language": "FR", "prayer_method": 2}, "role": "super_admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })


@app.on_event("startup")
async def on_startup():
    try:
        await db.users.create_index("email", unique=True)
        await db.baf_devices.create_index("device_id", unique=True)
        await db.revoked_tokens.create_index("jti", unique=True)
    except Exception as e:
        logger.warning("index error: %s", e)
    await seed_database()
    logger.info("Adhan Connect API v1.3 ready ✦")


@app.on_event("shutdown")
async def on_shutdown():
    client.close()


# ─── Register routers & middleware ──────────────────────────────────────
app.include_router(api)
app.include_router(admin_mosque)
app.include_router(admin_super)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
