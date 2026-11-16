// Guided detection guards: no re-detection during an export, no 100+ track requests, and a reply
// that a hand edit or release change has superseded is dropped instead of overwriting the edit.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();
const trk = (n, s, e) => ({ number: n, start: s, end: e, duration: e - s, editing: false, ignored: false });
const makeSidedRelease = () => ({ id: 2, tracks: ['A1', 'A2', 'B1'].map(position => ({ position, title: 't' + position, duration: '1:40' })) });
const makeRelease = count => ({ id: 1, tracks: Array.from({ length: count }, (_, i) => ({ position: 'A' + (i + 1), title: 't' + i, duration: '1:40' })) });

function mkRegions() {
    const list = [];
    return { list, getRegions: () => list, clearRegions() { list.length = 0; },
        addRegion(o) { const r = { ...o, end: o.end ?? o.start, element: { style: {} }, remove() { list.splice(list.indexOf(r), 1); }, setOptions(x) { Object.assign(r, x); } }; list.push(r); return r; } };
}
function mkApp(release = makeRelease(3)) {
    const app = vinylApp();
    app.$nextTick = async () => {};
    app.$refs = {};
    app.initWaveform = async () => { app.waveform = { getDuration: () => 600, destroy() {}, pause() {} }; app.waveformRegions = mkRegions(); app.addTrackRegions(); };
    app.currentFileId = 'f';
    app.uploadedFiles = [{ id: 'f', filename: 'a.wav' }];
    app.currentFile = { duration: 600 };
    app.waveform = { getDuration: () => 600, destroy() {}, pause() {} };
    app.waveformRegions = mkRegions();
    app.selectedRelease = release;
    app.selectedSides = ['A'];
    app.detectedTracks = [trk(1, 0, 90), trk(2, 90, 210), trk(3, 210, 300)];
    app.expectedBoundaries = [];
    app.resetMappingToTargets();
    app.addTrackRegions();
    return app;
}
const requests = [];
let pending = null;   // resolve(replyObject) completes the in-flight request
global.fetch = (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return new Promise(resolve => { pending = resolve; });
};
const guidedReply = () => ({ ok: true, json: async () => ({ method: 'durations', matched: true, music_start: 0, music_end: 300,
    tracks: [trk(1, 0, 100), trk(2, 100, 200), trk(3, 200, 300)],
    boundaries: [{ expected: 100, snapped: true }, { expected: 200, snapped: true }] }) });
const tick = () => new Promise(resolve => setImmediate(resolve));

// A request the test never answers leaves the script waiting on nothing: Node then exits 0 without
// reaching the end. Count that as a failure, or a guard that should prevent a request passes unseen.
let finished = false;
process.on('exit', () => {
    if (!finished) {
        console.error('guided supersede tests did not finish (an unexpected request is still pending)');
        process.exitCode = 1;
    }
});

