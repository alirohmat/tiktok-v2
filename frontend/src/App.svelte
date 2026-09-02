<script>
  import { onMount, onDestroy } from 'svelte';
  import { sseStatus, clipJobs, ytdlpJobs, renders, diagnostics, health, connectSSE, disconnectSSE, fetchHealth, fetchDiagnostics } from './lib/store.js';
  let tab='dl';
  let url='', fmt='mp4', qual='best', noPlaylist=true, info=null, infoLoading=false, dlBusy=false;
  let sse='connecting';
  let clipBusy=false;
  // clip sources + upload
  let sources=[], sourcesLoading=false, srcFilter='';
  let uploadFile=null, uploadBusy=false;
  // storage-stats + manage
  let storageStats=null, storageLoading=false;
  let selectedRenders=new Set(), selectedDownloads=new Set();
  let bulkBusy=false;
  let metaCache={}; // job_id -> meta
  let fromUrlBusy=false;
  const unsub=[];
  onMount(()=>{
    unsub.push(sseStatus.subscribe(v=>sse=v));
    connectSSE('');
    fetchHealth(''); fetchDiagnostics('');
    loadSources(); loadStorageStats();
    const t=setInterval(()=>{ if(!document.hidden) { fetchHealth(''); } }, 30000);
    const t2=setInterval(()=>{ if(!document.hidden && tab==='manage') loadStorageStats(); if(!document.hidden && tab==='clip') loadSources(); }, 15000);
    try{ const s=localStorage.getItem('activeTab'); if(s && ['dl','clip','manage'].includes(s)) tab=s; }catch{}
    return ()=>{ clearInterval(t); clearInterval(t2); };
  });
  onDestroy(()=>{ disconnectSSE(); unsub.forEach(fn=>fn()); });
  function switchTab(which){ tab=which; try{localStorage.setItem('activeTab', which);}catch{} if(which==='clip') loadSources(); if(which==='manage') loadStorageStats(); }
  async function doInfo(){
    if(!url) return;
    infoLoading=true;
    try{
      const r=await fetch('/api/ytdlp/info', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})});
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||j.error||'gagal');
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
      if(!r.ok) throw new Error(j.detail||j.error||'gagal');
      url='';
      setTimeout(loadSources, 1200);
    } catch(e){ alert(e.message); }
    finally{ dlBusy=false; }
  }
  async function doFromUrl(){
    if(!url) return;
    fromUrlBusy=true;
    try{
      const r=await fetch('/clip/from-url', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url, quality: qual, format: fmt, no_playlist: noPlaylist})});
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||j.error||'gagal');
      switchTab('clip');
    } catch(e){ alert(e.message); }
    finally{ fromUrlBusy=false; }
  }
  async function loadSources(){
    sourcesLoading=true;
    try{
      const r=await fetch('/clip/sources');
      const j=await r.json();
      if(r.ok) sources=j.files||j.video_files||j.all_files||[];
    }catch(e){} finally{ sourcesLoading=false; }
  }
  async function loadStorageStats(){
    storageLoading=true;
    try{
      const r=await fetch('/clip/storage-stats');
      const j=await r.json();
      if(r.ok) storageStats=j.stats||j;
      // also refresh diagnostics
      fetchDiagnostics('');
    }catch(e){} finally{ storageLoading=false; }
  }
  async function clipFromFile(name){
    try{
      const r=await fetch('/clip/from-download', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({filename: name})});
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||j.error||'gagal');
      switchTab('clip');
    } catch(e){ alert(e.message); }
  }
  async function doUpload(){
    if(!uploadFile) return alert('Pilih file dulu');
    uploadBusy=true;
    try{
      const fd=new FormData();
      fd.append('file', uploadFile);
      const r=await fetch('/clip', {method:'POST', body: fd});
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||j.error||'gagal');
      uploadFile=null;
      const el=document.getElementById('upload-input'); if(el) el.value='';
      switchTab('clip');
    }catch(e){ alert(e.message); } finally{ uploadBusy=false; }
  }
  async function delRender(job_id, filename){
    if(!confirm(`Hapus ${filename}?`)) return;
    try{
      const r=await fetch(`/renders/${job_id}/${encodeURIComponent(filename)}`, {method:'DELETE'});
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||'gagal');
    }catch(e){ alert(e.message); }
  }
  async function delJobFolder(job_id){
    if(!confirm(`Hapus semua render job ${job_id.slice(0,8)}?`)) return;
    try{
      const r=await fetch(`/clip/renders/${job_id}`, {method:'DELETE'});
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||'gagal');
    }catch(e){ alert(e.message); }
  }
  async function bulkDeleteRenders(){
    const ids=[...selectedRenders];
    if(ids.length===0) return alert('Pilih renders dulu');
    if(!confirm(`Hapus ${ids.length} job renders?`)) return;
    bulkBusy=true;
    try{
      const r=await fetch('/clip/bulk-delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({job_ids: ids, target:'renders'})});
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||'gagal');
      selectedRenders=new Set();
      loadStorageStats();
    }catch(e){ alert(e.message); } finally{ bulkBusy=false; }
  }
  async function bulkDeleteDownloads(){
    const names=[...selectedDownloads];
    if(names.length===0) return alert('Pilih downloads dulu');
    if(!confirm(`Hapus ${names.length} file downloads?`)) return;
    bulkBusy=true;
    try{
      const r=await fetch('/clip/bulk-delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({filenames: names, target:'downloads'})});
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||'gagal');
      selectedDownloads=new Set();
      loadSources(); loadStorageStats();
    }catch(e){ alert(e.message); } finally{ bulkBusy=false; }
  }
  let fetchingMeta=new Set();
  async function fetchMetaSilent(job_id){
    if(metaCache[job_id] || fetchingMeta.has(job_id)) return;
    fetchingMeta.add(job_id);
    try{
      const r=await fetch(`/clip/job-meta/${job_id}`);
      const j=await r.json();
      if(r.ok){ metaCache[job_id]=j; metaCache={...metaCache}; }
    }catch(e){} finally{ fetchingMeta.delete(job_id); }
  }
  function prefetchClipMeta(list){ for(const j of (list||[])){ if(j.status==='SUCCESS' || j.status==='FAILURE' || j.progress>=0.9) fetchMetaSilent(j.job_id); } }
  function prefetchRenderMeta(list){ for(const r of (list||[])) fetchMetaSilent(r.job_id); }
  $: prefetchClipMeta(clipList)
  $: prefetchRenderMeta(rendList)
  async function viewMeta(job_id){
    if(metaCache[job_id]) { metaCache[job_id]=null; return; }
    try{
      const r=await fetch(`/clip/job-meta/${job_id}`);
      const j=await r.json();
      if(!r.ok) throw new Error(j.detail||'gagal');
      metaCache[job_id]=j;
      metaCache={...metaCache};
    }catch(e){ alert(e.message); }
  }
  function toggleRender(id){ if(selectedRenders.has(id)){ selectedRenders.delete(id);} else selectedRenders.add(id); selectedRenders=new Set(selectedRenders); }
  function toggleDownload(name){ if(selectedDownloads.has(name)){ selectedDownloads.delete(name);} else selectedDownloads.add(name); selectedDownloads=new Set(selectedDownloads); }
  $: clipList=$clipJobs
  $: ytdList=$ytdlpJobs
  $: rendList=$renders
  $: diag=$diagnostics
  $: hlth=$health
  $: filteredSources = srcFilter ? sources.filter(s=> (s.name||'').toLowerCase().includes(srcFilter.toLowerCase())) : sources
