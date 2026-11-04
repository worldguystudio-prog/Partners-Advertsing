import asyncio, os, re, json, time, random, uuid, base64, logging, aiohttp, asyncpg, uvicorn, discord, yaml
from bs4 import BeautifulSoup
from dataclasses import dataclass
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bot")

PRODUCT = {
    "name": "Nomad",
    "one_liner": "Self-hosted bookmark manager that syncs across every browser.",
    "url": "https://example.com/nomad",
    "price": "free tier + $4/mo",
    "proof": "1,200 users, 4.8/5 on Product Hunt",
    "forbidden_claims": ["#1", "best", "guaranteed"],
}
LIMITS = {
    "joins_per_day_new": 12, "joins_per_day_aged": 45, "posts_per_hour": 6,
    "min_gap_between_posts_s": (180, 420), "pre_post_dwell_s": (45, 120),
    "account_age_days_for_dm": 30,
}
DISCOVERY = {
    "keywords": ["dev tools", "productivity", "self hosted", "programming"],
    "min_members": 400, "max_members": 150000,
}
AD_PAT = [r"self-?promo", r"advertis\w*", r"promo\w*", r"share-?your", r"plug\w*"]
PARTNER_PAT = [r"partner\w*", r"affiliat\w*", r"collab\w*"]
NEVER = {"general", "general-chat", "off-topic", "introductions"}
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id BIGSERIAL PRIMARY KEY, token TEXT UNIQUE NOT NULL, proxy_url TEXT,
  status TEXT DEFAULT 'active', age_days INT DEFAULT 0, joins_today INT DEFAULT 0,
  last_post_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE IF NOT EXISTS candidates (
  id BIGSERIAL PRIMARY KEY, invite_code TEXT UNIQUE NOT NULL, name TEXT,
  member_count INT, status TEXT DEFAULT 'pending', reason TEXT,
  found_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE IF NOT EXISTS guilds (
  id BIGSERIAL PRIMARY KEY, account_id BIGINT, guild_id BIGINT, name TEXT,
  ad_channel_id BIGINT, partnership_channel_id BIGINT, rules_text TEXT,
  status TEXT DEFAULT 'joined', joined_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (account_id, guild_id));
CREATE TABLE IF NOT EXISTS posts (
  id BIGSERIAL PRIMARY KEY, account_id BIGINT, guild_id BIGINT, channel_id BIGINT,
  message_id BIGINT, body TEXT, posted_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE IF NOT EXISTS partnerships (
  id BIGSERIAL PRIMARY KEY, account_id BIGINT, guild_id BIGINT, channel_id BIGINT,
  via TEXT, stage TEXT DEFAULT 'sent', sent_at TIMESTAMPTZ DEFAULT NOW());
"""

# ---------------- LLM failover ----------------
class Provider:
    def __init__(self, name, base, key, model, rpm, rpd):
        self.name, self.base, self.key, self.model, self.rpm, self.rpd = name, base, key, model, rpm, rpd
        self.minute, self.day, self.cool = [], 0, 0.0
    def available(self):
        now = time.time(); self.minute = [t for t in self.minute if now - t < 60]
        return bool(self.key) and now > self.cool and len(self.minute) < self.rpm and self.day < self.rpd
    def note(self):
        self.minute.append(time.time()); self.day += 1
    def trip(self, s):
        self.cool = time.time() + s; log.warning("%s cooldown %ss", self.name, s)

PROVIDERS = [
    Provider("groq", "https://api.groq.com/openai/v1", os.getenv("GROQ_API_KEY"), "openai/gpt-oss-120b", 28, 950),
    Provider("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", os.getenv("GEMINI_API_KEY"), "gemini-2.5-flash", 9, 240),
    Provider("openrouter", "https://openrouter.ai/api/v1", os.getenv("OPENROUTER_API_KEY"), "openrouter/free", 18, 45),
]

async def complete(prompt):
    errs = []
    for p in PROVIDERS:
        if not p.available():
            continue
        p.note()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{p.base}/chat/completions",
                    headers={"Authorization": f"Bearer {p.key}"},
                    json={"model": p.model, "max_tokens": 700, "temperature": 0.9,
                          "messages": [{"role": "user", "content": prompt}]}, timeout=90) as r:
                    if r.status == 429:
                        p.trip(int(r.headers.get("Retry-After", 60))); errs.append(f"{p.name}:429"); continue
                    if r.status >= 500:
                        p.trip(30); errs.append(f"{p.name}:{r.status}"); continue
                    r.raise_for_status()
                    return (await r.json())["choices"][0]["message"]["content"]
        except Exception as e:
            errs.append(f"{p.name}:{type(e).__name__}"); p.trip(20)
    raise RuntimeError(f"all LLM providers exhausted: {errs}")

PROMPT = """Write ONE short Discord ad. Obey server rules.
PRODUCT: {name} | {one_liner} | {url} | {price} | {proof}
never claim: {forbidden}
SERVER: {server} | topic: {topic} | channel #{channel}
RULES: {rules}
RECENT: {recent}
JSON only: {{"allowed":bool,"reason":"","content":"","embed":{{"title":"","description":"","url":"{url}"}}}}
content: one line <=140 chars, casual, no hashtags. description: <=200 chars.
If ads are banned or the format is impossible, allowed=false."""

async def write_ad(guild, channel, recent, rules):
    raw = await complete(PROMPT.format(
        name=PRODUCT["name"], one_liner=PRODUCT["one_liner"], url=PRODUCT["url"],
        price=PRODUCT["price"], proof=PRODUCT["proof"],
        forbidden=", ".join(PRODUCT["forbidden_claims"]), server=guild.name,
        topic=guild.topic or "-", channel=channel.name,
        rules=(rules or "-")[:2000], recent=(recent or "-")[:1200]))
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"allowed": False, "reason": "unparseable"}

# ---------------- discovery ----------------
INVITE = re.compile(r"(?:discord\.gg|discord\.com/invite)/([A-Za-z0-9-]{2,32})")
AD_SIG = re.compile(r"(advert|promo|self-?promo|partner|share your|plug|marketplace)", re.I)
BAN_SIG = re.compile(r"(no ads|no advertising|no promo|ad-free|ad free)", re.I)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"

async def disboard(kw, page=1):
    url = f"https://disboard.org/search?keyword={kw.replace(' ', '+')}&sort=member_count&page={page}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"User-Agent": UA}, timeout=30) as r:
                if r.status != 200:
                    return []
                html = await r.text()
    except Exception:
        return []
    out = []
    for a in BeautifulSoup(html, "html.parser").select("h1.server-name a, .server-name a"):
        m = INVITE.search(a.get("href", ""))
        if m:
            out.append({"name": a.get_text(strip=True), "invite_code": m.group(1)})
    await asyncio.sleep(random.uniform(2.5, 5.0))
    return out

def keep(c):
    blob = f"{c.get('name','')} {c.get('topic','')}"
    if DISCOVERY["require_ad_signal"] if "require_ad_signal" in DISCOVERY else True:
        if not AD_SIG.search(blob):
            return False
    if BAN_SIG.search(blob):
        return False
    return True

async def discovery_loop(db):
    while True:
        for kw in DISCOVERY["keywords"]:
            for page in range(1, 4):
                for c in await disboard(kw, page):
                    if keep(c):
                        await db.execute(
                            "INSERT INTO candidates (invite_code,name) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                            c["invite_code"], c["name"])
        await asyncio.sleep(21600)

# ---------------- captcha ----------------
class Broker:
    def __init__(self): self.pending = {}
    async def handle(self, captcha):
        cid = uuid.uuid4().hex[:12]
        self.pending[cid] = {"captcha": captcha, "ts": time.time()}
        url = os.getenv("ALERT_WEBHOOK")
        if url:
            try:
                async with aiohttp.ClientSession() as s:
                    await s.post(url, json={"content": f"Captcha needed: /captcha (id {cid})"})
            except Exception: pass
        if os.getenv("TWOCAPTCHA_KEY"):
            t = await self.solve2(captcha)
            if t: return await self.submit(captcha, t)
        end = time.time() + 600
        while time.time() < end:
            tok = self.pending.get(cid, {}).get("token")
            if tok: return await self.submit(captcha, tok)
            await asyncio.sleep(2)
        log.warning("captcha %s timed out", cid)
    async def submit(self, c, token):
        try: return await c.solve(token)
        except AttributeError: return await c.handler(token)
    async def solve2(self, c):
        key = os.getenv("TWOCAPTCHA_KEY")
        try:
            b64 = base64.b64encode(await c.get_captcha_image()).decode()
            async with aiohttp.ClientSession() as s:
                async with s.post("https://2captcha.com/in.php", data={
                    "key": key, "method": "base64", "body": b64, "json": 1,
                    "pageurl": await c.get_captcha_url(), "sitekey": await c.get_site_key()}) as r:
                    rid = (await r.json()).get("request_id")
                for _ in range(30):
                    await asyncio.sleep(5)
                    async with s.get(f"https://2captcha.com/res.php?key={key}&action=get&id={rid}&json=1") as r:
                        j = await r.json()
                        if j.get("status") == 1: return j["request"]
        except Exception as e: log.warning("2captcha: %s", e)
        return None

# ---------------- worker ----------------
class Bucket:
    def __init__(self, rate, burst):
        self.rate, self.cap, self.tokens = rate, burst, float(burst)
        self.upd, self.lock = time.monotonic(), asyncio.Lock()
    async def take(self):
        async with self.lock:
            now = time.monotonic()
            self.tokens = min(self.cap, self.tokens + (now - self.upd) * self.rate)
            self.upd = now
            if self.tokens < 1:
                await asyncio.sleep((1 - self.tokens) / self.rate)
                self.tokens, self.upd = 0.0, time.monotonic()
            else:
                self.tokens -= 1
        await asyncio.sleep(random.uniform(0.5, 2.0))

def jit(r): return random.uniform(*r)

def score(name, topic, pats):
    s = 0
    for p in pats:
        if re.search(p, name): s += 3
        if re.search(p, topic): s += 1
    return s

def pick(guild):
    best_a = best_p = None; sa = sp = 0
    for ch in guild.text_channels:
        if ch.name.lower() in NEVER: continue
        perms = ch.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links): continue
        n, t = ch.name.lower(), (ch.topic or "").lower()
        a = score(n, t, AD_PAT)
        if a > sa: best_a, sa = ch, a
        p = score(n, t, PARTNER_PAT)
        if p > sp: best_p, sp = ch, p
    return best_a, best_p

class Worker:
    def __init__(self, db, acc, broker):
        self.db, self.acc, self.broker = db, acc, broker
        self.joins = Bucket(1 / 90, 2)
        self.posts = Bucket(LIMITS["posts_per_hour"] / 3600, 1)
        self.client = discord.Client(captcha_handler=broker.handle, proxy=acc.get("proxy_url"))
    async def start(self): await self.client.start(self.acc["token"])
    def cap(self):
        return LIMITS["joins_per_day_aged"] if self.acc["age_days"] >= 60 else LIMITS["joins_per_day_new"]
    async def rules(self, g):
        for ch in g.text_channels:
            if any(k in ch.name.lower() for k in ("rule", "guideline", "start-here", "welcome")):
                try:
                    msgs = [m async for m in ch.history(limit=15)]
                    txt = "\n".join(m.content for m in reversed(msgs) if m.content)
                    if txt: return txt[:4000]
                except discord.HTTPException: pass
        return ""
    async def recent(self, ch):
        try:
            msgs = [m async for m in ch.history(limit=12)]
            return "\n".join(f"{m.author.display_name}: {m.content}" for m in reversed(msgs) if m.content)
        except discord.HTTPException: return ""
    async def join_one(self):
        if self.acc["joins_today"] >= self.cap(): return None
        row = await self.db.fetchrow("SELECT * FROM candidates WHERE status='pending' ORDER BY id LIMIT 1")
        if not row: return None
        await self.joins.take()
        try:
            inv = await self.client.fetch_invite(row["invite_code"], with_counts=True)
        except discord.NotFound:
            await self.db.execute("UPDATE candidates SET status='dead' WHERE id=$1", row["id"]); return None
        if inv.guild and self.client.get_guild(inv.guild.id):
            await self.db.execute("UPDATE candidates SET status='joined' WHERE id=$1", row["id"]); return None
        try:
            await self.client.accept_invite(row["invite_code"])
        except Exception as e:
            log.warning("join %s: %s", row["invite_code"], e)
            await self.db.execute("UPDATE candidates SET status='dead', reason=$2 WHERE id=$1",
                                  row["id"], str(e)[:150]); return None
        await self.db.execute("UPDATE accounts SET joins_today=joins_today+1 WHERE id=$1", self.acc["id"])
        self.acc["joins_today"] += 1
        await asyncio.sleep(5)
        return self.client.get_guild(inv.guild.id) if inv.guild else None
    async def handle(self, g):
        rules = await self.rules(g)
        ad, partner = pick(g)
        await self.db.execute("""INSERT INTO guilds (account_id,guild_id,name,ad_channel_id,partnership_channel_id,rules_text)
            VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING""",
            self.acc["id"], g.id, g.name, ad.id if ad else None, partner.id if partner else None, rules)
        if not ad:
            await self.db.execute("UPDATE guilds SET status='skipped' WHERE account_id=$1 AND guild_id=$2",
                                  self.acc["id"], g.id)
            log.info("%s: no ad channel", g.name); return
        await asyncio.sleep(jit(LIMITS["pre_post_dwell_s"]))
        await self.posts.take()
        out = await write_ad(g, ad, await self.recent(ad), rules)
        if not out.get("allowed"):
            await self.db.execute("UPDATE guilds SET status='skipped' WHERE account_id=$1 AND guild_id=$2",
                                  self.acc["id"], g.id)
            log.info("%s: skipped (%s)", g.name, out.get("reason")); return
        if DRY_RUN:
            log.info("DRY %s #%s :: %s", g.name, ad.name, out.get("content")); return
        e = out.get("embed") or {}
        emb = discord.Embed(title=e.get("title"), description=e.get("description"),
                            url=e.get("url") or PRODUCT["url"], colour=0x5865F2)
        try:
            msg = await ad.send(content=(out.get("content") or "")[:140], embed=emb)
        except discord.HTTPException as ex:
            log.warning("%s post failed: %s", g.name, ex); return
        await self.db.execute("UPDATE guilds SET status='posted' WHERE account_id=$1 AND guild_id=$2",
                              self.acc["id"], g.id)
        await self.db.execute("INSERT INTO posts (account_id,guild_id,channel_id,message_id,body) VALUES ($1,$2,$3,$4,$5)",
                              self.acc["id"], g.id, ad.id, msg.id, out.get("content"))
        if partner: await self.partner(g, partner)
    async def partner(self, g, ch):
        if self.acc["age_days"] < LIMITS["account_age_days_for_dm"]: return
        if await self.db.fetchval("SELECT 1 FROM partnerships WHERE guild_id=$1", g.id): return
        await asyncio.sleep(jit((60, 180)))
        try:
            await ch.send(f"Hey - running **{PRODUCT['name']}**. Open to a mutual promo? "
                          f"I can feature {g.name} in our weekly roundup.")
        except discord.HTTPException: return
        await self.db.execute("INSERT INTO partnerships (account_id,guild_id,channel_id,via) VALUES ($1,$2,$3,'channel')",
                              self.acc["id"], g.id, ch.id)
    async def loop(self):
        await self.client.wait_until_ready()
        while True:
            try:
                g = await self.join_one()
                if g:
                    await self.handle(g)
                    await asyncio.sleep(jit(LIMITS["min_gap_between_posts_s"]))
                else:
                    await asyncio.sleep(60)
            except discord.LoginFailure:
                await self.db.execute("UPDATE accounts SET status='banned' WHERE id=$1", self.acc["id"]); return
            except Exception as e:
                log.exception("loop: %s", e); await asyncio.sleep(45)

# ---------------- web ----------------
app = FastAPI(); BROKER = Broker()

@app.get("/health")
async def health(): return {"ok": True, "pending": len(BROKER.pending)}

@app.get("/captcha", response_class=HTMLResponse)
async def clist():
    rows = "".join(f'<li><a href="/captcha/{k}">{k}</a></li>' for k in BROKER.pending)
    return f"<h2>Pending captchas</h2><ul>{rows or '<li>none</li>'}</ul>"

@app.get("/captcha/{cid}", response_class=HTMLResponse)
async def citem(cid: str):
    return f"""<h2>{cid}</h2><script src="https://js.hcaptcha.com/1/api.js" async defer></script>
<div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001"></div>
<form method="post" action="/captcha/{cid}">
<input name="token" size="70" placeholder="paste token"><button>Submit</button></form>"""

@app.post("/captcha/{cid}")
async def csub(cid: str, token: str = Form(...)):
    if cid in BROKER.pending: BROKER.pending[cid]["token"] = token
    return RedirectResponse("/captcha", 302)

async def main():
    db = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=2, max_size=8)
    await db.execute(SCHEMA)
    proxies = [p for p in os.getenv("PROXY_URLS", "").split(",") if p]
    tokens = [t.strip() for t in os.environ["DISCORD_TOKENS"].split(",") if t.strip()]
    for i, tok in enumerate(tokens):
        await db.execute("INSERT INTO accounts (token,proxy_url) VALUES ($1,$2) ON CONFLICT (token) DO NOTHING",
                         tok, proxies[i] if i < len(proxies) else None)
    accounts = await db.fetch("SELECT * FROM accounts WHERE status='active'")
    log.info("booting %d account(s), dry_run=%s", len(accounts), DRY_RUN)
    asyncio.create_task(discovery_loop(db))
    for a in accounts:
        w = Worker(db, dict(a), BROKER)
        asyncio.create_task(w.start()); asyncio.create_task(w.loop())
    await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0",
        port=int(os.getenv("PORT", 8080)), log_level="warning")).serve()

if __name__ == "__main__":
    asyncio.run(main())
