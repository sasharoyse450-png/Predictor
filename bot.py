import asyncio, hashlib, hmac, io, json, logging, os, random, time, uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from PIL import Image, ImageDraw, ImageFont
from supabase import create_client, Client
from questions import EASY_QUESTIONS, MEDIUM_QUESTIONS, HARD_QUESTIONS, EXTREME_QUESTIONS, DIFFICULTY_LABELS, random_question

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("quizbot")

TOKEN=os.getenv("BOT_TOKEN",""); SUPABASE_URL=os.getenv("SUPABASE_URL",""); SUPABASE_KEY=os.getenv("SUPABASE_KEY","")
XROCKET_API_KEY=os.getenv("XROCKET_API_KEY",""); XROCKET_WEBHOOK_SECRET=os.getenv("XROCKET_WEBHOOK_SECRET","")
XROCKET_BASE="https://pay.api.xrocket.exchange"
XROCKET_SUBSCRIBE_URL=os.getenv("XROCKET_SUBSCRIBE_URL","https://t.me/xRocket")
XROCKET_REFERRAL_URL=os.getenv("XROCKET_REFERRAL_URL","https://t.me/xRocket")
PORT=int(os.getenv("PORT",8080))

ANSWERS_PER_LEVEL=10; MAX_LEVEL=10; RAKE_PCT=0.05
SUBSCRIBER_MULTIPLIER=2.0
SUBSCRIPTION_PRICE=0.50
SUBSCRIPTION_DAYS=7

MIN_WITHDRAW=0.05; DAILY_WITHDRAW_LIMIT=5.00; DEPOSIT_MIN=0.05; DEPOSIT_MAX=50.0
DUEL_MIN=0.05; DUEL_MAX=1.00; DUEL_TTL=120; TIMER_PROBABILITY=0.20; TIMER_SECONDS=10
POT_PERCENT=0.05; POT_HOUR=21; SPONSOR_PRICE=5.00; SPONSOR_QUESTIONS=20
ADMIN_IDS={8130244626,6173495222}
TZ=ZoneInfo(os.getenv("TZ","Europe/Moscow")); WORK_HOURS=list(range(8,24)); WITHDRAW_CONFIRM_TTL=120
WEBHOOK_MAX_AGE_SEC=300; TOP1_CHECK_EVERY=5; CHAT_SETTINGS_TTL=60

CORRECT_PHRASES=["🎉 <b>Правильно!</b>","🔥 <b>В точку!</b>","💎 <b>Красавчик!</b>","⚡ <b>Молниеносно!</b>","🧠 <b>Умница!</b>","🏆 <b>Есть!</b>","✨ <b>Верно!</b>","🚀 <b>Полетели!</b>","🎯 <b>Точно в цель!</b>","🌟 <b>Блестяще!</b>"]
LEVELS=[(1,"🐣","Новичок"),(2,"🥚","Ученик"),(3,"🐥","Знаток"),(4,"🦅","Эксперт"),(5,"🧠","Мастер"),(6,"🎓","Гуру"),(7,"💎","Легенда"),(8,"👑","Гений"),(9,"🔥","Титан"),(10,"⚡","Бог викторины")]
LEVEL_LATIN={1:"ROOKIE",2:"STUDENT",3:"EXPERT",4:"EAGLE",5:"MASTER",6:"GURU",7:"LEGEND",8:"GENIUS",9:"TITAN",10:"QUIZ GOD"}

if not TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    print("!!! Не заданы BOT_TOKEN / SUPABASE_URL / SUPABASE_KEY"); raise SystemExit(1)

bot=Bot(TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML)); dp=Dispatcher(); supabase:Client=create_client(SUPABASE_URL,SUPABASE_KEY)

ACTIVE_QUESTIONS={}; QUIZ_ENABLED=set(); PENDING_WITHDRAWS={}; BANNED_CACHE={}; SUBSCRIBERS_CACHE={}
DUEL_BUSY=set(); TOP_CACHE={}; TOP1_COUNTER={}; HTTP_SESSION=None; _FONT_PATH=None; _FONT_PATH_BOLD=None; _CHAT_SETTINGS_CACHE={}
TOURNAMENT_STATE={}; TOURNAMENT_ACTIVE={}; TOURNAMENT_EDIT={}; SPONSOR_SESSION={}
AVATAR_CACHE={}

