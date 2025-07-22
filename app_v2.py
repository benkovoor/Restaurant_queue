import sqlite3
import os
import datetime
import requests
from flask import Flask, request, render_template, redirect, url_for, Response
from dotenv import load_dotenv
from flask_cors import CORS

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
CORS(app)

# --- Admin Dashboard & App Configuration ---
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "supersecret")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
API_URL = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"

# --- Global State Variables ---
user_states = {}
AUTO_ALLOCATOR_ENABLED = True

# ======================================================================
# --- DATABASE FUNCTIONS ---
# ======================================================================

def get_db_connection():
    """Creates a database connection with a row factory for dict-like access."""
    conn = sqlite3.connect("users.db", detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database and ensures tables have the necessary columns."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL UNIQUE,
            name TEXT,
            people_count INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_number TEXT NOT NULL UNIQUE,
            capacity INTEGER NOT NULL,
            status TEXT DEFAULT 'free',
            occupied_by_user_id INTEGER,
            occupied_timestamp DATETIME,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (occupied_by_user_id) REFERENCES users(id) ON DELETE SET NULL
        )""")
    
    # Add columns to store seated customer info directly on the table.
    try:
        cursor.execute("ALTER TABLE tables ADD COLUMN customer_name TEXT;")
    except sqlite3.OperationalError:
        pass # Column already exists
    try:
        cursor.execute("ALTER TABLE tables ADD COLUMN people_count INTEGER;")
    except sqlite3.OperationalError:
        pass # Column already exists
    try: # ADDED: Column for customer phone number
        cursor.execute("ALTER TABLE tables ADD COLUMN customer_phone_number TEXT;")
    except sqlite3.OperationalError:
        pass # Column already exists

    conn.commit()

    initial_tables_config = [
        ("T1", 2), ("T2", 2), ("T3", 2), ("T4", 2),
        ("T5", 4), ("T6", 4), ("T7", 4), ("T8", 4),
        ("T9", 6), ("T10", 6)
    ]
    for table_num, capacity in initial_tables_config:
        cursor.execute("INSERT OR IGNORE INTO tables (table_number, capacity, status) VALUES (?, ?, 'free')", (table_num, capacity))
    
    conn.commit()
    conn.close()
    print("Database 'users.db' initialized.")

# ======================================================================
# --- WHATSAPP & CORE LOGIC FUNCTIONS ---
# ======================================================================

def send_message(to, text):
    """Sends a WhatsApp message."""
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        response = requests.post(API_URL, json=payload, headers=headers)
        response.raise_for_status()
        print(f"OUTGOING to {to}: {text}")
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"FAILED sending message to {to}: {e}")
        return {"error": str(e)}

def save_user_data_to_db(phone_number, name, people_count):
    """Saves or updates a user in the waiting queue."""
    conn = get_db_connection()
    try:
        existing_user = conn.execute("SELECT id FROM users WHERE phone_number = ?", (phone_number,)).fetchone()
        if existing_user:
            conn.execute("UPDATE users SET name = ?, people_count = ?, timestamp = ? WHERE id = ?", 
                         (name, people_count, datetime.datetime.now(), existing_user['id']))
            user_id = existing_user['id']
        else:
            cursor = conn.execute("INSERT INTO users (phone_number, name, people_count, timestamp) VALUES (?, ?, ?, ?)",
                                  (phone_number, name, people_count, datetime.datetime.now()))
            user_id = cursor.lastrowid
        conn.commit()
        return user_id
    except sqlite3.Error as e:
        print(f"Database error in save_user_data_to_db: {e}")
        return None
    finally:
        conn.close()

def update_table_status_to_free(table_number):
    """Marks a table as free and clears customer data."""
    conn = get_db_connection()
    try:
        cursor = conn.execute("""
            UPDATE tables SET 
                status = 'free', 
                occupied_by_user_id = NULL, 
                occupied_timestamp = NULL,
                customer_name = NULL,
                people_count = NULL,
                customer_phone_number = NULL
            WHERE table_number = ?
        """, (table_number,))
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        print(f"Database error in update_table_status_to_free: {e}")
        return False
    finally:
        conn.close()

def seat_customer(customer_id, table_id, table_number, customer_phone_number, customer_name, people_count):
    """Assigns a customer to a table, notifies them, and removes them from the queue."""
    conn = get_db_connection()
    try:
        conn.execute("""
            UPDATE tables SET 
                status = 'occupied', 
                occupied_by_user_id = ?, 
                occupied_timestamp = ?,
                customer_name = ?,
                people_count = ?,
                customer_phone_number = ?
            WHERE id = ?
        """, (customer_id, datetime.datetime.now(), customer_name, people_count, customer_phone_number, table_id))
        
        conn.execute("DELETE FROM users WHERE id = ?", (customer_id,))
        conn.commit()
        
        send_message(customer_phone_number, f"Great news, {customer_name}! Your table ({table_number}) is ready. Please proceed to the host.")
        print(f"SEATED: {customer_name} ({people_count}p) at Table {table_number}")
    except sqlite3.Error as e:
        print(f"Database error in seat_customer: {e}")
    finally:
        conn.close()

def attempt_seating_allocation():
    """The core auto-allocator logic."""
    if not AUTO_ALLOCATOR_ENABLED:
        print("Auto-allocator is disabled. Skipping allocation run.")
        return

    print("Running auto-allocator...")
    waiting_customers = get_waiting_customers()
    free_tables = get_free_tables()
    
    if not waiting_customers or not free_tables:
        print("No customers waiting or no free tables. Allocation ends.")
        return

    seated_customer_ids = set()
    occupied_table_ids = set()

    for customer in waiting_customers:
        if customer['id'] in seated_customer_ids:
            continue

        best_fit_table = None
        min_waste = float('inf')

        for table in free_tables:
            if table['id'] in occupied_table_ids:
                continue
            
            if table['capacity'] >= customer['people_count']:
                waste = table['capacity'] - customer['people_count']
                if waste < min_waste:
                    min_waste = waste
                    best_fit_table = table
        
        if best_fit_table:
            seat_customer(
                customer['id'],
                best_fit_table['id'],
                best_fit_table['table_number'],
                customer['phone_number'],
                customer['name'],
                customer['people_count']
            )
            seated_customer_ids.add(customer['id'])
            occupied_table_ids.add(best_fit_table['id'])
            
            attempt_seating_allocation()
            return

# ======================================================================
# --- ADMIN DASHBOARD HELPER FUNCTIONS ---
# ======================================================================

def get_waiting_customers():
    """Gets all customers from the waiting queue, ordered by time."""
    conn = get_db_connection()
    customers = conn.execute("SELECT id, phone_number, name, people_count, timestamp FROM users ORDER BY timestamp ASC").fetchall()
    conn.close()
    return customers

def get_free_tables():
    """Gets all tables with status 'free'."""
    conn = get_db_connection()
    tables = conn.execute("SELECT id, table_number, capacity FROM tables WHERE status = 'free' ORDER BY capacity ASC").fetchall()
    conn.close()
    return tables

def get_all_tables():
    """Gets all tables regardless of status for the overview grid."""
    conn = get_db_connection()
    tables = conn.execute("SELECT id, table_number, capacity, status FROM tables ORDER BY CAST(SUBSTR(table_number, 2) AS INTEGER)").fetchall()
    conn.close()
    return tables

def get_occupied_tables():
    """Gets details of all occupied tables and who is seated there."""
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT table_number, occupied_timestamp, customer_name, people_count, customer_phone_number
        FROM tables 
        WHERE status = 'occupied' 
        ORDER BY occupied_timestamp DESC
    """).fetchall()
    conn.close()

    occupied = []
    for row in rows:
        row_dict = dict(row)
        if row_dict['occupied_timestamp'] and isinstance(row_dict['occupied_timestamp'], str):
            try:
                if '.' in row_dict['occupied_timestamp']:
                    row_dict['occupied_timestamp'] = datetime.datetime.strptime(row_dict['occupied_timestamp'], '%Y-%m-%d %H:%M:%S.%f')
                else:
                    row_dict['occupied_timestamp'] = datetime.datetime.strptime(row_dict['occupied_timestamp'], '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                row_dict['occupied_timestamp'] = None
        occupied.append(row_dict)
        
    return occupied

def remove_user_from_queue(customer_id):
    """Removes a user from the waiting queue manually."""
    conn = get_db_connection()
    conn.execute("DELETE FROM users WHERE id = ?", (customer_id,))
    conn.commit()
    conn.close()

def get_user_details(user_id):
    """Gets a specific user's details from the queue."""
    conn = get_db_connection()
    user = conn.execute("SELECT phone_number, name, people_count FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user

def get_table_details(table_id):
    """Gets a specific table's details."""
    conn = get_db_connection()
    table = conn.execute("SELECT table_number FROM tables WHERE id = ?", (table_id,)).fetchone()
    conn.close()
    return table

# --- Basic Auth Functions ---
def check_auth(username, password):
    """Checks if a username/password combination is valid."""
    return username == ADMIN_USER and password == ADMIN_PASSWORD

def authenticate():
    """Sends a 401 response that enables basic auth."""
    return Response('Login Required', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})

# ======================================================================
# --- FLASK ROUTES ---
# ======================================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if data and data.get("object") == "whatsapp_business_account":
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if change.get("field") == "messages" and value.get("messages"):
                    msg = value["messages"][0]
                    sender, msg_type = msg["from"], msg.get("type")
                    print(f"INCOMING from {sender}: {msg.get('text', {}).get('body', '[non-text message]')}")

                    if msg_type != "text":
                        send_message(sender, "Sorry, I can only process text messages.")
                        return "EVENT_RECEIVED", 200
                    
                    content = msg["text"]["body"].lower().strip()
                    user_state = user_states.setdefault(sender, {"state": "initial"})

                    if user_state["state"] == "initial":
                        if content == "hi":
                            send_message(sender, "Welcome! Please reply with your name and the number of people in your party, separated by a comma (e.g., Alex, 4)")
                            user_state["state"] = "awaiting_details"
                        else:
                            send_message(sender, "Please say 'hi' to join the waiting list.")
                    
                    elif user_state["state"] == "awaiting_details":
                        try:
                            name, people_count_str = map(str.strip, msg["text"]["body"].split(","))
                            people_count = int(people_count_str)
                            if 1 <= people_count <= 10:
                                if save_user_data_to_db(sender, name.title(), people_count):
                                    send_message(sender, f"Thank you, {name.title()}! You're in the queue for a party of {people_count}. We'll message you when your table is ready.")
                                    user_states.pop(sender, None) # Reset state
                                    attempt_seating_allocation()
                                else:
                                    send_message(sender, "There was an error saving your details. Please try again.")
                            else:
                                send_message(sender, "Sorry, we can only accommodate parties of 1 to 10. Please provide a valid number.")
                        except (ValueError, IndexError):
                            send_message(sender, "Please use the correct format: Name, Number (e.g., Alex, 4)")
    return "EVENT_RECEIVED", 200

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification failed", 403

# --- ADMIN DASHBOARD ROUTES ---
@app.route('/admin')
def admin_dashboard():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    return render_template('admin.html',
        customers=get_waiting_customers(),
        all_tables=get_all_tables(),
        free_tables=get_free_tables(),
        occupied_tables=get_occupied_tables(),
        auto_allocator_status='ON' if AUTO_ALLOCATOR_ENABLED else 'OFF'
    )

@app.route('/admin/toggle_auto_allocator', methods=['POST'])
def toggle_auto_allocator():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    global AUTO_ALLOCATOR_ENABLED
    AUTO_ALLOCATOR_ENABLED = not AUTO_ALLOCATOR_ENABLED
    print(f"Auto-allocator status toggled to: {'ON' if AUTO_ALLOCATOR_ENABLED else 'OFF'}")

    if AUTO_ALLOCATOR_ENABLED:
        attempt_seating_allocation()
        
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/free_table', methods=['POST'])
def free_table():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    if update_table_status_to_free(request.form.get('table_number')):
        attempt_seating_allocation()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/remove_customer', methods=['POST'])
def remove_customer():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    remove_user_from_queue(request.form.get('customer_id'))
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/run_auto_seat', methods=['POST'])
def run_auto_seat():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    attempt_seating_allocation()
    return redirect(url_for('admin_dashboard'))
    
@app.route('/admin/seat_manually', methods=['POST'])
def seat_manually():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
    customer_id = request.form.get('customer_id')
    table_id = request.form.get('table_id')

    if customer_id and table_id:
        customer = get_user_details(customer_id)
        table = get_table_details(table_id)
        if customer and table:
            seat_customer(
                int(customer_id), 
                int(table_id), 
                table['table_number'], 
                customer['phone_number'], 
                customer['name'],
                customer['people_count']
            )
    return redirect(url_for('admin_dashboard'))

if __name__ == "__main__":
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
