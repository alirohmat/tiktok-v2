<script>
  import { onMount, onDestroy } from 'svelte';
  import { sseStatus, clipJobs, ytdlpJobs, renders, diagnostics, health, connectSSE, disconnectSSE, fetchHealth, fetchDiagnostics } from './lib/store.js';
  let tab='dl';
  let url='', fmt='mp4', qual='best', noPlaylist=true, info=null, infoLoading=false, dlBusy=false;
  let sse='connecting';
  let clipBusy=false;
  const unsub=[];
  onMount(()=>{
    unsub.push(sseStatus.subscribe(v=>sse=v));
    connectSSE('');
    fetchHealth('');
    fetchDiagnostics('');
    const t=setInterval(()=>{ if(!document.hidden) fetchHealth(''); }, 30000);
    // restore tab
    try{ const s=localStorage.getItem('activeTab'); if(s && ['dl','clip','manage'].includes(s)) tab=s; }catch{}
    return ()=>{ clearInterval(t); };
  });
  onDestroy(()=>{ disconnectSSE(); unsub.forEach(fn=>fn()); });
  function switchTab(which){ tab=which; try{localStorage.setItem('activeTab', which);}catch{} }
  async function doInfo(){
    if(!url) return;
    infoLoading=true;
    try{
      const r=await fetch('/api/ytdlp/info', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})});
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||'gagal');
      info=j.info||j.data||j;
    } catch(e){ alert(e.message); }
    finally{ infoLoading=false; }
  }
  async function doDownload(){
    if(!url) return;
    dlBusy=true;
    try{
      const r=await fetch('/api/ytdlp/download', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url, format: fmt, quality: qual, no_playlist: noPlaylist})});
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||'gagal');
    } catch(e){ alert(e.message); }
    finally{ dlBusy=false; }
  }
  async function clipFromFile(name){
    try{
      const r=await fetch('/clip/from-download', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({filename: name})});
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||'gagal');
      switchTab('clip');
    } catch(e){ alert(e.message); }
  }
  $: clipList=$clipJobs
  $: ytdList=$ytdlpJobs
  $: rendList=$renders
  $: diag=$diagnostics
  $: hlth=$health
</script>

<style>
  :global(body){font-family: ui-sans-system, -apple-system, sans-serif}
</style>

