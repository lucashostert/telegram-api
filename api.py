import os
import uuid
import asyncio
import aiofiles
from flask import Flask, request, jsonify
from flask_cors import CORS
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, AuthRestartError
from threading import Thread
import shutil
import time

app = Flask(__name__)
CORS(app)

# Variáveis globais
client = None
authenticated = False
authenticated_phone = None
api_id = None
api_hash = None

asyncio_loop = None

tasks = {}  # {task_id: {"group":..., "interval":..., "image":..., "text":..., "status":...}}
groups_cache = []  # lista de grupos
upload_dir = "uploads"
os.makedirs(upload_dir, exist_ok=True)

# Funções Assíncronas

async def send_code_request(api_id_local, api_hash_local, phone):
    global client
    try:
        client = TelegramClient('session', int(api_id_local), api_hash_local)
        await client.connect()
        await client.send_code_request(phone)
        return True, "Código enviado com sucesso!"
    except AuthRestartError:
        # Se for necessário reiniciar a autenticação
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
        return True, "Autenticado com sucesso!"
    except SessionPasswordNeededError:
        return False, "Senha de dois fatores necessária!"
    except Exception as e:
        return False, f"Erro ao autenticar: {e}"

async def load_groups():
    global groups_cache
    try:
        dialogs = await client.get_dialogs()
        groups = [dialog.title for dialog in dialogs if dialog.is_group]
        groups_cache = groups
        return True, groups
    except Exception as e:
        return False, f"Erro ao carregar grupos: {e}"

async def send_image_to_group(group, image_path, text=""):
    try:
        dialogs = await client.get_dialogs()
        chat = next(dialog for dialog in dialogs if dialog.is_group and dialog.title == group)
        await client.send_file(chat, image_path, caption=text)
        print(f"Imagem {image_path} enviada para o grupo {group} com o texto: {text}")
    except Exception as e:
        print(f"Erro ao enviar imagem para o grupo {group}: {e}")

async def schedule_task(task_id, task_details):
    while tasks.get(task_id, {}).get("status") == "Rodando":
        await send_image_to_group(task_details["group"], task_details["image"], task_details["text"])
        await asyncio.sleep(task_details["interval"] * 60)


# Loop assíncrono separado
def start_asyncio_loop():
    global asyncio_loop
    asyncio_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(asyncio_loop)
    asyncio_loop.run_forever()

asyncio_thread = Thread(target=start_asyncio_loop, daemon=True)
asyncio_thread.start()


# Funções de tarefa (parar, retomar, deletar)
def stop_task(task_id):
    if task_id in tasks:
        tasks[task_id]["status"] = "Parada"

def resume_task(task_id):
    if task_id in tasks:
        tasks[task_id]["status"] = "Rodando"
        asyncio.run_coroutine_threadsafe(schedule_task(task_id, tasks[task_id]), asyncio_loop)

def delete_task(task_id):
    if task_id in tasks:
        # Não há cancelamento ativo de asyncio aqui, mas se estiver "Rodando" não terá impacto imediato
        # A checagem no loop do schedule_task já impedirá o envio futuro se a task for removida
        del tasks[task_id]

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
        # Carregar grupos após autenticar
        future_groups = asyncio.run_coroutine_threadsafe(load_groups(), asyncio_loop)
        gsuccess, gdata = future_groups.result()
        if not gsuccess:
            # Não interrompe a autenticação, mas avisa do erro
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
    # Pode receber vários arquivos de imagem
    # Pode receber textos adicionais para cada imagem (opcional)
    # Exemplo: enviar com curl:
    # curl -F "images=@caminho_da_imagem.jpg" -F "images=@outra_imagem.png" http://localhost:5000/images

    if "images" not in request.files:
        return jsonify({"success": False, "message": "Nenhuma imagem enviada"}), 400

    files = request.files.getlist("images")
    # opcionalmente poderíamos receber textos associados a cada imagem, por ex:
    # texts = request.form.getlist("texts") -> lista de textos na mesma ordem

    # Para simplificar, vamos permitir que o texto da imagem seja enviado via um array JSON separado
    # Ex: texts=["texto da primeira imagem", "texto da segunda imagem"]
    texts = request.form.get("texts")
    if texts:
        import json
        try:
            texts = json.loads(texts)
        except:
            texts = []

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

@app.route("/tasks", methods=["POST"])
def add_new_tasks():
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401

    data = request.json
    group = data.get("group")
    interval = data.get("interval")
    images_data = data.get("images")  # lista de {path, text}

    if not group or not interval or not images_data:
        return jsonify({"success": False, "message": "group, interval e images são obrigatórios"}), 400

    try:
        interval = int(interval)
    except:
        return jsonify({"success": False, "message": "interval deve ser um inteiro"}), 400

    created_tasks = []
    for img in images_data:
        path = img.get("path")
        text = img.get("text", "")
        if not os.path.isfile(path):
            return jsonify({"success": False, "message": f"Imagem {path} não encontrada"}), 400

        task_id = str(uuid.uuid4())
        tasks[task_id] = {
            "group": group,
            "interval": interval,
            "image": path,
            "text": text,
            "status": "Rodando",
        }
        asyncio.run_coroutine_threadsafe(schedule_task(task_id, tasks[task_id]), asyncio_loop)
        created_tasks.append({"task_id": task_id})

    return jsonify({"success": True, "tasks_created": created_tasks}), 200

@app.route("/tasks", methods=["GET"])
def list_tasks():
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401

    # Lista todas as tarefas
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
    stop_task(task_id)
    return jsonify({"success": True, "message": "Tarefa parada"}), 200

@app.route("/tasks/<task_id>/resume", methods=["PUT"])
def resume_a_task(task_id):
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401

    if task_id not in tasks:
        return jsonify({"success": False, "message": "Tarefa não encontrada"}), 404
    resume_task(task_id)
    return jsonify({"success": True, "message": "Tarefa retomada"}), 200

@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    global authenticated, authenticated_phone, client
    if not authenticated:
        return jsonify({"success": False, "message": "Não está autenticado"}), 401
    
    # Desconectar do Telegram
    future = asyncio.run_coroutine_threadsafe(client.log_out(), asyncio_loop)
    try:
        future.result()
    except Exception as e:
        return jsonify({"success": False, "message": f"Erro ao deslogar: {e}"}), 400

    # Alternativamente, se log_out não funcionar como esperado, use:
    # future = asyncio.run_coroutine_threadsafe(client.disconnect(), asyncio_loop)
    # future.result()

    authenticated = False
    authenticated_phone = None
    return jsonify({"success": True, "message": "Desconectado com sucesso!"}), 200


@app.route("/tasks/<task_id>", methods=["DELETE"])
def delete_a_task(task_id):
    if not authenticated:
        return jsonify({"success": False, "message": "Não autenticado"}), 401

    if task_id not in tasks:
        return jsonify({"success": False, "message": "Tarefa não encontrada"}), 404
    delete_task(task_id)
    return jsonify({"success": True, "message": "Tarefa deletada"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
