import { writable, derived } from 'svelte/store';

export const sseStatus = writable('connecting'); // connecting|open|closed|error
export const clipJobs = writable([]);
export const ytdlpJobs = writable([]);
export const renders = writable([]);
export const diagnostics = writable(null);
export const health = writable(null);

// derived: has running
export const hasRunning = derived([clipJobs, ytdlpJobs], ([$c, $y]) =>
  [...$c, ...$y].some(j => ['downloading','queued','PENDING','STARTED','PROCESSING'].includes(j.status))
);

let es = null;
let retryTimer = null;
let explicitClose = false;

export function connectSSE(base = '') {
  explicitClose = false;
  disconnectSSE();
  const url = (base || '') + '/clip/events';
  sseStatus.set('connecting');
  try {
    es = new EventSource(url);
    es.onopen = () => sseStatus.set('open');
    es.onerror = () => {
      sseStatus.set('error');
      es?.close();
      if (!explicitClose && !document.hidden) {
        clearTimeout(retryTimer);
        retryTimer = setTimeout(() => connectSSE(base), 2500);
      }
    };
    es.onmessage = (e) => {
      try {
        const payload = JSON.parse(e.data);
        if (payload.clip_jobs) clipJobs.set(payload.clip_jobs);
        if (payload.ytdlp_jobs) ytdlpJobs.set(payload.ytdlp_jobs);
        if (payload.renders) renders.set(payload.renders);
        if (payload.disk) diagnostics.set(payload);
        // health is separate REST, but ffmpeg quick flag here
      } catch {}
    };
  } catch (e) {
    sseStatus.set('error');
    clearTimeout(retryTimer);
    retryTimer = setTimeout(() => connectSSE(base), 3000);
  }
  // pause when hidden
  const onVis = () => {
    if (document.hidden) {
      if (es) { try{ es.close(); }catch{} sseStatus.set('closed'); }
    } else {
      if (!es || es.readyState === 2) connectSSE(base);
    }
  };
  document.addEventListener('visibilitychange', onVis);
}

export function disconnectSSE() {
  explicitClose = true;
  if (es) { try{ es.close(); }catch{} es = null; }
  clearTimeout(retryTimer);
  sseStatus.set('closed');
}

export async function fetchHealth(base='') {
  try {
    const r = await fetch((base||'')+'/health');
    const j = await r.json();
    health.set(j);
    return j;
  } catch(e) { health.set({status:'error', error:String(e)}); return null; }
}

export async function fetchDiagnostics(base='') {
  try {
    const r = await fetch((base||'')+'/clip/diagnostics');
    const j = await r.json();
    diagnostics.set(j);
    return j;
  } catch(e){ return null; }
}