(async () => {
    // ---- 1. nothing re-detects while THIS recording is exporting (each guard caught on its own) ----
    const startExport = app => { app.uploadedFiles.find(f => f.id === app.currentFileId).status = 'processing'; app.isProcessing = true; };
    const stateOf = app => JSON.stringify([app.selectedSides, app.customMapping, app.editBanner, app.detectedTracks.map(t => t.end)]);
    const guards = {
        toggleSide: app => app.toggleSide('B'),
        applyPreset: app => app.applyPreset(['A', 'B']),
        snapToDiscogs: app => app.snapToDiscogs(),
        redetectForTargets: app => app.redetectForTargets(),
        smartRescan: app => app.smartRescan(3),
        analyzeFile: app => app.analyzeFile(),
        reanalyzeFile: app => app.reanalyzeFile()
    };
    for (const [name, act] of Object.entries(guards)) {
        const guarded = mkApp(makeSidedRelease());
        guarded.selectedSides = ['A'];
        guarded.resetMappingToTargets();
        guarded.editBanner = true;   // snapToDiscogs clears it before detecting
        startExport(guarded);
        const stateBefore = stateOf(guarded);
        const waveformBefore = guarded.waveform;
        await act(guarded);
        assert.strictEqual(guarded.waveform, waveformBefore, name + ' rebuilt the waveform while exporting');
        assert.strictEqual(requests.length, 0, name + ' sent a request while exporting');
        assert.strictEqual(stateOf(guarded), stateBefore, name + ' changed state while exporting');
    }
    // a pick on the exporting recording is refused with a warning, the release stays
    let exporting = mkApp(makeSidedRelease());
    startExport(exporting);
    exporting.selectRelease({ id: 9, tracks: [{ position: 'A1', title: 'x', duration: '1:00' }] });
    assert.strictEqual(exporting.selectedRelease.id, 2);
    assert.strictEqual(requests.length, 0);
    assert.ok(exporting.toasts.some(t => t.type === 'warning'));
    // ...while another recording opened during that export detects normally
    let other = mkApp(makeSidedRelease());
    other.uploadedFiles = [{ id: 'f', filename: 'a.wav', status: 'uploaded' }, { id: 'g', filename: 'b.wav', status: 'processing' }];
    other.isProcessing = true;   // the export of g is running
    let otherRun = other.redetectForTargets();
    assert.strictEqual(requests.length, 1, 'a recording that is not exporting must detect');
    pending(guidedReply());
    assert.strictEqual(await otherRun, true);
    requests.length = 0;

    // ---- 2c. an undisturbed request still installs its tracks ----
    app = mkApp();
    let run = app.redetectForTargets();
    pending(guidedReply());
    assert.strictEqual(await run, true);
    assert.deepStrictEqual(app.detectedTracks.map(t => t.end), [100, 200, 300]);
    assert.deepStrictEqual(app.expectedBoundaries, [100, 200]);

    // ---- 2a. a hand edit while detecting wins ----
    requests.length = 0;
    app = mkApp();
    run = app.redetectForTargets();
    app.detectedTracks[0].end = 95;
    app.markEdited();
    pending(guidedReply());
    assert.strictEqual(await run, false);
    assert.deepStrictEqual(app.detectedTracks.map(t => t.end), [95, 210, 300]);
    assert.strictEqual(app.boundariesEdited, true);
    assert.deepStrictEqual(app.expectedBoundaries, []);
    assert.strictEqual(app.processingMessage, '');
    assert.strictEqual(app.analyzing, false);
    assert.ok(!app.toasts.some(t => /Kept your changes/.test(t.message)), 'the banner explains it, no toast');

    // ---- 2a'. an Undo restore while detecting counts as an edit too ----
    // ghost markers of the previous sides are cleared and the hand-edit banner asks what to do
    app = mkApp();
    app.expectedBoundaries = [100, 200];
    app.addTrackRegions();
    run = app.redetectForTargets();
    app.markEdited();
    pending(guidedReply());
    await run;
    assert.deepStrictEqual(app.expectedBoundaries, []);
    assert.strictEqual(app.waveformRegions.list.filter(r => r.id.startsWith('expected-')).length, 0);
    assert.strictEqual(app.editBanner, true);

    // ---- 2a''. a superseded failure clears markers and shows the banner too, without a failure toast ----
    app = mkApp();
    app.expectedBoundaries = [100, 200];
    run = app.redetectForTargets();
    app.markEdited();
    pending({ ok: false, json: async () => ({ detail: 'boom' }) });
    await run;
    assert.deepStrictEqual(app.expectedBoundaries, []);
    assert.strictEqual(app.editBanner, true);

    // ---- 2a'''. ignore / mapping / reverse are disabled while detecting (they would be lost or
    //      mistaken for boundary edits), so the functions refuse too ----
    app = mkApp();
    run = app.redetectForTargets();
    app.trackMappingReversed = true;   // updateCustomMapping would clear it, reverseMapping would flip it
    const mappingBefore = [...app.customMapping];
    app.toggleTrackIgnored(app.detectedTracks[1]);
    app.updateCustomMapping();
    app.reverseMapping();
    assert.strictEqual(app.detectedTracks[1].ignored, false);
    assert.deepStrictEqual(app.customMapping, mappingBefore);
    assert.strictEqual(app.trackMappingReversed, true);
    pending(guidedReply());
    assert.strictEqual(await run, true, 'no edit happened, so the reply installs');

    app = mkApp();
    const snapshot = app.snapshotEdits();
    run = app.redetectForTargets();
    app.restoreEdits(snapshot);
    pending(guidedReply());
    assert.strictEqual(await run, false);
    assert.deepStrictEqual(app.detectedTracks.map(t => t.end), [90, 210, 300]);

    // ---- 2b. the release was removed while detecting ----
    app = mkApp();
    run = app.redetectForTargets();
    app.selectedRelease = null;
    app.detectedTracks = [trk(1, 0, 90)];
    pending(guidedReply());
    assert.strictEqual(await run, false);
    assert.deepStrictEqual(app.detectedTracks.map(t => t.end), [90]);
    assert.deepStrictEqual(app.expectedBoundaries, []);
    assert.ok(!app.toasts.some(t => /Kept your changes/.test(t.message)), 'silent when the release changed');
    assert.strictEqual(app.editBanner, false);

    // ---- 2d. a superseded failure shows no failure toast ----
    app = mkApp();
    run = app.redetectForTargets();
    app.markEdited();
    pending({ ok: false, json: async () => ({ detail: 'boom' }) });
    assert.strictEqual(await run, false);
    assert.ok(!app.toasts.some(t => /failed/i.test(t.message)), 'no failure toast');
    assert.deepStrictEqual(app.detectedTracks.map(t => t.end), [90, 210, 300]);

    // ---- 2d'. snapping, then deleting the only track mid-detection: the snapshot is not restored ----
    app = mkApp();
    app.detectedTracks = [trk(1, 0, 300)];
    app.resetMappingToTargets();
    run = app.snapToDiscogs();
    await tick();
    app.detectedTracks = [];
    app.markEdited();   // what deleteTrack does
    pending(guidedReply());
    await run;
    assert.deepStrictEqual(app.detectedTracks, [], 'the user deleted it; Snap must not bring it back');

    // ---- 2e. a snap that was superseded offers no Undo and restores nothing ----
    app = mkApp();
    const toastCount = app.toasts.length;
    run = app.snapToDiscogs();
    await tick();
    app.markEdited();
    pending(guidedReply());
    await run;
    assert.ok(!app.toasts.slice(toastCount).some(t => t.action), 'no Undo toast');
    assert.deepStrictEqual(app.detectedTracks.map(t => t.end), [90, 210, 300]);

    // ---- 4. a status of 'processing' without the running flag does not lock anything ----
    requests.length = 0;
    let stale = mkApp(makeSidedRelease());
    stale.uploadedFiles[0].status = 'processing';
    stale.isProcessing = false;
    assert.strictEqual(stale.currentFileExporting, false);
    let staleRun = stale.redetectForTargets();
    assert.strictEqual(requests.length, 1, 'the isProcessing half of the getter');
    pending(guidedReply());
    await staleRun;
    requests.length = 0;

    // ---- 5. Change release is refused while this recording exports ----
    exporting = mkApp(makeSidedRelease());
    startExport(exporting);
    exporting.changeRelease();
    assert.strictEqual(exporting.selectedRelease.id, 2);

    // ---- 6. WebSocket errors: a recoverable (detection) error must not touch another file's export ----
    const wsApp = () => {
        const a = mkApp(makeSidedRelease());
        a.uploadedFiles = [{ id: 'f', filename: 'a.wav', status: 'processing' }, { id: 'g', filename: 'b.wav', status: 'uploaded' }];
        a.currentFileId = 'g';
        a.isProcessing = true;   // the export of f is running; g is open
        a.currentJobId = 'job';
        return a;
    };
    app = wsApp();
    app.handleWebSocketMessage({ type: 'error', file_id: 'g', message: 'detection broke', recoverable: true });
    assert.strictEqual(app.isProcessing, true);
    assert.strictEqual(app.currentJobId, 'job', 'polling of the export keeps running');
    assert.strictEqual(app.uploadedFiles[0].status, 'processing');
    assert.ok(!app.toasts.some(t => /Processing error/.test(t.message)), 'detection shows its own failure');
    // an export failure still resets as before
    app = wsApp();
    app.currentFileId = 'f';
    app.handleWebSocketMessage({ type: 'error', file_id: 'f', message: 'ffmpeg died', recoverable: false });
    assert.strictEqual(app.isProcessing, false);
    assert.strictEqual(app.uploadedFiles[0].status, 'error');
    assert.ok(app.toasts.some(t => /Processing error: ffmpeg died/.test(t.message)));

    // ---- 3. more than 99 tracks: no request, a warning notice ----
    requests.length = 0;
    app = mkApp(makeRelease(100));
    assert.strictEqual(await app.redetectForTargets(), false);
    assert.strictEqual(requests.length, 0);
    assert.ok(/100 tracks/.test(app.rescanNotice) && /99/.test(app.rescanNotice), app.rescanNotice);
    assert.ok(/Pick a disc or side/.test(app.rescanNotice) === (app.sideChips.length > 1));
    // one side only: no advice to pick a side; stale ghost markers go
    app = mkApp(makeRelease(100));
    app.expectedBoundaries = [100, 200];
    app.addTrackRegions();
    await app.redetectForTargets();
    assert.ok(!/Pick a disc or side/.test(app.rescanNotice), app.rescanNotice);
    assert.deepStrictEqual(app.expectedBoundaries, []);
    // several sides: the advice stays
    const bigSided = { id: 5, tracks: [...Array(60).keys()].map(i => ({ position: 'A' + (i + 1), title: 't', duration: '1:00' }))
        .concat([...Array(40).keys()].map(i => ({ position: 'B' + (i + 1), title: 't', duration: '1:00' }))) };
    app = mkApp(bigSided);
    app.selectedSides = ['A', 'B'];
    await app.redetectForTargets();
    assert.ok(/Pick a disc or side/.test(app.rescanNotice), app.rescanNotice);
    assert.strictEqual(app.maxGuidedTracks, 99);
    finished = true;
    console.log('guided supersede tests passed');
})().catch(error => { console.error(error); process.exit(1); });
