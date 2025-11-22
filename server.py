import asyncio
import json
import sqlite3
import hashlib
from datetime import datetime

# You need to install this: pip install websockets
try:
    import websockets
except ImportError:
    print("ERROR: Missing 'websockets' library.")
    print("Run: pip install websockets")
    exit(1)

# --- Configuration ---
HOST = "0.0.0.0" 
PORT = 8765        
DB_NAME = "webchat_users.db"

# --- Global State ---
connected_clients = set() 
active_users = {}         
user_history = {}         

# --- Database Setup ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Note: contact is NOT unique in schema definition to avoid migration errors for existing DBs,
    # but we enforce uniqueness in Python code.
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            contact TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def register_user(username, password, contact):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. Check if Phone/Contact already exists
    c.execute("SELECT username FROM users WHERE contact=?", (contact,))
    if c.fetchone():
        conn.close()
        return False, "Phone number already registered."

    # 2. Register
    hashed_pw = hashlib.sha256(password.encode()).hexdigest()
    try:
        c.execute("INSERT INTO users (username, password, contact) VALUES (?, ?, ?)", 
                  (username, hashed_pw, contact))
        conn.commit()
        return True, "Account created!"
    except sqlite3.IntegrityError:
        return False, "Username taken."
    finally:
        conn.close()

def verify_user(username, password):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    hashed_pw = hashlib.sha256(password.encode()).hexdigest()
    c.execute("SELECT * FROM users WHERE username=? AND password=?", (username, hashed_pw))
    user = c.fetchone()
    conn.close()
    return user is not None

def get_user_details(username):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT contact FROM users WHERE username=?", (username,))
    result = c.fetchone()
    conn.close()
    return {'contact': result[0]} if result else None

def update_username_in_db(old_name, new_name):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        c.execute("UPDATE users SET username=? WHERE username=?", (new_name, old_name))
        if c.rowcount == 0:
            return False, "User not found"
        conn.commit()
        return True, "Username updated"
    except sqlite3.IntegrityError:
        return False, "Username already exists"
    finally:
        conn.close()

# --- Messaging Helpers ---
async def broadcast_user_list():
    all_users = set(active_users.keys()) | set(user_history.keys())
    user_list = []
    for user in all_users:
        status = "Online" if user in active_users else "Offline"
        last_seen = "Now" if user in active_users else user_history.get(user, "Unknown")
        user_list.append({"username": user, "status": status, "last_seen": last_seen})
    
    user_list.sort(key=lambda x: (x['status'] != 'Online', x['username']))
    message = json.dumps({"type": "user_list", "users": user_list})
    
    if connected_clients:
        await asyncio.gather(*[client.send(message) for client in connected_clients if not client.closed])

async def broadcast_message(message_dict):
    """Broadcasts to ALL connected clients (including sender)."""
    msg_json = json.dumps(message_dict)
    if connected_clients:
        targets = [c for c in connected_clients if not c.closed]
        if targets:
            await asyncio.gather(*[client.send(msg_json) for client in targets])

# --- Main Handler ---
async def handler(websocket):
    print(f"[NEW CONN] Client connected from {websocket.remote_address}")
    connected_clients.add(websocket)
    current_username = None
    
    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")
            
            # --- AUTH ---
            if msg_type == "register":
                success, msg = register_user(data['username'], data['password'], data['contact'])
                response = {
                    "type": "auth_result",
                    "status": "success" if success else "fail",
                    "msg": msg
                }
                await websocket.send(json.dumps(response))

            elif msg_type == "login":
                username = data['username']
                if username in active_users:
                     await websocket.send(json.dumps({"type": "auth_result", "status": "fail", "msg": "User already active."}))
                elif verify_user(username, data['password']):
                    current_username = username
                    active_users[username] = websocket
                    
                    # Send Login Success + Profile Info
                    details = get_user_details(username)
                    await websocket.send(json.dumps({
                        "type": "auth_result", 
                        "status": "success", 
                        "username": username,
                        "contact": details['contact'],
                        "msg": "Login successful"
                    }))
                    
                    print(f"[LOGIN] {username}")
                    await broadcast_message({"type": "system", "content": f"{username} joined."})
                    await broadcast_user_list()
                else:
                    await websocket.send(json.dumps({"type": "auth_result", "status": "fail", "msg": "Invalid credentials."}))

            # --- FEATURES (Authenticated) ---
            elif current_username:
                
                if msg_type == "update_profile":
                    new_name = data.get('new_username')
                    if new_name:
                        success, db_msg = update_username_in_db(current_username, new_name)
                        if success:
                            # Update memory maps
                            ws = active_users.pop(current_username)
                            active_users[new_name] = ws
                            current_username = new_name
                            
                            # Notify User
                            await websocket.send(json.dumps({
                                "type": "profile_updated",
                                "status": "success",
                                "new_username": new_name,
                                "msg": "Username changed successfully."
                            }))
                            # Refresh lists for everyone
                            await broadcast_user_list()
                        else:
                            await websocket.send(json.dumps({"type": "profile_updated", "status": "fail", "msg": db_msg}))

                elif msg_type == "typing":
                    target = data.get('target')
                    if target in active_users:
                        await active_users[target].send(json.dumps({"type": "typing", "sender": current_username}))
                    continue

                elif msg_type == "read_receipt":
                    target = data.get('target')
                    if target in active_users:
                        await active_users[target].send(json.dumps({"type": "read_receipt", "sender": current_username}))
                    continue

                elif msg_type == "share_contact":
                    details = get_user_details(current_username)
                    data['type'] = 'contact_card'
                    data['content'] = details['contact']

                # --- STANDARD MESSAGING ---
                data['sender'] = current_username
                data['timestamp'] = datetime.now().strftime("%I:%M %p")
                
                target = data.get('target', 'Group Chat')
                
                if target == "Group Chat":
                    data['isPublic'] = True
                    await broadcast_message(data)
                else:
                    data['isPublic'] = False
                    # Send to Recipient
                    if target in active_users:
                        await active_users[target].send(json.dumps(data))
                    
                    # Send Confirmation to Sender (FIX for "message not showing")
                    # We send it back to the sender explicitly so their UI can render it from the server confirmation
                    await websocket.send(json.dumps(data))

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"Error: {e}")
    finally:
        connected_clients.discard(websocket)
        if current_username and current_username in active_users:
            del active_users[current_username]
            user_history[current_username] = datetime.now().strftime("%I:%M %p")
            print(f"[DISCONNECT] {current_username}")
            await broadcast_message({"type": "system", "content": f"{current_username} left."})
            await broadcast_user_list()

async def main():
    init_db()
    print(f"Starting WebSocket Server on ws://{HOST}:{PORT}")
    async with websockets.serve(handler, HOST, PORT) as server:
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())