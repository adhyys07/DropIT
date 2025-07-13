from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit
import random, string
import os
app = Flask(__name__, static_folder='static')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")
    emit('client-id', {'clientId': request.sid})

offers = {}  # conn_key -> {'offer': ..., 'sender_sid': ..., 'receivers': set()}

def generate_conn_key():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=18))

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('static', path)

@socketio.on('register-offer')
def handle_offer(data):
    conn_key = generate_conn_key()
    offers[conn_key] = {
        'offer': data['signal'],
        'sender_sid': request.sid,
        'receivers': set()
    }
    print(f"Registered offer with key: {conn_key} for sender: {request.sid}")
    emit('offer-registered', {'connKey': conn_key}, room=request.sid)

@socketio.on('fetch-offer')
def handle_fetch(data):
    key = data['connKey']
    offer_obj = offers.get(key)
    if offer_obj:
        print(f"Delivering offer for key: {key} to receiver: {request.sid}")
        emit('deliver-offer', {'signal': offer_obj['offer']}, room=request.sid)
        offer_obj['receivers'].add(request.sid)
    else:
        print(f"Invalid key requested: {key}")
        emit('error', {'message': 'Invalid key'})

@socketio.on('send-answer')
def handle_answer(data):
    key = data.get('connKey')
    signal = data.get('signal')
    offer_obj = offers.get(key)
    if offer_obj:
        sender_sid = offer_obj['sender_sid']
        print(f"[Broadcast] Routing answer from receiver {request.sid} to sender {sender_sid} for key: {key}")
        socketio.emit('deliver-answer', signal, room=sender_sid)
        # Optionally, confirm to receiver
        emit('answer-delivered', {'status': 'ok', 'key': key})
    else:
        print(f"No offer found for answer with key: {key}")
        emit('error', {'message': 'No offer found for this key'})

@socketio.on('disconnect')
def handle_disconnect():
    print(f"Client disconnected: {request.sid}")
    # Clean up offers and receivers
    to_delete = []
    for key, offer_obj in list(offers.items()):
        if offer_obj['sender_sid'] == request.sid:
            to_delete.append(key)
        else:
            if request.sid in offer_obj['receivers']:
                print(f"Receiver {request.sid} disconnected from offer {key}")
            offer_obj['receivers'].discard(request.sid)
    for key in to_delete:
        print(f"Cleaning up offer: {key}")
        del offers[key]

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))  # allow external hosting platforms to set port
    socketio.run(app, host="0.0.0.0", port=port)