def is_admin(uid): return uid in ADMIN_IDS
def level_from_correct(c): return min(c//ANSWERS_PER_LEVEL+1,MAX_LEVEL)
def level_info(c):
    lvl=level_from_correct(c); emoji,name=LEVELS[lvl-1][1],LEVELS[lvl-1][2]
    ts=f"{emoji} {name} (ур. {lvl})"; p="🏆 Максимальный уровень!" if lvl>=MAX_LEVEL else f"до след. уровня: {ANSWERS_PER_LEVEL-(c-(lvl-1)*ANSWERS_PER_LEVEL)} отв."
    return lvl,emoji,name,0,ts,p
def make_progress_bar(c):
    lvl=level_from_correct(c)
    if lvl>=MAX_LEVEL: return "▓"*10+" 10/10"
    il=c-(lvl-1)*ANSWERS_PER_LEVEL
    return f"{'▓'*il}{'▒'*(ANSWERS_PER_LEVEL-il)} {il}/{ANSWERS_PER_LEVEL}"
def get_font(size=64, bold=False):
    global _FONT_PATH, _FONT_PATH_BOLD
    cands_bold=["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf","/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"]
    cands_reg=["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf","/usr/share/fonts/TTF/DejaVuSans.ttf"]
    if (bold and _FONT_PATH_BOLD is None) or (not bold and _FONT_PATH is None):
        for p in (cands_bold if bold else cands_reg):
            if os.path.exists(p):
                if bold: _FONT_PATH_BOLD=p
                else: _FONT_PATH=p
                break
        if bold and _FONT_PATH_BOLD is None: _FONT_PATH_BOLD=""
        if not bold and _FONT_PATH is None: _FONT_PATH=""
    path=_FONT_PATH_BOLD if bold else _FONT_PATH
    if not path and bold: path=_FONT_PATH
    try:
        if path: return ImageFont.truetype(path,size)
    except Exception: pass
    return ImageFont.load_default()

def render_question_image(text):
    W,H=800,300; img=Image.new("RGB",(W,H),"white"); draw=ImageDraw.Draw(img); font=get_font(80,bold=True)
    bbox=draw.textbbox((0,0),text,font=font); tw,th=bbox[2]-bbox[0],bbox[3]-bbox[1]
    while tw>W-60:
        cs=getattr(font,"size",0)
        if cs<=20: break
        font=get_font(cs-5,bold=True); bbox=draw.textbbox((0,0),text,font=font); tw,th=bbox[2]-bbox[0],bbox[3]-bbox[1]
    draw.text(((W-tw)/2-bbox[0],(H-th)/2-bbox[1]),text,fill=(20,20,80),font=font)
    buf=io.BytesIO(); img.save(buf,format="PNG"); return buf.getvalue()

async def fetch_avatar(user_id):
    now=time.time(); cached=AVATAR_CACHE.get(user_id)
    if cached and now-cached[1]<3600: return cached[0]
    try:
        photos=await bot.get_user_profile_photos(user_id, limit=1)
        if not photos or not photos.total_count:
            AVATAR_CACHE[user_id]=(None,now); return None
        sizes=photos.photos[0]; file_id=sizes[-1].file_id if len(sizes)>0 else sizes[0].file_id
        f=await bot.get_file(file_id); buf=io.BytesIO(); await bot.download_file(f.file_path, buf)
        data=buf.getvalue(); AVATAR_CACHE[user_id]=(data,now); return data
    except Exception as e:
        log.warning("fetch_avatar: %s",e); AVATAR_CACHE[user_id]=(None,now); return None

def _circle_mask(size):
    m=Image.new("L",(size,size),0); d=ImageDraw.Draw(m); d.ellipse([0,0,size-1,size-1],fill=255); return m

def render_profile_card(name, username, lvl, correct, place, balance, wd, is_sub, avatar_bytes=None):
    W, H = 720, 1120
    img = Image.new("RGB", (W, H), (10, 10, 28))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        r = int(16 + 50*t); g = int(12 + 26*t); b = int(42 + 120*t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    for cx, cy, rad, col in [(W-40, 100, 220, (255,200,60,22)), (40, H-120, 280, (120,80,255,16))]:
        overlay = Image.new("RGBA", (W, H), (0,0,0,0)); od = ImageDraw.Draw(overlay)
        od.ellipse([cx-rad, cy-rad, cx+rad, cy+rad], fill=col)
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([16, 16, W-17, H-17], radius=32, outline=(255,210,70), width=3)

    av_size = 230
    av_x = (W - av_size) // 2
    av_y = 80
    draw.ellipse([av_x-7, av_y-7, av_x+av_size+7, av_y+av_size+7], fill=(255,210,70))
    draw.ellipse([av_x, av_y, av_x+av_size, av_y+av_size], fill=(30,32,64))
    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGB").resize((av_size, av_size), Image.LANCZOS)
            img.paste(av, (av_x, av_y), _circle_mask(av_size))
            draw = ImageDraw.Draw(img)
        except Exception:
            avatar_bytes = None
    if not avatar_bytes:
        f_av = get_font(110, bold=True)
        letter = (name or "?")[0].upper()
        bb = draw.textbbox((0,0), letter, font=f_av)
        tw, th = bb[2]-bb[0], bb[3]-bb[1]
        draw.text((av_x + av_size/2 - tw/2 - bb[0], av_y + av_size/2 - th/2 - bb[1]), letter, fill=(255,210,70), font=f_av)

    def center(text, font, y_top, fill):
        bb = draw.textbbox((0,0), text, font=font)
        tw = bb[2]-bb[0]
        draw.text(((W-tw)/2 - bb[0], y_top - bb[1]), text, fill=fill, font=font)

    nm = (name or "Player").strip()
    if "@" in nm: nm = nm.split("@", 1)[0].strip() or "Player"
    if len(nm) > 20: nm = nm[:19] + "."
    f_name = get_font(44, bold=True)
    center(nm, f_name, 352, (255,255,255))

    if username:
        f_user = get_font(23)
        center(f"@{username[:26]}", f_user, 412, (150,160,200))
        y_lvl = 458
    else:
        y_lvl = 412

    lvl_name = LEVEL_LATIN.get(lvl, "")
    lvl_str = f"LEVEL {lvl}   {lvl_name}".strip()
    f_lvl = get_font(30, bold=True)
    center(lvl_str, f_lvl, y_lvl, (255,210,70))

    if is_sub:
        btxt = "SUBSCRIBER  x2"; bcol = (190, 55, 175); f_bdg = get_font(22, bold=True); btxt_col = (255,255,255)
    else:
        btxt = "FREE USER"; bcol = (50, 50, 80); f_bdg = get_font(22); btxt_col = (180, 180, 210)
    bb = draw.textbbox((0,0), btxt, font=f_bdg)
    bw_ = bb[2]-bb[0] + 56; bh_ = 44
    bx = (W - bw_) // 2; by = y_lvl + 60
    draw.rounded_rectangle([bx, by, bx+bw_, by+bh_], radius=bh_//2, fill=bcol)
    bbc = draw.textbbox((0,0), btxt, font=f_bdg)
    tw, th = bbc[2]-bbc[0], bbc[3]-bbc[1]
    draw.text((bx+(bw_-tw)/2-bbc[0], by+(bh_-th)/2-bbc[1]), btxt, fill=btxt_col, font=f_bdg)

    sep_y = by + bh_ + 50
    draw.line([(80, sep_y), (W-80, sep_y)], fill=(180,150,60), width=1)

    f_lbl = get_font(17); f_val = get_font(40, bold=True)
    cols_cx = [W*0.28, W*0.72]; rows_y = [sep_y + 45, sep_y + 165]
    stats = [("POINTS", str(correct), (100,220,255)),("RANK", f"#{place}" if place else "—", (255,210,70)),("BALANCE", f"${balance:.4f}", (95,255,145)),("WITHDRAWN TODAY", f"${wd:.4f}", (255,160,90))]
    for i, (lbl, val, col) in enumerate(stats):
        cx = cols_cx[i % 2]; cy = rows_y[i // 2]
        bb = draw.textbbox((0,0), lbl, font=f_lbl); tw = bb[2]-bb[0]
        draw.text((cx - tw/2 - bb[0], cy), lbl, fill=(140,150,190), font=f_lbl)
        bb = draw.textbbox((0,0), val, font=f_val); tw = bb[2]-bb[0]
        draw.text((cx - tw/2 - bb[0], cy + 26 - bb[1]), val, fill=col, font=f_val)

    bx, by, bw, bh = 60, H-170, W-120, 48
    il = correct - (lvl-1)*ANSWERS_PER_LEVEL
    if lvl >= MAX_LEVEL: filled, bt = bw, "MAX LEVEL"
    else: filled, bt = int(bw * il / ANSWERS_PER_LEVEL), f"{il} / {ANSWERS_PER_LEVEL}"
    draw.rounded_rectangle([bx, by, bx+bw, by+bh], radius=bh//2, fill=(28,28,54), outline=(70,70,110), width=2)
    if filled > 2: draw.rounded_rectangle([bx, by, bx+filled, by+bh], radius=bh//2, fill=(255,210,70))
    f_bar = get_font(22, bold=True)
    bb = draw.textbbox((0,0), bt, font=f_bar); tw, th = bb[2]-bb[0], bb[3]-bb[1]
    bar_center = bx + bw/2
    tcol = (25,25,45) if filled > (bar_center - bx) else (230,230,240)
    draw.text((bar_center - tw/2 - bb[0], by + bh/2 - th/2 - bb[1]), bt, fill=tcol, font=f_bar)

    f_ft = get_font(17); ft = "xRocket Quiz Bot"
    bb = draw.textbbox((0,0), ft, font=f_ft); tw = bb[2]-bb[0]
    draw.text(((W-tw)/2 - bb[0], H-58), ft, fill=(120,120,160), font=f_ft)

    buf = io.BytesIO(); img.save(buf, format="PNG", optimize=True); return buf.getvalue()

async def get_http():
    global HTTP_SESSION
    if HTTP_SESSION is None or HTTP_SESSION.closed:
        HTTP_SESSION=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15,connect=5),connector=aiohttp.TCPConnector(limit=100,ttl_dns_cache=300))
    return HTTP_SESSION
async def close_http():
    global HTTP_SESSION
    if HTTP_SESSION and not HTTP_SESSION.closed: await HTTP_SESSION.close()

async def safe_send(cf,*a,**kw):
    for _ in range(3):
        try: return await cf(*a,**kw)
        except TelegramRetryAfter as e: log.warning("FloodWait %s",e.retry_after); await asyncio.sleep(e.retry_after+1)
        except TelegramBadRequest as e:
            if "can't parse entities" in str(e) and "parse_mode" in kw:
                kw.pop("parse_mode",None)
                try: return await cf(*a,**kw)
                except Exception: return None
            log.warning("safe_send: %s",e); return None
        except Exception as e: log.warning("safe_send: %s",e); return None
    return None
async def safe_edit(cf,*a,**kw):
    try: return await cf(*a,**kw)
    except TelegramBadRequest as e:
        s=str(e)
        if "message is not modified" in s or "message to edit not found" in s: return None
        log.warning("safe_edit: %s",e); return None
    except Exception as e: log.warning("safe_edit: %s",e); return None

def _rpc(name,params):
    try: return supabase.rpc(name,params).execute()
    except Exception as e: log.warning("rpc %s: %s",name,e); return None

def _pack_difficulty(diff,winners):
    diff=(diff or "medium").split("#")[0]; return diff if int(winners)==1 else f"{diff}#w{int(winners)}"
def _unpack_difficulty(raw):
    raw=raw or "medium"
    if "#w" in raw:
        d,w=raw.split("#w",1)
        try: return (d or "medium"),int(w)
        except ValueError: return (d or "medium"),1
    return raw,1

# =============== БАЗА ===============

def get_player_sync(cid,uid,un=None,fn=None):
    try:
        res=supabase.table("quiz_players").select("*").eq("chat_id",cid).eq("user_id",uid).execute()
        if res.data:
            row=res.data[0]; upd={}
            if not row.get("first_name") and fn: upd["first_name"]=fn
            if not row.get("username") and un: upd["username"]=un
            if upd:
                try:
                    supabase.table("quiz_players").update(upd).eq("chat_id",cid).eq("user_id",uid).execute(); row.update(upd)
                except Exception: pass
            return row
    except Exception as e: log.warning("get_player: %s",e)
    try: supabase.table("quiz_players").insert({"chat_id":cid,"user_id":uid,"username":un,"first_name":fn}).execute()
    except Exception as e: log.warning("get_player ins: %s",e)
    return {"chat_id":cid,"user_id":uid,"username":un,"first_name":fn,"balance":0,"total_won":0,"correct_answers":0,"is_withdrawing":False}

def add_score_sync(cid,uid):
    r=_rpc("add_balance_atomic",{"p_chat_id":cid,"p_user_id":uid,"p_amount":0,"p_count_correct":True}); return bool(r and r.data)
def add_balance_sync(cid,uid,amt,cc=False):
    r=_rpc("add_balance_atomic",{"p_chat_id":cid,"p_user_id":uid,"p_amount":amt,"p_count_correct":cc}); return float(r.data) if r and r.data is not None else None
def deduct_balance_sync(cid,uid,amt):
    r=_rpc("deduct_balance_atomic",{"p_chat_id":cid,"p_user_id":uid,"p_amount":amt}); return bool(r and r.data is True)
def try_lock_withdraw_sync(cid,uid):
    r=_rpc("try_lock_withdraw",{"p_chat_id":cid,"p_user_id":uid}); return bool(r and r.data is True)
def unlock_withdraw_sync(cid,uid): _rpc("unlock_withdraw",{"p_chat_id":cid,"p_user_id":uid})
def withdrawn_today_sync(cid,uid):
    r=_rpc("withdrawn_today",{"p_chat_id":cid,"p_user_id":uid}); return float(r.data) if r and r.data is not None else 0.0

def log_house_income_sync(cid,amt,src):
    try: supabase.table("quiz_house").insert({"chat_id":cid,"amount":amt,"source":src}).execute()
    except Exception as e: log.warning("house: %s",e)
def get_house_total_sync(cid=None):
    try:
        q=supabase.table("quiz_house").select("amount")
        if cid is not None: q=q.eq("chat_id",cid)
        return sum(float(r["amount"]) for r in (q.execute().data or []))
    except Exception: return 0.0
def get_house_by_source_sync():
    try:
        res=supabase.table("quiz_house").select("amount,source").execute(); by={}
        for r in res.data or []: by[r["source"]]=by.get(r["source"],0.0)+float(r["amount"])
        return by
    except Exception: return {}

def add_to_pot_sync(cid,amt):
    try:
        res=supabase.table("quiz_pot").select("amount").eq("chat_id",cid).execute(); cur=float(res.data[0]["amount"]) if res.data else 0.0; na=round(cur+amt,4)
        if res.data: supabase.table("quiz_pot").update({"amount":na}).eq("chat_id",cid).execute()
        else: supabase.table("quiz_pot").insert({"chat_id":cid,"amount":na}).execute()
        return na
    except Exception as e: log.warning("add_pot: %s",e); return None
def payout_pot_sync(cid):
    try:
        res=supabase.table("quiz_pot").select("amount").eq("chat_id",cid).execute()
        if not res.data: return 0.0
        amt=float(res.data[0]["amount"]); supabase.table("quiz_pot").update({"amount":0}).eq("chat_id",cid).execute(); return amt
    except Exception: return 0.0
def get_pot_sync(cid):
    try:
        res=supabase.table("quiz_pot").select("amount").eq("chat_id",cid).execute(); return float(res.data[0]["amount"]) if res.data else 0.0
    except Exception: return 0.0
def pot_take_sync(cid,amt):
    try:
        res=supabase.table("quiz_pot").select("amount").eq("chat_id",cid).execute(); cur=float(res.data[0]["amount"]) if res.data else 0.0
        if cur<amt: return False,cur
        na=round(cur-amt,4); supabase.table("quiz_pot").update({"amount":na}).eq("chat_id",cid).execute(); return True,na
    except Exception: return False,0.0

def create_invoice_sync(ciid,uid,cid,amt,mid=None,kind="deposit"):
    try:
        res=supabase.table("quiz_invoices").insert({"client_invoice_id":ciid,"user_id":uid,"chat_id":cid,"amount":amt,"message_id":mid,"kind":kind}).execute(); return res.data[0]["id"] if res.data else None
    except Exception as e: log.warning("inv: %s",e); return None
def mark_invoice_paid_sync(ciid):
    try:
        res=supabase.table("quiz_invoices").select("*").eq("client_invoice_id",ciid).execute()
        if not res.data: return None
        inv=res.data[0]
        if inv["status"]=="paid": return None
        supabase.table("quiz_invoices").update({"status":"paid","paid_at":datetime.now(timezone.utc).isoformat()}).eq("client_invoice_id",ciid).execute(); return inv
    except Exception: return None

def load_bans_sync():
    try:
        res=supabase.table("quiz_bans").select("chat_id,user_id,banned_until").execute(); cache={}; now=datetime.now(timezone.utc)
        for row in res.data or []:
            bu=row.get("banned_until")
            if bu:
                try:
                    u=datetime.fromisoformat(bu.replace("Z","+00:00"))
                    if u<=now: continue
                except Exception: pass
            cache.setdefault(int(row["chat_id"]),set()).add(int(row["user_id"]))
        return cache
    except Exception: return {}
def load_subscribers_sync():
    try:
        now_iso=datetime.now(timezone.utc).isoformat()
        res=supabase.table("quiz_subscribers").select("chat_id,user_id").gt("expires_at",now_iso).execute(); cache={}
        for row in res.data or []: cache.setdefault(int(row["chat_id"]),set()).add(int(row["user_id"]))
        return cache
    except Exception: return {}

def ban_user_sync(cid,uid,reason,aid,bu=None):
    try: supabase.table("quiz_bans").upsert({"chat_id":cid,"user_id":uid,"reason":reason,"banned_by":aid,"banned_until":bu}).execute()
    except Exception as e: log.warning("ban: %s",e)
def unban_user_sync(cid,uid):
    try: supabase.table("quiz_bans").delete().eq("chat_id",cid).eq("user_id",uid).execute()
    except Exception: pass

def save_active_sync(cid,q,ans,im):
    try: supabase.table("quiz_active").upsert({"chat_id":cid,"question":q,"answer":"||".join(ans),"is_multi":im}).execute()
    except Exception: pass
def clear_active_sync(cid):
    try: supabase.table("quiz_active").delete().eq("chat_id",cid).execute()
    except Exception: pass
def load_active_sync():
    try: return supabase.table("quiz_active").select("*").execute().data or []
    except Exception: return []
def unlock_all_withdrawals_sync():
    try: supabase.table("quiz_players").update({"is_withdrawing":False}).eq("is_withdrawing",True).execute()
    except Exception: pass

def get_top_sync(cid,limit=10):
    try: return supabase.table("quiz_players").select("user_id,username,first_name,balance,correct_answers").eq("chat_id",cid).order("correct_answers",desc=True).order("balance",desc=True).limit(limit).execute().data or []
    except Exception: return []
def get_top1_sync(cid):
    r=get_top_sync(cid,1); return int(r[0]["user_id"]) if r else None
def get_player_place_sync(cid,uid):
    try:
        me=supabase.table("quiz_players").select("correct_answers,balance").eq("chat_id",cid).eq("user_id",uid).execute()
        if not me.data: return None
        mca=int(me.data[0]["correct_answers"]); mb=float(me.data[0].get("balance") or 0)
        above_ca=supabase.table("quiz_players").select("user_id").eq("chat_id",cid).gt("correct_answers",mca).execute().data or []
        same_ca_above=supabase.table("quiz_players").select("user_id").eq("chat_id",cid).eq("correct_answers",mca).gt("balance",mb).execute().data or []
        return len(above_ca)+len(same_ca_above)+1
    except Exception: return None

def get_stats_sync():
    try:
        p=supabase.table("quiz_players").select("balance,total_won,correct_answers").execute().data or []
        po=supabase.table("quiz_payouts").select("amount,status").execute().data or []
        b=supabase.table("quiz_bans").select("user_id").execute().data or []
        s=supabase.table("quiz_subscribers").select("user_id").execute().data or []
        return p,po,b,s
    except Exception: return [],[],[],[]
def get_payouts_sync(limit=20):
    try: return supabase.table("quiz_payouts").select("*").order("created_at",desc=True).limit(limit).execute().data or []
    except Exception: return []
def log_payout_sync(cid,uid,amt,pid,st):
    try: supabase.table("quiz_payouts").insert({"chat_id":cid,"user_id":uid,"amount":amt,"xrocket_payout_id":pid,"status":st}).execute()
    except Exception as e: log.warning("log_payout: %s",e)
def get_coins_stats_sync(cid=None):
    try:
        pq=supabase.table("quiz_players").select("balance,correct_answers,user_id")
        if cid is not None: pq=pq.eq("chat_id",cid)
        p=pq.execute().data or []; tb=sum(float(x["balance"]) for x in p); tp=sum(int(x["correct_answers"]) for x in p)
        pq2=supabase.table("quiz_payouts").select("amount,status")
        if cid is not None: pq2=pq2.eq("chat_id",cid)
        po=pq2.execute().data or []; tp2=sum(float(x["amount"]) for x in po if x["status"]=="finished")
        hq=supabase.table("quiz_house").select("amount")
        if cid is not None: hq=hq.eq("chat_id",cid)
        h=hq.execute().data or []; th=sum(float(x["amount"]) for x in h)
        return {"balance":tb,"points":tp,"players":len(p),"paid":tp2,"house":th}
    except Exception as e: log.warning("coins: %s",e); return None

def get_chat_settings_sync(cid):
    try:
        res=supabase.table("quiz_settings").select("*").eq("chat_id",cid).execute()
        if res.data: return res.data[0]
    except Exception as e: log.warning("settings: %s",e)
    d={"chat_id":cid,"difficulty":"medium"}
    try:
        ins=supabase.table("quiz_settings").insert(d).execute()
        if ins.data: return ins.data[0]
    except Exception as e:
        log.warning("settings ins: %s",e)
        try:
            res=supabase.table("quiz_settings").select("*").eq("chat_id",cid).execute()
            if res.data: return res.data[0]
        except Exception: pass
    return d
def update_chat_setting_sync(cid,f,v):
    try:
        get_chat_settings_sync(cid); supabase.table("quiz_settings").update({f:v}).eq("chat_id",cid).execute(); return True
    except Exception as e: log.warning("upd_set: %s",e); return False

def tournament_create_sync(cid,prize):
    try:
        res=supabase.table("quiz_tournaments").insert({"chat_id":cid,"prize":prize,"status":"active"}).execute(); return res.data[0]["id"] if res.data else None
    except Exception as e: log.warning("t_create: %s",e); return None
def tournament_finish_sync(tid,wid):
    try: supabase.table("quiz_tournaments").update({"status":"finished","finished_at":datetime.now(timezone.utc).isoformat(),"winner_id":wid}).eq("id",tid).execute()
    except Exception as e: log.warning("t_fin: %s",e)
def get_t_settings_sync(cid):
    try:
        res=supabase.table("quiz_tournament_settings").select("*").eq("chat_id",cid).execute()
        if res.data:
            row=res.data[0]; d,wc=_unpack_difficulty(row.get("difficulty","medium")); row["difficulty"]=d; row["winners_count"]=wc; return row
    except Exception as e: log.warning("t_set: %s",e)
    d={"chat_id":cid,"question_seconds":30,"questions":10,"prize":0.30,"difficulty":"medium","winners_count":1}
    try:
        ins=supabase.table("quiz_tournament_settings").insert(d).execute()
        if ins.data:
            row=ins.data[0]; row["difficulty"]="medium"; row["winners_count"]=1; return row
    except Exception as e:
        log.warning("t_set ins: %s",e)
        try:
            res=supabase.table("quiz_tournament_settings").select("*").eq("chat_id",cid).execute()
            if res.data:
                row=res.data[0]; dd,wc=_unpack_difficulty(row.get("difficulty","medium")); row["difficulty"]=dd; row["winners_count"]=wc; return row
        except Exception: pass
    return d
def update_t_setting_sync(cid,f,v):
    try:
        cur=get_t_settings_sync(cid)
        if f=="winners_count":
            nd=_pack_difficulty(cur.get("difficulty","medium"),int(v)); supabase.table("quiz_tournament_settings").update({"difficulty":nd}).eq("chat_id",cid).execute(); return True
        if f=="difficulty":
            nd=_pack_difficulty(v,cur.get("winners_count",1)); supabase.table("quiz_tournament_settings").update({"difficulty":nd}).eq("chat_id",cid).execute(); return True
        supabase.table("quiz_tournament_settings").update({f:v}).eq("chat_id",cid).execute(); return True
    except Exception as e: log.warning("upd_t_set: %s",e); return False

def sponsor_add_sync(uid,cid,q,a,pos):
    try:
        res=supabase.table("quiz_sponsor_questions").insert({"sponsor_id":uid,"chat_id":cid,"question":q,"answer":a,"position":pos}).execute(); return res.data[0]["id"] if res.data else None
    except Exception as e: log.warning("sp_add: %s",e); return None
def sponsor_get_next_sync(cid):
    try:
        res=supabase.table("quiz_sponsor_questions").select("*").eq("chat_id",cid).eq("used",False).order("position").limit(1).execute(); return res.data[0] if res.data else None
    except Exception: return None
def sponsor_mark_used_sync(qid):
    try: supabase.table("quiz_sponsor_questions").update({"used":True}).eq("id",qid).execute()
    except Exception: pass

# ===== ПОДПИСКА =====

def activate_subscription_sync(user_id, days):
    try:
        now=datetime.now(timezone.utc)
        res=supabase.table("quiz_subscribers").select("*").eq("chat_id",0).eq("user_id",user_id).execute()
        base=now
        if res.data:
            cur=res.data[0].get("expires_at")
            if cur:
                try:
                    exp=datetime.fromisoformat(str(cur).replace("Z","+00:00"))
                    if exp>now: base=exp
                except Exception: pass
            new_exp=base+timedelta(days=days)
            supabase.table("quiz_subscribers").update({"expires_at":new_exp.isoformat()}).eq("chat_id",0).eq("user_id",user_id).execute()
            return new_exp
        new_exp=now+timedelta(days=days)
        supabase.table("quiz_subscribers").insert({"chat_id":0,"user_id":user_id,"expires_at":new_exp.isoformat()}).execute()
        return new_exp
    except Exception as e: log.warning("activate_sub: %s",e); return None
def get_subscription_sync(user_id):
    try:
        res=supabase.table("quiz_subscribers").select("expires_at").eq("chat_id",0).eq("user_id",user_id).execute()
        if not res.data: return None
        exp=res.data[0].get("expires_at")
        if not exp: return None
        return datetime.fromisoformat(str(exp).replace("Z","+00:00"))
    except Exception as e: log.warning("get_sub: %s",e); return None
def list_subscriptions_sync(limit=50):
    try:
        res=supabase.table("quiz_subscribers").select("user_id,expires_at").eq("chat_id",0).order("expires_at",desc=True).limit(limit).execute(); return res.data or []
    except Exception as e: log.warning("list_subs: %s",e); return []
def deactivate_subscription_sync(user_id):
    try:
        supabase.table("quiz_subscribers").delete().eq("chat_id",0).eq("user_id",user_id).execute(); return True
    except Exception as e: log.warning("deact_sub: %s",e); return False

# =============== ASYNC-ОБЁРТКИ ===============

async def get_player(cid,uid,un=None,fn=None): return await asyncio.to_thread(get_player_sync,cid,uid,un,fn)
async def add_score(cid,uid): return await asyncio.to_thread(add_score_sync,cid,uid)
async def add_balance(cid,uid,amt,cc=False): return await asyncio.to_thread(add_balance_sync,cid,uid,amt,cc)
async def deduct_balance(cid,uid,amt): return await asyncio.to_thread(deduct_balance_sync,cid,uid,amt)
async def try_lock_withdraw(cid,uid): return await asyncio.to_thread(try_lock_withdraw_sync,cid,uid)
async def unlock_withdraw(cid,uid): await asyncio.to_thread(unlock_withdraw_sync,cid,uid)
async def withdrawn_today(cid,uid): return await asyncio.to_thread(withdrawn_today_sync,cid,uid)
async def log_house_income(cid,amt,src): await asyncio.to_thread(log_house_income_sync,cid,amt,src)
async def get_house_total(cid=None): return await asyncio.to_thread(get_house_total_sync,cid)
async def get_house_by_source(): return await asyncio.to_thread(get_house_by_source_sync)
async def add_to_pot(cid,amt): return await asyncio.to_thread(add_to_pot_sync,cid,amt)
async def payout_pot(cid): return await asyncio.to_thread(payout_pot_sync,cid)
async def get_pot(cid): return await asyncio.to_thread(get_pot_sync,cid)
async def pot_take(cid,amt): return await asyncio.to_thread(pot_take_sync,cid,amt)
async def create_invoice(ciid,uid,cid,amt,mid=None,kind="deposit"): return await asyncio.to_thread(create_invoice_sync,ciid,uid,cid,amt,mid,kind)
async def mark_invoice_paid(ciid): return await asyncio.to_thread(mark_invoice_paid_sync,ciid)
async def get_top1(cid): return await asyncio.to_thread(get_top1_sync,cid)
async def get_player_place(cid,uid): return await asyncio.to_thread(get_player_place_sync,cid,uid)
async def get_coins_stats(cid=None): return await asyncio.to_thread(get_coins_stats_sync,cid)
async def get_chat_settings(cid):
    now=time.time(); c=_CHAT_SETTINGS_CACHE.get(cid)
    if c and now-c[1]<CHAT_SETTINGS_TTL: return c[0]
    d=await asyncio.to_thread(get_chat_settings_sync,cid); _CHAT_SETTINGS_CACHE[cid]=(d,now); return d
async def update_chat_setting(cid,f,v):
    ok=await asyncio.to_thread(update_chat_setting_sync,cid,f,v); _CHAT_SETTINGS_CACHE.pop(cid,None); return ok
async def tournament_create(cid,prize): return await asyncio.to_thread(tournament_create_sync,cid,prize)
async def tournament_finish(tid,wid): await asyncio.to_thread(tournament_finish_sync,tid,wid)
async def get_t_settings(cid): return await asyncio.to_thread(get_t_settings_sync,cid)
async def update_t_setting(cid,f,v): return await asyncio.to_thread(update_t_setting_sync,cid,f,v)
async def sponsor_add(uid,cid,q,a,pos): return await asyncio.to_thread(sponsor_add_sync,uid,cid,q,a,pos)
async def sponsor_get_next(cid): return await asyncio.to_thread(sponsor_get_next_sync,cid)
async def sponsor_mark_used(qid): await asyncio.to_thread(sponsor_mark_used_sync,qid)
async def activate_subscription(uid,days): return await asyncio.to_thread(activate_subscription_sync,uid,days)
async def get_subscription(uid): return await asyncio.to_thread(get_subscription_sync,uid)
async def list_subscriptions(limit=50): return await asyncio.to_thread(list_subscriptions_sync,limit)
async def deactivate_subscription(uid): return await asyncio.to_thread(deactivate_subscription_sync,uid)

def is_banned_cached(cid,uid): return uid in BANNED_CACHE.get(cid,set())
def is_subscriber_cached(cid,uid):
    if uid in SUBSCRIBERS_CACHE.get(0,set()): return True
    return uid in SUBSCRIBERS_CACHE.get(cid,set())

async def ban_user(cid,uid,reason,aid,bu=None):
    await asyncio.to_thread(ban_user_sync,cid,uid,reason,aid,bu); BANNED_CACHE.setdefault(cid,set()).add(uid)
async def unban_user(cid,uid):
    await asyncio.to_thread(unban_user_sync,cid,uid); BANNED_CACHE.get(cid,set()).discard(uid)
async def save_active(cid,q,a,im): await asyncio.to_thread(save_active_sync,cid,q,a,im)
async def clear_active(cid): await asyncio.to_thread(clear_active_sync,cid)
async def get_top(cid,limit=10): return await asyncio.to_thread(get_top_sync,cid,limit)
async def get_stats(): return await asyncio.to_thread(get_stats_sync)
async def get_payouts(limit=20): return await asyncio.to_thread(get_payouts_sync,limit)
async def log_payout(cid,uid,amt,pid,st): await asyncio.to_thread(log_payout_sync,cid,uid,amt,pid,st)

# =============== xROCKET ===============

async def xrocket_payout(cid,uid,amount):
    if not XROCKET_API_KEY: return False,"XROCKET_API_KEY не задан"
    payload={"clientPayoutId":f"quiz_{cid}_{uid}_{int(datetime.now().timestamp()*1000)}","target":str(uid),"targetType":"telegram_user_id","asset":"USDT","amount":f"{amount:.4f}","description":"Quiz reward"}
    try:
        s=await get_http()
        async with s.post(f"{XROCKET_BASE}/api/v1/payouts",headers={"Authorization":f"Bearer {XROCKET_API_KEY}","Content-Type":"application/json"},json=payload) as r:
            data=await r.json(); log.info("xRocket [%s] %s",r.status,data)
            if r.status in (200,201): return True,data.get("payoutId") or data.get("id") or "ok"
            return False,data.get("detail") or data.get("title") or str(data)
    except Exception as e: return False,str(e)
async def xrocket_create_invoice(ciid,amount,desc):
    if not XROCKET_API_KEY: return False,"XROCKET_API_KEY не задан"
    payload={"priceAmount":f"{amount:.4f}","priceCurrency":"USDT","numPayments":1,"clientInvoiceId":ciid,"description":desc[:1000],"expiresIn":3600000}
    try:
        s=await get_http()
        async with s.post(f"{XROCKET_BASE}/api/v1/invoices",headers={"Authorization":f"Bearer {XROCKET_API_KEY}","Content-Type":"application/json"},json=payload) as r:
            data=await r.json(); log.info("xRocket inv [%s] %s",r.status,data)
            if r.status in (200,201):
                iid=data.get("id"); return True,data.get("links",{}).get("telegramBotLink") or f"https://t.me/xRocket?start={iid}"
            return False,data.get("detail") or data.get("title") or str(data)
    except Exception as e: return False,str(e)

def verify_webhook_signature(raw,sig,ts,secret):
    if not sig or not ts or not secret: return False
    try: tsi=int(ts)
    except (ValueError,TypeError): return False
    if tsi>10_000_000_000: tsi//=1000
    if abs(int(time.time())-tsi)>WEBHOOK_MAX_AGE_SEC: log.warning("ts old"); return False
    signed=f"{ts}.{raw.decode('utf-8')}"; exp=hmac.new(secret.encode(),signed.encode(),hashlib.sha256).hexdigest(); return hmac.compare_digest(exp,sig)
async def handle_webhook(request):
    try:
        raw=await request.read(); sig=request.headers.get("Signature",""); ver=request.headers.get("Signature-Version",""); ts=request.headers.get("Signature-Timestamp","")
        if ver!="v1": return web.Response(status=401,text="bad version")
        if not verify_webhook_signature(raw,sig,ts,XROCKET_WEBHOOK_SECRET): return web.Response(status=401,text="bad sig")
        try: event=json.loads(raw)
        except Exception: return web.Response(status=400,text="bad json")
        et=event.get("type"); data=event.get("data",{}); log.info("Webhook: %s",et)
        if et=="invoice" and data.get("event")=="invoice_status_changed":
            inv=data.get("invoice",{})
            if inv.get("status")=="paid":
                ciid=inv.get("clientInvoiceId")
                if ciid: await process_paid_invoice(ciid)
        return web.Response(status=200,text="ok")
    except Exception as e: log.error("Webhook: %s",e); return web.Response(status=200,text="ok")

async def process_paid_invoice(ciid):
    inv=await mark_invoice_paid(ciid)
    if not inv: return
    uid=int(inv["user_id"]); cid=int(inv["chat_id"]) if inv.get("chat_id") else uid
    amt=float(inv["amount"]); mid=inv.get("message_id"); kind=inv.get("kind") or "deposit"

    if kind=="subscription":
        exp=await activate_subscription(uid, SUBSCRIPTION_DAYS)
        try:
            global SUBSCRIBERS_CACHE
            SUBSCRIBERS_CACHE=await asyncio.to_thread(load_subscribers_sync)
        except Exception: pass
        if mid:
            try:
                await bot.edit_message_text(chat_id=cid,message_id=int(mid),
                    text=(f"✅ <b>Подписка активирована!</b>\n\n"
                          f"📅 До: <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>\n"
                          f"🔥 Множитель очков: <b>×{SUBSCRIBER_MULTIPLIER:.0f}</b>"))
            except Exception: pass
        try:
            await bot.send_message(uid,
                f"🎉 <b>Подписка оформлена!</b>\n\n"
                f"📅 До: <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>\n"
                f"🔥 Теперь за каждый ответ: <b>×{SUBSCRIBER_MULTIPLIER:.0f} очка</b>")
        except Exception: pass
        return

    if kind=="sponsor":
        SPONSOR_SESSION[uid]={"chat_id":None,"collected":0,"target":SPONSOR_QUESTIONS}
        try: await bot.send_message(uid,f"✅ <b>Оплата ${amt:.2f} получена!</b>\n\nТеперь напиши <b>ID чата</b>.\n<i>(напр: -1002712583382)</i>")
        except Exception: pass
        return

    nb=await add_balance(cid,uid,amt)
    if nb is None:
        p=await get_player(cid,uid); nb=float(p.get("balance",0))
    if mid:
        try: await bot.edit_message_text(chat_id=cid,message_id=int(mid),text=f"✅ <b>Пополнение успешно!</b>\n\n💳 Зачислено: <b>${amt:.4f}</b> USDT\n💰 Баланс: <b>${nb:.4f}</b>\n\n<i>Спасибо!</i>")
        except Exception: pass
    try: await bot.send_message(uid,f"✅ <b>Баланс пополнен!</b>\n\n💰 Сумма: <b>${amt:.4f}</b>\n💼 Баланс: <b>${nb:.4f}</b>")
    except Exception: pass

async def start_webhook_server():
    app=web.Application(); app.router.add_post("/webhook",handle_webhook); app.router.get("/",lambda r: web.Response(text="ok"))
    runner=web.AppRunner(app); await runner.setup(); site=web.TCPSite(runner,"0.0.0.0",PORT); await site.start(); log.info("Webhook on %s",PORT)

# =============== ВИКТОРИНА ===============

async def ask_question(cid):
    if cid in TOURNAMENT_ACTIVE: return False
    cs=await get_chat_settings(cid); diff=cs.get("difficulty","medium"); dl=DIFFICULTY_LABELS.get(diff,"🟡 Средне")
    sq=await sponsor_get_next(cid)
    if sq:
        q=sq["question"]; answers=[sq["answer"].lower()]; is_multi=False; is_image=False
        await sponsor_mark_used(sq["id"]); sn="\n🎁 <i>Спонсорский вопрос</i>"
    else:
        q,answers,is_multi,is_image=random_question(difficulty=diff); sn=""
    ACTIVE_QUESTIONS[cid]={"question":q,"answers":answers,"is_multi":is_multi,"timer":False,"timer_task":None}
    await save_active(cid,q,answers,is_multi)
    pot=await get_pot(cid); pl=f"\n🎰 Копилка: <b>${pot:.3f}</b>" if pot>=0.01 else ""
    ut=random.random()<TIMER_PROBABILITY
    if ut: ACTIVE_QUESTIONS[cid]["timer"]=True; tl=f"\n⏱ <b>Таймер: {TIMER_SECONDS} сек!</b>"
    else: tl=""
    try: await bot.send_chat_action(cid,"typing")
    except Exception: pass
    try:
        if is_image:
            png=render_question_image(q); buf=BufferedInputFile(png,filename="q.png")
            cap=f"🧠 <b>Вопрос!</b>{sn}\n\n{dl} · 🏆 +1 очко\n🔓 Вопрос открыт до правильного ответа.{pl}{tl}"
            msg=await safe_send(bot.send_photo,cid,buf,caption=cap)
        else:
            msg=await safe_send(bot.send_message,cid,f"🧠 <b>Вопрос!</b>{sn}\n\n❓ {q}\n\n{dl} · 🏆 +1 очко\n🔓 Вопрос открыт до правильного ответа.{pl}{tl}")
        if msg:
            try: await bot.set_message_reaction(cid,msg.message_id,["🧠"])
            except Exception: pass
        if ut:
            async def tt():
                await asyncio.sleep(TIMER_SECONDS); cur=ACTIVE_QUESTIONS.get(cid)
                if cur and cur.get("timer"):
                    ACTIVE_QUESTIONS.pop(cid,None); await clear_active(cid); await safe_send(bot.send_message,cid,f"⌛ <b>Время вышло!</b>\nОтвет: <b>{answers[0]}</b>")
            ACTIVE_QUESTIONS[cid]["timer_task"]=asyncio.create_task(tt())
        return True
    except Exception as e: log.warning("ask: %s",e); return False

def next_run_time(now):
    for h in WORK_HOURS:
        t=now.replace(hour=h,minute=0,second=5,microsecond=0)
        if t>now: return t
    return (now+timedelta(days=1)).replace(hour=WORK_HOURS[0],minute=0,second=5,microsecond=0)
async def question_scheduler():
    await asyncio.sleep(10)
    while True:
        now=datetime.now(TZ); t=next_run_time(now); w=(t-now).total_seconds()
        log.info("Next Q at %s (in %.0f)",t.strftime("%H:%M:%S"),w); await asyncio.sleep(max(1,w))
        if QUIZ_ENABLED: await asyncio.gather(*[ask_question(c) for c in list(QUIZ_ENABLED)],return_exceptions=True)
        await asyncio.sleep(60)
async def caches_refresh_loop():
    global BANNED_CACHE,SUBSCRIBERS_CACHE
    while True:
        try:
            BANNED_CACHE=await asyncio.to_thread(load_bans_sync); SUBSCRIBERS_CACHE=await asyncio.to_thread(load_subscribers_sync)
        except Exception as e: log.warning("cache: %s",e)
        await asyncio.sleep(60)

async def distribute_pot(cid,reason="manual"):
    amt=await payout_pot(cid)
    if amt<0.01: return False,"Копилка пуста"
    rows=await get_top(cid,10)
    if not rows: await add_to_pot(cid,amt); return False,"Нет игроков"
    w=random.choice(rows); wuid=int(w["user_id"]); wn=w.get("first_name") or w.get("username") or str(wuid)
    await add_balance(cid,wuid,amt)
    head="🎰 <b>Розыгрыш копилки!</b>" if reason=="auto" else "🎉 <b>Копилка разыграна вручную!</b>"
    await safe_send(bot.send_message,cid,f"{head}\n\n💰 Выигрыш: <b>${amt:.4f}</b>\n🏆 <b>{wn}</b> (ID <code>{wuid}</code>)\n<i>Случайный из топ-10</i>")
    return True,f"${amt:.4f} → {wn}"
async def pot_payout_loop():
    await asyncio.sleep(30); last=None
    while True:
        now=datetime.now(TZ); td=now.date()
        if now.hour==POT_HOUR and last!=td:
            last=td
            for cid in list(QUIZ_ENABLED):
                try: await distribute_pot(cid,reason="auto")
                except Exception as e: log.warning("pot: %s",e)
        await asyncio.sleep(60)

def split_prize(prize,top_scores):
    tp=sum(p for _,p in top_scores)
    if tp==0: return []
    r=[]; acc=0.0
    for i,(uid,pts) in enumerate(top_scores):
        if i==len(top_scores)-1: sh=round(prize-acc,4)
        else: sh=round(prize*(pts/tp),4); acc+=sh
        r.append((uid,sh))
    return r

async def run_tournament(tid,cid,settings):
    TOURNAMENT_ACTIVE[cid]=tid
    tq=int(settings["questions"]); qs=int(settings["question_seconds"]); pr=float(settings["prize"])
    diff=settings.get("difficulty","medium"); dl=DIFFICULTY_LABELS.get(diff,"🟡 Средне"); wc=int(settings.get("winners_count",1))
    scores={}
    try:
        await safe_send(bot.send_message,cid,f"🏁 <b>ТУРНИР НАЧАЛСЯ!</b>\n\n🎯 {dl}\n❓ {tq} вопросов\n⏱ {qs} сек\n💰 Приз: <b>${pr:.2f}</b>\n🏆 Победителей: <b>{wc}</b>\n\nОтвечать может любой!")
        await asyncio.sleep(2)
        for qn in range(tq):
            q,answers,_,_=random_question(difficulty=diff)
            TOURNAMENT_STATE[cid]={"tid":tid,"q_num":qn,"answers":answers,"answered_by":None}
            await safe_send(bot.send_message,cid,f"❓ <b>Вопрос {qn+1}/{tq}</b>\n\n{q}\n\n⏱ {qs} сек")
            for _ in range(qs):
                await asyncio.sleep(1); st=TOURNAMENT_STATE.get(cid)
                if st and st.get("answered_by"): break
            st=TOURNAMENT_STATE.get(cid) or {}; wu=st.get("answered_by")
            if wu:
                scores[wu]=scores.get(wu,0)+1
                try:
                    p=await get_player(cid,wu); nm=p.get("first_name") or p.get("username") or str(wu)
                    await safe_send(bot.send_message,cid,f"✅ <b>{nm}</b> +1")
                except Exception: pass
            else: await safe_send(bot.send_message,cid,f"⌛ Никто. Ответ: <b>{answers[0]}</b>")
            await asyncio.sleep(2)
        if not scores:
            await safe_send(bot.send_message,cid,"🏁 <b>Турнир окончен.</b>\nНикто не ответил.")
            await tournament_finish(tid,None); return
        ss=sorted(scores.items(),key=lambda x:-x[1]); tn=ss[:wc]; sh=split_prize(pr,tn)
        wids=[]
        for uid,s in sh:
            if s>0: await add_balance(cid,uid,s)
            wids.append(uid)
        await tournament_finish(tid,wids[0] if wids else None)
        lines=["🏆 <b>ТУРНИР ЗАВЕРШЁН!</b>",""]
        medals=["🥇","🥈","🥉","4.","5.","6.","7.","8.","9.","10."]
        for i,(uid,s) in enumerate(sh):
            try:
                pp=await get_player(cid,uid); pnm=pp.get("first_name") or pp.get("username") or str(uid)
            except Exception: pnm=str(uid)
            med=medals[i] if i<len(medals) else f"{i+1}."
            lines.append(f"{med} {pnm} — {scores[uid]} прав. · <b>${s:.4f}</b>")
        rest=ss[wc:]
        if rest:
            lines.append(""); lines.append("<i>Остальные:</i>")
            for uid,cnt in rest[:5]:
                try:
                    pp=await get_player(cid,uid); pnm=pp.get("first_name") or pp.get("username") or str(uid)
                except Exception: pnm=str(uid)
                lines.append(f"• {pnm} — {cnt}")
        lines.append(""); lines.append(f"💰 Приз: <b>${pr:.2f}</b> · Победителей: {wc}")
        await safe_send(bot.send_message,cid,"\n".join(lines))
    finally:
        TOURNAMENT_ACTIVE.pop(cid,None); TOURNAMENT_STATE.pop(cid,None)

def turik_kb(s):
    diff=s.get("difficulty","medium"); wc=s.get("winners_count",1)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏱ {s['question_seconds']}с",callback_data="turik:set:question_seconds"),InlineKeyboardButton(text=f"❓ {s['questions']}",callback_data="turik:set:questions")],
        [InlineKeyboardButton(text=f"💰 Приз: ${float(s['prize']):.2f}",callback_data="turik:set:prize")],
        [InlineKeyboardButton(text=f"🏆 Победителей: {wc}",callback_data="turik:winners_count")],
        [InlineKeyboardButton(text=f"🎯 {DIFFICULTY_LABELS.get(diff,diff)}",callback_data="turik:difficulty")],
        [InlineKeyboardButton(text="🚀 Запустить турнир",callback_data="turik:start")],
        [InlineKeyboardButton(text="🔄 Обновить",callback_data="turik:refresh")]])
def turik_text(s):
    diff=s.get("difficulty","medium"); wc=s.get("winners_count",1)
    return (f"🏁 <b>Настройки турнира</b>\n\n⏱ На вопрос: <b>{s['question_seconds']}</b> сек\n❓ Вопросов: <b>{s['questions']}</b>\n💰 Приз: <b>${float(s['prize']):.2f}</b>\n🏆 Победителей: <b>{wc}</b>\n🎯 Сложность: <b>{DIFFICULTY_LABELS.get(diff,diff)}</b>\n\n<i>Приз делится пропорционально очкам.</i>\n\nЖми кнопку → напиши число.")

@dp.message(Command("AiTurik"))
async def cmd_turik(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    if message.chat.type not in ("group","supergroup"): return
    TOURNAMENT_EDIT.pop(message.from_user.id,None); s=await get_t_settings(message.chat.id)
    await safe_send(message.reply,turik_text(s),reply_markup=turik_kb(s))
@dp.message(F.chat.type.in_({"group","supergroup"}),F.text,~F.text.startswith("/"))
async def turik_value_input(message:Message):
    if not message.from_user or message.from_user.id not in TOURNAMENT_EDIT: raise SkipHandler()
    edit=TOURNAMENT_EDIT[message.from_user.id]
    if edit["chat_id"]!=message.chat.id: raise SkipHandler()
    f=edit["field"]; raw=message.text.strip().replace(",",".")
    try:
        if f=="prize":
            v=float(raw)
            if not (0.1<=v<=100): raise ValueError
            v=round(v,4)
        else:
            v=int(raw); rng={"question_seconds":(5,300),"questions":(3,50)}[f]
            if not (rng[0]<=v<=rng[1]): raise ValueError
    except ValueError:
        await safe_send(message.reply,"❌ Неверное значение. /AiTurik для отмены."); return
    ok=await update_t_setting(edit["chat_id"],f,v); TOURNAMENT_EDIT.pop(message.from_user.id,None)
    if not ok: await safe_send(message.reply,"❌ Не сохранилось."); return
    s=await get_t_settings(edit["chat_id"])
    try: await message.delete()
    except Exception: pass
    await safe_send(message.answer,turik_text(s),reply_markup=turik_kb(s))
@dp.callback_query(F.data.startswith("turik:"))
async def on_turik_cb(cb:CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id): await cb.answer("⛔",show_alert=True); return
    if not isinstance(cb.message,Message): await cb.answer(); return
    cid=cb.message.chat.id; parts=cb.data.split(":"); action=parts[1] if len(parts)>1 else ""
    if action in ("refresh","menu"):
        s=await get_t_settings(cid); await safe_edit(cb.message.edit_text,turik_text(s),reply_markup=turik_kb(s)); await cb.answer(); return
    if action=="winners_count":
        s=await get_t_settings(cid); cur=s.get("winners_count",1)
        def m(n): return f"{'✅ ' if n==cur else ''}Топ-{n}"
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=m(1),callback_data="turik:wc_set:1")],[InlineKeyboardButton(text=m(2),callback_data="turik:wc_set:2")],[InlineKeyboardButton(text=m(3),callback_data="turik:wc_set:3")],[InlineKeyboardButton(text=m(5),callback_data="turik:wc_set:5")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="turik:menu")]])
        await safe_edit(cb.message.edit_text,f"🏆 <b>Сколько победителей?</b>\n\nПриз делится <b>пропорционально очкам</b>.\n\nТекущее: <b>{cur}</b>",reply_markup=kb); await cb.answer(); return
    if action=="wc_set":
        try: n=int(parts[2])
        except (ValueError,IndexError): await cb.answer("Ошибка",show_alert=True); return
        if n not in (1,2,3,5): await cb.answer("Ошибка",show_alert=True); return
        ok=await update_t_setting(cid,"winners_count",n)
        if not ok: await cb.answer("❌ Не сохранилось",show_alert=True); return
        await cb.answer(f"Победителей: {n}"); s=await get_t_settings(cid); await safe_edit(cb.message.edit_text,turik_text(s),reply_markup=turik_kb(s)); return
    if action=="difficulty":
        s=await get_t_settings(cid); cur=s.get("difficulty","medium")
        def m(d): return f"{'✅ ' if d==cur else ''}{DIFFICULTY_LABELS[d]}"
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=m("easy"),callback_data="turik:diff_set:easy")],[InlineKeyboardButton(text=m("medium"),callback_data="turik:diff_set:medium")],[InlineKeyboardButton(text=m("hard"),callback_data="turik:diff_set:hard")],[InlineKeyboardButton(text=m("extreme"),callback_data="turik:diff_set:extreme")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="turik:menu")]])
        await safe_edit(cb.message.edit_text,"🎯 <b>Сложность турнира</b>\n\n🟢 Легко\n🟡 Средне\n🟠 Сложно\n🔴 Экстрим",reply_markup=kb); await cb.answer(); return
    if action=="diff_set":
        d=parts[2] if len(parts)>2 else ""
        if d not in ("easy","medium","hard","extreme"): await cb.answer("Ошибка",show_alert=True); return
        ok=await update_t_setting(cid,"difficulty",d)
        if not ok: await cb.answer("❌ Не сохранилось",show_alert=True); return
        await cb.answer(f"Сложность: {DIFFICULTY_LABELS[d]}"); s=await get_t_settings(cid); await safe_edit(cb.message.edit_text,turik_text(s),reply_markup=turik_kb(s)); return
    if action=="set":
        f=parts[2]
        if f not in ("question_seconds","questions","prize"): await cb.answer("Ошибка",show_alert=True); return
        TOURNAMENT_EDIT[cb.from_user.id]={"chat_id":cid,"field":f}
        p={"question_seconds":"⏱ Секунд на вопрос? (5-300)","questions":"❓ Всего вопросов? (3-50)","prize":"💰 Приз в USDT? (0.1-100)"}[f]
        await cb.answer("Жду число..."); await safe_send(cb.message.answer,p+"\n\n<i>Отмена: /AiTurik</i>"); return
    if action=="start":
        if cid in TOURNAMENT_ACTIVE: await cb.answer("⏳ Уже идёт",show_alert=True); return
        s=await get_t_settings(cid); tid=await tournament_create(cid,float(s["prize"]))
        if not tid: await cb.answer("Ошибка",show_alert=True); return
        await cb.answer("Запускаю!"); asyncio.create_task(run_tournament(tid,cid,s)); return

