// Snap to Discogs: guided detection on demand.  It replaces hand edits, so it is the choice on the
// edit banner, and an Undo toast puts the boundaries, mapping and ghost markers back.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();
const tick = () => new Promise(resolve => setImmediate(resolve));
const release = { id: 1, tracks: ['A1', 'A2', 'B1'].map(position => ({ position, title: 't' + position, duration: '1:40' })) };
const trk = (n, s, e) => ({ number: n, start: s, end: e, duration: e - s, editing: false, ignored: false });

function mkRegions() {
    const list = [];
    return { list, getRegions: () => list, clearRegions() { list.length = 0; },
        addRegion(o) { const r = { ...o, end: o.end ?? o.start, element: { style: {} }, remove() { list.splice(list.indexOf(r), 1); }, setOptions(x) { Object.assign(r, x); } }; list.push(r); return r; } };
}
function mkApp() {
    const app = vinylApp();
    app.$nextTick = async () => {};
    app.$refs = {};
    app.initWaveform = async () => { app.waveform = { getDuration: () => 600, destroy() {}, pause() {} }; app.waveformRegions = mkRegions(); app.addTrackRegions(); };
    app.currentFileId = 'f';
    app.uploadedFiles = [{ id: 'f', filename: 'a.wav' }];
    app.currentFile = { duration: 600 };
    app.waveform = { getDuration: () => 600, destroy() {}, pause() {} };
    app.waveformRegions = mkRegions();
    // An edited recording of the three-track release, hand-adjusted
    app.selectedRelease = release;
    app.selectedSides = ['A', 'B'];
    app.detectedTracks = [trk(1, 0, 90), trk(2, 90, 210), trk(3, 210, 300)];
    app.expectedBoundaries = [100, 200];
    app.resetMappingToTargets();
    app.markEdited();
    app.addTrackRegions();
    return app;
}
let reply;
const requests = [];
global.fetch = async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return reply();
};
const guidedOk = () => ({ ok: true, json: async () => ({ method: 'durations', matched: true, music_start: 0, music_end: 300,
    tracks: [trk(1, 0, 100), trk(2, 100, 200), trk(3, 200, 300)],
    boundaries: [{ expected: 100, snapped: true }, { expected: 200, snapped: true }] }) });

(async () => {
    // ---- snap replaces the hand edits and offers Undo ----
    reply = guidedOk;
    let app = mkApp();
    app.editBanner = true;
    await app.snapToDiscogs();
    assert.strictEqual(requests.length, 1);
    assert.strictEqual(requests[0].url, '/api/analyze-guided');
    assert.deepStrictEqual(requests[0].body.durations, [100, 100, 100]);
    assert.deepStrictEqual(requests[0].body.side_breaks, [2]);
    assert.deepStrictEqual(app.detectedTracks.map(t => t.end), [100, 200, 300]);
    assert.strictEqual(app.editBanner, false);
    assert.strictEqual(app.boundariesEdited, false, 'the snapped cuts are Discogs-placed, not hand edits');
    assert.deepStrictEqual(app.customMapping, [0, 1, 2]);
    const toast = app.toasts[app.toasts.length - 1];
    assert.strictEqual(toast.action.label, 'Undo');

    // ---- Undo: boundaries, edit flag, mapping and markers from before ----
    app.runToastAction(toast.id);
    assert.deepStrictEqual(app.detectedTracks.map(t => t.end), [90, 210, 300]);
    assert.strictEqual(app.boundariesEdited, true, 'the hand edits are hand edits again');
    assert.deepStrictEqual(app.expectedBoundaries, [100, 200]);
    assert.deepStrictEqual(app.waveformRegions.list.filter(r => r.id.startsWith('expected-')).map(r => r.start), [100, 200]);
    assert.strictEqual(app.waveformRegions.list.find(r => r.id === 'track-1').end, 90, 'regions redrawn from the snapshot');
    assert.deepStrictEqual(app.customMapping, [0, 1, 2]);
    assert.strictEqual(app.trackCountMismatch, false);

    // ---- a failed snap keeps the edits and offers no Undo ----
    reply = () => ({ ok: false, json: async () => ({ detail: 'envelope not ready' }) });
    app = mkApp();
    const toastsBefore = app.toasts.length;
    await app.snapToDiscogs();
    assert.deepStrictEqual(app.detectedTracks.map(t => t.end), [90, 210, 300]);
    assert.strictEqual(app.boundariesEdited, true);
    assert.ok(app.toasts.slice(toastsBefore).every(t => t.type === 'error' && !t.action), 'an error, nothing to undo');
    assert.strictEqual(app.analyzing, false);

    // ---- snap needs a release with tracks on the chosen sides, and no running detection ----
    reply = guidedOk;
    requests.length = 0;
    app = mkApp();
    app.selectedRelease = null;
    await app.snapToDiscogs();
    app = mkApp();
    app.selectedSides = [];
    await app.snapToDiscogs();
    app = mkApp();
    app.analyzing = true;
    await app.snapToDiscogs();
    assert.strictEqual(requests.length, 0);

    // ---- a side that is a subset: the snapped tracks map to that side's release indices ----
    app = mkApp();
    app.selectedSides = ['B'];
    reply = () => ({ ok: true, json: async () => ({ method: 'durations', matched: true, tracks: [trk(1, 0, 100)], boundaries: [] }) });
    await app.snapToDiscogs();
    assert.deepStrictEqual(app.customMapping, [2], 'B1 is release.tracks[2]');
    // ---- a snap that finds no tracks: a warning, no "Snapped" toast, nothing to undo into an empty editor ----
    reply = () => ({ ok: true, json: async () => ({ method: 'count', matched: false, tracks: [], boundaries: [] }) });
    app = mkApp();
    const emptyToastsBefore = app.toasts.length;
    const emptyResult = await app.snapToDiscogs();
    assert.strictEqual(emptyResult, undefined);
    const emptyToasts = app.toasts.slice(emptyToastsBefore);
    assert.ok(emptyToasts.some(t => t.type === 'warning'), 'says nothing was found');
    assert.ok(!emptyToasts.some(t => t.action), 'no Snapped / Undo toast');
    // ...and the hand-edited boundaries stay, with their waveform: a snap that finds nothing changes nothing
    assert.deepStrictEqual(app.detectedTracks.map(t => [t.start, t.end]), [[0, 90], [90, 210], [210, 300]]);
    assert.ok(app.waveform, 'the waveform is back');
    assert.strictEqual(app.boundariesEdited, true, 'still hand edits');
    assert.ok(app.waveformRegions.list.some(r => r.id === 'track-3'), 'regions redrawn');

    // ---- Undo rebuilds the waveform when there is none (a snap may have destroyed it) ----
    reply = guidedOk;
    app = mkApp();
    await app.snapToDiscogs();
    const snapToast = app.toasts[app.toasts.length - 1];
    app.destroyWaveform();
    assert.strictEqual(app.waveform, null);
    app.runToastAction(snapToast.id);
    await tick();
    assert.ok(app.waveform, 'the waveform is rebuilt');
    assert.strictEqual(app.waveformRegions.list.find(r => r.id === 'track-1').end, 90);

    // ---- Undo clears the "Placed N tracks" notice that belonged to the snap ----
    app = mkApp();
    await app.snapToDiscogs();
    assert.ok(app.rescanNotice.includes('Placed'));
    app.runToastAction(app.toasts[app.toasts.length - 1].id);
    await tick();
    assert.strictEqual(app.rescanNotice, '');

    console.log('snap to discogs ok');
})().catch(error => { console.error(error); process.exit(1); });
