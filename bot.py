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
SUBSCRIBER_MULTIPLIER=2.0; SUBSCRIPTION_PRICE=0.50; SUBSCRIPTION_DAYS=7
DEPOSIT_COMMISSION=0.05; WITHDRAW_COMMISSION=0.05
BASE_MONEY_PER_CORRECT=0.05
MONEY_PER_LEVEL=0.005
MIN_WITHDRAW=0.05; DAILY_WITHDRAW_LIMIT=5.00; DEPOSIT_MIN=0.05; DEPOSIT_MAX=50.0
DUEL_MIN=0.05; DUEL_MAX=1.00; DUEL_TTL=120; TIMER_PROBABILITY=0.20; TIMER_SECONDS=10
POT_PERCENT=0.05; POT_HOUR=21; SPONSOR_PRICE=5.00; SPONSOR_QUESTIONS=20
LOTTERY_PRICE=0.05; LOTTERY_HOUR=21; LOTTERY_WINNER_SHARE=0.50; LOTTERY_MAX_TICKETS=20
LOTTERY_PACKS={1:0.05, 5:0.20, 10:0.35}
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
TOURNAMENT_STATE={}; TOURNAMENT_ACTIVE={}; TOURNAMENT_EDIT={}; SPONSOR_SESSION={}; AVATAR_CACHE={}
ACTIVE_BOOSTERS={}
BOOSTER_TYPES={"points":{"name":"Очки за ответ","emoji":"🎯"},"money":{"name":"Деньги за ответ","emoji":"💰"},"commission":{"name":"Комиссия","emoji":"💸"}}

