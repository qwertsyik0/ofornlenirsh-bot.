from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import sqlite3
import unicodedata

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
              decor INTEGER NOT NULL DEFAULT 1,
              underline INTEGER NOT NULL DEFAULT 0,
              fonts INTEGER NOT NULL DEFAULT 1,
              quotes INTEGER NOT NULL DEFAULT 1,
              strike INTEGER NOT NULL DEFAULT 1
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
        migrations = {
            "underline": "INTEGER NOT NULL DEFAULT 0",
            "fonts": "INTEGER NOT NULL DEFAULT 1",
            "quotes": "INTEGER NOT NULL DEFAULT 1",
            "strike": "INTEGER NOT NULL DEFAULT 1",
        }
        if con.kind == "pg":
            for col, ddl in migrations.items():
                con.execute(f"ALTER TABLE settings ADD COLUMN IF NOT EXISTS {col} {ddl}")
        else:
            present = {r["name"] for r in con.execute("PRAGMA table_info(settings)").fetchall()}
            for col, ddl in migrations.items():
                if col not in present:
                    con.execute(f"ALTER TABLE settings ADD COLUMN {col} {ddl}")


def settings(uid: int) -> dict[str, Any]:
    with connect() as con:
        row = con.execute("SELECT * FROM settings WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            con.execute("INSERT INTO settings(user_id) VALUES(?)", (uid,))
            return {"user_id": uid, "style": "shadow", "intensity": 2, "decor": 1, "underline": 0, "fonts": 1, "quotes": 1, "strike": 1}
        return dict(row)


def set_setting(uid: int, key: str, value: Any) -> None:
    if key not in {"style", "intensity", "decor", "underline", "fonts", "quotes", "strike"}:
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
            "SELECT e.custom_emoji_id,e.fallback,e.set_name,e.pos,p.title "
            "FROM emojis e LEFT JOIN packs p ON p.user_id=e.user_id AND p.set_name=e.set_name "
            "WHERE e.user_id=? ORDER BY e.set_name,e.pos",
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
    seen: set[tuple[Any, ...]] = set()
    for e in ents:
        if not (0 <= e.start < e.end <= len(text)):
            continue
        item: dict[str, Any] = {
            "type": e.type,
            "offset": pref[e.start],
            "length": pref[e.end] - pref[e.start],
        }
        if e.extra:
            item.update(e.extra)
        sig = (item["type"], item["offset"], item["length"], item.get("custom_emoji_id"))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(item)
    out.sort(key=lambda x: (x["offset"], -x["length"], x["type"]))
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


def overlap(ents: list[Ent], s: int, e: int, types: set[str] | None = None) -> bool:
    for x in ents:
        if types is not None and x.type not in types:
            continue
        if max(x.start, s) < min(x.end, e):
            return True
    return False


DECOR = {
    "shadow": ["⌗", "⦿", "𖤐", "⛧", "☾", "⊹", "⋆", "𓆩", "𓆪", "♱", "◌"],
    "minimal": ["✦", "⟡", "◌", "⋆", "·"],
}

HEADERS = {
    "dni", "правила", "админы", "admins", "вп", "мп", "faq", "итоги", "итог",
    "важно", "новости", "условия", "набор", "розыгрыш", "объявление", "анонс",
}

SEMANTIC_WORDS: dict[str, tuple[str, ...]] = {
    "warning": ("важно", "внимание", "осторож", "предупреж", "нельзя", "запрещ", "наруш", "ошиб"),
    "gift": ("подар", "приз", "розыгрыш", "побед", "награ", "бонус"),
    "money": ("деньг", "цена", "стоим", "руб", "звезд", "stars", "оплат", "скид", "покуп", "магаз"),
    "heart": ("люб", "серд", "спасибо", "благодар", "поддерж", "забот", "мил"),
    "dark": ("тень", "dark", "shadow", "ноч", "чёрн", "черн", "мрак"),
    "eye": ("смотр", "глаз", "вид", "наблю", "след"),
    "fire": ("огон", "гор", "жар", "хайп", "жёст", "жест"),
    "sad": ("груст", "плач", "боль", "жаль", "плохо"),
    "happy": ("рад", "счаст", "ура", "круто", "поздрав"),
    "time": ("сегодня", "завтра", "вчера", "срок", "врем", "дата", "день", "час"),
    "info": ("инфо", "новост", "объяв", "анонс", "сообщ", "подроб"),
    "question": ("вопрос", "почему", "зачем", "как ", "кто ", "что "),
    "people": ("админ", "участ", "человек", "команд", "пользоват", "кандидат"),
    "shop": ("шоп", "shop", "товар", "каталог", "куп", "продаж"),
    "wallet": ("wallet", "кошел", "баланс", "лапкоин", "лк"),
    "star": ("звезд", "star", "премиум", "premium"),
    "secret": ("секрет", "спойлер", "сюрприз", "скоро", "тайн"),
    "arrow": ("сюда", "ниже", "далее", "ссылка", "переход", "жми", "нажм"),
}

EMOJI_NAME_HINTS: dict[str, tuple[str, ...]] = {
    "warning": ("warning", "exclamation", "alert", "prohibited", "cross mark"),
    "gift": ("gift", "wrapped", "trophy", "medal", "party", "confetti"),
    "money": ("money", "coin", "dollar", "bank", "credit", "cash"),
    "heart": ("heart", "love", "kiss"),
    "dark": ("black", "bat", "vampire", "coffin", "dark"),
    "eye": ("eye",),
    "fire": ("fire", "flame"),
    "sad": ("cry", "sad", "tear", "broken heart"),
    "happy": ("smil", "grin", "joy", "party"),
    "time": ("clock", "calendar", "hourglass", "watch"),
    "info": ("information", "newspaper", "speaker", "megaphone", "bell"),
    "question": ("question",),
    "people": ("person", "people", "family", "bust"),
    "shop": ("shopping", "cart", "bag", "store"),
    "wallet": ("wallet", "purse"),
    "star": ("star", "spark", "glow"),
    "secret": ("shushing", "zipper", "lock", "key"),
    "arrow": ("arrow", "triangle", "pointer"),
    "skull": ("skull", "bones"),
    "moon": ("moon",),
}

EMPHASIS_PHRASES = (
    "без причины", "отдельно", "раньше времени", "поддержка", "накрутка", "скам",
    "мёртвые каналы", "мертвые каналы", "актив", "правила", "важно", "приз",
    "итоги", "условия", "скидка", "бонус", "бесплатно", "ограничено",
)

MONO_UP = {chr(ord("A") + i): chr(0x1D670 + i) for i in range(26)}
MONO_LOW = {chr(ord("a") + i): chr(0x1D68A + i) for i in range(26)}
MONO_DIG = {chr(ord("0") + i): chr(0x1D7F6 + i) for i in range(10)}
MONO_MAP = {**MONO_UP, **MONO_LOW, **MONO_DIG}


def semantic_tags(text: str) -> set[str]:
    low = text.casefold()
    tags: set[str] = set()
    for tag, words in SEMANTIC_WORDS.items():
        if any(w in low for w in words):
            tags.add(tag)
    if re.search(r"\b\d{1,4}\b", text):
        tags.add("time" if any(w in low for w in ("день", "час", "минут", "сегодня", "завтра")) else "info")
    if "!" in text:
        tags.add("warning")
    if "?" in text:
        tags.add("question")
    return tags


def emoji_tags(item: dict[str, Any]) -> set[str]:
    fb = str(item.get("fallback") or "")
    pack = f"{item.get('set_name') or ''} {item.get('title') or ''}".casefold()
    names = " ".join(unicodedata.name(ch, "") for ch in fb).casefold()
    raw = f"{fb} {pack} {names}"
    tags: set[str] = set()
    for tag, hints in EMOJI_NAME_HINTS.items():
        if any(h in raw for h in hints):
            tags.add(tag)
    if any(ch in fb for ch in "❤️🩷🖤🤍💜💙💚💛🧡💘💝💖💕💞"):
        tags.add("heart")
    if any(ch in fb for ch in "✨⭐🌟💫✦✧⋆"):
        tags.add("star")
    if any(ch in fb for ch in "🎁🏆🥇🥈🥉🎉"):
        tags.add("gift")
    if any(ch in fb for ch in "💰💸💳🪙💵"):
        tags.add("money")
    if any(ch in fb for ch in "👁👀"):
        tags.add("eye")
    if any(ch in fb for ch in "🔥"):
        tags.add("fire")
    if any(ch in fb for ch in "🌙🌚🌑"):
        tags.add("moon")
        tags.add("dark")
    if any(ch in fb for ch in "☠💀🦇⚰"):
        tags.add("dark")
        tags.add("skull")
    if any(ch in fb for ch in "⚠❗‼⛔"):
        tags.add("warning")
    if len(fb.strip()) == 1 and fb.strip().isalnum():
        tags.add("letter")
    if "letter" in pack or "alphabet" in pack or "букв" in pack:
        tags.add("letter")
    if any(x in pack for x in ("dark", "shadow", "black", "goth", "emo")):
        tags.add("dark")
    if any(x in pack for x in ("pink", "love", "heart", "cute", "nyan")):
        tags.add("heart")
    return tags


def apply_text_font(text: str, start: int, end: int, intensity: int, seed: int) -> str:
    if intensity < 2 or start >= end:
        return text
    segment = text[start:end]
    words = list(re.finditer(r"[A-Za-z0-9]{3,24}", segment))
    if not words:
        return text
    rng = random.Random(seed ^ 0xA53C)
    chosen = words[0] if intensity == 2 else rng.choice(words[: min(3, len(words))])
    a, b = start + chosen.start(), start + chosen.end()
    styled = "".join(MONO_MAP.get(ch, ch) for ch in text[a:b])
    # Every ASCII character maps to exactly one Unicode code point, so Python indexes stay stable.
    return text[:a] + styled + text[b:]


def best_quote(rows: list[tuple[int, int, str]], ents: list[Ent], intensity: int, seed: int) -> tuple[int, int] | None:
    if intensity < 2 or len(rows) < 3:
        return None
    ranked: list[tuple[int, int, int]] = []
    for idx, (a, b, line) in enumerate(rows[1:], 1):
        clean = line.strip()
        if not (18 <= len(clean) <= 220):
            continue
        if re.match(r"^[•·\-–—#]", clean):
            continue
        score = 0
        low = clean.casefold()
        if any(w in low for w in ("важно", "главное", "помни", "услов", "итог", "если ", "почему", "обратите", "учти")):
            score += 4
        if clean.endswith((".", "!", "?")):
            score += 1
        if idx >= len(rows) - 2:
            score += 1
        if not overlap(ents, a, b, {"code", "pre"}):
            ranked.append((score, a, b))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    score, a, b = ranked[0]
    rng = random.Random(seed ^ 0xB10C)
    threshold = 3 if intensity == 2 else 1
    if score >= threshold or (intensity == 3 and rng.random() < 0.45):
        line = next((l for x, y, l in rows if x == a and y == b), "")
        left = len(line) - len(line.lstrip())
        right = len(line.rstrip())
        return a + left, a + right
    return None


def context_at(text: str, p: int) -> str:
    a = text.rfind("\n", 0, max(0, p)) + 1
    b = text.find("\n", p)
    if b < 0:
        b = len(text)
    return text[a:b]


def choose_custom_emoji(pool: list[dict[str, Any]], wanted: set[str], style: str,
                        used: set[str], rng: random.Random) -> dict[str, Any] | None:
    candidates = [x for x in pool if str(x.get("custom_emoji_id")) not in used]
    if not candidates:
        return None
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in candidates:
        tags = emoji_tags(item)
        score = 0.0
        score += 5.0 * len(tags & wanted)
        if style == "shadow" and tags & {"dark", "eye", "moon", "skull"}:
            score += 2.2
        if style == "minimal" and tags & {"star", "info", "arrow"}:
            score += 1.5
        if "letter" in tags:
            score -= 3.5
        if not tags:
            score -= 0.5
        score += rng.random() * 0.8
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored[0][0] < 0.2:
        nonletters = [x for x in candidates if "letter" not in emoji_tags(x)]
        return rng.choice(nonletters or candidates)
    return scored[0][1]


def base_format(source: str, style: str, intensity: int, decor_on: bool,
                underline_on: bool, fonts_on: bool, quotes_on: bool,
                strike_on: bool, seed: int) -> tuple[str, list[Ent], list[int]]:
    intensity = max(1, min(3, int(intensity)))
    rng = random.Random(seed)
    rows = line_ranges(source)
    nonempty = [(a, b, l) for a, b, l in rows if l.strip()]
    ents: list[Ent] = []
    title_span: tuple[int, int] | None = None
    text = source

    # Title: bold, optionally a real Unicode text font on Latin/digits.
    if nonempty:
        a, b, line = nonempty[0]
        ts = a + len(line) - len(line.lstrip())
        te = b - (len(line) - len(line.rstrip()))
        if te > ts and te - ts <= 120:
            title_span = (ts, te)
            if fonts_on:
                text = apply_text_font(text, ts, te, intensity, seed)
            ents.append(Ent("bold", ts, te))

    # Secondary headings.
    for a, b, line in rows[1:]:
        clean = re.sub(r"^[\s#•·.\-–—]+", "", line).strip()
        normalized = re.sub(r"[^\wА-Яа-яЁё]+", "", clean).casefold()
        if clean and len(clean) <= 50 and (normalized in HEADERS or (clean.isupper() and len(clean) <= 28)):
            p = a + line.find(clean)
            if not overlap(ents, p, p + len(clean), {"code", "pre"}):
                ents.append(Ent("bold", p, p + len(clean)))

    # Semantic emphasis. Important phrases are bold, but not every post gets the same treatment.
    if intensity >= 2:
        low = source.casefold()
        candidates: list[tuple[int, int]] = []
        for phrase in EMPHASIS_PHRASES:
            for m in re.finditer(re.escape(phrase), low):
                candidates.append((m.start(), m.end()))
        candidates.sort()
        budget = 1 if intensity == 2 else 2
        rng.shuffle(candidates)
        for a, b in candidates:
            if budget <= 0:
                break
            if not overlap(ents, a, b, {"code", "pre"}):
                ents.append(Ent("bold", a, b))
                budget -= 1

    # Italics only when a line reads like explanatory/descriptive copy.
    if intensity >= 2:
        scored: list[tuple[int, int, int]] = []
        for a, b, line in nonempty[1:]:
            clean = line.strip()
            if not (12 <= len(clean) <= 180) or re.match(r"^[•·\-–—#]", clean):
                continue
            tags = semantic_tags(clean)
            score = (2 if tags & {"heart", "sad", "happy", "secret", "info"} else 0) + (1 if clean.endswith(".") else 0)
            scored.append((score, a + len(line) - len(line.lstrip()), a + len(line.rstrip())))
        if scored:
            scored.sort(reverse=True)
            _, a, b = scored[0]
            if not overlap(ents, a, b, {"code", "pre", "blockquote"}):
                ents.append(Ent("italic", a, b))

    # A quote is selected from a meaningful standalone line, not blindly on every message.
    if quotes_on:
        q = best_quote(rows, ents, intensity, seed)
        if q:
            ents.append(Ent("blockquote", q[0], q[1]))

    # Sparse aesthetic strike-through: one letter in the title, never a whole meaningful word.
    if strike_on and intensity >= 2 and title_span:
        a, b = title_span
        letters = [i for i in range(a + 1, max(a + 1, b - 1)) if text[i].isalnum()]
        chance = 0.45 if intensity == 2 else 0.8
        if letters and rng.random() < chance:
            i = rng.choice(letters)
            ents.append(Ent("strikethrough", i, i + 1))

    # Underline remains opt-in and rare.
    if underline_on and intensity >= 3:
        low = source.casefold()
        for kw in ("важно", "итоги", "правила", "условия"):
            i = low.find(kw)
            if i >= 0:
                ents.append(Ent("underline", i, i + len(kw)))
                break

    # Spoiler only where the wording itself implies a spoiler/secret.
    if intensity >= 3:
        low = source.casefold()
        for kw in ("спойлер", "секрет", "сюрприз", "скоро"):
            i = low.find(kw)
            if i >= 0 and not overlap(ents, i, i + len(kw), {"code", "pre"}):
                ents.append(Ent("spoiler", i, i + len(kw)))
                break

    # Telegram monospace for explicitly technical tokens. This is another text font, not a design image.
    for m in re.finditer(r"(?<!\w)(@[A-Za-z0-9_]{4,32}|/[A-Za-z0-9_]{2,32})(?!\w)", text):
        if not overlap(ents, m.start(), m.end(), {"code", "pre"}):
            ents.append(Ent("code", m.start(), m.end()))

    # Decorative prefix is allowed, but wording remains untouched.
    prefix = ""
    if decor_on and source.strip():
        chars = DECOR.get(style, DECOR["shadow"])
        if style == "minimal":
            if intensity >= 2:
                prefix = rng.choice(chars) + "  "
        else:
            if intensity == 1:
                prefix = rng.choice(chars) + "  "
            elif intensity == 2:
                prefix = rng.choice(chars) + " " + rng.choice(chars) + "  "
            else:
                prefix = rng.choice(chars) + " " + rng.choice(chars) + " .. "

    shifted = [Ent(e.type, e.start + len(prefix), e.end + len(prefix), e.extra) for e in ents]
    text = prefix + text

    # Candidate emoji insertion points. The actual emoji is selected semantically later.
    points: list[int] = []
    if source.strip():
        points.append(len(prefix) + len(source) - len(source.lstrip()))
        if intensity >= 2:
            for a, _, line in nonempty[1:]:
                clean = line.strip()
                if not clean:
                    continue
                points.append(len(prefix) + a + len(line) - len(line.lstrip()))
                if len(points) >= (2 if intensity == 2 else 4):
                    break
    return text, shifted, points


def add_custom(text: str, ents: list[Ent], points: list[int], pool: list[dict[str, Any]],
               count: int, seed: int, style: str) -> tuple[str, list[Ent]]:
    if not pool or not points or count <= 0:
        return text, ents
    rng = random.Random(seed ^ 0x5F3759DF)
    global_tags = semantic_tags(text)
    used: set[str] = set()
    ops: list[tuple[int, str, str]] = []

    for p in points[:count]:
        local = context_at(text, p)
        wanted = semantic_tags(local) | global_tags
        item = choose_custom_emoji(pool, wanted, style, used, rng)
        if not item:
            continue
        cid = str(item["custom_emoji_id"])
        used.add(cid)
        fb = (item.get("fallback") or "✨").strip() or "✨"
        if len(fb) > 8 or (len(fb) == 1 and fb.isalnum()):
            fb = "✨"
        ops.append((p, fb + " ", cid))

    for p, ins, cid in sorted(ops, reverse=True):
        delta = len(ins)
        for e in ents:
            if e.start >= p:
                e.start += delta
                e.end += delta
            elif e.end > p:
                e.end += delta
        text = text[:p] + ins + text[p:]
        ents.append(Ent("custom_emoji", p, p + len(ins.rstrip()), {"custom_emoji_id": cid}))
    return text, ents


def format_post(uid: int, source: str, variant: int = 0, override: int | None = None,
                custom: bool = True) -> tuple[str, list[dict[str, Any]]]:
    s = settings(uid)
    intensity = int(override or s["intensity"])
    seed = (uid * 1009 + variant * 7919 + sum(map(ord, source[:240]))) & 0x7FFFFFFF
    text, ents, points = base_format(
        source,
        s["style"],
        intensity,
        bool(s["decor"]),
        bool(s.get("underline", 0)),
        bool(s.get("fonts", 1)),
        bool(s.get("quotes", 1)),
        bool(s.get("strike", 1)),
        seed,
    )
    if custom:
        text, ents = add_custom(
            text, ents, points, emojis(uid), {1: 1, 2: 2, 3: 4}[intensity], seed, s["style"]
        )
    if len(text) > 4096:
        text, ents, _ = base_format(
            source, s["style"], intensity, False, bool(s.get("underline", 0)),
            bool(s.get("fonts", 1)), bool(s.get("quotes", 1)), bool(s.get("strike", 1)), seed
        )
    return text, tg_entities(text, ents)


# ---------- UI ----------

KB = {"inline_keyboard": [
    [{"text": "♻️ Иначе", "callback_data": "reroll"}, {"text": "💎 Больше", "callback_data": "more"}, {"text": "◌ Меньше", "callback_data": "less"}],
    [{"text": "🖤 Shadow", "callback_data": "style:shadow"}, {"text": "◻️ Minimal", "callback_data": "style:minimal"}],
]}

START = (
    "Пришли готовый текст поста. Слова и смысл не переписываю. Я анализирую структуру и смысл, "
    "а затем оформляю: жирный, курсив, редкое подчёркивание, зачёркивание отдельных букв, цитаты, "
    "спойлеры, Telegram monospace, Unicode-шрифт для подходящих латинских фрагментов и premium/custom emoji из твоих паков.\n\n"
    "Emoji pack: просто пришли ссылку t.me/addemoji/... Я просканирую его и буду подбирать emoji по смыслу текста.\n\n"
    "/packs — мои паки\n/delpack <номер> — удалить пак\n/style shadow|minimal\n/intensity 1|2|3\n"
    "/decor on|off\n/fonts on|off\n/quotes on|off\n/strike on|off\n/underline on|off"
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
    if c == "underline":
        if arg.casefold() not in {"on","off"}:
            await send(chat, "Используй /underline on или /underline off", reply_to=mid)
            return
        set_setting(uid,"underline",1 if arg.casefold()=="on" else 0)
        await send(chat, "Подчёркивание включено." if arg.casefold()=="on" else "Подчёркивание выключено.", reply_to=mid)
        return
    if c in {"fonts", "quotes", "strike"}:
        if arg.casefold() not in {"on","off"}:
            await send(chat, f"Используй /{c} on или /{c} off", reply_to=mid)
            return
        set_setting(uid, c, 1 if arg.casefold()=="on" else 0)
        names = {"fonts": "Текстовые шрифты", "quotes": "Цитаты", "strike": "Зачёркивание"}
        await send(chat, f"{names[c]}: {'включено' if arg.casefold()=='on' else 'выключено'}.", reply_to=mid)
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
            {"command":"underline","description":"подчёркивание on/off"},
            {"command":"fonts","description":"текстовые Unicode-шрифты"},
            {"command":"quotes","description":"умные цитаты on/off"},
            {"command":"strike","description":"зачёркивание букв on/off"},
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
