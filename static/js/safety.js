const WORDS = [
  'amber','anchor','badge','birch','bison','bloom','cedar','cobalt','comet','coral',
  'cliff','dune','ember','falcon','fern','flint','garnet','glade','harbor','hazel',
  'indigo','ivory','jade','kelp','lantern','linen','marble','meadow','mint','north',
  'onyx','opal','otter','pebble','quartz','quill','raven','reef','saffron','slate',
  'summit','thistle','tide','topaz','umber','vale','walnut','willow','xenon','yarrow',
  'zephyr','arbor','basalt','clover','dusk','elm','frost','grove','heron','iris',
  'juniper','kestrel','lotus','moss','nimbus','orchid','pine','ridge','spruce','terra',
];

export function fingerprints(peer) {
  const pc = peer && peer._pc;
  if (!pc || !pc.localDescription || !pc.remoteDescription) return null;
  const grab = (sdp) => {
    const m = /a=fingerprint:\S+\s+([0-9A-Fa-f:]+)/.exec(sdp || '');
    return m ? m[1].replace(/:/g, '').toLowerCase() : null;
  };
  const local = grab(pc.localDescription.sdp);
  const remote = grab(pc.remoteDescription.sdp);
  return local && remote ? { local, remote } : null;
}

export async function safetyWords(peer, count=4) {
  const fp = fingerprints(peer);
  if (!fp) return null;
  const joined = [fp.local, fp.remote].sort().join('|');
  const digest = new Uint8Array(
    await crypto.subtle.digest('SHA-256', new TextEncoder().encode(joined)));
  return Array.from({ length: count }, (_, i) => WORDS[digest[i] % WORDS.length]);
}

export async function safetyCode(peer) {
  const words = await safetyWords(peer);
  return words ? words.join(' · ') : null;
}
