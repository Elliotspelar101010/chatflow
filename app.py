from flask import Flask, request, jsonify, send_from_directory
import os, time, hashlib, secrets, json
import psycopg2, psycopg2.extras
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def D(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    conn = get_db(); c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, avatar_color TEXT NOT NULL,
        avatar_img TEXT DEFAULT NULL, bio TEXT DEFAULT '',
        status_text TEXT DEFAULT '', theme TEXT DEFAULT 'default',
        created_at BIGINT NOT NULL, last_seen BIGINT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id SERIAL PRIMARY KEY, name TEXT, is_group BOOLEAN DEFAULT FALSE,
        pinned_message_id INTEGER DEFAULT NULL, created_at BIGINT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS conversation_members (
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        last_read_at BIGINT DEFAULT 0,
        PRIMARY KEY (conversation_id, user_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        content TEXT NOT NULL, msg_type TEXT DEFAULT 'text',
        edited BOOLEAN DEFAULT FALSE, deleted BOOLEAN DEFAULT FALSE,
        sent_at BIGINT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS reactions (
        message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        emoji TEXT NOT NULL,
        PRIMARY KEY (message_id, user_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at BIGINT NOT NULL)''')
    # Migrations for existing DBs
    for col, defn in [
        ('avatar_img','TEXT DEFAULT NULL'), ('bio','TEXT DEFAULT \'\''),
        ('status_text','TEXT DEFAULT \'\''), ('theme','TEXT DEFAULT \'default\'')
    ]:
        try: c.execute(f'ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {defn}')
        except: pass
    for col, defn in [('msg_type','TEXT DEFAULT \'text\''),('edited','BOOLEAN DEFAULT FALSE'),('deleted','BOOLEAN DEFAULT FALSE')]:
        try: c.execute(f'ALTER TABLE messages ADD COLUMN IF NOT EXISTS {col} {defn}')
        except: pass
    try: c.execute('ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pinned_message_id INTEGER DEFAULT NULL')
    except: pass
    try: c.execute('ALTER TABLE conversation_members ADD COLUMN IF NOT EXISTS last_read_at BIGINT DEFAULT 0')
    except: pass
    try: c.execute('''CREATE TABLE IF NOT EXISTS reactions (
        message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        emoji TEXT NOT NULL, PRIMARY KEY (message_id, user_id))''')
    except: pass
    conn.commit(); conn.close()

def get_user_from_token(token):
    conn = get_db(); c = D(conn)
    c.execute('SELECT u.* FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token=%s', (token,))
    user = c.fetchone(); conn.close()
    return user

def tok(): return request.headers.get('Authorization','').replace('Bearer ','')
def online(last_seen): return (int(time.time()) - last_seen) < 120

def msg_dict(m):
    d = dict(m)
    if d.get('deleted'): d['content'] = '🚫 This message was deleted'; d['msg_type'] = 'deleted'
    return d

@app.route('/')
def index(): return send_from_directory('static', 'index.html')

@app.route('/api/ping')
def ping():
    t = tok()
    if t:
        try:
            conn = get_db(); c = conn.cursor()
            c.execute('UPDATE users SET last_seen=%s WHERE id=(SELECT user_id FROM sessions WHERE token=%s)', (int(time.time()), t))
            conn.commit(); conn.close()
        except: pass
    return jsonify({'ok': True})

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username','').strip().lower()
    password = data.get('password','')
    if not username or not password: return jsonify({'error':'Username and password required'}), 400
    if len(username) < 3: return jsonify({'error':'Username must be 3+ characters'}), 400
    colors = ['#FF6B6B','#4ECDC4','#45B7D1','#96CEB4','#DDA0DD','#98D8C8','#F7DC6F','#BB8FCE','#6c63ff','#f59e0b','#10b981','#3b82f6']
    color = colors[len(username) % len(colors)]
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    now = int(time.time())
    conn = get_db(); c = D(conn)
    try:
        c.execute('INSERT INTO users (username,password_hash,avatar_color,created_at,last_seen) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                  (username, pw_hash, color, now, now))
        uid = c.fetchone()['id']
    except psycopg2.errors.UniqueViolation:
        conn.rollback(); conn.close(); return jsonify({'error':'Username already taken'}), 409
    token = secrets.token_hex(32)
    c.execute('INSERT INTO sessions (token,user_id,created_at) VALUES (%s,%s,%s)', (token, uid, now))
    conn.commit(); conn.close()
    return jsonify({'token': token, 'user': {'id':uid,'username':username,'avatar_color':color,'avatar_img':None,'bio':'','status_text':'','theme':'default'}})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username','').strip().lower()
    pw_hash = hashlib.sha256(data.get('password','').encode()).hexdigest()
    conn = get_db(); c = D(conn)
    c.execute('SELECT * FROM users WHERE username=%s AND password_hash=%s', (username, pw_hash))
    user = c.fetchone()
    if not user: conn.close(); return jsonify({'error':'Invalid credentials'}), 401
    token = secrets.token_hex(32); now = int(time.time())
    c.execute('INSERT INTO sessions (token,user_id,created_at) VALUES (%s,%s,%s)', (token, user['id'], now))
    c.execute('UPDATE users SET last_seen=%s WHERE id=%s', (now, user['id']))
    conn.commit(); conn.close()
    return jsonify({'token': token, 'user': {k:user[k] for k in ['id','username','avatar_color','avatar_img','bio','status_text','theme']}})

@app.route('/api/me', methods=['PATCH'])
def update_me():
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    data = request.json
    fields, vals = [], []
    for k in ['bio','status_text','theme']:
        if k in data: fields.append(f'{k}=%s'); vals.append(str(data[k])[:200])
    if not fields: return jsonify({'error':'Nothing to update'}), 400
    vals.append(user['id'])
    conn = get_db(); c = D(conn)
    c.execute(f'UPDATE users SET {",".join(fields)} WHERE id=%s RETURNING *', vals)
    updated = dict(c.fetchone()); conn.commit(); conn.close()
    return jsonify({k:updated[k] for k in ['id','username','avatar_color','avatar_img','bio','status_text','theme']})

@app.route('/api/me/avatar', methods=['POST'])
def upload_avatar():
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    img = request.json.get('image','')
    if not img: return jsonify({'error':'No image'}), 400
    if len(img) > 1_400_000: return jsonify({'error':'Image too large'}), 400
    conn = get_db(); c = conn.cursor()
    c.execute('UPDATE users SET avatar_img=%s WHERE id=%s', (img, user['id']))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'avatar_img':img})

@app.route('/api/me/avatar', methods=['DELETE'])
def remove_avatar():
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); c = conn.cursor()
    c.execute('UPDATE users SET avatar_img=NULL WHERE id=%s', (user['id'],))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/conversations', methods=['GET'])
def get_conversations():
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    now = int(time.time())
    conn = get_db(); c = D(conn)
    c.execute('UPDATE users SET last_seen=%s WHERE id=%s', (now, user['id']))
    c.execute('''
        SELECT c.id, c.name, c.is_group, c.created_at, c.pinned_message_id,
               (SELECT content FROM messages WHERE conversation_id=c.id AND deleted=FALSE ORDER BY sent_at DESC LIMIT 1) as last_message,
               (SELECT sent_at FROM messages WHERE conversation_id=c.id ORDER BY sent_at DESC LIMIT 1) as last_message_time,
               cm.last_read_at,
               (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id AND m.sent_at > cm.last_read_at AND m.sender_id != %s AND m.deleted=FALSE) as unread_count
        FROM conversations c
        JOIN conversation_members cm ON c.id=cm.conversation_id AND cm.user_id=%s
        ORDER BY last_message_time DESC NULLS LAST
    ''', (user['id'], user['id']))
    convos = []
    for row in c.fetchall():
        row = dict(row)
        c2 = D(conn)
        c2.execute('''SELECT u.id,u.username,u.avatar_color,u.avatar_img,u.last_seen,u.status_text FROM users u
            JOIN conversation_members cm ON u.id=cm.user_id WHERE cm.conversation_id=%s''', (row['id'],))
        members = [dict(m) for m in c2.fetchall()]
        row['members'] = [{k:v for k,v in m.items() if k != 'avatar_img'} for m in members]
        if not row['is_group']:
            other = next((m for m in members if m['id'] != user['id']), None)
            if other:
                row['display_name'] = other['username']
                row['avatar_color'] = other['avatar_color']
                row['avatar_img'] = other['avatar_img']
                row['other_last_seen'] = other['last_seen']
                row['other_online'] = online(other['last_seen'])
                row['other_status'] = other['status_text']
        else:
            row['display_name'] = row['name']; row['avatar_img'] = None
            row['other_online'] = any(online(m['last_seen']) and m['id'] != user['id'] for m in members)
        convos.append(row)
    conn.commit(); conn.close()
    return jsonify(convos)

@app.route('/api/conversations', methods=['POST'])
def create_conversation():
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    data = request.json; now = int(time.time())
    conn = get_db(); c = D(conn)
    uname = data.get('username','').strip().lower()
    if uname:
        c.execute('SELECT id FROM users WHERE username=%s', (uname,))
        target = c.fetchone()
        if not target: conn.close(); return jsonify({'error':'User not found'}), 404
        c.execute('''SELECT c.id FROM conversations c
            JOIN conversation_members cm1 ON c.id=cm1.conversation_id AND cm1.user_id=%s
            JOIN conversation_members cm2 ON c.id=cm2.conversation_id AND cm2.user_id=%s
            WHERE c.is_group=FALSE''', (user['id'], target['id']))
        ex = c.fetchone()
        if ex: conn.close(); return jsonify({'id':ex['id'],'existing':True})
        c.execute('INSERT INTO conversations (name,is_group,created_at) VALUES (%s,FALSE,%s) RETURNING id', ('', now))
        cid = c.fetchone()['id']
        c.execute('INSERT INTO conversation_members (conversation_id,user_id) VALUES (%s,%s)', (cid, user['id']))
        c.execute('INSERT INTO conversation_members (conversation_id,user_id) VALUES (%s,%s)', (cid, target['id']))
        conn.commit(); conn.close(); return jsonify({'id':cid})
    gname = data.get('group_name','').strip()
    if gname:
        c.execute('INSERT INTO conversations (name,is_group,created_at) VALUES (%s,TRUE,%s) RETURNING id', (gname, now))
        cid = c.fetchone()['id']
        c.execute('INSERT INTO conversation_members (conversation_id,user_id) VALUES (%s,%s)', (cid, user['id']))
        for un in data.get('members',[]):
            c.execute('SELECT id FROM users WHERE username=%s', (un.strip().lower(),))
            u2 = c.fetchone()
            if u2 and u2['id'] != user['id']:
                c.execute('INSERT INTO conversation_members (conversation_id,user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING', (cid, u2['id']))
        conn.commit(); conn.close(); return jsonify({'id':cid})
    conn.close(); return jsonify({'error':'Provide username or group_name'}), 400

@app.route('/api/messages/<int:cid>', methods=['GET'])
def get_messages(cid):
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); c = D(conn)
    c.execute('SELECT 1 FROM conversation_members WHERE conversation_id=%s AND user_id=%s', (cid, user['id']))
    if not c.fetchone(): conn.close(); return jsonify({'error':'Forbidden'}), 403
    # Mark as read
    now = int(time.time())
    c.execute('UPDATE conversation_members SET last_read_at=%s WHERE conversation_id=%s AND user_id=%s', (now, cid, user['id']))
    c.execute('''SELECT m.*,u.username,u.avatar_color,u.avatar_img,
        (SELECT json_agg(json_build_object('emoji',r.emoji,'user_id',r.user_id,'username',u2.username))
         FROM reactions r JOIN users u2 ON r.user_id=u2.id WHERE r.message_id=m.id) as reactions
        FROM messages m JOIN users u ON m.sender_id=u.id
        WHERE m.conversation_id=%s ORDER BY m.sent_at ASC''', (cid,))
    messages = [msg_dict(r) for r in c.fetchall()]
    # Get pinned message
    c.execute('SELECT pinned_message_id FROM conversations WHERE id=%s', (cid,))
    row = c.fetchone()
    pinned_id = row['pinned_message_id'] if row else None
    conn.commit(); conn.close()
    return jsonify({'messages': messages, 'pinned_message_id': pinned_id})

@app.route('/api/messages/<int:cid>', methods=['POST'])
def send_message(cid):
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    data = request.json or {}
    content = data.get('content','').strip()
    msg_type = data.get('msg_type','text')
    if not content: return jsonify({'error':'Empty message'}), 400
    conn = get_db(); c = D(conn)
    c.execute('SELECT 1 FROM conversation_members WHERE conversation_id=%s AND user_id=%s', (cid, user['id']))
    if not c.fetchone(): conn.close(); return jsonify({'error':'Forbidden'}), 403
    now = int(time.time())
    c.execute('UPDATE users SET last_seen=%s WHERE id=%s', (now, user['id']))
    c.execute('UPDATE conversation_members SET last_read_at=%s WHERE conversation_id=%s AND user_id=%s', (now, cid, user['id']))
    c.execute('INSERT INTO messages (conversation_id,sender_id,content,msg_type,sent_at) VALUES (%s,%s,%s,%s,%s) RETURNING id',
              (cid, user['id'], content, msg_type, now))
    mid = c.fetchone()['id']
    conn.commit()
    c.execute('''SELECT m.*,u.username,u.avatar_color,u.avatar_img,NULL as reactions
        FROM messages m JOIN users u ON m.sender_id=u.id WHERE m.id=%s''', (mid,))
    msg = msg_dict(c.fetchone()); conn.close()
    return jsonify(msg)

@app.route('/api/messages/<int:mid>', methods=['PATCH'])
def edit_message(mid):
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    content = (request.json or {}).get('content','').strip()
    if not content: return jsonify({'error':'Empty'}), 400
    conn = get_db(); c = D(conn)
    c.execute('SELECT * FROM messages WHERE id=%s AND sender_id=%s', (mid, user['id']))
    msg = c.fetchone()
    if not msg: conn.close(); return jsonify({'error':'Not found or not yours'}), 403
    c.execute('UPDATE messages SET content=%s, edited=TRUE WHERE id=%s', (content, mid))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'content':content,'edited':True})

@app.route('/api/messages/<int:mid>', methods=['DELETE'])
def delete_message(mid):
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    conn = get_db(); c = D(conn)
    c.execute('SELECT * FROM messages WHERE id=%s AND sender_id=%s', (mid, user['id']))
    if not c.fetchone(): conn.close(); return jsonify({'error':'Not found or not yours'}), 403
    c.execute('UPDATE messages SET deleted=TRUE WHERE id=%s', (mid,))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/messages/<int:mid>/react', methods=['POST'])
def react(mid):
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    emoji = (request.json or {}).get('emoji','')
    if not emoji: return jsonify({'error':'No emoji'}), 400
    conn = get_db(); c = D(conn)
    # Toggle: if same emoji exists remove it, else upsert
    c.execute('SELECT emoji FROM reactions WHERE message_id=%s AND user_id=%s', (mid, user['id']))
    existing = c.fetchone()
    if existing and existing['emoji'] == emoji:
        c.execute('DELETE FROM reactions WHERE message_id=%s AND user_id=%s', (mid, user['id']))
    else:
        c.execute('INSERT INTO reactions (message_id,user_id,emoji) VALUES (%s,%s,%s) ON CONFLICT (message_id,user_id) DO UPDATE SET emoji=%s',
                  (mid, user['id'], emoji, emoji))
    # Return updated reactions
    c.execute('''SELECT json_agg(json_build_object('emoji',r.emoji,'user_id',r.user_id,'username',u.username)) as reactions
        FROM reactions r JOIN users u ON r.user_id=u.id WHERE r.message_id=%s''', (mid,))
    row = c.fetchone(); conn.commit(); conn.close()
    return jsonify({'ok':True,'reactions': row['reactions'] or []})

@app.route('/api/conversations/<int:cid>/pin', methods=['POST'])
def pin_message(cid):
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    mid = (request.json or {}).get('message_id')
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT 1 FROM conversation_members WHERE conversation_id=%s AND user_id=%s', (cid, user['id']))
    if not c.fetchone(): conn.close(); return jsonify({'error':'Forbidden'}), 403
    c.execute('UPDATE conversations SET pinned_message_id=%s WHERE id=%s', (mid, cid))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'pinned_message_id':mid})

@app.route('/api/users/search')
def search_users():
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    q = request.args.get('q','').strip()
    if not q: return jsonify([])
    conn = get_db(); c = D(conn)
    c.execute('SELECT id,username,avatar_color,avatar_img,last_seen,status_text FROM users WHERE username ILIKE %s AND id!=%s LIMIT 10',
              (f'%{q}%', user['id']))
    users = [dict(r) for r in c.fetchall()]
    for u in users: u['online'] = online(u['last_seen'])
    conn.close(); return jsonify(users)

@app.route('/api/poll/<int:cid>')
def poll(cid):
    user = get_user_from_token(tok())
    if not user: return jsonify({'error':'Unauthorized'}), 401
    after = int(request.args.get('after', 0))
    conn = get_db(); c = D(conn)
    now = int(time.time())
    c.execute('UPDATE users SET last_seen=%s WHERE id=%s', (now, user['id']))
    # Mark read if active
    c.execute('UPDATE conversation_members SET last_read_at=%s WHERE conversation_id=%s AND user_id=%s', (now, cid, user['id']))
    c.execute('''SELECT m.*,u.username,u.avatar_color,u.avatar_img,
        (SELECT json_agg(json_build_object('emoji',r.emoji,'user_id',r.user_id,'username',u2.username))
         FROM reactions r JOIN users u2 ON r.user_id=u2.id WHERE r.message_id=m.id) as reactions
        FROM messages m JOIN users u ON m.sender_id=u.id
        WHERE m.conversation_id=%s AND m.sent_at>%s ORDER BY m.sent_at ASC''', (cid, after))
    messages = [msg_dict(r) for r in c.fetchall()]
    c.execute('''SELECT u.id,u.username,u.last_seen,u.status_text FROM users u
        JOIN conversation_members cm ON u.id=cm.user_id WHERE cm.conversation_id=%s AND u.id!=%s''', (cid, user['id']))
    members = [{'id':r['id'],'username':r['username'],'online':online(r['last_seen']),'status_text':r['status_text']} for r in c.fetchall()]
    # Unread counts for all convos (for badge updates)
    c.execute('''SELECT cm.conversation_id,
        COUNT(m.id) FILTER (WHERE m.sent_at > cm.last_read_at AND m.sender_id != %s AND m.deleted=FALSE) as unread
        FROM conversation_members cm
        LEFT JOIN messages m ON m.conversation_id=cm.conversation_id
        WHERE cm.user_id=%s GROUP BY cm.conversation_id''', (user['id'], user['id']))
    unread_map = {r['conversation_id']: r['unread'] for r in c.fetchall()}
    conn.commit(); conn.close()
    return jsonify({'messages': messages, 'members': members, 'unread_map': unread_map})

init_db()
if __name__ == '__main__':
    app.run(debug=True, port=5000)
