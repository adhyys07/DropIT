/* Feature 10: file previews. Styling comes from dialog.preview in dropit.css. */

const IMG   = /^image\/(png|jpeg|gif|webp|avif|bmp|svg\+xml)$/;
const VIDEO = /^video\/(mp4|webm|ogg|quicktime)$/;
const AUDIO = /^audio\//;
const TEXTY = /^(text\/|application\/(json|xml|javascript|x-sh|x-yaml))/;
const PREVIEW_TEXT_LIMIT = 256 * 1024;

export function canPreview(file) {
  const m = (file && file.mime) || '';
  return IMG.test(m) || VIDEO.test(m) || AUDIO.test(m)
      || m === 'application/pdf' || TEXTY.test(m);
}

/** Returns a node to mount, or null. The caller still owns file.url. */
export async function buildPreview(file) {
  if (!file || !file.url || !canPreview(file)) return null;
  const m = file.mime || '';

  if (IMG.test(m)) {
    const img = document.createElement('img');
    img.src = file.url;
    img.alt = file.name;
    img.loading = 'lazy';
    img.decoding = 'async';
    return img;
  }

  if (VIDEO.test(m)) {
    const v = document.createElement('video');
    v.src = file.url;
    v.controls = true;
    v.preload = 'metadata';
    v.playsInline = true;
    return v;
  }

  if (AUDIO.test(m)) {
    const a = document.createElement('audio');
    a.src = file.url;
    a.controls = true;
    a.preload = 'metadata';
    return a;
  }

  if (m === 'application/pdf') {
    // <embed> degrades better than <iframe> where no PDF viewer exists.
    const e = document.createElement('embed');
    e.src = file.url;
    e.type = 'application/pdf';
    return e;
  }

  if (TEXTY.test(m)) {
    const source = file.blob || await (await fetch(file.url)).blob();
    const text = await source.slice(0, PREVIEW_TEXT_LIMIT).text();
    const pre = document.createElement('pre');
    pre.textContent = text + (file.size > PREVIEW_TEXT_LIMIT ? '\n\n… truncated' : '');
    return pre;
  }

  return null;
}

/** Native <dialog> gives focus trapping, Escape and an inert backdrop for free. */
export async function openPreviewDialog(file) {
  const node = await buildPreview(file);
  if (!node) return null;

  const dlg = document.createElement('dialog');
  dlg.className = 'preview';

  const bar = document.createElement('div');
  bar.className = 'pvbar';

  const title = document.createElement('strong');
  title.textContent = file.name;
  title.style.flex = '1';

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'btn btn-sm';
  close.textContent = 'Close';
  close.onclick = () => dlg.close();

  bar.append(title, close);

  const body = document.createElement('div');
  body.className = 'pvbody';
  body.append(node);

  dlg.append(bar, body);
  dlg.addEventListener('close', () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}
