// Review fixes of the track-table / Play-Pause player PR: the count-mismatch box after an empty detection,
// the volume slider, the plain-audio fallback when the waveform cannot load, the media `error` event,
// the right-click split messages, the notice for a clamped drag, the "3.75" length hint, and the markup of
// the row buttons.  initWaveform runs against fakes (WaveSurfer, its media element, fetch, localStorage).
// Every async step sets `finished` at the end: run.js only checks exit codes, and a fake fetch that is
// never answered would otherwise exit 0 without reaching its asserts.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'index.html'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();
const track = (number, start, end) => ({ number, start, end, duration: end - start, ignored: false, editing: false });

let finished = false;
process.on('exit', () => { if (!finished) { console.error('pr99_review: async part did not finish'); process.exitCode = 1; } });

// ---------- markup (sync) ----------
// The table card shows for tracks OR the count warning; header and table only with tracks
assert.ok(/<div x-show="detectedTracks\.length > 0 \|\| showTrackCountMismatch" x-cloak class="bg-card rounded-lg shadow p-6">/.test(html), 'card condition');
assert.ok(/<div x-show="detectedTracks\.length > 0" class="flex flex-wrap justify-between[^"]*">\s*<h3[^>]*>\s*Tracks \(/.test(html), 'header only with tracks');
assert.ok(/<div x-show="detectedTracks\.length > 0" class="track-table-scroll">/.test(html), 'table only with tracks');
assert.ok(/<div x-show="showTrackCountMismatch" class="mb-4 p-4 rounded-lg border-2"/.test(html), 'the box has its own condition');
// Row buttons wait for the waveform to be ready (getDuration() is 0 before 'ready')
for (const click of ["auditionCut(idx, 'start')", "auditionCut(idx, 'end')", 'toggleTrackPlay(idx)']) {
    const tag = html.match(new RegExp('<button @click="' + click.replace(/[()]/g, '\\$&') + '"[^>]*>'))[0];
    assert.ok(/:disabled="!playerReady/.test(tag), click + ' disabled until ready');
}
assert.ok(/:disabled="!playerReady \|\| \(track\.ignored && !\(isPlaying && playheadIdx === idx\)\)"/.test(html), 'a playing ignored row keeps its Pause');
// Header Play and zoom use the same readiness as the rows and Space
for (const click of ['togglePlay()', 'zoomIn()', 'zoomOut()']) {
    const tag = html.match(new RegExp('<button @click="' + click.replace(/[()]/g, '\\$&') + '"[^>]*>'))[0];
    assert.ok(/:disabled="!playerReady"/.test(tag), click + ' needs a ready player');
}
// No waveform but the fallback: no empty box, no 0:00 readout, no "drag the regions" tip, no placeholder
assert.ok(/<div id="waveform"[^>]*x-show="!fallbackAudioUrl"/.test(html), 'empty waveform box hidden');
assert.ok(/x-show="waveform && !fallbackAudioUrl"[^>]*>[^<]*<\/span>|<span x-show="waveform"[^>]*formatClock/.test(html), 'readout');
assert.ok(/x-show="!waveform && !waveformLoading && !fallbackAudioUrl"/.test(html), 'placeholder');
assert.ok(/<p x-show="!fallbackAudioUrl" class="text-sm text-muted">💡 Tip/.test(html), 'tip hidden with the fallback');
// Volume slider with a name and a visible focus ring; fallback player under the waveform
const slider = html.match(/<input type="range"[^>]*>/)[0];
assert.ok(/@input="setPlayerVolume\(\$event\.target\.value\)"/.test(slider), 'slider wiring');
assert.ok(/aria-label="Volume"/.test(slider) && /min="0"/.test(slider) && /max="1"/.test(slider) && /step="0.05"/.test(slider));
assert.ok(/\.volume-slider:focus-visible\s*\{[^}]*outline/.test(html), 'visible focus');
const audioTag = html.match(/<audio[^>]*>/)[0];
assert.ok(/controls/.test(audioTag) && /preload="metadata"/.test(audioTag) && /:src="fallbackAudioUrl \|\| false"/.test(audioTag), 'no src="" before a failure');
assert.ok(/@volumechange="onFallbackVolumeChange\(\$event\.target\)"/.test(audioTag), 'fallback volume wiring');
assert.ok(/x-show="fallbackAudioUrl"/.test(html));
// The region-click slop is gone
assert.ok(!/_regionDownX|REGION_CLICK_SLOP_PX/.test(src), 'dead drag-slop code removed');

// ---------- fakes ----------
function storage(initial) {
    const data = { ...initial };
    return { data, getItem: key => (key in data ? data[key] : null), setItem(key, value) { data[key] = String(value); } };
}
function emitter() {
    const handlers = {};
    return {
        handlers,
        on(name, fn) { (handlers[name] = handlers[name] || []).push(fn); },
        emit(name, ...args) { (handlers[name] || []).slice().forEach(fn => fn(...args)); },
    };
}
function fakeMedia() {
    return Object.assign(emitter(), {
        preload: null, volume: 1,
        addEventListener(name, fn) { this.on(name, fn); },
        removeEventListener(name, fn) { this.handlers[name] = (this.handlers[name] || []).filter(h => h !== fn); },
    });
}
function fakeWaveSurfer(media) {
    return Object.assign(emitter(), {
        registerPlugin: () => Object.assign(emitter(), { getRegions: () => [] }),
        getMediaElement: () => media,
        load: async () => {},
        destroyed: false,
        destroy() { this.destroyed = true; this.emit('destroy'); },
    });
}
function install(media, waveform) {
    global.window = global.window || {};
    global.WaveSurfer = { create: () => waveform, Regions: { create: () => ({}) } };
}
function mkApp(fileId = 'f1') {
    const app = vinylApp();
    app.toasts = []; app.notify = (message, options = {}) => { app.toasts.push({ message, ...options }); return app.toasts.length; };
    app.uploadedFiles = [{ id: fileId, filename: 'side.wav', status: 'analyzed' }];
    app.currentFileId = fileId;
    app.currentFile = app.uploadedFiles[0];
    app.detectedTracks = [track(1, 10, 100)];
    return app;
}

// ---------- volume: restored from storage, defensive reads ----------
const KEY = 'vinylflow.playerVolume.v1';
global.localStorage = storage({ [KEY]: '0.4' });
assert.strictEqual(vinylApp().playerVolume, 0.4, 'persisted volume restored');
for (const bad of ['loud', '', '2', '-1', 'NaN', 'Infinity']) {
    global.localStorage = storage({ [KEY]: bad });
    assert.strictEqual(vinylApp().playerVolume, 1, 'invalid stored value ' + JSON.stringify(bad));
}
global.localStorage = storage({});
assert.strictEqual(vinylApp().playerVolume, 1, 'nothing stored');
global.localStorage = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
const appWithoutStorage = vinylApp();
assert.strictEqual(appWithoutStorage.playerVolume, 1, 'unavailable storage');
appWithoutStorage.setPlayerVolume('0.5');   // must not throw
assert.strictEqual(appWithoutStorage.playerVolume, 0.5);
delete global.localStorage;
assert.strictEqual(vinylApp().playerVolume, 1, 'no localStorage at all');

(async () => {
    // ---- volume applies to the waveform's media element on creation and on change ----
    global.localStorage = storage({ [KEY]: '0.3' });
    let media = fakeMedia(), waveform = fakeWaveSurfer(media);
    install(media, waveform);
    global.fetch = async () => ({ ok: true, json: async () => ({ peaks: [0, 1], duration: 100 }) });
    let app = mkApp();
    await app.initWaveform();
    assert.strictEqual(media.volume, 0.3, 'applied right after the waveform is created');
    app.setPlayerVolume('0.75');
    assert.strictEqual(media.volume, 0.75);
    assert.strictEqual(global.localStorage.data[KEY], '0.75');
    app.setPlayerVolume('7'); assert.strictEqual(media.volume, 1, 'clamped');
    app.setPlayerVolume('abc'); assert.strictEqual(media.volume, 1, 'garbage ignored');
    assert.strictEqual(global.localStorage.data[KEY], '1');

    // ---- the media `error` event ends the buffering state ----
    media.emit('waiting'); assert.strictEqual(app.playerBuffering, true);
    media.emit('error'); assert.strictEqual(app.playerBuffering, false, 'an error is not "still buffering"');

    // ---- a successful build leaves no fallback player ----
    app.fallbackAudioUrl = '/api/audio/f1';
    app.destroyWaveform();
    assert.strictEqual(app.fallbackAudioUrl, '', 'destroyWaveform clears the fallback');
    app.fallbackAudioUrl = '/api/audio/f1';
    media = fakeMedia(); waveform = fakeWaveSurfer(media); install(media, waveform);
    await app.initWaveform();
    assert.strictEqual(app.fallbackAudioUrl, '', 'a rebuilt waveform clears the fallback');

    // ---- peaks request fails: a plain player for this file, at the chosen volume ----
    media = fakeMedia(); waveform = fakeWaveSurfer(media); install(media, waveform);
    global.fetch = async () => ({ ok: false, status: 500, json: async () => ({ detail: 'no peaks' }) });
    app = mkApp();
    const fallbackElement = { volume: 1 };
    app.$refs = { fallbackAudio: fallbackElement };
    await app.initWaveform();
    assert.strictEqual(app.fallbackAudioUrl, '/api/audio/f1');
    assert.strictEqual(app.waveformLoading, false);
    assert.ok(app.toasts.some(t => /waveform/i.test(t.message)), 'the failure is still reported');
    // The half-built waveform is gone: no Play that never settles, no zoom on nothing, one player only
    assert.strictEqual(app.waveform, null, 'waveform torn down after a failed build');
    assert.strictEqual(app.waveformRegions, null);
    assert.strictEqual(waveform.destroyed, true, 'and destroyed, not just forgotten');
    assert.strictEqual(app.playerReady, false);
    app.setPlayerVolume('0.2');
    assert.strictEqual(fallbackElement.volume, 0.2, 'volume reaches the fallback player');
    // the native control's own volume slider is remembered too
    app.onFallbackVolumeChange({ volume: 0.6 });
    assert.strictEqual(app.playerVolume, 0.6);
    // Switching file clears it (the switch destroys the waveform)
    app.destroyWaveform();
    assert.strictEqual(app.fallbackAudioUrl, '');

    // ---- the source itself is gone or changed (410/409): /api/audio answers the same, so no
    // fallback player that can never play; the toast carries the server's reason ----
    for (const status of [410, 409]) {
        media = fakeMedia(); waveform = fakeWaveSurfer(media); install(media, waveform);
        global.fetch = async () => ({ ok: false, status, json: async () => ({ detail: 'The source file is gone.' }) });
        app = mkApp();
        await app.initWaveform();
        assert.strictEqual(app.fallbackAudioUrl, '', status + ': no fallback player for a source the server refuses');
        assert.strictEqual(app.waveform, null, status + ': the half-built waveform is still torn down');
        assert.ok(app.toasts.some(t => /source file is gone/.test(t.message)), status + ': the reason is shown');
    }

    // ---- a failure after load() set the source (the audio request fails): same cleanup ----
    media = fakeMedia(); waveform = fakeWaveSurfer(media); install(media, waveform);
    waveform.load = async () => { throw new Error('audio failed'); };
    global.fetch = async () => ({ ok: true, json: async () => ({ peaks: [0, 1], duration: 100 }) });
    app = mkApp();
    await app.initWaveform();
    assert.strictEqual(app.waveform, null); assert.strictEqual(waveform.destroyed, true);
    assert.strictEqual(app.fallbackAudioUrl, '/api/audio/f1');

    // ---- clearing the fallback stops it (removing src does not stop a playing element) ----
    let paused = 0;
    app.$refs = { fallbackAudio: { pause() { paused++; } } };
    app.destroyWaveform();
    assert.strictEqual(paused, 1, 'the fallback player is paused when it goes away');
    assert.strictEqual(app.fallbackAudioUrl, '');

    // ---- stale builds: neither a late failure nor a late success touches a newer waveform ----
    for (const outcome of ['fails', 'succeeds', 'rejects']) {
        const mediaOne = fakeMedia(), waveformOne = fakeWaveSurfer(mediaOne);
        const mediaTwo = fakeMedia(), waveformTwo = fakeWaveSurfer(mediaTwo);
        const created = [waveformOne, waveformTwo];
        global.WaveSurfer = { create: () => created.shift(), Regions: { create: () => ({}) } };
        const answers = [];
        global.fetch = () => new Promise((resolve, reject) => answers.push(Object.assign(resolve, { reject })));
        app = mkApp('f1');
        const firstBuild = app.initWaveform();
        app.destroyWaveform();                       // re-analysis / Snap: the old build is abandoned
        const secondBuild = app.initWaveform();      // same file id, a newer build
        answers[1]({ ok: true, json: async () => ({ peaks: [0, 1], duration: 100 }) });
        await secondBuild;
        assert.strictEqual(app.waveform, waveformTwo);
        const loadsBefore = (waveformTwo.handlers.ready || []).length;
        if (outcome === 'rejects') answers[0].reject(new Error('network down'));
        else answers[0](outcome === 'fails'
            ? { ok: false, status: 500, json: async () => ({ detail: 'slow and broken' }) }
            : { ok: true, json: async () => ({ peaks: [0, 1], duration: 100 }) });
        await firstBuild;
        assert.strictEqual(app.waveform, waveformTwo, outcome + ': the newer waveform stays');
        assert.strictEqual(waveformTwo.destroyed, false, outcome + ': and is not destroyed');
        assert.strictEqual(app.fallbackAudioUrl, '', outcome + ': no fallback over a healthy waveform');
        assert.strictEqual(app.toasts.length, 0, outcome + ': no toast');
        assert.strictEqual((waveformTwo.handlers.ready || []).length, loadsBefore, outcome + ': nothing attached to the newer waveform');
    }

    // ---- the build goes stale while its response body is still being read ----
    {
        const mediaOne = fakeMedia(), waveformOne = fakeWaveSurfer(mediaOne);
        const mediaTwo = fakeMedia(), waveformTwo = fakeWaveSurfer(mediaTwo);
        const created = [waveformOne, waveformTwo];
        global.WaveSurfer = { create: () => created.shift(), Regions: { create: () => ({}) } };
        let releaseBody;
        let calls = 0;
        global.fetch = async () => (++calls === 1
            ? { ok: true, json: () => new Promise(resolve => { releaseBody = () => resolve({ peaks: [0, 1], duration: 100 }); }) }
            : { ok: true, json: async () => ({ peaks: [0, 1], duration: 100 }) });
        app = mkApp('f1');
        const firstBuild = app.initWaveform();
        await new Promise(resolve => setImmediate(resolve));   // the first build is waiting for the body
        app.destroyWaveform();
        await app.initWaveform();
        const readyHandlers = (waveformTwo.handlers.ready || []).length;
        releaseBody();
        await firstBuild;
        assert.strictEqual((waveformTwo.handlers.ready || []).length, readyHandlers, 'a stale body attaches nothing to the newer waveform');
        assert.strictEqual(app.waveform, waveformTwo);
    }

    // ---- "process next file" with nothing queued stops and removes whichever player is up ----
    {
        const mediaNext = fakeMedia(), waveformNext = fakeWaveSurfer(mediaNext);
        install(mediaNext, waveformNext);
        app = mkApp('f1');
        let fallbackPauses = 0;
        app.$refs = { fallbackAudio: { pause() { fallbackPauses++; } } };
        app.waveform = waveformNext; app.isPlaying = true;
        app.fallbackAudioUrl = '/api/audio/f1';
        app.uploadedFiles = [];
        app.resetForNextFile();
        assert.strictEqual(waveformNext.destroyed, true, 'the waveform is destroyed');
        assert.strictEqual(app.waveform, null);
        assert.strictEqual(app.isPlaying, false);
        assert.strictEqual(fallbackPauses, 1, 'the fallback is paused');
        assert.strictEqual(app.fallbackAudioUrl, '');
        assert.strictEqual(app.currentFileId, null);
    }

    // ---- the 100 ms timer after 'ready' belongs to its own build ----
    {
        const container = { offsetWidth: 1000, addEventListener() {} };
        global.document = { getElementById: () => container };
        const timers = [];
        const realSetTimeout = global.setTimeout;
        global.setTimeout = (fn) => { timers.push(fn); return 0; };
        const mediaOne = fakeMedia(), waveformOne = Object.assign(fakeWaveSurfer(mediaOne), { getDuration: () => 100, zoom() {} });
        const mediaTwo = fakeMedia(), waveformTwo = fakeWaveSurfer(mediaTwo);
        const created = [waveformOne, waveformTwo];
        global.WaveSurfer = { create: () => created.shift(), Regions: { create: () => ({}) } };
        global.fetch = async () => ({ ok: true, json: async () => ({ peaks: [0, 1], duration: 100 }) });
        app = mkApp('f1');
        await app.initWaveform();
        waveformOne.emit('ready');                  // schedules the region timer
        const timersAfterReady = timers.length;
        app.destroyWaveform();                      // Snap / Undo / re-detect within the 100 ms
        await app.initWaveform();                   // newer build, not ready yet
        let regionRuns = 0; app.addTrackRegions = () => { regionRuns++; };
        timers.slice(0, timersAfterReady).forEach(fn => fn());   // the old timer fires now
        global.setTimeout = realSetTimeout; delete global.document;
        assert.strictEqual(regionRuns, 0, 'the old timer draws nothing on the newer waveform');
        assert.strictEqual(app.waveformLoading, true, 'and does not end the newer build\'s loading state');
    }

    // ---- a file switch during a build leaves no "Loading waveform..." behind ----
    {
        const mediaSwitch = fakeMedia(), waveformSwitch = fakeWaveSurfer(mediaSwitch);
        install(mediaSwitch, waveformSwitch);
        let answerPeaks;
        global.fetch = () => new Promise(resolve => { answerPeaks = resolve; });
        app = mkApp('f1');
        const build = app.initWaveform();
        assert.strictEqual(app.waveformLoading, true);
        app.destroyWaveform(); app.currentFileId = 'f2';   // what selecting another file does
        assert.strictEqual(app.waveformLoading, false, 'destroying the waveform ends its loading state');
        answerPeaks({ ok: true, json: async () => ({ peaks: [0], duration: 10 }) });
        await build;
        assert.strictEqual(app.waveformLoading, false);
    }

    // ---- a playing fallback is stopped when a rebuild hides it (no destroyWaveform on that path) ----
    {
        const mediaRebuild = fakeMedia(), waveformRebuild = fakeWaveSurfer(mediaRebuild);
        install(mediaRebuild, waveformRebuild);
        global.fetch = async () => ({ ok: true, json: async () => ({ peaks: [0], duration: 10 }) });
        app = mkApp('f1');
        let fallbackPauses = 0;
        app.$refs = { fallbackAudio: { pause() { fallbackPauses++; } } };
        app.fallbackAudioUrl = '/api/audio/f1';
        await app.initWaveform();
        assert.strictEqual(fallbackPauses, 1, 'the fallback is paused before it is hidden');
        assert.strictEqual(app.fallbackAudioUrl, '');
    }

    // ---- after a failed build the hidden container is shown again before WaveSurfer measures it ----
    {
        const container = { offsetParent: null };
        global.document = { getElementById: () => container };
        const mediaHidden = fakeMedia(), waveformHidden = fakeWaveSurfer(mediaHidden);
        let measuredVisible = null;
        global.WaveSurfer = { create: () => { measuredVisible = container.offsetParent !== null; return waveformHidden; }, Regions: { create: () => ({}) } };
        global.fetch = async () => ({ ok: true, json: async () => ({ peaks: [0], duration: 10 }) });
        app = mkApp('f1');
        let tickCallback;
        app.$nextTick = async () => { container.offsetParent = {}; if (tickCallback) tickCallback(); };
        await app.initWaveform();
        assert.strictEqual(measuredVisible, true, 'the container is visible when WaveSurfer is created');
        // ... and a build that was superseded or switched away while waiting creates nothing
        for (const interfere of [a => { a.currentFileId = 'f2'; }, a => { a.waveform = { newer: true }; }]) {
            container.offsetParent = null; measuredVisible = null;
            app = mkApp('f1');
            app.$nextTick = async () => { interfere(app); };
            await app.initWaveform();
            assert.strictEqual(measuredVisible, null, 'no waveform is created for a superseded build');
        }
        delete global.document;
    }

    // ---- a failure that arrives after the user moved to another file says nothing ----
    media = fakeMedia(); waveform = fakeWaveSurfer(media); install(media, waveform);
    let answer;
    global.fetch = () => new Promise(resolve => { answer = resolve; });
    app = mkApp('f1');
    const staleInit = app.initWaveform();
    app.currentFileId = 'f2';
    app.destroyWaveform();
    answer({ ok: false, status: 500, json: async () => ({ detail: 'late' }) });
    await staleInit;
    assert.strictEqual(app.fallbackAudioUrl, '', 'stale failure for the previous file');
    assert.strictEqual(app.toasts.length, 0, 'and no toast about a waveform nobody is looking at');

    // ---- the media error event wiring is removed with the waveform (no leak) ----
    media = fakeMedia(); waveform = fakeWaveSurfer(media); install(media, waveform);
    global.fetch = async () => ({ ok: true, json: async () => ({ peaks: [0], duration: 100 }) });
    app = mkApp(); await app.initWaveform(); waveform.emit('destroy');
    assert.ok(Object.values(media.handlers).every(list => list.length === 0));

    // ---- an empty guided detection leaves the count warning up (the box is where Smart rescan lives) ----
    global.fetch = async () => ({ ok: true, json: async () => ({ method: 'count', matched: false, tracks: [], boundaries: [] }) });
    app = mkApp();
    app.$nextTick = async () => {}; app.$refs = {};
    app.detectedTracks = [];
    app.selectedRelease = { id: 1, tracks: ['A1', 'A2'].map(position => ({ position, title: position, duration: '3:00' })) };
    app.selectedSides = ['A'];
    await app.redetectForTargets();
    assert.strictEqual(app.detectedTracks.length, 0);
    assert.strictEqual(app.showTrackCountMismatch, true, 'mismatch box shown with 0 detected tracks');

    // ---- right-click split: between tracks says so; on a boundary keeps the old message ----
    for (const [menu, split] of [['contextMenu', 'splitAtContextMenu'], ['regionContextMenu', 'splitAtRegionContextMenu']]) {
        app = mkApp();
        app.detectedTracks = [track(1, 10, 100), track(2, 120, 200)];
        app.addTrackRegions = () => {};
        app[menu].time = 110; app[split]();
        assert.strictEqual(app.toasts.at(-1).message, 'This spot is between tracks; right-click inside a track to split it.', menu + ' gap');
        app[menu].time = 5; app[split]();
        assert.strictEqual(app.toasts.at(-1).message, 'This spot is between tracks; right-click inside a track to split it.', menu + ' before all tracks');
        app[menu].time = 250; app[split]();
        assert.strictEqual(app.toasts.at(-1).message, 'This spot is between tracks; right-click inside a track to split it.', menu + ' after all tracks');
        app[menu].time = 100; app[split]();
        assert.strictEqual(app.toasts.at(-1).message, 'Cannot split here: the position must be inside a track, not on a boundary.', menu + ' on a boundary');
        assert.strictEqual(app.detectedTracks.length, 2);
    }

    // ---- a clamped drag: one info toast; a plain drag and a setOptions echo: none ----
    const dragApp = (tracks) => { const a = mkApp(); a.detectedTracks = tracks; a.playDuration = 1000; a.syncRegionsAround = () => {}; return a; };
    app = dragApp([track(1, 0, 100), track(2, 100, 200)]);
    app.updateTrackFromRegion({ id: 'track-1', start: 0, end: 150 });
    assert.strictEqual(app.toasts.length, 0, 'an ordinary drag');
    app.updateTrackFromRegion({ id: 'track-1', start: 0, end: 150 });
    assert.strictEqual(app.toasts.length, 0, 'the setOptions echo of the synced region');
    app = dragApp([track(1, 0, 100), track(2, 100, 200)]);
    app.updateTrackFromRegion({ id: 'track-1', start: 0, end: 300 });   // squeezed: track 2 keeps 1 s
    assert.deepStrictEqual(app.detectedTracks.map(t => [t.start, t.end]), [[0, 199], [199, 200]]);
    assert.strictEqual(app.toasts.length, 1);
    assert.strictEqual(app.toasts[0].message, 'Track 1 stops here: every track keeps at least 1 s.');
    assert.strictEqual(app.toasts[0].type, 'info');
    // a start dragged below 0 is not "the end of the recording"
    app = dragApp([track(1, 5, 100), track(2, 100, 150)]);
    app.updateTrackFromRegion({ id: 'track-1', start: -20, end: 100 });
    assert.ok(!/recording/.test(app.toasts.at(-1)?.message || ''), 'start below 0: no end-of-recording wording');
    app = dragApp([track(1, 0, 100), track(2, 100, 150)]);
    app.playDuration = 200;
    app.updateTrackFromRegion({ id: 'track-2', start: 100, end: 260 });   // past the recording
    assert.strictEqual(app.toasts[0].message, 'Track 2 stops at the end of the recording.');
    app.updateTrackFromRegion({ id: 'track-2', start: 100, end: 260 });   // already there: nothing can move
    assert.strictEqual(app.toasts[1].message, "Track 2 can't move past the end of the recording.");
    app = dragApp([track(1, 0, 100), track(2, 100, 100.5)]);
    app.updateTrackFromRegion({ id: 'track-1', start: 0, end: 120 });   // nothing can move
    assert.deepStrictEqual(app.detectedTracks.map(t => [t.start, t.end]), [[0, 100], [100, 100.5]]);
    assert.strictEqual(app.toasts.length, 1);
    assert.strictEqual(app.toasts[0].message, "Track 1 can't move there: every track keeps at least 1 s.");
    app = dragApp([track(1, 0, 100)]);
    app.updateTrackFromRegion({ id: 'expected-1', start: 0, end: 50 });
    assert.strictEqual(app.toasts.length, 0, 'ghost markers are not tracks');

    // ---- "3.75" is no m:ss slip: no "Use 3:75" action ----
    app = dragApp([track(1, 10, 235), track(2, 235, 470)]);
    const field = { value: '3.75' };
    assert.strictEqual(app.setTrackLength(0, '3.75', field), false);
    assert.strictEqual(field.value, '3:45');
    assert.strictEqual(app.toasts.at(-1).message, 'Type a length like 3:45 (minutes:seconds).');
    assert.ok(!app.toasts.at(-1).action, 'no action for seconds >= 60');
    app.setTrackLength(0, '3.59', { value: '' });
    assert.strictEqual(app.toasts.at(-1).action.label, 'Use 3:59', 'seconds below 60 still get the offer');

    finished = true;
    console.log('pr99_review ok');
})().catch(error => { console.error(error); process.exit(1); });
