import sqlite3
import os
from datetime import datetime

# CONFIGURACIÓN DE LA BASE DE DATOS
if os.environ.get('RENDER'):
    # Ruta para el disco persistente en Render
    BASE_DIR = "/opt/render/project/data"
    os.makedirs(BASE_DIR, exist_ok=True)
    DB_NAME = os.path.join(BASE_DIR, "casa_apuestas.db")
else:
    # Ruta local
    DB_NAME = "casa_apuestas.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Tabla Usuarios
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Tabla Transacciones
    # Nota: photo_path ya no se usará para guardar archivos en disco, 
    # pero la dejamos en la DB por si quieres guardar un ID o referencia futura.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            amount REAL,
            status TEXT DEFAULT 'PENDING',
            account_info TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Tabla Eventos
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            odds_local REAL,
            odds_draw REAL,
            odds_away REAL,
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    
    # Tabla Apuestas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event_id INTEGER,
            selection TEXT,
            odds REAL,
            amount REAL,
            potential_win REAL,
            status TEXT DEFAULT 'PENDING'
        )
    ''')

    conn.commit()
    conn.close()

# --- FUNCIONES DE USUARIO ---
def register_or_update_user(user_id, username, first_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)', (user_id, username, first_name))
    # Actualizar datos si el usuario ya existe
    cursor.execute('UPDATE users SET username=?, first_name=? WHERE user_id=?', (username, first_name, user_id))
    conn.commit()
    conn.close()

def get_user_balance(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row['balance'] if row else 0.0

def update_user_balance(user_id, amount):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

# --- FUNCIONES DE TRANSACCIONES ---
def create_transaction(user_id, t_type, amount, account_info=None, photo_path=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    # Pasamos photo_path como None ya que no guardamos archivo
    cursor.execute('INSERT INTO transactions (user_id, type, amount, account_info) VALUES (?, ?, ?, ?)', 
                   (user_id, t_type, amount, account_info))
    conn.commit()
    trans_id = cursor.lastrowid
    conn.close()
    return trans_id

def update_transaction_status(trans_id, status):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE transactions SET status = ? WHERE id = ?', (status, trans_id))
    conn.commit()
    conn.close()

def get_transaction(trans_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM transactions WHERE id = ?', (trans_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

# --- FUNCIONES DE EVENTOS Y APUESTAS ---
def create_event(name, o_local, o_draw, o_away):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO events (name, odds_local, odds_draw, odds_away) VALUES (?, ?, ?, ?)', 
                   (name, o_local, o_draw, o_away))
    conn.commit()
    conn.close()

def get_active_events():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM events WHERE is_active = 1')
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def place_bet(user_id, event_id, selection, odds, amount, potential_win):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, user_id))
        cursor.execute('INSERT INTO bets (user_id, event_id, selection, odds, amount, potential_win) VALUES (?, ?, ?, ?, ?, ?)', 
                       (user_id, event_id, selection, odds, amount, potential_win))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Error placing bet: {e}")
        return False
    finally:
        conn.close()

# Inicializar DB
init_db()
