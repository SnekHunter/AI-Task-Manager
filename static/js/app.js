// App JS extracted from index.html
(function(){
  const API_BASE = (
    location.hostname === 'localhost' ||
    location.hostname === '127.0.0.1' ||
    location.hostname === '' || // file:// -> hostname is empty
    location.protocol === 'file:'
  ) ? 'http://127.0.0.1:5000' : '';

  const fullUrl = (path) => API_BASE ? (API_BASE + path) : new URL(path, location.origin).toString();
  const API = {
    list: (params = {}) => {
      const u = new URL(fullUrl('/v1/tasks'));
      if (params.completed !== undefined && params.completed !== 'all') u.searchParams.set('completed', String(params.completed));
      u.searchParams.set('sort', 'short_id');
      return fetch(u.toString());
    },
    add: (title) => fetch(fullUrl('/v1/tasks'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }) }),
    toggle: (id, completed) => fetch(fullUrl('/v1/tasks/' + encodeURIComponent(id)), { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ completed }) }),
    del: (id) => fetch(fullUrl('/v1/tasks/' + encodeURIComponent(id)), { method: 'DELETE' }),
    delete_all: (confirm=true) => fetch(fullUrl('/v1/tasks?confirm=' + (confirm ? 'true' : 'false')), { method: 'DELETE' }),
    // chat will use fetchWithFallback below
    chat: null,
    // restore deleted items using the server undo token
    restore: (token) => fetch(fullUrl('/v1/undo/restore'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) }),
  };

  // Resilient fetch that tries the primary origin then falls back to localhost:5000
  async function fetchWithFallback(path, opts){
    try{
      return await fetch(fullUrl(path), opts);
    }catch(err){
      console.warn('Primary fetch failed, attempting fallback to http://127.0.0.1:5000', err);
      try{
        const alt = 'http://127.0.0.1:5000' + path;
        return await fetch(alt, opts);
      }catch(err2){
        console.warn('Fallback fetch failed', err2);
        throw err; // rethrow original
      }
    }
  }

  // wire chat to resilient fetch
  API.chat = (message) => fetchWithFallback('/v1/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message }) });

  // Removal animation timing
  const REMOVE_STAGGER = 80; const REMOVE_ANIM_MS = 360;
  function removalWait(count=1) { const total = REMOVE_ANIM_MS + Math.max(0, (count - 1)) * REMOVE_STAGGER + 60; return new Promise(res => setTimeout(res, total)); }

  // State
  let tasks = []; let filter = 'all'; let searchTerm = ''; let lastDeleted = null; let lastDeletedAll = null; let lastDeletedAllToken = null; let searchExpanded = false;

  // Utilities
  const qs = (sel, el=document) => el.querySelector(sel);
  const qsa = (sel, el=document) => Array.from(el.querySelectorAll(sel));
  const debounce = (fn, ms=200) => { let t; return (...args)=>{ clearTimeout(t); t=setTimeout(()=>fn(...args), ms); }; };

  function computeNumberingMap(list) { const ordered = list.slice().sort((a,b)=>{ const sa = parseInt(a.short_id||0,10); const sb = parseInt(b.short_id||0,10); if (sa!==sb) return sa-sb; return new Date(a.created_at)-new Date(b.created_at); }); const map={}; ordered.forEach((t,i)=>{ map[t.id]=i+1; }); return map; }

  function showSnackbar(msg, actionLabel, onAction) { const bar=qs('#snackbar'); const text=qs('#snackbarMsg'); const action=qs('#snackbarAction'); text.textContent=msg; if (actionLabel && onAction) { action.textContent=actionLabel; action.style.display='inline'; action.onclick = ()=>{ onAction(); hide(); }; } else { action.style.display='none'; action.onclick=null; } bar.classList.add('show'); let timer = setTimeout(hide,5000); function hide(){ bar.classList.remove('show'); clearTimeout(timer); } }

  // Theme helpers (moved from inline HTML) — ensure dark mode works after extraction
  function setTheme(mode){
    if (!mode) return;
    document.documentElement.setAttribute('data-theme', mode);
    try{ localStorage.setItem('todo.theme', mode); }catch(e){}
  }
  function initTheme(){
    try{
      const stored = localStorage.getItem('todo.theme');
      setTheme(stored || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
    }catch(e){ setTheme('light'); }
  }

  function setSearchExpanded(expanded) { const container=qs('.search'); const clearCompletedBtn=qs('#clearCompleted'); const deleteAllBtn=qs('#deleteAllBtn'); if (!container) return; searchExpanded=!!expanded; if (searchExpanded) { container.classList.remove('collapsed'); container.classList.add('expanded'); const clr=qs('#clearSearch'); if (clr){ clr.style.opacity='1'; clr.style.pointerEvents='auto'; } if (clearCompletedBtn) clearCompletedBtn.style.display='none'; if (deleteAllBtn) deleteAllBtn.style.display='none'; const inp=qs('#search'); if (inp) inp.focus(); } else { container.classList.remove('expanded'); container.classList.add('collapsed'); const clr=qs('#clearSearch'); if (clr){ clr.style.opacity='0'; clr.style.pointerEvents='none'; } if (clearCompletedBtn) clearCompletedBtn.style.display=''; if (deleteAllBtn) deleteAllBtn.style.display=''; } }
  function toggleSearch(){ setSearchExpanded(!searchExpanded); }

  function render(){ const list=qs('#list'); const frag=document.createDocumentFragment(); const filtered = tasks.filter(t=>{ if (filter==='active' && t.completed) return false; if (filter==='completed' && !t.completed) return false; if (searchTerm) return t.title.toLowerCase().includes(searchTerm); return true; }); const numberingMap=computeNumberingMap(tasks); const removedInView = filtered.filter(x=>x._removed).map(x=>x.id); qs('#count').textContent=`${filtered.length} item${filtered.length!==1?'s':''}`; const hasCompleted = tasks.some(t=>t.completed); const clearCompletedBtn=qs('#clearCompleted'); if (clearCompletedBtn) clearCompletedBtn.disabled = !hasCompleted; const deleteAllBtn=qs('#deleteAllBtn'); if (deleteAllBtn) deleteAllBtn.disabled = tasks.length===0; if (filtered.length===0){ const empty=document.createElement('div'); empty.className='empty'; empty.textContent = searchTerm ? 'Nothing matches your filters.' : 'No tasks yet.'; frag.appendChild(empty); list.replaceChildren(frag); return; } for (const t of filtered){ const row=document.createElement('div'); row.className='row'; row.setAttribute('role','listitem'); if (t._new) row.classList.add('item-new'); if (t._removed){ row.classList.add('item-removed'); const idx = removedInView.indexOf(t.id); const delay = Math.max(0, idx) * REMOVE_STAGGER; row.style.animationDelay = `${delay}ms`; row.style.animationDuration = `${REMOVE_ANIM_MS}ms`; } else { row.style.animationDelay=''; row.style.animationDuration=''; } const cb=document.createElement('input'); cb.type='checkbox'; cb.className='checkbox'; cb.checked = !!t.completed; cb.setAttribute('aria-label','Toggle completed'); cb.addEventListener('change', async ()=>{ const prev = t.completed; t.completed = cb.checked; row.querySelector('.title').classList.toggle('completed', t.completed); try{ const r = await API.toggle(t.id, t.completed); if (!r.ok) throw new Error('toggle failed'); const updated = await r.json(); const idx = tasks.findIndex(x=>x.id===updated.id); if (idx!==-1) tasks[idx]=updated; render(); }catch(e){ t.completed=prev; cb.checked=prev; row.querySelector('.title').classList.toggle('completed', t.completed); showSnackbar('Could not update task.'); } }); const span=document.createElement('div'); span.className = 'title' + (t.completed ? ' completed' : ''); span.textContent = t.title; if (t.short_id !== undefined){ const pill = document.createElement('span'); pill.className='pill'; const disp = numberingMap[t.id] || t.short_id || ''; pill.textContent = '#'+disp; span.prepend(pill); } const actions=document.createElement('div'); actions.className='row-actions'; const del=document.createElement('button'); del.className='icon-btn-mini'; del.title='Delete'; del.innerHTML='🗑️'; del.type='button'; del.addEventListener('click', ()=> onDelete(t)); actions.appendChild(del); row.appendChild(cb); row.appendChild(span); row.appendChild(actions); frag.appendChild(row); } list.replaceChildren(frag); }

  async function refresh(){ const list=qs('#list'); list.innerHTML=''; for (let i=0;i<3;i++){ const s=document.createElement('div'); s.className='skeleton'; list.appendChild(s);} try{ const r = await API.list({ completed: filter==='all' ? 'all' : (filter==='completed') }); if (!r.ok) throw new Error('list failed'); const data = await r.json(); tasks = data.items || []; }catch(e){ tasks=[]; showSnackbar('Could not load tasks.'); } render(); }

  async function onAdd(){ const input=qs('#newTitle'); if (!input) return; const title=(input.value||'').trim(); if(!title) return; input.disabled=true; const addBtn=qs('#addBtn'); if (addBtn) addBtn.disabled=true; try{ const r=await API.add(title); if (!r.ok) throw new Error('add failed'); const created = await r.json(); created._new=true; tasks.push(created); tasks.sort((a,b)=>(parseInt(a.short_id||0,10)-parseInt(b.short_id||0,10))); input.value=''; render(); setTimeout(()=>{ created._new=false; render(); }, 700); }catch(e){ showSnackbar('Could not add task.'); } finally{ input.disabled=false; if (addBtn) addBtn.disabled=false; input.focus(); } }

  async function onDelete(task){ const prevTasks = tasks.slice(); const idx = tasks.findIndex(t=>t.id===task.id); if (idx===-1) return; tasks[idx]._removed=true; render(); try{ const r = await API.del(task.id); if (!r.ok) throw new Error('delete failed'); const data = await r.json(); await removalWait(1); tasks = tasks.filter(t=>t.id!==task.id); const map=computeNumberingMap(tasks); tasks.forEach(t=>{ t.short_id = map[t.id] || t.short_id; }); render(); lastDeleted = task; // prefer server-provided undo token if available
      const token = data && (data.undo_token || (data.result && data.result.undo_token)); if (token){ showSnackbar('Task deleted.', 'Undo', async ()=>{ try{ const rr = await API.restore(token); if (rr.ok) { await refresh(); showSnackbar('Restored.'); } else { showSnackbar('Could not restore.'); } }catch(e){ showSnackbar('Could not restore.'); } }); } else { showSnackbar('Task deleted.', 'Undo', async ()=>{ try{ await API.add(task.title); await refresh(); }catch(e){ showSnackbar('Could not restore.'); } }); }
    }catch(e){ tasks = prevTasks; render(); showSnackbar('Could not delete task.'); } }

  async function onClearCompleted(){ const completed = tasks.filter(t=>t.completed); if (completed.length===0) return; const prev = tasks.slice(); const ids=new Set(completed.map(t=>t.id)); tasks.forEach(t=>{ if (ids.has(t.id)) t._removed=true; }); render(); try{ for (const t of completed){ const r = await API.del(t.id); if (!r.ok) throw new Error('clear failed'); await new Promise(res=>setTimeout(res, REMOVE_STAGGER)); } await removalWait(completed.length); tasks = tasks.filter(t=>!ids.has(t.id)); const map2=computeNumberingMap(tasks); tasks.forEach(t=>{ t.short_id = map2[t.id] || t.short_id; }); render(); showSnackbar(`Cleared ${completed.length} task${completed.length!==1?'s':''}.`); }catch(e){ tasks=prev; render(); showSnackbar('Could not clear completed.'); } }

  async function onDeleteAllClicked(){ if (!confirm('Delete ALL tasks? This cannot be undone.')) return; if (tasks.length===0) return; const prev = tasks.slice(); lastDeletedAll = prev.slice(); lastDeletedAllToken = null; try{ const r = await API.delete_all(true); if (!r.ok){ const txt = await r.text(); throw new Error(txt||'delete all failed'); } const data = await r.json(); lastDeletedAllToken = data && data.undo_token; const toRemoveCount = tasks.length; tasks.forEach(t=>t._removed=true); render(); await removalWait(toRemoveCount); tasks = []; render(); showSnackbar(`Deleted ${data.deleted} task${data.deleted!==1?'s':''}.`, 'Undo', async ()=>{ await undoDeleteAll(); }); }catch(e){ tasks=prev; lastDeletedAll=null; lastDeletedAllToken=null; render(); showSnackbar('Could not delete all tasks.'); } }

  async function undoDeleteAll(){ if (!lastDeletedAll || !lastDeletedAll.length){ showSnackbar('Nothing to undo.'); return; } const toRestore = lastDeletedAll.slice(); const token = lastDeletedAllToken; lastDeletedAll=null; lastDeletedAllToken=null; try{ if (token){ const rr = await API.restore(token); if (rr.ok){ await refresh(); showSnackbar(`Restored ${toRestore.length} task${toRestore.length!==1?'s':''}.`); return; } }
    // fallback: recreate titles only
    for (const t of toRestore){ await API.add(t.title || t.description || ''); await new Promise(res=>setTimeout(res,60)); }
    await refresh(); showSnackbar(`Restored ${toRestore.length} task${toRestore.length!==1?'s':''}.`);
  }catch(e){ showSnackbar('Could not undo delete all.'); } }

  function addMessage(role,text,pre=false){ const log=qs('#chatLog'); const row=document.createElement('div'); row.className='chat-row '+role; const bubble=document.createElement(pre?'pre':'div'); bubble.className='bubble'; bubble.textContent=text; row.appendChild(bubble); log.appendChild(row); log.scrollTop = log.scrollHeight; }
  function summarize(tool,result){ if (!tool || !tool.function) return 'No tool recognized.'; const fn = tool.function; if (fn==='addTask' && result && result.title) return `Added: ${result.title} (#${result.short_id})`; if (fn==='viewTasks' && result && Array.isArray(result.items)) return `You have ${result.items.length} task(s).`; if (fn==='completeTask' && result && result.title) return `Completed: ${result.title} (#${result.short_id})`; if (fn==='deleteTask' && result && result.deleted) return `Deleted task #${result.short_id}`; return 'Done.'; }

  // Find a task by its short ID (client-side only, does not contact server)
  function findTaskByShort(short_id){ short_id = Number(short_id); if (!Number.isFinite(short_id)) return null; return tasks.find(t=>Number(t.short_id)===short_id) || null; }

  // Render assistant choices (disambiguation) returned by the server
  function renderChoices(data){ const choices = data.choices || []; if (!choices.length) return; const assistantText = data.assistant_message || ''; // determine action intent
    let actionType = 'both'; if (/\b(delete|remove|clear)\b/i.test(assistantText)) actionType='delete'; else if (/\b(mark|complete|done|completed)\b/i.test(assistantText)) actionType='complete';
    // Build a bubble containing options and buttons
    const log = qs('#chatLog'); const wrapper = document.createElement('div'); wrapper.className = 'chat-row assistant'; const box = document.createElement('div'); box.className='bubble';
    const intro = document.createElement('div'); intro.textContent = assistantText; box.appendChild(intro);
    const list = document.createElement('div'); list.style.marginTop='8px'; list.style.display='grid'; list.style.gap='6px';
    choices.forEach(ch => {
      const row = document.createElement('div'); row.style.display='flex'; row.style.alignItems='center'; row.style.justifyContent='space-between';
      const label = document.createElement('div'); label.textContent = `#${ch.short_id} ${ch.title}`; label.style.flex='1'; label.style.marginRight='8px';
      const btns = document.createElement('div'); btns.style.display='inline-flex'; btns.style.gap='6px';
      if (actionType==='both' || actionType==='complete'){
        const mbtn = document.createElement('button'); mbtn.className='btn-secondary'; mbtn.type='button'; mbtn.textContent='Mark'; mbtn.addEventListener('click', async ()=>{ await onChoiceComplete(ch.short_id); }); btns.appendChild(mbtn);
      }
      if (actionType==='both' || actionType==='delete'){
        const dbtn = document.createElement('button'); dbtn.className='btn-secondary'; dbtn.type='button'; dbtn.textContent='Delete'; dbtn.addEventListener('click', async ()=>{ await onChoiceDelete(ch.short_id); }); btns.appendChild(dbtn);
      }
      row.appendChild(label); row.appendChild(btns); list.appendChild(row);
    });
    box.appendChild(list); wrapper.appendChild(box); log.appendChild(wrapper); log.scrollTop = log.scrollHeight; }

  async function onChoiceDelete(short_id){ const task = findTaskByShort(short_id); if (!task){ showSnackbar(`Task #${short_id} not found.`); return; } // optimistic UI
    const idx = tasks.findIndex(t=>t.id===task.id); if (idx===-1) return; tasks[idx]._removed = true; render(); try{ const r = await API.del(task.id); if (!r.ok) throw new Error('delete failed'); const data = await r.json(); await removalWait(1); tasks = tasks.filter(t=>t.id!==task.id); const map = computeNumberingMap(tasks); tasks.forEach(t=>{ t.short_id = map[t.id] || t.short_id; }); render(); // show undo using server token if present
      const token = data && data.undo_token; if (token){ showSnackbar('Task deleted.', 'Undo', async ()=>{ try{ const rr = await API.restore(token); if (rr.ok) { await refresh(); showSnackbar('Restored.'); } else { showSnackbar('Could not restore.'); } }catch(e){ showSnackbar('Could not restore.'); } }); } else { showSnackbar('Task deleted.'); }
    }catch(e){ // revert
      tasks = tasks.map(t=> t.id===task.id ? Object.assign({},t,{_removed:false}) : t);
      render(); showSnackbar('Could not delete task.'); }
  }

  async function onChoiceComplete(short_id){ const task = findTaskByShort(short_id); if (!task){ showSnackbar(`Task #${short_id} not found.`); return; } const idx = tasks.findIndex(t=>t.id===task.id); if (idx===-1) return; const prev = tasks[idx].completed; tasks[idx].completed = true; render(); try{ const r = await API.toggle(task.id, true); if (!r.ok) throw new Error('toggle failed'); const updated = await r.json(); const idxx = tasks.findIndex(x=>x.id===updated.id); if (idxx!==-1) tasks[idxx]=updated; render(); showSnackbar(`Marked #${short_id} as complete.`); }catch(e){ tasks[idx].completed = prev; render(); showSnackbar('Could not update task.'); } }

  async function onChatSend(){ const input=qs('#chatInput'); if (!input) return; const msg=(input.value||'').trim(); if(!msg) return; addMessage('user',msg); input.value=''; const sendBtn=qs('#chatSend'); if (sendBtn) sendBtn.disabled=true; try{ const r = await API.chat(msg); if (r.status===501){ let body=null; try{ body = await r.json(); }catch(_){ body = await r.text(); } showSnackbar('Chat not configured.'); addMessage('assistant', typeof body==='string'?body:JSON.stringify(body), true); return; } if (!r.ok){ const text = await r.text(); throw new Error(text||'chat failed'); } const data = await r.json(); if (data.assistant_message) { addMessage('assistant', data.assistant_message); } else if (data.tool_request) { addMessage('assistant', JSON.stringify(data.tool_request, null, 2), true); addMessage('assistant', summarize(data.tool_request, data.result)); }
    // If the server returned disambiguation choices, render interactive buttons
    if (data.choices && Array.isArray(data.choices) && data.choices.length) { renderChoices(data); }
    if (data.result){ if (Array.isArray(data.result.added) && data.result.added.length){ data.result.added.forEach(t=>t._new=true); tasks = tasks.concat(data.result.added); tasks.sort((a,b)=>(parseInt(a.short_id||0,10)-parseInt(b.short_id||0,10))); render(); setTimeout(()=>{ tasks.forEach(t=>{ if (t._new) t._new=false; }); render(); },700); } if (data.result.deleted){ if (data.result.short_id!==undefined && data.result.short_id!==null){ const sid=data.result.short_id; const idx=tasks.findIndex(t=>t.short_id===sid); if (idx!==-1){ tasks[idx]._removed=true; render(); await removalWait(1); tasks = tasks.filter(t=>t.short_id!==sid); const map3=computeNumberingMap(tasks); tasks.forEach(t=>{ t.short_id = map3[t.id] || t.short_id; }); render(); } } else if (data.result.matched){ const target=(data.result.matched||'').toLowerCase(); const idx=tasks.findIndex(t=> (t.title||'').toLowerCase().includes(target)); if (idx!==-1){ tasks[idx]._removed=true; render(); await removalWait(1); tasks.splice(idx,1); const map4=computeNumberingMap(tasks); tasks.forEach(t=>{ t.short_id = map4[t.id] || t.short_id; }); render(); } } } } if (data.tool_request && data.tool_request.function === 'deleteAll'){ if (tasks.length>0){ tasks.forEach(t=>t._removed=true); render(); await removalWait(tasks.length); if (data.result && typeof data.result.deleted==='number' && data.result.deleted>0){ tasks=[]; render(); } else { await refresh(); } } else { await refresh(); } } const fn = data.tool_request && data.tool_request.function; if (fn==='addTask' || fn==='completeTask' || fn==='deleteTask' || fn==='deleteAll'){ await refresh(); } else if (fn==='viewTasks'){ const lines=(data.result.items||[]).map(t=>`#${t.short_id} ${t.completed ? '[x]':'[ ]'} ${t.title}`); if (lines.length) addMessage('assistant', lines.join('\n'), true); } }catch(e){
      // Provide clearer guidance for network/mixed-content failures
      const msg = (e && e.message) ? e.message : String(e);
      console.warn('Chat send failed:', e);
      if (/failed to fetch|networkerror|network error|TypeError/i.test(msg)){
        const hint = "Can't reach the backend API. Make sure the Flask server is running on http://127.0.0.1:5000 and that the page isn't loaded over HTTPS (mixed-content blocks HTTP).";
        showSnackbar('Chat network error — see console for details.');
        addMessage('assistant', hint, false);
      } else {
        showSnackbar('Chat error.');
        addMessage('assistant', 'Chat error: '+msg, false);
      }
     } finally{
       if (sendBtn) sendBtn.disabled=false; const inputEl=qs('#chatInput'); if (inputEl) inputEl.focus(); } }

  function wire(){
    const addBtn=qs('#addBtn'); if (addBtn) addBtn.addEventListener('click', onAdd);
    const newTitleEl = qs('#newTitle'); if (newTitleEl) newTitleEl.addEventListener('keydown', e=>{ if (e.key==='Enter') onAdd(); });
    const deleteAllBtn = qs('#deleteAllBtn'); if (deleteAllBtn) deleteAllBtn.addEventListener('click', onDeleteAllClicked);

    // Chat popup open/close wiring
    const openChatBtn = qs('#openChatBtn'); const chatPopup = qs('#chatPopup'); // const closeChatBtn = qs('#closeChatBtn');
    if (openChatBtn && chatPopup){ openChatBtn.addEventListener('click', ()=>{ const isHidden = chatPopup.classList.toggle('hidden'); chatPopup.setAttribute('aria-hidden', String(isHidden)); const ci = qs('#chatInput'); if (!isHidden && ci) ci.focus(); if (isHidden) openChatBtn.focus(); }); }

    // Filters
    qsa('.segmented button').forEach(btn=>btn.addEventListener('click', async ()=>{
      qsa('.segmented button').forEach(b=>{ b.classList.remove('active'); b.setAttribute('aria-selected','false'); });
      btn.classList.add('active'); btn.setAttribute('aria-selected','true');
      filter=btn.dataset.filter;
      const listEl=qs('#list'); if (listEl){ listEl.classList.add('filter-transition'); setTimeout(()=>listEl.classList.remove('filter-transition'),300); }
      await refresh();
    }));

    // Search (client-side) — keep rendering debounced but toggle clear button immediately
    const searchEl = qs('#search');
    const onSearch = debounce(()=>{ if (!searchEl) return; searchTerm = searchEl.value.trim().toLowerCase(); render(); },200);
    if (searchEl) {
      searchEl.addEventListener('input', (e)=>{
        const val = (searchEl.value||'').trim();
        const clearBtn = qs('#clearSearch');
        if (clearBtn) clearBtn.classList.toggle('visible', !!val);
        onSearch();
      });
      // initialize clear button state
      const initClear = qs('#clearSearch'); if (initClear && (searchEl.value||'').trim()) initClear.classList.add('visible');
    }

    // Clear button clears the input and collapses if empty
    const clearBtn = qs('#clearSearch'); if (clearBtn) clearBtn.addEventListener('click', ()=>{ const se = qs('#search'); if (se) se.value=''; if (clearBtn) clearBtn.classList.remove('visible'); searchTerm=''; render(); if (!se || se.value === '') setSearchExpanded(false); });

    // Toggle button opens the search input
    const toggle=qs('#searchToggle'); if (toggle) toggle.addEventListener('click', ()=>{ toggleSearch(); });
    if (searchEl) searchEl.addEventListener('blur', ()=>{ if (!searchEl.value.trim()) setSearchExpanded(false); });

    // Clear completed
    const clearCompletedBtn=qs('#clearCompleted'); if (clearCompletedBtn) clearCompletedBtn.addEventListener('click', onClearCompleted);

    // Theme
    initTheme();
    const themeToggle = qs('#themeToggle'); if (themeToggle) themeToggle.addEventListener('click', ()=>{ const mode = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'; setTheme(mode); });

    // Chat
    const chatInput=qs('#chatInput'); const chatSend=qs('#chatSend');
    if (chatInput && chatSend){ chatSend.addEventListener('click', onChatSend); chatInput.addEventListener('keydown', e=>{ if (e.key==='Enter') onChatSend(); }); }
  }

  // Boot — ensure DOM is ready before wiring so the script works even if not deferred
  document.addEventListener('DOMContentLoaded', () => {
    try{ initTheme(); }catch(e){}
    wire();
    setSearchExpanded(false);
    refresh();
  });

})();
