// Audio processing panel (rumble filter + click & pop removal): state, immediate persistence,
// Settings mirroring (Cancel restores what was saved), the export request, and the markup.
const fs=require('fs'), assert=require('assert'), path=require('path');
const root=path.join(__dirname,'..','..','backend','static');
const src=fs.readFileSync(path.join(root,'app.js'),'utf8');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
global.document={addEventListener(){},getElementById(){return null;}}; global.window=global; global.WaveSurfer={};
const vinylApp=new Function(src+';return vinylApp;')();

// Fake backend: remembers PUT bodies, applies them like the real /api/config.
function fakeServer(initial={}){
  const state={rumble_filter:false,declick:'off',bit_depth:'source',output_dir:'/out',...initial};
  const puts=[];
  global.fetch=async(url,opt={})=>{
    if(opt.method==='PUT'){
      const body=JSON.parse(opt.body); puts.push(body);
      Object.assign(state,body);
      return {ok:true,json:async()=>({...state})};
    }
    return {ok:true,json:async()=>({...state})};
  };
  return {state,puts};
}
const mk=()=>{ const a=vinylApp(); a.toasts_=[]; a.notify=(m,o)=>{a.toasts_.push({m,o});}; a.currentFileId='f'; a.currentFile={}; return a; };
const tick=()=>new Promise(resolve=>setImmediate(resolve));

let finished=false;
process.on('exit',()=>{ if(!finished){ console.error('audio panel test did not finish'); process.exitCode=1; } });

