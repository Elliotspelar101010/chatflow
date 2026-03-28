from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os, time, hashlib, secrets
import psycopg2, psycopg2.extras

app = Flask(__name__, static_folder='static')
CORS(app)

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, avatar_color TEXT NOT NULL,
        created_at BIGINT NOT NULL, last_seen BIGINT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id SERIAL PRIMARY KEY, name TEXT, is_group BOOLEAN DEFAULT FALSE,
        created_at BIGINT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS conversation_members (
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        PRIMARY KEY (conversation_id, user_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        content TEXT NOT NULL, sent_at BIGINT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at BIGINT NOT NULL)''')
    conn.commit()
    conn.close()

def get_user_from_token(token):
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute('SELECT u.* FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token=%s', (token,))
    user = c.fetchone()
    conn.close()
    return user

def tok():
    return request.headers.get('Authorization', '').replace('Bearer ', '')

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/ping')
def ping():
    return jsonify({'ok': True})

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').strip().lower()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    if len(username) < 3:
        return jsonify({'error': 'Username must be at least 3 characters'}), 400
    colors = ['#FF6B6B','#4ECDC4','#45B7D1','#96CEB4','#DDA0DD','#98D8C8','#F7DC6F','#BB8FCE','#6c63ff','#f59e0b','#10b981','#3b82f6']
    color = colors[len(username) % len(colors)]
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    now = int(time.time())
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c.execute('INSERT INTO users (username,password_hash,avatar_color,created_at,last_seen) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                  (username, pw_hash, color, now, now))
        user_id = c.fetchone()['id']
    except psycopg2.errors.UniqueViolation:
        conn.rollback(); conn.close()
        return jsonify({'error': 'Username already taken'}), 409
    token = secrets.token_hex(32)
    c.execute('INSERT INTO sessions (token,user_id,created_at) VALUES (%s,%s,%s)', (token, user_id, now))
    conn.commit(); conn.close()
    return jsonify({'token': token, 'user': {'id': user_id, 'username': username, 'avatar_color': color}})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip().lower()
    password = data.get('password', '')
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute('SELECT * FROM users WHERE username=%s AND password_hash=%s', (username, pw_hash))
    user = c.fetchone()
    if not user:
        conn.close(); return jsonify({'error': 'Invalid credentials'}), 401
    token = secrets.token_hex(32)
    now = int(time.time())
    c.execute('INSERT INTO sessions (token,user_id,created_at) VALUES (%s,%s,%s)', (token, user['id'], now))
    c.execute('UPDATE users SET last_seen=%s WHERE id=%s', (now, user['id']))
    conn.commit(); conn.close()
    return jsonify({'token': token, 'user': {'id': user['id'], 'username': user['username'], 'avatar_color': user['avatar_color']}})

@app.route('/api/conversations', methods=['GET'])
def get_conversations():
    user = get_user_from_token(tok())
    if not user: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute('''
        SELECT c.id, c.name, c.is_group, c.created_at,
               (SELECT content FROM messages WHERE conversation_id=c.id ORDER BY sent_at DESC LIMIT 1) as last_message,
               (SELECT sent_at FROM messages WHERE conversation_id=c.id ORDER BY sent_at DESC LIMIT 1) as last_message_time
        FROM conversations c
        JOIN conversation_members cm ON c.id=cm.conversation_id
        WHERE cm.user_id=%s ORDER BY last_message_time DESC NULLS LAST
    ''', (user['id'],))
    convos = []
    for row in c.fetchall():
        row = dict(row)
        c2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c2.execute('''SELECT u.id,u.username,u.avatar_color FROM users u
            JOIN conversation_members cm ON u.id=cm.user_id WHERE cm.conversation_id=%s''', (row['id'],))
        members = [dict(m) for m in c2.fetchall()]
        row['members'] = members
        if not row['is_group']:
            other = next((m for m in members if m['id'] != user['id']), None)
            if other:
                row['display_name'] = other['username']
                row['avatar_color'] = other['avatar_color']
        else:
            row['display_name'] = row['name']
        convos.append(row)
    conn.close()
    return jsonify(convos)

@app.route('/api/conversations', methods=['POST'])
def create_conversation():
    user = get_user_from_token(tok())
    if not user: return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    now = int(time.time())
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    target_username = data.get('username', '').strip().lower()
    if target_username:
        c.execute('SELECT id FROM users WHERE username=%s', (target_username,))
        target = c.fetchone()
        if not target:
            conn.close(); return jsonify({'error': 'User not found'}), 404
        c.execute('''SELECT c.id FROM conversations c
            JOIN conversation_members cm1 ON c.id=cm1.conversation_id AND cm1.user_id=%s
            JOIN conversation_members cm2 ON c.id=cm2.conversation_id AND cm2.user_id=%s
            WHERE c.is_group=FALSE''', (user['id'], target['id']))
        existing = c.fetchone()
        if existing:
            conn.close(); return jsonify({'id': existing['id'], 'existing': True})
        c.execute('INSERT INTO conversations (name,is_group,created_at) VALUES (%s,FALSE,%s) RETURNING id', ('', now))
        convo_id = c.fetchone()['id']
        c.execute('INSERT INTO conversation_members VALUES (%s,%s)', (convo_id, user['id']))
        c.execute('INSERT INTO conversation_members VALUES (%s,%s)', (convo_id, target['id']))
        conn.commit(); conn.close()
        return jsonify({'id': convo_id})

    group_name = data.get('group_name', '').strip()
    members_list = data.get('members', [])
    if group_name:
        c.execute('INSERT INTO conversations (name,is_group,created_at) VALUES (%s,TRUE,%s) RETURNING id', (group_name, now))
        convo_id = c.fetchone()['id']
        c.execute('INSERT INTO conversation_members VALUES (%s,%s)', (convo_id, user['id']))
        for uname in members_list:
            c.execute('SELECT id FROM users WHERE username=%s', (uname.strip().lower(),))
            u = c.fetchone()
            if u and u['id'] != user['id']:
                c.execute('INSERT INTO conversation_members VALUES (%s,%s) ON CONFLICT DO NOTHING', (convo_id, u['id']))
        conn.commit(); conn.close()
        return jsonify({'id': convo_id})

    conn.close()
    return jsonify({'error': 'Provide username or group_name'}), 400

@app.route('/api/messages/<int:convo_id>', methods=['GET'])
def get_messages(convo_id):
    user = get_user_from_token(tok())
    if not user: return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute('SELECT 1 FROM conversation_members WHERE conversation_id=%s AND user_id=%s', (convo_id, user['id']))
    if not c.fetchone():
        conn.close(); return jsonify({'error': 'Forbidden'}), 403
    c.execute('''SELECT m.*,u.username,u.avatar_color FROM messages m
        JOIN users u ON m.sender_id=u.id WHERE m.conversation_id=%s ORDER BY m.sent_at ASC''', (convo_id,))
    messages = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(messages)

@app.route('/api/messages/<int:convo_id>', methods=['POST'])
def send_message(convo_id):
    user = get_user_from_token(tok())
    if not user: return jsonify({'error': 'Unauthorized'}), 401
    content = (request.json or {}).get('content', '').strip()
    if not content: return jsonify({'error': 'Empty message'}), 400
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute('SELECT 1 FROM conversation_members WHERE conversation_id=%s AND user_id=%s', (convo_id, user['id']))
    if not c.fetchone():
        conn.close(); return jsonify({'error': 'Forbidden'}), 403
    now = int(time.time())
    c.execute('INSERT INTO messages (conversation_id,sender_id,content,sent_at) VALUES (%s,%s,%s,%s) RETURNING id',
              (convo_id, user['id'], content, now))
    msg_id = c.fetchone()['id']
    conn.commit()
    c.execute('SELECT m.*,u.username,u.avatar_color FROM messages m JOIN users u ON m.sender_id=u.id WHERE m.id=%s', (msg_id,))
    msg = dict(c.fetchone())
    conn.close()
    return jsonify(msg)

@app.route('/api/users/search')
def search_users():
    user = get_user_from_token(tok())
    if not user: return jsonify({'error': 'Unauthorized'}), 401
    q = request.args.get('q', '').strip()
    if not q: return jsonify([])
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute('SELECT id,username,avatar_color FROM users WHERE username ILIKE %s AND id!=%s LIMIT 10',
              (f'%{q}%', user['id']))
    users = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(users)

@app.route('/api/poll/<int:convo_id>')
def poll_messages(convo_id):
    user = get_user_from_token(tok())
    if not user: return jsonify({'error': 'Unauthorized'}), 401
    after = int(request.args.get('after', 0))
    conn = get_db()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute('''SELECT m.*,u.username,u.avatar_color FROM messages m JOIN users u ON m.sender_id=u.id
        WHERE m.conversation_id=%s AND m.sent_at>%s ORDER BY m.sent_at ASC''', (convo_id, after))
    messages = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(messages)

init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
