// Undo flows: queue-file removal (delayed DELETE), track delete, re-analyze,
// Snap to Discogs.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const trk = (n, s, e) => ({ number: n, start: s, end: e, duration: e - s, editing: false, ignored: false });

// Fake regions plugin: records the regions addTrackRegions() creates so the
// tests can check that Undo redraws them.
function mkRegions() {
    const list = [];
    return {
        list, getRegions: () => list, clearRegions() { list.length = 0; },
        addRegion(o) { const r = { ...o, element: {}, setOptions() {} }; list.push(r); return r; }
    };
}
// Alpine normally provides $refs/$nextTick; stub them. initWaveform is replaced
// by a fake that only rebuilds regions (the real one needs WaveSurfer + fetch).
function mkApp() {
    const a = vinylApp();
    a.$nextTick = async () => {};
    a.$refs = {};
    a.uploadedFiles = [{ id: 'f1', filename: 'one.wav', status: 'uploaded' }, { id: 'f2', filename: 'two.wav', status: 'uploaded' }, { id: 'f3', filename: 'three.wav', status: 'uploaded' }];
    a.currentFileId = 'f2'; a.currentFile = a.uploadedFiles[1];
    a.waveform = { getDuration: () => 600, destroy() {}, pause() {} };
    a.waveformRegions = mkRegions();
    a.initWaveform = async () => { a.waveform = { getDuration: () => 600, destroy() {}, pause() {} }; a.waveformRegions = mkRegions(); a.addTrackRegions(); };
    return a;
}

