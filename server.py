from flask import Flask, send_from_directory
from flask_socketio import SocketIO, emit
import random, string

app = Flask(__name__, static_folder='static')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

signal_map = {}  # key -> offer signal

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
    signal_map[conn_key] = data['signal']
    emit('offer-registered', {'connKey': conn_key})

@socketio.on('fetch-offer')
def handle_fetch(data):
    key = data['connKey']
    offer = signal_map.get(key)
    if offer:
        emit('deliver-offer', {'signal': offer})
    else:
        emit('error', {'message': 'Invalid key'})

@socketio.on('send-answer')
def handle_answer(data):
    emit('deliver-answer', data['signal'], broadcast=True)

if __name__ == '__main__':
    socketio.run(app, port=5000)
