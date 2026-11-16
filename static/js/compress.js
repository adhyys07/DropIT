const SKIP = /^(image\/(jpeg|png|webp|avif|gif)|video\/|audio\/|application\/(zip|gzip|x-7z|x-rar|pdf))/;
const MIN_BYTES = 4096;

export const supported = () => typeof CompressionStream === 'function';

export function worthCompressing(mime, size) {
  if (!supported()) return false;
  if (size < MIN_BYTES) return false;
  return !SKIP.test(mime || '');
}

export async function gzip(u8) {
  const cs = new CompressionStream('gzip');
  const stream = new Blob([u8]).stream().pipeThrough(cs);
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

export async function gunzip(u8) {
  const ds = new DecompressionStream('gzip');
  const stream = new Blob([u8]).stream().pipeThrough(ds);
  return new Uint8Array(await new Response(stream).arrayBuffer());
}