<header class="sticky top-0 z-30 bg-zinc-900/80 backdrop-blur border-b border-zinc-800">
  <div class="max-w-[1320px] mx-auto px-4 sm:px-6 py-3 flex items-center gap-3">
    <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-violet-600 to-fuchsia-600 grid place-items-center font-black">t2</div>
    <div><div class="font-extrabold leading-none">tiktok-v2</div><div class="text-xs text-zinc-500">Digital DNA Rebirth • Svelte+SSe</div></div>
    <div class="ml-auto flex items-center gap-2 text-xs">
      <span class="px-2 py-1 rounded-full border text-[11px] {sse==='open' ? 'bg-emerald-950 border-emerald-800 text-emerald-200' : sse==='connecting' ? 'bg-zinc-800 border-zinc-700' : 'bg-red-950 border-red-900 text-red-200'}">SSE {sse}</span>
      {#if hlth}<span class="hidden sm:inline text-zinc-500">ffmpeg {hlth.ffmpeg||'?'} • redis {hlth.redis||'?'}</span>{/if}
    </div>
  </div>
  <div class="max-w-[1320px] mx-auto px-4 sm:px-6 pb-3 flex gap-2">
    <button on:click={()=>switchTab('dl')} class="px-5 py-2 rounded-full border text-sm font-bold {tab==='dl' ? 'bg-white text-zinc-900 border-white' : 'bg-zinc-900 border-zinc-800 hover:bg-zinc-800'}">⬇ Download</button>
    <button on:click={()=>switchTab('clip')} class="px-5 py-2 rounded-full border text-sm font-bold {tab==='clip' ? 'bg-white text-zinc-900 border-white' : 'bg-zinc-900 border-zinc-800 hover:bg-zinc-800'}">✂ Clipper</button>
    <button on:click={()=>switchTab('manage')} class="px-5 py-2 rounded-full border text-sm font-bold {tab==='manage' ? 'bg-white text-zinc-900 border-white' : 'bg-zinc-900 border-zinc-800 hover:bg-zinc-800'}">🗂 Kelola</button>
  </div>
</header>

<main class="max-w-[1320px] mx-auto px-4 sm:px-6 py-6">
  {#if sse==='error'}<div class="mb-4 p-3 rounded-xl bg-red-950/30 border border-red-900 text-sm">SSE terputus — reconnect otomatis. Jika terus error, cek /health atau refresh.</div>{/if}
  {#if diag && diag.quota_error}<div class="mb-4 p-3 rounded-xl bg-amber-950/30 border border-amber-900 text-sm">Quota: {diag.quota_error}</div>{/if}

  {#if tab==='dl'}
    <section class="rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
      <h2 class="font-bold">Download sumber (ytdlp)</h2>
      <div class="mt-3 flex flex-col sm:flex-row gap-2">
        <input bind:value={url} placeholder="https://youtube.com/watch?v=... atau tiktok.com/..." class="flex-1 px-3 py-2.5 rounded-xl bg-zinc-950 border border-zinc-800 text-sm" />
        <button on:click={doInfo} disabled={infoLoading} class="px-4 py-2.5 rounded-xl bg-zinc-800 text-sm font-bold disabled:opacity-50">Info</button>
        <button on:click={doDownload} disabled={dlBusy} class="px-5 py-2.5 rounded-xl bg-violet-600 text-white text-sm font-bold disabled:opacity-50">Download</button>
      </div>
      <div class="mt-3 flex gap-2 text-xs text-zinc-400">
        <label class="flex items-center gap-1"><input type="checkbox" bind:checked={noPlaylist} /> no-playlist</label>
        <select bind:value={fmt} class="px-2 py-1 rounded bg-zinc-800 border border-zinc-700"><option value="mp4">mp4</option><option value="webm">webm</option><option value="mp3">mp3</option></select>
        <select bind:value={qual} class="px-2 py-1 rounded bg-zinc-800 border border-zinc-700"><option value="best">best</option><option value="worst">worst</option></select>
      </div>
      {#if info}<pre class="mt-4 p-3 rounded-xl bg-zinc-950 border border-zinc-800 text-xs overflow-auto max-h-[220px]">{JSON.stringify(info,null,2)}</pre>{/if}
    </section>
    <section class="mt-6">
      <h3 class="font-bold text-sm">Jobs ytdlp — live SSE</h3>
      {#if ytdList.length===0}<p class="text-sm text-zinc-500 mt-2">Belum ada job. Download untuk melihat progress live.</p>
      {:else}
        <div class="mt-3 grid gap-3">
          {#each ytdList as j (j.job_id)}
            <div class="p-4 rounded-2xl border {j.status==='error' ? 'border-red-900 bg-red-950/20' : j.status==='completed' ? 'border-emerald-900 bg-emerald-950/10' : 'border-zinc-800 bg-zinc-900'}">
              <div class="flex gap-3">
                <div class="w-10 h-10 rounded-xl {j.status==='completed' ? 'bg-emerald-900' : j.status==='error' ? 'bg-red-900' : 'bg-zinc-800'} grid place-items-center text-sm">{j.status==='completed' ? '✓' : j.status==='error' ? '!' : '⬇'}</div>
                <div class="min-w-0 flex-1">
                  <div class="text-sm font-semibold truncate">{j.filename||j.url}</div>
                  <div class="text-xs text-zinc-500 truncate">{j.url}</div>
                  <div class="mt-2 h-2 rounded-full bg-zinc-800 overflow-hidden"><div class="h-full bg-violet-600 transition-all" style="width: {Math.round((j.progress||0)*100)}%"></div></div>
                  {#if j.error}<div class="mt-2 p-2 rounded bg-red-950/30 border border-red-900 text-xs break-all">{j.error}</div>{/if}
                  {#if j.filename && j.status==='completed'}<button on:click={()=>clipFromFile(j.filename)} class="mt-2 px-3 py-1.5 rounded-full bg-white text-zinc-900 text-xs font-bold">✂ Jadikan Clipper</button>{/if}
                </div>
                <span class="text-xs px-2 py-1 rounded-full bg-zinc-800 h-fit">{j.status} {Math.round((j.progress||0)*100)}%</span>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </section>
  {:else if tab==='clip'}
    <section class="rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
      <h2 class="font-bold">Clip jobs — live SSE</h2>
      <p class="text-xs text-zinc-500 mt-1">SSE push setiap 1.5s saat berubah, heartbeat keep-alive. Visibility pause.</p>
      {#if clipList.length===0}<p class="text-sm text-zinc-500 mt-3">Belum ada clip. Download lalu Jadikan Clipper.</p>
      {:else}
        <div class="mt-4 grid gap-3">
          {#each clipList as j (j.job_id)}
            <div class="p-4 rounded-2xl border {j.status==='FAILURE' ? 'border-red-900 bg-red-950/20' : j.status==='SUCCESS' ? 'border-emerald-900 bg-emerald-950/10' : 'border-zinc-800 bg-zinc-900'}">
              <div class="flex items-center justify-between gap-2">
                <span class="text-xs font-mono truncate">{j.job_id}</span>
                <span class="text-xs px-2 py-1 rounded-full {j.status==='SUCCESS' ? 'bg-emerald-900 text-emerald-200' : j.status==='FAILURE' ? 'bg-red-900 text-red-200' : 'bg-zinc-800'}">{j.status} {Math.round((j.progress||0)*100)}%</span>
              </div>
              <div class="mt-2 h-2 rounded-full bg-zinc-800 overflow-hidden"><div class="h-full bg-violet-600 transition-all" style="width: {Math.min(100, Math.round((j.progress||0)*100))}%"></div></div>
              <div class="mt-1 text-xs text-zinc-500">{j.phase||''}</div>
              {#if j.error}<div class="mt-2 p-2 rounded bg-red-950/30 border border-red-900 text-xs break-all">{j.error}</div>{/if}
              {#if j.result}<div class="mt-2 text-xs">Outputs: {j.result.length} file — lihat Kelola / Renders</div>{/if}
            </div>
          {/each}
        </div>
      {/if}
    </section>
    <section class="mt-6">
      <h3 class="font-bold text-sm">Renders SSE</h3>
      {#if rendList.length===0}<p class="text-sm text-zinc-500 mt-2">Belum ada renders.</p>
      {:else}
        <div class="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {#each rendList as r}
            <div class="p-3 rounded-2xl border border-zinc-800 bg-zinc-900">
              <div class="text-xs font-mono truncate">{r.filename}</div>
              <div class="text-xs text-zinc-500">{r.job_id.slice(0,8)} • {(r.size/1024/1024).toFixed(1)} MB</div>
              <a href="/renders/{r.job_id}/{r.filename}" class="mt-2 inline-block px-3 py-1 rounded-full bg-violet-600 text-white text-xs">Download</a>
              <a href="/api/renders/{r.job_id}/{r.filename}" class="ml-1 text-xs text-zinc-400">/api</a>
            </div>
          {/each}
        </div>
      {/if}
    </section>
  {:else}
    <section class="rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
      <h2 class="font-bold">Kelola File — storage & renders</h2>
      {#if diag}
        <div class="mt-3 grid grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
          {#each Object.entries(diag.storage_stats||{}) as [k,v]}
            <div class="p-3 rounded-xl bg-zinc-950 border border-zinc-800"><div class="font-bold">{k}</div><div>{v?.human||''} {v?.files||0} files</div></div>
          {/each}
        </div>
        {#if diag.disk}<div class="mt-3 text-xs text-zinc-500">Disk root {diag.disk.root?.used_pct||'?'}% used • storage {(diag.disk.storage?.free_gb||'?')} GB free</div>{/if}
      {:else}<p class="text-sm text-zinc-500 mt-2">Memuat diagnostics...</p>{/if}
      <div class="mt-4 grid gap-3">
        {#each rendList as r}
          <div class="p-3 rounded-xl bg-zinc-950 border border-zinc-800 flex items-center gap-3">
            <div class="min-w-0 flex-1"><div class="text-sm font-mono truncate">{r.filename}</div><div class="text-xs text-zinc-500">{r.job_id}</div></div>
            <a href="/renders/{r.job_id}/{r.filename}" class="px-3 py-1 rounded-full bg-zinc-800 text-xs">Unduh</a>
          </div>
        {/each}
      </div>
    </section>
  {/if}
</main>