# ===== ПОДПИСКА UI =====

@dp.message(Command("AiSubscribe"))
async def cmd_aisub(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user:
        await safe_send(message.reply,"Только в чате"); return
    uid=message.from_user.id
    exp=await get_subscription(uid); now=datetime.now(timezone.utc)
    if exp and exp>now:
        delta=exp-now; d=delta.days; h=int(delta.total_seconds()//3600%24)
        await safe_send(message.reply,
            f"💎 <b>Подписка активна</b>\n\n"
            f"📅 Осталось: <b>{d} дн. {h} ч.</b>\n"
            f"⏰ До: <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>\n\n"
            f"🔥 Множитель очков: <b>×{SUBSCRIBER_MULTIPLIER:.0f}</b>")
        return
    ciid=f"sub_{uid}_{uuid.uuid4().hex[:12]}"
    ok,res=await xrocket_create_invoice(ciid, SUBSCRIPTION_PRICE, f"Subscription {SUBSCRIPTION_DAYS}d (user {uid})")
    if not ok:
        await safe_send(message.reply,f"❌ <code>{res}</code>"); return
    kb=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"💳 Оплатить ${SUBSCRIPTION_PRICE:.2f}", url=res)
    ]])
    sent=await safe_send(message.reply,
        f"💎 <b>Подписка ×{SUBSCRIBER_MULTIPLIER:.0f} очков</b>\n\n"
        f"💵 Цена: <b>${SUBSCRIPTION_PRICE:.2f}</b> за <b>{SUBSCRIPTION_DAYS} дней</b>\n"
        f"📈 Множитель: <b>×{SUBSCRIBER_MULTIPLIER:.0f}</b> к очкам\n\n"
        f"Нажми кнопку ниже → оплати в @xrocket → подписка активируется автоматически.",
        reply_markup=kb)
    mid=sent.message_id if sent else None
    await create_invoice(ciid, uid, message.chat.id, SUBSCRIPTION_PRICE, mid, kind="subscription")

