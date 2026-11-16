const FALLBACK = {
  iceServers: [{ urls: ['stun:stun.l.google.com:19302'] }],
  hasTurn: false,
  note: 'Could not reach /ice — using a default STUN server only.',
};

let cache = null;
let cacheUntil = 0;
let inflight = null;

export async function iceConfig() {
    if (cache && Date.now() < cacheUntil) return cache;
    if (inflight) return inflight;
     
    inflight = (async () => {
    try {
      const res = await fetch('/ice', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      cache = data;
      cacheUntil = Date.now() + Math.max(30, (data.ttlSeconds || 3600) - 60) * 1000;
      return cache;
    } catch {
      cache = FALLBACK;
      cacheUntil = Date.now() + 60_000;
      return cache;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

export async function peerOptions(extra = {}) {
    const { iceServers } = await iceConfig();
    return {
        trickle: true,
    config: {
      iceServers,
      iceTransportPolicy: 'all',
      bundlePolicy: 'max-bundle',
    },
    ...extra,
  };
}

export async function hasTurn() {
  return (await iceConfig()).hasTurn === true;
}

export function attachIceWatch(peer, hooks = {}, { blockedAfterMs = 12_000 } = {}) {
  const pc = peer && peer._pc;
  if (!pc) return () => {};

  let timer = setTimeout(async () => {
    if (['connected', 'completed'].includes(pc.iceConnectionState)) return;
    const relay = await usedCandidateTypes(pc);
    hooks.onLikelyBlocked?.({
      state: pc.iceConnectionState,
      gathered: relay,
      hasTurn: (await iceConfig()).hasTurn,
    });
  }, blockedAfterMs);

  const onIce = () => {
    const s = pc.iceConnectionState;
    hooks.onState?.(s);
    if (s === 'connected' || s === 'completed') { clearTimeout(timer); hooks.onOk?.(); }
    if (s === 'failed') { clearTimeout(timer); hooks.onFailed?.(s); }
  };
  const onGather = () => hooks.onGathering?.(pc.iceGatheringState);

  pc.addEventListener('iceconnectionstatechange', onIce);
  pc.addEventListener('icegatheringstatechange', onGather);

  return () => {
    clearTimeout(timer);
    pc.removeEventListener('iceconnectionstatechange', onIce);
    pc.removeEventListener('icegatheringstatechange', onGather);
  };
}

/** Which candidate types actually got gathered — tells you what's blocked. */
async function usedCandidateTypes(pc) {
  try {
    const stats = await pc.getStats();
    const types = new Set();
    stats.forEach((r) => {
      if (r.type === 'local-candidate' && r.candidateType) types.add(r.candidateType);
    });
    return [...types];                 // e.g. ['host'] means STUN never replied
  } catch { return []; }
}

export function explainIce({ state, gathered, hasTurn }) {
  if (!gathered.length || gathered.join() === 'host') {
    return 'Your network blocked the connection check (no public address found). '
         + (hasTurn ? 'Retrying over the relay…'
                    : 'This usually needs a TURN relay, which is not configured.');
  }
  if (!hasTurn) {
    return 'A direct link could not be made. Mobile and office networks often '
         + 'need a TURN relay, which is not configured on this server.';
  }
  return `Still negotiating (${state}). If this persists, one side's firewall is blocking UDP.`;
}

export async function connectionQuality(peer) {
  const pc = peer && peer._pc;
  if (!pc) return null;
  try {
    const stats = await pc.getStats();
    let pair = null;
    const local = new Map(), remote = new Map();
    stats.forEach((r) => {
      if (r.type === 'local-candidate') local.set(r.id, r);
      if (r.type === 'remote-candidate') remote.set(r.id, r);
      if (r.type === 'candidate-pair' && (r.nominated || r.state === 'succeeded')) {
        if (!pair || (r.bytesSent || 0) > (pair.bytesSent || 0)) pair = r;
      }
    });
    if (!pair) return null;
    const l = local.get(pair.localCandidateId);
    const r = remote.get(pair.remoteCandidateId);
    const relayed = l?.candidateType === 'relay' || r?.candidateType === 'relay';
    return {
      relayed,
      path: relayed ? 'relayed via TURN'
          : l?.candidateType === 'host' ? 'direct (same network)' : 'direct',
      rttMs: pair.currentRoundTripTime != null
        ? Math.round(pair.currentRoundTripTime * 1000) : null,
      availableOutgoingBps: pair.availableOutgoingBitrate || null,
      bytesSent: pair.bytesSent || 0,
      bytesReceived: pair.bytesReceived || 0,
      protocol: l?.protocol || null,
    };
  } catch { return null; }
}
