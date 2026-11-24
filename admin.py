"""Administrative support tools. No commerce endpoints or promotional messaging."""
import os, time, sqlite3, tempfile, asyncio, logging
from aiogram import F, Bot
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from api import db, DB_PATH

import safety_tools

OWNER = int(os.environ['ADMIN_ID'])

def init():
    safety_tools.init()
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS moderation_notes(id INTEGER PRIMARY KEY, target INTEGER NOT NULL, actor INTEGER NOT NULL, body TEXT NOT NULL, at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS staff_roles(uid INTEGER PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','support')));
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
    if r in ('owner','admin'): items += [('🛡 Модерация V4','safety'),('📋 Журнал','logs'),('🛠 Техработы','maintenance'),('📈 Графики','charts'),('👤 Последняя активность','recent'),('👥 Пользователи','users:0'),('🚫 Заблокированные','banned:0'),('🔎 Поиск пользователя','search'),('📊 Состояние базы','health')]
    if r=='owner': items += [('💾 Бэкап','backup'),('👥 Администраторы','staff'),('📢 Сервисное уведомление','notice')]
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
    if action.split(':')[0] in ('users','banned','profile','block','allow','confirm','search','health'):
        if r in ('owner','admin'): await moderation(message,uid,action)
        return
    if action=='safety' and r in ('owner','admin'):
        await message.answer('/tempban ID минуты причина\n/warn ID причина\n/violations ID\n/priority ID low|normal|high\n/assign_ticket ID ID_сотрудника\n/reopen_ticket ID\n/maintenance_for минуты\n/diagnostics',reply_markup=panel(r)); return
    if action=='home': await message.answer('Панель управления',reply_markup=panel(r)); return
    if action=='backup' and r=='owner': await backup(message); return
    if action=='charts' and r in ('owner','admin'): await charts(message); return
    if action=='notice' and r=='owner': await message.answer('/notice maintenance — технические работы\n/notice restored — восстановление'); return
    with db() as c:
        if action=='staff' and r=='owner':
            out='Владелец: '+str(OWNER)+'\n'+ '\n'.join(f'{x[0]} — {x[1]}' for x in c.execute('SELECT uid,role FROM staff_roles'))+'\n/staff ID admin|support|remove'
        elif action=='logs' and r in ('owner','admin'):
            out='Последние действия в боте:\n'+'\n'.join(f'{time.strftime("%d.%m %H:%M",time.gmtime(x[2]))} UTC | {x[0]} | {x[1]}' for x in c.execute('SELECT uid,action,at FROM staff_audit ORDER BY id DESC LIMIT 20'))
        elif action=='recent' and r in ('owner','admin'):
            out='Последняя активность (UTC):\n'+'\n'.join(f'{x[0]} | {time.strftime("%d.%m %H:%M",time.gmtime(x[1]))}' for x in c.execute('SELECT uid,MAX(at) AS last FROM activity_events GROUP BY uid ORDER BY last DESC LIMIT 30'))
        elif action=='activity':
            now=int(time.time()); out='Активность бота (уникальные пользователи)\n'
            counts=[c.execute('SELECT COUNT(DISTINCT uid) FROM activity_events WHERE at>=?',(now-d*86400,)).fetchone()[0] for d in (1,7,30)]
            for d,n in zip((1,7,30),counts): out+=f'\n{d} дн. | '+ '█'*round(20*n/max(1,max(counts)))+f' {n}'
        elif action=='tickets':
            out='Открытые обращения:\n'+'\n'.join(f'#{x[0]} | {x[1]} | {x[2]}: {x[3][:100]}' for x in c.execute("SELECT id,uid,kind,body FROM support_tickets WHERE status='open' ORDER BY id DESC LIMIT 15"))+'\n/ticket ID — история\n/answer_ticket ID текст\n/close_ticket ID'
        elif action=='maintenance' and r in ('owner','admin'):
            c.execute("DELETE FROM service_settings WHERE key='maintenance_until'")
            old=c.execute("SELECT value FROM service_settings WHERE key='maintenance'").fetchone(); value='0' if old and old[0]=='1' else '1'
            c.execute("INSERT OR REPLACE INTO service_settings VALUES('maintenance',?)",(value,)); out='Техработы в боте: '+('включены' if value=='1' else 'выключены')
        else: return
    await message.answer(out[:4000],reply_markup=panel(r))

def register(dp):
    import sys
    safety_tools.register(dp, sys.modules[__name__])
    @dp.message.outer_middleware()
    async def track(handler,event,data):
        safety_tools.tick()
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

    @dp.message(Command('finduser','modnote','ban','unban'),F.chat.type=='private')
    async def mod_command(m:Message,command:CommandObject):
        uid=m.from_user.id
        if role(uid) not in ('owner','admin'): return
        arg=(command.args or '').strip()
        if command.command=='finduser':
            if not arg: await m.answer('/finduser ID или username'); return
            with db() as c:
                rows=c.execute("SELECT user_id,username FROM users WHERE CAST(user_id AS TEXT)=? OR username LIKE ? ESCAPE '\\' LIMIT 20",(arg,'%'+arg.lstrip('@').replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')).fetchall()
            await m.answer('Результаты поиска' if rows else 'Не найдено',reply_markup=keyboard([(f'{x[0]} @{x[1] or "—"}',f'profile:{x[0]}') for x in rows]+[('Назад','home')]))
            return
        parts=arg.split(maxsplit=1)
        if not parts or not parts[0].isdigit() or len(parts[0])>18: await m.answer('Укажи корректный ID'); return
        target=int(parts[0])
        if command.command=='modnote':
            if len(parts)!=2: await m.answer('/modnote ID текст'); return
            with db() as c:
                if not c.execute('SELECT 1 FROM users WHERE user_id=?',(target,)).fetchone(): await m.answer('Не найдено'); return
                c.execute('INSERT INTO moderation_notes(target,actor,body,at) VALUES(?,?,?,?)',(target,uid,parts[1][:500],int(time.time())))
            audit(uid,f'note:{target}'); await m.answer('Заметка сохранена'); return
        await moderation(m,uid,f'confirm:{"block" if command.command=="ban" else "allow"}:{target}')

    @dp.message(Command('notice'),F.chat.type=='private')
    async def notice(m:Message,command:CommandObject,bot:Bot):
        if m.from_user.id!=OWNER:return
        key=(command.args or '').strip()
        if key not in NOTICES: await m.answer('/notice maintenance|restored');return
        audit(OWNER,'notice:'+key)
        ok,fail=await notify(bot,key)
        await m.answer(f'Доставлено: {ok}; ошибок: {fail}')

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


NOTICES={'maintenance':'🚧 Технические работы. Некоторые функции временно недоступны.', 'restored':'✅ Технические работы завершены. Бот снова доступен.'}
notice_lock=asyncio.Lock()
async def notify(bot,key):
    async with notice_lock:
        with db() as c:
            ids=[x[0] for x in c.execute('SELECT user_id FROM users WHERE banned=0')]
        ok=fail=0
        for uid in ids:
            try:
                await bot.send_message(uid,NOTICES[key]);ok+=1
            except Exception:fail+=1
            await asyncio.sleep(.08)
        return ok,fail

async def daily_backup(bot):
    while True:
        try:
            day=time.strftime('%Y-%m-%d',time.gmtime())
            with db() as c:
                last=c.execute("SELECT value FROM service_settings WHERE key='backup_day'").fetchone()
            if not last or last[0]!=day:
                with tempfile.TemporaryDirectory() as tmp:
                    path=os.path.join(tmp,'freevape.db')
                    await asyncio.to_thread(backup_file,path)
                    await bot.send_document(OWNER,FSInputFile(path),caption='Ежедневный бэкап SQLite (UTC). Храните конфиденциально.')
                with db() as c:c.execute("INSERT OR REPLACE INTO service_settings VALUES('backup_day',?)",(day,))
                audit(OWNER,'automatic_backup')
        except Exception:logging.exception('Daily backup failed')
        await asyncio.sleep(3600)

async def charts(message):
    now=int(time.time())
    with db() as c:
        for days,bins in ((1,24),(7,7),(30,30)):
            step=days*86400//bins
            counts=[c.execute('SELECT COUNT(DISTINCT uid) FROM activity_events WHERE at>=? AND at<?',(now-days*86400+i*step,now-days*86400+(i+1)*step)).fetchone()[0] for i in range(bins)]
            lines=[f'Активность за {days} дн. (UTC, уникальные за интервал)']
            for i,n in enumerate(counts):
                label=time.strftime('%H:%M' if days==1 else '%d.%m',time.gmtime(now-days*86400+i*step))
                lines.append(f'{label} | '+ '█'*round(18*n/max(1,max(counts)))+f' {n}')
            await message.answer('\n'.join(lines))


async def moderation(message, actor, action):
    if role(actor) not in ('owner','admin'): return
    p=action.split(':'); kind=p[0]
    if kind=='search': await message.answer('/finduser ID или username\n/modnote ID текст — внутренняя заметка'); return
    if kind=='health':
        with db() as c:
            counts=[(t,c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]) for t in ('users','support_tickets','staff_audit','activity_events','moderation_notes')]
        await message.answer('Состояние базы\n'+'\n'.join(f'{t}: {n}' for t,n in counts)+f'\nРазмер файла: {os.path.getsize(DB_PATH)} байт',reply_markup=keyboard([('Назад','home')])); return
    if kind=='confirm':
        if len(p)!=3 or p[1] not in ('block','allow') or not p[2].isdigit() or len(p[2])>18: return
        await message.answer(f'Подтвердить {"блокировку" if p[1]=="block" else "разблокировку"} ID {p[2]}?',reply_markup=keyboard([('Подтвердить',f'{p[1]}:{p[2]}'),('Отмена',f'profile:{p[2]}')])); return
    if len(p)!=2 or not p[1].isdigit() or len(p[1])>18: return
    n=int(p[1])
    if kind in ('users','banned'):
        where='WHERE banned=1' if kind=='banned' else ''
        with db() as c: rows=c.execute(f'SELECT user_id,username,banned FROM users {where} ORDER BY joined DESC,user_id DESC LIMIT 11 OFFSET ?',(n,)).fetchall()
        items=[(f'{"🚫" if x[2] else "👤"} {x[0]} @{x[1] or "—"}',f'profile:{x[0]}') for x in rows[:10]]
        if n: items.append(('⬅️ Назад',f'{kind}:{max(0,n-10)}'))
        if len(rows)>10: items.append(('Далее ➡️',f'{kind}:{n+10}'))
        await message.answer('Заблокированные' if kind=='banned' else 'Пользователи',reply_markup=keyboard(items+[('Главная','home')])); return
    if kind in ('block','allow'):
        if role(n): await message.answer('Нельзя блокировать или изменять блокировку сотрудника'); return
        with db() as c:
            c.execute('DELETE FROM safety_bans WHERE uid=?',(n,))
            cur=c.execute('UPDATE users SET banned=? WHERE user_id=?',(int(kind=='block'),n))
            changed=cur.rowcount
        if not changed: await message.answer('Пользователь не найден'); return
        audit(actor,f'{kind}:{n}')
    with db() as c:
        u=c.execute('SELECT * FROM users WHERE user_id=?',(n,)).fetchone()
        last=c.execute('SELECT MAX(at) FROM activity_events WHERE uid=?',(n,)).fetchone()[0]
        notes=c.execute('SELECT body FROM moderation_notes WHERE target=? ORDER BY id DESC LIMIT 3',(n,)).fetchall()
        tickets=c.execute('SELECT COUNT(*) FROM support_tickets WHERE uid=?',(n,)).fetchone()[0]
    if not u: await message.answer('Пользователь не найден'); return
    out=f'ID: {n}\nИмя: {u["full_name"] or "—"}\nUsername: @{u["username"] or "—"}\nСтатус: {"заблокирован" if u["banned"] else "активен"}\nПоследняя активность: {time.strftime("%d.%m.%Y %H:%M",time.gmtime(last)) if last else "нет данных"} UTC\nОбращений: {tickets}\nЗаметки:\n'+'\n'.join(x[0] for x in notes)
    items=[] if role(n) else [('Разбанить' if u['banned'] else 'Забанить',f'confirm:{"allow" if u["banned"] else "block"}:{n}')]
    await message.answer(out[:3900],reply_markup=keyboard(items+[('Пользователи','users:0'),('Главная','home')]))
    
