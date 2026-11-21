// Precise playback: the waveform plays the original WAV when the browser can, falls back once to
// the MP3 preview when the WAV fails to play (keeping peaks, zoom, playhead and paused/playing
// state), scales its peaks to the whole recording, and shows a loading overlay meanwhile.
// WaveSurfer, the media element and fetch are faked; their events are fired by hand.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'index.html'), 'utf8');
const { vinylApp, scalePeaksToRecording, PREVIEW_AUDIO_NOTICE } =
    new Function(src + ';return {vinylApp, scalePeaksToRecording, PREVIEW_AUDIO_NOTICE};')();

const nextTask = () => new Promise(resolve => setImmediate(resolve));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let finished = false;
process.on('exit', () => { if (!finished) { console.error('precise_playback: async part did not finish'); process.exitCode = 1; } });

/**
 * A fake WaveSurfer. `loadResults` is consumed one per load(): 'ok' resolves, 'fail' rejects.
 * The media element records its listeners so a test can fire its `error` event.
 */
function harness({ canPlay = 'maybe', peaks = [0.1, 0.2, 0.05], loadResults = [], fetchOverride = null } = {}) {
    const app = vinylApp();
    app.currentFileId = 'one';
    const wave = {
        handlers: {}, loads: [], plays: 0, zooms: [], times: [],
        media: { listeners: {}, error: null, readyState: 0, preload: '', canPlayType: () => canPlay,
            addEventListener(name, fn) { this.listeners[name] = fn; }, removeEventListener() {} },
        on(name, fn) { this.handlers[name] = fn; },
        getMediaElement() { return this.media; },
        registerPlugin() { return { on() {}, getRegions: () => [], clearRegions() {} }; },
        getWrapper: () => ({ getBoundingClientRect: () => ({ left: 0, width: 1000 }) }),
        zoom(value) { this.zooms.push(value); },
        setTime(time) { this.times.push(time); }, destroy() {}, getDuration: () => 120,
        play() { this.plays++; return Promise.resolve(); }, pause() {},
        load(url, loadedPeaks, duration) {
            this.loads.push({ url, peaks: loadedPeaks, duration });
            // Like a real reload, a new source clears the element's error
            this.media.error = null;
            const result = loadResults.shift() || 'ok';
            if (typeof result !== 'string') return result;   // a promise the test settles by hand
            return result === 'ok' ? Promise.resolve() : Promise.reject(new Error('load failed'));
        },
    };
    const container = { offsetWidth: 1000, added: [], addEventListener(name) { this.added.push(name); } };
    wave.container = container;
    global.document = { getElementById: () => container };
    global.WaveSurfer = { create: options => { wave.options = options; return wave; }, Regions: { create: () => ({}) } };
    global.fetch = fetchOverride || (async url => {
        assert.strictEqual(url, '/api/waveform-peaks/one');
        return { ok: true, json: async () => ({ peaks, duration: 120 }) };
    });
    return { app, wave };
}
async function started(options) {
    const result = harness(options);
    await result.app.initWaveform();
    return result;
}
// The vendored WaveSurfer registers its own media 'error' listener before ours and re-emits it as
// emit('error', event.error), i.e. with undefined. So a media error reaches the WaveSurfer
// handler first (no argument), then our media listener, as here.
const mediaError = (wave, code) => {
    wave.media.error = { code };
    if (wave.handlers.error) wave.handlers.error(undefined);
    wave.media.listeners.error();
};
const decodeError = wave => mediaError(wave, 3);