# =============== ХЕНДЛЕРЫ ===============

@dp.message(CommandStart())
async def cmd_start(message:Message):
    if message.from_user: TOURNAMENT_EDIT.pop(message.from_user.id,None)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Подписка $0.50/нед",url=XROCKET_SUBSCRIBE_URL)],[InlineKeyboardButton(text="🔗 Партнёрка xRocket",url=XROCKET_REFERRAL_URL)]])
    await safe_send(message.reply,f"👋 <b>Викторина с дуэлями!</b>\n\n🎯 Квиз: +1 очко\n📈 10 уровней\n🎴 /AiCard — карточка\n🎲 Дуэли: <code>/AiDuel 0.20</code>\n💳 <code>/AiDeposit 1.0</code>\n💸 Вывод от ${MIN_WITHDRAW:.2f}\n\n📖 /AiHelp · 📜 /AiRules",reply_markup=kb)

@dp.message(Command("AiHelp"))
async def cmd_aihelp(message:Message):
    if message.chat.type not in ("group","supergroup"): return
    text=("📖 <b>СПРАВКА</b>\n\n<b>🎮 Игра</b>\n/AiBalance · /AiProfile · /AiCard · /AiTop · /AiLevels · /AiCoins\n/AiDuel 0.20 — дуэль\n\n<b>💰 Деньги</b>\n"
          f"/AiDeposit 0.05 — пополнить (от ${DEPOSIT_MIN:.2f})\n/AiWithdraw — вывод от ${MIN_WITHDRAW:.2f}\n"
          f"/AiSubscribe — подписка ×{SUBSCRIBER_MULTIPLIER:.0f} (${SUBSCRIPTION_PRICE:.2f}/{SUBSCRIPTION_DAYS}дн)\n/AiSponsor — спонсорские вопросы (в ЛС)\n\n<b>📜 Общее</b>\n/AiRules · /AiHelp\n")
    if message.from_user and is_admin(message.from_user.id):
        text+=("\n<b>🛠 Админ</b>\n/AiAdmin — панель\n/AiTurik — турниры\n/AiGive &lt;id&gt; &lt;сумма&gt; — выдать\n"
               "/AiSubGive &lt;id&gt; [дней] — подписка\n/AiSubInfo &lt;id&gt; · /AiSubList · /AiSubDel &lt;id&gt;\n"
               "/AiPot · /AiPotAdd · /AiPotTake · /AiPotGive\n/AiBan &lt;id&gt; [время] [причина]\n/AiUnban &lt;id&gt;\n")
    await safe_send(message.reply,text)

