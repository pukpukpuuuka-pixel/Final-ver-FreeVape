import time, sqlite3, os, logging
from aiogram.filters import Command, CommandObject
from aiogram import F
from api import db, DB_PATH

START=time.monotonic()
def init():
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS safety_bans(uid INTEGER PRIMARY KEY, until_at INTEGER NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS safety_warnings(id INTEGER PRIMARY KEY, uid INTEGER, actor INTEGER, reason TEXT, at INTEGER);
        CREATE TABLE IF NOT EXISTS ticket_meta(ticket INTEGER PRIMARY KEY, priority TEXT DEFAULT 'normal', assignee INTEGER);
        CREATE TABLE IF NOT EXISTS safety_errors(id INTEGER PRIMARY KEY, at INTEGER, kind TEXT);''')
def expire():
    with db() as c:
        now=int(time.time())
        c.execute('UPDATE users SET banned=0 WHERE user_id IN (SELECT uid FROM safety_bans WHERE until_at<=?)',(now,))
        c.execute('DELETE FROM safety_bans WHERE until_at<=?',(now,))
def register(dp, admin):
    @dp.message(Command('tempban','warn','violations','priority','assign_ticket','reopen_ticket','maintenance_for','diagnostics'), F.chat.type=='private')
    async def command(m, command:CommandObject):
        actor=m.from_user.id; role=admin.role(actor); action=command.command
        if role not in ('owner','admin') and not (role=='support' and action in ('priority','assign_ticket','reopen_ticket')): return
        p=(command.args or '').split(maxsplit=2)
        if action=='diagnostics':
            with db() as c: result=c.execute('PRAGMA quick_check').fetchone()[0]
            await m.answer(f'Uptime: {int(time.monotonic()-START)} сек.\nSQLite: {result}\nРазмер: {os.path.getsize(DB_PATH)} байт'); return
        if not p or not p[0].isdigit() or len(p[0])>18: await m.answer('Укажи числовой ID или срок в минутах.'); return
        n=int(p[0]); now=int(time.time())
        if action=='maintenance_for':
            if not 1<=n<=10080: await m.answer('Срок: 1–10080 минут'); return
            with db() as c:
                c.execute("INSERT OR REPLACE INTO service_settings VALUES('maintenance','1')")
                c.execute("INSERT OR REPLACE INTO service_settings VALUES('maintenance_until',?)",(str(now+n*60),))
        elif action in ('tempban','warn','violations'):
            if admin.role(n): await m.answer('Сотрудники защищены'); return
            with db() as c:
                if not c.execute('SELECT 1 FROM users WHERE user_id=?',(n,)).fetchone(): await m.answer('Пользователь не найден'); return
                if action=='violations':
                    rows=c.execute('SELECT reason,at FROM safety_warnings WHERE uid=? ORDER BY id DESC LIMIT 15',(n,)).fetchall()
                    await m.answer(('Предупреждения:\n'+'\n'.join(f'{x[1]}: {x[0]}' for x in rows))[:3900]); return
                if action=='warn':
                    if len(p)<2: await m.answer('/warn ID причина'); return
                    c.execute('INSERT INTO safety_warnings(uid,actor,reason,at) VALUES(?,?,?,?)',(n,actor,' '.join(p[1:])[:500],now))
                else:
                    if len(p)<3 or not p[1].isdigit() or not 1<=int(p[1])<=43200: await m.answer('/tempban ID минуты причина (1–43200)'); return
                    c.execute('UPDATE users SET banned=1 WHERE user_id=?',(n,))
                    c.execute('INSERT OR REPLACE INTO safety_bans VALUES(?,?,?)',(n,now+int(p[1])*60,p[2][:500]))
        else:
            with db() as c:
                if not c.execute('SELECT 1 FROM support_tickets WHERE id=?',(n,)).fetchone(): await m.answer('Обращение не найдено'); return
                c.execute('INSERT OR IGNORE INTO ticket_meta(ticket) VALUES(?)',(n,))
                if action=='priority':
                    if len(p)!=2 or p[1] not in ('low','normal','high'): await m.answer('/priority ID low|normal|high'); return
                    c.execute('UPDATE ticket_meta SET priority=? WHERE ticket=?',(p[1],n))
                elif action=='assign_ticket':
                    if len(p)!=2 or not p[1].isdigit() or not admin.role(int(p[1])): await m.answer('/assign_ticket ID ID_сотрудника'); return
                    c.execute('UPDATE ticket_meta SET assignee=? WHERE ticket=?',(int(p[1]),n))
                else: c.execute("UPDATE support_tickets SET status='open' WHERE id=?",(n,))
        admin.audit(actor,f'{action}:{n}'); await m.answer('Готово')

def tick():
    expire()
    with db() as c:
        r=c.execute("SELECT value FROM service_settings WHERE key='maintenance_until'").fetchone()
        if r and int(r[0])<=time.time():
            c.execute("DELETE FROM service_settings WHERE key='maintenance_until'")
            c.execute("INSERT OR REPLACE INTO service_settings VALUES('maintenance','0')")
