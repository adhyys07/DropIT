import { MSG, send } from "./protocol.js";

export function attachSenderReconnect(socket, state, hooks={}) {
    socket.io.on('reconnect', () => {
    if (!state.roomCode || !state.resumeToken) return;
    socket.emit('resume-room', { code: state.roomCode, resumeToken: state.resumeToken });
  });
  socket.on('room-resumed', (room) => hooks.onResumed?.(room));
  socket.on('error', (e) => hooks.onError?.(e));

  const beat = setInterval(() => {
    if (state.roomCode && socket.connected) {
      socket.emit('touch-room', { code: state.roomCode });
    }
  }, 60_000);
  return () => clearInterval(beat);
}

export function attachReceiverReconnect(socket, state, hooks = {}) {
  socket.io.on('reconnect', () => {
    if (!state.roomCode) return;
    socket.emit('join-room', {
      code: state.roomCode, password: state.password, deviceName: state.deviceName,
    });
  });
  socket.on('sender-dropped', (d) => hooks.onSenderDropped?.(d));
  socket.on('sender-back', () => hooks.onSenderBack?.());
  socket.on('room-closed', (d) => hooks.onRoomClosed?.(d));
}


export function withPeerRetry({ rebuildPeer, maxAttempts = 6, baseMs = 800 }) {
  let attempt = 0;
  let stopped = false;

  async function run() {
    while (!stopped && attempt < maxAttempts) {
      try {
        const peer = await rebuildPeer();
        attempt = 0;
        return peer;
      } catch (err) {
        attempt++;
        if (attempt >= maxAttempts) throw err;
        const wait = Math.min(15000, baseMs * 2 ** (attempt - 1))
                   + Math.random() * 400;   // jitter
        await new Promise((r) => setTimeout(r, wait));
      }
    }
    throw new Error('Reconnection abandoned');
  }

  return { run, stop: () => { stopped = true; }, get attempt() { return attempt; } };
}

export function resumeTransfer(peer, recvEngine) {
  recvEngine.peer = peer;
  peer.on('data', (d) => recvEngine.onData(d));
  peer.on('connect', () => recvEngine.announceResumeState());
}
