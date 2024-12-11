import os
import uuid
import asyncio
import sqlite3
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, AuthRestartError
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.functions.channels import GetFullChannelRequest
from threading import Thread
import time

# Inicialização do Flask
app = Flask(__name__)
CORS(app)

# Variáveis Globais
client = None
authenticated = False
authenticated_phone = None
api_id = None
api_hash = None
asyncio_loop = None

tasks = {}  # {task_id: {"group_name": ..., "time": ..., "image": ..., "text": ..., "status": ...}}
groups_cache = []
upload_dir = "uploads"
os.makedirs(upload_dir, exist_ok=True)
db_file = "data.db"

# Banco de Dados
def init_db():
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS login (
            id INTEGER PRIMARY KEY,
            api_id TEXT,
            api_hash TEXT,
            phone TEXT,
            session TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            group_name TEXT,
            time TEXT,
            image TEXT,
            text TEXT,
            status TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Funções Assíncronas
async def send_code_request(api_id_local, api_hash_local, phone):
    global client
    try:
        client = TelegramClient(StringSession(), int(api_id_local), api_hash_local)
        await client.connect()
        await client.send_code_request(phone)
        return True, "Código enviado com sucesso!"
    except AuthRestartError:
        client.disconnect()
        await client.connect()
        await client.send_code_request(phone)
        return True, "Autenticação reiniciada, código reenviado!"
    except Exception as e:
        return False, f"Erro ao enviar código: {e}"

async def do_authenticate(phone, code):
    global authenticated, authenticated_phone
    try:
        await client.sign_in(phone, code)
        authenticated = True
        authenticated_phone = phone

        # Salva o login no banco
        conn = sqlite3.connect(db_file)
        c = conn.cursor()
        c.execute("INSERT INTO login (api_id, api_hash, phone, session) VALUES (?, ?, ?, ?)",
                  (api_id, api_hash, phone, client.session.save()))
        conn.commit()
        conn.close()

        return True, "Autenticado com sucesso!"
    except SessionPasswordNeededError:
        return False, "Senha de dois fatores necessária!"
    except Exception as e:
        return False, f"Erro ao autenticar: {e}"

async def load_groups():
    global groups_cache
    try:
        dialogs = await client.get_dialogs()
        groups_info = []
        for dialog in dialogs:
            if dialog.is_group:
                entity = dialog.entity
                link = None
                if dialog.is_channel:
                    full = await client(GetFullChannelRequest(channel=entity))
                    invite = full.full_chat.exported_invite
                    if invite:
                        link = invite.link
                    elif hasattr(entity, 'username') and entity.username:
                        link = f"https://t.me/{entity.username}"
                else:
                    full = await client(GetFullChatRequest(chat_id=entity.id))
                    invite = full.full_chat.exported_invite
                    if invite:
                        link = invite.link

                groups_info.append({"title": dialog.title, "link": link})

        groups_cache = groups_info
        return True, groups_info
    except Exception as e:
        return False, f"Erro ao carregar grupos: {e}"

async def send_image_to_group(group_name, image_path, text=""):
    try:
        dialogs = await client.get_dialogs()
        chat = next(dialog for dialog in dialogs if dialog.is_group and dialog.title == group_name)
        await client.send_file(chat, image_path, caption=text)
    except Exception as e:
        print(f"Erro ao enviar imagem para o grupo {group_name}: {e}")

async def schedule_task(task_id, task_details):
    while tasks.get(task_id, {}).get("status") == "Rodando":
        current_time = time.strftime("%H:%M")
        if current_time == task_details["time"]:
            await send_image_to_group(task_details["group_name"], task_details["image"], task_details["text"])
            await asyncio.sleep(60)  # Espera 1 minuto antes de verificar novamente
        await asyncio.sleep(1)

# Loop Assíncrono
def start_asyncio_loop():
    global asyncio_loop
    asyncio_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(asyncio_loop)
    asyncio_loop.run_forever()

asyncio_thread = Thread(target=start_asyncio_loop, daemon=True)
asyncio_thread.start()

# Persistência do Estado
def load_state():
    global authenticated, authenticated_phone, client, api_id, api_hash
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("SELECT api_id, api_hash, phone, session FROM login ORDER BY id DESC LIMIT 1")
    result = c.fetchone()
    if result:
        api_id, api_hash, authenticated_phone, session_str = result
        client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
        asyncio.run_coroutine_threadsafe(client.connect(), asyncio_loop)
        authenticated = True
    c.execute("SELECT id, group_name, time, image, text, status FROM tasks")
    for row in c.fetchall():
        task_id, group_name, time, image, text, status = row
        tasks[task_id] = {"group_name": group_name, "time": time, "image": image, "text": text, "status": status}
        if status == "Rodando":
            asyncio.run_coroutine_threadsafe(schedule_task(task_id, tasks[task_id]), asyncio_loop)
    conn.close()

load_state()

# Rotas da API
@app.route("/auth/send_code", methods=["POST"])
def auth_send_code():
    global api_id, api_hash
    data = request.json
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    phone = data.get("phone")
    if not api_id or not api_hash or not phone:
        return jsonify({"success": False, "message": "api_id, api_hash e phone são obrigatórios"}), 400

    future = asyncio.run_coroutine_threadsafe(send_code_request(api_id, api_hash, phone), asyncio_loop)
    success, msg = future.result()
    status = 200 if success else 400
    return jsonify({"success": success, "message": msg}), status

@app.route("/auth/verify_code", methods=["POST"])
def auth_verify_code():
    if not api_id or not api_hash:
        return jsonify({"success": False, "message": "Envie primeiro o código"}), 400
    data = request.json
    phone = data.get("phone")
    code = data.get("code")
    if not phone or not code:
        return jsonify({"success": False, "message": "phone e code são obrigatórios"}), 400

    future = asyncio.run_coroutine_threadsafe(do_authenticate(phone, code), asyncio_loop)
    success, msg = future.result()
    status = 200 if success else 400
    if success:
        future_groups = asyncio.run_coroutine_threadsafe(load_groups(), asyncio_loop)
        gsuccess, gdata = future_groups.result()
        if not gsuccess:
            msg += f" | Aviso: {gdata}"
    return jsonify({"success": success, "message": msg}), status

@app.route("/groups", methods=["GET"])
def get_groups():
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401
    return jsonify({"success": True, "groups": groups_cache}), 200

@app.route("/tasks", methods=["POST"])
def add_new_tasks():
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401

    data = request.json
    group_name = data.get("group_name")
    time = data.get("time")
    images_data = data.get("images")

    if not group_name or not time or not images_data:
        return jsonify({"success": False, "message": "group_name, time e images são obrigatórios"}), 400

    try:
        time.strptime(time, "%H:%M")
    except ValueError:
        return jsonify({"success": False, "message": "Horário inválido. Use o formato HH:MM"}), 400

    created_tasks = []
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    for img in images_data:
        path = img.get("path")
        text = img.get("text", "")
        if not os.path.isfile(path):
            return jsonify({"success": False, "message": f"Imagem {path} não encontrada"}), 400

        task_id = str(uuid.uuid4())
        tasks[task_id] = {
            "group_name": group_name,
            "time": time,
            "image": path,
            "text": text,
            "status": "Rodando",
        }
        c.execute("INSERT INTO tasks (id, group_name, time, image, text, status) VALUES (?, ?, ?, ?, ?, ?)",
                  (task_id, group_name, time, path, text, "Rodando"))
        asyncio.run_coroutine_threadsafe(schedule_task(task_id, tasks[task_id]), asyncio_loop)
        created_tasks.append({"task_id": task_id})
    conn.commit()
    conn.close()

    return jsonify({"success": True, "tasks_created": created_tasks}), 200

@app.route("/tasks", methods=["GET"])
def list_tasks():
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401

    tasks_list = []
    for tid, tdata in tasks.items():
        tasks_list.append({"task_id": tid, **tdata})
    return jsonify({"success": True, "tasks": tasks_list}), 200

@app.route("/tasks/<task_id>/stop", methods=["PUT"])
def stop_a_task(task_id):
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401

    if task_id not in tasks:
        return jsonify({"success": False, "message": "Tarefa não encontrada"}), 404

    tasks[task_id]["status"] = "Parada"
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("UPDATE tasks SET status = ? WHERE id = ?", ("Parada", task_id))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": "Tarefa parada"}), 200

@app.route("/tasks/<task_id>/resume", methods=["PUT"])
def resume_a_task(task_id):
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401

    if task_id not in tasks:
        return jsonify({"success": False, "message": "Tarefa não encontrada"}), 404

    tasks[task_id]["status"] = "Rodando"
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("UPDATE tasks SET status = ? WHERE id = ?", ("Rodando", task_id))
    conn.commit()
    conn.close()

    asyncio.run_coroutine_threadsafe(schedule_task(task_id, tasks[task_id]), asyncio_loop)
    return jsonify({"success": True, "message": "Tarefa retomada"}), 200

@app.route("/tasks/<task_id>", methods=["DELETE"])
def delete_a_task(task_id):
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401

    if task_id not in tasks:
        return jsonify({"success": False, "message": "Tarefa não encontrada"}), 404

    del tasks[task_id]
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": "Tarefa deletada"}), 200

@app.route("/uploads/<path:filename>", methods=["GET"])
def serve_uploaded_file(filename):
    return send_from_directory(upload_dir, filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=443, ssl_context=("/etc/letsencrypt/live/paineltech.shop/fullchain.pem", "/etc/letsencrypt/live/paineltech.shop/privkey.pem"))