(async () => {
    // ---- scalePeaksToRecording: one scale for the whole recording ----
    assert.deepStrictEqual(scalePeaksToRecording([0.1, 0.2, 0.05]), [0.5, 1, 0.25]);
    assert.deepStrictEqual(scalePeaksToRecording([0, 0]), [0, 0], 'digital silence stays flat, never NaN');
    assert.deepStrictEqual(scalePeaksToRecording([]), []);
    // an album's worth of peaks must not blow the argument stack (Math.max(...peaks) would)
    assert.strictEqual(scalePeaksToRecording(new Array(900000).fill(0.25))[0], 1);

    // ---- URL choice, per-recording scale handed to WaveSurfer unnormalised ----
    let { app, wave } = await started();
    assert.strictEqual(wave.loads[0].url, '/api/audio/one?precise=true', 'WAV when the browser can play it');
    assert.ok(!wave.options.normalize, 'WaveSurfer must not normalise each canvas itself');
    assert.deepStrictEqual(wave.loads[0].peaks, [[0.5, 1, 0.25]]);
    assert.strictEqual(wave.loads[0].duration, 120);
    assert.strictEqual(app.playerInfo, '');

    ({ app, wave } = await started({ canPlay: '' }));
    assert.strictEqual(wave.loads.length, 1);
    assert.strictEqual(wave.loads[0].url, '/api/audio/one', 'preview directly when canPlayType says no');
    assert.strictEqual(app.playerInfo, PREVIEW_AUDIO_NOTICE);
    assert.match(app.playerInfo, /cut points may be up to ~50 ms less precise/);

    // ---- a decode error reloads the preview once, keeping peaks, duration, zoom, position ----
    ({ app, wave } = await started());
    app.playTime = 42; app.currentZoom = 80; app.isPlaying = false;
    decodeError(wave);   // WaveSurfer's re-emit (undefined) and our media listener both fire
    assert.strictEqual(app.playerNotice, '', 'the twin event is absorbed, not shown as an error');
    assert.strictEqual(app.isPlaying, false);
    assert.strictEqual(app.waveformLoading, true, 'the overlay stays up during the reload');
    await nextTask();
    assert.strictEqual(wave.loads.length, 2);
    assert.strictEqual(wave.loads[1].url, '/api/audio/one');
    assert.deepStrictEqual(wave.loads[1].peaks, wave.loads[0].peaks, 'same peaks, not refetched');
    assert.strictEqual(wave.loads[1].duration, 120);
    assert.strictEqual(app._pendingSeek, 42, 'playhead survives the reload');
    assert.strictEqual(app._restoreZoom, 80);
    assert.strictEqual(wave.plays, 0, 'it was paused, so it stays paused');
    assert.strictEqual(app.playerInfo, PREVIEW_AUDIO_NOTICE);
    // 'ready' after the reload restores the playhead and zoom and clears the overlay
    app.playTime = 42;
    wave.handlers.ready();
    assert.ok(wave.times.includes(42), 'ready seeks back to the playhead');
    assert.strictEqual(app.currentZoom, 80); assert.strictEqual(wave.zooms.at(-1), 80);
    assert.strictEqual(app._restoreZoom, null);
    await sleep(150);
    assert.strictEqual(app.waveformLoading, false, 'overlay cleared once the preview is ready');
    // a second failure never loops
    decodeError(wave);
    assert.strictEqual(wave.loads.length, 2, 'never loops');
    assert.match(app.playerNotice, /Audio playback error: .*could not be decoded/, 'a useful message, not "unknown"');
    assert.strictEqual(app.waveformLoading, false);

    // ---- it resumes only when it was playing ----
    ({ app, wave } = await started());
    app.isPlaying = true; decodeError(wave);
    assert.strictEqual(app.isPlaying, false, 'the toggle shows Play while the preview loads');
    await nextTask();
    assert.strictEqual(wave.plays, 1, 'was playing: resumes on the preview');

    // ---- only decode (3) and unsupported (4) errors fall back ----
    // The vendored WaveSurfer re-emits EVERY media error (network 2, aborted 1 included) as
    // emit('error', undefined), so a WaveSurfer error is no evidence of a format problem: only the
    // media element's own error code is. (This replaces an earlier assertion that any WaveSurfer
    // error falls back, which contradicted that requirement.)
    ({ app, wave } = await started());
    mediaError(wave, 2); await nextTask();
    assert.strictEqual(wave.loads.length, 1, 'network error: no reload');
    assert.match(app.playerNotice, /Audio playback error: .*network/);
    app.playerNotice = '';
    mediaError(wave, 1); await nextTask();
    assert.strictEqual(wave.loads.length, 1, 'aborted: no reload');
    assert.match(app.playerNotice, /Audio playback error: .*aborted/);
    assert.strictEqual(app.waveformLoading, false);
    // an error thrown inside a handler (load() catches it and emits it) is not a format problem either
    wave.media.error = null; wave.handlers.error(new Error('boom in ready'));
    assert.strictEqual(wave.loads.length, 1);
    assert.match(app.playerNotice, /Audio playback error: boom in ready/);
    mediaError(wave, 4); await nextTask();
    assert.strictEqual(wave.loads.length, 2, 'unsupported format falls back');
    // a non-format error while the fallback is in flight is the same failure's twin: absorbed
    ({ app, wave } = await started({ loadResults: ['ok', new Promise(() => {})] }));
    decodeError(wave); app.playerNotice = '';
    wave.handlers.error(undefined);
    assert.strictEqual(app.playerNotice, '');
    assert.strictEqual(wave.loads.length, 2);

    // ---- an error while already on the preview shows the notice and does not reload ----
    ({ app, wave } = await started({ canPlay: '' }));
    decodeError(wave);
    assert.strictEqual(wave.loads.length, 1);
    assert.match(app.playerNotice, /Audio playback error: .*could not be decoded/);
    assert.strictEqual(app.waveformLoading, false, 'spinner cleared on failure');

    // ---- a failing fallback reload clears the overlay and says why ----
    ({ app, wave } = await started({ loadResults: ['ok', 'fail'] }));
    decodeError(wave); await nextTask();
    assert.strictEqual(wave.loads.length, 2);
    assert.strictEqual(app.waveformLoading, false);
    assert.match(app.playerNotice, /Audio playback error: load failed/);

    // ---- stale replies are dropped: waveform replaced or file switched ----
    ({ app, wave } = await started());
    app.waveform = null;
    decodeError(wave);
    assert.strictEqual(wave.loads.length, 1, 'a replaced waveform never reloads');
    ({ app, wave } = await started());
    app.currentFileId = 'two'; decodeError(wave);
    assert.strictEqual(wave.loads.length, 1, 'a switched file never reloads');
    // reload in flight, then the waveform is replaced: a late success does not resume ...
    ({ app, wave } = await started({ loadResults: ['ok', 'ok'] }));
    app.isPlaying = true; decodeError(wave);
    let replacementPlays = 0;
    app.waveform = { play() { replacementPlays++; return Promise.resolve(); }, pause() {} };
    await nextTask();
    assert.strictEqual(wave.plays + replacementPlays, 0, 'no resume on a replaced waveform');
    // ... and a late failure shows no notice
    ({ app, wave } = await started({ loadResults: ['ok', 'fail'] }));
    decodeError(wave);
    app.waveform = null; app.playerNotice = '';
    await nextTask();
    assert.strictEqual(app.playerNotice, '');
    // destroyWaveform forgets the audio source and notices
    ({ app, wave } = await started({ canPlay: '' }));
    app.waveform = wave; app.destroyWaveform();
    assert.strictEqual(app.playerInfo, ''); assert.strictEqual(app._audioSource, null);

    // ---- a stale play() rejection must not cancel the fallback's resume ----
    // Play was clicked before the WAV failure was known: the original play() promise rejects
    // (NotSupportedError) after the fallback has already restarted playback on the preview.
    ({ app, wave } = await started());
    let rejectOriginalPlay; wave.pauses = 0;
    wave.play = function () {
        this.plays++;
        return this.plays === 1 ? new Promise((resolve, reject) => { rejectOriginalPlay = reject; }) : Promise.resolve();
    };
    wave.pause = function () { this.pauses++; };
    app.startPlayback(); wave.handlers.play();      // the toggle shows Pause
    decodeError(wave); await nextTask();
    assert.strictEqual(wave.plays, 2, 'the fallback resumed on the preview');
    rejectOriginalPlay(Object.assign(new Error('The element has no supported sources'), { name: 'NotSupportedError' }));
    await nextTask();
    assert.strictEqual(app.playerNotice, '', 'the stale rejection says nothing about the preview');
    assert.strictEqual(wave.pauses, 0, 'and does not pause it');

    // ---- the audio source is kept raw, and the fallback reuses the very same peaks array ----
    ({ app, wave } = await started());
    assert.strictEqual(app._audioSource.__v_skip, true, 'Alpine must not proxy the ~240k-element peaks');
    decodeError(wave); await nextTask();
    assert.strictEqual(wave.loads[1].peaks, wave.loads[0].peaks, 'the same array object, not a copy or proxy');

    // ---- destroying the waveform while its peaks load leaves no stuck overlay ----
    let releaseFetch;
    ({ app, wave } = harness({ fetchOverride: () => new Promise(resolve => { releaseFetch = resolve; }) }));
    let init = app.initWaveform(); await nextTask();
    assert.strictEqual(app.waveformLoading, true);
    app.destroyWaveform();
    assert.strictEqual(app.waveformLoading, false, 'destroy clears the overlay');
    releaseFetch({ ok: true, json: async () => ({ peaks: [0.1], duration: 120 }) });
    await init;
    assert.strictEqual(app.waveformLoading, false); assert.strictEqual(wave.loads.length, 0);

    // ---- stale returns in initWaveform ----
    // the waveform is replaced while the peaks request is in flight: not even an error is reported
    let notified = [];
    ({ app, wave } = harness({ fetchOverride: () => new Promise(resolve => { releaseFetch = resolve; }) }));
    app.notifyError = message => notified.push(message);
    init = app.initWaveform(); await nextTask();
    app.waveform = null;
    releaseFetch({ ok: false, json: async () => ({ detail: 'gone' }) });
    await init;
    assert.deepStrictEqual(notified, [], 'a replaced waveform reports nothing');
    assert.strictEqual(wave.loads.length, 0);
    // the file is switched while the peaks body is parsed: nothing is loaded or bound
    let releaseJson;
    ({ app, wave } = harness({ fetchOverride: async () => ({ ok: true, json: () => new Promise(resolve => { releaseJson = resolve; }) }) }));
    init = app.initWaveform(); await nextTask(); await nextTask();
    app.currentFileId = 'two';
    releaseJson({ peaks: [0.1], duration: 120 });
    await init;
    assert.strictEqual(wave.loads.length, 0, 'a switched file loads nothing');
    assert.strictEqual(wave.handlers.ready, undefined, 'and binds no ready handler');

    // ---- a failure of the first load after the fallback took over is not a second error ----
    let rejectFirstLoad; notified = [];
    ({ app, wave } = harness({ loadResults: [new Promise((resolve, reject) => { rejectFirstLoad = reject; }), 'ok'] }));
    app.notifyError = message => notified.push(message);
    init = app.initWaveform();
    while (!wave.loads.length) await nextTask();
    decodeError(wave);
    rejectFirstLoad(new Error('superseded by the reload'));
    await init;
    assert.deepStrictEqual(notified, [], 'no "Failed to load the waveform" toast when the fallback took over');
    // control: the same failure without a fallback is reported
    ({ app, wave } = harness({ loadResults: ['fail'] }));
    app.notifyError = message => notified.push(message);
    await app.initWaveform();
    assert.strictEqual(notified.length, 1);

    // ---- 'ready' binds its DOM listeners once, also after a fallback reload ----
    ({ app, wave } = await started());
    wave.handlers.ready(); wave.handlers.ready();
    assert.deepStrictEqual(wave.container.added, ['contextmenu', 'mousedown'], 'no duplicate listeners on the second ready');
    await sleep(150);

    // ---- the 100 ms region timer drops a replaced waveform ----
    ({ app, wave } = await started());
    let regionBuilds = 0; app.addTrackRegions = () => { regionBuilds++; };
    wave.handlers.ready();
    await sleep(150);
    assert.strictEqual(regionBuilds, 1, 'control: regions are drawn for the live waveform');
    wave.handlers.ready(); app.waveform = null;
    await sleep(150);
    assert.strictEqual(regionBuilds, 1, 'a replaced waveform draws nothing');

    // ---- a peaks failure clears the overlay ----
    ({ app, wave } = harness());
    global.fetch = async () => ({ ok: false, json: async () => ({ detail: 'gone' }) });
    app.notifyError = () => {};
    await app.initWaveform();
    assert.strictEqual(app.waveformLoading, false);

    // ---- markup: an overlay on the waveform area, not a line that shifts the layout ----
    const overlay = html.match(/<div x-show="waveformLoading"[\s\S]*?<\/div>\s*<span>[\s\S]*?<\/span>\s*<\/div>/)[0];
    assert.match(overlay, /role="status"/); assert.match(overlay, /absolute inset-0/);
    assert.match(overlay, /class="spinner spinner-lg"/); assert.match(overlay, /Loading waveform/);
    assert.ok(!/py-8 text-lg">Loading waveform\.\.\./.test(html), 'the in-flow loading line is gone');
    assert.match(html, /x-show="playerInfo"/);

    finished = true;
    console.log('precise_playback: ok');
})().catch(error => { console.error(error); process.exit(1); });
