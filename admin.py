"""Administrative support tools. No commerce endpoints or promotional messaging."""
import os, time, sqlite3, tempfile, asyncio, logging
from aiogram import F, Bot
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from api import db, DB_PATH

OWNER = int(os.environ['ADMIN_ID'])

def init():
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS staff_roles(uid INTEGER PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','support')));
        CREATE TABLE IF NOT EXISTS staff_audit(id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, action TEXT NOT NULL, at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS activity_events(uid INTEGER NOT NULL, at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS activity_at ON activity_events(at);
        CREATE TABLE IF NOT EXISTS support_tickets(id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS support_replies(id INTEGER PRIMARY KEY, ticket INTEGER NOT NULL, uid INTEGER NOT NULL, body TEXT NOT NULL, at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS service_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);''')

def role(uid):
    if uid == OWNER: return 'owner'
    with db() as c:
        r=c.execute('SELECT role FROM staff_roles WHERE uid=?',(uid,)).fetchone()
    return r[0] if r else None

def audit(uid, action):
    with db() as c: c.execute('INSERT INTO staff_audit(uid,action,at) VALUES(?,?,?)',(uid,action,int(time.time())))

def keyboard(items):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data='ops:'+v)] for t,v in items])

def panel(r):
    items=[('📨 Обращения','tickets'),('📊 Активность','activity')]
    if r in ('owner','admin'): items += [('📋 Журнал','logs'),('🛠 Техработы','maintenance')]
    if r=='owner': items += [('💾 Бэкап','backup'),('👥 Администраторы','staff')]
    return keyboard(items)

def backup_file(path):
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(path) as dest: source.backup(dest)

async def backup(message):
    with tempfile.TemporaryDirectory() as tmp:
        path=os.path.join(tmp,'freevape.db')
        await asyncio.to_thread(backup_file,path)
        await message.answer_document(FSInputFile(path),caption='Резервная копия SQLite. Храните конфиденциально.')

async def screen(message, uid, action):
    r=role(uid)
    if not r: return
    audit(uid,'panel:'+action)
    if action=='backup' and r=='owner': await backup(message); return
    with db() as c:
        if action=='staff' and r=='owner':
            out='Владелец: '+str(OWNER)+'\n'+ '\n'.join(f'{x[0]} — {x[1]}' for x in c.execute('SELECT uid,role FROM staff_roles'))+'\n/staff ID admin|support|remove'
        elif action=='logs' and r in ('owner','admin'):
            out='Последние действия в боте:\n'+'\n'.join(f'{time.strftime("%d.%m %H:%M",time.gmtime(x[2]))} UTC | {x[0]} | {x[1]}' for x in c.execute('SELECT uid,action,at FROM staff_audit ORDER BY id DESC LIMIT 20'))
        elif action=='activity':
            now=int(time.time()); out='Активность бота (уникальные пользователи)\n'
            counts=[c.execute('SELECT COUNT(DISTINCT uid) FROM activity_events WHERE at>=?',(now-d*86400,)).fetchone()[0] for d in (1,7,30)]
            for d,n in zip((1,7,30),counts): out+=f'\n{d} дн. | '+ '█'*round(20*n/max(1,max(counts)))+f' {n}'
        elif action=='tickets':
            out='Открытые обращения:\n'+'\n'.join(f'#{x[0]} | {x[1]} | {x[2]}: {x[3][:100]}' for x in c.execute("SELECT id,uid,kind,body FROM support_tickets WHERE status='open' ORDER BY id DESC LIMIT 15"))+'\n/ticket ID — история\n/answer_ticket ID текст\n/close_ticket ID'
        elif action=='maintenance' and r in ('owner','admin'):
            old=c.execute("SELECT value FROM service_settings WHERE key='maintenance'").fetchone(); value='0' if old and old[0]=='1' else '1'
            c.execute("INSERT OR REPLACE INTO service_settings VALUES('maintenance',?)",(value,)); out='Техработы в боте: '+('включены' if value=='1' else 'выключены')
        else: return
    await message.answer(out[:4000],reply_markup=panel(r))

def register(dp):
    @dp.message.outer_middleware()
    async def track(handler,event,data):
        u=event.from_user
        if u and event.chat.type=='private':
            r=role(u.id)
            with db() as c:
                c.execute('INSERT INTO activity_events VALUES(?,?)',(u.id,int(time.time())))
                m=c.execute("SELECT value FROM service_settings WHERE key='maintenance'").fetchone()
            if r: audit(u.id,'message:'+((event.text or '').split()[0].split('@')[0] if (event.text or '').startswith('/') else 'text'))
            if m and m[0]=='1' and not r and not (event.text or '').startswith(('/complaint','/support','/mytickets')):
                await event.answer('В боте технические работы. Поддержка: /support текст'); return
        return await handler(event,data)

    @dp.message(Command('admin','ops'), F.chat.type=='private')
    async def menu(m):
        r=role(m.from_user.id)
        if r: await m.answer('Панель управления',reply_markup=panel(r))

    @dp.callback_query(F.data.startswith('ops:'))
    async def callback(q:CallbackQuery):
        if not role(q.from_user.id) or not isinstance(q.message,Message) or q.message.chat.type!='private': await q.answer('Нет доступа',show_alert=True); return
        await q.answer(); await screen(q.message,q.from_user.id,q.data.split(':',1)[1])

    @dp.message(Command('staff'),F.chat.type=='private')
    async def staff(m:Message,command:CommandObject):
        if m.from_user.id!=OWNER:return
        p=(command.args or '').split()
        if len(p)!=2 or not p[0].isdigit() or int(p[0])==OWNER or p[1] not in ('admin','support','remove'): await m.answer('/staff ID admin|support|remove');return
        with db() as c:
            if p[1]=='remove':c.execute('DELETE FROM staff_roles WHERE uid=?',(int(p[0]),))
            else:c.execute('INSERT OR REPLACE INTO staff_roles VALUES(?,?)',(int(p[0]),p[1]))
        audit(OWNER,'staff:'+ ' '.join(p)); await m.answer('Права обновлены')

    @dp.message(Command('support','complaint'),F.chat.type=='private')
    async def ticket_new(m:Message,command:CommandObject,bot:Bot):
        body=(command.args or '').strip()
        if not body:await m.answer('Укажи текст после команды');return
        with db() as c:
            tid=c.execute('INSERT INTO support_tickets(uid,kind,body,at) VALUES(?,?,?,?)',(m.from_user.id,command.command,body[:3000],int(time.time()))).lastrowid
        await m.answer(f'Обращение #{tid} зарегистрировано')
        try:await bot.send_message(OWNER,f'Новое обращение #{tid}. /ticket {tid}')
        except Exception:logging.exception('Ticket notification failed')

    @dp.message(Command('mytickets'),F.chat.type=='private')
    async def mine(m):
        with db() as c: rows=c.execute('SELECT id,kind,status FROM support_tickets WHERE uid=? ORDER BY id DESC LIMIT 30',(m.from_user.id,)).fetchall()
        await m.answer('\n'.join(f'#{x[0]} {x[1]} — {x[2]}' for x in rows) or 'Нет обращений')

    @dp.message(Command('ticket','answer_ticket','close_ticket'),F.chat.type=='private')
    async def manage(m:Message,command:CommandObject,bot:Bot):
        if not role(m.from_user.id):return
        p=(command.args or '').split(maxsplit=1)
        if not p or not p[0].isdigit():await m.answer('Укажи номер обращения');return
        tid=int(p[0]); action=command.command
        with db() as c:
            t=c.execute('SELECT * FROM support_tickets WHERE id=?',(tid,)).fetchone()
            replies=c.execute('SELECT body FROM support_replies WHERE ticket=? ORDER BY id DESC LIMIT 10',(tid,)).fetchall()
        if not t:await m.answer('Не найдено');return
        if action=='ticket':await m.answer((f'#{tid} | {t["status"]}\n{t["body"]}\n'+'\n'.join(x[0] for x in replies))[:4000]);return
        if action=='answer_ticket':
            if len(p)!=2 or t['status']!='open':await m.answer('Нужен текст и открытое обращение');return
            try:await bot.send_message(t['uid'],f'Поддержка #{tid}:\n{p[1]}')
            except Exception:await m.answer('Не доставлено');return
            with db() as c:c.execute('INSERT INTO support_replies(ticket,uid,body,at) VALUES(?,?,?,?)',(tid,m.from_user.id,p[1],int(time.time())))
        else:
            with db() as c:c.execute("UPDATE support_tickets SET status='closed' WHERE id=?",(tid,))
        audit(m.from_user.id,f'{action}:{tid}');await m.answer('Готово')