(async()=>{
  // ---- defaults + controls ----
  { const a=mk();
    assert.strictEqual(a.rumbleFilter,false); assert.strictEqual(a.declick,'off');
    assert.deepStrictEqual(a.declickLevels.map(l=>l.id),['off','light','medium','strong']);
    assert.deepStrictEqual(a.declickLevels.map(l=>l.label),['Off','Light','Medium','Strong']);
    assert.strictEqual(a.normDeclick('brutal'),'off'); assert.strictEqual(a.normDeclick(undefined),'off');
    assert.strictEqual(a.normDeclick('strong'),'strong');
    assert.strictEqual(a.config.bit_depth,'source');
  }
  // ---- each control persists immediately, as a single field ----
  { const srv=fakeServer(); const a=mk();
    a.toggleRumble(); await tick();
    assert.strictEqual(a.rumbleFilter,true); assert.deepStrictEqual(srv.puts.pop(),{rumble_filter:true});
    assert.strictEqual(a.config.rumble_filter,true,'config mirror updated from the response');
    a.toggleRumble(); await tick();
    assert.strictEqual(a.rumbleFilter,false); assert.deepStrictEqual(srv.puts.pop(),{rumble_filter:false});
    a.setDeclick('medium'); await tick();
    assert.strictEqual(a.declick,'medium'); assert.deepStrictEqual(srv.puts.pop(),{declick:'medium'});
    assert.strictEqual(a.config.declick,'medium');
    a.setDeclick('medium'); await tick();
    assert.strictEqual(srv.puts.length,0,'re-selecting the same strength does not save');
    a.setDeclick('nonsense'); await tick();
    assert.strictEqual(a.declick,'off'); assert.deepStrictEqual(srv.puts.pop(),{declick:'off'});
  }
  // ---- a failed save keeps the choice and warns ----
  { const a=mk();
    global.fetch=async()=>({ok:false,status:500,json:async()=>({})});
    a.setDeclick('strong'); await tick();
    assert.strictEqual(a.declick,'strong'); assert.strictEqual(a.toasts_.length,1);
    assert.strictEqual(a.toasts_[0].o.type,'warning'); assert.ok(/Couldn't save this as your default/.test(a.toasts_[0].m));
    global.fetch=async()=>{ throw new Error('offline'); };
    const quiet=console.error; console.error=()=>{};
    a.toggleRumble(); await tick(); console.error=quiet;
    assert.strictEqual(a.rumbleFilter,true); assert.strictEqual(a.toasts_.length,2);
    fakeServer(); const b=mk(); b.setDeclick('light'); await tick();
    assert.strictEqual(b.toasts_.length,0,'a successful save stays quiet');
  }
  // ---- a failed panel save: Settings shows, and a later Save re-sends, what the panel shows ----
  { fakeServer({declick:'off'}); const a=mk(); await a.loadConfig();
    global.fetch=async()=>({ok:false,status:500,json:async()=>({})});
    a.setDeclick('strong'); a.toggleRumble(); await tick();
    assert.strictEqual(a.config.declick,'strong'); assert.strictEqual(a.config.rumble_filter,true);
    a.openSettings(); assert.strictEqual(a.config.declick,'strong','Settings shows the panel value');
    a.closeSettings(); assert.strictEqual(a.config.declick,'strong');
    // with Settings open while the save fails, Cancel keeps it too
    a.openSettings(); a.setDeclick('light'); await tick(); a.closeSettings();
    assert.strictEqual(a.config.declick,'light'); assert.strictEqual(a.declick,'light');
  }
  // ---- loadConfig answering while Settings is open: Cancel restores the loaded values, not the built-in defaults ----
  { fakeServer({declick:'medium',output_dir:'/music'}); const a=mk();
    a.openSettings(); await a.loadConfig();
    a.config.output_dir=''; a.closeSettings();
    assert.strictEqual(a.config.output_dir,'/music'); assert.strictEqual(a.config.declick,'medium');
  }
  // ---- Settings Save refused by the server: the dialog stays open, nothing is synced, an error shows ----
  { fakeServer({declick:'strong',rumble_filter:true}); const a=mk(); await a.loadConfig();
    a.openSettings(); a.config.silence_threshold='';
    global.fetch=async()=>({ok:false,status:422,json:async()=>({detail:[{msg:'bad'}]})});
    await a.saveConfig();
    assert.strictEqual(a.showSettings,true,'dialog stays open');
    assert.strictEqual(a.declick,'strong'); assert.strictEqual(a.rumbleFilter,true,'panel untouched');
    assert.ok(Array.isArray(a.config.detail)===false && a.config.declick==='strong','config is not replaced by the error body');
    assert.strictEqual(a.toasts_.pop().o.type,'error');
    assert.ok(a.settingsSnapshot,'still cancellable');
  }
  // ---- loadConfig fills the panel ----
  { fakeServer({rumble_filter:true,declick:'strong'});
    const a=mk(); await a.loadConfig();
    assert.strictEqual(a.rumbleFilter,true); assert.strictEqual(a.declick,'strong');
    fakeServer({declick:'bogus'}); await a.loadConfig(); assert.strictEqual(a.declick,'off');
  }
  // ---- Settings Save: the modal's values reach the server and the panel ----
  { const srv=fakeServer(); const a=mk(); await a.loadConfig();
    a.openSettings();
    a.config.declick='strong'; a.config.rumble_filter=true; a.config.bit_depth='24';
    srv.puts.length=0; await a.saveConfig();
    const sent=srv.puts[0];
    assert.strictEqual(sent.declick,'strong'); assert.strictEqual(sent.rumble_filter,true); assert.strictEqual(sent.bit_depth,'24');
    assert.strictEqual(a.declick,'strong'); assert.strictEqual(a.rumbleFilter,true);
    assert.strictEqual(a.showSettings,false); assert.strictEqual(a.settingsSnapshot,null);
  }
  // ---- Cancel / Escape / click-away restore the saved copy, so an unsaved change never reaches an export ----
  { fakeServer({rumble_filter:true,declick:'light'});
    const a=mk(); await a.loadConfig();
    a.openSettings(); assert.strictEqual(a.showSettings,true);
    a.config.declick='strong'; a.config.rumble_filter=false; a.config.bit_depth='24';
    a.closeSettings();
    assert.strictEqual(a.showSettings,false);
    assert.strictEqual(a.config.declick,'light'); assert.strictEqual(a.config.rumble_filter,true);
    assert.strictEqual(a.config.bit_depth,'source');
    assert.strictEqual(a.declick,'light'); assert.strictEqual(a.rumbleFilter,true,'the panel never saw the unsaved edit');
    a.closeSettings(); // window-level Escape fires even when closed: harmless
    assert.strictEqual(a.config.declick,'light');
  }
  // ---- a panel change saved while Settings is open is not undone by Cancel ----
  { const srv=fakeServer(); const a=mk(); await a.loadConfig();
    a.openSettings(); a.setDeclick('medium'); await tick();
    a.closeSettings();
    assert.strictEqual(a.config.declick,'medium'); assert.strictEqual(a.declick,'medium');
    assert.strictEqual(srv.state.declick,'medium');
  }
  // ---- the support-note choice survives Cancel ----
  { fakeServer(); const a=mk(); await a.loadConfig();
    a.openSettings(); await a.hideSupportAsk(); a.closeSettings();
    assert.strictEqual(a.config.hide_support_ask,true);
  }
  // ---- the export request carries the panel values and no legacy field ----
  { fakeServer(); const a=mk();
    a.rumbleFilter=true; a.declick='strong';
    a.selectedRelease={id:5,tracks:[{position:'A1'}]}; a.customMapping=[0];
    a.detectedTracks=[{number:1,start:0,end:100,duration:100,ignored:false}];
    a.uploadedFiles=[{id:'f',status:'ready'}];
    let body;
    global.fetch=async(url,opt)=>{ if(opt&&opt.method==='POST') body=JSON.parse(opt.body); return {ok:true,json:async()=>({job_id:'f'})}; };
    await a.processFile();
    clearInterval(a.processPollTimer); // processFile starts polling the job; this test only reads the request
    assert.strictEqual(body.rumble_filter,true); assert.strictEqual(body.declick,'strong');
    assert.ok(!('restoration_level' in body));
  }
  // ---- markup ----
  { assert.ok(html.includes('Audio processing')); assert.ok(html.includes('Removes inaudible turntable rumble below 15 Hz.'));
    assert.ok(html.includes('Stronger settings catch fainter clicks but take longer to process.'));
    assert.ok(!html.includes('Clean Up Clicks'), 'old Clean Up toggle gone');
    assert.ok(html.includes('toggleRumble()') && html.includes('setDeclick(level.id)'));
    assert.ok(/aria-pressed="rumbleFilter"/.test(html) && /:aria-pressed="declick === level\.id"/.test(html), 'pressed state exposed');
    assert.ok(html.includes('x-model="config.rumble_filter"') && html.includes('x-model="config.declick"') && html.includes('x-model="config.bit_depth"'));
    assert.ok(html.includes('Match source (recommended)') && html.includes('24-bit'));
    // Settings closes through closeSettings everywhere, so Cancel / Escape / click-away restore the saved copy
    assert.ok(!/showSettings\s*=\s*false/.test(html), 'no path closes Settings without restoring');
    assert.ok(html.includes('@keydown.escape.window="closeSettings()"') && html.includes('@click.away="closeSettings()"'));
    // focus rings on the panel buttons are dark (>= 3:1), not Tailwind's default light blue
    assert.ok(/\.panel-toggle:focus-visible\s*\{[^}]*#1E1E1E/.test(html) && /\.panel-segment:focus-visible\s*\{[^}]*#1E1E1E/.test(html));
    assert.ok(/class="panel-toggle /.test(html) && /class="panel-segment /.test(html));
    const panelHtml=html.slice(html.indexOf('data-testid="audio-panel"'),html.indexOf('<!-- Process Button -->'));
    assert.ok(!/ring-2|focus:outline-none/.test(panelHtml),'no default-coloured ring in the panel');
    // short window: the dialog scrolls and Save stays visible
    assert.ok(/max-height: 90vh/.test(html) && /overflow-y-auto/.test(html) && /sticky bottom-0/.test(html));
  }
  finished=true;
  console.log('audio panel ok');
})().catch(error=>{console.error(error);process.exit(1);});