(async () => {
    const deletes = [];
    global.fetch = async (url, opts) => { deletes.push(`${opts.method} ${url}`); return { ok: true, status: 200, json: async () => ({}) }; };

    // ---- file removal: soft remove, DELETE only after the window ----
    let a = mkApp();
    a.detectedTracks = [trk(1, 0, 100)];
    a.removeFile('f2', 40);
    assert.deepStrictEqual(a.uploadedFiles.map(f => f.id), ['f1', 'f3']);
    assert.strictEqual(a.currentFileId, null);
    assert.deepStrictEqual(a.detectedTracks, []);
    assert.strictEqual(deletes.length, 0, 'no DELETE inside the undo window');
    assert.strictEqual(a.toasts.length, 1);
    assert.strictEqual(a.toasts[0].action.label, 'Undo');
    await sleep(120);
    assert.deepStrictEqual(deletes, ['DELETE /api/queue/f2']);
    assert.strictEqual(a.toasts.length, 0);
    assert.deepStrictEqual(a.pendingRemovals, {});

    // ---- undo restores position, current file, tracks and regions; no DELETE ----
    deletes.length = 0;
    a = mkApp();
    a.detectedTracks = [trk(1, 0, 100), trk(2, 100, 200)];
    const rel = { id: 5, tracks: [] };
    a.selectedRelease = rel;
    a.removeFile('f2', 40);
    a.runToastAction(a.toasts[0].id);
    await sleep(5);
    assert.deepStrictEqual(a.uploadedFiles.map(f => f.id), ['f1', 'f2', 'f3']);
    assert.strictEqual(a.currentFileId, 'f2');
    assert.strictEqual(a.currentFile.id, 'f2');
    assert.deepStrictEqual(a.detectedTracks.map(t => t.end), [100, 200]);
    assert.strictEqual(a.selectedRelease, rel);
    assert.strictEqual(a.waveformRegions.list.length, 2);
    assert.deepStrictEqual(a.pendingRemovals, {});
    await sleep(120);
    assert.strictEqual(deletes.length, 0);

    // undo of a non-current file does not steal the selection
    a = mkApp();
    a.removeFile('f1', 5000);
    a.runToastAction(a.toasts[0].id);
    assert.deepStrictEqual(a.uploadedFiles.map(f => f.id), ['f1', 'f2', 'f3']);
    assert.strictEqual(a.currentFileId, 'f2');

    // ---- closing the toast (or unload) commits immediately; double commit is harmless ----
    a = mkApp(); deletes.length = 0;
    a.removeFile('f3', 5000);
    a.dismissToast(a.toasts[0].id);
    await sleep(10);
    assert.deepStrictEqual(deletes, ['DELETE /api/queue/f3']);
    deletes.length = 0;
    a.removeFile('f1', 5000); a.removeFile('f3', 5000);
    await a.commitAllRemovals();
    assert.deepStrictEqual(deletes.sort(), ['DELETE /api/queue/f1']);
    assert.strictEqual(a.toasts.length, 0);

    // removing the same file twice keeps one pending entry
    a = mkApp(); deletes.length = 0;
    a.removeFile('f1', 5000); a.removeFile('f1', 5000);
    assert.strictEqual(a.toasts.length, 1);
    await a.commitAllRemovals();
    assert.strictEqual(deletes.length, 1);

    // the page-unload hook is registered and commits with keepalive
    const listeners = {};
    global.window = { addEventListener: (n, fn) => { listeners[n] = fn; }, location: { protocol: 'http:', host: 'x' } };
    global.document = { addEventListener() {} };
    const keepalives = [];
    global.fetch = async (url, opts) => { keepalives.push(opts && opts.keepalive); return { ok: true, status: 200, json: async () => ({}) }; };
    a = mkApp();
    a.loadConfig = async () => {}; a.loadFormats = async () => {}; a.checkStatus = async () => {};
    a.connectWebSocket = () => {}; a.$watch = () => {};
    await a.init();
    assert.ok(listeners.pagehide, 'init registers a pagehide handler');
    keepalives.length = 0;
    a.removeFile('f1', 5000);
    listeners.pagehide();
    await sleep(10);
    assert.deepStrictEqual(keepalives, [true]);
    delete global.window; delete global.document;

    // failed DELETE puts the file back and reports an error
    global.fetch = async () => ({ ok: false, status: 500, json: async () => ({}) });
    a = mkApp();
    a.removeFile('f1', 5000);
    await a.commitAllRemovals();
    assert.ok(a.uploadedFiles.some(f => f.id === 'f1'));
    assert.ok(a.toasts.some(t => t.type === 'error'));
    global.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });

    // a recording that is being analyzed or exported is not removable
    a = mkApp(); a.analyzing = true;
    a.removeFile('f2', 5000);
    assert.strictEqual(a.uploadedFiles.length, 3);
    assert.strictEqual(a.toasts[0].type, 'warning');
    a = mkApp(); a.isProcessing = true;
    a.removeFile('f2', 5000);
    assert.strictEqual(a.uploadedFiles.length, 3);

    // ---- delete track: immediate, undo restores tracks + mapping + release + regions ----
    a = mkApp();
    a.detectedTracks = [trk(1, 0, 100), trk(2, 100, 200), trk(3, 200, 300)];
    a.customMapping = [0, 1, 2];
    const release = { id: 3, tracks: [{}, {}, {}] };
    a.selectedRelease = release;
    a.addTrackRegions();
    assert.strictEqual(a.waveformRegions.list.length, 3);
    a.deleteTrack(2);
    assert.deepStrictEqual(a.detectedTracks.map(t => [t.number, t.start]), [[1, 0], [2, 200]]);
    assert.strictEqual(a.waveformRegions.list.length, 2);
    const t = a.toasts[a.toasts.length - 1];
    assert.strictEqual(t.action.label, 'Undo');
    a.runToastAction(t.id);
    assert.deepStrictEqual(a.detectedTracks.map(x => [x.number, x.start, x.end]), [[1, 0, 100], [2, 100, 200], [3, 200, 300]]);
    assert.deepStrictEqual(a.customMapping, [0, 1, 2]);
    assert.strictEqual(a.selectedRelease, release);
    assert.strictEqual(a.waveformRegions.list.length, 3);
    // deleting an unknown track does nothing
    a.toasts.length = 0; a.deleteTrack(42); assert.strictEqual(a.toasts.length, 0);
    // region-menu path goes through the same flow
    a.regionContextMenu = { show: true, trackNumber: 1 };
    a.deleteTrackFromRegionMenu();
    assert.strictEqual(a.detectedTracks.length, 2); assert.strictEqual(a.regionContextMenu.show, false);
    assert.strictEqual(a.toasts[a.toasts.length - 1].action.label, 'Undo');
    // undo after switching files refuses instead of clobbering
    a.currentFileId = 'f3'; a.detectedTracks = [trk(1, 0, 10)];
    a.runToastAction(a.toasts[a.toasts.length - 1].id);
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [10]);

    // ---- re-analyze: immediate, undo restores tracks + release + results ----
    a = mkApp();
    const rel2 = { id: 7, tracks: [{ position: 'A1' }, { position: 'A2' }] };
    const results = [{ id: 7 }];
    a.detectedTracks = [trk(1, 0, 100), trk(2, 100, 200)];
    a.selectedRelease = rel2; a.searchResults = results; a.customMapping = [0, 1];
    a.analyzeFile = async () => { a.detectedTracks = [trk(1, 0, 50), trk(2, 50, 120), trk(3, 120, 200)]; };
    await a.reanalyzeFile();
    assert.strictEqual(a.selectedRelease, null);
    assert.strictEqual(a.detectedTracks.length, 3);
    const rt = a.toasts[a.toasts.length - 1];
    assert.strictEqual(rt.action.label, 'Undo');
    a.runToastAction(rt.id);
    await sleep(5);
    assert.strictEqual(a.selectedRelease, rel2);
    assert.strictEqual(a.searchResults, results);
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [100, 200]);
    assert.deepStrictEqual(a.customMapping, [0, 1]);
    assert.strictEqual(a.waveformRegions.list.length, 2);

    // undo after switching files refuses instead of clobbering
    a = mkApp();
    a.detectedTracks = [trk(1, 0, 100)];
    a.analyzeFile = async () => { a.detectedTracks = [trk(1, 0, 50)]; };
    await a.reanalyzeFile();
    const stale = a.toasts[a.toasts.length - 1];
    a.currentFileId = 'f3';
    a.detectedTracks = [trk(1, 0, 10)];
    a.runToastAction(stale.id);
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [10]);
    assert.ok(a.toasts.some(x => x.type === 'warning'));

    // ---- Snap to Discogs: runs without a confirm dialog, then offers Undo ----
    a = mkApp();
    a.detectedTracks = [trk(1, 0, 100)];
    a.selectedRelease = { id: 1, tracks: [{ position: 'A1', duration: '1:00' }, { position: 'A2', duration: '1:00' }] };
    a.selectedSides = ['A'];
    a.resetMappingToTargets();
    global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ tracks: [trk(1, 0, 60), trk(2, 60, 120)], method: 'durations', matched: true, boundaries: [{ expected: 60, snapped: true }] }) });
    await a.snapToDiscogs();
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [60, 120]);
    assert.deepStrictEqual(a.expectedBoundaries, [60]);
    assert.deepStrictEqual(a.customMapping, [0, 1]);
    const dt = a.toasts[a.toasts.length - 1];
    assert.strictEqual(dt.action.label, 'Undo');
    a.runToastAction(dt.id);
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [100]);
    assert.deepStrictEqual(a.expectedBoundaries, []);
    assert.deepStrictEqual(a.customMapping, [0]);
    assert.strictEqual(a.trackCountMismatch, true, 'one detected track, two on the side');
    // after switching files the Undo refuses instead of clobbering
    await a.snapToDiscogs();
    const staleSnap = a.toasts[a.toasts.length - 1];
    a.currentFileId = 'f3'; a.detectedTracks = [trk(1, 0, 10)];
    a.runToastAction(staleSnap.id);
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [10]);
    assert.ok(a.toasts.some(x => x.type === 'warning'));
    // no release: nothing to snap to, no request
    a = mkApp();
    let called = false;
    global.fetch = async () => { called = true; return { ok: true, json: async () => ({}) }; };
    await a.snapToDiscogs();
    assert.strictEqual(called, false);

    // ---- a failed re-analysis keeps the previous boundaries (there was no confirm) ----
    a = mkApp();
    const rel3 = { id: 9, tracks: [] };
    a.detectedTracks = [trk(1, 0, 100), trk(2, 100, 200)];
    a.selectedRelease = rel3;
    // analyzeFile reports its own error toast and leaves detectedTracks empty
    a.analyzeFile = async () => { a.notify('Analysis failed: boom', { type: 'error' }); };
    await a.reanalyzeFile();
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [100, 200]);
    assert.strictEqual(a.selectedRelease, rel3);
    assert.strictEqual(a.waveformRegions.list.length, 2);
    assert.ok(!a.toasts.some(x => x.action), 'nothing to undo');

    // ---- undo of removing a current file that had no tracks yet selects it cleanly ----
    a = mkApp();
    a.removeFile('f2', 5000);
    a.runToastAction(a.toasts[0].id);
    await sleep(5);
    assert.strictEqual(a.currentFileId, 'f2');
    assert.strictEqual(a.currentFile.id, 'f2');
    assert.strictEqual(a.searchQuery, a.cleanFilename('two.wav'), 'selected like a fresh click');

    // ---- a file that is exporting can't be removed, even when another file is open (review #86-1) ----
    deletes.length = 0;
    global.fetch = async (url, opts) => {
        deletes.push(`${(opts && opts.method) || 'GET'} ${url}`);
        return { ok: true, status: 200, json: async () => (url === '/api/process' ? { job_id: 'j1' } : {}) };
    };
    a = mkApp();
    a.detectedTracks = [trk(1, 0, 100)];
    a.customMapping = [0];
    a.selectedRelease = { id: 1, tracks: [{ position: 'A1' }] };
    await a.processFile();                  // exports f2
    a.stopProcessPolling();                 // no polling timer in this test
    assert.strictEqual(a.uploadedFiles[1].status, 'processing');
    a.selectFile('f1');
    a.removeFile('f2', 20);
    assert.deepStrictEqual(a.uploadedFiles.map(f => f.id), ['f1', 'f2', 'f3'], 'the exporting file stays');
    assert.ok(a.toasts.some(t => t.type === 'warning'));
    assert.deepStrictEqual(a.pendingRemovals, {});
    await sleep(60);
    await a.commitAllRemovals();
    assert.ok(!deletes.some(d => d.startsWith('DELETE')), 'no DELETE for a file being exported');
    global.fetch = async (url, opts) => { deletes.push(`${opts.method} ${url}`); return { ok: true, status: 200, json: async () => ({}) }; };

    // ...also while its export request is still in flight (status not yet confirmed by the server)
    deletes.length = 0;
    let answerProcess;
    global.fetch = (url, opts) => {
        deletes.push(`${(opts && opts.method) || 'GET'} ${url}`);
        if (url === '/api/process') return new Promise(resolve => { answerProcess = resolve; });
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    };
    a = mkApp();
    a.detectedTracks = [trk(1, 0, 100)];
    a.customMapping = [0];
    a.selectedRelease = { id: 1, tracks: [{ position: 'A1' }] };
    const exporting = a.processFile();      // exports f2; the server hasn't answered yet
    a.selectFile('f1');
    a.removeFile('f2', 20);
    assert.deepStrictEqual(a.uploadedFiles.map(f => f.id), ['f1', 'f2', 'f3'], 'a file whose export is starting stays');
    assert.ok(a.toasts.some(t => t.type === 'warning'));
    answerProcess({ ok: true, status: 200, json: async () => ({ job_id: 'j2' }) });
    await exporting;
    a.stopProcessPolling();
    await sleep(60);
    assert.ok(!deletes.some(d => d.startsWith('DELETE')), 'no DELETE for a file whose export is starting');
    // a failed start puts the previous status back, so the file can be removed again
    global.fetch = async () => ({ ok: false, status: 500, json: async () => ({ detail: 'boom' }) });
    a = mkApp();
    a.detectedTracks = [trk(1, 0, 100)];
    a.customMapping = [0];
    a.selectedRelease = { id: 1, tracks: [{ position: 'A1' }] };
    await a.processFile();
    assert.strictEqual(a.uploadedFiles[1].status, 'uploaded');
    assert.strictEqual(a.isProcessing, false);
    global.fetch = async (url, opts) => { deletes.push(`${opts.method} ${url}`); return { ok: true, status: 200, json: async () => ({}) }; };

    // ---- delete-track Undo keeps a search made after the deletion (review #86-4a) ----
    a = mkApp();
    a.detectedTracks = [trk(1, 0, 100), trk(2, 100, 200)];
    a.customMapping = [0, 1];
    a.searchResults = [{ id: 1 }]; a.searchQuery = 'old query';
    a.deleteTrack(2);
    const newResults = [{ id: 2 }];
    a.searchResults = newResults; a.searchQuery = 'new query';   // a Discogs search after the delete
    a.runToastAction(a.toasts[a.toasts.length - 1].id);
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [100, 200]);
    assert.strictEqual(a.searchResults, newResults, 'Undo leaves the newer search results alone');
    assert.strictEqual(a.searchQuery, 'new query');

    // undoing the removal of the open file brings its search results and query back
    a = mkApp();
    const oldResults = [{ id: 4 }];
    a.detectedTracks = [trk(1, 0, 100)];
    a.searchResults = oldResults; a.searchQuery = 'two query';
    a.removeFile('f2', 5000);
    assert.deepStrictEqual(a.searchResults, []);
    a.runToastAction(a.toasts[0].id);
    await sleep(5);
    assert.strictEqual(a.searchResults, oldResults);
    assert.strictEqual(a.searchQuery, 'two query');

    // ---- the toast cap never commits a pending removal (review #86-2) ----
    deletes.length = 0;
    a = mkApp();
    a.uploadedFiles = ['f1', 'f2', 'f3', 'f4', 'f5', 'f6'].map(id => ({ id, filename: `${id}.wav`, status: 'uploaded' }));
    a.currentFileId = null; a.currentFile = null;
    for (const f of [...a.uploadedFiles]) a.removeFile(f.id, 5000);
    await sleep(10);
    assert.deepStrictEqual(deletes, [], 'no DELETE inside any undo window');
    assert.strictEqual(a.toasts.filter(t => t.action).length, 6, 'every removal keeps its Undo');
    for (const t of [...a.toasts]) a.runToastAction(t.id);
    assert.deepStrictEqual(a.uploadedFiles.map(f => f.id), ['f1', 'f2', 'f3', 'f4', 'f5', 'f6']);
    await sleep(10);
    assert.deepStrictEqual(deletes, []);

    // ...nor drops a delete-track Undo when error toasts pile up after it
    a = mkApp();
    a.detectedTracks = [trk(1, 0, 100), trk(2, 100, 200)];
    a.customMapping = [0, 1];
    a.deleteTrack(2);
    const delUndo = a.toasts[a.toasts.length - 1];
    for (let n = 1; n <= 5; n++) a.notify(`Failure ${n}`, { type: 'error' });
    assert.ok(a.toasts.some(t => t.id === delUndo.id), 'the Undo toast survives the cap');
    assert.deepStrictEqual(a.toasts.map(t => t.message).slice(1), ['Failure 2', 'Failure 3', 'Failure 4', 'Failure 5'],
        'the oldest toast without an action made room');
    a.runToastAction(delUndo.id);
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [100, 200]);

    // ---- Undo puts files back in their old order (review #86-3) ----
    const ids = (app) => app.uploadedFiles.map(f => f.id);
    const removalToast = (app, id) => app.toasts.find(t => t.id === app.pendingRemovals[id].toastId);
    for (const undoOrder of [['f1', 'f2'], ['f2', 'f1']]) {
        a = mkApp(); a.currentFileId = null; a.currentFile = null;
        a.removeFile('f1', 5000); a.removeFile('f2', 5000);
        const toastsById = { f1: removalToast(a, 'f1'), f2: removalToast(a, 'f2') };
        for (const id of undoOrder) a.runToastAction(toastsById[id].id);
        assert.deepStrictEqual(ids(a), ['f1', 'f2', 'f3'], `undo ${undoOrder.join(' then ')}`);
        await a.commitAllRemovals();
    }
    // a file added during the window doesn't break the order
    a = mkApp(); a.currentFileId = null; a.currentFile = null;
    a.removeFile('f2', 5000);
    a.uploadedFiles = [...a.uploadedFiles, { id: 'f4', filename: 'four.wav', status: 'uploaded' }];
    a.runToastAction(removalToast(a, 'f2').id);
    assert.deepStrictEqual(ids(a), ['f1', 'f2', 'f3', 'f4']);
    // a failed DELETE puts the file back between its old neighbours
    global.fetch = async () => ({ ok: false, status: 500, json: async () => ({}) });
    a = mkApp(); a.currentFileId = null; a.currentFile = null;
    a.removeFile('f2', 5000);
    await a.commitAllRemovals();
    assert.deepStrictEqual(ids(a), ['f1', 'f2', 'f3']);
    global.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });

    // ---- an Undo refuses while track detection or an export runs (review #86-4b) ----
    const deleteThenBlock = (block) => {
        const app = mkApp();
        app.detectedTracks = [trk(1, 0, 100), trk(2, 100, 200)];
        app.customMapping = [0, 1];
        app.deleteTrack(2);
        const undo = app.toasts[app.toasts.length - 1];
        block(app);
        app.runToastAction(undo.id);
        return app;
    };
    a = deleteThenBlock(app => { app.analyzing = true; });
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [100], 'undo during detection leaves the tracks alone');
    assert.ok(a.toasts.some(x => x.type === 'warning'));
    a = deleteThenBlock(app => { app.isProcessing = true; });
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [100], 'undo during an export leaves the tracks alone');
    assert.ok(a.toasts.some(x => x.type === 'warning'));
    // re-analyze Undo while a detection runs
    a = mkApp();
    a.detectedTracks = [trk(1, 0, 100)];
    a.analyzeFile = async () => { a.detectedTracks = [trk(1, 0, 50)]; };
    await a.reanalyzeFile();
    const reUndo = a.toasts[a.toasts.length - 1];
    a.analyzing = true;
    a.runToastAction(reUndo.id);
    await sleep(5);
    assert.deepStrictEqual(a.detectedTracks.map(x => x.end), [50], 're-analyze undo during detection is refused');
    assert.ok(a.toasts.some(x => x.type === 'warning'));
    // file removal stays undoable while another file is open or an export runs
    a = mkApp();
    a.removeFile('f1', 5000);
    a.isProcessing = true;
    a.runToastAction(removalToast(a, 'f1').id);
    assert.deepStrictEqual(ids(a), ['f1', 'f2', 'f3']);

    console.log('undo ok');
})().catch(err => { console.error(err); process.exit(1); });
