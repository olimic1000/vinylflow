// The single Play/Pause player: the transport state follows the player's events, the playhead
// marks the current track, rows play and audition cuts, a click on a region seeks.
// WaveSurfer is faked: its events are fired by hand, and a play() that rejects stands for a
// browser that blocks playback.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();

function fakeWaveform() {
    const handlers = {};
    let playing = false, currentTime = 0, rejectPlay = false;
    const waveform = {
        on(name, fn) { (handlers[name] = handlers[name] || []).push(fn); },
        emit(name, ...args) { (handlers[name] || []).forEach(fn => fn(...args)); },
        isPlaying: () => playing,
        getCurrentTime: () => currentTime,
        getDuration: () => 600,
        setTime(time) { currentTime = time; },
        play() {
            if (rejectPlay) return Promise.reject(new Error('NotAllowed'));
            playing = true; waveform.emit('play'); return Promise.resolve();
        },
        pause() { if (playing) { playing = false; waveform.emit('pause'); } },
        tick(time) { currentTime = time; waveform.emit('timeupdate', time); },
        rejectNextPlays(value) { rejectPlay = value; },
        getWrapper: () => ({ getBoundingClientRect: () => ({ left: 100, width: 1000 }) }),
        destroy() {},
    };
    return waveform;
}
function fakeRegions() {
    const list = [];
    return {
        list, getRegions: () => list, clearRegions() { list.length = 0; },
        addRegion(options) {
            const region = { ...options, element: { style: {} }, setOptions(changes) { Object.assign(region, changes); } };
            list.push(region); return region;
        },
    };
}
const track = (number, start, end, ignored = false) => ({ number, start, end, duration: end - start, editing: false, ignored });
function mkApp() {
    const app = vinylApp();
    app.waveform = fakeWaveform();
    app.waveformRegions = fakeRegions();
    // The same listeners initWaveform registers
    const waveform = app.waveform;
    waveform.on('play', () => { app.isPlaying = true; });
    waveform.on('pause', () => { app.isPlaying = false; app.cancelAudition(); });
    waveform.on('finish', () => { app.isPlaying = false; app.cancelAudition(); });
    waveform.on('timeupdate', time => app.onPlayerTime(time));
    app.detectedTracks = [track(1, 10, 100), track(2, 110, 200), track(3, 200, 300, true)];
    app.addTrackRegions();
    return app;
}
const nextTask = () => new Promise(resolve => setImmediate(resolve));
let finished = false;
process.on('exit', () => { if (!finished) { console.error('track_player: async part did not finish'); process.exitCode = 1; } });

