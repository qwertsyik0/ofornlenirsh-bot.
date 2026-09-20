from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import sqlite3

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # local SQLite fallback
    psycopg = None
    dict_row = None

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "ofornlenirsh_bot").strip().lstrip("@")
BASE_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or (
    hashlib.sha256((BOT_TOKEN + ":ofornlenirsh").encode()).hexdigest()[:40] if BOT_TOKEN else ""
)
DB_PATH = Path(os.getenv("DB_PATH", "/tmp/ofornlenirsh.sqlite3"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_TEXT = 3900

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ofornlenirsh")
app = FastAPI(title="Telegram Post Styler", version="1.1.0")
client: httpx.AsyncClient | None = None

PACK_RE = re.compile(r"(?:https?://)?t\.me/(?:addemoji|addstickers)/([A-Za-z0-9_]+)", re.I)
CMD_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", re.S)


# ---------- persistence ----------

class DBConn:
    def __init__(self) -> None:
        if DATABASE_URL:
            if psycopg is None:
                raise RuntimeError("DATABASE_URL is set but psycopg is not installed")
            self.kind = "pg"
            self.raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.kind = "sqlite"
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.raw = sqlite3.connect(DB_PATH)
            self.raw.row_factory = sqlite3.Row

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.kind == "pg" else sql

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()):
        return self.raw.execute(self._sql(sql), params)

    def executemany(self, sql: str, params):
        if self.kind == "pg":
            with self.raw.cursor() as cur:
                return cur.executemany(self._sql(sql), params)
        return self.raw.executemany(self._sql(sql), params)

    def executescript(self, script: str) -> None:
        if self.kind == "sqlite":
            self.raw.executescript(script)
            return
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self.raw.execute(statement)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            self.raw.close()


def connect() -> DBConn:
    return DBConn()


def init_db() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings(
              user_id BIGINT PRIMARY KEY,
              style TEXT NOT NULL DEFAULT 'shadow',
              intensity INTEGER NOT NULL DEFAULT 2,
              decor INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS drafts(
              user_id BIGINT PRIMARY KEY,
              text TEXT NOT NULL,
              variant INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS packs(
              user_id BIGINT NOT NULL,
              set_name TEXT NOT NULL,
              title TEXT NOT NULL,
              PRIMARY KEY(user_id, set_name)
            );
            CREATE TABLE IF NOT EXISTS emojis(
              user_id BIGINT NOT NULL,
              set_name TEXT NOT NULL,
              pos INTEGER NOT NULL,
              custom_emoji_id TEXT NOT NULL,
              fallback TEXT,
              PRIMARY KEY(user_id, set_name, pos)
            );
            """
        )


def settings(uid: int) -> dict[str, Any]:
    with connect() as con:
        row = con.execute("SELECT * FROM settings WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            con.execute("INSERT INTO settings(user_id) VALUES(?)", (uid,))
            return {"user_id": uid, "style": "shadow", "intensity": 2, "decor": 1}
        return dict(row)


def set_setting(uid: int, key: str, value: Any) -> None:
    if key not in {"style", "intensity", "decor"}:
        return
    settings(uid)
    with connect() as con:
        con.execute(f"UPDATE settings SET {key}=? WHERE user_id=?", (value, uid))


def save_draft(uid: int, text: str, variant: int = 0) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO drafts(user_id,text,variant) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET text=excluded.text, variant=excluded.variant",
            (uid, text, variant),
        )


def draft(uid: int) -> tuple[str, int] | None:
    with connect() as con:
        row = con.execute("SELECT text,variant FROM drafts WHERE user_id=?", (uid,)).fetchone()
    return (row["text"], int(row["variant"])) if row else None


def next_variant(uid: int) -> int:
    d = draft(uid)
    if not d:
        return 0
    v = d[1] + 1
    save_draft(uid, d[0], v)
    return v


# ---------- Telegram ----------

async def tg(method: str, payload: dict[str, Any] | None = None) -> Any:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")
    assert client is not None
    r = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload or {}, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description', 'Telegram API error')}")
    return data.get("result")


async def send(chat_id: int, text: str, *, entities: list[dict[str, Any]] | None = None,
               keyboard: dict[str, Any] | None = None, reply_to: int | None = None) -> Any:
    p: dict[str, Any] = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
    if entities:
        p["entities"] = entities
    if keyboard:
        p["reply_markup"] = keyboard
    if reply_to:
        p["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return await tg("sendMessage", p)


async def callback_answer(qid: str, text: str | None = None) -> None:
    try:
        await tg("answerCallbackQuery", {"callback_query_id": qid, **({"text": text} if text else {})})
    except Exception:
        log.exception("answerCallbackQuery failed")


# ---------- premium emoji packs ----------

async def import_pack(uid: int, raw: str) -> tuple[bool, str]:
    m = PACK_RE.search(raw.strip())
    if not m:
        return False, "Пришли ссылку вида https://t.me/addemoji/название_пака"
    set_name = m.group(1)
    try:
        st = await tg("getStickerSet", {"name": set_name})
    except Exception as e:
        return False, f"Не смог открыть пак: {e}"
    found: list[tuple[int, str, str]] = []
    for i, sticker in enumerate(st.get("stickers") or []):
        cid = sticker.get("custom_emoji_id")
        if cid:
            found.append((i, str(cid), str(sticker.get("emoji") or "✨")))
    if not found:
        return False, "В этом наборе Telegram не отдал custom_emoji_id. Нужен именно emoji pack."
    with connect() as con:
        con.execute(
            "INSERT INTO packs(user_id,set_name,title) VALUES(?,?,?) "
            "ON CONFLICT(user_id,set_name) DO UPDATE SET title=excluded.title",
            (uid, set_name, st.get("title") or set_name),
        )
        con.execute("DELETE FROM emojis WHERE user_id=? AND set_name=?", (uid, set_name))
        con.executemany(
            "INSERT INTO emojis(user_id,set_name,pos,custom_emoji_id,fallback) VALUES(?,?,?,?,?)",
            [(uid, set_name, p, cid, fb) for p, cid, fb in found],
        )
    return True, f"Добавил «{st.get('title') or set_name}»: {len(found)} premium emoji."


def pack_rows(uid: int) -> list[sqlite3.Row]:
    with connect() as con:
        return con.execute(
            "SELECT p.set_name,p.title,COUNT(e.pos) AS n FROM packs p "
            "LEFT JOIN emojis e ON e.user_id=p.user_id AND e.set_name=p.set_name "
            "WHERE p.user_id=? GROUP BY p.set_name,p.title ORDER BY p.title",
            (uid,),
        ).fetchall()


def emojis(uid: int) -> list[dict[str, Any]]:
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT custom_emoji_id,fallback,set_name,pos FROM emojis WHERE user_id=? ORDER BY set_name,pos",
            (uid,),
        ).fetchall()]


def delete_pack(uid: int, token: str) -> bool:
    rows = pack_rows(uid)
    target: str | None = None
    if token.isdigit() and 1 <= int(token) <= len(rows):
        target = rows[int(token)-1]["set_name"]
    else:
        for r in rows:
            if token.casefold() in {r["set_name"].casefold(), r["title"].casefold()}:
                target = r["set_name"]
                break
    if not target:
        return False
    with connect() as con:
        con.execute("DELETE FROM emojis WHERE user_id=? AND set_name=?", (uid, target))
        con.execute("DELETE FROM packs WHERE user_id=? AND set_name=?", (uid, target))
    return True


# ---------- formatting engine ----------

@dataclass
class Ent:
    type: str
    start: int
    end: int
    extra: dict[str, Any] | None = None


def u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def tg_entities(text: str, ents: list[Ent]) -> list[dict[str, Any]]:
    pref = [0]
    n = 0
    for ch in text:
        n += u16(ch)
        pref.append(n)
    out: list[dict[str, Any]] = []
    for e in ents:
        if not (0 <= e.start < e.end <= len(text)):
            continue
        item: dict[str, Any] = {"type": e.type, "offset": pref[e.start], "length": pref[e.end] - pref[e.start]}
        if e.extra:
            item.update(e.extra)
        out.append(item)
    out.sort(key=lambda x: (x["offset"], -x["length"]))
    return out


def line_ranges(text: str) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        out.append((pos, pos + len(raw), raw))
        pos += len(line)
    if text and not out:
        out.append((0, len(text), text))
    return out


def overlap(ents: list[Ent], s: int, e: int) -> bool:
    return any(max(x.start, s) < min(x.end, e) for x in ents)


DECOR = {
    "shadow": ["⌗", "⦿", "𖤐", "⛧", "☾", "⊹", "⋆", "𓆩", "𓆪"],
    "minimal": ["✦", "⟡", "◌", "⋆"],
}
KEYWORDS = [
    "без причины", "отдельно", "раньше времени", "поддержка", "накрутка", "скам",
    "мёртвые каналы", "мертвые каналы", "актив", "правила", "важно", "приз", "итоги",
]
HEADERS = {"dni", "правила", "админы", "admins", "вп", "мп", "faq", "итоги", "итог", "важно", "новости"}


def base_format(source: str, style: str, intensity: int, decor_on: bool, seed: int) -> tuple[str, list[Ent], list[int]]:
    intensity = max(1, min(3, int(intensity)))
    rows = line_ranges(source)
    nonempty = [(a,b,l) for a,b,l in rows if l.strip()]
    ents: list[Ent] = []

    if nonempty:
        a,b,l = nonempty[0]
        s = a + len(l) - len(l.lstrip())
        e = b - (len(l) - len(l.rstrip()))
        if e > s and e-s <= 120:
            ents.append(Ent("bold", s, e))
            if intensity >= 2 and e-s <= 70:
                ents.append(Ent("underline", s, e))

    for a,b,l in rows[1:]:
        clean = re.sub(r"^[\s#•·.\-–—]+", "", l).strip()
        normalized = re.sub(r"[^\wА-Яа-яЁё]+", "", clean).casefold()
        if clean and len(clean) <= 50 and (normalized in HEADERS or (clean.isupper() and len(clean) <= 28)):
            s = a + l.find(clean)
            if not overlap(ents, s, s+len(clean)):
                ents.append(Ent("bold", s, s+len(clean)))

    if intensity >= 2:
        low = source.casefold()
        budget = 2 if intensity == 2 else 4
        for kw in KEYWORDS:
            i = low.find(kw)
            if i >= 0 and budget and not overlap(ents, i, i+len(kw)):
                ents.append(Ent("underline", i, i+len(kw)))
                budget -= 1

    if intensity >= 3:
        used = 0
        for a,b,l in nonempty[1:]:
            c = l.strip()
            if 8 <= len(c) <= 80:
                s = a + l.find(c)
                if not overlap(ents, s, s+len(c)):
                    ents.append(Ent("italic", s, s+len(c)))
                    used += 1
                    if used == 2:
                        break

    prefix = ""
    if decor_on and source.strip() and style != "minimal":
        rng = random.Random(seed)
        chars = DECOR.get(style, DECOR["shadow"])
        prefix = (rng.choice(chars) + "  ") if intensity == 1 else (
            rng.choice(chars) + " " + rng.choice(chars) + "  " if intensity == 2 else
            rng.choice(chars) + " " + rng.choice(chars) + " .. "
        )
    elif decor_on and source.strip() and intensity >= 2:
        rng = random.Random(seed)
        prefix = rng.choice(DECOR["minimal"]) + "  "

    shifted = [Ent(e.type, e.start+len(prefix), e.end+len(prefix), e.extra) for e in ents]
    text = prefix + source
    points: list[int] = []
    if source.strip():
        points.append(len(prefix) + len(source) - len(source.lstrip()))
        if intensity >= 2:
            for a,_,l in nonempty[1:]:
                points.append(len(prefix) + a + len(l) - len(l.lstrip()))
                if len(points) >= (2 if intensity == 2 else 4):
                    break
    return text, shifted, points


def add_custom(text: str, ents: list[Ent], points: list[int], pool: list[dict[str, Any]], count: int, seed: int) -> tuple[str, list[Ent]]:
    if not pool or not points or count <= 0:
        return text, ents
    rng = random.Random(seed ^ 0x5F3759DF)
    sample = pool[:]
    rng.shuffle(sample)
    chosen = sample[:min(count, len(points), len(sample))]
    ops: list[tuple[int,str,str]] = []
    for p,item in zip(points, chosen):
        fb = (item.get("fallback") or "✨").strip() or "✨"
        if len(fb) > 8:
            fb = "✨"
        ops.append((p, fb + " ", str(item["custom_emoji_id"])))
    for p,ins,cid in sorted(ops, reverse=True):
        delta = len(ins)
        for e in ents:
            if e.start >= p:
                e.start += delta
                e.end += delta
            elif e.end > p:
                e.end += delta
        text = text[:p] + ins + text[p:]
        ents.append(Ent("custom_emoji", p, p+len(ins.rstrip()), {"custom_emoji_id": cid}))
    return text, ents


def format_post(uid: int, source: str, variant: int = 0, override: int | None = None, custom: bool = True) -> tuple[str, list[dict[str, Any]]]:
    s = settings(uid)
    intensity = int(override or s["intensity"])
    seed = (uid * 1009 + variant * 7919 + sum(map(ord, source[:180]))) & 0x7fffffff
    text, ents, points = base_format(source, s["style"], intensity, bool(s["decor"]), seed)
    if custom:
        text, ents = add_custom(text, ents, points, emojis(uid), {1:1,2:2,3:4}[intensity], seed)
    if len(text) > 4096:
        text, ents, _ = base_format(source, s["style"], intensity, False, seed)
    return text, tg_entities(text, ents)


# ---------- UI ----------

KB = {"inline_keyboard": [
    [{"text": "♻️ Иначе", "callback_data": "reroll"}, {"text": "💎 Больше", "callback_data": "more"}, {"text": "◌ Меньше", "callback_data": "less"}],
    [{"text": "🖤 Shadow", "callback_data": "style:shadow"}, {"text": "◻️ Minimal", "callback_data": "style:minimal"}],
]}

START = (
    "Пришли готовый текст поста. Я не переписываю слова, не исправляю формулировки и не добавляю новые фразы. "
    "Только оформляю: жирный, курсив, подчёркивание, декоративные символы и premium/custom emoji из твоих паков.\n\n"
    "Чтобы добавить emoji pack, просто пришли ссылку t.me/addemoji/...\n\n"
    "/packs — мои паки\n/delpack <номер> — удалить пак\n/style shadow|minimal\n/intensity 1|2|3\n/decor on|off"
)


async def command(chat: int, uid: int, mid: int, name: str, arg: str) -> None:
    c, arg = name.casefold(), (arg or "").strip()
    if c in {"start", "help"}:
        await send(chat, START, reply_to=mid)
        return
    if c == "addpack":
        _, msg = await import_pack(uid, arg)
        await send(chat, msg, reply_to=mid)
        return
    if c == "packs":
        rows = pack_rows(uid)
        txt = "Паков пока нет. Пришли ссылку t.me/addemoji/..." if not rows else "Твои паки:\n" + "\n".join(f"{i}. {r['title']} — {r['n']} emoji" for i,r in enumerate(rows,1))
        await send(chat, txt, reply_to=mid)
        return
    if c == "delpack":
        await send(chat, "Удалил." if delete_pack(uid,arg) else "Не нашёл такой пак.", reply_to=mid)
        return
    if c == "style":
        if arg not in {"shadow","minimal"}:
            await send(chat, "Используй /style shadow или /style minimal", reply_to=mid)
            return
        set_setting(uid,"style",arg)
        await send(chat, f"Стиль: {arg}.", reply_to=mid)
        return
    if c == "intensity":
        if arg not in {"1","2","3"}:
            await send(chat, "Используй /intensity 1, 2 или 3", reply_to=mid)
            return
        set_setting(uid,"intensity",int(arg))
        await send(chat, f"Насыщенность: {arg}.", reply_to=mid)
        return
    if c == "decor":
        if arg.casefold() not in {"on","off"}:
            await send(chat, "Используй /decor on или /decor off", reply_to=mid)
            return
        set_setting(uid,"decor",1 if arg.casefold()=="on" else 0)
        await send(chat, "Готово.", reply_to=mid)
        return
    await send(chat, "Не знаю такую команду. /help", reply_to=mid)


async def on_message(m: dict[str, Any]) -> None:
    text = m.get("text")
    chat = (m.get("chat") or {}).get("id")
    uid = (m.get("from") or {}).get("id")
    mid = m.get("message_id")
    if not isinstance(text,str) or not isinstance(chat,int) or not isinstance(uid,int):
        return
    cm = CMD_RE.match(text)
    if cm:
        await command(chat,uid,mid,cm.group(1),cm.group(2) or "")
        return
    if PACK_RE.search(text.strip()):
        _, msg = await import_pack(uid,text)
        await send(chat,msg,reply_to=mid)
        return
    if len(text) > MAX_TEXT:
        await send(chat, f"Слишком длинно: {len(text)} символов. Максимум сейчас {MAX_TEXT}.", reply_to=mid)
        return
    save_draft(uid,text,0)
    out, ent = format_post(uid,text)
    try:
        await send(chat,out,entities=ent,keyboard=KB,reply_to=mid)
    except Exception as e:
        log.warning("custom formatting failed, retry without custom emoji: %s", e)
        out, ent = format_post(uid,text,custom=False)
        await send(chat,out,entities=ent,keyboard=KB,reply_to=mid)


async def on_callback(q: dict[str, Any]) -> None:
    qid = q.get("id")
    uid = (q.get("from") or {}).get("id")
    chat = ((q.get("message") or {}).get("chat") or {}).get("id")
    data = q.get("data") or ""
    if not isinstance(uid,int) or not isinstance(chat,int):
        if qid:
            await callback_answer(qid)
        return
    d = draft(uid)
    if not d:
        await callback_answer(qid,"Сначала пришли текст.")
        return
    text,_ = d
    override = None
    if data == "reroll":
        v = next_variant(uid)
    elif data in {"more","less"}:
        s = settings(uid)
        cur = int(s["intensity"])
        val = min(3,cur+1) if data=="more" else max(1,cur-1)
        set_setting(uid,"intensity",val)
        override = val
        v = next_variant(uid)
    elif data.startswith("style:") and data.split(":",1)[1] in {"shadow","minimal"}:
        set_setting(uid,"style",data.split(":",1)[1])
        v = next_variant(uid)
    else:
        await callback_answer(qid)
        return
    out, ent = format_post(uid,text,v,override)
    try:
        await send(chat,out,entities=ent,keyboard=KB)
        await callback_answer(qid,"Готово")
    except Exception:
        out, ent = format_post(uid,text,v,override,custom=False)
        await send(chat,out,entities=ent,keyboard=KB)
        await callback_answer(qid,"Готово")


async def process(update: dict[str, Any]) -> None:
    try:
        if "message" in update:
            await on_message(update["message"])
        elif "callback_query" in update:
            await on_callback(update["callback_query"])
    except Exception:
        log.exception("update failed")


@app.on_event("startup")
async def startup() -> None:
    global client
    init_db()
    client = httpx.AsyncClient()
    if not BOT_TOKEN:
        log.error("BOT_TOKEN missing")
        return
    try:
        me = await tg("getMe")
        log.info("connected as @%s", me.get("username"))
        await tg("setMyCommands", {"commands": [
            {"command":"start","description":"начать"},
            {"command":"addpack","description":"добавить premium emoji pack"},
            {"command":"packs","description":"мои emoji packs"},
            {"command":"delpack","description":"удалить emoji pack"},
            {"command":"style","description":"стиль оформления"},
            {"command":"intensity","description":"насыщенность 1–3"},
            {"command":"decor","description":"декоративные символы"},
            {"command":"help","description":"помощь"},
        ]})
        if BASE_URL and WEBHOOK_SECRET:
            await tg("setWebhook", {
                "url": BASE_URL + "/telegram/webhook",
                "secret_token": WEBHOOK_SECRET,
                "allowed_updates": ["message","callback_query"],
                "drop_pending_updates": False,
            })
            log.info("webhook set: %s/telegram/webhook", BASE_URL)
        else:
            log.warning("No RENDER_EXTERNAL_URL/WEBHOOK_URL, webhook not configured")
    except Exception:
        log.exception("startup Telegram setup failed")


@app.on_event("shutdown")
async def shutdown() -> None:
    global client
    if client:
        await client.aclose()
        client = None


@app.get("/")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "ofornlenirsh-bot", "bot": "@" + BOT_USERNAME}


@app.post("/telegram/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict[str,bool]:
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="invalid webhook secret")
    update = await request.json()
    asyncio.create_task(process(update))
    return {"ok": True}