@dp.message(Command("AiRules"))
async def cmd_airules(message:Message):
    if message.chat.type not in ("group","supergroup"): return
    await safe_send(message.reply,"📜 <b>ПРАВИЛА</b>\n\n1. Оскорбления — бан.\n2. Обход бана — перманентный.\n3. Скрипты — бан.\n4. Спам — бан.\n5. Фиктивные дуэли — бан обоим.\n6. Обман вывода — бан + обнуление.\n\n"+f"💸 Вывод: ${MIN_WITHDRAW:.2f} · ${DAILY_WITHDRAW_LIMIT:.2f}/сутки\n🎲 Рейк: {RAKE_PCT*100:.0f}%\n\n<i>Незнание не освобождает.</i>")

@dp.message(Command("AiCoins"))
async def cmd_aicoins(message:Message):
    if message.chat.type not in ("group","supergroup"): return
    st=await get_coins_stats(message.chat.id)
    if not st: await safe_send(message.reply,"Ошибка"); return
    await safe_send(message.reply,f"💼 <b>Экономика чата</b>\n\n👥 Игроков: <b>{st['players']}</b>\n💰 Балансы: <b>${st['balance']:.4f}</b>\n🎯 Очков: <b>{st['points']}</b>\n\n💸 Выплачено: <b>${st['paid']:.4f}</b>\n🏦 Касса: <b>${st['house']:.4f}</b>")

@dp.message(Command("AiSponsor"))
async def cmd_sponsor(message:Message):
    if message.chat.type!="private": await safe_send(message.reply,"Только в ЛС"); return
    uid=message.from_user.id
    if uid in SPONSOR_SESSION:
        s=SPONSOR_SESSION[uid]
        if not s.get("chat_id"): await safe_send(message.reply,"📌 Укажи <b>ID чата</b>.\nнапр: <code>-1002712583382</code>")
        else: await safe_send(message.reply,f"📝 Режим спонсора\nОсталось: <b>{s['target']-s['collected']}</b>\n\nФормат: <code>вопрос | ответ</code>")
        return
    parts=(message.text or "").split()
    if len(parts)!=2 or parts[1]!="5":
        await safe_send(message.reply,f"🎁 <b>Спонсорские вопросы</b>\n\nЦена: <b>${SPONSOR_PRICE:.2f}</b> = <b>{SPONSOR_QUESTIONS} вопросов</b>\n\nОплата: <code>/AiSponsor 5</code>"); return
    ciid=f"sponsor_{uid}_{uuid.uuid4().hex[:12]}"; ok,res=await xrocket_create_invoice(ciid,SPONSOR_PRICE,f"Sponsor {uid}")
    if not ok: await safe_send(message.reply,f"❌ <code>{res}</code>"); return
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💳 Оплатить ${SPONSOR_PRICE:.2f}",url=res)]])
    await create_invoice(ciid,uid,0,SPONSOR_PRICE,None,kind="sponsor")
    await safe_send(message.reply,f"🎁 <b>Пакет спонсора</b>\n\n💰 <b>${SPONSOR_PRICE:.2f}</b>\n📝 <b>{SPONSOR_QUESTIONS}</b>\n\nПосле оплаты бот попросит ID чата.",reply_markup=kb)

