"""Bundled Glasshouse client. Kept buildless so `replicanta --web` stays local."""

APP_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Replicanta Glasshouse</title><link rel="stylesheet" href="/app.css"></head><body><header><b><i>R</i> REPLICANTA <em>glasshouse</em></b><nav><button data-view="habitat" class="on">Habitat</button><button data-view="atlas">Mind</button><button data-view="memory">Memory</button><button data-view="inner">Inner</button><button data-view="cells">Cells</button><button data-view="mud">MUD</button></nav><span id="local">● local</span><button id="still">Stillness</button></header><aside id="nursery"><small>NURSERY</small><div id="orgs"></div><button id="new">＋ Grow a new mind</button></aside><main><section id="heading"><small>GLASSHOUSE · <span data-name></span></small></section><section id="habitat" class="view on"><div class="specimen card"><div class="field"><div class="rings"></div><div class="orb"><span data-name></span></div></div><small>LIVE SPECIMEN <span id="state"></span></small><div class="meters"><label>Mood <b id="mood"></b></label><label>Stress <b id="stress"></b></label><label>Grounding <b id="ground"></b></label><label>Chaos <input id="chaos" type="range" min="0" max="1" step=".05"></label></div></div><div class="chat card"><h2>Conversation <button id="export-chat" type="button" title="Export chat">↓</button></h2><div id="messages"></div><div id="typing" class="typing-indicator" hidden><span></span><span></span><span></span></div><form id="chat"><textarea aria-label="Message" placeholder="Teach, ask, or wonder aloud…"></textarea><button>↑</button></form></div></section><section id="atlas" class="view"><div id="beliefs" class="card list"></div></section><section id="memory" class="view"><div id="episodes" class="card list"></div></section><section id="inner" class="view"><div id="inner-panel" class="card list"></div></section><section id="cells" class="view"><div id="cells-panel" class="card list"></div></section><section id="mud" class="view"><div id="mud-panel" class="card list"><h2>MUD Companion</h2><div id="mud-body"></div><form id="mud-form"><input id="mud-input" aria-label="MUD command" placeholder="No active game" autocomplete="off"><button>↑</button></form></div></section></main><aside id="steward"><small>STATUS</small><div id="activity" class="activity-strip"></div><div id="persona-panel" class="persona-strip"><small>PERSONA</small><b id="persona-active">—</b><div id="persona-list"></div></div><small>MIND</small><label>Auto-apply mutations <input id="auto" type="checkbox"></label><label>Attention <input id="focus" placeholder="Guide attention"></label><div id="mutation"></div><small>SENSES &amp; VOICE</small><div class="row"><label>Voice</label><button id="voice-toggle">voice off</button><select id="voice-select"></select></div><div class="btngrid"><button id="listen">Listen</button><button id="look">Look</button><button id="self-talk">Self-talk off</button><button id="git">Git off</button></div><small>LIFECYCLE</small><button id="sleep">Sleep / wake</button><p class="warning">AI-generated output may be wrong. This interface is a playground, not a product.</p></aside><footer id="commandbar"><form id="command"><div id="hints"></div><input id="cmd" aria-label="Command" placeholder="Type / for commands, or chat…" autocomplete="off"><button>↑</button></form></footer><div id="toast"></div><dialog id="trace"><p>Beliefs are built from attention, memory, and reasoning. This trace shows how the latest utterance was grounded.</p><button id="close">Close</button></dialog><script src="/app.js"></script></body></html>'''

APP_CSS = r''':root{--bg:#f3f0e7;--panel:#fbf9f3;--panel-2:#f0ede1;--ink:#1b281f;--muted:#67705f;--line:#ddd8c7;--accent:#174235;--accent-2:#7a9a6a;--amber:#b58900;--amber-ink:#6d5b10;--danger:#8a2c2c;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;display:grid;height:100vh;overflow:hidden;grid-template-columns:208px minmax(0,1fr) 288px;grid-template-rows:48px minmax(0,1fr) 52px;grid-template-areas:"head head head" "nursery main steward" "cmd cmd cmd"}
header{grid-area:head;display:flex;align-items:center;gap:16px;padding:0 16px;border-bottom:1px solid var(--line)}
header b{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;letter-spacing:1px}
header b i{display:inline-grid;place-items:center;background:var(--accent);color:#fff;border-radius:50%;width:24px;height:24px;font:13px Georgia}
header em{font:italic 13px Georgia;color:var(--muted);letter-spacing:0}
nav{display:flex;height:100%;gap:2px}
button{cursor:pointer;font-family:inherit}
input,textarea,select{font-family:inherit;color:inherit}
nav button{border:0;background:none;padding:0 14px;color:var(--muted);font-size:13px;height:100%}
nav button.on{color:var(--accent);box-shadow:inset 0 -2px 0 var(--accent)}
#local{margin-left:auto;color:#478052;font-size:11px}
#still{border:1px solid var(--accent);background:none;color:var(--accent);height:30px;padding:0 12px;border-radius:6px}
aside{padding:16px 12px;overflow:auto}
aside#nursery{grid-area:nursery;border-right:1px solid var(--line)}
aside small{letter-spacing:1.5px;font-size:10px;color:var(--muted);text-transform:uppercase}
#orgs{margin-top:8px}
#orgs button{display:block;width:100%;text-align:left;border:0;background:none;padding:8px 10px;margin-top:4px;border-radius:6px;color:var(--ink)}
#orgs button.on{background:var(--accent)}
#orgs button.on b{color:#fff}
#orgs button.on span{color:#ffffffb3}
#orgs b{font-size:13px;font-weight:600}
#orgs span{display:block;font-size:10px;color:var(--muted)}
#new{width:100%;height:34px;margin-top:8px;border:1px dashed var(--line);background:none;color:var(--muted);border-radius:6px}
main{grid-area:main;padding:16px 20px;display:flex;flex-direction:column;gap:12px;min-height:0;overflow:hidden}
#heading{margin:0}
#heading small{font-size:11px;letter-spacing:1.5px;color:var(--muted);text-transform:uppercase}
.view{display:none}
.view.on{display:grid;flex:1;min-height:0;gap:12px;grid-template-rows:minmax(0,1fr)}
.view#habitat{grid-template-rows:auto minmax(0,1fr)}
.card{border:1px solid var(--line);border-radius:8px;background:var(--panel)}
.specimen{display:flex;align-items:center;gap:16px;padding:10px 14px}
.field{position:relative;width:64px;height:64px;flex:0 0 auto;display:grid;place-items:center;background:linear-gradient(#1742350b 1px,transparent 1px),linear-gradient(90deg,#1742350b 1px,transparent 1px);background-size:16px 16px;overflow:hidden;border-radius:6px}
.rings{width:60px;height:60px;border-radius:50%;border:1px solid #17423522;position:absolute}
.orb{width:52px;height:52px;border-radius:45% 55%;background:radial-gradient(circle at 35% 30%,#e2ed9d,#75a16b 30%,#174235 75%);box-shadow:0 0 24px #527c5a66;display:grid;place-items:center;color:#fff;font:italic 13px Georgia;animation:pulse 6s infinite alternate}
@keyframes pulse{from{transform:scale(.98)}to{transform:scale(1.02)}}
.specimen>small{padding:0;position:static;font-size:10px;letter-spacing:1.5px;color:var(--muted);text-transform:uppercase;white-space:nowrap}
#state{display:inline-block;margin-left:6px;padding:1px 8px;border:1px solid var(--accent);border-radius:10px;font:11px var(--mono);color:var(--accent);letter-spacing:.5px}
.meters{display:flex;flex:1;gap:20px;padding:0;align-items:center}
.meters label{display:flex;flex-direction:column;gap:3px;text-transform:uppercase;font-size:10px;letter-spacing:1px;color:var(--muted)}
.meters b{font:13px var(--mono);color:var(--ink)}
.meters label:last-child{margin-left:auto}
.meters input[type=range]{width:120px}
input[type=range],input[type=checkbox]{accent-color:var(--accent)}
.chat{display:grid;grid-template-rows:auto minmax(0,1fr) auto auto;min-height:0}
.chat h2{margin:0;padding:12px 14px 8px;font-size:15px;font-weight:600}
#export-chat{float:right;width:24px;height:24px;border:1px solid var(--line);background:none;color:var(--muted);border-radius:6px;cursor:pointer;font-size:12px;line-height:1;padding:0}
#export-chat:hover{color:var(--accent);border-color:var(--accent)}
#messages{padding:10px 14px;overflow:auto;min-height:0}
.msg{margin:6px 0;padding:8px 10px;background:var(--panel-2);border-left:2px solid var(--accent)}
.msg.org{border-left-color:var(--accent-2);background:#f0f4ea}
.msg.sys{border-left-color:var(--amber);background:#fbf6e9}
.msg.sys>div{font:12px var(--mono);color:var(--amber-ink)}
.msg>div{white-space:pre-wrap}
.msg small{display:block;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.msg .why{border:0;background:none;color:var(--accent);font-size:11px;padding:0;margin-top:6px;cursor:pointer}
.msg .copy{float:right;border:0;background:none;color:var(--muted);font-size:11px;cursor:pointer;margin-left:8px;text-transform:none}
.msg .copy:hover{color:var(--accent)}
#typing[hidden]{display:none}
.typing-indicator{display:flex;gap:4px;align-items:center;padding:6px 14px;font-size:11px;color:var(--muted)}
.typing-indicator span{width:6px;height:6px;background:var(--accent);border-radius:50%;animation:bounce 1s infinite}
.typing-indicator span:nth-child(2){animation-delay:.15s}
.typing-indicator span:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,80%,100%{transform:scale(.6);opacity:.4}40%{transform:scale(1);opacity:1}}
.orb.typing{animation:pulse-typing 1s infinite}
@keyframes pulse-typing{0%,100%{transform:scale(1)}50%{transform:scale(1.05)}}
form#chat{display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--line)}
#chat textarea{flex:1;height:38px;resize:none;border:1px solid var(--line);border-radius:6px;background:var(--panel-2);padding:9px 10px;font:inherit}
#chat button{width:38px;height:38px;border:0;background:var(--accent);color:#fff;border-radius:6px}
.list{padding:14px;max-width:860px;overflow:auto;min-height:0}
.list h2{margin:0 0 10px;font-size:15px;font-weight:600}
.list>h3{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:var(--muted);margin:14px 0 6px}
.list article{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)}
.list article h3{margin:0;font-size:13px;font-weight:600}
.list article h3 em{color:var(--muted);font-weight:400}
.list article p{margin:2px 0 0;font-size:12px;color:var(--muted)}
.list article b{font:11px var(--mono);color:var(--muted);white-space:nowrap;min-width:40px;text-align:right;font-variant-numeric:tabular-nums}
#beliefs article b{font-size:12px}
.empty{font-style:italic;color:var(--muted)}
.gauges{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
.gauge{padding:8px 10px;background:var(--panel-2);border-left:2px solid var(--accent)}
.gauge label{display:block;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
.gauge b{font:16px var(--mono)}
.activity{white-space:pre-wrap;font:12px/1.5 var(--mono);background:var(--panel-2);padding:12px;max-height:40vh;overflow:auto}
.chip{display:inline-block;background:var(--panel-2);border:1px solid var(--line);padding:3px 8px;border-radius:12px;font-size:11px;margin:2px}
aside#steward{grid-area:steward;border-right:0;border-left:1px solid var(--line);display:flex;flex-direction:column;gap:6px;padding:16px 14px}
#steward>small{margin:10px 0 0}
#steward>small:first-child{margin-top:0}
#steward label{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px;margin:0}
#steward button{height:30px;padding:0 10px;border:1px solid var(--accent);background:none;color:var(--accent);border-radius:6px;margin:0}
#steward #focus{flex:1;min-width:0;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel-2);font-size:12px}
#steward .row{display:flex;gap:6px;align-items:center;margin:0}
#steward .row label{flex:0 0 auto;margin:0}
#steward .row button{flex:0 0 auto;width:auto;margin:0}
#steward .row select{flex:1;min-width:0;padding:5px 6px;border:1px solid var(--line);border-radius:6px;background:var(--panel-2);font-size:12px}
.btngrid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.btngrid button{margin:0;width:auto}
#sleep{width:100%}
.activity-strip{font-size:11px;color:var(--muted);padding:8px;background:var(--panel-2);border-left:2px solid var(--accent);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.persona-strip{padding:10px;background:var(--panel-2);border-left:2px solid var(--accent)}
.persona-strip small{display:block;font-size:10px;letter-spacing:1.5px;color:var(--muted);margin-bottom:4px}
.persona-strip b{font-size:13px;font-weight:600;display:block;margin-bottom:8px}
#persona-list{display:flex;flex-wrap:wrap;gap:6px}
#persona-list button{font-size:11px;padding:4px 8px;border:1px solid var(--accent);background:none;color:var(--accent);border-radius:4px;height:auto;width:auto}
#persona-list button.on{background:var(--accent);color:#fff}
.mutation{padding:10px;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.mutation small{display:block;color:var(--muted);font-size:10px;letter-spacing:1.5px}
.mutation button{margin:8px 6px 0 0;width:auto;height:28px}
.warning{margin:auto 0 0;font-size:10px;color:var(--muted);line-height:1.4}
#commandbar{grid-area:cmd;border-top:1px solid var(--line);background:var(--panel);padding:8px 16px;display:flex;align-items:center}
#commandbar form{width:100%;display:flex;gap:8px;position:relative}
#commandbar input{flex:1;height:36px;border:1px solid var(--line);padding:0 12px;border-radius:6px;background:var(--panel-2);font-size:13px}
#commandbar button{border:0;background:var(--accent);color:#fff;width:36px;height:36px;border-radius:6px}
#hints{position:absolute;left:0;right:44px;bottom:calc(100% + 6px);background:var(--panel);border:1px solid var(--line);border-radius:6px;box-shadow:0 4px 12px #0002;display:none;max-height:160px;overflow:auto}
#hints.on{display:block}
#hints div{padding:8px 12px;font-size:12px;cursor:pointer}
#hints div:hover,#hints div.active{background:var(--panel-2)}
#hints small{color:var(--muted);float:right}
#toast{position:fixed;top:16px;right:16px;background:var(--danger);color:#fff;padding:10px 16px;border-radius:6px;box-shadow:0 4px 12px #0003;opacity:0;pointer-events:none;transition:opacity .3s;z-index:100}
#toast.on{opacity:1}
dialog{border:1px solid var(--line);border-radius:8px;padding:20px;max-width:420px;background:var(--panel);color:var(--ink)}
dialog::backdrop{background:#0003}
body.still .orb{animation:none}
body.still .rings{display:none}
#mud-panel{display:grid;grid-template-rows:auto minmax(0,1fr) auto;gap:12px;height:100%;max-height:none;max-width:none;overflow:hidden}
#mud-body{display:flex;flex-direction:column;min-height:0;overflow:auto}
#mud-body .empty{margin:auto;text-align:center}
.mud-meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
.mud-meta div{display:flex;flex-direction:column}
.mud-meta b{font-size:13px;font-weight:600}
.mud-meta small{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:1px}
#mud-roster{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin:0 0 10px}
.mud-actor{padding:8px;background:var(--panel-2);border-left:2px solid var(--muted)}
.mud-actor.you{border-left-color:var(--accent-2);background:#f0f4ea}
.mud-actor.turn{border-left-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.mud-actor b{display:block;font-size:13px;font-weight:600}
.mud-actor small{display:block;font-size:10px;color:var(--muted)}
#mud-log{white-space:pre-wrap;font:12px/1.5 var(--mono);background:var(--panel-2);padding:12px;flex:1;min-height:120px;max-height:none;overflow:auto;margin:0}
#mud-form{display:flex;gap:8px}
#mud-form input{flex:1;height:36px;border:1px solid var(--line);padding:0 12px;border-radius:6px;background:var(--panel-2);font-size:13px}
#mud-form button{border:0;background:var(--accent);color:#fff;width:36px;height:36px;border-radius:6px}
#mud-form input:disabled{background:var(--panel-2);color:var(--muted)}
'''

APP_JS = r'''const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];let state;const token=location.hash.replace(/^#token=/,'');
async function api(path,data){const h={'Content-Type':'application/json'};if(token)h['X-Replicanta-Token']=token;const r=await fetch('/api/'+path,{method:data?'POST':'GET',headers:h,body:data?JSON.stringify(data):null});const j=await r.json();if(!r.ok)throw Error(j.error);return j}
function esc(x){const d=document.createElement('div');d.textContent=String(x??'');return d.innerHTML}
async function copyText(text){try{await navigator.clipboard.writeText(text);toast('copied')}catch(e){const t=document.createElement('textarea');t.value=text;document.body.appendChild(t);t.select();document.execCommand('copy');document.body.removeChild(t);toast('copied')}}
function toast(msg){const t=$('#toast');t.textContent=msg;t.classList.add('on');setTimeout(()=>t.classList.remove('on'),4000)}
function render(s){state=s;$$('[data-name]').forEach(x=>x.textContent=s.organism.name);$('#state').textContent=s.organism.state.toUpperCase();$('#mood').textContent=s.organism.mood;$('#stress').textContent=s.organism.stress;$('#ground').textContent=s.organism.rationality;$('#chaos').value=s.organism.chaos;$('#auto').checked=s.organism.auto_apply;$('#activity').textContent=(s.activity&&s.activity.length)?s.activity[0]:'quiet';
const pp=$('#persona-panel'), pa=$('#persona-active'), pl=$('#persona-list');
if(s.persona&&s.persona.available.length){pa.textContent=s.persona.active?s.persona.active:'—';
pl.innerHTML=s.persona.available.map(n=>`<button class="${n===s.persona.active?'on':''}" data-persona="${esc(n)}">${esc(n)}</button>`).join('');
pp.style.display='block'}
else{pp.style.display='none'}$('#voice-toggle').textContent=s.speech.enabled?'voice on':'voice off';const vs=$('#voice-select');vs.innerHTML='<option value="">voice…</option>'+s.speech.voices.map(v=>`<option value="${esc(v)}" ${v===s.speech.current?'selected':''}>${esc(v)}</option>`).join('');vs.disabled=!s.speech.voices.length;$('#self-talk').textContent=s.self_talk?'self-talk on':'self-talk off';$('#git').textContent=s.git_enabled?'git on':'git off';$('#messages').innerHTML=s.chat.map((m,i)=>`<div class="msg ${m.role==='org'?'org':m.role==='system'?'sys':''}"><small>${m.role==='org'?s.organism.name:m.role==='system'?'SYSTEM':'YOU'}<button class="copy" data-idx="${i}">copy</button></small><div>${esc(m.text)}</div>${m.role==='org'?'<button class="why">⌁ How did this form?</button>':''}</div>`).join('');$$('#messages .copy').forEach(b=>b.onclick=()=>copyText(s.chat[b.dataset.idx].text));$('#messages').scrollTop=1e9;$('#beliefs').innerHTML='<h2>What this mind holds to be true</h2>'+(s.beliefs.length?s.beliefs.map(b=>`<article><div><h3>${esc(b.subject)} <em>${esc(b.relation)}</em> ${esc(b.value)}</h3><p>Inspectable symbolic belief</p></div><b>${Math.round(b.confidence*100)}%</b></article>`).join(''):'<p class="empty">No beliefs yet. Chat with the organism to seed its mind.</p>');$('#episodes').innerHTML='<h2>Developmental timeline</h2>'+(s.memory.length?s.memory.map(e=>`<article><div><h3>${esc(e.kind)}</h3><p>${esc(e.text)}</p></div><b>cycle ${e.cycle}</b></article>`).join(''):'<p class="empty">No memories yet.</p>');const ip=$('#inner-panel');ip.innerHTML='<h2>Inner life</h2><div class="gauges"><div class="gauge"><label>Arousal</label><b>'+s.organism.arousal+'</b></div><div class="gauge"><label>Rationality</label><b>'+s.organism.rationality+'</b></div><div class="gauge"><label>Irrationality</label><b>'+s.organism.irrationality+'</b></div><div class="gauge"><label>Insane</label><b>'+(s.organism.insane?'yes':'no')+'</b></div></div>'+(s.activity&&s.activity.length?'<h3>Activity</h3><pre class="activity">'+s.activity.map(esc).join('\n')+'</pre>':'<p class="empty">No activity yet.</p>');const cp=$('#cells-panel');cp.innerHTML='<h2>Attention & skills</h2><h3>Attention</h3>'+(s.attention.length?s.attention.map(a=>`<span class="chip">${esc(a[0])} → ${esc(a[1])}</span>`).join(''):'<p class="empty">Attention is floating free.</p>')+'<h3>Goals</h3>'+(s.goals.length?s.goals.map(g=>`<article><div><h3>${esc(g.text)}</h3></div><b>${g.done_cycle?'done '+g.done_cycle:'active'}</b></article>`).join(''):'<p class="empty">No goals yet.</p>')+'<h3>Skills</h3>'+(s.skills.length?s.skills.map(sk=>`<article><div><h3>${esc(sk.name)}</h3><p>${esc(sk.when)}</p></div><b>${sk.uses} uses</b></article>`).join(''):'<p class="empty">No skills yet.</p>');const mp=$('#mud-panel');const mudBody=$('#mud-body');const mudInput=$('#mud-input');if(s.mud&&s.mud.active){const myTurn=s.mud.roster&&s.mud.roster.some(a=>a.is_you&&a.is_turn);mudBody.innerHTML='<div class="mud-meta"><div><small>Scenario</small><b>'+esc(s.mud.scenario)+'</b></div><div><small>Room</small><b>'+esc(s.mud.room||'unknown')+'</b></div><div><small>Turn</small><b>'+esc(s.mud.turn||'—')+(s.mud.paused?' (paused)':'')+'</b></div></div><div id="mud-roster">'+(s.mud.roster||[]).map(a=>'<div class="mud-actor '+(a.is_you?'you ':'')+(a.is_turn?'turn ':'')+'"><b>'+esc(a.name)+'</b><small>'+esc(a.kind)+' · '+esc(a.room)+'</small><small>'+(a.inventory.length?'carrying: '+a.inventory.map(esc).join(', '):'empty-handed')+'</small></div>').join('')+'</div><div id="mud-log">'+(s.mud.log||[]).join('\n')+'</div>';mudInput.placeholder=myTurn?'What do you do?':'Waiting for '+esc(s.mud.turn||'...');mudInput.disabled=s.mud.finished}else{mudBody.innerHTML='<p class="empty">No dungeon crawl active. Start one with <code>/mud</code> or <code>/mud scenario &lt;description&gt;</code>.</p>';mudInput.placeholder='No active game';mudInput.disabled=true}$('#orgs').innerHTML=s.nursery.organisms.map(n=>`<button data-org="${esc(n)}" class="${n===s.organism.name?'on':''}"><b>${esc(n)}</b><span>${n===s.organism.name?s.organism.state:'stored'}</span></button>`).join('');const p=s.extensions.pending;$('#mutation').innerHTML=p?`<div class="mutation"><small>PENDING MUTATION</small><p><b>${esc(p.kind)}</b></p><button data-mut="approve">Approve</button><button data-mut="reject">Reject</button></div>`:'';bindDynamic()}
function bindDynamic(){$$('.why').forEach(b=>b.onclick=()=>$('#trace').showModal());$$('[data-org]').forEach(b=>b.onclick=async()=>render(await api('swap',{name:b.dataset.org})));$$('[data-mut]').forEach(b=>b.onclick=async()=>render((await api('mutation',{action:b.dataset.mut})).state));$$('[data-persona]').forEach(b=>b.onclick=async()=>runCmd('/persona '+b.dataset.persona))}
function switchTab(view){$$('[data-view],.view').forEach(x=>x.classList.remove('on'));const b=$(`[data-view="${view}"]`);if(b)b.classList.add('on');$('#'+view).classList.add('on');localStorage.setItem('r-tab',view)}
$$('[data-view]').forEach(b=>b.onclick=()=>switchTab(b.dataset.view));$('#close').onclick=()=>$('#trace').close();
const cmdInput=$('#cmd'),hints=$('#hints');
function updateHints(){const v=cmdInput.value;if(!v.startsWith('/')){hints.classList.remove('on');hints.innerHTML='';return}const tok=v.split(' ')[0];fetch('/api/commands').then(r=>r.json()).then(list=>{const matches=list.filter(c=>c.name.startsWith(tok));hints.innerHTML=matches.map(c=>`<div data-cmd="${esc(c.name)}"><b>${esc(c.name)}</b> <small>${esc(c.description)}</small></div>`).join('');hints.classList.toggle('on',matches.length>0)}).catch(()=>{})}
cmdInput.oninput=updateHints;cmdInput.onfocus=updateHints;cmdInput.onblur=()=>setTimeout(()=>hints.classList.remove('on'),200);
hints.onclick=e=>{const d=e.target.closest('[data-cmd]');if(!d)return;cmdInput.value=d.dataset.cmd+' ';cmdInput.focus();updateHints()};
async function runCmd(cmd){try{const r=await api('command',{command:cmd});render(r.state);if(r.messages&&r.messages.length)toast(r.messages.join('\n'))}catch(x){toast(x.message)}}
$('#command').onsubmit=async e=>{e.preventDefault();const v=cmdInput.value.trim();if(!v)return;cmdInput.disabled=true;try{if(v.startsWith('/')){await runCmd(v)}else{render((await api('chat',{text:v})).state)}cmdInput.value='';hints.classList.remove('on')}catch(x){toast(x.message)}finally{cmdInput.disabled=false;cmdInput.focus()}};
let typingTimer=null;let lastTyping=0;function setTyping(show){const ti=$('#typing');if(ti)ti.hidden=!show;const orb=$('.orb');if(orb)orb.classList.toggle('typing',show)}
async function sendTyping(){try{await api('typing',{typing:true})}catch(x){}}
function debounceTyping(){setTyping(true);const now=Date.now();lastTyping=now;clearTimeout(typingTimer);typingTimer=setTimeout(()=>{setTyping(false)},1200);if(now-lastTyping>4000||lastTyping===now)sendTyping()}
const chatArea=$('textarea');chatArea.oninput=debounceTyping;chatArea.onblur=()=>{clearTimeout(typingTimer);setTyping(false)};chatArea.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('#chat').requestSubmit()}};
$('#chat').onsubmit=async e=>{e.preventDefault();const t=chatArea;if(!t.value.trim())return;t.disabled=true;setTyping(false);clearTimeout(typingTimer);try{render((await api('chat',{text:t.value})).state);t.value=''}catch(x){toast(x.message)}finally{t.disabled=false;t.focus()}};$('#mud-form').onsubmit=async e=>{e.preventDefault();const t=$('#mud-input');const v=t.value.trim();if(!v)return;if(state.mud&&state.mud.active){const myTurn=state.mud.roster&&state.mud.roster.some(a=>a.is_you&&a.is_turn);if(!myTurn){toast('Waiting for '+esc(state.mud.turn||'...')+'\'s turn');return}}t.disabled=true;try{const r=await api('mud-act',{text:v});render(r.state);if(r.messages&&r.messages.length)toast(r.messages.join('\n'))}catch(x){toast(x.message)}finally{t.disabled=false;t.focus()}};
$('#chaos').onchange=async e=>render(await api('settings',{chaos:Number(e.target.value)}));$('#auto').onchange=async e=>render(await api('settings',{auto_apply:e.target.checked}));$('#focus').onchange=async e=>render(await api('settings',{focus:e.target.value}));
$('#voice-toggle').onclick=async()=>runCmd(state.speech.enabled?'/voice off':'/voice on');
$('#export-chat').onclick=async()=>{try{const r=await api('command',{command:'/export'});render(r.state);if(r.messages&&r.messages.length)toast(r.messages.join('\n'))}catch(x){toast(x.message)}};
$('#voice-select').onchange=async e=>{const v=e.target.value;if(v)runCmd('/voice use '+v)};
$('#listen').onclick=async()=>runCmd('/listen');
$('#look').onclick=async()=>runCmd('/look');
$('#self-talk').onclick=async()=>runCmd('/self-talk');
$('#git').onclick=async()=>runCmd(state.git_enabled?'/git off':'/git on');
$('#sleep').onclick=async()=>render((await api('lifecycle',{action:state.organism.state==='wake'?'sleep':'wake'})).state);$('#still').onclick=()=>{document.body.classList.toggle('still');$('#still').textContent=document.body.classList.contains('still')?'Resume':'Stillness'};$('#new').onclick=async()=>{const n=prompt('Name the new organism');if(n)try{render(await api('organisms',{name:n}))}catch(x){toast(x.message)}};
window.onkeydown=e=>{if(e.key==='/'&&document.activeElement!==cmdInput&&document.activeElement.tagName!=='TEXTAREA'){e.preventDefault();cmdInput.focus()}};
api('state').then(s=>{render(s);const saved=localStorage.getItem('r-tab');if(saved&&$(`[data-view="${saved}"]`))switchTab(saved)}).catch(x=>document.body.innerHTML='<p>'+esc(x.message)+'</p>');'''