(async () => {
    // ---- the toggle follows the player's events, not its own guess ----
    let app = mkApp(), waveform = app.waveform;
    assert.strictEqual(app.isPlaying, false);
    app.togglePlay(); assert.strictEqual(app.isPlaying, true);
    app.togglePlay(); assert.strictEqual(app.isPlaying, false);
    app.togglePlay(); waveform.pause(); assert.strictEqual(app.isPlaying, false, 'paused from outside');
    app.togglePlay(); waveform.emit('finish'); assert.strictEqual(app.isPlaying, false, 'end of the recording');

    // ---- a refused play shows a notice and leaves the toggle on Play ----
    app = mkApp(); waveform = app.waveform; waveform.rejectNextPlays(true);
    app.togglePlay(); await nextTask();
    assert.ok(/Could not start playback/.test(app.playerNotice));
    assert.strictEqual(app.isPlaying, false);
    waveform.rejectNextPlays(false); app.togglePlay(); await nextTask();
    assert.strictEqual(app.playerNotice, '', 'a later success clears the notice');

    // ---- an interrupted play is not a failure ----
    // media.play() rejects with AbortError when a pause or a teardown comes before playback starts;
    // that is the user's own action, not a blocked browser.
    const abortError = () => Object.assign(new Error('The play() request was interrupted by a call to pause()'), { name: 'AbortError' });
    app = mkApp(); waveform = app.waveform;
    let rejectPending;
    waveform.play = () => new Promise((resolve, reject) => { rejectPending = reject; });
    app.togglePlay();                       // play starts buffering ...
    rejectPending(abortError()); await nextTask();   // ... and Pause interrupts it
    assert.strictEqual(app.playerNotice, '', 'Play then Pause before buffering: no alert');

    // Play, then the file changes (destroyWaveform): the late rejection must not bring the notice back
    app = mkApp(); waveform = app.waveform;
    waveform.play = () => new Promise((resolve, reject) => { rejectPending = reject; });
    app.startPlayback();
    app.destroyWaveform();
    rejectPending(new Error('NotAllowed')); await nextTask();
    assert.strictEqual(app.playerNotice, '', 'a reply for a destroyed waveform is dropped');
    app = mkApp(); waveform = app.waveform;                      // ... also when a new waveform replaced it
    waveform.play = () => new Promise((resolve, reject) => { rejectPending = reject; });
    app.startPlayback();
    app.waveform = fakeWaveform();
    rejectPending(new Error('NotAllowed')); await nextTask();
    assert.strictEqual(app.playerNotice, '');

    // ---- un-ignoring the track under the playhead brings its emphasis back ----
    app = mkApp(); waveform = app.waveform; app.togglePlay();
    waveform.tick(50);
    const emphasised = number => app.waveformRegions.list.find(r => r.id === 'track-' + number).color;
    assert.strictEqual(emphasised(1), 'rgba(59, 130, 246, 0.55)');
    app.toggleTrackIgnored(app.detectedTracks[0]);
    assert.strictEqual(emphasised(1), 'rgba(107, 114, 128, 0.3)', 'ignored: grey');
    app.toggleTrackIgnored(app.detectedTracks[0]);
    assert.strictEqual(emphasised(1), 'rgba(59, 130, 246, 0.55)', 'un-ignored under the playhead: emphasised again');
    app.toggleTrackIgnored(app.detectedTracks[1]); app.toggleTrackIgnored(app.detectedTracks[1]);
    assert.strictEqual(emphasised(2), 'rgba(16, 185, 129, 0.3)', 'another track keeps the plain tint');

    // ---- clock ----
    assert.strictEqual(app.formatClock(75), '1:15');
    assert.strictEqual(app.formatClock(605), '10:05');
    assert.strictEqual(app.formatClock(-3), '0:00');
    assert.strictEqual(app.formatClock(NaN), '0:00');

    // ---- the playhead marks the current track; the regions are only touched when it changes ----
    app = mkApp(); waveform = app.waveform; app.togglePlay();
    let regionReads = 0; const getRegions = app.waveformRegions.getRegions;
    app.waveformRegions.getRegions = () => { regionReads++; return getRegions(); };
    waveform.tick(5); assert.strictEqual(app.playheadIdx, -1, 'before the first track');
    waveform.tick(10); assert.strictEqual(app.playheadIdx, 0);
    const readsAfterEntering = regionReads;
    waveform.tick(11); waveform.tick(50); waveform.tick(99.9);
    assert.strictEqual(regionReads, readsAfterEntering, 'no region work while the track stays the same');
    waveform.tick(100); assert.strictEqual(app.playheadIdx, -1, 'gap between tracks');
    waveform.tick(110); assert.strictEqual(app.playheadIdx, 1);
    const region = number => app.waveformRegions.list.find(r => r.id === 'track-' + number);
    assert.strictEqual(region(2).color, 'rgba(16, 185, 129, 0.55)', 'current track is emphasised');
    assert.strictEqual(region(1).color, 'rgba(59, 130, 246, 0.3)');
    waveform.tick(250); assert.strictEqual(app.playheadIdx, 2);
    assert.strictEqual(region(3).color, 'rgba(107, 114, 128, 0.3)', 'an ignored track stays grey');
    assert.strictEqual(region(2).color, 'rgba(16, 185, 129, 0.3)', 'emphasis removed when the playhead leaves');

    // rebuilt regions (split, delete, Undo) pick the emphasis up again
    app.addTrackRegions();
    assert.strictEqual(app.playheadIdx, 2);

    // ---- row play: starts at the track, pauses on the same row, continues into the next ----
    app = mkApp(); waveform = app.waveform;
    app.toggleTrackPlay(1);
    assert.strictEqual(waveform.getCurrentTime(), 110); assert.ok(app.isPlaying); assert.strictEqual(app.playheadIdx, 1);
    waveform.tick(120); app.toggleTrackPlay(1); assert.strictEqual(app.isPlaying, false, 'second click on the playing row pauses');
    app.toggleTrackPlay(0); assert.strictEqual(waveform.getCurrentTime(), 10);
    waveform.tick(150); assert.strictEqual(app.playheadIdx, 1, 'playing on into the following track');
    app.toggleTrackPlay(7);   // no such row: nothing happens
    assert.strictEqual(waveform.getCurrentTime(), 150);

    // ---- cut audition: 3 s before to 3 s after the cut, clamped to the recording ----
    app = mkApp(); waveform = app.waveform;
    app.auditionCut(1, 'start'); assert.strictEqual(waveform.getCurrentTime(), 107); assert.ok(app.isPlaying);
    waveform.tick(112); assert.ok(app.isPlaying);
    waveform.tick(113); assert.strictEqual(app.isPlaying, false); assert.strictEqual(app._auditionStop, null);
    app.auditionCut(1, 'end'); assert.strictEqual(waveform.getCurrentTime(), 197);
    waveform.tick(203); assert.strictEqual(app.isPlaying, false);
    app.auditionCut(0, 'start'); assert.strictEqual(waveform.getCurrentTime(), 7);
    app = mkApp(); waveform = app.waveform; app.detectedTracks[0].start = 1;
    app.auditionCut(0, 'start'); assert.strictEqual(waveform.getCurrentTime(), 0); assert.strictEqual(app._auditionStop, 4);
    app.detectedTracks[2].end = 599; app.auditionCut(2, 'end'); assert.strictEqual(app._auditionStop, 600);

    // an audition is abandoned by a seek, the toggle, a pause, or the next audition replaces it
    app = mkApp(); waveform = app.waveform;
    app.auditionCut(1, 'start'); app.onRegionClicked({ id: 'track-1' }, { button: 0, clientX: 600, target: {} });
    assert.strictEqual(app._auditionStop, null); waveform.tick(120); assert.ok(app.isPlaying, 'it keeps playing past the old stop');
    app.auditionCut(1, 'start'); app.togglePlay(); assert.strictEqual(app._auditionStop, null);
    app = mkApp(); waveform = app.waveform; app.auditionCut(1, 'start'); waveform.pause(); assert.strictEqual(app._auditionStop, null);
    app = mkApp(); app.auditionCut(1, 'start'); app.auditionCut(0, 'end'); assert.strictEqual(app._auditionStop, 103);

    // ---- a click on a region seeks (a region covers the waveform and swallows the click) ----
    app = mkApp(); waveform = app.waveform;
    app.onRegionClicked({ id: 'track-1' }, { button: 0, clientX: 600, target: {} });   // (600-100)/1000*600
    assert.strictEqual(waveform.getCurrentTime(), 300);
    waveform.setTime(5);
    app.onRegionClicked({ id: 'track-1' }, { button: 2, clientX: 600, target: {} }); assert.strictEqual(waveform.getCurrentTime(), 5, 'right click');
    app.onRegionClicked({ id: 'track-1' }, { button: 0, ctrlKey: true, clientX: 600, target: {} }); assert.strictEqual(waveform.getCurrentTime(), 5, 'ctrl click');
    app._regionDownX = 300;
    app.onRegionClicked({ id: 'track-1' }, { button: 0, clientX: 600, target: {} }); assert.strictEqual(waveform.getCurrentTime(), 5, 'end of a drag');
    app.onRegionClicked({ id: 'expected-1' }, { button: 0, clientX: 600, target: {} }); assert.strictEqual(waveform.getCurrentTime(), 5, 'ghost marker');
    app.onRegionClicked({ id: 'track-1' }, { button: 0, clientX: 50, target: {} }); assert.strictEqual(waveform.getCurrentTime(), 0, 'clamped to the start');

    // ---- seeking before the media element knows its length is replayed on loadedmetadata ----
    app = mkApp(); waveform = app.waveform;
    waveform.getMediaElement = () => ({ readyState: 0 });
    app.seekTo(42); assert.strictEqual(app._pendingSeek, 42);
    waveform.setTime(0); app.applyPendingSeek();
    assert.strictEqual(waveform.getCurrentTime(), 42); assert.strictEqual(app._pendingSeek, null);

    // ---- destroying the waveform resets the transport ----
    app = mkApp(); app.togglePlay(); app.waveform.tick(120);
    app.destroyWaveform();
    assert.strictEqual(app.isPlaying, false); assert.strictEqual(app.playheadIdx, -1);
    assert.strictEqual(app.playTime, 0); assert.strictEqual(app.waveform, null);

    finished = true;
    console.log('track_player: ok');
})().catch(error => { console.error(error); process.exit(1); });
