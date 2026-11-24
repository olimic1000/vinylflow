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
function harness({ canPlay = 'maybe', peaks = [0.1, 0.2, 0.05], loadResults = [], fetchOverride = null, audioProbe = async () => ({ ok: true, status: 206 }) } = {}) {
    const app = vinylApp();
    app.currentFileId = 'one';
    const wave = {
        handlers: {}, loads: [], probes: [], plays: 0, zooms: [], times: [],
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
    global.document = { getElementById: () => container, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
    global.WaveSurfer = { create: options => { wave.options = options; return wave; }, Regions: { create: () => ({}) } };
    // audioProbe(url, options) answers the status probe the failure notice makes on the preview URL
    global.fetch = fetchOverride || (async (url, options) => {
        if (url.startsWith('/api/audio/')) {
            wave.probes.push({ url, options });
            return audioProbe(url, options);
        }
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
    // a needle-drop thump (one 1.0 bin) must not shrink the music: scale to the 99.9th percentile
    // (a mostly-silent recording keeps a floor: reference >= loudest / 8, below)
    const thumpSide = new Array(10000).fill(0.2); thumpSide[5000] = 1.0; thumpSide[6000] = -1.0;
    const thumpScaled = scalePeaksToRecording(thumpSide);
    assert.ok(Math.abs(thumpScaled[0] - 1) < 1e-9, 'the music scales to full height, got ' + thumpScaled[0]);
    assert.strictEqual(thumpScaled[5000], 1, 'the spike clips at 1');
    assert.strictEqual(thumpScaled[6000], -1, 'negative spikes clip at -1');
    assert.deepStrictEqual(scalePeaksToRecording([-0.1, 0.1, -0.4]), [-0.25, 0.25, -1], 'sign kept');
    // 50 loud bins in 10 000 (0.5 %) are real signal, not a thump: a 99th-percentile reference would
    // stretch the music to full height and clip them all
    const loudPassages = new Array(10000).fill(0.2); for (let index = 0; index < 50; index++) loudPassages[index * 200] = 1.0;
    assert.ok(Math.abs(scalePeaksToRecording(loudPassages)[1] - 0.2) < 1e-9, '0.5 % loud bins set the scale');
    // five spikes (0.05 %) are a thump: ignored
    const fewSpikes = new Array(10000).fill(0.2); for (let index = 0; index < 5; index++) fewSpikes[index * 2000] = 1.0;
    assert.ok(Math.abs(scalePeaksToRecording(fewSpikes)[1] - 1) < 1e-9);
    // mostly digital silence: the reference never drops under loudest / 8 (it used to draw a solid block)
    const mostlySilent = new Array(10000).fill(0); for (let index = 0; index < 5; index++) mostlySilent[index * 2000 + 1] = 0.8;
    mostlySilent[7] = 0.05;
    const silentScaled = scalePeaksToRecording(mostlySilent);
    assert.strictEqual(silentScaled[0], 0, 'silence stays flat');
    assert.strictEqual(silentScaled[1], 1, 'the loud bins clip at 1');
    assert.ok(Math.abs(silentScaled[7] - 0.5) < 1e-9, 'scaled by loudest / 8 = 0.1, got ' + silentScaled[7]);

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
    decodeError(wave);   // the preview itself fails to decode too
    assert.match(app.playerNotice, /Audio playback error: .*could not be decoded/, 'a useful message at once, not "unknown"');
    await sleep(20);
    assert.strictEqual(wave.loads.length, 3, 'the preview is fine on the server: reloaded once');
    decodeError(wave); await sleep(20);
    assert.strictEqual(wave.loads.length, 3, 'never loops');
    assert.match(app.playerNotice, /Audio playback error: .*could not be decoded/);
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
    decodeError(wave); await sleep(20);
    assert.strictEqual(wave.loads.length, 2, 'the probe says the preview is ok: one reload');
    decodeError(wave); await sleep(20);
    assert.strictEqual(wave.loads.length, 2, 'only once');
    assert.match(app.playerNotice, /Audio playback error: .*could not be decoded/);
    assert.strictEqual(app.waveformLoading, false, 'spinner cleared on failure');

    // ---- a failing fallback reload clears the overlay and says why ----
    ({ app, wave } = await started({ loadResults: ['ok', 'fail', 'fail'] }));
    decodeError(wave); await sleep(30);
    assert.strictEqual(wave.loads.length, 3, 'the probe is ok, so the preview is retried once');
    assert.strictEqual(app.waveformLoading, false);
    assert.match(app.playerNotice, /Audio playback error: load failed/);

    // ---- the preview failing too says what the server says, not a blind "format" claim ----
    for (const [status, detail] of [[410, 'The source file is gone.'], [409, 'The file changed.'], [503, 'The audio preview is still being prepared. Try again in a moment.']]) {
        ({ app, wave } = await started({ loadResults: ['ok', 'fail'],
            audioProbe: async () => ({ ok: false, status, json: async () => ({ detail }) }) }));
        decodeError(wave); await sleep(20);
        assert.strictEqual(wave.probes.length >= 1, true, 'the preview URL is probed');
        assert.strictEqual(wave.probes[0].url, '/api/audio/one?wait=0', 'the probe never waits for or starts the encode');
        assert.deepStrictEqual(wave.probes[0].options, { headers: { Range: 'bytes=0-1' } });
        assert.strictEqual(app.playerNotice, 'Audio playback error: ' + detail, 'HTTP ' + status);
        assert.strictEqual(app.waveformLoading, false);
    }
    // the fallback message shows at once, and the probe's detail replaces it
    let releaseSlow;
    ({ app, wave } = await started({ loadResults: ['ok', 'fail'], audioProbe: () => new Promise(resolve => { releaseSlow = resolve; }) }));
    decodeError(wave); await sleep(20);
    assert.match(app.playerNotice, /Audio playback error: load failed/, 'no waiting for the probe');
    releaseSlow({ ok: false, status: 410, json: async () => ({ detail: 'gone' }) }); await sleep(20);
    assert.strictEqual(app.playerNotice, 'Audio playback error: gone');
    // a probe that rejects keeps the fallback message
    ({ app, wave } = await started({ loadResults: ['ok', 'fail'], audioProbe: async () => { throw new TypeError('network down'); } }));
    decodeError(wave); await sleep(20);
    assert.match(app.playerNotice, /Audio playback error: load failed/);
    assert.strictEqual(wave.loads.length, 2, 'no reload when the probe could not answer');
    // "still being prepared" (503 + Retry-After): polled until ready, then ONE reload, notice cleared
    const preparing = { ok: false, status: 503, headers: { get: name => name === 'Retry-After' ? '3' : null }, json: async () => ({ detail: 'The audio preview is still being prepared. Try again in a moment.' }) };
    let answers = [preparing, preparing, { ok: true, status: 206 }];
    ({ app, wave } = await started({ loadResults: ['ok', 'fail', 'ok'], audioProbe: async () => answers.shift() }));
    app.previewPollIntervalMs = 5;
    decodeError(wave); await sleep(10);
    assert.match(app.playerNotice, /still being prepared/, 'says why while it waits');
    await sleep(80);
    assert.strictEqual(wave.probes.length, 3, 'polled until ready');
    assert.strictEqual(wave.loads.length, 3, 'exactly one reload once ready');
    assert.strictEqual(wave.loads[2].url, '/api/audio/one');
    assert.strictEqual(app.playerNotice, '', 'the notice clears when the preview plays');
    await sleep(40);
    assert.strictEqual(wave.probes.length, 3, 'polling stopped');
    // polling stops when the file is switched, and when the waveform is rebuilt
    for (const stop of [a => { a.currentFileId = 'two'; }, a => { a.waveform = null; }]) {
        ({ app, wave } = await started({ loadResults: ['ok', 'fail', 'ok'], audioProbe: async () => preparing }));
        app.previewPollIntervalMs = 5;
        decodeError(wave); await sleep(20);
        const seen = wave.probes.length;
        assert.ok(seen >= 2, 'it was polling');
        stop(app); await sleep(40);
        assert.ok(wave.probes.length <= seen + 1, 'polling stopped after the switch');
        assert.strictEqual(wave.loads.length, 2, 'and never reloads');
    }
    // a 503 without Retry-After (preview could not be made) is final: never polled
    ({ app, wave } = await started({ loadResults: ['ok', 'fail'], audioProbe: async () => ({ ok: false, status: 503, headers: { get: () => null }, json: async () => ({ detail: 'could not be made' }) }) }));
    app.previewPollIntervalMs = 5;
    decodeError(wave); await sleep(60);
    assert.strictEqual(wave.probes.length, 1, 'no polling without Retry-After');
    assert.strictEqual(app.playerNotice, 'Audio playback error: could not be made');
    // the same error reported twice starts one probe loop, not two
    ({ app, wave } = await started({ canPlay: '', audioProbe: () => new Promise(() => {}) }));
    wave.media.error = { code: 3 };
    wave.handlers.error(undefined); wave.handlers.error(undefined); await sleep(20);
    assert.strictEqual(wave.probes.length, 1, 'one probe for the error and its twin');
    // an OK reply that arrives after the file was switched, or the waveform rebuilt, reloads nothing
    let releaseOk;
    const lateOk = () => new Promise(resolve => { releaseOk = () => resolve({ ok: true, status: 206 }); });
    for (const stop of [a => { a.currentFileId = 'two'; }, a => { a.waveform = null; }]) {
        ({ app, wave } = await started({ loadResults: ['ok', 'fail'], audioProbe: lateOk }));
        decodeError(wave); await sleep(20);
        stop(app); releaseOk(); await sleep(20);
        assert.strictEqual(wave.loads.length, 2, 'a late OK reply for a stale waveform reloads nothing');
    }
    // while a reload is still in flight (source not settled) an OK reply does not start another
    ({ app, wave } = await started({ loadResults: ['ok', 'fail'], audioProbe: lateOk }));
    decodeError(wave); await sleep(20);
    app._audioSource.settled = false; releaseOk(); await sleep(20);
    assert.strictEqual(wave.loads.length, 2, 'no second reload on top of one in flight');
    // polling is bounded
    ({ app, wave } = await started({ loadResults: ['ok', 'fail'], audioProbe: async () => preparing }));
    app.previewPollIntervalMs = 5; app.previewPollMaxMs = 30;
    decodeError(wave); await sleep(120);
    const boundedProbes = wave.probes.length;
    await sleep(50);
    assert.strictEqual(wave.probes.length, boundedProbes, 'gives up after the total bound');
    assert.ok(boundedProbes < 15);
    // a non-JSON error body still says something useful
    ({ app, wave } = await started({ loadResults: ['ok', 'fail'], audioProbe: async () => ({ ok: false, status: 502, json: async () => { throw new Error('html'); } }) }));
    decodeError(wave); await sleep(20);
    assert.match(app.playerNotice, /HTTP 502/);
    // a reply that arrives after the file was switched or the waveform rebuilt shows nothing
    let releaseProbe;
    const slowProbe = () => new Promise(resolve => { releaseProbe = () => resolve({ ok: false, status: 503, json: async () => ({ detail: 'late' }) }); });
    ({ app, wave } = await started({ loadResults: ['ok', 'fail'], audioProbe: slowProbe }));
    decodeError(wave); await sleep(20);
    app.currentFileId = 'two'; app.playerNotice = '';
    releaseProbe(); await sleep(20);
    assert.strictEqual(app.playerNotice, '', 'file switched: stale reply dropped');
    ({ app, wave } = await started({ loadResults: ['ok', 'fail'], audioProbe: slowProbe }));
    decodeError(wave); await sleep(20);
    app.waveform = null; app.playerNotice = '';
    releaseProbe(); await sleep(20);
    assert.strictEqual(app.playerNotice, '', 'waveform rebuilt: stale reply dropped');
    // an error straight on the preview URL (the browser cannot play the WAV at all) is probed too
    ({ app, wave } = await started({ canPlay: '', audioProbe: async () => ({ ok: false, status: 410, json: async () => ({ detail: 'gone for good' }) }) }));
    decodeError(wave); await sleep(20);
    assert.strictEqual(app.playerNotice, 'Audio playback error: gone for good');

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
