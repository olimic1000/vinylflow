// Round-2 review fixes for the merged track table and the single Play/Pause player.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'index.html'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();

const trk = (n, s, e) => ({ number: n, start: s, end: e, duration: e - s, ignored: false, editing: false });
function mkApp(tracks) {
    const app = vinylApp();
    app.detectedTracks = tracks;
    app.toasts = []; app.notify = (m, o = {}) => { app.toasts.push({ m, ...o }); return app.toasts.length; };
    app.syncRegionsAround = () => {};
    return app;
}
const nextTask = () => new Promise(resolve => setImmediate(resolve));
let finished = false;
process.on('exit', () => { if (!finished) { console.error('pr21_round2: did not finish'); process.exitCode = 1; } });

/** Fake player. A rejected play() can leave the media element "playing" (media.paused false). */
function fakeWaveform({ rejectLeavesPlaying = false } = {}) {
    const handlers = {};
    let playing = false, rejectNext = false, playCalls = 0;
    const waveform = {
        on(name, fn) { (handlers[name] = handlers[name] || []).push(fn); },
        emit(name, ...args) { (handlers[name] || []).forEach(fn => fn(...args)); },
        isPlaying: () => playing,
        getDuration: () => 600,
        play() {
            playCalls++;
            if (rejectNext) { if (rejectLeavesPlaying) playing = true; return Promise.reject(new Error('NotSupportedError')); }
            playing = true; waveform.emit('play'); return Promise.resolve();
        },
        pause() { if (playing) { playing = false; waveform.emit('pause'); } },
        rejectNext(value) { rejectNext = value; },
        playCalls: () => playCalls,
    };
    return waveform;
}

(async () => {
    // ---- 1. a failed play leaves the next Play click working ----
    let app = mkApp([trk(1, 10, 100)]);
    let waveform = app.waveform = fakeWaveform({ rejectLeavesPlaying: true });
    waveform.on('play', () => { app.isPlaying = true; });
    waveform.on('pause', () => { app.isPlaying = false; });
    waveform.rejectNext(true);
    app.togglePlay(); await nextTask();
    assert.strictEqual(app.isPlaying, false);
    assert.strictEqual(waveform.isPlaying(), false, 'the failed play is stopped on the player too');
    waveform.rejectNext(false);
    app.togglePlay(); await nextTask();
    assert.strictEqual(waveform.playCalls(), 2, 'the next click plays again instead of pausing');
    assert.strictEqual(app.isPlaying, true);

    // togglePlay follows the app's own state, like the row buttons
    app = mkApp([trk(1, 10, 100)]);
    waveform = app.waveform = fakeWaveform();
    waveform.isPlaying = () => true;
    app.isPlaying = false;
    app.togglePlay();
    assert.strictEqual(waveform.playCalls(), 1);

    // the rejection handler must clear the flag itself (the player here reports "paused")
    app = mkApp([trk(1, 10, 100)]);
    waveform = app.waveform = fakeWaveform();
    app.isPlaying = true;
    waveform.rejectNext(true);
    app.startPlayback(); await nextTask();
    assert.strictEqual(app.isPlaying, false);

    // ---- 2. a neighbour already under 1 s does not block the adjacent cut ----
    app = mkApp([trk(1, 10, 100), trk(2, 100, 100.5), trk(3, 100.5, 200)]);
    assert.ok(app.applyTrackBounds(0, 10, 99), 'shrinking track 1 leaves the tiny neighbour alone');
    assert.strictEqual(app.detectedTracks[0].end, 99);
    app = mkApp([trk(1, 10, 100), trk(2, 100, 100.5), trk(3, 100.5, 200)]);
    assert.ok(app.applyTrackBounds(2, 101, 200), 'moving track 3 start away from the tiny previous track');
    assert.strictEqual(app.detectedTracks[2].start, 101);

    // ---- 3. a typed length capped by the recording end is explained even when nothing changed ----
    app = mkApp([trk(1, 100, 200)]);
    app.playDuration = 200;
    const input = { value: '5:00' };
    assert.strictEqual(app.setTrackLength(0, '5:00', input), false);
    assert.strictEqual(app.toasts.length, 1, 'the notice is shown');
    assert.match(app.toasts[0].m, /can only be/);
    assert.strictEqual(input.value, '1:40');

    // ---- 6a. a typed track under 1 s is refused ----
    app = mkApp([trk(1, 10, 100), trk(2, 100, 200)]);
    assert.strictEqual(app.updateTrackEnd(0, '10.5', { value: '10.5' }), false);
    assert.match(app.toasts.at(-1).m, /at least 1 s/);
    assert.strictEqual(app.detectedTracks[0].end, 100);

    // ---- 6b. a play that RESOLVES for a replaced waveform leaves the new one alone ----
    app = mkApp([trk(1, 10, 100)]);
    let resolvePending;
    app.waveform = { play: () => new Promise(resolve => { resolvePending = resolve; }) };
    app.startPlayback();
    app.waveform = fakeWaveform();
    app.playerNotice = 'Could not start playback: new waveform problem';
    resolvePending(); await nextTask();
    assert.strictEqual(app.playerNotice, 'Could not start playback: new waveform problem');

    // ---- 6c. init() registers the Space listener ----
    app = vinylApp();
    const listeners = {};
    global.document = { addEventListener: (name, fn) => { listeners[name] = fn; } };
    global.window = { addEventListener() {} };
    app.loadConfig = app.loadFormats = app.checkStatus = app.loadImportList = async () => {};
    app.connectWebSocket = app.detectDesktopPicker = app.chooseDefaultImportTab = () => {};
    app.$watch = () => {};
    let routed = null;
    app.onGlobalKeydown = (e) => { routed = e; };
    try { await app.init(); } catch (err) { /* the rest of init is not under test */ }
    delete global.document; delete global.window;
    assert.strictEqual(typeof listeners.keydown, 'function', 'init registers a keydown listener');
    for (const name of ['keyup', 'pointerdown', 'pointerup', 'focusin']) assert.strictEqual(typeof listeners[name], 'function', `init registers ${name}`);
    const event = { code: 'Space' };
    listeners.keydown(event);
    assert.strictEqual(routed, event, 'the keydown handler routes to onGlobalKeydown');

    // ---- 4/5. markup ----
    assert.ok(!/Amber buttons: white on #E5A100/.test(html), 'stale amber-button comment is gone');
    assert.ok(html.includes(':disabled="!playerReady || (track.ignored && !(isPlaying && playheadIdx === idx))"'),
        'the playing row keeps its Pause enabled even when ignored');

    finished = true;
    console.log('pr21_round2: ok');
})();