def is_admin(uid): return uid in ADMIN_IDS
def level_from_correct(c): return min(c//ANSWERS_PER_LEVEL+1,MAX_LEVEL)
def level_info(c):
    lvl=level_from_correct(c); emoji,name=LEVELS[lvl-1][1],LEVELS[lvl-1][2]
    ts=f"{emoji} {name} (ур. {lvl})"; p="🏆 Максимальный уровень!" if lvl>=MAX_LEVEL else f"до след. уровня: {ANSWERS_PER_LEVEL-(c-(lvl-1)*ANSWERS_PER_LEVEL)} отв."
    return lvl,emoji,name,0,ts,p
def money_for_answer(correct_answers_before):
    lvl=level_from_correct(correct_answers_before)
    return round(BASE_MONEY_PER_CORRECT+(lvl-1)*MONEY_PER_LEVEL,4)
def make_progress_bar(c):
    lvl=level_from_correct(c)
    if lvl>=MAX_LEVEL: return "▓"*10+" 10/10"
    il=c-(lvl-1)*ANSWERS_PER_LEVEL
    return f"{'▓'*il}{'▒'*(ANSWERS_PER_LEVEL-il)} {il}/{ANSWERS_PER_LEVEL}"
def get_font(size=64, bold=False):
    global _FONT_PATH,_FONT_PATH_BOLD
    cb=["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf","/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"]
    cr=["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf","/usr/share/fonts/TTF/DejaVuSans.ttf"]
    if (bold and _FONT_PATH_BOLD is None) or (not bold and _FONT_PATH is None):
        for p in (cb if bold else cr):
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
    bb=draw.textbbox((0,0),text,font=font); tw,th=bb[2]-bb[0],bb[3]-bb[1]
    while tw>W-60:
        cs=getattr(font,"size",0)
        if cs<=20: break
        font=get_font(cs-5,bold=True); bb=draw.textbbox((0,0),text,font=font); tw,th=bb[2]-bb[0],bb[3]-bb[1]
    draw.text(((W-tw)/2-bb[0],(H-th)/2-bb[1]),text,fill=(20,20,80),font=font)
    buf=io.BytesIO(); img.save(buf,format="PNG"); return buf.getvalue()
async def fetch_avatar(uid):
    now=time.time(); c=AVATAR_CACHE.get(uid)
    if c and now-c[1]<3600: return c[0]
    try:
        ph=await bot.get_user_profile_photos(uid,limit=1)
        if not ph or not ph.total_count: AVATAR_CACHE[uid]=(None,now); return None
        s=ph.photos[0]; fid=s[-1].file_id if len(s)>0 else s[0].file_id
        f=await bot.get_file(fid); buf=io.BytesIO(); await bot.download_file(f.file_path,buf)
        d=buf.getvalue(); AVATAR_CACHE[uid]=(d,now); return d
    except Exception as e:
        log.warning("avatar: %s",e); AVATAR_CACHE[uid]=(None,now); return None
def _cmask(sz):
    m=Image.new("L",(sz,sz),0); ImageDraw.Draw(m).ellipse([0,0,sz-1,sz-1],fill=255); return m
def render_profile_card(name,username,lvl,correct,place,balance,wd,is_sub,avatar_bytes=None):
    W,H=720,1120
    img=Image.new("RGB",(W,H),(10,10,28)); draw=ImageDraw.Draw(img)
    for y in range(H):
        t=y/H; draw.line([(0,y),(W,y)],fill=(int(16+50*t),int(12+26*t),int(42+120*t)))
    for cx,cy,rad,col in [(W-40,100,220,(255,200,60,22)),(40,H-120,280,(120,80,255,16))]:
        ov=Image.new("RGBA",(W,H),(0,0,0,0)); ImageDraw.Draw(ov).ellipse([cx-rad,cy-rad,cx+rad,cy+rad],fill=col)
        img=Image.alpha_composite(img.convert("RGBA"),ov).convert("RGB")
    draw=ImageDraw.Draw(img); draw.rounded_rectangle([16,16,W-17,H-17],radius=32,outline=(255,210,70),width=3)
    avs=230; ax=(W-avs)//2; ay=80
    draw.ellipse([ax-7,ay-7,ax+avs+7,ay+avs+7],fill=(255,210,70))
    draw.ellipse([ax,ay,ax+avs,ay+avs],fill=(30,32,64))
    if avatar_bytes:
        try:
            av=Image.open(io.BytesIO(avatar_bytes)).convert("RGB").resize((avs,avs),Image.LANCZOS)
            img.paste(av,(ax,ay),_cmask(avs)); draw=ImageDraw.Draw(img)
        except Exception: avatar_bytes=None
    if not avatar_bytes:
        f=get_font(110,bold=True); L=(name or "?")[0].upper()
        bb=draw.textbbox((0,0),L,font=f); tw,th=bb[2]-bb[0],bb[3]-bb[1]
        draw.text((ax+avs/2-tw/2-bb[0],ay+avs/2-th/2-bb[1]),L,fill=(255,210,70),font=f)
    def ctr(txt,f,y,fill):
        bb=draw.textbbox((0,0),txt,font=f); tw=bb[2]-bb[0]
        draw.text(((W-tw)/2-bb[0],y-bb[1]),txt,fill=fill,font=f)
    nm=(name or "Player").strip()
    if "@" in nm: nm=nm.split("@",1)[0].strip() or "Player"
    if len(nm)>20: nm=nm[:19]+"."
    ctr(nm,get_font(44,bold=True),352,(255,255,255))
    if username:
        ctr(f"@{username[:26]}",get_font(23),412,(150,160,200)); y_lvl=458
    else: y_lvl=412
    ctr(f"LEVEL {lvl}   {LEVEL_LATIN.get(lvl,'')}".strip(),get_font(30,bold=True),y_lvl,(255,210,70))
    if is_sub: bt="SUBSCRIBER  x2"; bc=(190,55,175); fb=get_font(22,bold=True); btc=(255,255,255)
    else: bt="FREE USER"; bc=(50,50,80); fb=get_font(22); btc=(180,180,210)
    bb=draw.textbbox((0,0),bt,font=fb); bw=bb[2]-bb[0]+56; bh=44
    bx=(W-bw)//2; by=y_lvl+60
    draw.rounded_rectangle([bx,by,bx+bw,by+bh],radius=bh//2,fill=bc)
    bbc=draw.textbbox((0,0),bt,font=fb); tw,th=bbc[2]-bbc[0],bbc[3]-bbc[1]
    draw.text((bx+(bw-tw)/2-bbc[0],by+(bh-th)/2-bbc[1]),bt,fill=btc,font=fb)
    sy=by+bh+50; draw.line([(80,sy),(W-80,sy)],fill=(180,150,60),width=1)
    fl=get_font(17); fv=get_font(40,bold=True)
    ccx=[W*0.28,W*0.72]; ry=[sy+45,sy+165]
    for i,(lbl,val,col) in enumerate([("POINTS",str(correct),(100,220,255)),("RANK",f"#{place}" if place else "—",(255,210,70)),("BALANCE",f"${balance:.4f}",(95,255,145)),("WITHDRAWN TODAY",f"${wd:.4f}",(255,160,90))]):
        cx=ccx[i%2]; cy=ry[i//2]
        bb=draw.textbbox((0,0),lbl,font=fl); tw=bb[2]-bb[0]
        draw.text((cx-tw/2-bb[0],cy),lbl,fill=(140,150,190),font=fl)
        bb=draw.textbbox((0,0),val,font=fv); tw=bb[2]-bb[0]
        draw.text((cx-tw/2-bb[0],cy+26-bb[1]),val,fill=col,font=fv)
    bx,by,bw,bh=60,H-170,W-120,48; il=correct-(lvl-1)*ANSWERS_PER_LEVEL
    if lvl>=MAX_LEVEL: fl2,bt=bw,"MAX LEVEL"
    else: fl2,bt=int(bw*il/ANSWERS_PER_LEVEL),f"{il} / {ANSWERS_PER_LEVEL}"
    draw.rounded_rectangle([bx,by,bx+bw,by+bh],radius=bh//2,fill=(28,28,54),outline=(70,70,110),width=2)
    if fl2>2: draw.rounded_rectangle([bx,by,bx+fl2,by+bh],radius=bh//2,fill=(255,210,70))
    fbar=get_font(22,bold=True); bb=draw.textbbox((0,0),bt,font=fbar); tw,th=bb[2]-bb[0],bb[3]-bb[1]
    bcx=bx+bw/2; tcol=(25,25,45) if fl2>(bcx-bx) else (230,230,240)
    draw.text((bcx-tw/2-bb[0],by+bh/2-th/2-bb[1]),bt,fill=tcol,font=fbar)
    ft="xRocket Quiz Bot"; bb=draw.textbbox((0,0),ft,font=get_font(17)); tw=bb[2]-bb[0]
    draw.text(((W-tw)/2-bb[0],H-58),ft,fill=(120,120,160),font=get_font(17))
    buf=io.BytesIO(); img.save(buf,format="PNG",optimize=True); return buf.getvalue()

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
        except TelegramRetryAfter as e: log.warning("Flood %s",e.retry_after); await asyncio.sleep(e.retry_after+1)
        except TelegramBadRequest as e:
            if "can't parse entities" in str(e) and "parse_mode" in kw:
                kw.pop("parse_mode",None)
                try: return await cf(*a,**kw)
                except Exception: return None
            log.warning("send: %s",e); return None
        except Exception as e: log.warning("send: %s",e); return None
    return None
async def safe_edit(cf,*a,**kw):
    try: return await cf(*a,**kw)
    except TelegramBadRequest as e:
        s=str(e)
        if "message is not modified" in s or "message to edit not found" in s: return None
        log.warning("edit: %s",e); return None
    except Exception as e: log.warning("edit: %s",e); return None
def _rpc(n,p):
    try: return supabase.rpc(n,p).execute()
    except Exception as e: log.warning("rpc %s: %s",n,e); return None
def _booster_get(cid,bt):
    b=ACTIVE_BOOSTERS.get(cid,{}).get(bt)
    if not b: return None
    u=b.get("until")
    if u and u<datetime.now(timezone.utc):
        ACTIVE_BOOSTERS.get(cid,{}).pop(bt,None); return None
    return b
def booster_mult(cid,bt,d=1.0):
    b=_booster_get(cid,bt); return b["multiplier"] if b else d
def _booster_set(cid,bt,m,s):
    if cid not in ACTIVE_BOOSTERS: ACTIVE_BOOSTERS[cid]={}
    until=None if s==0 else datetime.now(timezone.utc)+timedelta(seconds=s)
    ACTIVE_BOOSTERS[cid][bt]={"multiplier":float(m),"until":until}
def _booster_stop(cid,bt):
    if cid in ACTIVE_BOOSTERS: ACTIVE_BOOSTERS[cid].pop(bt,None)
def _bfmt(u):
    if not u: return "навсегда"
    t=int((u-datetime.now(timezone.utc)).total_seconds())
    if t<=0: return "истёк"
    h=t//3600; m=(t%3600)//60
    return f"{h}ч {m}м" if h>0 else f"{m}м"
def _boost_text(cid):
    lines=["🚀 <b>БУСТЕРЫ</b>",""]
    for bt,cfg in BOOSTER_TYPES.items():
        b=_booster_get(cid,bt)
        if b:
            if bt=="commission": st=f"🟢 0% · {_bfmt(b['until'])}"
            else: st=f"🟢 x{int(b['multiplier'])} · {_bfmt(b['until'])}"
        else: st="🔴 выкл"
        lines.append(f"{cfg['emoji']} {cfg['name']}: {st}")
    lines.append(""); lines.append("<i>Бустеры в памяти, сбрасываются при рестарте.</i>")
    return "\n".join(lines)
def _boost_kb(cid):
    rows=[]; row=[]
    for bt,cfg in BOOSTER_TYPES.items():
        row.append(InlineKeyboardButton(text=f"{cfg['emoji']} {cfg['name']}",callback_data=f"boost:pick:{bt}"))
        if len(row)==2: rows.append(row); row=[]
    if row: rows.append(row)
    if any(_booster_get(cid,t) for t in BOOSTER_TYPES): rows.append([InlineKeyboardButton(text="⏹ Выключить всё",callback_data="boost:stopall")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить",callback_data="boost:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def _bpick_text(cid,bt):
    cfg=BOOSTER_TYPES[bt]; b=_booster_get(cid,bt)
    if b:
        if bt=="commission": st=f"🟢 активен · {_bfmt(b['until'])}"
        else: st=f"🟢 активен · x{int(b['multiplier'])} · {_bfmt(b['until'])}"
    else: st="🔴 выключен"
    lines=[f"{cfg['emoji']} <b>БУСТЕР: {cfg['name']}</b>","",f"Статус: {st}",""]
    if bt=="points": lines.append("Множитель очков за ответ."); lines.append("<i>База: 1 очко (×2 с подпиской).</i>")
    elif bt=="money": lines.append("Множитель денег за ответ."); lines.append(f"<i>База: ${BASE_MONEY_PER_CORRECT:.4f} + ${MONEY_PER_LEVEL:.4f} за каждый уровень.</i>")
    else: lines.append("Убирает комиссию при пополнении и выводе.")
    lines.append(""); lines.append("Запусти кнопкой 👇")
    return "\n".join(lines)
def _bpick_kb(bt):
    rows=[]
    if bt=="commission":
        rows.append([InlineKeyboardButton(text="🚀 0% · 1ч",callback_data=f"boost:start:{bt}:0:3600"),InlineKeyboardButton(text="🚀 0% · 24ч",callback_data=f"boost:start:{bt}:0:86400")])
        rows.append([InlineKeyboardButton(text="🚀 0% · ∞",callback_data=f"boost:start:{bt}:0:0")])
    else:
        rows.append([InlineKeyboardButton(text="🚀 x2 · 1ч",callback_data=f"boost:start:{bt}:2:3600"),InlineKeyboardButton(text="🚀 x2 · 3ч",callback_data=f"boost:start:{bt}:2:10800")])
        rows.append([InlineKeyboardButton(text="🚀 x2 · 24ч",callback_data=f"boost:start:{bt}:2:86400"),InlineKeyboardButton(text="🚀 x3 · 1ч",callback_data=f"boost:start:{bt}:3:3600")])
        rows.append([InlineKeyboardButton(text="🚀 x5 · 1ч",callback_data=f"boost:start:{bt}:5:3600"),InlineKeyboardButton(text="🚀 x5 · 24ч",callback_data=f"boost:start:{bt}:5:86400")])
        rows.append([InlineKeyboardButton(text="🚀 x2 · ∞",callback_data=f"boost:start:{bt}:2:0")])
    rows.append([InlineKeyboardButton(text="⏹ Остановить",callback_data=f"boost:stop:{bt}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад",callback_data="boost:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# =============== ЛОТЕРЕЯ ===============

def loto_time_left():
    now=datetime.now(TZ); target=now.replace(hour=LOTTERY_HOUR,minute=0,second=0,microsecond=0)
    if target<=now: target+=timedelta(days=1)
    s=int((target-now).total_seconds()); return s//3600,(s%3600)//60
def loto_bank_sync(cid):
    try:
        r=supabase.table("quiz_invoices").select("amount").eq("chat_id",cid).eq("kind","lottery_ticket").eq("status","paid").execute()
        return round(sum(float(x["amount"]) for x in (r.data or [])),4)
    except Exception as e: log.warning("loto_bank: %s",e); return 0.0
def loto_tickets_sync(cid):
    try:
        r=supabase.table("quiz_invoices").select("user_id").eq("chat_id",cid).eq("kind","lottery_ticket").eq("status","paid").execute()
        return [int(x["user_id"]) for x in (r.data or [])]
    except Exception as e: log.warning("loto_tickets: %s",e); return []
def loto_my_sync(cid,uid):
    try:
        r=supabase.table("quiz_invoices").select("id").eq("chat_id",cid).eq("user_id",uid).eq("kind","lottery_ticket").eq("status","paid").execute()
        return len(r.data or [])
    except Exception as e: log.warning("loto_my: %s",e); return 0
def loto_buy_pack_sync(cid,uid,count,pack_price):
    try:
        per=round(pack_price/count,4)
        rows=[]
        for i in range(count):
            ciid=f"lotto_{cid}_{uid}_{uuid.uuid4().hex[:10]}"
            rows.append({"client_invoice_id":ciid,"user_id":uid,"chat_id":cid,"amount":per,"kind":"lottery_ticket","status":"paid","paid_at":datetime.now(timezone.utc).isoformat()})
        supabase.table("quiz_invoices").insert(rows).execute()
        return True
    except Exception as e: log.warning("loto_buy: %s",e); return False
def loto_clear_sync(cid):
    try:
        supabase.table("quiz_invoices").update({"kind":"lottery_drawn"}).eq("chat_id",cid).eq("kind","lottery_ticket").execute(); return True
    except Exception as e: log.warning("loto_clear: %s",e); return False
async def loto_bank(cid): return await asyncio.to_thread(loto_bank_sync,cid)
async def loto_tickets(cid): return await asyncio.to_thread(loto_tickets_sync,cid)
async def loto_my(cid,uid): return await asyncio.to_thread(loto_my_sync,cid,uid)
async def loto_buy_pack(cid,uid,count,pack_price): return await asyncio.to_thread(loto_buy_pack_sync,cid,uid,count,pack_price)
async def loto_clear(cid): return await asyncio.to_thread(loto_clear_sync,cid)

async def loto_text(cid,uid=None):
    bank=await loto_bank(cid); tickets=await loto_tickets(cid); total=len(tickets)
    h,m=loto_time_left()
    lines=["🎰 <b>ЛОТЕРЕЯ</b>","",
           f"💰 Банк: <b>${bank:.4f}</b>",
           f"🎟 Билетов продано: <b>{total}</b>",
           f"⏳ До розыгрыша: <b>{h}ч {m}м</b>","",
           f"🎫 Базовая цена: <b>${LOTTERY_PRICE:.2f}</b>","",
           "<b>🏆 Что получает победитель:</b>",
           "• 💵 <b>Возврат всех своих билетов</b>",
           f"• 💰 Плюс <b>{int(LOTTERY_WINNER_SHARE*100)}%</b> от оставшегося банка",
           "",
           "<i>Пример: банк $1.20, ты поставил $1 → получишь $1 + $0.10 = $1.10</i>","",
           "<b>📦 Пакеты (выгоднее):</b>"]
    for cnt,price in LOTTERY_PACKS.items():
        base=round(cnt*LOTTERY_PRICE,4)
        if price<base:
            disc=int(round((1-price/base)*100))
            lines.append(f"🎫 {cnt} бил. — <b>${price:.2f}</b> <s>${base:.2f}</s> (−{disc}%)")
        else:
            lines.append(f"🎫 {cnt} бил. — <b>${price:.2f}</b>")
    if uid is not None:
        my=await loto_my(cid,uid); lines.append(""); lines.append(f"🎟 У тебя билетов: <b>{my}</b>")
        if my>=LOTTERY_MAX_TICKETS: lines.append(f"<i>Достигнут лимит {LOTTERY_MAX_TICKETS}</i>")
    lines.append(""); lines.append(f"<i>Розыгрыш ежедневно в {LOTTERY_HOUR}:00 МСК · билеты сохраняются</i>")
    return "\n".join(lines)
def loto_kb(uid):
    rows=[]
    for cnt,price in LOTTERY_PACKS.items():
        base=round(cnt*LOTTERY_PRICE,4)
        if price<base:
            disc=int(round((1-price/base)*100))
            txt=f"🎫 {cnt} билетов — ${price:.2f} (−{disc}%)"
        else:
            txt=f"🎫 {cnt} билет — ${price:.2f}"
        rows.append([InlineKeyboardButton(text=txt,callback_data=f"loto:buy:{uid}:{cnt}")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить",callback_data=f"loto:refresh:{uid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
async def loto_draw(cid):
    bank=await loto_bank(cid); tickets=await loto_tickets(cid)
    if not tickets or bank<LOTTERY_PRICE*2:
        await loto_clear(cid); return
    winner=random.choice(tickets)
    try:
        r=supabase.table("quiz_invoices").select("amount").eq("chat_id",cid).eq("user_id",winner).eq("kind","lottery_ticket").eq("status","paid").execute()
        winner_spent=round(sum(float(x["amount"]) for x in (r.data or [])),4)
    except Exception:
        winner_spent=0.0
    remaining=round(bank-winner_spent,4)
    if remaining<0: remaining=0.0
    bonus=round(remaining*LOTTERY_WINNER_SHARE,4)
    payout=round(winner_spent+bonus,4)
    if payout>bank: payout=bank
    house=round(bank-payout,4)
    if payout>0: await add_balance(cid,winner,payout)
    if house>0:
        try: await log_house_income(cid,house,"lottery")
        except Exception: pass
    try:
        pl=await get_player(cid,winner); nm=pl.get("first_name") or pl.get("username") or str(winner)
    except Exception: nm=str(winner)
    await safe_send(bot.send_message,cid,
        f"🎰 <b>РОЗЫГРЫШ ЛОТЕРЕИ!</b>\n\n"
        f"💰 Банк: <b>${bank:.4f}</b>\n"
        f"🎟 Билетов: <b>{len(tickets)}</b>\n\n"
        f"🏆 <b>{nm}</b> (ID <code>{winner}</code>)\n\n"
        f"↩️ Возврат за его билеты: <b>${winner_spent:.4f}</b>\n"
        f"💵 Остаток банка: <b>${remaining:.4f}</b>\n"
        f"💰 Бонус ({int(LOTTERY_WINNER_SHARE*100)}% от остатка): <b>${bonus:.4f}</b>\n"
        f"🎉 <b>Итого выигрыш: ${payout:.4f}</b>\n"
        f"🏦 В кассу: <b>${house:.4f}</b>")
    await loto_clear(cid)
async def loto_remind(cid):
    bank=await loto_bank(cid); tickets=await loto_tickets(cid); total=len(tickets)
    h,m=loto_time_left()
    if bank<=0 and total==0:
        await safe_send(bot.send_message,cid,f"🎰 <b>ЛОТЕРЕЯ</b>\n\n💰 Банк пуст — стань первым!\n⏳ До розыгрыша: <b>{h}ч {m}м</b>\n\n🎫 Билет — <b>${LOTTERY_PRICE:.2f}</b>\n💎 Возврат билетов + {int(LOTTERY_WINNER_SHARE*100)}% банка\n👉 /AiLoto")
    else:
        await safe_send(bot.send_message,cid,f"🎰 <b>ЛОТЕРЕЯ</b> · напоминание\n\n💰 Банк: <b>${bank:.4f}</b>\n🎟 Билетов: <b>{total}</b>\n⏳ До розыгрыша: <b>{h}ч {m}м</b>\n\n🎫 Билет — <b>${LOTTERY_PRICE:.2f}</b> · 📦 5 за ${LOTTERY_PACKS[5]:.2f} · 💎 10 за ${LOTTERY_PACKS[10]:.2f}\n↩️ Возврат + {int(LOTTERY_WINNER_SHARE*100)}% банка\n👉 /AiLoto")
async def loto_loop():
    await asyncio.sleep(45)
    last_hour=datetime.now(TZ).strftime("%Y-%m-%d-%H")
    last_draw=datetime.now(TZ).date() if datetime.now(TZ).hour>=LOTTERY_HOUR else None
    while True:
        now=datetime.now(TZ); hkey=now.strftime("%Y-%m-%d-%H")
        if last_hour!=hkey:
            last_hour=hkey; h=now.hour; td=now.date()
            if h==LOTTERY_HOUR and last_draw!=td:
                last_draw=td
                for cid in list(QUIZ_ENABLED):
                    try: await loto_draw(cid)
                    except Exception as e: log.warning("loto draw: %s",e)
            elif h!=LOTTERY_HOUR and 8<=h<23:
                for cid in list(QUIZ_ENABLED):
                    try: await loto_remind(cid)
                    except Exception as e: log.warning("loto rem: %s",e)
        await asyncio.sleep(60)
@dp.message(Command("AiLoto"))
async def cmd_loto(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    cid,uid=message.chat.id,message.from_user.id
    if is_banned_cached(cid,uid): await safe_send(message.reply,"🚫"); return
    await safe_send(message.reply,await loto_text(cid,uid),reply_markup=loto_kb(uid))
@dp.callback_query(F.data.startswith("loto:"))
async def on_loto_cb(cb:CallbackQuery):
    if not cb.from_user or not isinstance(cb.message,Message): await cb.answer(); return
    p=cb.data.split(":")
    if len(p)<3: await cb.answer("Ошибка",show_alert=True); return
    act=p[1]
    try: owner=int(p[2])
    except ValueError: await cb.answer("Ошибка",show_alert=True); return
    if cb.from_user.id!=owner: await cb.answer("⛔ Не твоя кнопка",show_alert=True); return
    cid,uid=cb.message.chat.id,cb.from_user.id
    if act=="refresh":
        try: await cb.message.edit_text(await loto_text(cid,uid),reply_markup=loto_kb(uid))
        except Exception: pass
        await cb.answer(); return
    if act=="buy":
        try: count=int(p[3])
        except (ValueError,IndexError): count=1
        if count not in LOTTERY_PACKS: await cb.answer("Ошибка",show_alert=True); return
        price=LOTTERY_PACKS[count]
        my=await loto_my(cid,uid)
        if my+count>LOTTERY_MAX_TICKETS:
            await cb.answer(f"❌ Лимит {LOTTERY_MAX_TICKETS} (у тебя {my})",show_alert=True); return
        pl=await get_player(cid,uid)
        if float(pl["balance"])<price:
            await cb.answer(f"❌ Нужно ${price:.2f}",show_alert=True); return
        if not await deduct_balance(cid,uid,price):
            await cb.answer("❌",show_alert=True); return
        ok=await loto_buy_pack(cid,uid,count,price)
        if not ok:
            await add_balance(cid,uid,price); await cb.answer("❌ Ошибка записи",show_alert=True); return
        await cb.answer(f"🎫 Куплено {count} билетов! Всего: {my+count}",show_alert=True)
        try: await cb.message.edit_text(await loto_text(cid,uid),reply_markup=loto_kb(uid))
        except Exception: pass
        return
    await cb.answer()

def get_player_sync(cid,uid,un=None,fn=None):
    try:
        r=supabase.table("quiz_players").select("*").eq("chat_id",cid).eq("user_id",uid).execute()
        if r.data:
            row=r.data[0]; u={}
            if not row.get("first_name") and fn: u["first_name"]=fn
            if not row.get("username") and un: u["username"]=un
            if u:
                try: supabase.table("quiz_players").update(u).eq("chat_id",cid).eq("user_id",uid).execute(); row.update(u)
                except Exception: pass
            return row
    except Exception as e: log.warning("gplayer: %s",e)
    try: supabase.table("quiz_players").insert({"chat_id":cid,"user_id":uid,"username":un,"first_name":fn}).execute()
    except Exception as e: log.warning("gplayer ins: %s",e)
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
        r=supabase.table("quiz_house").select("amount,source").execute(); by={}
        for x in r.data or []: by[x["source"]]=by.get(x["source"],0.0)+float(x["amount"])
        return by
    except Exception: return {}
def add_to_pot_sync(cid,amt):
    try:
        r=supabase.table("quiz_pot").select("amount").eq("chat_id",cid).execute(); cu=float(r.data[0]["amount"]) if r.data else 0.0; na=round(cu+amt,4)
        if r.data: supabase.table("quiz_pot").update({"amount":na}).eq("chat_id",cid).execute()
        else: supabase.table("quiz_pot").insert({"chat_id":cid,"amount":na}).execute()
        return na
    except Exception as e: log.warning("add_pot: %s",e); return None
def payout_pot_sync(cid):
    try:
        r=supabase.table("quiz_pot").select("amount").eq("chat_id",cid).execute()
        if not r.data: return 0.0
        a=float(r.data[0]["amount"]); supabase.table("quiz_pot").update({"amount":0}).eq("chat_id",cid).execute(); return a
    except Exception: return 0.0
def get_pot_sync(cid):
    try:
        r=supabase.table("quiz_pot").select("amount").eq("chat_id",cid).execute(); return float(r.data[0]["amount"]) if r.data else 0.0
    except Exception: return 0.0
def pot_take_sync(cid,amt):
    try:
        r=supabase.table("quiz_pot").select("amount").eq("chat_id",cid).execute(); cu=float(r.data[0]["amount"]) if r.data else 0.0
        if cu<amt: return False,cu
        na=round(cu-amt,4); supabase.table("quiz_pot").update({"amount":na}).eq("chat_id",cid).execute(); return True,na
    except Exception: return False,0.0
def create_invoice_sync(ciid,uid,cid,amt,mid=None,kind="deposit"):
    try:
        r=supabase.table("quiz_invoices").insert({"client_invoice_id":ciid,"user_id":uid,"chat_id":cid,"amount":amt,"message_id":mid,"kind":kind}).execute(); return r.data[0]["id"] if r.data else None
    except Exception as e: log.warning("inv: %s",e); return None
def mark_invoice_paid_sync(ciid):
    try:
        r=supabase.table("quiz_invoices").select("*").eq("client_invoice_id",ciid).execute()
        if not r.data: return None
        inv=r.data[0]
        if inv["status"]=="paid": return None
        supabase.table("quiz_invoices").update({"status":"paid","paid_at":datetime.now(timezone.utc).isoformat()}).eq("client_invoice_id",ciid).execute(); return inv
    except Exception: return None
def load_bans_sync():
    try:
        r=supabase.table("quiz_bans").select("chat_id,user_id,banned_until").execute(); ca={}; n=datetime.now(timezone.utc)
        for row in r.data or []:
            bu=row.get("banned_until")
            if bu:
                try:
                    u=datetime.fromisoformat(bu.replace("Z","+00:00"))
                    if u<=n: continue
                except Exception: pass
            ca.setdefault(int(row["chat_id"]),set()).add(int(row["user_id"]))
        return ca
    except Exception: return {}
def load_subscribers_sync():
    try:
        ni=datetime.now(timezone.utc).isoformat()
        r=supabase.table("quiz_subscribers").select("chat_id,user_id").gt("expires_at",ni).execute(); ca={}
        for row in r.data or []: ca.setdefault(int(row["chat_id"]),set()).add(int(row["user_id"]))
        return ca
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
        m=supabase.table("quiz_players").select("correct_answers,balance").eq("chat_id",cid).eq("user_id",uid).execute()
        if not m.data: return None
        mca=int(m.data[0]["correct_answers"]); mb=float(m.data[0].get("balance") or 0)
        a=supabase.table("quiz_players").select("user_id").eq("chat_id",cid).gt("correct_answers",mca).execute().data or []
        b=supabase.table("quiz_players").select("user_id").eq("chat_id",cid).eq("correct_answers",mca).gt("balance",mb).execute().data or []
        return len(a)+len(b)+1
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
        r=supabase.table("quiz_settings").select("*").eq("chat_id",cid).execute()
        if r.data: return r.data[0]
    except Exception as e: log.warning("settings: %s",e)
    d={"chat_id":cid,"difficulty":"medium"}
    try:
        i=supabase.table("quiz_settings").insert(d).execute()
        if i.data: return i.data[0]
    except Exception as e:
        log.warning("settings ins: %s",e)
        try:
            r=supabase.table("quiz_settings").select("*").eq("chat_id",cid).execute()
            if r.data: return r.data[0]
        except Exception: pass
    return d
def update_chat_setting_sync(cid,f,v):
    try:
        get_chat_settings_sync(cid); supabase.table("quiz_settings").update({f:v}).eq("chat_id",cid).execute(); return True
    except Exception as e: log.warning("upd_set: %s",e); return False
def tournament_create_sync(cid,prize):
    try:
        r=supabase.table("quiz_tournaments").insert({"chat_id":cid,"prize":prize,"status":"active"}).execute(); return r.data[0]["id"] if r.data else None
    except Exception as e: log.warning("t_create: %s",e); return None
def tournament_finish_sync(tid,wid):
    try: supabase.table("quiz_tournaments").update({"status":"finished","finished_at":datetime.now(timezone.utc).isoformat(),"winner_id":wid}).eq("id",tid).execute()
    except Exception as e: log.warning("t_fin: %s",e)
def _pdi(d,w):
    d=(d or "medium").split("#")[0]; return d if int(w)==1 else f"{d}#w{int(w)}"
def _udi(raw):
    raw=raw or "medium"
    if "#w" in raw:
        d,w=raw.split("#w",1)
        try: return (d or "medium"),int(w)
        except ValueError: return (d or "medium"),1
    return raw,1
def get_t_settings_sync(cid):
    try:
        r=supabase.table("quiz_tournament_settings").select("*").eq("chat_id",cid).execute()
        if r.data:
            row=r.data[0]; d,wc=_udi(row.get("difficulty","medium")); row["difficulty"]=d; row["winners_count"]=wc; return row
    except Exception as e: log.warning("t_set: %s",e)
    d={"chat_id":cid,"question_seconds":30,"questions":10,"prize":0.30,"difficulty":"medium","winners_count":1}
    try:
        i=supabase.table("quiz_tournament_settings").insert(d).execute()
        if i.data:
            row=i.data[0]; row["difficulty"]="medium"; row["winners_count"]=1; return row
    except Exception as e:
        log.warning("t_set ins: %s",e)
        try:
            r=supabase.table("quiz_tournament_settings").select("*").eq("chat_id",cid).execute()
            if r.data:
                row=r.data[0]; dd,wc=_udi(row.get("difficulty","medium")); row["difficulty"]=dd; row["winners_count"]=wc; return row
        except Exception: pass
    return d
def update_t_setting_sync(cid,f,v):
    try:
        c=get_t_settings_sync(cid)
        if f=="winners_count":
            nd=_pdi(c.get("difficulty","medium"),int(v)); supabase.table("quiz_tournament_settings").update({"difficulty":nd}).eq("chat_id",cid).execute(); return True
        if f=="difficulty":
            nd=_pdi(v,c.get("winners_count",1)); supabase.table("quiz_tournament_settings").update({"difficulty":nd}).eq("chat_id",cid).execute(); return True
        supabase.table("quiz_tournament_settings").update({f:v}).eq("chat_id",cid).execute(); return True
    except Exception as e: log.warning("upd_t_set: %s",e); return False
def sponsor_add_sync(uid,cid,q,a,pos):
    try:
        r=supabase.table("quiz_sponsor_questions").insert({"sponsor_id":uid,"chat_id":cid,"question":q,"answer":a,"position":pos}).execute(); return r.data[0]["id"] if r.data else None
    except Exception as e: log.warning("sp_add: %s",e); return None
def sponsor_get_next_sync(cid):
    try:
        r=supabase.table("quiz_sponsor_questions").select("*").eq("chat_id",cid).eq("used",False).order("position").limit(1).execute(); return r.data[0] if r.data else None
    except Exception: return None
def sponsor_mark_used_sync(qid):
    try: supabase.table("quiz_sponsor_questions").update({"used":True}).eq("id",qid).execute()
    except Exception: pass
def activate_subscription_sync(uid,days):
    try:
        now=datetime.now(timezone.utc)
        r=supabase.table("quiz_subscribers").select("*").eq("chat_id",0).eq("user_id",uid).execute()
        base=now
        if r.data:
            cu=r.data[0].get("expires_at")
            if cu:
                try:
                    e=datetime.fromisoformat(str(cu).replace("Z","+00:00"))
                    if e>now: base=e
                except Exception: pass
            ne=base+timedelta(days=days)
            supabase.table("quiz_subscribers").update({"expires_at":ne.isoformat()}).eq("chat_id",0).eq("user_id",uid).execute(); return ne
        ne=now+timedelta(days=days)
        supabase.table("quiz_subscribers").insert({"chat_id":0,"user_id":uid,"expires_at":ne.isoformat()}).execute(); return ne
    except Exception as e: log.warning("act_sub: %s",e); return None
def get_subscription_sync(uid):
    try:
        r=supabase.table("quiz_subscribers").select("expires_at").eq("chat_id",0).eq("user_id",uid).execute()
        if not r.data: return None
        e=r.data[0].get("expires_at")
        if not e: return None
        return datetime.fromisoformat(str(e).replace("Z","+00:00"))
    except Exception as e: log.warning("get_sub: %s",e); return None
def list_subscriptions_sync(limit=50):
    try: return supabase.table("quiz_subscribers").select("user_id,expires_at").eq("chat_id",0).order("expires_at",desc=True).limit(limit).execute().data or []
    except Exception as e: log.warning("list_subs: %s",e); return []
def deactivate_subscription_sync(uid):
    try: supabase.table("quiz_subscribers").delete().eq("chat_id",0).eq("user_id",uid).execute(); return True
    except Exception as e: log.warning("dsub: %s",e); return False

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

async def xrocket_payout(cid,uid,amount):
    if not XROCKET_API_KEY: return False,"XROCKET_API_KEY не задан"
    payload={"clientPayoutId":f"quiz_{cid}_{uid}_{int(datetime.now().timestamp()*1000)}","target":str(uid),"targetType":"telegram_user_id","asset":"USDT","amount":f"{amount:.4f}","description":"Quiz reward"}
    try:
        s=await get_http()
        async with s.post(f"{XROCKET_BASE}/api/v1/payouts",headers={"Authorization":f"Bearer {XROCKET_API_KEY}","Content-Type":"application/json"},json=payload) as r:
            d=await r.json(); log.info("xR [%s] %s",r.status,d)
            if r.status in (200,201): return True,d.get("payoutId") or d.get("id") or "ok"
            return False,d.get("detail") or d.get("title") or str(d)
    except Exception as e: return False,str(e)
async def xrocket_create_invoice(ciid,amount,desc):
    if not XROCKET_API_KEY: return False,"XROCKET_API_KEY не задан"
    payload={"priceAmount":f"{amount:.4f}","priceCurrency":"USDT","numPayments":1,"clientInvoiceId":ciid,"description":desc[:1000],"expiresIn":3600000}
    try:
        s=await get_http()
        async with s.post(f"{XROCKET_BASE}/api/v1/invoices",headers={"Authorization":f"Bearer {XROCKET_API_KEY}","Content-Type":"application/json"},json=payload) as r:
            d=await r.json(); log.info("xR inv [%s] %s",r.status,d)
            if r.status in (200,201):
                iid=d.get("id"); return True,d.get("links",{}).get("telegramBotLink") or f"https://t.me/xRocket?start={iid}"
            return False,d.get("detail") or d.get("title") or str(d)
    except Exception as e: return False,str(e)
def verify_wh(raw,sig,ts,secret):
    if not sig or not ts or not secret: return False
    try: ti=int(ts)
    except (ValueError,TypeError): return False
    if ti>10_000_000_000: ti//=1000
    if abs(int(time.time())-ti)>WEBHOOK_MAX_AGE_SEC: log.warning("ts old"); return False
    sg=f"{ts}.{raw.decode('utf-8')}"; ex=hmac.new(secret.encode(),sg.encode(),hashlib.sha256).hexdigest(); return hmac.compare_digest(ex,sig)
async def handle_webhook(request):
    try:
        raw=await request.read(); sig=request.headers.get("Signature",""); ver=request.headers.get("Signature-Version",""); ts=request.headers.get("Signature-Timestamp","")
        if ver!="v1": return web.Response(status=401,text="bad version")
        if not verify_wh(raw,sig,ts,XROCKET_WEBHOOK_SECRET): return web.Response(status=401,text="bad sig")
        try: ev=json.loads(raw)
        except Exception: return web.Response(status=400,text="bad json")
        et=ev.get("type"); d=ev.get("data",{}); log.info("WH: %s",et)
        if et=="invoice" and d.get("event")=="invoice_status_changed":
            inv=d.get("invoice",{})
            if inv.get("status")=="paid":
                ciid=inv.get("clientInvoiceId")
                if ciid: await process_paid_invoice(ciid)
        return web.Response(status=200,text="ok")
    except Exception as e: log.error("WH: %s",e); return web.Response(status=200,text="ok")
async def process_paid_invoice(ciid):
    inv=await mark_invoice_paid(ciid)
    if not inv: return
    uid=int(inv["user_id"]); cid=int(inv["chat_id"]) if inv.get("chat_id") else uid
    amt=float(inv["amount"]); mid=inv.get("message_id"); kind=inv.get("kind") or "deposit"
    if kind=="subscription":
        exp=await activate_subscription(uid,SUBSCRIPTION_DAYS)
        try:
            global SUBSCRIBERS_CACHE
            SUBSCRIBERS_CACHE=await asyncio.to_thread(load_subscribers_sync)
        except Exception: pass
        if mid:
            try: await bot.edit_message_text(chat_id=cid,message_id=int(mid),text=f"✅ <b>Подписка активирована!</b>\n\n📅 До: <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>\n🔥 ×{SUBSCRIBER_MULTIPLIER:.0f}")
            except Exception: pass
        try: await bot.send_message(uid,f"🎉 <b>Подписка оформлена!</b>\n\n📅 До: <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>\n🔥 ×{SUBSCRIBER_MULTIPLIER:.0f}")
        except Exception: pass
        return
    if kind=="sponsor":
        SPONSOR_SESSION[uid]={"chat_id":None,"collected":0,"target":SPONSOR_QUESTIONS}
        try: await bot.send_message(uid,f"✅ <b>Оплата ${amt:.2f} получена!</b>\n\nТеперь напиши <b>ID чата</b>.\n<i>(напр: -1002712583382)</i>")
        except Exception: pass
        return
    cm=booster_mult(cid,"commission",1.0); ec=DEPOSIT_COMMISSION*cm
    com=round(amt*ec,4); cr=round(amt-com,4)
    if cr<0: cr=0
    nb=await add_balance(cid,uid,cr)
    if nb is None:
        p=await get_player(cid,uid); nb=float(p.get("balance",0))
    if com>0:
        try: await log_house_income(cid,com,"deposit_commission")
        except Exception: pass
    if mid:
        try: await bot.edit_message_text(chat_id=cid,message_id=int(mid),text=f"✅ <b>Пополнение успешно!</b>\n\n💳 Оплачено: <b>${amt:.4f}</b>\n🏦 Комиссия {ec*100:.0f}%: <b>-${com:.4f}</b>\n💰 Зачислено: <b>${cr:.4f}</b>\n💼 Баланс: <b>${nb:.4f}</b>")
        except Exception: pass
    try: await bot.send_message(uid,f"✅ <b>Баланс пополнен!</b>\n\n💳 Оплачено: <b>${amt:.4f}</b>\n🏦 Комиссия {ec*100:.0f}%: <b>-${com:.4f}</b>\n💰 Зачислено: <b>${cr:.4f}</b>\n💼 Баланс: <b>{nb:.4f}</b>")
    except Exception: pass
async def start_webhook_server():
    app=web.Application(); app.router.add_post("/webhook",handle_webhook); app.router.get("/",lambda r: web.Response(text="ok"))
    runner=web.AppRunner(app); await runner.setup(); site=web.TCPSite(runner,"0.0.0.0",PORT); await site.start(); log.info("Webhook on %s",PORT)

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
            msg=await safe_send(bot.send_photo,cid,buf,caption=f"🧠 <b>Вопрос!</b>{sn}\n\n{dl} · 🏆 +1 очко · 💰 ${BASE_MONEY_PER_CORRECT:.2f}+\n🔓 Вопрос открыт до правильного ответа.{pl}{tl}")
        else:
            msg=await safe_send(bot.send_message,cid,f"🧠 <b>Вопрос!</b>{sn}\n\n❓ {q}\n\n{dl} · 🏆 +1 очко · 💰 ${BASE_MONEY_PER_CORRECT:.2f}+\n🔓 Вопрос открыт до правильного ответа.{pl}{tl}")
        if msg:
            try: await bot.set_message_reaction(cid,msg.message_id,["🧠"])
            except Exception: pass
        if ut:
            async def tt():
                await asyncio.sleep(TIMER_SECONDS); cu=ACTIVE_QUESTIONS.get(cid)
                if cu and cu.get("timer"):
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
        log.info("Next Q %s (%.0f)",t.strftime("%H:%M:%S"),w); await asyncio.sleep(max(1,w))
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
    h="🎰 <b>Розыгрыш копилки!</b>" if reason=="auto" else "🎉 <b>Копилка разыграна вручную!</b>"
    await safe_send(bot.send_message,cid,f"{h}\n\n💰 ${amt:.4f}\n🏆 <b>{wn}</b> (ID <code>{wuid}</code>)")
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
def split_prize(prize,top):
    tp=sum(p for _,p in top)
    if tp==0: return []
    r=[]; a=0.0
    for i,(uid,pts) in enumerate(top):
        if i==len(top)-1: sh=round(prize-a,4)
        else: sh=round(prize*(pts/tp),4); a+=sh
        r.append((uid,sh))
    return r
async def run_tournament(tid,cid,settings):
    TOURNAMENT_ACTIVE[cid]=tid
    tq=int(settings["questions"]); qs=int(settings["question_seconds"]); pr=float(settings["prize"])
    diff=settings.get("difficulty","medium"); dl=DIFFICULTY_LABELS.get(diff,"🟡 Средне"); wc=int(settings.get("winners_count",1))
    scores={}
    try:
        await safe_send(bot.send_message,cid,f"🏁 <b>ТУРНИР НАЧАЛСЯ!</b>\n\n🎯 {dl}\n❓ {tq} вопросов\n⏱ {qs} сек\n💰 Приз: <b>${pr:.2f}</b>\n🏆 Победителей: <b>{wc}</b>")
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
            await safe_send(bot.send_message,cid,"🏁 Окончен. Никто не ответил.")
            await tournament_finish(tid,None); return
        ss=sorted(scores.items(),key=lambda x:-x[1]); tn=ss[:wc]; sh=split_prize(pr,tn)
        wids=[]
        for uid,s in sh:
            if s>0: await add_balance(cid,uid,s)
            wids.append(uid)
        await tournament_finish(tid,wids[0] if wids else None)
        lines=["🏆 <b>ТУРНИР ЗАВЕРШЁН!</b>",""]
        med=["🥇","🥈","🥉","4.","5.","6.","7.","8.","9.","10."]
        for i,(uid,s) in enumerate(sh):
            try:
                pp=await get_player(cid,uid); pnm=pp.get("first_name") or pp.get("username") or str(uid)
            except Exception: pnm=str(uid)
            lines.append(f"{med[i] if i<len(med) else f'{i+1}.'} {pnm} — {scores[uid]} прав. · <b>${s:.4f}</b>")
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
        [InlineKeyboardButton(text="🚀 Запустить",callback_data="turik:start")],
        [InlineKeyboardButton(text="🔄 Обновить",callback_data="turik:refresh")]])
def turik_text(s):
    diff=s.get("difficulty","medium"); wc=s.get("winners_count",1)
    return f"🏁 <b>Турнир</b>\n\n⏱ {s['question_seconds']} сек\n❓ {s['questions']}\n💰 <b>${float(s['prize']):.2f}</b>\n🏆 {wc}\n🎯 {DIFFICULTY_LABELS.get(diff,diff)}"
@dp.message(Command("AiTurik"))
async def cmd_turik(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    if message.chat.type not in ("group","supergroup"): return
    TOURNAMENT_EDIT.pop(message.from_user.id,None); s=await get_t_settings(message.chat.id)
    await safe_send(message.reply,turik_text(s),reply_markup=turik_kb(s))
@dp.message(F.chat.type.in_({"group","supergroup"}),F.text,~F.text.startswith("/"))
async def turik_value_input(message:Message):
    if not message.from_user or message.from_user.id not in TOURNAMENT_EDIT: raise SkipHandler()
    ed=TOURNAMENT_EDIT[message.from_user.id]
    if ed["chat_id"]!=message.chat.id: raise SkipHandler()
    f=ed["field"]; raw=message.text.strip().replace(",",".")
    try:
        if f=="prize":
            v=float(raw)
            if not (0.1<=v<=100): raise ValueError
            v=round(v,4)
        else:
            v=int(raw); rg={"question_seconds":(5,300),"questions":(3,50)}[f]
            if not (rg[0]<=v<=rg[1]): raise ValueError
    except ValueError:
        await safe_send(message.reply,"❌ Неверное значение. Попробуй снова или /AiTurik для отмены."); return
    ok=await update_t_setting(ed["chat_id"],f,v); TOURNAMENT_EDIT.pop(message.from_user.id,None)
    if not ok: await safe_send(message.reply,"❌ Не удалось сохранить."); return
    s=await get_t_settings(ed["chat_id"])
    try: await message.delete()
    except Exception: pass
    await safe_send(message.answer,turik_text(s),reply_markup=turik_kb(s))
@dp.callback_query(F.data.startswith("turik:"))
async def on_turik_cb(cb:CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id): await cb.answer("⛔",show_alert=True); return
    if not isinstance(cb.message,Message): await cb.answer(); return
    cid=cb.message.chat.id; p=cb.data.split(":"); a=p[1] if len(p)>1 else ""
    if a in ("refresh","menu"):
        s=await get_t_settings(cid); await safe_edit(cb.message.edit_text,turik_text(s),reply_markup=turik_kb(s)); await cb.answer(); return
    if a=="winners_count":
        s=await get_t_settings(cid); cu=s.get("winners_count",1)
        def m(n): return f"{'✅ ' if n==cu else ''}Топ-{n}"
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=m(1),callback_data="turik:wc_set:1")],[InlineKeyboardButton(text=m(2),callback_data="turik:wc_set:2")],[InlineKeyboardButton(text=m(3),callback_data="turik:wc_set:3")],[InlineKeyboardButton(text=m(5),callback_data="turik:wc_set:5")],[InlineKeyboardButton(text="⬅️",callback_data="turik:menu")]])
        await safe_edit(cb.message.edit_text,f"🏆 Победителей? Текущее: <b>{cu}</b>",reply_markup=kb); await cb.answer(); return
    if a=="wc_set":
        try: n=int(p[2])
        except (ValueError,IndexError): await cb.answer("Ошибка",show_alert=True); return
        if n not in (1,2,3,5): await cb.answer("Ошибка",show_alert=True); return
        ok=await update_t_setting(cid,"winners_count",n)
        if not ok: await cb.answer("❌",show_alert=True); return
        await cb.answer(f"{n}"); s=await get_t_settings(cid); await safe_edit(cb.message.edit_text,turik_text(s),reply_markup=turik_kb(s)); return
    if a=="difficulty":
        s=await get_t_settings(cid); cu=s.get("difficulty","medium")
        def m(d): return f"{'✅ ' if d==cu else ''}{DIFFICULTY_LABELS[d]}"
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=m("easy"),callback_data="turik:diff_set:easy")],[InlineKeyboardButton(text=m("medium"),callback_data="turik:diff_set:medium")],[InlineKeyboardButton(text=m("hard"),callback_data="turik:diff_set:hard")],[InlineKeyboardButton(text=m("extreme"),callback_data="turik:diff_set:extreme")],[InlineKeyboardButton(text="⬅️",callback_data="turik:menu")]])
        await safe_edit(cb.message.edit_text,"🎯 Сложность",reply_markup=kb); await cb.answer(); return
    if a=="diff_set":
        d=p[2] if len(p)>2 else ""
        if d not in ("easy","medium","hard","extreme"): await cb.answer("Ошибка",show_alert=True); return
        ok=await update_t_setting(cid,"difficulty",d)
        if not ok: await cb.answer("❌",show_alert=True); return
        await cb.answer(f"{DIFFICULTY_LABELS[d]}"); s=await get_t_settings(cid); await safe_edit(cb.message.edit_text,turik_text(s),reply_markup=turik_kb(s)); return
    if a=="set":
        f=p[2]
        if f not in ("question_seconds","questions","prize"): await cb.answer("Ошибка",show_alert=True); return
        TOURNAMENT_EDIT[cb.from_user.id]={"chat_id":cid,"field":f}
        pr={"question_seconds":"⏱ Секунд на вопрос? (5-300)","questions":"❓ Всего вопросов? (3-50)","prize":"💰 Приз в USDT? (0.1-100)"}[f]
        await cb.answer("Жду число..."); await safe_send(cb.message.answer,pr+"\n\n<i>Отмена: /AiTurik</i>"); return
    if a=="start":
        if cid in TOURNAMENT_ACTIVE: await cb.answer("⏳ Турнир уже идёт",show_alert=True); return
        s=await get_t_settings(cid); tid=await tournament_create(cid,float(s["prize"]))
        if not tid: await cb.answer("Ошибка создания",show_alert=True); return
        await cb.answer("🚀 Запускаю!"); asyncio.create_task(run_tournament(tid,cid,s)); return
@dp.message(Command("AiBoost"))
async def cmd_boost(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    if message.chat.type not in ("group","supergroup"): return
    await safe_send(message.reply,_boost_text(message.chat.id),reply_markup=_boost_kb(message.chat.id))
@dp.callback_query(F.data.startswith("boost:"))
async def on_boost_cb(cb:CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id): await cb.answer("⛔",show_alert=True); return
    if not isinstance(cb.message,Message): await cb.answer(); return
    cid=cb.message.chat.id; p=cb.data.split(":"); a=p[1] if len(p)>1 else ""
    if a=="menu":
        await safe_edit(cb.message.edit_text,_boost_text(cid),reply_markup=_boost_kb(cid)); await cb.answer(); return
    if a=="pick":
        bt=p[2] if len(p)>2 else ""
        if bt not in BOOSTER_TYPES: await cb.answer("Ошибка",show_alert=True); return
        await safe_edit(cb.message.edit_text,_bpick_text(cid,bt),reply_markup=_bpick_kb(bt)); await cb.answer(); return
    if a=="start":
        try: bt=p[2]; m=float(p[3]); s=int(p[4])
        except (ValueError,IndexError): await cb.answer("Ошибка",show_alert=True); return
        if bt not in BOOSTER_TYPES: await cb.answer("Ошибка",show_alert=True); return
        _booster_set(cid,bt,m,s); cfg=BOOSTER_TYPES[bt]
        await cb.answer("🚀 Запущен!")
        await safe_edit(cb.message.edit_text,_bpick_text(cid,bt),reply_markup=_bpick_kb(bt))
        dur="навсегда" if s==0 else (f"{s//3600}ч" if s>=3600 else f"{s//60}м")
        ms="0%" if bt=="commission" else f"x{int(m)}"
        await safe_send(bot.send_message,cid,f"🚀 <b>Бустер:</b> {cfg['emoji']} {cfg['name']} · {ms} · {dur}")
        return
    if a=="stop":
        bt=p[2] if len(p)>2 else ""
        if bt in BOOSTER_TYPES:
            _booster_stop(cid,bt); cfg=BOOSTER_TYPES[bt]
            await cb.answer("⏹ Остановлен")
            await safe_edit(cb.message.edit_text,_bpick_text(cid,bt),reply_markup=_bpick_kb(bt))
            await safe_send(bot.send_message,cid,f"⏹ <b>Выкл:</b> {cfg['emoji']} {cfg['name']}")
        else: await cb.answer()
        return
    if a=="stopall":
        ACTIVE_BOOSTERS.pop(cid,None); await cb.answer("Все бустеры выключены",show_alert=True)
        await safe_edit(cb.message.edit_text,_boost_text(cid),reply_markup=_boost_kb(cid)); return
    await cb.answer()
@dp.message(Command("AiSubscribe"))
async def cmd_aisub(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    uid=message.from_user.id; exp=await get_subscription(uid); now=datetime.now(timezone.utc)
    if exp and exp>now:
        dt=exp-now; d=dt.days; h=int(dt.total_seconds()//3600%24)
        await safe_send(message.reply,f"💎 <b>Подписка активна</b>\n\n📅 Осталось: <b>{d} дн. {h} ч.</b>\n⏰ До: <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>\n\n🔥 ×{SUBSCRIBER_MULTIPLIER:.0f}"); return
    ciid=f"sub_{uid}_{uuid.uuid4().hex[:12]}"
    ok,res=await xrocket_create_invoice(ciid,SUBSCRIPTION_PRICE,f"Sub {SUBSCRIPTION_DAYS}d {uid}")
    if not ok: await safe_send(message.reply,f"❌ <code>{res}</code>"); return
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💳 Оплатить ${SUBSCRIPTION_PRICE:.2f}",url=res)]])
    sent=await safe_send(message.reply,f"💎 <b>Подписка ×{SUBSCRIBER_MULTIPLIER:.0f}</b>\n\n💵 ${SUBSCRIPTION_PRICE:.2f} за {SUBSCRIPTION_DAYS} дней\n\nЖми кнопку — активируется после оплаты.",reply_markup=kb)
    mid=sent.message_id if sent else None
    await create_invoice(ciid,uid,message.chat.id,SUBSCRIPTION_PRICE,mid,kind="subscription")
@dp.message(CommandStart())
async def cmd_start(message:Message):
    if message.from_user: TOURNAMENT_EDIT.pop(message.from_user.id,None)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Подписка $0.50/нед",url=XROCKET_SUBSCRIBE_URL)],[InlineKeyboardButton(text="🎰 Лотерея /AiLoto",callback_data="loto:open:0")]])
    await safe_send(message.reply,f"👋 <b>Викторина!</b>\n\n💰 За правильный ответ: <b>${BASE_MONEY_PER_CORRECT:.2f} + ${MONEY_PER_LEVEL:.3f}/уровень</b>\n🏆 Очки + уровень\n🎴 /AiCard\n🎰 /AiLoto — лотерея\n🎲 /AiDuel 0.20\n💳 /AiDeposit 1.0\n💸 Вывод ${MIN_WITHDRAW:.2f}\n\n📖 /AiHelp",reply_markup=kb)
@dp.callback_query(F.data=="loto:open:0")
async def on_loto_open(cb:CallbackQuery):
    if not cb.from_user or not isinstance(cb.message,Message): await cb.answer(); return
    cid,uid=cb.message.chat.id,cb.from_user.id
    await cb.message.answer(await loto_text(cid,uid),reply_markup=loto_kb(uid)); await cb.answer()
@dp.message(Command("AiHelp"))
async def cmd_aihelp(message:Message):
    if message.chat.type not in ("group","supergroup"): return
    t=("📖 <b>СПРАВКА</b>\n\n<b>🎮 Игра</b>\n/AiBalance · /AiProfile · /AiCard · /AiTop · /AiLevels · /AiCoins\n/AiDuel 0.20\n\n<b>💰 Деньги</b>\n"
       f"💰 За правильный ответ: <b>${BASE_MONEY_PER_CORRECT:.2f} + ${MONEY_PER_LEVEL:.3f} × (уровень-1)</b>\n"
       f"/AiDeposit — комиссия {DEPOSIT_COMMISSION*100:.0f}%\n/AiWithdraw — комиссия {WITHDRAW_COMMISSION*100:.0f}%, мин ${MIN_WITHDRAW:.2f}\n"
       f"/AiSubscribe — ×{SUBSCRIBER_MULTIPLIER:.0f} (${SUBSCRIPTION_PRICE:.2f}/{SUBSCRIPTION_DAYS}дн)\n"
       f"/AiLoto — лотерея (возврат + {int(LOTTERY_WINNER_SHARE*100)}% банка)\n/AiSponsor — в ЛС\n\n<b>📜</b> /AiRules\n")
    if message.from_user and is_admin(message.from_user.id):
        t+=("\n<b>🛠 Админ</b>\n/AiAdmin · /AiBoost · /AiTurik · /AiGive · /AiSubGive\n/AiSubInfo · /AiSubList · /AiSubDel\n/AiPot · /AiPotAdd · /AiPotTake · /AiPotGive\n/AiBan · /AiUnban\n")
    await safe_send(message.reply,t)
@dp.message(Command("AiRules"))
async def cmd_airules(message:Message):
    if message.chat.type not in ("group","supergroup"): return
    await safe_send(message.reply,f"📜 <b>ПРАВИЛА</b>\n\n1. Оскорбления — бан.\n2. Обход бана — перманентный.\n3. Скрипты — бан.\n4. Спам — бан.\n5. Фиктивные дуэли — бан.\n6. Обман вывода — бан.\n\n💰 За ответ: ${BASE_MONEY_PER_CORRECT:.2f} + ${MONEY_PER_LEVEL:.3f}/уровень\n💸 Вывод: ${MIN_WITHDRAW:.2f} · комиссия {WITHDRAW_COMMISSION*100:.0f}%\n💳 Депозит: комиссия {DEPOSIT_COMMISSION*100:.0f}%\n🎰 Лотерея: возврат + {int(LOTTERY_WINNER_SHARE*100)}% банка · {LOTTERY_HOUR}:00 МСК\n🎲 Рейк: {RAKE_PCT*100:.0f}%")
@dp.message(Command("AiCoins"))
async def cmd_aicoins(message:Message):
    if message.chat.type not in ("group","supergroup"): return
    st=await get_coins_stats(message.chat.id)
    if not st: await safe_send(message.reply,"Ошибка"); return
    await safe_send(message.reply,f"💼 <b>Экономика</b>\n\n👥 {st['players']}\n💰 Балансы: <b>${st['balance']:.4f}</b>\n🎯 Очков: <b>{st['points']}</b>\n💸 Выплачено: <b>${st['paid']:.4f}</b>\n🏦 Касса: <b>${st['house']:.4f}</b>")
@dp.message(Command("AiSponsor"))
async def cmd_sponsor(message:Message):
    if message.chat.type!="private": await safe_send(message.reply,"В ЛС"); return
    uid=message.from_user.id
    if uid in SPONSOR_SESSION:
        s=SPONSOR_SESSION[uid]
        if not s.get("chat_id"): await safe_send(message.reply,"📌 Укажи <b>ID чата</b>, куда постить вопросы.\nНапример: <code>-1002712583382</code>")
        else: await safe_send(message.reply,f"📝 Режим спонсора\nОсталось: <b>{s['target']-s['collected']}</b>\n\nФормат: <code>вопрос | ответ</code>")
        return
    parts=(message.text or "").split()
    if len(parts)!=2 or parts[1]!="5":
        await safe_send(message.reply,f"🎁 <b>Спонсор</b>\n\nЦена: <b>${SPONSOR_PRICE:.2f}</b> = <b>{SPONSOR_QUESTIONS} вопросов</b>\n\nОплата: <code>/AiSponsor 5</code>"); return
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
        except ValueError: await safe_send(message.reply,"❌ Неверный ID. Пример: <code>-1002712583382</code>"); return
        s["chat_id"]=cid; await safe_send(message.reply,f"✅ Чат: <code>{cid}</code>\n\nОтправляй вопросы: <code>вопрос | ответ</code>\nОсталось: <b>{s['target']}</b>"); return
    t=message.text.strip()
    if "|" not in t: await safe_send(message.reply,"❌ Формат: <code>вопрос | ответ</code>"); return
    q,a=t.split("|",1); q,a=q.strip(),a.strip().lower()
    if not q or not a: await safe_send(message.reply,"❌ Пустой вопрос или ответ"); return
    pos=s["collected"]+1; await sponsor_add(uid,s["chat_id"],q,a,pos); s["collected"]+=1; l=s["target"]-s["collected"]
    if l<=0: await safe_send(message.reply,"🎉 Все вопросы приняты!"); SPONSOR_SESSION.pop(uid,None)
    else: await safe_send(message.reply,f"✅ Принято. Осталось: <b>{l}</b>")
def admin_kb(cid):
    en=cid in QUIZ_ENABLED
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏸ Выкл" if en else "▶️ Вкл",callback_data="adm:toggle")],
        [InlineKeyboardButton(text="❓ Вопрос",callback_data="adm:ask"),InlineKeyboardButton(text="📊 Стата",callback_data="adm:stats")],
        [InlineKeyboardButton(text="💼 Касса",callback_data="adm:house"),InlineKeyboardButton(text="💸 Выплаты",callback_data="adm:payouts")],
        [InlineKeyboardButton(text="📋 Топ",callback_data="adm:top"),InlineKeyboardButton(text="🚫 Баны",callback_data="adm:bans")],
        [InlineKeyboardButton(text="🎯 Сложность",callback_data="adm:difficulty"),InlineKeyboardButton(text="🎰 Копилка",callback_data="adm:pot")],
        [InlineKeyboardButton(text="🚀 Бустеры",callback_data="boost:menu"),InlineKeyboardButton(text="🎰 Лотерея",callback_data="loto:open:0")],
        [InlineKeyboardButton(text="🧪 xRocket",callback_data="adm:xrdbg")]])
def admin_text(cid):
    st="🟢 вкл" if cid in QUIZ_ENABLED else "🔴 выкл"; cu=ACTIVE_QUESTIONS.get(cid)
    ct=f"\n🔓 Открыт: {cu['question']}" if cu else ""
    bl=[]
    for bt,cfg in BOOSTER_TYPES.items():
        b=_booster_get(cid,bt)
        if b:
            if bt=="commission": bl.append(f"{cfg['emoji']} 0%")
            else: bl.append(f"{cfg['emoji']} x{int(b['multiplier'])}")
    bstr=(" · ".join(bl)) if bl else "нет"
    return f"🛠 <b>Админ</b>\nВикторина: {st}\n💰 За ответ: ${BASE_MONEY_PER_CORRECT:.2f}+${MONEY_PER_LEVEL:.3f}/ур\nРейк: {RAKE_PCT*100:.0f}% · Деп {DEPOSIT_COMMISSION*100:.0f}% · Выв {WITHDRAW_COMMISSION*100:.0f}%\n🚀 Бустеры: {bstr}{ct}"
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
    ls=[f"💼 <b>Касса</b>\n",f"💰 Чат: <b>${t:.4f}</b>",f"📈 Всего: <b>${ta:.4f}</b>\n"]
    if by:
        ls.append("<b>Источники:</b>")
        for s,a in sorted(by.items(),key=lambda x:-x[1]): ls.append(f"• {s}: ${a:.4f}")
    await safe_send(message.reply,"\n".join(ls))
@dp.message(Command("AiPot"))
async def cmd_aipot(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    if message.chat.type not in ("group","supergroup"): return
    p=await get_pot(message.chat.id)
    await safe_send(message.reply,f"🎰 <b>Копилка</b>\n\nСейчас: <b>${p:.4f}</b>\n\n/AiPotAdd &lt;сумма&gt;\n/AiPotTake &lt;сумма&gt;\n/AiPotGive\n\nАвто: {POT_HOUR}:00 МСК")
@dp.message(Command("AiPotAdd"))
async def cmd_aipotadd(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)!=2: await safe_send(message.reply,"🎰 Формат: <code>/AiPotAdd 0.50</code>\n\nСумма — число больше 0"); return
    try: a=round(float(parts[1]),4)
    except ValueError: await safe_send(message.reply,"🎰 Формат: <code>/AiPotAdd 0.50</code>\n\nСумма — число больше 0"); return
    if a<=0: await safe_send(message.reply,"❌ Сумма должна быть > 0"); return
    np=await add_to_pot(message.chat.id,a)
    if np is None: await safe_send(message.reply,"❌ Ошибка добавления"); return
    await safe_send(message.reply,f"✅ Добавлено <b>${a:.4f}</b>\n🎰 В фонде: <b>${np:.4f}</b>")
@dp.message(Command("AiPotTake"))
async def cmd_aipottake(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)!=2: await safe_send(message.reply,"🎰 Формат: <code>/AiPotTake 0.50</code>\n\nСумма — число больше 0"); return
    try: a=round(float(parts[1]),4)
    except ValueError: await safe_send(message.reply,"🎰 Формат: <code>/AiPotTake 0.50</code>\n\nСумма — число больше 0"); return
    if a<=0: await safe_send(message.reply,"❌ Сумма должна быть > 0"); return
    ok,np=await pot_take(message.chat.id,a)
    if not ok: await safe_send(message.reply,f"❌ В копилке только <b>${np:.4f}</b>"); return
    await add_balance(message.chat.id,message.from_user.id,a)
    await safe_send(message.reply,f"✅ Снято <b>${a:.4f}</b>\n💰 Зачислено тебе\n🎰 В фонде: <b>${np:.4f}</b>")
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
    if len(parts)<3: await safe_send(message.reply,"💰 Формат: <code>/AiGive &lt;user_id&gt; &lt;сумма&gt; [причина]</code>\n\nПример: <code>/AiGive 123456789 0.50 приз</code>"); return
    try: t=int(parts[1]); a=round(float(parts[2]),4)
    except ValueError: await safe_send(message.reply,"💰 Формат: <code>/AiGive &lt;user_id&gt; &lt;сумма&gt; [причина]</code>\n\nПример: <code>/AiGive 123456789 0.50 приз</code>"); return
    if a<=0 or a>100: await safe_send(message.reply,"❌ Сумма должна быть от <b>$0.0001</b> до <b>$100</b>"); return
    r=" ".join(parts[3:]) if len(parts)>3 else "admin_give"
    await get_player(message.chat.id,t); nb=await add_balance(message.chat.id,t,a)
    if nb is None: await safe_send(message.reply,"❌ Ошибка начисления"); return
    try: await bot.send_message(t,f"🎁 <b>Начисление от админа</b>\n\n💰 +${a:.4f} USDT\n💼 Баланс: <b>${nb:.4f}</b>\n📝 {r}")
    except Exception: pass
    await safe_send(message.reply,f"✅ <code>{t}</code> +${a:.4f}\n💰 Баланс: ${nb:.4f}\n📝 {r}")
@dp.message(Command("AiSubGive"))
async def cmd_subgive(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)<2: await safe_send(message.reply,"💎 Формат: <code>/AiSubGive &lt;user_id&gt; [дней]</code>\n\nПример: <code>/AiSubGive 123456789 30</code>"); return
    try: uid=int(parts[1]); days=int(parts[2]) if len(parts)>2 else SUBSCRIPTION_DAYS
    except ValueError: await safe_send(message.reply,"💎 Формат: <code>/AiSubGive &lt;user_id&gt; [дней]</code>\n\nuser_id и дни — числа"); return
    exp=await activate_subscription(uid,days)
    if not exp: await safe_send(message.reply,"❌ Ошибка активации"); return
    await safe_send(message.reply,f"✅ <code>{uid}</code> — подписка до <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>")
    try: await bot.send_message(uid,f"🎉 <b>Подписка активирована!</b>\n\n📅 До: <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>")
    except Exception: pass
@dp.message(Command("AiSubInfo"))
async def cmd_subinfo(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split(); uid=message.from_user.id
    if len(parts)>=2:
        try: uid=int(parts[1])
        except ValueError: await safe_send(message.reply,"💎 Формат: <code>/AiSubInfo &lt;user_id&gt;</code>\n\nuser_id — число"); return
    exp=await get_subscription(uid); n=datetime.now(timezone.utc)
    if exp and exp>n:
        d=(exp-n).days; h=int((exp-n).total_seconds()//3600%24)
        await safe_send(message.reply,f"💎 <code>{uid}</code> до <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>\n⏳ Осталось: {d}д {h}ч")
    else: await safe_send(message.reply,f"❌ <code>{uid}</code> без подписки")
@dp.message(Command("AiSubList"))
async def cmd_sublist(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    rows=await list_subscriptions(50)
    if not rows: await safe_send(message.reply,"Активных подписок нет."); return
    n=datetime.now(timezone.utc); ls=["💎 <b>Активные подписки</b>"]
    for r in rows:
        try: e=datetime.fromisoformat(str(r["expires_at"]).replace("Z","+00:00"))
        except Exception: continue
        if e<=n: continue
        ls.append(f"• <code>{r['user_id']}</code> — {(e-n).days}д (до {e.strftime('%d.%m')})")
    if len(ls)==1: await safe_send(message.reply,"Активных подписок нет."); return
    await safe_send(message.reply,"\n".join(ls))
@dp.message(Command("AiSubDel"))
async def cmd_subdel(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)!=2: await safe_send(message.reply,"💎 Формат: <code>/AiSubDel &lt;user_id&gt;</code>\n\nuser_id — число"); return
    try: uid=int(parts[1])
    except ValueError: await safe_send(message.reply,"💎 Формат: <code>/AiSubDel &lt;user_id&gt;</code>\n\nuser_id — число"); return
    ok=await deactivate_subscription(uid)
    await safe_send(message.reply,"✅ Подписка снята" if ok else "❌ Ошибка")
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
    if len(parts)<2: await safe_send(message.reply,"📛 Формат: <code>/AiBan &lt;user_id&gt; [время] [причина]</code>\n\n• user_id — числовой ID\n• время — <code>1h</code>, <code>2d</code>, <code>1w</code> или <code>perm</code>\n• причина — текст"); return
    try: t=int(parts[1])
    except ValueError: await safe_send(message.reply,"📛 Формат: <code>/AiBan &lt;user_id&gt; [время] [причина]</code>\n\nuser_id — числовой ID (например <code>123456789</code>)"); return
    dur=None; r="без причины"; bu=None
    if len(parts)>=3:
        p=parse_duration(parts[2])
        if p=="error": r=" ".join(parts[2:])
        else:
            dur=p; r=parts[3] if len(parts)>3 else "без причины"
            if dur is not None: bu=(datetime.now(timezone.utc)+dur).isoformat()
    await ban_user(message.chat.id,t,r,message.from_user.id,bu)
    if bu: await safe_send(message.reply,f"🔨 <code>{t}</code> забанен до <b>{bu[:19].replace('T',' ')} UTC</b>\nПричина: {r}")
    else: await safe_send(message.reply,f"🔨 <code>{t}</code> забанен <b>навсегда</b>\nПричина: {r}")
@dp.message(Command("AiUnban"))
async def cmd_aiunban(message:Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)!=2: await safe_send(message.reply,"📛 Формат: <code>/AiUnban &lt;user_id&gt;</code>\n\nuser_id — числовой ID игрока."); return
    try: t=int(parts[1])
    except ValueError: await safe_send(message.reply,"📛 Формат: <code>/AiUnban &lt;user_id&gt;</code>\n\nuser_id — числовой ID игрока."); return
    await unban_user(message.chat.id,t); await safe_send(message.reply,f"✅ <code>{t}</code> разбанен")
@dp.callback_query(F.data.startswith("adm:"))
async def on_admin_cb(cb:CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id): await cb.answer("⛔",show_alert=True); return
    if not isinstance(cb.message,Message): await cb.answer(); return
    cid=cb.message.chat.id; a=cb.data.split(":")[1]
    if a=="toggle":
        if cid in QUIZ_ENABLED:
            QUIZ_ENABLED.discard(cid); ACTIVE_QUESTIONS.pop(cid,None); await clear_active(cid); await cb.answer("Викторина выключена")
        else: QUIZ_ENABLED.add(cid); await cb.answer("Викторина включена")
        await safe_edit(cb.message.edit_text,admin_text(cid),reply_markup=admin_kb(cid)); return
    if a=="ask": QUIZ_ENABLED.add(cid); await cb.answer("Задаю вопрос..."); asyncio.create_task(ask_question(cid)); return
    if a=="difficulty":
        s=await get_chat_settings(cid); cu=s.get("difficulty","medium")
        def m(d): return f"{'✅ ' if d==cu else ''}{DIFFICULTY_LABELS[d]}"
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=m("easy"),callback_data="adm:diff_set:easy")],[InlineKeyboardButton(text=m("medium"),callback_data="adm:diff_set:medium")],[InlineKeyboardButton(text=m("hard"),callback_data="adm:diff_set:hard")],[InlineKeyboardButton(text=m("extreme"),callback_data="adm:diff_set:extreme")],[InlineKeyboardButton(text="⬅️",callback_data="adm:menu")]])
        await safe_edit(cb.message.edit_text,f"🎯 <b>Сложность</b>\n\nТекущая: <b>{DIFFICULTY_LABELS.get(cu,cu)}</b>",reply_markup=kb); await cb.answer(); return
    if a=="diff_set":
        d=cb.data.split(":")[2]
        if d not in ("easy","medium","hard","extreme"): await cb.answer("Ошибка",show_alert=True); return
        ok=await update_chat_setting(cid,"difficulty",d)
        if not ok: await cb.answer("❌ Не сохранилось",show_alert=True); return
        await cb.answer(f"Установлено: {DIFFICULTY_LABELS[d]}"); await safe_edit(cb.message.edit_text,admin_text(cid),reply_markup=admin_kb(cid)); return
    if a=="menu": await safe_edit(cb.message.edit_text,admin_text(cid),reply_markup=admin_kb(cid)); await cb.answer(); return
    if a=="stats":
        await cb.answer("Собираю..."); pl,po,ba,su=await get_stats()
        tb=sum(float(p["balance"]) for p in pl); tc=sum(int(p["correct_answers"]) for p in pl)
        fin=[p for p in po if p["status"]=="finished"]; ps=sum(float(p["amount"]) for p in fin)
        h=await get_house_total(cid); pt=await get_pot(cid)
        await safe_send(cb.message.answer,f"📊 <b>Стата</b>\n👥 Игроков: {len(pl)}\n🏆 Очков: {tc}\n💎 Подписчиков: {len(su)}\n🚫 Забанено: {len(ba)}\n💰 Балансы: ${tb:.4f}\n💼 Касса: ${h:.4f}\n🎰 Копилка: ${pt:.4f}\n💸 Выплат: {len(fin)} (${ps:.4f})"); return
    if a=="pot":
        await cb.answer(); p=await get_pot(cid)
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎉 Раздать сейчас",callback_data="adm:potgive")],[InlineKeyboardButton(text="🔄 Обновить",callback_data="adm:pot")],[InlineKeyboardButton(text="⬅️",callback_data="adm:menu")]])
        await safe_send(cb.message.answer,f"🎰 <b>Копилка</b>\n${p:.4f}",reply_markup=kb); return
    if a=="potgive":
        await cb.answer("Раздаю..."); ok,info=await distribute_pot(cid,reason="manual")
        if not ok: await safe_send(cb.message.answer,f"❌ {info}")
        return
    if a=="house":
        await cb.answer(); t=await get_house_total(cid); ta=await get_house_total(None); by=await get_house_by_source()
        ls=[f"💼 <b>Касса</b>\n💰 ${t:.4f}",f"📈 ${ta:.4f}\n"]
        if by:
            ls.append("<b>Источники:</b>")
            for s,amt in sorted(by.items(),key=lambda x:-x[1]): ls.append(f"• {s}: ${amt:.4f}")
        await safe_send(cb.message.answer,"\n".join(ls)); return
    if a=="payouts":
        await cb.answer(); rows=await get_payouts(20)
        if not rows: await safe_send(cb.message.answer,"Выплат не было."); return
        ls=["💸 <b>Выплаты</b>"]
        for p in rows:
            dt=(p.get("created_at") or "")[:19].replace("T"," "); em="✅" if p["status"]=="finished" else "❌"
            ls.append(f"{em} ${float(p['amount']):.4f} · <code>{p['user_id']}</code> · {dt}")
        await safe_send(cb.message.answer,"\n".join(ls)); return
    if a=="top":
        await cb.answer(); rows=await get_top(cid,10)
        if not rows: await safe_send(cb.message.answer,"Никто не играл."); return
        ls=["🏆 <b>Топ</b>"]
        for i,r in enumerate(rows,1):
            ca=int(r.get("correct_answers",0)); _,em,_,_,_,_=level_info(ca)
            nm=r.get("first_name") or r.get("username") or str(r["user_id"]); med=["🥇","🥈","🥉"][i-1] if i<=3 else f"{i}."
            ls.append(f"{med} {em} {nm} — {ca} · ${float(r['balance']):.4f}")
        await safe_send(cb.message.answer,"\n".join(ls)); return
    if a=="bans":
        await cb.answer()
        def q():
            try: return supabase.table("quiz_bans").select("*").eq("chat_id",cid).execute().data or []
            except Exception: return []
        rows=await asyncio.to_thread(q)
        if not rows: await safe_send(cb.message.answer,"Забаненных нет."); return
        ls=["🚫 <b>Баны</b>"]
        for b in rows:
            u=b.get("banned_until")
            if u: ls.append(f"<code>{b['user_id']}</code> — до {u[:19].replace('T',' ')}")
            else: ls.append(f"<code>{b['user_id']}</code> — навсегда")
        await safe_send(cb.message.answer,"\n".join(ls)); return
    if a=="xrdbg":
        await cb.answer("Проверяю..."); msg=await safe_send(cb.message.answer,"⏳..."); info=await xrocket_debug()
        if msg:
            try: await msg.edit_text(info)
            except Exception: pass
        return
async def xrocket_debug():
    ls=["🧪 <b>xRocket</b>",f"Key: <code>{XROCKET_API_KEY[:12]}...</code>",""]
    if not XROCKET_API_KEY: ls.append("❌ пусто"); return "\n".join(ls)
    try:
        s=await get_http()
        for u in (f"{XROCKET_BASE}/api/v1/me",f"{XROCKET_BASE}/api/v1/balance"):
            try:
                async with s.get(u,headers={"Authorization":f"Bearer {XROCKET_API_KEY}"}) as r:
                    t=(await r.text())[:200]; ls.append(f"[{r.status}] <code>{u}</code>\n<code>{t}</code>\n")
            except Exception as e: ls.append(f"[ERR] {u}: <code>{e}</code>")
    except Exception as e: ls.append(f"[ERR] {e}")
    return "\n".join(ls)
@dp.message(Command("AiBalance"))
async def cmd_aibalance(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    p=await get_player(message.chat.id,message.from_user.id,message.from_user.username,message.from_user.first_name)
    td=await withdrawn_today(message.chat.id,message.from_user.id); ca=int(p["correct_answers"])
    _,_,_,_,ts,_=level_info(ca); bar=make_progress_bar(ca); sub=is_subscriber_cached(message.chat.id,message.from_user.id)
    sl=f"\n💎 ×{SUBSCRIBER_MULTIPLIER:.0f}" if sub else ""
    per=money_for_answer(ca)
    await safe_send(message.reply,f"💰 <b>${float(p['balance']):.4f} USDT</b>\n🎖 {ts}\n🏆 {ca}\n<code>{bar}</code>{sl}\n💵 За ответ: <b>${per:.4f}</b>\n💸 Сегодня: ${td:.4f} / ${DAILY_WITHDRAW_LIMIT:.2f}\n💳 /AiDeposit · 🎴 /AiCard · 🎰 /AiLoto")
@dp.message(Command("AiCard"))
async def cmd_aicard(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    cid,uid=message.chat.id,message.from_user.id
    p=await get_player(cid,uid,message.from_user.username,message.from_user.first_name)
    ca=int(p["correct_answers"]); lvl,_,nm,_,_,_=level_info(ca)
    pl=await get_player_place(cid,uid); td=await withdrawn_today(cid,uid); sub=is_subscriber_cached(cid,uid)
    name=message.from_user.first_name or "Player"
    try: await bot.send_chat_action(cid,"upload_photo")
    except Exception: pass
    av=await fetch_avatar(uid)
    try:
        png=render_profile_card(name,message.from_user.username,lvl,ca,pl,float(p["balance"]),td,sub,av)
        buf=BufferedInputFile(png,filename=f"c{uid}.png")
        await safe_send(bot.send_photo,cid,buf,caption=f"<b>{name}</b> · {nm} (ур. {lvl})")
    except Exception as e:
        log.warning("card: %s",e)
        await safe_send(message.reply,f"🎴 {nm} (ур. {lvl})\n🏆 {ca} · 💰 ${float(p['balance']):.4f}")
@dp.message(Command("AiProfile"))
async def cmd_aiprofile(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    p=await get_player(message.chat.id,message.from_user.id,message.from_user.username,message.from_user.first_name)
    ca=int(p["correct_answers"]); lvl,_,_,_,ts,_=level_info(ca); bar=make_progress_bar(ca)
    sub=is_subscriber_cached(message.chat.id,message.from_user.id)
    pl=await get_player_place(message.chat.id,message.from_user.id); ps=f"#{pl}" if pl else "—"
    td=await withdrawn_today(message.chat.id,message.from_user.id)
    nl="🏆 Максимальный уровень!" if lvl>=MAX_LEVEL else f"⬆️ До {LEVELS[lvl][1]} {LEVELS[lvl][2]}: {ANSWERS_PER_LEVEL-(ca-(lvl-1)*ANSWERS_PER_LEVEL)} очк."
    sl=f"\n💎 Подписка: <b>активна</b>" if sub else "\n💎 Подписка: нет"
    per=money_for_answer(ca)
    await safe_send(message.reply,f"👤 <b>{message.from_user.first_name}</b>\n\n🎖 <b>{ts}</b>{sl}\n\n<code>{bar}</code>\n{nl}\n\n💰 <b>${float(p['balance']):.4f}</b>\n💵 За ответ: <b>${per:.4f}</b>\n💸 Выведено: ${td:.4f}\n🏆 <b>{ca}</b>\n📍 <b>{ps}</b>")
@dp.message(Command("AiLevels"))
async def cmd_ailevels(message:Message):
    if message.chat.type not in ("group","supergroup"): return
    ls=["🎖 <b>Уровни и выплаты</b>\n"]
    for lvl,em,nm in LEVELS:
        mn=(lvl-1)*ANSWERS_PER_LEVEL; rq=f"{mn}+" if lvl==MAX_LEVEL else f"{mn}-{mn+ANSWERS_PER_LEVEL-1}"
        per=round(BASE_MONEY_PER_CORRECT+(lvl-1)*MONEY_PER_LEVEL,4)
        ls.append(f"{em} <b>Ур. {lvl}</b> · {nm} · <i>{rq}</i> · 💰 ${per:.4f}")
    await safe_send(message.reply,"\n".join(ls))
@dp.message(Command("AiTop"))
async def cmd_aitop(message:Message):
    if message.chat.type not in ("group","supergroup"): return
    rows=await get_top(message.chat.id,10)
    if not rows: await safe_send(message.reply,"Никто не играл."); return
    av=message.from_user and is_admin(message.from_user.id); ls=["🏆 <b>Топ</b>"]
    for i,r in enumerate(rows,1):
        ca=int(r.get("correct_answers",0)); _,em,_,_,_,_=level_info(ca)
        nm=r.get("first_name") or r.get("username") or str(r["user_id"]); med=["🥇","🥈","🥉"][i-1] if i<=3 else f"{i}."
        uid=f" · <code>{r['user_id']}</code>" if av else ""
        ls.append(f"{med} {em} {nm} — {ca} · ${float(r['balance']):.4f}{uid}")
    await safe_send(message.reply,"\n".join(ls))
@dp.message(Command("AiDeposit"))
async def cmd_deposit(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    parts=(message.text or "").split()
    if len(parts)!=2:
        await safe_send(message.reply,f"💳 <b>Пополнение</b>\n\nФормат: <code>/AiDeposit 1.0</code>\n\nМин: <b>${DEPOSIT_MIN:.2f}</b> · Макс: <b>${DEPOSIT_MAX:.2f}</b>\n⚠️ Комиссия: <b>{DEPOSIT_COMMISSION*100:.0f}%</b>")
        return
    try: a=round(float(parts[1]),4)
    except ValueError: await safe_send(message.reply,"💳 Формат: <code>/AiDeposit 1.0</code>\n\nСумма — число"); return
    if a<DEPOSIT_MIN or a>DEPOSIT_MAX: await safe_send(message.reply,f"❌ Сумма должна быть от <b>${DEPOSIT_MIN:.2f}</b> до <b>${DEPOSIT_MAX:.2f}</b>"); return
    cm=booster_mult(message.chat.id,"commission",1.0); ec=DEPOSIT_COMMISSION*cm
    com=round(a*ec,4); cr=round(a-com,4)
    ciid=f"dep_{message.from_user.id}_{uuid.uuid4().hex[:12]}"; ok,res=await xrocket_create_invoice(ciid,a,f"Dep {message.from_user.id}")
    if not ok: await safe_send(message.reply,f"❌ <code>{res}</code>"); return
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💳 Оплатить ${a:.2f}",url=res)]])
    sent=await safe_send(message.reply,f"💳 <b>Счёт на пополнение</b>\n\n💵 Оплата: <b>${a:.4f}</b>\n🏦 Комиссия {ec*100:.0f}%: <b>-${com:.4f}</b>\n💰 Зачислится: <b>${cr:.4f}</b>\n\nДействителен 1 час. Оплати в @xrocket.",reply_markup=kb)
    mid=sent.message_id if sent else None
    await create_invoice(ciid,message.from_user.id,message.chat.id,a,mid,kind="deposit")
@dp.message(Command("AiDuel"))
async def cmd_duel(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    cid,uid=message.chat.id,message.from_user.id
    if is_banned_cached(cid,uid): await safe_send(message.reply,"🚫 Ты в бане"); return
    parts=(message.text or "").split()
    if len(parts)<2: await safe_send(message.reply,f"🎲 <b>Дуэль на кубах</b>\n\nФормат: <code>/AiDuel 0.20</code>\n\nСтавка: <b>${DUEL_MIN:.2f}</b>–<b>${DUEL_MAX:.2f}</b>\n\nОтветь на сообщение противника командой."); return
    try: a=round(float(parts[1]),4)
    except ValueError: await safe_send(message.reply,"🎲 Формат: <code>/AiDuel 0.20</code>\n\nСтавка — число"); return
    if a<DUEL_MIN or a>DUEL_MAX: await safe_send(message.reply,f"❌ Ставка должна быть от <b>${DUEL_MIN:.2f}</b> до <b>${DUEL_MAX:.2f}</b>"); return
    oid=onm=None
    if message.reply_to_message and message.reply_to_message.from_user:
        o=message.reply_to_message.from_user; oid,onm=o.id,o.first_name
    elif message.entities:
        for e in message.entities:
            if e.type=="text_mention" and e.user: oid,onm=e.user.id,e.user.first_name; break
    if not oid: await safe_send(message.reply,"🎲 Ответь на сообщение противника, чтобы вызвать его на дуэль."); return
    if oid==uid: await safe_send(message.reply,"😄 Себе нельзя"); return
    if oid==bot.id: await safe_send(message.reply,"🤖 С ботом нельзя"); return
    pc=await get_player(cid,uid,message.from_user.username,message.from_user.first_name); po=await get_player(cid,oid)
    if float(pc["balance"])<a: await safe_send(message.reply,f"❌ У тебя только <b>${float(pc['balance']):.4f}</b>"); return
    if float(po["balance"])<a: await safe_send(message.reply,f"❌ У противника только <b>${float(po['balance']):.4f}</b>"); return
    if uid in DUEL_BUSY or oid in DUEL_BUSY: await safe_send(message.reply,"⏳ Кто-то уже в дуэли"); return
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅",callback_data=f"duel:a:{uid}:{oid}:{a}"),InlineKeyboardButton(text="❌",callback_data=f"duel:r:{uid}:{oid}:{a}")]])
    sent=await safe_send(message.reply,f"🎲 <b>Дуэль!</b>\n\n<b>{message.from_user.first_name}</b> vs <b>{onm}</b>\n\n💵 Ставка: <b>${a:.4f}</b> · Банк: <b>${a*2:.4f}</b>\n<i>Рейк {RAKE_PCT*100:.0f}%</i>",reply_markup=kb)
    if not sent: return
    async def ac():
        await asyncio.sleep(DUEL_TTL)
        try:
            await bot.edit_message_reply_markup(cid,sent.message_id,reply_markup=None); await bot.edit_message_text(chat_id=cid,message_id=sent.message_id,text=f"⌛ Дуэль истекла. {onm} не ответил.")
        except Exception: pass
    asyncio.create_task(ac())
@dp.callback_query(F.data.startswith("duel:"))
async def on_duel_cb(cb:CallbackQuery):
    if not cb.from_user or not isinstance(cb.message,Message): await cb.answer(); return
    parts=cb.data.split(":")
    if len(parts)!=5: await cb.answer("Ошибка",show_alert=True); return
    _,act,cs,os_,as_=parts
    try: ch=int(cs); op=int(os_); a=float(as_)
    except ValueError: await cb.answer("Ошибка",show_alert=True); return
    cid=cb.message.chat.id
    if cb.from_user.id!=op: await cb.answer("Это не твой вызов.",show_alert=True); return
    if act=="r":
        try: await cb.message.edit_text(f"❌ <b>Отклонено.</b>\n{cb.from_user.first_name} отказался от дуэли.")
        except Exception: pass
        await cb.answer("Отклонено"); return
    await cb.answer("Поехали!")
    if ch in DUEL_BUSY or op in DUEL_BUSY:
        try: await cb.message.edit_text("⏳ Кто-то уже в дуэли")
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
            try: await cb.message.edit_text("❌ Не удалось списать у вызывающего")
            except Exception: pass
            return
        if not await deduct_balance(cid,op,a):
            await add_balance(cid,ch,a)
            try: await cb.message.edit_text("❌ Не удалось списать у соперника")
            except Exception: pass
            return
        nc=pc.get("first_name") or str(ch); no=po.get("first_name") or str(op)
        try: await cb.message.edit_text(f"🎲 <b>Дуэль началась!</b>\n\n💰 Банк: <b>${a*2:.4f}</b>\n🎯 {nc} vs {no}")
        except Exception: pass
        await asyncio.sleep(1); m1=await safe_send(bot.send_dice,cid,emoji="🎲"); r1=m1.dice.value if m1 and m1.dice else 0
        await asyncio.sleep(2); m2=await safe_send(bot.send_dice,cid,emoji="🎲"); r2=m2.dice.value if m2 and m2.dice else 0
        await asyncio.sleep(2)
        if r1==r2:
            await add_balance(cid,ch,a); await add_balance(cid,op,a)
            await safe_send(bot.send_message,cid,f"🤝 <b>Ничья! {r1}:{r2}</b>\nСтавки возвращены."); return
        wid,wn=(ch,nc) if r1>r2 else (op,no); lid=op if wid==ch else ch
        tp=round(a*2,4); rk=round(tp*RAKE_PCT,4); py=round(tp-rk,4)
        await add_balance(cid,wid,py); await log_house_income(cid,rk,"duel")
        await safe_send(bot.send_message,cid,f"🏆 <b>{wn} победил!</b>\n\n🎲 {nc}: <b>{r1}</b> · {no}: <b>{r2}</b>\n💰 Забирает: <b>${py:.4f}</b>")
        try: await bot.send_message(lid,f"💔 <b>Проиграл дуэль</b> против {wn}\nСтавка <b>${a:.4f}</b> списана.")
        except Exception: pass
    finally:
        DUEL_BUSY.discard(ch); DUEL_BUSY.discard(op)
@dp.message(Command("AiWithdraw"))
async def cmd_aiwithdraw(message:Message):
    if message.chat.type not in ("group","supergroup") or not message.from_user: return
    cid,uid=message.chat.id,message.from_user.id
    if is_banned_cached(cid,uid): await safe_send(message.reply,"🚫 Ты в бане"); return
    p=await get_player(cid,uid,message.from_user.username,message.from_user.first_name); bal=float(p["balance"])
    if bal<MIN_WITHDRAW: await safe_send(message.reply,f"❌ Минимум <b>${MIN_WITHDRAW:.2f}</b>. У тебя <b>${bal:.4f}</b>"); return
    td=await withdrawn_today(cid,uid); rm=DAILY_WITHDRAW_LIMIT-td
    if rm<=0: await safe_send(message.reply,f"❌ Дневной лимит исчерпан (${DAILY_WITHDRAW_LIMIT:.2f})"); return
    a=min(bal,rm); cm=booster_mult(cid,"commission",1.0); ec=WITHDRAW_COMMISSION*cm
    com=round(a*ec,4); rc=round(a-com,4)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅",callback_data=f"wd:accept:{uid}"),InlineKeyboardButton(text="❌",callback_data=f"wd:reject:{uid}")]])
    sent=await safe_send(message.reply,f"💸 <b>Вывод</b>\n\n💼 Списание: <b>${a:.4f}</b>\n🏦 Комиссия {ec*100:.0f}%: <b>-${com:.4f}</b>\n📤 Получишь: <b>${rc:.4f}</b>\nID: <code>{uid}</code>\n\n⚠️ Зайди в <a href=\"{XROCKET_REFERRAL_URL}\">@xrocket</a>.\n\nЗапрос: {WITHDRAW_CONFIRM_TTL//60} мин.",reply_markup=kb)
    if not sent: return
    PENDING_WITHDRAWS[sent.message_id]={"chat_id":cid,"user_id":uid,"amount":a,"ts":time.time()}
    async def ac():
        await asyncio.sleep(WITHDRAW_CONFIRM_TTL); info=PENDING_WITHDRAWS.pop(sent.message_id,None)
        if not info: return
        try: await bot.edit_message_text(chat_id=cid,message_id=sent.message_id,text="⌛ Запрос истёк. /AiWithdraw снова.")
        except Exception: pass
    asyncio.create_task(ac())
@dp.callback_query(F.data.startswith("wd:"))
async def on_withdraw_cb(cb:CallbackQuery):
    if not cb.from_user or not isinstance(cb.message,Message): await cb.answer(); return
    parts=cb.data.split(":")
    if len(parts)!=3: await cb.answer("Ошибка",show_alert=True); return
    act,os_=parts[1],parts[2]
    try: oid=int(os_)
    except ValueError: await cb.answer("Ошибка",show_alert=True); return
    if cb.from_user.id!=oid: await cb.answer("⛔ Не твой запрос",show_alert=True); return
    info=PENDING_WITHDRAWS.pop(cb.message.message_id,None)
    if not info: await cb.answer("⌛ Уже истёк",show_alert=True); return
    cid,uid,a=info["chat_id"],info["user_id"],info["amount"]
    if act=="reject":
        try: await cb.message.edit_text(f"❌ Отменено. <b>${a:.4f}</b> на балансе.")
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
            try: await cb.message.edit_text(f"❌ Минимум <b>${MIN_WITHDRAW:.2f}</b>")
            except Exception: pass
            return
        cm=booster_mult(cid,"commission",1.0); ec=WITHDRAW_COMMISSION*cm
        com=round(a*ec,4); rc=round(a-com,4)
        if rc<=0:
            try: await cb.message.edit_text("❌ Сумма слишком мала после комиссии")
            except Exception: pass
            return
        try: await cb.message.edit_text(f"⏳ Отправляю ${rc:.4f}...")
        except Exception: pass
        ok,res=await xrocket_payout(cid,uid,rc)
        if ok:
            await deduct_balance(cid,uid,a); await log_payout(cid,uid,a,res,"finished")
            if com>0:
                try: await log_house_income(cid,com,"withdraw_commission")
                except Exception: pass
            try: await cb.message.edit_text(f"✅ <b>Выплачено</b>\n💼 Списано: <b>${a:.4f}</b>\n🏦 Комиссия: <b>${com:.4f}</b>\n📤 Получено: <b>${rc:.4f}</b>\nID: <code>{res}</code>")
            except Exception: pass
        else:
            await log_payout(cid,uid,a,"","failed")
            try: await cb.message.edit_text(f"❌ <b>Ошибка выплаты</b>\n<code>{res}</code>")
            except Exception: pass
    finally: await unlock_withdraw(cid,uid)
@dp.message(F.text & ~F.text.startswith("/") & F.chat.type.in_({"group","supergroup"}))
async def handle_answer(message:Message):
    if not message.from_user: return
    if message.from_user.id in TOURNAMENT_EDIT: raise SkipHandler()
    cid,uid=message.chat.id,message.from_user.id; tx=(message.text or "").strip().lower()
    if cid in TOURNAMENT_ACTIVE:
        st=TOURNAMENT_STATE.get(cid)
        if st and not st.get("answered_by") and tx in st["answers"]: st["answered_by"]=uid
        return
    q=ACTIVE_QUESTIONS.get(cid)
    if not q: return
    if tx not in q["answers"]:
        if is_admin(uid):
            try: await bot.set_message_reaction(cid,message.message_id,["❌"])
            except Exception: pass
        return
    if is_banned_cached(cid,uid): return
    pp=ACTIVE_QUESTIONS.pop(cid,None)
    if pp is None: return
    tk=pp.get("timer_task")
    if tk: tk.cancel()
    p,_=await asyncio.gather(get_player(cid,uid,message.from_user.username,message.from_user.first_name),clear_active(cid),return_exceptions=True)
    if isinstance(p,Exception) or p is None: p=await get_player(cid,uid,message.from_user.username,message.from_user.first_name)
    old_correct=int(p["correct_answers"])
    ol=level_from_correct(old_correct); ot=TOP_CACHE.get(cid)
    is_sub=is_subscriber_cached(cid,uid)
    base=2 if is_sub else 1
    pm=booster_mult(cid,"points",1.0)
    pg=int(base*pm)
    money_base=money_for_answer(old_correct)
    money_mult=booster_mult(cid,"money",1.0)
    money_reward=round(money_base*money_mult,4)
    await add_score(cid,uid)
    for _ in range(pg-1): await add_score(cid,uid)
    if money_reward>0:
        try: await add_balance(cid,uid,money_reward)
        except Exception: pass
    nc=old_correct+pg; nl=level_from_correct(nc)
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
    ph=random.choice(CORRECT_PHRASES)
    if q["is_multi"]: ash="любой из: "+", ".join(q["answers"][:5])+("..." if len(q["answers"])>5 else "")
    else: ash=q["answers"][0]
    parts2=[f"+{pg} очк"]
    if is_sub: parts2.append("💎")
    parts2.append(f"+${money_reward:.4f} 💰")
    if money_mult>1: parts2.append(f"x{int(money_mult)}")
    msg=f"{ph}\n{message.from_user.first_name} — {' · '.join(parts2)}\n<i>Ответ: {ash}</i>"
    if nl>ol:
        new_per=money_for_answer(nc)
        msg+=f"\n\n{LEVELS[nl-1][1]} <b>НОВЫЙ УРОВЕНЬ {nl}!</b>\n🎖 {LEVELS[nl-1][2]}\n💵 Теперь за ответ: <b>${new_per:.4f}</b>"
    sent=await safe_send(message.reply,msg)
    if sent:
        try: await bot.set_message_reaction(cid,message.message_id,["✅"])
        except Exception: pass

async def main():
    print("="*50); print("Quiz Bot · money per answer by level")
    print(f"Easy {len(EASY_QUESTIONS)} · Medium {len(MEDIUM_QUESTIONS)} · Hard {len(HARD_QUESTIONS)} · Extreme {len(EXTREME_QUESTIONS)}")
    print(f"Admins: {sorted(ADMIN_IDS)}")
    print(f"Money per answer: ${BASE_MONEY_PER_CORRECT:.4f} + ${MONEY_PER_LEVEL:.4f} × (level-1)")
    print(f"Commissions: dep {DEPOSIT_COMMISSION*100:.0f}% / wd {WITHDRAW_COMMISSION*100:.0f}%")
    print(f"Lottery: base {LOTTERY_PRICE:.2f}/ticket · packs {LOTTERY_PACKS} · refund + {int(LOTTERY_WINNER_SHARE*100)}% остатка")
    await get_http(); await asyncio.to_thread(unlock_all_withdrawals_sync)
    global BANNED_CACHE,SUBSCRIBERS_CACHE
    BANNED_CACHE=await asyncio.to_thread(load_bans_sync); SUBSCRIBERS_CACHE=await asyncio.to_thread(load_subscribers_sync)
    ar=await asyncio.to_thread(load_active_sync)
    for row in ar:
        ans=row["answer"].split("||")
        ACTIVE_QUESTIONS[int(row["chat_id"])]={"question":row["question"],"answers":[a.lower() for a in ans],"is_multi":row.get("is_multi",False),"timer":False,"timer_task":None}
        QUIZ_ENABLED.add(int(row["chat_id"]))
    for cid in QUIZ_ENABLED:
        try:
            t=await get_top1(cid)
            if t: TOP_CACHE[cid]=t
        except Exception: pass
    me=await bot.get_me(); print(f"@{me.username}")
    await start_webhook_server()
    asyncio.create_task(question_scheduler()); asyncio.create_task(caches_refresh_loop()); asyncio.create_task(pot_payout_loop()); asyncio.create_task(loto_loop())
    print("Запущен."); print("="*50)
    try: await dp.start_polling(bot)
    finally: await close_http()
if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
    except Exception as e:
        print("!!!",type(e).__name__,"-",e); raise
