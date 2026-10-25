import os
import re
import uuid
import math
import secrets
import string
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from urllib.parse import urlencode

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Header, Request
from fastapi.responses import RedirectResponse, PlainTextResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from brainrots import BRAINROTS, BY_NAME, MUTATIONS  # noqa: E402
import lua_scripts  # noqa: E402

client = AsyncIOMotorClient(os.environ["MONGO_URL"], tz_aware=True)
db = client[os.environ["DB_NAME"]]

JWT_SECRET = os.environ["JWT_SECRET"]
OWNER_DISCORD_ID = os.environ["OWNER_DISCORD_ID"]


def is_owner(u):
    return u.get("discord_id") == OWNER_DISCORD_ID
BOT_TOKEN = os.environ["BOT_API_TOKEN"]
FRONTEND_URL = os.environ["FRONTEND_URL"]
PUBLIC_API = os.environ["PUBLIC_API"].rstrip("/")
DISCORD_CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
DISCORD_CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
DISCORD_REDIRECT_URI = os.environ["DISCORD_REDIRECT_URI"]
MOCK_LOGIN = os.environ["MOCK_LOGIN"].lower() == "true"
DISCORD_API = "https://discord.com/api/v10"
PLACE_ID = 109983668079237

DIVISIONS = {
    "king": {"name": "King", "capacity": 6, "price": 5.0, "tiers": ["og", "peak", "high", "mid", "low"],
             "desc": "Access to every log: OG, Peak, High and Mid."},
    "slave": {"name": "Slave", "capacity": 10, "price": 3.0, "tiers": ["high", "mid", "low"],
              "desc": "Access to High and Mid logs."},
    "russian": {"name": "Russian", "capacity": 12, "price": 1.25, "tiers": ["mid", "low"],
                "desc": "Access to Mid logs."},
}
MAX_HOURS = 24
MAX_RENT_PRICE = 5.25
DEFAULT_SETTINGS = {"system_frozen": False, "frozen_divisions": [], "whitelist_only": False,
                    "global_blacklist": [], "discord_invite": "", "owner_selfbot": False,
                    "min_log_value": 1_000_000}

app = FastAPI()
api = APIRouter(prefix="/api")
logger = logging.getLogger("horloge")


def now():
    return datetime.now(timezone.utc)


def clean(doc):
    if doc is None:
        return None
    out = {}
    for k, v in doc.items():
        if k == "_id":
            continue
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


def gen_code(prefix, groups=3):
    alphabet = string.ascii_uppercase + string.digits
    return prefix + "-" + "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(groups))


def parse_income(text: str) -> Optional[float]:
    m = re.search(r"([\d.,]+)\s*([KMBT]?)", (text or "").replace("$", "").upper())
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return num * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[m.group(2)]


def tier_for(rarity: str, value: float) -> str:
    if rarity == "OG":
        return "og"
    if value >= 100e6:
        return "peak"
    if value >= 10e6:
        return "high"
    if value >= 1e6:
        return "mid"
    return "low"


def fmt_value(v: float) -> str:
    for div, s in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"${v / div:.2f}{s}/s"
    return f"${v:.0f}/s"


async def get_settings():
    doc = await db.settings.find_one({"_id": "global"}) or {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in doc.items() if k != "_id"}}


# ---------------- Auth ----------------
def make_token(user_id: str) -> str:
    return jwt.encode({"sub": user_id, "exp": now() + timedelta(days=7)}, JWT_SECRET, algorithm="HS256")


async def current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    try:
        uid = jwt.decode(authorization[7:], JWT_SECRET, algorithms=["HS256"])["sub"]
    except (jwt.PyJWTError, KeyError):
        raise HTTPException(401, "Invalid or expired session")
    user = await db.users.find_one({"id": uid}, {"_id": 0})
    if not user:
        raise HTTPException(401, "User not found")
    if user.get("banned"):
        raise HTTPException(403, "You are banned")
    user["is_admin"] = is_owner(user)
    return user


async def admin_user(user=Depends(current_user)):
    if not user["is_admin"]:
        raise HTTPException(403, "Owner only")
    return user


async def upsert_user(discord_id, username, display_name, avatar):
    existing = await db.users.find_one({"discord_id": discord_id})
    if existing:
        await db.users.update_one({"discord_id": discord_id}, {"$set": {
            "username": username, "display_name": display_name, "avatar": avatar}})
        if discord_id == OWNER_DISCORD_ID and not existing.get("key"):
            await db.users.update_one({"discord_id": discord_id}, {"$set": {"key": gen_code("HJ", 4), "whitelisted": True}})
        return existing["id"]
    uid = str(uuid.uuid4())
    owner = discord_id == OWNER_DISCORD_ID
    await db.users.insert_one({
        "id": uid, "discord_id": discord_id, "username": username, "display_name": display_name,
        "avatar": avatar, "balance": 0.0, "total_spent": 0.0,
        "key": gen_code("HJ", 4) if owner else None, "hwid": None, "hwid_reset_at": None,
        "whitelisted": owner, "banned": False,
        "filters": {"blacklist": [], "whitelist": [], "min_value": 0, "auto_join": True, "self_bot": False},
        "created_at": now(),
    })
    return uid


