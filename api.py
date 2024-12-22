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
from datetime import datetime
import base64


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
            scheduled_time TEXT,
            message_text TEXT,
            image_path TEXT,
            status TEXT,
            tag_members INTEGER
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
        if current_time == task_details["scheduled_time"]:
            await send_image_to_group(task_details["group_name"], task_details["image_path"], task_details["message_text"])
            await asyncio.sleep(60)  # Espera 1 minuto antes de verificar novamente
        await asyncio.sleep(1)

async def get_group_members(group_name):
    try:
        for dialog in await client.get_dialogs():
            if dialog.name == group_name:
                participants = await client.get_participants(dialog)
                return [{"id": user.id, "username": user.username, "first_name": user.first_name} for user in participants]
    except Exception as e:
        print(f"Erro ao obter membros do grupo: {str(e)}")
        return []

async def send_tag_message(group_name):
    try:
        # Encontrar o grupo
        target_group = None
        async for dialog in client.iter_dialogs():
            if dialog.name == group_name:
                target_group = dialog
                break
        
        if not target_group:
            return False, "Grupo não encontrado"

        # Obter participantes
        participants = await client.get_participants(target_group)
        
        # Criar mensagem de marcação
        tag_message = ""
        for user in participants:
            if user.username:
                tag_message += f"@{user.username} "
            elif user.first_name:
                tag_message += f"[{user.first_name}](tg://user?id={user.id}) "
        
        # Enviar mensagem
        if tag_message:
            await client.send_message(target_group, tag_message.strip())
            return True, "Mensagem de marcação enviada com sucesso"
        return False, "Nenhum membro para marcar"
        
    except Exception as e:
        return False, f"Erro ao enviar mensagem de marcação: {str(e)}"

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
    c.execute("SELECT id, group_name, scheduled_time, message_text, image_path, status, tag_members FROM tasks")
    for row in c.fetchall():
        task_id, group_name, scheduled_time, message_text, image_path, status, tag_members = row
        tasks[task_id] = {"group_name": group_name, "scheduled_time": scheduled_time, "message_text": message_text, "image_path": image_path, "status": status, "tag_members": tag_members}
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

@app.route("/images", methods=["POST"])
def upload_images():
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401

    # Recebe arquivos via multipart/form-data
    if "images" not in request.files:
        return jsonify({"success": False, "message": "Nenhuma imagem enviada"}), 400

    files = request.files.getlist("images")
    texts = request.form.get("texts")
    if texts:
        import json
        try:
            texts = json.loads(texts)
        except:
            return jsonify({"success": False, "message": "Erro ao processar os textos das imagens"}), 400

    if texts and len(texts) != len(files):
        return jsonify({"success": False, "message": "O número de textos não corresponde ao número de imagens"}), 400

    uploaded_data = []
    for i, f in enumerate(files):
        filename = f"{uuid.uuid4()}_{f.filename}"
        filepath = os.path.join(upload_dir, filename)
        f.save(filepath)
        text = texts[i] if texts and i < len(texts) else ""
        uploaded_data.append({"path": filepath, "text": text})

    return jsonify({"success": True, "uploaded_images": uploaded_data}), 200

@app.route("/tag_members/<group_name>", methods=["GET"])
async def tag_members(group_name):
    if not authenticated:
        return jsonify({"error": "Not authenticated"}), 401

    members = await get_group_members(group_name)
    tag_text = ""
    for member in members:
        if member["username"]:
            tag_text += f"@{member['username']} "
        elif member["first_name"]:
            tag_text += f"[{member['first_name']}](tg://user?id={member['id']}) "

    return jsonify({"tag_text": tag_text.strip()})

@app.route("/tag_members_individual/<group_name>", methods=["GET"])
async def tag_members_individual(group_name):
    if not authenticated:
        return jsonify({"error": "Not authenticated"}), 401

    members = await get_group_members(group_name)
    individual_tags = []
    for member in members:
        if member["username"]:
            individual_tags.append(f"@{member['username']}")
        elif member["first_name"]:
            individual_tags.append(f"[{member['first_name']}](tg://user?id={member['id']})")

    return jsonify({"individual_tags": individual_tags})