</script>

<style>
  :global(body){font-family: ui-sans-system, -apple-system, sans-serif}
</style>

<header class="sticky top-0 z-30 bg-zinc-900/80 backdrop-blur border-b border-zinc-800">
  <div class="max-w-[1320px] mx-auto px-4 sm:px-6 py-3 flex items-center gap-3">
    <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-violet-600 to-fuchsia-600 grid place-items-center font-black">t2</div>
    <div><div class="font-extrabold leading-none">tiktok-v2</div><div class="text-xs text-zinc-500">Digital DNA Rebirth • Svelte+SSE</div></div>
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
      <h2 class="font-bold">Download sumber (yt-dlp)</h2>
      <div class="mt-3 flex flex-col sm:flex-row gap-2">
        <input bind:value={url} placeholder="https://youtube.com/watch?v=... atau tiktok.com/..." class="flex-1 px-3 py-2.5 rounded-xl bg-zinc-950 border border-zinc-800 text-sm" />
        <button on:click={doInfo} disabled={infoLoading} class="px-4 py-2.5 rounded-xl bg-zinc-800 text-sm font-bold disabled:opacity-50">Info</button>
        <button on:click={doDownload} disabled={dlBusy} class="px-5 py-2.5 rounded-xl bg-violet-600 text-white text-sm font-bold disabled:opacity-50">{dlBusy ? '...' : 'Download'}</button>
        <button on:click={doFromUrl} disabled={fromUrlBusy} title="Download lalu langsung clip" class="px-5 py-2.5 rounded-xl bg-emerald-600 text-white text-sm font-bold disabled:opacity-50">{fromUrlBusy ? '...' : '⬇+✂'}</button>
      </div>
      <div class="mt-3 flex gap-2 text-xs text-zinc-400 flex-wrap">
        <label class="flex items-center gap-1"><input type="checkbox" bind:checked={noPlaylist} /> no-playlist</label>
        <select bind:value={fmt} class="px-2 py-1 rounded bg-zinc-800 border border-zinc-700"><option value="mp4">mp4</option><option value="webm">webm</option><option value="mp3">mp3</option><option value="m4a">m4a</option></select>
        <select bind:value={qual} class="px-2 py-1 rounded bg-zinc-800 border border-zinc-700"><option value="best">best</option><option value="worst">worst</option><option value="720">720p</option></select>
      </div>
      {#if info}<pre class="mt-4 p-3 rounded-xl bg-zinc-950 border border-zinc-800 text-xs overflow-auto max-h-[260px]">{JSON.stringify(info,null,2)}</pre>{/if}
    </section>
    <section class="mt-6">
      <div class="flex items-center justify-between"><h3 class="font-bold text-sm">Jobs yt-dlp — live SSE</h3><button on:click={()=>fetch('/api/ytdlp/jobs').then(r=>r.json()).then(j=>console.log(j))} class="text-xs text-zinc-500">/api/ytdlp/jobs</button></div>
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
    <section class="mt-6 rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
      <div class="flex items-center justify-between"><h3 class="font-bold text-sm">Sumber siap clip — /clip/sources</h3><button on:click={loadSources} class="text-xs px-3 py-1 rounded-full bg-zinc-800">{sourcesLoading ? '...' : '↻'}</button></div>
      <input bind:value={srcFilter} placeholder="filter nama..." class="mt-3 w-full px-3 py-2 rounded-xl bg-zinc-950 border border-zinc-800 text-sm" />
      {#if filteredSources.length===0}<p class="text-sm text-zinc-500 mt-3">{sourcesLoading ? 'Memuat...' : 'Belum ada file video di downloads.'}</p>
      {:else}
        <div class="mt-3 grid gap-2">
          {#each filteredSources as f}
            <div class="p-3 rounded-xl bg-zinc-950 border border-zinc-800 flex items-center gap-3">
              <div class="min-w-0 flex-1"><div class="text-sm font-mono truncate">{f.name||f.filename||f.path}</div><div class="text-xs text-zinc-500">{f.size_human||''} {f.mtime ? new Date(f.mtime*1000).toLocaleString() : ''}</div></div>
              <button on:click={()=>clipFromFile(f.name||f.filename)} class="px-3 py-1.5 rounded-full bg-violet-600 text-white text-xs font-bold">✂ Clip</button>
            </div>
          {/each}
        </div>
      {/if}
    </section>
  {:else if tab==='clip'}
    <section class="rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
      <h2 class="font-bold">Upload lalu Clip — POST /clip</h2>
      <div class="mt-3 flex flex-col sm:flex-row gap-2">
        <input id="upload-input" type="file" accept=".mp4,.mov,.mkv,.avi,.webm,.m4v,.mp3,.m4a,.opus,.wav" on:change={(e)=> uploadFile=e.target.files[0]} class="flex-1 text-sm file:mr-3 file:px-3 file:py-1.5 file:rounded-full file:border-0 file:bg-zinc-800 file:text-white" />
        <button on:click={doUpload} disabled={uploadBusy||!uploadFile} class="px-5 py-2.5 rounded-xl bg-white text-zinc-900 text-sm font-bold disabled:opacity-50">{uploadBusy ? 'Upload...' : 'Upload & Clip'}</button>
      </div>
      <p class="text-xs text-zinc-500 mt-2">Max 200 MB. Atau pilih dari Sumber di bawah.</p>
      <div class="mt-4">
        <div class="flex items-center justify-between"><h3 class="font-bold text-sm">Sumber — pilih file</h3><button on:click={loadSources} class="text-xs px-3 py-1 rounded-full bg-zinc-800">{sourcesLoading ? '...' : '↻'}</button></div>
        <input bind:value={srcFilter} placeholder="filter..." class="mt-2 w-full px-3 py-2 rounded-xl bg-zinc-950 border border-zinc-800 text-sm" />
        {#if filteredSources.length===0}<p class="text-sm text-zinc-500 mt-2">{sourcesLoading ? 'Memuat...' : 'Kosong'}</p>
        {:else}<div class="mt-2 grid gap-2 max-h-[260px] overflow-auto pr-1">{#each filteredSources as f}<div class="p-2.5 rounded-xl bg-zinc-950 border border-zinc-800 flex items-center gap-2"><span class="text-xs font-mono truncate flex-1">{f.name||f.filename}</span><button on:click={()=>clipFromFile(f.name||f.filename)} class="px-3 py-1 rounded-full bg-violet-600 text-white text-xs">✂</button></div>{/each}</div>{/if}
      </div>
    </section>
    <section class="mt-6 rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
      <h2 class="font-bold">Clip jobs — live SSE</h2>
      <p class="text-xs text-zinc-500 mt-1">SSE push setiap 1.5s saat berubah, heartbeat keep-alive. Visibility pause.</p>
      {#if clipList.length===0}<p class="text-sm text-zinc-500 mt-3">Belum ada clip. Upload atau pilih sumber lalu Clip.</p>
      {:else}
        <div class="mt-4 grid gap-3">
          {#each clipList as j (j.job_id)}
            <div class="p-4 rounded-2xl border {j.status==='FAILURE' ? 'border-red-900 bg-red-950/20' : j.status==='SUCCESS' ? 'border-emerald-900 bg-emerald-950/10' : 'border-zinc-800 bg-zinc-900'}">
              <div class="flex items-center justify-between gap-2">
                <span class="text-xs font-mono truncate">{j.job_id}</span>
                <span class="text-xs px-2 py-1 rounded-full {j.status==='SUCCESS' ? 'bg-emerald-900 text-emerald-200' : j.status==='FAILURE' ? 'bg-red-900 text-red-200' : 'bg-zinc-800'}">{j.status} {Math.round((j.progress||0)*100)}%</span>
              </div>
              <div class="mt-2 h-2 rounded-full bg-zinc-800 overflow-hidden"><div class="h-full bg-violet-600 transition-all" style="width: {Math.min(100, Math.round((j.progress||0)*100))}%;"></div></div>
              <div class="mt-1 text-xs text-zinc-500">{j.phase||''}</div>
              {#if j.error}<div class="mt-2 p-2 rounded bg-red-950/30 border border-red-900 text-xs break-all">{j.error}</div>{/if}
              {#if j.result}<div class="mt-2 text-xs">Outputs: {j.result.length} file</div>{/if}
              {#if j.logs && j.logs.length}<details class="mt-2"><summary class="text-xs text-zinc-400 cursor-pointer">logs {j.logs.length}</summary><pre class="mt-1 p-2 rounded bg-zinc-950 border border-zinc-800 text-[11px] overflow-auto max-h-[160px]">{j.logs.slice(-80).join('\n')}</pre></details>{/if}
              {#if metaCache[j.job_id] && metaCache[j.job_id].captions}
                <div class="mt-2 p-2 rounded-xl bg-zinc-950 border border-zinc-800 text-xs">
                  {#each Object.entries(metaCache[j.job_id].captions).slice(0,3) as [fname, cap]}
                    <div class="truncate"><span class="text-zinc-500">{fname.slice(0,22)}:</span> {cap.slice(0,90)}{cap.length>90?'…':''}</div>
                  {/each}
                  {#if metaCache[j.job_id].engagement}
                    <div class="mt-2 flex flex-wrap gap-1 items-center">
                      <span class="px-2 py-0.5 rounded-full text-[11px] font-bold border {metaCache[j.job_id].engagement.niche_score>=80 ? 'bg-emerald-950 border-emerald-800 text-emerald-200' : metaCache[j.job_id].engagement.niche_score>=60 ? 'bg-amber-950 border-amber-800 text-amber-200' : 'bg-zinc-800 border-zinc-700 text-zinc-300'}">{metaCache[j.job_id].engagement.niche_tag||'umum'} {metaCache[j.job_id].engagement.niche_profit_tier||''} • {metaCache[j.job_id].engagement.niche_score??70}/100 {metaCache[j.job_id].engagement.niche_mode ? '('+metaCache[j.job_id].engagement.niche_mode+')' : ''}</span>
                      {#if metaCache[j.job_id].engagement.niche_advisory}<span class="text-[11px] text-zinc-400">{metaCache[j.job_id].engagement.niche_advisory}</span>{/if}
                    </div>
                    <div class="mt-1 text-[11px] text-zinc-500 line-clamp-1">{(metaCache[j.job_id].engagement.comments||[]).slice(0,1).join(' ')}</div>
                  {/if}
                </div>
              {/if}
              <div class="mt-2 flex gap-2"><button on:click={()=>viewMeta(j.job_id)} class="text-xs px-3 py-1 rounded-full bg-zinc-800">{metaCache[j.job_id] ? 'Tutup meta' : 'Meta CTA/SEO'}</button><a href="/jobs/{j.job_id}" target="_blank" class="text-xs px-3 py-1 rounded-full bg-zinc-800">/jobs/{j.job_id.slice(0,8)}</a></div>
              {#if metaCache[j.job_id]}<details class="mt-2"><summary class="text-[11px] text-zinc-500 cursor-pointer">raw JSON</summary><pre class="mt-1 p-2 rounded bg-zinc-950 border border-zinc-800 text-[11px] overflow-auto max-h-[200px]">{JSON.stringify(metaCache[j.job_id], null, 2)}</pre></details>{/if}
            </div>
          {/each}
        </div>
      {/if}
    </section>
    <section class="mt-6">
      <h3 class="font-bold text-sm">Renders SSE — /clip/renders + /renders/&#123;job&#125;/&#123;file&#125;</h3>
      {#if rendList.length===0}<p class="text-sm text-zinc-500 mt-2">Belum ada renders.</p>
      {:else}
        <div class="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {#each rendList as r}
            <div class="p-3 rounded-2xl border border-zinc-800 bg-zinc-900">
              <div class="text-xs font-mono truncate">{r.filename}</div>
              <div class="text-xs text-zinc-500">{r.job_id.slice(0,8)} • {(r.size/1024/1024).toFixed(1)} MB</div>
              <div class="mt-2 flex gap-1 flex-wrap">
                <a href="/renders/{r.job_id}/{r.filename}" class="px-3 py-1 rounded-full bg-violet-600 text-white text-xs">Download</a>
                <a href="/api/renders/{r.job_id}/{r.filename}" class="px-2 py-1 rounded-full bg-zinc-800 text-xs">/api</a>
                <button on:click={()=>delRender(r.job_id, r.filename)} class="px-2 py-1 rounded-full bg-red-900 text-red-200 text-xs">Hapus</button>
                <button on:click={()=>viewMeta(r.job_id)} class="px-2 py-1 rounded-full bg-zinc-800 text-xs">Meta</button>
              </div>
              {#if metaCache[r.job_id] && metaCache[r.job_id].captions}
                <div class="mt-2 p-1.5 rounded bg-zinc-950 border border-zinc-800 text-[11px] leading-tight">
                  {#each Object.values(metaCache[r.job_id].captions).slice(0,1) as cap}
                    <div class="line-clamp-2">{cap.slice(0,120)}{cap.length>120?'…':''}</div>
                  {/each}
                </div>
              {/if}
              {#if metaCache[r.job_id] && metaCache[r.job_id].engagement}
                <div class="mt-1 flex flex-wrap gap-1 items-center">
                  <span class="px-2 py-0.5 rounded-full text-[10px] font-bold border {metaCache[r.job_id].engagement.niche_score>=80 ? 'bg-emerald-950 border-emerald-800 text-emerald-200' : metaCache[r.job_id].engagement.niche_score>=60 ? 'bg-amber-950 border-amber-800 text-amber-200' : 'bg-zinc-800 border-zinc-700 text-zinc-300'}">{metaCache[r.job_id].engagement.niche_tag||'umum'} {metaCache[r.job_id].engagement.niche_profit_tier||''} {metaCache[r.job_id].engagement.niche_score??70}</span>
                  <span class="text-[10px] text-zinc-500 truncate max-w-[160px]">{metaCache[r.job_id].engagement.niche_advisory||''}</span>
                </div>
              {/if}
              {#if metaCache[r.job_id]}<details class="mt-1"><summary class="text-[11px] text-zinc-600 cursor-pointer">meta</summary><pre class="p-2 rounded bg-zinc-950 text-[11px] overflow-auto max-h-[160px]">{JSON.stringify(metaCache[r.job_id], null, 2)}</pre></details>{/if}
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
        {#if diag.quota_error}<div class="mt-2 p-2 rounded bg-amber-950/30 border border-amber-900 text-xs">{diag.quota_error}</div>{/if}
      {:else}<p class="text-sm text-zinc-500 mt-2">Memuat diagnostics...</p>{/if}
      {#if storageStats}
        <div class="mt-4 p-3 rounded-xl bg-zinc-950 border border-zinc-800 text-xs">
          <div class="font-bold">/clip/storage-stats</div>
          <div class="mt-1 grid grid-cols-2 gap-1">{#each Object.entries(storageStats) as [k,v]}<div>{k}: {v.human||v.bytes} ({v.files} files)</div>{/each}</div>
        </div>
      {/if}
      <div class="mt-4 flex gap-2"><button on:click={loadStorageStats} class="px-3 py-1.5 rounded-full bg-zinc-800 text-xs">{storageLoading ? '...' : '↻ Refresh'}</button><button on:click={bulkDeleteRenders} disabled={bulkBusy||selectedRenders.size===0} class="px-3 py-1.5 rounded-full bg-red-900 text-red-200 text-xs disabled:opacity-50">Hapus {selectedRenders.size} renders terpilih</button></div>
      <h3 class="font-bold text-sm mt-6">Renders — pilih untuk bulk delete</h3>
      {#if rendList.length===0}<p class="text-sm text-zinc-500 mt-2">Belum ada renders.</p>
      {:else}
        <div class="mt-3 grid gap-2">
          {#each rendList as r}
            <div class="p-3 rounded-xl bg-zinc-950 border border-zinc-800 flex items-center gap-3">
              <input type="checkbox" checked={selectedRenders.has(r.job_id)} on:change={()=>toggleRender(r.job_id)} />
              <div class="min-w-0 flex-1"><div class="text-sm font-mono truncate">{r.filename}</div><div class="text-xs text-zinc-500">{r.job_id} • {(r.size/1024/1024).toFixed(1)} MB</div></div>
              <a href="/renders/{r.job_id}/{r.filename}" class="px-3 py-1 rounded-full bg-zinc-800 text-xs">Unduh</a>
              <button on:click={()=>delRender(r.job_id, r.filename)} class="px-2 py-1 rounded-full bg-red-900/50 text-xs">Hapus</button>
            </div>
          {/each}
        </div>
      {/if}
      <h3 class="font-bold text-sm mt-6">Downloads — pilih untuk bulk delete</h3>
      <div class="flex gap-2 mt-2"><button on:click={bulkDeleteDownloads} disabled={bulkBusy||selectedDownloads.size===0} class="px-3 py-1.5 rounded-full bg-red-900 text-red-200 text-xs disabled:opacity-50">Hapus {selectedDownloads.size} downloads</button><button on:click={loadSources} class="px-3 py-1.5 rounded-full bg-zinc-800 text-xs">↻ Sources</button></div>
      {#if sources.length===0}<p class="text-sm text-zinc-500 mt-2">Tidak ada downloads.</p>
      {:else}
        <div class="mt-3 grid gap-2">
          {#each sources as f}
            <div class="p-3 rounded-xl bg-zinc-950 border border-zinc-800 flex items-center gap-3">
              <input type="checkbox" checked={selectedDownloads.has(f.name||f.filename)} on:change={()=>toggleDownload(f.name||f.filename)} />
              <div class="min-w-0 flex-1"><div class="text-sm font-mono truncate">{f.name||f.filename}</div><div class="text-xs text-zinc-500">{f.size_human||''}</div></div>
              <button on:click={()=>clipFromFile(f.name||f.filename)} class="px-2 py-1 rounded-full bg-violet-600 text-white text-xs">✂</button>
            </div>
          {/each}
        </div>
      {/if}
      <h3 class="font-bold text-sm mt-6">Bulk by age — POST /clip/bulk-delete</h3>
      <p class="text-xs text-zinc-500">Hapus file lama &gt; N hari via bulk-delete older_than_days.</p>
    </section>
  {/if}
</main>