@api.get("/auth/config")
async def auth_config():
    return {"discord_enabled": bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET), "mock_enabled": MOCK_LOGIN and not (DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET)}


@api.get("/auth/discord/login")
async def discord_login():
    if not DISCORD_CLIENT_ID:
        raise HTTPException(400, "Discord OAuth not configured")
    state = secrets.token_urlsafe(32)
    await db.oauth_states.insert_one({"_id": state, "expires_at": now() + timedelta(minutes=10)})
    params = {"client_id": DISCORD_CLIENT_ID, "response_type": "code", "redirect_uri": DISCORD_REDIRECT_URI,
              "scope": "identify", "state": state, "prompt": "none"}
    return RedirectResponse("https://discord.com/oauth2/authorize?" + urlencode(params), status_code=307)


@api.get("/auth/discord/callback")
async def discord_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    fail = f"{FRONTEND_URL}/login?error="
    if error or not code or not state:
        return RedirectResponse(fail + "cancelled", status_code=303)
    saved = await db.oauth_states.find_one_and_delete({"_id": state})
    if not saved or saved["expires_at"] < now():
        return RedirectResponse(fail + "state", status_code=303)
    async with httpx.AsyncClient(timeout=10) as hc:
        tr = await hc.post(f"{DISCORD_API}/oauth2/token", data={
            "client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code", "code": code, "redirect_uri": DISCORD_REDIRECT_URI,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if tr.is_error:
            logger.error("Discord token exchange failed: %s", tr.text)
            return RedirectResponse(fail + "token", status_code=303)
        pr = await hc.get(f"{DISCORD_API}/users/@me",
                          headers={"Authorization": f"Bearer {tr.json()['access_token']}"})
        if pr.is_error:
            return RedirectResponse(fail + "profile", status_code=303)
        p = pr.json()
    avatar = f"https://cdn.discordapp.com/avatars/{p['id']}/{p['avatar']}.png" if p.get("avatar") else None
    uid = await upsert_user(p["id"], p["username"], p.get("global_name") or p["username"], avatar)
    return RedirectResponse(f"{FRONTEND_URL}/auth/callback#token={make_token(uid)}", status_code=303)


class MockLogin(BaseModel):
    username: str = Field(min_length=2, max_length=32)


@api.post("/auth/mock-login")
async def mock_login(body: MockLogin):
    if not MOCK_LOGIN or (DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET):
        raise HTTPException(403, "Demo login disabled")
    name = body.username.strip()
    uid = await upsert_user(f"mock-{name.lower()}", name, name, None)
    return {"token": make_token(uid)}


# ---------------- Slots helpers ----------------
def holder(slot):
    if slot.get("rented_to") and slot.get("rent_until") and slot["rent_until"] > now():
        return slot["rented_to"]
    return slot["user_id"]


async def live_slots(division=None):
    q = {"expires_at": {"$gt": now()}}
    if division:
        q["division"] = division
    return await db.slots.find(q, {"_id": 0}).to_list(500)


def slot_end_for(slot, uid):
    return slot["rent_until"] if holder(slot) != slot["user_id"] and holder(slot) == uid else slot["expires_at"]


async def user_access(user):
    """Returns list of {division, expires_at, slot_id, rented} the user currently holds."""
    if is_owner(user):
        return [{"division": d, "expires_at": None, "slot_id": None, "infinite": True} for d in DIVISIONS]
    out = []
    for s in await live_slots():
        if holder(s) == user["id"]:
            out.append({"division": s["division"], "expires_at": slot_end_for(s, user["id"]).isoformat(),
                        "slot_id": s["id"], "rented": s["user_id"] != user["id"]})
    return out


def allowed_tiers(access):
    tiers = set()
    for a in access:
        tiers.update(DIVISIONS[a["division"]]["tiers"])
    return tiers


def public_user(u):
    d = clean(u)
    d["is_admin"] = is_owner(u)
    return d


async def add_tx(user_id, type_, amount, note, extra=None):
    await db.transactions.insert_one({"id": str(uuid.uuid4()), "user_id": user_id, "type": type_,
                                      "amount": round(amount, 2), "note": note, "created_at": now(), **(extra or {})})


async def charge(uid, cost):
    r = await db.users.update_one({"id": uid, "balance": {"$gte": cost - 1e-9}},
                                  {"$inc": {"balance": -cost, "total_spent": cost}})
    if r.modified_count == 0:
        raise HTTPException(400, "Insufficient balance")


async def ensure_key(user):
    if not user.get("key"):
        await db.users.update_one({"id": user["id"]}, {"$set": {"key": gen_code("HJ", 4)}})


@api.get("/auth/me")
async def me(user=Depends(current_user)):
    rented_out = [{"division": s["division"], "rent_until": s["rent_until"].isoformat()}
                  for s in await live_slots() if s["user_id"] == user["id"] and holder(s) != user["id"]]
    return {**public_user(user), "access": await user_access(user), "rented_out": rented_out}


@api.get("/settings/public")
async def public_settings():
    s = await get_settings()
    return {k: s[k] for k in ("system_frozen", "frozen_divisions", "whitelist_only", "discord_invite")}


@api.get("/divisions")
async def divisions(user=Depends(current_user)):
    s = await get_settings()
    slots = await live_slots()
    users = {u["id"]: u for u in await db.users.find(
        {"id": {"$in": list({holder(x) for x in slots})}}, {"_id": 0, "id": 1, "username": 1, "avatar": 1, "display_name": 1}
    ).to_list(500)}
    out = []
    for key, d in DIVISIONS.items():
        occ = []
        for x in slots:
            if x["division"] != key:
                continue
            h = users.get(holder(x), {})
            occ.append({"slot_id": x["id"], "username": h.get("display_name") or h.get("username", "?"),
                        "avatar": h.get("avatar"), "expires_at": slot_end_for(x, holder(x)).isoformat(),
                        "rented": holder(x) != x["user_id"]})
        out.append({"id": key, **d, "used": len(occ), "available": d["capacity"] - len(occ),
                    "frozen": s["system_frozen"] or key in s["frozen_divisions"], "occupants": occ})
    return out


class Purchase(BaseModel):
    division: str
    hours: int = Field(ge=1, le=MAX_HOURS)


@api.post("/slots/purchase")
async def purchase(body: Purchase, user=Depends(current_user)):
    if body.division not in DIVISIONS:
        raise HTTPException(400, "Unknown division")
    s = await get_settings()
    if s["system_frozen"] or body.division in s["frozen_divisions"]:
        raise HTTPException(423, "Division frozen")
    if s["whitelist_only"] and not user.get("whitelisted"):
        raise HTTPException(403, "Whitelist only: ask the owner to whitelist you")
    d = DIVISIONS[body.division]
    slots = await live_slots(body.division)
    mine = next((x for x in slots if x["user_id"] == user["id"]), None)
    if mine:
        new_exp = mine["expires_at"] + timedelta(hours=body.hours)
        if new_exp - now() > timedelta(hours=MAX_HOURS, minutes=1):
            raise HTTPException(400, f"Max {MAX_HOURS}h of remaining time per slot")
    elif len(slots) >= d["capacity"]:
        raise HTTPException(409, "No slot available in this division")
    cost = round(d["price"] * body.hours, 2)
    await charge(user["id"], cost)
    await ensure_key(user)
    if mine:
        await db.slots.update_one({"id": mine["id"]}, {"$set": {"expires_at": new_exp}})
    else:
        await db.slots.insert_one({"id": str(uuid.uuid4()), "user_id": user["id"], "division": body.division,
                                   "started_at": now(), "expires_at": now() + timedelta(hours=body.hours),
                                   "rented_to": None, "rent_until": None})
    await add_tx(user["id"], "purchase", -cost, f"{d['name']} slot · {body.hours}h")
    return {"ok": True, "cost": cost}


# ---------------- Key ----------------
@api.post("/key/reset-hwid")
async def reset_hwid(user=Depends(current_user)):
    if not user.get("key"):
        raise HTTPException(400, "You don't have a key yet")
    last = user.get("hwid_reset_at")
    if last and not user["is_admin"] and now() - last < timedelta(minutes=30):
        raise HTTPException(429, "HWID reset available every 30 minutes")
    await db.users.update_one({"id": user["id"]}, {"$set": {"hwid": None, "hwid_reset_at": now()}})
    return {"ok": True}


@api.post("/key/regen")
async def regen_key(user=Depends(current_user)):
    if not user.get("key"):
        raise HTTPException(400, "You don't have a key yet")
    key = gen_code("HJ", 4)
    await db.users.update_one({"id": user["id"]}, {"$set": {"key": key, "hwid": None}})
    return {"key": key}


class Transfer(BaseModel):
    username: str
    division: str


@api.post("/key/transfer")
async def transfer(body: Transfer, user=Depends(current_user)):
    target = await db.users.find_one({"username": {"$regex": f"^{re.escape(body.username.strip())}$", "$options": "i"}},
                                     {"_id": 0})
    if not target or target["id"] == user["id"]:
        raise HTTPException(404, "Target user not found")
    slots = await live_slots(body.division)
    mine = next((x for x in slots if x["user_id"] == user["id"] and holder(x) == user["id"]), None)
    if not mine:
        raise HTTPException(400, "No active slot (not rented out) in this division")
    if any(target["id"] in (holder(x), x["user_id"]) for x in slots):
        raise HTTPException(400, "Target already owns a slot in this division")
    await ensure_key(target)
    await db.slots.update_one({"id": mine["id"]}, {"$set": {"user_id": target["id"]}})
    await db.market.update_many({"slot_id": mine["id"], "status": "active"}, {"$set": {"status": "cancelled"}})
    await add_tx(user["id"], "transfer", 0, f"Transferred {DIVISIONS[body.division]['name']} slot to {target['username']}")
    await add_tx(target["id"], "transfer", 0, f"Received {DIVISIONS[body.division]['name']} slot from {user['username']}")
    return {"ok": True}


# ---------------- Gift cards ----------------
class Redeem(BaseModel):
    code: str


@api.post("/giftcards/redeem")
async def redeem(body: Redeem, user=Depends(current_user)):
    card = await db.giftcards.find_one_and_update(
        {"code": body.code.strip().upper(), "redeemed_by": None},
        {"$set": {"redeemed_by": user["username"], "redeemed_at": now()}})
    if not card:
        raise HTTPException(404, "Invalid or already used gift card")
    await db.users.update_one({"id": user["id"]}, {"$inc": {"balance": card["amount"]}})
    await add_tx(user["id"], "deposit", card["amount"], f"Gift card {card['code']}")
    return {"ok": True, "amount": card["amount"]}


# ---------------- Filters ----------------
class Filters(BaseModel):
    blacklist: List[str] = []
    whitelist: List[str] = []
    min_value: float = 0
    auto_join: bool = True
    self_bot: bool = False


@api.put("/me/filters")
async def set_filters(body: Filters, user=Depends(current_user)):
    await db.users.update_one({"id": user["id"]}, {"$set": {"filters": body.model_dump()}})
    return body.model_dump()


# ---------------- Logs ----------------
async def insert_log(name, mutation, job_id, place_id, players, generation="", source="bot"):
    b = BY_NAME.get((name or "").strip().lower())
    mutation = mutation if mutation in MUTATIONS else "Normal"
    base = b["income"] if b else 0
    gen_value = parse_income(generation) if "/s" in (generation or "") else None
    value = gen_value if gen_value is not None else base * MUTATIONS[mutation]
    rarity = b["rarity"] if b else "Unknown"
    s = await get_settings()
    if name.strip().lower() in [x.lower() for x in s["global_blacklist"]]:
        return None
    tier = tier_for(rarity, value)
    if rarity != "OG" and value < s["min_log_value"]:
        return None
    dup = {"job_id": job_id, "name": b["name"] if b else name.strip(), "mutation": mutation,
           "created_at": {"$gte": now() - timedelta(minutes=10)}}
    if await db.logs.find_one(dup, {"_id": 1}):
        return None
    doc = {"id": str(uuid.uuid4()), "name": b["name"] if b else name.strip(), "rarity": rarity,
           "image": b["image"] if b else None, "mutation": mutation, "value": value, "value_text": fmt_value(value),
           "tier": tier, "job_id": job_id, "place_id": place_id or PLACE_ID, "players": players,
           "source": source, "created_at": now()}
    await db.logs.insert_one(doc)
    return doc


class Found(BaseModel):
    name: str
    mutation: str = "Normal"
    generation: str = ""


class Ingest(BaseModel):
    job_id: str
    place_id: int = PLACE_ID
    players: int = 0
    brainrots: List[Found] = []


@api.post("/logs/ingest")
async def ingest(body: Ingest, x_bot_token: Optional[str] = Header(None)):
    if x_bot_token != BOT_TOKEN:
        raise HTTPException(401, "Invalid bot token")
    n = 0
    for f in body.brainrots[:120]:
        if await insert_log(f.name, f.mutation, body.job_id, body.place_id, body.players, f.generation, "bot"):
            n += 1
    await db.selfbot_runs.insert_one({"id": str(uuid.uuid4()), "job_id": body.job_id, "place_id": body.place_id,
                                      "scanned": len(body.brainrots), "logged": n, "source": "bot", "created_at": now()})
    return {"inserted": n}


def log_view(log, tiers):
    d = clean(log)
    d["locked"] = log["tier"] not in tiers
    if d["locked"]:
        d["job_id"] = None
    return d


@api.get("/logs")
async def get_logs(limit: int = 60, tier: Optional[str] = None, user=Depends(current_user)):
    q = {"tier": tier} if tier else {}
    logs = await db.logs.find(q, {"_id": 0}).sort("created_at", -1).to_list(min(limit, 200))
    tiers = allowed_tiers(await user_access(user))
    return [log_view(x, tiers) for x in logs]


@api.get("/logs/recent")
async def recent_logs():
    logs = await db.logs.find({}, {"_id": 0, "job_id": 0}).sort("created_at", -1).to_list(8)
    return [clean(x) for x in logs]


@api.get("/stats")
async def stats(user=Depends(current_user)):
    since = now() - timedelta(hours=24)
    logs = await db.logs.find({"created_at": {"$gte": since}}, {"_id": 0, "job_id": 0}).sort("created_at", -1).to_list(5000)
    hourly = [0] * 24
    counts = {"og": 0, "peak": 0, "high": 0, "mid": 0}
    recent = {"og": [], "peak": [], "high": []}
    for x in logs:
        idx = 23 - min(23, int((now() - x["created_at"]).total_seconds() // 3600))
        hourly[idx] += 1
        counts[x["tier"]] = counts.get(x["tier"], 0) + 1
        if x["tier"] in recent and len(recent[x["tier"]]) < 8:
            recent[x["tier"]].append(clean(x))
    chart = [{"label": "Now" if i == 23 else f"-{23 - i}h", "count": c} for i, c in enumerate(hourly)]
    return {"chart": chart, "counts": counts, "recent": recent, "total": len(logs)}


@api.get("/leaderboard")
async def leaderboard(user=Depends(current_user)):
    top = await db.users.find({"total_spent": {"$gt": 0}}, {"_id": 0, "username": 1, "display_name": 1, "avatar": 1,
                                                           "total_spent": 1}).sort("total_spent", -1).to_list(20)
    pipeline = [{"$match": {"created_at": {"$gte": now() - timedelta(hours=24)}}},
                {"$group": {"_id": "$name", "count": {"$sum": 1}, "image": {"$first": "$image"},
                            "best": {"$max": "$value"}}},
                {"$sort": {"count": -1}}, {"$limit": 10}]
    found = [{"name": x["_id"], "count": x["count"], "image": x["image"], "best": fmt_value(x["best"])}
             async for x in db.logs.aggregate(pipeline)]
    return {"spenders": top, "brainrots": found}


@api.get("/brainrots")
async def brainrot_index():
    return {"brainrots": BRAINROTS, "mutations": MUTATIONS}


# ---------------- Marketplace ----------------
class ListingIn(BaseModel):
    division: str
    type: str = Field(pattern="^(rent|sell)$")
    price: float = Field(gt=0)
    hours: int = Field(default=1, ge=1, le=MAX_HOURS)


def remaining_hours(slot):
    return max(0, math.ceil(((slot["expires_at"] - now()).total_seconds() - 60) / 3600))


@api.post("/market/listings")
async def create_listing(body: ListingIn, user=Depends(current_user)):
    if body.division not in DIVISIONS:
        raise HTTPException(400, "Unknown division")
    slot = next((x for x in await live_slots(body.division) if x["user_id"] == user["id"] and holder(x) == user["id"]), None)
    if not slot:
        raise HTTPException(400, "You need an active key in this division to create a listing")
    rem = remaining_hours(slot)
    if rem < 1:
        raise HTTPException(400, "Less than 1 hour remaining on your slot")
    if body.type == "rent":
        if body.price > MAX_RENT_PRICE:
            raise HTTPException(400, f"Max rent price is ${MAX_RENT_PRICE}/h")
        if body.hours > rem:
            raise HTTPException(400, f"You only have {rem}h remaining")
    elif body.price > MAX_RENT_PRICE * rem:
        raise HTTPException(400, f"Max sell price is ${MAX_RENT_PRICE * rem:.2f} for {rem}h")
    if await db.market.find_one({"slot_id": slot["id"], "status": "active"}):
        raise HTTPException(400, "You already have an active listing for this slot")
    doc = {"id": str(uuid.uuid4()), "seller_id": user["id"], "seller": user.get("display_name") or user["username"],
           "seller_avatar": user.get("avatar"), "slot_id": slot["id"], "division": body.division, "type": body.type,
           "price": round(body.price, 2), "hours": body.hours if body.type == "rent" else rem,
           "status": "active", "created_at": now()}
    await db.market.insert_one(doc)
    return clean(doc)


@api.get("/market/listings")
async def list_listings(type: str = "rent", division: Optional[str] = None, user=Depends(current_user)):
    q = {"status": "active", "type": type}
    if division:
        q["division"] = division
    out = []
    for x in await db.market.find(q, {"_id": 0}).sort("created_at", -1).to_list(200):
        slot = await db.slots.find_one({"id": x["slot_id"]}, {"_id": 0})
        if not slot or slot["expires_at"] <= now() or slot["user_id"] != x["seller_id"] or holder(slot) != x["seller_id"]:
            if not slot or slot["expires_at"] <= now() or slot["user_id"] != x["seller_id"]:
                await db.market.update_one({"id": x["id"]}, {"$set": {"status": "expired"}})
            continue
        d = clean(x)
        d["remaining_hours"] = remaining_hours(slot)
        d["mine"] = x["seller_id"] == user["id"]
        out.append(d)
    return out


@api.delete("/market/listings/{lid}")
async def cancel_listing(lid: str, user=Depends(current_user)):
    r = await db.market.update_one({"id": lid, "seller_id": user["id"], "status": "active"},
                                   {"$set": {"status": "cancelled"}})
    if not r.modified_count:
        raise HTTPException(404, "Listing not found")
    return {"ok": True}


class BuyIn(BaseModel):
    hours: int = Field(default=1, ge=1, le=MAX_HOURS)


@api.post("/market/listings/{lid}/buy")
async def buy_listing(lid: str, body: BuyIn, user=Depends(current_user)):
    s = await get_settings()
    if s["system_frozen"]:
        raise HTTPException(423, "System frozen")
    lst = await db.market.find_one({"id": lid, "status": "active"}, {"_id": 0})
    if not lst:
        raise HTTPException(404, "Listing not available")
    if lst["seller_id"] == user["id"]:
        raise HTTPException(400, "You can't buy your own listing")
    slot = await db.slots.find_one({"id": lst["slot_id"]}, {"_id": 0})
    if not slot or slot["expires_at"] <= now() or holder(slot) != lst["seller_id"]:
        raise HTTPException(400, "Seller's slot is not available")
    if any(user["id"] in (holder(x), x["user_id"]) for x in await live_slots(lst["division"])):
        raise HTTPException(400, "You already have access in this division")
    div = DIVISIONS[lst["division"]]["name"]
    if lst["type"] == "rent":
        hours = min(body.hours, lst["hours"], remaining_hours(slot))
        if hours < 1:
            raise HTTPException(400, "Not enough hours available")
        cost = round(lst["price"] * hours, 2)
        await charge(user["id"], cost)
        await ensure_key(user)
        await db.slots.update_one({"id": slot["id"]}, {"$set": {"rented_to": user["id"],
                                                               "rent_until": min(now() + timedelta(hours=hours), slot["expires_at"])}})
        await db.market.update_one({"id": lid}, {"$set": {"status": "filled", "buyer": user["username"]}})
        note = f"Rented {div} key · {hours}h"
    else:
        cost = lst["price"]
        await charge(user["id"], cost)
        await ensure_key(user)
        await db.slots.update_one({"id": slot["id"]}, {"$set": {"user_id": user["id"], "rented_to": None,
                                                               "rent_until": None}})
        await db.market.update_one({"id": lid}, {"$set": {"status": "sold", "buyer": user["username"]}})
        note = f"Bought {div} key · {remaining_hours(slot)}h"
    await db.users.update_one({"id": lst["seller_id"]}, {"$inc": {"balance": cost}})
    extra = {"market": True, "division": lst["division"]}
    await add_tx(user["id"], "market_buy", -cost, note + f" from {lst['seller']}", extra)
    await add_tx(lst["seller_id"], "market_sale", cost, note + f" to {user['username']}", extra)
    return {"ok": True, "cost": cost}


@api.get("/me/transactions")
async def my_transactions(user=Depends(current_user)):
    txs = await db.transactions.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return [clean(x) for x in txs]


# ---------------- Lua script endpoints ----------------
@api.get("/script")
async def my_script(user=Depends(current_user)):
    access = await user_access(user)
    if not access or not user.get("key"):
        return {"locked": True}
    loader = f'getgenv().script_key = "{user["key"]}"\nloadstring(game:HttpGet("{PUBLIC_API}/script/client.lua"))()'
    out = {"locked": False, "loader": loader, "key": user["key"], "hwid_bound": bool(user.get("hwid"))}
    if user["is_admin"]:
        out["selfbot_loader"] = (f'getgenv().script_key = "{user["key"]}"\n'
                                 f'loadstring(game:HttpGet("{PUBLIC_API}/script/selfbot.lua"))()')
    return out


@api.get("/script/client.lua")
async def client_lua(request: Request):
    user_agent = request.headers.get("user-agent", "").lower()

    if "roblox" not in user_agent:
        return RedirectResponse(
            url="https://horloge-joiner-6mp8.vercel.app/",
            status_code=302
        )

    return PlainTextResponse(
        content=lua_scripts.render(lua_scripts.CLIENT, PUBLIC_API)
    )










@api.get("/script/selfbot.lua", response_class=PlainTextResponse)
async def selfbot_lua():
    return lua_scripts.render(lua_scripts.OWNER_SELFBOT, PUBLIC_API)


@api.get("/script/bot.lua", response_class=PlainTextResponse)
async def bot_lua(token: str):
    if token != BOT_TOKEN:
        raise HTTPException(401, "Invalid bot token")
    return lua_scripts.render(lua_scripts.BOT, PUBLIC_API, BOT_TOKEN)


class ScriptAuth(BaseModel):
    key: str
    hwid: str


async def script_user(body: ScriptAuth):
    user = await db.users.find_one({"key": body.key}, {"_id": 0})
    if not user or user.get("banned"):
        raise HTTPException(401, "Invalid key")
    if not user.get("hwid"):
        await db.users.update_one({"id": user["id"]}, {"$set": {"hwid": body.hwid}})
    elif user["hwid"] != body.hwid:
        raise HTTPException(403, "HWID mismatch - reset your HWID on the panel")
    user["is_admin"] = is_owner(user)
    return user


@api.post("/script/auth")
async def script_auth(body: ScriptAuth):
    user = await script_user(body)
    access = await user_access(user)
    if not access:
        raise HTTPException(402, "No active slot")
    return {"username": user["username"], "divisions": [a["division"] for a in access],
            "self_bot": user["filters"].get("self_bot", False), "auto_join": user["filters"].get("auto_join", True),
            "server_time": now().isoformat()}


class ScriptSettings(ScriptAuth):
    auto_join: bool


@api.post("/script/settings")
async def script_settings(body: ScriptSettings):
    user = await script_user(body)
    await db.users.update_one({"id": user["id"]}, {"$set": {"filters.auto_join": body.auto_join}})
    return {"auto_join": body.auto_join}


class Poll(ScriptAuth):
    since: Optional[str] = None


@api.post("/script/poll")
async def script_poll(body: Poll):
    user = await script_user(body)
    s = await get_settings()
    access = await user_access(user)
    t = now()
    if not access or s["system_frozen"]:
        return {"logs": [], "auto_join": False, "server_time": t.isoformat()}
    tiers = allowed_tiers(access)
    try:
        since = datetime.fromisoformat(body.since) if body.since else t - timedelta(seconds=8)
    except ValueError:
        since = t - timedelta(seconds=8)
    since = max(since, t - timedelta(seconds=8))
    f = user["filters"]
    bl = {x.lower() for x in f.get("blacklist", [])}
    wl = {x.lower() for x in f.get("whitelist", [])}
    q = {"created_at": {"$gt": since}, "tier": {"$in": list(tiers)}, "value": {"$gte": f.get("min_value", 0)}}
    logs = []
    for x in await db.logs.find(q, {"_id": 0}).sort("value", -1).to_list(50):
        n = x["name"].lower()
        if n in bl or (wl and n not in wl):
            continue
        logs.append({k: x.get(k) for k in ("id", "name", "mutation", "value_text", "tier", "job_id", "place_id", "image")})
    return {"logs": logs, "auto_join": f.get("auto_join", True), "server_time": t.isoformat()}


class Report(ScriptAuth):
    job_id: str
    place_id: int = PLACE_ID
    players: int = 0
    brainrots: List[Found] = []
    selfbot: bool = False


@api.post("/script/report")
async def script_report(body: Report):
    user = await script_user(body)
    if not await user_access(user):
        raise HTTPException(402, "No active slot")
    n = 0
    for f in body.brainrots[:120]:
        if await insert_log(f.name, f.mutation, body.job_id, body.place_id, body.players, f.generation,
                            f"selfbot:{user['username']}"):
            n += 1
    if body.selfbot and user["is_admin"]:
        await db.selfbot_runs.insert_one({"id": str(uuid.uuid4()), "job_id": body.job_id, "place_id": body.place_id,
                                          "scanned": len(body.brainrots), "logged": n, "source": "selfbot", "created_at": now()})
        return {"inserted": n, **await selfbot_stats()}
    return {"inserted": n}


async def selfbot_stats():
    agg = await db.selfbot_runs.aggregate([{"$match": {"source": {"$ne": "bot"}}}, {"$group": {"_id": None, "runs": {"$sum": 1}, "logged": {"$sum": "$logged"}}}]).to_list(1)
    return {"runs": agg[0]["runs"], "logged": agg[0]["logged"]} if agg else {"runs": 0, "logged": 0}


@api.post("/script/selfbot/status")
async def selfbot_status(body: ScriptAuth):
    user = await script_user(body)
    if not user["is_admin"]:
        raise HTTPException(403, "Owner only")
    return {"enabled": (await get_settings())["owner_selfbot"], **await selfbot_stats()}


class SelfbotSet(ScriptAuth):
    enabled: bool


@api.post("/script/selfbot/set")
async def selfbot_set(body: SelfbotSet):
    user = await script_user(body)
    if not user["is_admin"]:
        raise HTTPException(403, "Owner only")
    await db.settings.update_one({"_id": "global"}, {"$set": {"owner_selfbot": body.enabled}}, upsert=True)
    return {"enabled": body.enabled}


# ---------------- Admin ----------------
@api.get("/admin/overview")
async def admin_overview(user=Depends(admin_user)):
    runs = await db.selfbot_runs.find({}, {"_id": 0}).sort("created_at", -1).to_list(15)
    return {
        "settings": await get_settings(),
        "users": await db.users.count_documents({}),
        "active_slots": len(await live_slots()),
        "logs_24h": await db.logs.count_documents({"created_at": {"$gte": now() - timedelta(hours=24)}}),
        "giftcards_unused": await db.giftcards.count_documents({"redeemed_by": None}),
        "selfbot_runs": [clean(r) for r in runs],
        "bot_loader": f'loadstring(game:HttpGet("{PUBLIC_API}/script/bot.lua?token={BOT_TOKEN}"))()',
        "ingest_url": f"{PUBLIC_API}/logs/ingest",
    }


class SettingsIn(BaseModel):
    system_frozen: Optional[bool] = None
    frozen_divisions: Optional[List[str]] = None
    whitelist_only: Optional[bool] = None
    global_blacklist: Optional[List[str]] = None
    discord_invite: Optional[str] = None
    owner_selfbot: Optional[bool] = None
    min_log_value: Optional[float] = None


@api.put("/admin/settings")
async def update_settings(body: SettingsIn, user=Depends(admin_user)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    await db.settings.update_one({"_id": "global"}, {"$set": patch}, upsert=True)
    return await get_settings()


@api.get("/admin/users")
async def admin_users(user=Depends(admin_user)):
    users = await db.users.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return [public_user(u) for u in users]


class UserPatch(BaseModel):
    add_balance: Optional[float] = None
    balance: Optional[float] = None
    whitelisted: Optional[bool] = None
    banned: Optional[bool] = None
    reset_hwid: Optional[bool] = None


@api.patch("/admin/users/{uid}")
async def admin_patch_user(uid: str, body: UserPatch, user=Depends(admin_user)):
    target = await db.users.find_one({"id": uid}, {"_id": 0})
    if not target:
        raise HTTPException(404, "User not found")
    upd = {}
    if body.balance is not None:
        upd["balance"] = round(max(0, body.balance), 2)
    if body.whitelisted is not None:
        upd["whitelisted"] = body.whitelisted
    if body.banned is not None and not is_owner(target):
        upd["banned"] = body.banned
    if body.reset_hwid:
        upd["hwid"] = None
    op = {"$set": upd} if upd else {}
    if body.add_balance:
        op["$inc"] = {"balance": round(body.add_balance, 2)}
        await add_tx(uid, "admin", body.add_balance, "Balance adjusted by owner")
    if op:
        await db.users.update_one({"id": uid}, op)
    return public_user(await db.users.find_one({"id": uid}, {"_id": 0}))


class GiftIn(BaseModel):
    amount: float = Field(gt=0, le=1000)
    count: int = Field(default=1, ge=1, le=50)


@api.post("/admin/giftcards")
async def create_giftcards(body: GiftIn, user=Depends(admin_user)):
    docs = [{"id": str(uuid.uuid4()), "code": gen_code("HJGIFT", 3), "amount": round(body.amount, 2),
             "created_by": user["username"], "created_at": now(), "redeemed_by": None, "redeemed_at": None}
            for _ in range(body.count)]
    await db.giftcards.insert_many(docs)
    return [clean(d) for d in docs]


@api.get("/admin/giftcards")
async def list_giftcards(user=Depends(admin_user)):
    return [clean(x) for x in await db.giftcards.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)]


@api.delete("/admin/giftcards/{gid}")
async def delete_giftcard(gid: str, user=Depends(admin_user)):
    await db.giftcards.delete_one({"id": gid, "redeemed_by": None})
    return {"ok": True}


@api.get("/admin/slots")
async def admin_slots(user=Depends(admin_user)):
    slots = await live_slots()
    users = {u["id"]: u["username"] for u in await db.users.find({}, {"_id": 0, "id": 1, "username": 1}).to_list(2000)}
    return [{**clean(x), "owner": users.get(x["user_id"]), "holder": users.get(holder(x))} for x in slots]


@api.delete("/admin/slots/{sid}")
async def revoke_slot(sid: str, user=Depends(admin_user)):
    await db.slots.update_one({"id": sid}, {"$set": {"expires_at": now()}})
    await db.market.update_many({"slot_id": sid, "status": "active"}, {"$set": {"status": "cancelled"}})
    return {"ok": True}


@app.on_event("startup")
async def startup():
    await db.users.create_index("id", unique=True)
    await db.users.create_index("discord_id", unique=True)
    await db.logs.create_index("created_at")
    await db.slots.create_index("expires_at")
    await db.giftcards.create_index("code", unique=True)
    await db.oauth_states.create_index("expires_at", expireAfterSeconds=0)


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

















@api.get("/script/client.lua")
async def client_lua(request: Request):
    user_agent = request.headers.get("user-agent", "").lower()

    # Si le User-Agent contient "roblox", renvoyer le script Lua
    if "roblox" in user_agent:
        return PlainTextResponse(content=lua_scripts.CLIENT)

    # Sinon, rediriger vers la page d'accueil
    return RedirectResponse(
        url="https://horloge-joiner-6mp8.vercel.app/",
        status_code=302
    )