@app.route("/add_tasks", methods=["POST"])
def add_new_tasks():
    if not authenticated:
        return jsonify({"error": "Not authenticated"}), 401

    if 'tasks' not in request.json:
        return jsonify({"error": "No tasks provided"}), 400

    tasks_data = request.json['tasks']
    response_tasks = []

    for task in tasks_data:
        if 'group_name' not in task or 'scheduled_time' not in task:
            continue

        task_id = str(uuid.uuid4())
        
        # Processar imagem se existir
        image_path = None
        if 'image' in task and task['image'].strip():
            try:
                image_data = task['image'].split(',')[1]
                image_bytes = base64.b64decode(image_data)
                filename = f"{task_id}{os.path.splitext(task.get('filename', '.jpg'))[1]}"
                image_path = os.path.join(upload_dir, filename)
                
                with open(image_path, 'wb') as f:
                    f.write(image_bytes)
            except Exception as e:
                print(f"Erro ao processar imagem: {str(e)}")

        task_details = {
            "group_name": task['group_name'],
            "scheduled_time": task['scheduled_time'],
            "image_path": image_path,
            "message_text": task.get('text', ''),
            "status": "scheduled",
            "tag_members": task.get('tag_members', False)
        }

        tasks[task_id] = task_details
        
        # Salvar no banco de dados
        conn = sqlite3.connect(db_file)
        c = conn.cursor()
        c.execute("""
            INSERT INTO tasks (id, group_name, scheduled_time, message_text, image_path, status, tag_members)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (task_id, task_details["group_name"], task_details["scheduled_time"], 
              task_details["message_text"], task_details.get("image_path", ""), task_details["status"],
              task_details["tag_members"]))
        conn.commit()
        conn.close()

        # Se tag_members for True, agendar o envio da mensagem de marcação
        if task_details["tag_members"]:
            asyncio.run_coroutine_threadsafe(send_tag_message(task_details["group_name"]), asyncio_loop)

        asyncio.run_coroutine_threadsafe(schedule_task(task_id, task_details), asyncio_loop)
        response_tasks.append({"task_id": task_id, **task_details})

    return jsonify({"message": "Tasks added successfully", "tasks": response_tasks})

@app.route("/tasks", methods=["GET"])
def list_tasks():
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401

    tasks_list = []
    for tid, tdata in tasks.items():
        tasks_list.append({"task_id": tid, **tdata})
    return jsonify({"success": True, "tasks": tasks_list}), 200

@app.route("/auth/logout", methods=["POST"])
def logout():
    global authenticated, authenticated_phone, tasks, client

    try:
        print("[LOGOUT] Iniciando logout...")

        # Desconectar do Telegram
        if client:
            print("[LOGOUT] Desconectando do cliente do Telegram...")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(client.disconnect())

        # Atualizar estado global
        authenticated = False
        authenticated_phone = None

        # Limpar tarefas em memória
        print("[LOGOUT] Limpando tarefas...")
        tasks.clear()

        # Apagar tarefas do banco de dados
        conn = sqlite3.connect(db_file)
        c = conn.cursor()
        c.execute("DELETE FROM tasks")
        conn.commit()
        conn.close()
        print("[LOGOUT] Tarefas removidas do banco de dados.")

        # Apagar login salvo no banco de dados
        conn = sqlite3.connect(db_file)
        c = conn.cursor()
        c.execute("DELETE FROM login")
        conn.commit()
        conn.close()
        print("[LOGOUT] Login removido do banco de dados.")

        return jsonify({"success": True, "message": "Logout realizado com sucesso"}), 200

    except Exception as e:
        print(f"[ERRO] Erro ao realizar logout: {e}")
        return jsonify({"success": False, "message": f"Erro ao realizar logout: {e}"}), 500

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

@app.route("/edit_task/<task_id>", methods=["PUT"])
def edit_task(task_id):
    if task_id not in tasks:
        return jsonify({"error": "Task not found"}), 404

    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    task = tasks[task_id]
    if "group_name" in data:
        task["group_name"] = data["group_name"]
    if "scheduled_time" in data:
        task["scheduled_time"] = data["scheduled_time"]
    if "message_text" in data:
        task["message_text"] = data["message_text"]

    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("""
        UPDATE tasks 
        SET group_name = ?, scheduled_time = ?, message_text = ?, status = ?
        WHERE id = ?
    """, (task["group_name"], task["scheduled_time"], task["message_text"], task["status"], task_id))
    conn.commit()
    conn.close()

    return jsonify({"message": "Task updated successfully", "task": task})

@app.route("/send_tag_message/<group_name>", methods=["POST"])
def tag_message_endpoint(group_name):
    if not authenticated:
        return jsonify({"error": "Not authenticated"}), 401

    success, message = asyncio.run(send_tag_message(group_name))
    
    if success:
        return jsonify({"message": message}), 200
    return jsonify({"error": message}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=443, ssl_context=("/etc/letsencrypt/live/paineltech.shop/fullchain.pem", "/etc/letsencrypt/live/paineltech.shop/privkey.pem"))
