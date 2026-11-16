/* Reconnection, at two levels:
     - socket   : room membership (Socket.IO already retries the transport)
     - peer     : the WebRTC datachannel itself, rebuilt with backoff
   Feature 8. */

/** Socket.IO retries on its own; this re-establishes room state on top. */
export function attachSenderReconnect(socket, state, hooks = {}) {
  socket.io.on('reconnect', () => {
    if (!state.roomCode || !state.resumeToken) return;
    socket.emit('resume-room', { code: state.roomCode, resumeToken: state.resumeToken });
  });
  socket.on('room-resumed', (room) => hooks.onResumed?.(room));
  socket.on('error', (e) => hooks.onError?.(e));

  const beat = setInterval(() => {
    if (state.roomCode && socket.connected) socket.emit('touch-room', { code: state.roomCode });
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

/**
 * Peer-level supervisor: rebuilds a dropped datachannel with exponential
 * backoff plus jitter, and gives up cleanly rather than retrying forever.
 *
 *   const sup = supervise({ build, shouldRetry: () => running });
 *   sup.start(); await build();
 *   // peer.on('connect') -> sup.noteConnected()
 *   // peer.on('close')   -> sup.noteLost()
 */
export function supervise({ build, shouldRetry = () => true, hooks = {},
                            maxAttempts = 8, baseMs = 700, capMs = 20_000 }) {
  let attempt = 0;
  let stopped = false;
  let timer = null;
  let pending = false;

  function schedule() {
    if (stopped || pending || !shouldRetry()) return;
    if (attempt >= maxAttempts) { hooks.onGaveUp?.(attempt); return; }
    attempt++;
    pending = true;
    const wait = Math.min(capMs, baseMs * 2 ** (attempt - 1)) + Math.random() * 400;
    hooks.onRetry?.({ attempt, waitMs: Math.round(wait), max: maxAttempts });
    timer = setTimeout(async () => {
      pending = false;
      if (stopped || !shouldRetry()) return;
      try {
        await build();
      } catch (err) {
        hooks.onError?.(err);
        schedule();
      }
    }, wait);
  }

  return {
    /** Call from peer.on('connect'). */
    noteConnected() {
      const wasRetrying = attempt > 0;
      attempt = 0;
      pending = false;
      clearTimeout(timer);
      if (wasRetrying) hooks.onRecovered?.();
    },
    /** Call from peer.on('close') or peer.on('error'). */
    noteLost() { schedule(); },
    start() { stopped = false; attempt = 0; },
    stop() { stopped = true; pending = false; clearTimeout(timer); },
    get attempt() { return attempt; },
    get stopped() { return stopped; },
  };
}

/** Re-point a RecvEngine at a fresh peer and re-announce what we already hold. */
export function rebindRecvEngine(peer, engine) {
  engine.peer = peer;
  peer.on('connect', () => engine.announceResumeState());
}
