// The real initWaveform wires the player: WaveSurfer's play/pause/finish/timeupdate/interaction events
// drive the transport state, and the media element's events drive the buffering flag.  track_player.test.js
// tests the handlers; this one fails when the wiring itself is deleted.  WaveSurfer, its regions plugin and
// the media element are fakes whose events are emitted by hand; the peaks fetch fails on purpose so
// initWaveform stops right after the wiring.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();

global.window = global.window || {};
global.fetch = async () => ({ ok: false, status: 500, json: async () => ({ detail: 'stop here' }) });

function emitter() {
    const handlers = {};
    return {
        handlers,
        on(name, fn) { (handlers[name] = handlers[name] || []).push(fn); },
        emit(name, ...args) { (handlers[name] || []).slice().forEach(fn => fn(...args)); },
    };
}
const media = Object.assign(emitter(), {
    preload: null,
    addEventListener(name, fn) { this.on(name, fn); },
    removeEventListener(name, fn) { this.handlers[name] = (this.handlers[name] || []).filter(h => h !== fn); },
});
const waveform = Object.assign(emitter(), {
    registerPlugin: () => Object.assign(emitter(), { getRegions: () => [] }),
    getMediaElement: () => media,
    destroy() { this.emit('destroy'); },
});
global.WaveSurfer = { create: () => waveform, Regions: { create: () => ({}) } };

let finished = false;
process.on('exit', () => { if (!finished) { console.error('track_player_wiring: async part did not finish'); process.exitCode = 1; } });

(async () => {
    const app = vinylApp();
    app.uploadedFiles = [{ id: 'f1', filename: 'side.wav', status: 'analyzed' }];
    app.currentFileId = 'f1';
    app.currentFile = app.uploadedFiles[0];
    app.detectedTracks = [{ number: 1, start: 10, end: 100, duration: 90, ignored: false, editing: false }];
    await app.initWaveform();

    waveform.emit('play');
    assert.strictEqual(app.isPlaying, true, 'WaveSurfer "play" sets the transport');
    waveform.emit('timeupdate', 42);
    assert.strictEqual(app.playTime, 42, '"timeupdate" moves the clock');
    assert.strictEqual(app.playheadIdx, 0);
    waveform.emit('pause');
    assert.strictEqual(app.isPlaying, false, '"pause"');
    waveform.emit('play'); waveform.emit('finish');
    assert.strictEqual(app.isPlaying, false, '"finish"');

    app._auditionStop = 50; app.isPlaying = true;
    waveform.emit('pause');
    assert.strictEqual(app._auditionStop, null, 'a pause abandons an audition');

    app._auditionStop = 50;
    app.waveform.getDuration = () => 600; app.waveform.setTime = () => {};
    waveform.emit('interaction', 20);
    assert.strictEqual(app._auditionStop, null, 'a click on the waveform abandons an audition');
    assert.strictEqual(app.playTime, 20, '... and seeks');

    // The media element: streams on demand (never buffers the whole recording), reports buffering
    assert.strictEqual(media.preload, 'metadata');
    media.emit('waiting'); assert.strictEqual(app.playerBuffering, true);
    media.emit('playing'); assert.strictEqual(app.playerBuffering, false);
    media.emit('seeking'); assert.strictEqual(app.playerBuffering, true);
    media.emit('seeked'); assert.strictEqual(app.playerBuffering, false);
    app._pendingSeek = 33;
    media.emit('loadedmetadata'); assert.strictEqual(app._pendingSeek, null, 'loadedmetadata replays a pending seek');

    // Destroying the waveform detaches the media listeners
    waveform.emit('destroy');
    assert.ok(Object.values(media.handlers).every(list => list.length === 0), 'media listeners removed on destroy');

    finished = true;
    console.log('track_player_wiring: ok');
})().catch(error => { console.error(error); process.exit(1); });