@dp.message(F.chat.type=="private",F.text,~F.text.startswith("/"))
async def handle_sponsor_input(message:Message):
    if not message.from_user or not message.text: raise SkipHandler()
    uid=message.from_user.id; s=SPONSOR_SESSION.get(uid)
    if not s: raise SkipHandler()
    if not s.get("chat_id"):
        try: cid=int(message.text.strip())
        except ValueError: await safe_send(message.reply,"❌ Неверный ID"); return
        s["chat_id"]=cid; await safe_send(message.reply,f"✅ Чат: <code>{cid}</code>\n\nОтправляй: <code>вопрос | ответ</code>\nОсталось: <b>{s['target']}</b>"); return
    t=message.text.strip()
    if "|" not in t: await safe_send(message.reply,"❌ Формат: <code>вопрос | ответ</code>"); return
    q,a=t.split("|",1); q,a=q.strip(),a.strip().lower()
    if not q or not a: await safe_send(message.reply,"❌ Пусто"); return
    pos=s["collected"]+1; await sponsor_add(uid,s["chat_id"],q,a,pos); s["collected"]+=1; left=s["target"]-s["collected"]
    if left<=0:
        await safe_send(message.reply,f"🎉 Все {s['target']} приняты!"); SPONSOR_SESSION.pop(uid,None)
    else: await safe_send(message.reply,f"✅ Принято. Осталось: <b>{left}</b>")

def admin_kb(cid):
    en=cid in QUIZ_ENABLED
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏸ Выкл" if en else "▶️ Вкл",callback_data="adm:toggle")],
        [InlineKeyboardButton(text="❓ Вопрос",callback_data="adm:ask"),InlineKeyboardButton(text="📊 Стата",callback_data="adm:stats")],
        [InlineKeyboardButton(text="💼 Касса",callback_data="adm:house"),InlineKeyboardButton(text="💸 Выплаты",callback_data="adm:payouts")],
        [InlineKeyboardButton(text="📋 Топ",callback_data="adm:top"),InlineKeyboardButton(text="🚫 Баны",callback_data="adm:bans")],
        [InlineKeyboardButton(text="🎯 Сложность",callback_data="adm:difficulty"),InlineKeyboardButton(text="🎰 Копилка",callback_data="adm:pot")],
        [InlineKeyboardButton(text="🧪 xRocket",callback_data="adm:xrdbg")]])
def admin_text(cid):
    st="🟢 вкл" if cid in QUIZ_ENABLED else "🔴 выкл"; cur=ACTIVE_QUESTIONS.get(cid)
    ct=f"\n🔓 Открыт: {cur['question']}" if cur else ""
    return f"🛠 <b>Админ</b>\nВикторина: {st}\nРейк: <b>{RAKE_PCT*100:.0f}%</b>\nДепозит: ${DEPOSIT_MIN:.2f}\nВывод: ${MIN_WITHDRAW:.2f}{ct}"

