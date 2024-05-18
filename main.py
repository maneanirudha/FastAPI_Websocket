from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from typing import Dict, List
import json
import uuid
from models import Base, FormData, SessionLocal, engine

app = FastAPI()

# Ensure database tables are created
Base.metadata.create_all(bind=engine)

html = """
<!DOCTYPE html>
<html>
    <head>
        <title>Real-time Form Collaboration</title>
    </head>
    <body>
        <h1>Real-time Form Collaboration</h1>
        <button id="createForm">Create New Form</button>
        <form id="collabForm" style="display:none;">
            <label for="name">Name:</label><br>
            <input type="text" id="name" name="name"><br>
            <span id="nameEditor"></span><br>
            <label for="email">Email:</label><br>
            <input type="email" id="email" name="email"><br>
            <span id="emailEditor"></span><br>
        </form>
        <div id="userList"></div>
        <script>
            const createFormButton = document.getElementById('createForm');
            createFormButton.onclick = async function() {
                const response = await fetch('/create_form', { method: 'POST' });
                const data = await response.json();
                const formId = data.form_id;
                window.location.href = `/?form=${formId}`;
            };

            const urlParams = new URLSearchParams(window.location.search);
            const formId = urlParams.get('form');
            if (formId) {
                const userId = Math.random().toString(36).substring(2, 15);
                const form = document.getElementById('collabForm');
                form.style.display = 'block';
                const ws = new WebSocket(`wss://${location.host}/ws/${formId}/${userId}`);

                let editingField = null;

                ws.onmessage = function(event) {
                    const data = JSON.parse(event.data);
                    if (data.type === 'update') {
                        for (let key in data.payload) {
                            if (form.elements[key]) {
                                form.elements[key].value = data.payload[key];
                            }
                        }
                    } else if (data.type === 'user_list') {
                        const userListDiv = document.getElementById('userList');
                        userListDiv.innerHTML = 'Users editing: ' + data.payload.join(', ');
                    } else if (data.type === 'lock') {
                        const field = form.elements[data.payload.field];
                        const editorSpan = document.getElementById(data.payload.field + 'Editor');
                        if (data.payload.user_id !== userId) {
                            field.disabled = true;
                            editorSpan.textContent = `Editing by ${data.payload.user_id}`;
                        } else {
                            field.disabled = false;
                            editorSpan.textContent = '';
                        }
                    } else if (data.type === 'unlock') {
                        const field = form.elements[data.payload.field];
                        field.disabled = false;
                        document.getElementById(data.payload.field + 'Editor').textContent = '';
                    }
                };

                form.addEventListener('input', function(event) {
                    const formData = new FormData(form);
                    const data = {};
                    formData.forEach((value, key) => data[key] = value);
                    ws.send(JSON.stringify({ type: 'update', user_id: userId, payload: data }));
                });

                form.addEventListener('focusin', function(event) {
                    if (editingField === event.target.name) return;
                    editingField = event.target.name;
                    ws.send(JSON.stringify({ type: 'lock', user_id: userId, payload: { field: editingField } }));
                });

                form.addEventListener('focusout', function(event) {
                    if (editingField !== event.target.name) return;
                    ws.send(JSON.stringify({ type: 'unlock', user_id: userId, payload: { field: editingField } }));
                    editingField = null;
                });

                ws.onopen = function() {
                    ws.send(JSON.stringify({ type: 'fetch_data' }));
                };
            }
        </script>
    </body>
</html>
"""

class ConnectionManager:
    def __init__(self):
        self.rooms: Dict[str, Dict[str, WebSocket]] = {}
        self.locks: Dict[str, Dict[str, str]] = {}  # room_id -> field -> user_id

    async def connect(self, websocket: WebSocket, room_id: str, user_id: str):
        await websocket.accept()
        if room_id not in self.rooms:
            self.rooms[room_id] = {}
        self.rooms[room_id][user_id] = websocket
        await self.broadcast_user_list(room_id)

    def disconnect(self, room_id: str, user_id: str):
        if room_id in self.rooms and user_id in self.rooms[room_id]:
            del self.rooms[room_id][user_id]
        if not self.rooms[room_id]:
            del self.rooms[room_id]

        if room_id in self.locks:
            fields_to_unlock = [field for field, uid in self.locks[room_id].items() if uid == user_id]
            for field in fields_to_unlock:
                del self.locks[room_id][field]
            if not self.locks[room_id]:
                del self.locks[room_id]

    async def broadcast(self, room_id: str, message: str, sender_id: str = None):
        for user_id, connection in self.rooms.get(room_id, {}).items():
            if sender_id is None or user_id != sender_id:
                try:
                    await connection.send_text(message)
                except Exception:
                    pass  # Handle potential connection issues

    async def broadcast_user_list(self, room_id: str):
        user_list = list(self.rooms[room_id].keys())
        await self.broadcast(room_id, json.dumps({"type": "user_list", "payload": user_list}))

    async def broadcast_lock(self, room_id: str, field: str):
        if room_id in self.locks and field in self.locks[room_id]:
            user_id = self.locks[room_id][field]
            await self.broadcast(room_id, json.dumps({"type": "lock", "payload": {"field": field, "user_id": user_id}}))

    async def broadcast_unlock(self, room_id: str, field: str):
        await self.broadcast(room_id, json.dumps({"type": "unlock", "payload": {"field": field}}))

manager = ConnectionManager()

@app.get("/")
async def get():
    return HTMLResponse(html)

@app.post("/create_form")
async def create_form():
    form_id = str(uuid.uuid4())
    return JSONResponse(content={"form_id": form_id})

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.websocket("/ws/{room_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, user_id: str, db: Session = Depends(get_db)):
    await manager.connect(websocket, room_id, user_id)
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            if message['type'] == 'update':
                form_data = message['payload']
                form_entry = db.query(FormData).filter(FormData.id == room_id).first()
                if not form_entry:
                    form_entry = FormData(id=room_id, **form_data)
                    db.add(form_entry)
                else:
                    form_entry.name = form_data.get('name', form_entry.name)
                    form_entry.email = form_data.get('email', form_entry.email)
                db.commit()
                await manager.broadcast(room_id, json.dumps(message), sender_id=user_id)
            elif message['type'] == 'fetch_data':
                form_entry = db.query(FormData).filter(FormData.id == room_id).first()
                if form_entry:
                    await websocket.send_text(json.dumps({"type": "update", "payload": {"name": form_entry.name, "email": form_entry.email}}))
            elif message['type'] == 'lock':
                field = message['payload']['field']
                if room_id not in manager.locks:
                    manager.locks[room_id] = {}
                manager.locks[room_id][field] = user_id
                await manager.broadcast_lock(room_id, field)
            elif message['type'] == 'unlock':
                field = message['payload']['field']
                if room_id in manager.locks and field in manager.locks[room_id] and manager.locks[room_id][field] == user_id:
                    del manager.locks[room_id][field]
                    await manager.broadcast_unlock(room_id, field)
    except WebSocketDisconnect:
        manager.disconnect(room_id, user_id)
        await manager.broadcast_user_list(room_id)

@app.get("/form/{form_id}")
async def read_form_data(form_id: str, db: Session = Depends(get_db)):
    form_entry = db.query(FormData).filter(FormData.id == form_id).first()
    if not form_entry:
        raise HTTPException(status_code=404, detail="Form not found")
    return form_entry