@dp.message(Command("AiAdmin"))
async def cmd_aiadmin(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    if message.chat.type not in ("group","supergroup"): return
    await safe_send(message.reply,admin_text(message.chat.id),reply_markup=admin_kb(message.chat.id))

@dp.message(Command("AiHouse"))
async def cmd_house(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    if message.chat.type not in ("group","supergroup"): return
    t=await get_house_total(message.chat.id); ta=await get_house_total(None); by=await get_house_by_source()
    lines=[f"💼 <b>Касса</b>\n",f"💰 Чат: <b>${t:.4f}</b>",f"📈 Всего: <b>${ta:.4f}</b>\n"]
    if by:
        lines.append("<b>Источники:</b>")
        for s,a in sorted(by.items(),key=lambda x:-x[1]): lines.append(f"• {s}: ${a:.4f}")
    await safe_send(message.reply,"\n".join(lines))

@dp.message(Command("AiPot"))
async def cmd_aipot(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    if message.chat.type not in ("group","supergroup"): return
    p=await get_pot(message.chat.id)
    await safe_send(message.reply,f"🎰 <b>Копилка</b>\n\nСейчас: <b>${p:.4f}</b>\n\n/AiPotAdd &lt;сумма&gt;\n/AiPotTake &lt;сумма&gt;\n/AiPotGive\n\nАвто: <b>{POT_HOUR}:00 МСК</b>")

@dp.message(Command("AiPotAdd"))
async def cmd_aipotadd(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)!=2: await safe_send(message.reply,"Формат: <code>/AiPotAdd 0.50</code>"); return
    try: a=round(float(parts[1]),4)
    except ValueError: await safe_send(message.reply,"Число."); return
    if a<=0: await safe_send(message.reply,"&gt; 0"); return
    np=await add_to_pot(message.chat.id,a)
    if np is None: await safe_send(message.reply,"Ошибка"); return
    await safe_send(message.reply,f"✅ +${a:.4f}\n🎰 ${np:.4f}")

@dp.message(Command("AiPotTake"))
async def cmd_aipottake(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)!=2: await safe_send(message.reply,"Формат: <code>/AiPotTake 0.50</code>"); return
    try: a=round(float(parts[1]),4)
    except ValueError: await safe_send(message.reply,"Число."); return
    if a<=0: await safe_send(message.reply,"&gt; 0"); return
    ok,np=await pot_take(message.chat.id,a)
    if not ok: await safe_send(message.reply,f"❌ Только ${np:.4f}"); return
    await add_balance(message.chat.id,message.from_user.id,a)
    await safe_send(message.reply,f"✅ Снято ${a:.4f}\n🎰 ${np:.4f}")

@dp.message(Command("AiPotGive"))
async def cmd_aipotgive(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    if message.chat.type not in ("group","supergroup"): return
    ok,info=await distribute_pot(message.chat.id,reason="manual")
    if not ok: await safe_send(message.reply,f"❌ {info}")

@dp.message(Command("AiGive"))
async def cmd_aigive(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    if message.chat.type not in ("group","supergroup"): return
    parts=(message.text or "").split()
    if len(parts)<3: await safe_send(message.reply,"Формат: <code>/AiGive &lt;user_id&gt; &lt;сумма&gt; [причина]</code>"); return
    try: target=int(parts[1]); amt=round(float(parts[2]),4)
    except ValueError: await safe_send(message.reply,"user_id и сумма — числа."); return
    if amt<=0 or amt>100: await safe_send(message.reply,"Сумма: 0.0001-100"); return
    reason=" ".join(parts[3:]) if len(parts)>3 else "admin_give"
    await get_player(message.chat.id,target)
    nb=await add_balance(message.chat.id,target,amt)
    if nb is None: await safe_send(message.reply,"❌ Ошибка начисления"); return
    try: await bot.send_message(target,f"🎁 <b>Начисление от админа</b>\n\n💰 +${amt:.4f} USDT\n💼 Баланс: <b>${nb:.4f}</b>\n📝 {reason}")
    except Exception: pass
    await safe_send(message.reply,f"✅ <code>{target}</code> +${amt:.4f}\n💰 Баланс: ${nb:.4f}\n📝 {reason}")

@dp.message(Command("AiSubGive"))
async def cmd_subgive(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)<2:
        await safe_send(message.reply,"Формат: <code>/AiSubGive &lt;user_id&gt; [дней]</code>"); return
    try:
        uid=int(parts[1]); days=int(parts[2]) if len(parts)>2 else SUBSCRIPTION_DAYS
    except ValueError:
        await safe_send(message.reply,"user_id и дни — числа."); return
    exp=await activate_subscription(uid, days)
    if not exp:
        await safe_send(message.reply,"❌ Ошибка активации"); return
    await safe_send(message.reply,f"✅ <code>{uid}</code> — подписка до <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>")
    try: await bot.send_message(uid,f"🎉 <b>Подписка активирована!</b>\n\n📅 До: <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>\n🔥 ×{SUBSCRIBER_MULTIPLIER:.0f} очков")
    except Exception: pass

@dp.message(Command("AiSubInfo"))
async def cmd_subinfo(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split()
    uid=message.from_user.id
    if len(parts)>=2:
        try: uid=int(parts[1])
        except ValueError: await safe_send(message.reply,"user_id — число"); return
    exp=await get_subscription(uid); now=datetime.now(timezone.utc)
    if exp and exp>now:
        d=(exp-now).days; h=int((exp-now).total_seconds()//3600%24)
        await safe_send(message.reply,f"💎 <code>{uid}</code> подписан до <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>\n⏳ Осталось: {d} дн {h} ч")
    else:
        await safe_send(message.reply,f"❌ <code>{uid}</code> без подписки")

@dp.message(Command("AiSubList"))
async def cmd_sublist(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    rows=await list_subscriptions(50)
    if not rows: await safe_send(message.reply,"Активных подписок нет."); return
    now=datetime.now(timezone.utc); lines=["💎 <b>Активные подписки</b>"]
    for r in rows:
        try: exp=datetime.fromisoformat(str(r["expires_at"]).replace("Z","+00:00"))
        except Exception: continue
        if exp<=now: continue
        d=(exp-now).days
        lines.append(f"• <code>{r['user_id']}</code> — {d} дн. (до {exp.strftime('%d.%m')})")
    if len(lines)==1: await safe_send(message.reply,"Активных подписок нет."); return
    await safe_send(message.reply,"\n".join(lines))

@dp.message(Command("AiSubDel"))
async def cmd_subdel(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)!=2:
        await safe_send(message.reply,"Формат: <code>/AiSubDel &lt;user_id&gt;</code>"); return
    try: uid=int(parts[1])
    except ValueError: await safe_send(message.reply,"user_id — число"); return
    ok=await deactivate_subscription(uid)
    await safe_send(message.reply,"✅ Снято" if ok else "❌ Ошибка")

def parse_duration(s):
    s=s.lower().strip()
    if s in ("perm","forever","навсегда","permanent"): return None
    u={"m":60,"h":3600,"d":86400,"w":604800}
    if s and s[-1] in u:
        try: return timedelta(seconds=int(s[:-1])*u[s[-1]])
        except ValueError: return "error"
    return "error"

@dp.message(Command("AiBan"))
async def cmd_aiban(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split(maxsplit=3)
    if len(parts)<2: await safe_send(message.reply,"📛 <code>/AiBan &lt;id&gt; [время] [причина]</code>"); return
    try: t=int(parts[1])
    except ValueError: await safe_send(message.reply,"user_id — целое"); return
    dur=None; reason="без причины"; bu=None
    if len(parts)>=3:
        p=parse_duration(parts[2])
        if p=="error": reason=" ".join(parts[2:])
        else:
            dur=p; reason=parts[3] if len(parts)>3 else "без причины"
            if dur is not None: bu=(datetime.now(timezone.utc)+dur).isoformat()
    await ban_user(message.chat.id,t,reason,message.from_user.id,bu)
    if bu: await safe_send(message.reply,f"🔨 <code>{t}</code> до <b>{bu[:19].replace('T',' ')} UTC</b>\nПричина: {reason}")
    else: await safe_send(message.reply,f"🔨 <code>{t}</code> <b>навсегда</b>\nПричина: {reason}")

@dp.message(Command("AiUnban"))
async def cmd_aiunban(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)!=2: await safe_send(message.reply,"Формат: <code>/AiUnban &lt;id&gt;</code>"); return
    try: t=int(parts[1])
    except ValueError: await safe_send(message.reply,"user_id — целое"); return
    await unban_user(message.chat.id,t); await safe_send(message.reply,f"✅ <code>{t}</code> разбанен")

@dp.callback_query(F.data.startswith("adm:"))
async def on_admin_cb(cb:CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id): await cb.answer("⛔",show_alert=True); return
    if not isinstance(cb.message,Message): await cb.answer(); return
    cid=cb.message.chat.id; action=cb.data.split(":")[1]
    if action=="toggle":
        if cid in QUIZ_ENABLED:
            QUIZ_ENABLED.discard(cid); ACTIVE_QUESTIONS.pop(cid,None); await clear_active(cid); await cb.answer("Выкл")
        else: QUIZ_ENABLED.add(cid); await cb.answer("Вкл")
        await safe_edit(cb.message.edit_text,admin_text(cid),reply_markup=admin_kb(cid)); return
    if action=="ask": QUIZ_ENABLED.add(cid); await cb.answer("Задаю..."); asyncio.create_task(ask_question(cid)); return
    if action=="difficulty":
        s=await get_chat_settings(cid); cur=s.get("difficulty","medium")
        def m(d): return f"{'✅ ' if d==cur else ''}{DIFFICULTY_LABELS[d]}"
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=m("easy"),callback_data="adm:diff_set:easy")],[InlineKeyboardButton(text=m("medium"),callback_data="adm:diff_set:medium")],[InlineKeyboardButton(text=m("hard"),callback_data="adm:diff_set:hard")],[InlineKeyboardButton(text=m("extreme"),callback_data="adm:diff_set:extreme")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:menu")]])
        await safe_edit(cb.message.edit_text,f"🎯 <b>Сложность</b>\n\nТекущая: <b>{DIFFICULTY_LABELS.get(cur,cur)}</b>\n\n🟢 Легко\n🟡 Средне\n🟠 Сложно\n🔴 Экстрим",reply_markup=kb); await cb.answer(); return
    if action=="diff_set":
        d=cb.data.split(":")[2]
        if d not in ("easy","medium","hard","extreme"): await cb.answer("Ошибка",show_alert=True); return
        ok=await update_chat_setting(cid,"difficulty",d)
        if not ok: await cb.answer("❌",show_alert=True); return
        await cb.answer(f"ОК: {DIFFICULTY_LABELS[d]}")
        s=await get_chat_settings(cid); cur=s.get("difficulty","medium")
        def mm(x): return f"{'✅ ' if x==cur else ''}{DIFFICULTY_LABELS[x]}"
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=mm("easy"),callback_data="adm:diff_set:easy")],[InlineKeyboardButton(text=mm("medium"),callback_data="adm:diff_set:medium")],[InlineKeyboardButton(text=mm("hard"),callback_data="adm:diff_set:hard")],[InlineKeyboardButton(text=mm("extreme"),callback_data="adm:diff_set:extreme")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:menu")]])
        await safe_edit(cb.message.edit_reply_markup,reply_markup=kb); return
    if action=="menu": await safe_edit(cb.message.edit_text,admin_text(cid),reply_markup=admin_kb(cid)); await cb.answer(); return
    if action=="stats":
        await cb.answer("Собираю..."); pl,po,ba,su=await get_stats()
        tb=sum(float(p["balance"]) for p in pl); tc=sum(int(p["correct_answers"]) for p in pl)
        fin=[p for p in po if p["status"]=="finished"]; ps=sum(float(p["amount"]) for p in fin)
        h=await get_house_total(cid); pt=await get_pot(cid)
        await safe_send(cb.message.answer,f"📊 <b>Стата</b>\n\n👥 Игроков: {len(pl)}\n🏆 Очков: {tc}\n💎 Подписчиков: {len(su)}\n🚫 Забанено: {len(ba)}\n\n💰 Балансы: ${tb:.4f}\n💼 Касса: ${h:.4f}\n🎰 Копилка: ${pt:.4f}\n💸 Выплат: {len(fin)} (${ps:.4f})"); return
    if action=="pot":
        await cb.answer(); p=await get_pot(cid)
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎉 Раздать сейчас",callback_data="adm:potgive")],[InlineKeyboardButton(text="🔄 Обновить",callback_data="adm:pot")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:menu")]])
        await safe_send(cb.message.answer,f"🎰 <b>Копилка</b>\n\nВ фонде: <b>${p:.4f}</b>\n\n/AiPotAdd 0.50\n/AiPotTake 0.50\n\nАвто: <b>{POT_HOUR}:00 МСК</b>",reply_markup=kb); return
    if action=="potgive":
        await cb.answer("Раздаю..."); ok,info=await distribute_pot(cid,reason="manual")
        if not ok: await safe_send(cb.message.answer,f"❌ {info}")
        return
    if action=="house":
        await cb.answer(); t=await get_house_total(cid); ta=await get_house_total(None); by=await get_house_by_source()
        lines=[f"💼 <b>Касса</b>\n",f"💰 Чат: <b>${t:.4f}</b>",f"📈 Всего: <b>${ta:.4f}</b>\n"]
        if by:
            lines.append("<b>Источники:</b>")
            for s,a in sorted(by.items(),key=lambda x:-x[1]): lines.append(f"• {s}: ${a:.4f}")
        await safe_send(cb.message.answer,"\n".join(lines)); return
    if action=="payouts":
        await cb.answer(); rows=await get_payouts(20)
        if not rows: await safe_send(cb.message.answer,"Выплат не было."); return
        lines=["💸 <b>Выплаты</b>"]
        for p in rows:
            dt=(p.get("created_at") or "")[:19].replace("T"," "); em="✅" if p["status"]=="finished" else "❌"
            lines.append(f"{em} ${float(p['amount']):.4f} · <code>{p['user_id']}</code> · {dt}")
        await safe_send(cb.message.answer,"\n".join(lines)); return
    if action=="top":
        await cb.answer(); rows=await get_top(cid,10)
        if not rows: await safe_send(cb.message.answer,"Никто не играл."); return
        lines=["🏆 <b>Топ</b>"]
        for i,r in enumerate(rows,1):
            ca=int(r.get("correct_answers",0)); _,em,_,_,_,_=level_info(ca)
            nm=r.get("first_name") or r.get("username") or str(r["user_id"]); med=["🥇","🥈","🥉"][i-1] if i<=3 else f"{i}."
            lines.append(f"{med} {em} {nm} — {ca} очк · ${float(r['balance']):.4f}")
        await safe_send(cb.message.answer,"\n".join(lines)); return
    if action=="bans":
        await cb.answer()
        def q():
            try: return supabase.table("quiz_bans").select("*").eq("chat_id",cid).execute().data or []
            except Exception: return []
        rows=await asyncio.to_thread(q)
        if not rows: await safe_send(cb.message.answer,"Забаненных нет."); return
        lines=["🚫 <b>Забаненные</b>"]
        for b in rows:
            u=b.get("banned_until")
            if u: lines.append(f"<code>{b['user_id']}</code> — до {u[:19].replace('T',' ')} UTC · {b.get('reason','—')}")
            else: lines.append(f"<code>{b['user_id']}</code> — навсегда · {b.get('reason','—')}")
        await safe_send(cb.message.answer,"\n".join(lines)); return
    if action=="xrdbg":
        await cb.answer("Проверяю..."); msg=await safe_send(cb.message.answer,"⏳..."); info=await xrocket_debug()
        if msg:
            try: await msg.edit_text(info)
            except Exception: pass
        return

async def xrocket_debug():
    lines=["🧪 <b>xRocket</b>",f"Key: <code>{XROCKET_API_KEY[:12]}...</code>",""]
    if not XROCKET_API_KEY: lines.append("❌ пусто"); return "\n".join(lines)
    try:
        s=await get_http()
        for u in (f"{XROCKET_BASE}/api/v1/me",f"{XROCKET_BASE}/api/v1/balance"):
            try:
                async with s.get(u,headers={"Authorization":f"Bearer {XROCKET_API_KEY}"}) as r:
                    t=(await r.text())[:200]; lines.append(f"[{r.status}] <code>{u}</code>\n<code>{t}</code>\n")
            except Exception as e: lines.append(f"[ERR] {u}: <code>{e}</code>")
    except Exception as e: lines.append(f"[ERR] {e}")
    return "\n".join(lines)

@dp.message(Command("AiBalance"))
async def cmd_aibalance(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    p=await get_player(message.chat.id,message.from_user.id,message.from_user.username,message.from_user.first_name)
    td=await withdrawn_today(message.chat.id,message.from_user.id); ca=int(p["correct_answers"])
    _,_,_,_,ts,_=level_info(ca); bar=make_progress_bar(ca); sub=is_subscriber_cached(message.chat.id,message.from_user.id)
    sl=f"\n💎 Подписка · ×{SUBSCRIBER_MULTIPLIER:.0f}" if sub else ""
    await safe_send(message.reply,f"💰 <b>${float(p['balance']):.4f} USDT</b>\n🎖 {ts}\n🏆 Очков: <b>{ca}</b>\n<code>{bar}</code>{sl}\n💸 Сегодня: ${td:.4f} / ${DAILY_WITHDRAW_LIMIT:.2f}\n💳 /AiDeposit · 🎴 /AiCard")

@dp.message(Command("AiCard"))
async def cmd_aicard(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    cid,uid=message.chat.id,message.from_user.id
    p=await get_player(cid,uid,message.from_user.username,message.from_user.first_name)
    ca=int(p["correct_answers"]); lvl,_,nm,_,_,_=level_info(ca)
    place=await get_player_place(cid,uid); td=await withdrawn_today(cid,uid)
    sub=is_subscriber_cached(cid,uid)
    name=message.from_user.first_name or "Player"
    try: await bot.send_chat_action(cid,"upload_photo")
    except Exception: pass
    avatar=await fetch_avatar(uid)
    try:
        png=render_profile_card(name,message.from_user.username,lvl,ca,place,float(p["balance"]),td,sub,avatar)
        buf=BufferedInputFile(png,filename=f"card_{uid}.png")
        await safe_send(bot.send_photo,cid,buf,caption=f"<b>{name}</b> · {nm} (ур. {lvl})")
    except Exception as e:
        log.warning("card: %s",e)
        await safe_send(message.reply,f"🎴 {nm} (ур. {lvl})\n🏆 {ca} очк · 💰 ${float(p['balance']):.4f}\n📍 #{place if place else '—'}")

@dp.message(Command("AiProfile"))
async def cmd_aiprofile(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    p=await get_player(message.chat.id,message.from_user.id,message.from_user.username,message.from_user.first_name)
    ca=int(p["correct_answers"]); lvl,_,_,_,ts,_=level_info(ca); bar=make_progress_bar(ca)
    sub=is_subscriber_cached(message.chat.id,message.from_user.id)
    place=await get_player_place(message.chat.id,message.from_user.id); ps=f"#{place}" if place else "—"
    td=await withdrawn_today(message.chat.id,message.from_user.id)
    nl="🏆 Максимальный уровень!" if lvl>=MAX_LEVEL else f"⬆️ До {LEVELS[lvl][1]} <b>{LEVELS[lvl][2]}</b>: <b>{ANSWERS_PER_LEVEL-(ca-(lvl-1)*ANSWERS_PER_LEVEL)}</b> очк."
    sl=f"\n💎 Подписка: <b>активна</b>" if sub else "\n💎 Подписка: нет"
    await safe_send(message.reply,f"👤 <b>{message.from_user.first_name}</b>\n\n🎖 <b>{ts}</b>{sl}\n\n<code>{bar}</code>\n{nl}\n\n💰 <b>${float(p['balance']):.4f}</b>\n💸 ${td:.4f}\n🏆 <b>{ca}</b>\n📍 <b>{ps}</b>\n\n🎴 /AiCard — карточка")

@dp.message(Command("AiLevels"))
async def cmd_ailevels(message:Message):
    if message.chat.type not in ("group","supergroup"): return
    lines=["🎖 <b>Уровни</b>\n"]
    for lvl,em,nm in LEVELS:
        mn=(lvl-1)*ANSWERS_PER_LEVEL; req=f"{mn}+" if lvl==MAX_LEVEL else f"{mn}-{mn+ANSWERS_PER_LEVEL-1}"
        lines.append(f"{em} <b>Ур. {lvl}</b> · {nm} · <i>{req} очк.</i>")
    await safe_send(message.reply,"\n".join(lines))

@dp.message(Command("AiTop"))
async def cmd_aitop(message:Message):
    if message.chat.type not in ("group","supergroup"): return
    rows=await get_top(message.chat.id,10)
    if not rows: await safe_send(message.reply,"Никто не играл."); return
    av=message.from_user and is_admin(message.from_user.id); lines=["🏆 <b>Топ</b>"]
    for i,r in enumerate(rows,1):
        ca=int(r.get("correct_answers",0)); _,em,_,_,_,_=level_info(ca)
        nm=r.get("first_name") or r.get("username") or str(r["user_id"]); med=["🥇","🥈","🥉"][i-1] if i<=3 else f"{i}."
        uid=f" · <code>{r['user_id']}</code>" if av else ""
        lines.append(f"{med} {em} {nm} — {ca} очк · ${float(r['balance']):.4f}{uid}")
    await safe_send(message.reply,"\n".join(lines))

@dp.message(Command("AiDeposit"))
async def cmd_deposit(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    parts=(message.text or "").split()
    if len(parts)!=2: await safe_send(message.reply,f"💳 <b>Пополнение</b>\n\nФормат: <code>/AiDeposit 1.0</code>\nМин: ${DEPOSIT_MIN:.2f} · Макс: ${DEPOSIT_MAX:.2f}"); return
    try: a=round(float(parts[1]),4)
    except ValueError: await safe_send(message.reply,"Число."); return
    if a<DEPOSIT_MIN or a>DEPOSIT_MAX: await safe_send(message.reply,f"${DEPOSIT_MIN:.2f}–${DEPOSIT_MAX:.2f}"); return
    ciid=f"dep_{message.from_user.id}_{uuid.uuid4().hex[:12]}"; ok,res=await xrocket_create_invoice(ciid,a,f"Deposit {message.from_user.id}")
    if not ok: await safe_send(message.reply,f"❌ <code>{res}</code>"); return
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💳 Оплатить ${a:.2f}",url=res)]])
    sent=await safe_send(message.reply,f"💳 <b>Счёт</b>\n\nСумма: <b>${a:.4f}</b> USDT\nДействителен 1 час.\n\nОплати в @xrocket.",reply_markup=kb)
    mid=sent.message_id if sent else None; await create_invoice(ciid,message.from_user.id,message.chat.id,a,mid,kind="deposit")

@dp.message(Command("AiDuel"))
async def cmd_duel(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    cid,uid=message.chat.id,message.from_user.id
    if is_banned_cached(cid,uid): await safe_send(message.reply,"🚫 Бан"); return
    parts=(message.text or "").split()
    if len(parts)<2: await safe_send(message.reply,f"🎲 <b>Дуэль</b>\n\n<code>/AiDuel 0.20</code>\nСтавка: ${DUEL_MIN:.2f}–${DUEL_MAX:.2f}"); return
    try: a=round(float(parts[1]),4)
    except ValueError: await safe_send(message.reply,"Число."); return
    if a<DUEL_MIN or a>DUEL_MAX: await safe_send(message.reply,f"${DUEL_MIN:.2f}–${DUEL_MAX:.2f}"); return
    oid=onm=None
    if message.reply_to_message and message.reply_to_message.from_user:
        o=message.reply_to_message.from_user; oid,onm=o.id,o.first_name
    elif message.entities:
        for e in message.entities:
            if e.type=="text_mention" and e.user: oid,onm=e.user.id,e.user.first_name; break
    if not oid: await safe_send(message.reply,"Ответь на сообщение противника."); return
    if oid==uid: await safe_send(message.reply,"Себе нельзя 😄"); return
    if oid==bot.id: await safe_send(message.reply,"С ботом нельзя 😄"); return
    pc=await get_player(cid,uid,message.from_user.username,message.from_user.first_name); po=await get_player(cid,oid)
    if float(pc["balance"])<a: await safe_send(message.reply,f"❌ У тебя ${float(pc['balance']):.4f}"); return
    if float(po["balance"])<a: await safe_send(message.reply,f"❌ У противника ${float(po['balance']):.4f}"); return
    if uid in DUEL_BUSY or oid in DUEL_BUSY: await safe_send(message.reply,"⏳ Кто-то в дуэли"); return
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Принять",callback_data=f"duel:a:{uid}:{oid}:{a}"),InlineKeyboardButton(text="❌ Отклонить",callback_data=f"duel:r:{uid}:{oid}:{a}")]])
    sent=await safe_send(message.reply,f"🎲 <b>Дуэль!</b>\n\n<b>{message.from_user.first_name}</b> vs <b>{onm}</b>\n\n💵 <b>${a:.4f}</b>\n💰 Банк: <b>${a*2:.4f}</b>\n<i>Рейк {RAKE_PCT*100:.0f}%</i>",reply_markup=kb)
    if not sent: return
    async def ac():
        await asyncio.sleep(DUEL_TTL)
        try:
            await bot.edit_message_reply_markup(cid,sent.message_id,reply_markup=None); await bot.edit_message_text(chat_id=cid,message_id=sent.message_id,text=f"⌛ <b>Истекла.</b>\n{onm} не ответил.")
        except Exception: pass
    asyncio.create_task(ac())

@dp.callback_query(F.data.startswith("duel:"))
async def on_duel_cb(cb:CallbackQuery):
    if not cb.from_user or not isinstance(cb.message,Message): await cb.answer(); return
    parts=cb.data.split(":")
    if len(parts)!=5: await cb.answer("Ошибка",show_alert=True); return
    _,action,cs,os_,as_=parts
    try: ch=int(cs); op=int(os_); a=float(as_)
    except ValueError: await cb.answer("Ошибка",show_alert=True); return
    cid=cb.message.chat.id
    if cb.from_user.id!=op: await cb.answer("Не твой вызов.",show_alert=True); return
    if action=="r":
        try: await cb.message.edit_text(f"❌ <b>Отклонено.</b>\n{cb.from_user.first_name} отказался.")
        except Exception: pass
        await cb.answer("Отклонено"); return
    await cb.answer("Поехали!")
    if ch in DUEL_BUSY or op in DUEL_BUSY:
        try: await cb.message.edit_text("⏳ Кто-то в дуэли")
        except Exception: pass
        return
    DUEL_BUSY.add(ch); DUEL_BUSY.add(op)
    try:
        pc=await get_player(cid,ch); po=await get_player(cid,op)
        if float(pc["balance"])<a or float(po["balance"])<a:
            try: await cb.message.edit_text("❌ У кого-то не хватает")
            except Exception: pass
            return
        if not await deduct_balance(cid,ch,a):
            try: await cb.message.edit_text("❌ Не списать у вызывающего")
            except Exception: pass
            return
        if not await deduct_balance(cid,op,a):
            await add_balance(cid,ch,a)
            try: await cb.message.edit_text("❌ Не списать у соперника")
            except Exception: pass
            return
        nc=pc.get("first_name") or str(ch); no=po.get("first_name") or str(op)
        try: await cb.message.edit_text(f"🎲 <b>Началась!</b>\n\n💰 Банк: <b>${a*2:.4f}</b>\n🎯 {nc} vs {no}")
        except Exception: pass
        await asyncio.sleep(1); m1=await safe_send(bot.send_dice,cid,emoji="🎲"); r1=m1.dice.value if m1 and m1.dice else 0
        await asyncio.sleep(2); m2=await safe_send(bot.send_dice,cid,emoji="🎲"); r2=m2.dice.value if m2 and m2.dice else 0
        await asyncio.sleep(2)
        if r1==r2:
            await add_balance(cid,ch,a); await add_balance(cid,op,a)
            await safe_send(bot.send_message,cid,f"🤝 <b>Ничья! {r1}:{r2}</b>\nСтавки возвращены."); return
        wid,wn=(ch,nc) if r1>r2 else (op,no); lid=op if wid==ch else ch
        tp=round(a*2,4); rk=round(tp*RAKE_PCT,4); pay=round(tp-rk,4)
        await add_balance(cid,wid,pay); await log_house_income(cid,rk,"duel")
        await safe_send(bot.send_message,cid,f"🏆 <b>{wn} победил!</b>\n\n🎲 {nc}: <b>{r1}</b>\n🎲 {no}: <b>{r2}</b>\n\n💰 <b>${pay:.4f}</b>\n<i>рейк {RAKE_PCT*100:.0f}% = ${rk:.4f}</i>")
        try: await bot.send_message(lid,f"💔 <b>Проиграл</b> против {wn}\nСтавка <b>${a:.4f}</b> списана.")
        except Exception: pass
    finally:
        DUEL_BUSY.discard(ch); DUEL_BUSY.discard(op)

@dp.message(Command("AiWithdraw"))
async def cmd_aiwithdraw(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    cid,uid=message.chat.id,message.from_user.id
    if is_banned_cached(cid,uid): await safe_send(message.reply,"🚫 Бан"); return
    p=await get_player(cid,uid,message.from_user.username,message.from_user.first_name); bal=float(p["balance"])
    if bal<MIN_WITHDRAW: await safe_send(message.reply,f"❌ Мин ${MIN_WITHDRAW:.2f}. У тебя ${bal:.4f}"); return
    td=await withdrawn_today(cid,uid); rem=DAILY_WITHDRAW_LIMIT-td
    if rem<=0: await safe_send(message.reply,"❌ Лимит на сутки"); return
    a=min(bal,rem)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Принять",callback_data=f"wd:accept:{uid}"),InlineKeyboardButton(text="❌ Отклонить",callback_data=f"wd:reject:{uid}")]])
    sent=await safe_send(message.reply,f"💸 <b>Вывод</b>\n\nСумма: <b>${a:.4f}</b> USDT\nКуда: <code>{uid}</code>\n\n⚠️ Зайди в <a href=\"{XROCKET_REFERRAL_URL}\">@xrocket</a>.\n\nЗапрос: {WITHDRAW_CONFIRM_TTL//60} мин.",reply_markup=kb)
    if not sent: return
    PENDING_WITHDRAWS[sent.message_id]={"chat_id":cid,"user_id":uid,"amount":a,"ts":time.time()}
    async def ac():
        await asyncio.sleep(WITHDRAW_CONFIRM_TTL); info=PENDING_WITHDRAWS.pop(sent.message_id,None)
        if not info: return
        try: await bot.edit_message_text(chat_id=cid,message_id=sent.message_id,text="⌛ <b>Запрос истёк.</b> /AiWithdraw снова.")
        except Exception: pass
    asyncio.create_task(ac())

@dp.callback_query(F.data.startswith("wd:"))
async def on_withdraw_cb(cb:CallbackQuery):
    if not cb.from_user or not isinstance(cb.message,Message): await cb.answer(); return
    parts=cb.data.split(":")
    if len(parts)!=3: await cb.answer("Ошибка",show_alert=True); return
    action,os_=parts[1],parts[2]
    try: oid=int(os_)
    except ValueError: await cb.answer("Ошибка",show_alert=True); return
    if cb.from_user.id!=oid: await cb.answer("⛔ Не твой.",show_alert=True); return
    info=PENDING_WITHDRAWS.pop(cb.message.message_id,None)
    if not info: await cb.answer("⌛ Истёк",show_alert=True); return
    cid,uid,a=info["chat_id"],info["user_id"],info["amount"]
    if action=="reject":
        try: await cb.message.edit_text(f"❌ Отменено. ${a:.4f} на балансе.")
        except Exception: pass
        await cb.answer("Отменено"); return
    await cb.answer("Принято...")
    if not await try_lock_withdraw(cid,uid):
        try: await cb.message.edit_text("⏳ Другой вывод обрабатывается")
        except Exception: pass
        return
    try:
        p=await get_player(cid,uid); bal=float(p["balance"]); a=min(bal,a)
        if a<MIN_WITHDRAW:
            try: await cb.message.edit_text(f"❌ Мин ${MIN_WITHDRAW:.2f}")
            except Exception: pass
            return
        try: await cb.message.edit_text(f"⏳ Отправляю ${a:.4f}...")
        except Exception: pass
        ok,res=await xrocket_payout(cid,uid,a)
        if ok:
            await deduct_balance(cid,uid,a); await log_payout(cid,uid,a,res,"finished")
            try: await cb.message.edit_text(f"✅ <b>Выплачено ${a:.4f}</b>\nID: <code>{res}</code>")
            except Exception: pass
        else:
            await log_payout(cid,uid,a,"","failed")
            try: await cb.message.edit_text(f"❌ <b>Ошибка</b>\n<code>{res}</code>")
            except Exception: pass
    finally: await unlock_withdraw(cid,uid)

@dp.message(F.text & ~F.text.startswith("/") & F.chat.type.in_({"group","supergroup"}))
async def handle_answer(message:Message):
    if not message.from_user: return
    if message.from_user.id in TOURNAMENT_EDIT: raise SkipHandler()
    cid,uid=message.chat.id,message.from_user.id; text=(message.text or "").strip().lower()
    if cid in TOURNAMENT_ACTIVE:
        st=TOURNAMENT_STATE.get(cid)
        if st and not st.get("answered_by") and text in st["answers"]: st["answered_by"]=uid
        return
    q=ACTIVE_QUESTIONS.get(cid)
    if not q: return
    if text not in q["answers"]:
        if is_admin(uid):
            try: await bot.set_message_reaction(cid,message.message_id,["❌"])
            except Exception: pass
        return
    if is_banned_cached(cid,uid): return
    popped=ACTIVE_QUESTIONS.pop(cid,None)
    if popped is None: return
    tsk=popped.get("timer_task")
    if tsk: tsk.cancel()
    p,_=await asyncio.gather(get_player(cid,uid,message.from_user.username,message.from_user.first_name),clear_active(cid),return_exceptions=True)
    if isinstance(p,Exception) or p is None: p=await get_player(cid,uid,message.from_user.username,message.from_user.first_name)
    ol=level_from_correct(int(p["correct_answers"])); ot=TOP_CACHE.get(cid)
    is_sub=is_subscriber_cached(cid,uid)
    points_gain=2 if is_sub else 1
    await add_score(cid,uid)
    if points_gain>1: await add_score(cid,uid)
    nc=int(p["correct_answers"])+points_gain; nl=level_from_correct(nc)
    TOP1_COUNTER[cid]=TOP1_COUNTER.get(cid,0)+1
    if TOP1_COUNTER[cid]%TOP1_CHECK_EVERY==0:
        nt=await get_top1(cid)
        if nt and nt!=ot:
            TOP_CACHE[cid]=nt
            if ot is not None:
                try:
                    tp=await get_player(cid,nt); nm=tp.get("first_name") or tp.get("username") or str(nt)
                    await safe_send(bot.send_message,cid,f"👑 <b>{nm}</b> вышел на первое место!")
                except Exception: pass
    phrase=random.choice(CORRECT_PHRASES)
    if q["is_multi"]: ash="любой из: "+", ".join(q["answers"][:5])+("..." if len(q["answers"])>5 else "")
    else: ash=q["answers"][0]
    pts_line=f"×{points_gain} очка 💎" if is_sub else "+1 очко"
    msg=f"{phrase}\n{message.from_user.first_name} {pts_line}\n<i>Ответ: {ash}</i>"
    if nl>ol: msg+=f"\n\n{LEVELS[nl-1][1]} <b>НОВЫЙ УРОВЕНЬ {nl}!</b>\n🎖 {LEVELS[nl-1][2]}"
    sent=await safe_send(message.reply,msg)
    if sent:
        try: await bot.set_message_reaction(cid,message.message_id,["✅"])
        except Exception: pass

async def main():
    print("="*50); print("Quiz Bot · подписка через xRocket-инвойс")
    print(f"Easy: {len(EASY_QUESTIONS)} · Medium: {len(MEDIUM_QUESTIONS)} · Hard: {len(HARD_QUESTIONS)} · Extreme: {len(EXTREME_QUESTIONS)}")
    print(f"Админы: {sorted(ADMIN_IDS)}")
    await get_http(); await asyncio.to_thread(unlock_all_withdrawals_sync)
    global BANNED_CACHE,SUBSCRIBERS_CACHE
    BANNED_CACHE=await asyncio.to_thread(load_bans_sync); SUBSCRIBERS_CACHE=await asyncio.to_thread(load_subscribers_sync)
    active_rows=await asyncio.to_thread(load_active_sync)
    for row in active_rows:
        answers=row["answer"].split("||")
        ACTIVE_QUESTIONS[int(row["chat_id"])]={"question":row["question"],"answers":[a.lower() for a in answers],"is_multi":row.get("is_multi",False),"timer":False,"timer_task":None}
        QUIZ_ENABLED.add(int(row["chat_id"]))
    for cid in QUIZ_ENABLED:
        try:
            t=await get_top1(cid)
            if t: TOP_CACHE[cid]=t
        except Exception: pass
    me=await bot.get_me(); print(f"Подключился как @{me.username}")
    await start_webhook_server()
    asyncio.create_task(question_scheduler()); asyncio.create_task(caches_refresh_loop()); asyncio.create_task(pot_payout_loop())
    print("Запущен."); print("="*50)
    try: await dp.start_polling(bot)
    finally: await close_http()

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
    except Exception as e:
        print("!!! УПАЛ !!!"); print(type(e).__name__,"-",e); raise
