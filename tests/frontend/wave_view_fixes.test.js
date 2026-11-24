// Review fixes for the waveform scrollbar and wheel zoom, each reproduced in a real browser first:
//  1. the drawn thumb (24 px minimum) and the drag maths use one geometry, also for tiny spans/ends
//  2. scrollbar and wheel zoom stop playback from pulling the view back; seeking/playing resumes it
//  3. one fit-to-width floor for the zoom-out button, the initial zoom and the wheel
//  4. a slow pinch on a long recording still zooms (relative, not absolute, no-op test)
//  5. the bar shows by pixels: WaveSurfer's Math.ceil makes "fit" 1 px too wide
//  6. a scrollbar drag ends when pointer capture is lost
// Fakes: the scroll element is a plain object with scroll geometry; the waveform records zoom calls and
// has the `options` object the renderer shares (autoScroll lives there); the bar records its listeners.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../../backend/static/index.html'), 'utf8');
const { vinylApp, waveThumbGeometry, waveStartForThumbLeft, WAVE_THUMB_MIN_PX } =
    new Function(source + ';return { vinylApp, waveThumbGeometry, waveStartForThumbLeft, WAVE_THUMB_MIN_PX };')();

const closeTo = (actual, expected, message, tolerance = 1e-9) =>
    assert.ok(Math.abs(actual - expected) < tolerance, `${message}: ${actual} != ${expected}`);

function setup({ clientWidth = 1000, duration = 100, zoomPxPerSec = 50, scrollLeft = 0 } = {}) {
    const container = { listeners: [], offsetWidth: clientWidth,
        addEventListener(type, fn) { this.listeners.push({ type, fn }); }, removeEventListener() {} };
    global.document = { getElementById: id => (id === 'waveform' ? container : null), addEventListener() {} };
    // WaveSurfer sizes the wrapper Math.ceil(duration * minPxPerSec), never below the view
    const widthFor = px => Math.max(clientWidth, Math.ceil(px * duration));
    const scroller = { clientWidth, scrollWidth: widthFor(zoomPxPerSec), scrollLeft, listeners: {},
        addEventListener(type, fn) { this.listeners[type] = fn; }, removeEventListener() {},
        getBoundingClientRect: () => ({ left: 0 }) };
    const handlers = {};
    const waveform = { zoomCalls: [], options: { autoScroll: true },
        on(event, fn) { (handlers[event] ||= []).push(fn); }, destroy() {},
        getWrapper: () => ({ parentElement: scroller }), getDuration: () => duration,
        getMediaElement: () => ({ readyState: 4 }), setTime() {}, play: () => undefined, pause() {},
        zoom(px) { this.zoomCalls.push(px); scroller.scrollWidth = widthFor(px); } };
    const app = vinylApp();
    app.waveform = waveform;
    app.bindWaveScrollbar(waveform);
    return { app, scroller, waveform, handlers, container, widthFor };
}
const wheel = (deltaY, extra = {}) => ({ ctrlKey: true, metaKey: false, deltaY, deltaMode: 0, clientX: 400, preventDefault() {}, ...extra });
function pointerDown(app, clientX, barWidth = 800) {
    const barListeners = {}, classes = new Set();
    const bar = { getBoundingClientRect: () => ({ left: 0, width: barWidth }), classList: { add: c => classes.add(c), remove: c => classes.delete(c) },
        setPointerCapture() {}, addEventListener: (type, fn) => { barListeners[type] = fn; },
        removeEventListener: type => { delete barListeners[type]; } };
    app.waveScrollbarDown({ currentTarget: bar, button: 0, clientX, pointerId: 1, preventDefault() {} });
    return { barListeners, classes };
}

// ---- 1. one thumb geometry ----
assert.strictEqual(WAVE_THUMB_MIN_PX, 24);
assert.ok(/max\(24px, calc\(var\(--thumb-span/.test(html), 'the CSS draws the same minimum width');
// A 20-minute side at 300 px/s in an 873 px view shows 0.0007 of it: the thumb is the 24 px minimum
let t = setup({ clientWidth: 873, duration: 1200, zoomPxPerSec: 300 });
t.app.waveView = { start: 0.5, end: 0.5 + 873 / (1200 * 300) };
let thumb = waveThumbGeometry(t.app.waveView, 800);
assert.strictEqual(thumb.width, 24, 'drawn at the minimum width');
// Pressing the drawn thumb, outside the (sub-pixel) true span, must not jump the view
t.scroller.scrollLeft = 123456;
pointerDown(t.app, thumb.left + 20);
assert.strictEqual(t.scroller.scrollLeft, 123456, 'a press on the drawn thumb is not a track press');
// ...and a press just beyond the drawn thumb is one
pointerDown(t.app, thumb.left + thumb.width + 5);
assert.notStrictEqual(t.scroller.scrollLeft, 123456, 'a press just outside the thumb jumps');
// At the far end the drawn thumb ends exactly at the bar's end (it used to stick out ~23 px)
const spanTiny = 873 / (1200 * 300);
const atEnd = { start: 1 - spanTiny, end: 1 };
thumb = waveThumbGeometry(atEnd, 800);
closeTo(thumb.left + thumb.width, 800, 'thumb ends at the bar end');
assert.strictEqual(waveThumbGeometry({ start: 0, end: spanTiny }, 800).left, 0, 'thumb starts at the bar start');
// Dragging to the right end scrolls to the end, to the left end to the start (1 % span too)
t.app.waveView = atEnd;
let drag = pointerDown(t.app, thumb.left + 10);
drag.barListeners.pointermove({ clientX: 5000 });
closeTo(t.scroller.scrollLeft / t.scroller.scrollWidth, 1 - spanTiny, 'right end', 1e-9);
drag.barListeners.pointermove({ clientX: -5000 });
assert.strictEqual(t.scroller.scrollLeft, 0, 'left end');
// Round trip: the start a thumb position maps back to is the start it was drawn for
for (const view of [{ start: 0.1, end: 0.11 }, { start: 0.45, end: 0.55 }, { start: 0.6, end: 0.9 }]) {
    closeTo(waveStartForThumbLeft(waveThumbGeometry(view, 700).left, view, 700), view.start, 'round trip', 1e-9);
}
// Span of the whole recording: nothing to travel
assert.deepStrictEqual(waveThumbGeometry({ start: 0, end: 1 }, 700), { width: 700, left: 0 });
// The markup gets the same fractions as CSS variables
t.app.waveView = { start: 0.2, end: 0.4 };
const style = t.app.waveThumbStyle();
closeTo(style['--thumb-span'], 0.2, 'span var'); closeTo(style['--thumb-position'], 0.25, 'position var (start / (1 - span))');

// ---- 2. user navigation stops following the playhead; seeking and playing resume it ----
t = setup();
t.app.waveView = { start: 0.2, end: 0.4 };
pointerDown(t.app, 400);
assert.strictEqual(t.waveform.options.autoScroll, false, 'scrollbar press stops following');
t.app.seekTo(10);
assert.strictEqual(t.waveform.options.autoScroll, true, 'seeking resumes following');
t.app.wheelZoom(wheel(-100), t.scroller);
assert.strictEqual(t.waveform.options.autoScroll, false, 'wheel zoom stops following');
t.app.startPlayback();
assert.strictEqual(t.waveform.options.autoScroll, true, 'starting playback resumes following');
t.app.wheelZoom(wheel(-100), t.scroller);
t.app.togglePlay();
assert.strictEqual(t.waveform.options.autoScroll, true, 'Play resumes following');
for (const resume of [app => { app.detectedTracks = [{ start: 30, end: 60, number: 1 }]; app.toggleTrackPlay(0); },
                      app => { app.detectedTracks = [{ start: 30, end: 60, number: 1 }]; app.auditionCut(0, 'start'); }]) {
    t.app.wheelZoom(wheel(-100), t.scroller);
    assert.strictEqual(t.waveform.options.autoScroll, false);
    resume(t.app);
    assert.strictEqual(t.waveform.options.autoScroll, true, 'row play and cut audition resume following');
}
// The zoom buttons keep whatever following state there is
t.app.wheelZoom(wheel(-100), t.scroller);
t.app.zoomIn(); t.app.zoomOut();
assert.strictEqual(t.waveform.options.autoScroll, false, 'zoom buttons do not change following');
// A wheel that changes nothing (already at the limit) does not stop following
t = setup({ zoomPxPerSec: 500 });
t.app.wheelZoom(wheel(-100), t.scroller);
assert.strictEqual(t.waveform.options.autoScroll, true, 'a no-op wheel leaves following alone');
// (a rebuilt waveform starting to follow again is checked through initWaveform at the end of the file)
// No waveform / a fake without options: harmless
vinylApp().followPlayhead(false);
const bare = vinylApp(); bare.waveform = {}; bare.followPlayhead(true);

// ---- 2b. at a clamp bound the rounded-up width must not look like a step ----
t = setup({ clientWidth: 900, duration: 75.3371, zoomPxPerSec: 500 });   // ceil(37668.55) = 37669: shown = 499.9999..
closeTo(t.scroller.scrollWidth / 75.3371, 500, 'rounded up width is not exactly 500 px/s', 1e-2);
assert.ok(Math.abs(500 - t.scroller.scrollWidth / 75.3371) > 500 * 1e-6, 'beyond the relative threshold');
for (let i = 0; i < 5; i++) t.app.wheelZoom(wheel(-100), t.scroller);
assert.strictEqual(t.waveform.zoomCalls.length, 0, 'at the zoom-in limit further wheel events do nothing');
assert.strictEqual(t.waveform.options.autoScroll, true, 'and do not stop following');
t = setup({ clientWidth: 701, duration: 1260.25, zoomPxPerSec: 701 / 1260.25 });
t.scroller.scrollWidth = 702;   // WaveSurfer's Math.ceil at fit
for (let i = 0; i < 5; i++) t.app.wheelZoom(wheel(100), t.scroller);
assert.strictEqual(t.waveform.zoomCalls.length, 0, 'at fit (one pixel too wide) further zoom-out events do nothing');
assert.strictEqual(t.waveform.options.autoScroll, true);
// but a step that really moves (2 px/s -> fit) still zooms out
t = setup({ clientWidth: 701, duration: 1260.25, zoomPxPerSec: 2 });
t.app.wheelZoom(wheel(200), t.scroller);
assert.strictEqual(t.waveform.zoomCalls.length, 1);

// ---- 3. one fit-to-width floor ----
t = setup({ clientWidth: 900, duration: 5400, zoomPxPerSec: 1 });   // 90 min: fit = 0.1667 px/s
const fit = 900 / 5400;
for (let i = 0; i < 30; i++) t.app.wheelZoom(wheel(200), t.scroller);
closeTo(t.app.currentZoom, fit, 'wheel zoom-out ends at fit');
t.app.zoomOut();
closeTo(t.app.currentZoom, fit, 'zoom-out button stays at fit (it used to zoom in to 1 px/s)');
closeTo(t.waveform.zoomCalls[t.waveform.zoomCalls.length - 1], fit, 'and tells WaveSurfer so');

// A stale currentZoom (1, from a 'ready' that measured a 0-wide container) must not make the first
// zoom-in clicks invisible: both buttons work from the live fit
t = setup({ clientWidth: 1400, duration: 126, zoomPxPerSec: 1400 / 126 });
const liveFit = 1400 / 126;
t.app.currentZoom = 1;
t.app.zoomIn();
closeTo(t.app.currentZoom, liveFit * 1.5, 'one zoom-in from a stale zoom is 1.5 x fit (it was 1.5 px/s)');
t.app.currentZoom = 1;
t.app.zoomOut();
closeTo(t.app.currentZoom, liveFit, 'zoom-out from a stale zoom stays at fit');
t.app.currentZoom = 1;
t.app._restoreZoom = null;
closeTo(t.app.effectiveZoom(), liveFit, 'effective zoom is never below fit');

// ---- 4. slow pinch on a long recording ----
t = setup({ clientWidth: 900, duration: 5400, zoomPxPerSec: 900 / 5400 });
let before = t.waveform.zoomCalls.length;
for (let i = 0; i < 5; i++) t.app.wheelZoom(wheel(-1), t.scroller);
assert.strictEqual(t.waveform.zoomCalls.length - before, 5, 'deltaY -1 zooms in even at 0.17 px/s');
assert.ok(t.app.currentZoom > fit * 1.01);

// ---- 5. the bar shows by pixels, not by fraction ----
t = setup({ clientWidth: 701, duration: 1260.25, zoomPxPerSec: 0.1 });   // Math.ceil(1260.25 * 701 / 1260.25)
t.waveform.zoom(701 / 1260.25);
assert.strictEqual(t.scroller.scrollWidth, 702, 'WaveSurfer is 1 px too wide at fit (the real browser case)');
t.scroller.listeners.scroll();
assert.strictEqual(t.app.waveScrollable, false, 'fit does not show the scrollbar');
t.waveform.zoom(2);
t.scroller.listeners.scroll();
assert.strictEqual(t.app.waveScrollable, true, 'zoomed in shows it');
t.waveform.zoom(701 / 1260.25);
t.scroller.listeners.scroll();
assert.strictEqual(t.app.waveScrollable, false, 'and zooming back out hides it again');
t.waveform.zoom(2); t.scroller.listeners.scroll();
assert.strictEqual(t.app.waveScrollable, true);
t.app.destroyWaveform();
assert.strictEqual(t.app.waveScrollable, false, 'a destroyed waveform hides the bar');
assert.ok(/<div x-show="waveScrollable"[^>]*class="wave-scrollbar"/.test(html), 'the markup reads the pixel-based state');

// ---- 6. a lost pointer capture ends the drag ----
t = setup();
t.app.waveView = { start: 0.2, end: 0.4 };
drag = pointerDown(t.app, 400);
assert.ok(drag.barListeners.pointermove && drag.barListeners.lostpointercapture, 'listening for the drag');
drag.barListeners.lostpointercapture();
assert.deepStrictEqual(Object.keys(drag.barListeners), [], 'no listener leaks after capture is lost');
assert.ok(!drag.classes.has('dragging'));

// ---- 7. markup nits ----
assert.ok(/class="wave-scrollbar"[^>]*aria-hidden="true"|aria-hidden="true"[^>]*class="wave-scrollbar"|class="wave-scrollbar" data-testid="wave-scrollbar" aria-hidden="true"/.test(html), 'the pointer-only bar is hidden from assistive tech');
assert.ok(/Ctrl on Windows and Linux/.test(html), 'tip names Linux too');

// initWaveform's ready handler, run through the real build with fakes. Last in the file: it replaces
// global.document, which the synchronous sections above and below set up for themselves.
let finished = false;
process.on('exit', () => { if (!finished) { console.error('wave_view_fixes: async part did not finish'); process.exitCode = 1; } });
(async () => {
    global.window = global.window || {};
    global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ peaks: [0, 1], duration: 5400 }) });
    const built = { handlers: {}, zoomCalls: [] };
    const viewScroller = { clientWidth: 900, scrollWidth: 900, scrollLeft: 0, addEventListener() {}, removeEventListener() {}, getBoundingClientRect: () => ({ left: 0 }) };
    const makeWaveform = () => ({ options: { autoScroll: true }, on(event, fn) { (built.handlers[event] ||= []).push(fn); }, destroy() {},
        registerPlugin: () => ({ on() {}, getRegions: () => [], clearRegions() {}, addRegion() {} }), getMediaElement: () => null, getWrapper: () => ({ parentElement: viewScroller }),
        getDuration: () => 5400, zoom(px) { built.zoomCalls.push(px); }, load: async () => {}, setTime() {}, getCurrentTime: () => 0 });
    let fakeWaveform = makeWaveform();
    global.WaveSurfer = { create: () => fakeWaveform, Regions: { create: () => ({}) } };
    const container = { offsetWidth: 900, addEventListener() {} };
    global.document = { getElementById: () => container, addEventListener() {}, removeEventListener() {} };
    const app = vinylApp();
    app.uploadedFiles = [{ id: 'f1', filename: 'side.wav', status: 'analyzed' }];
    app.currentFileId = 'f1'; app.currentFile = app.uploadedFiles[0];
    app.detectedTracks = [];
    await app.initWaveform();
    (built.handlers.ready || []).forEach(fn => fn());
    assert.ok(built.zoomCalls.length > 0, 'the ready handler zoomed');
    closeTo(built.zoomCalls[0], 900 / 5400, 'a 90-minute recording opens at fit, not at 1 px/s');
    // A short recording is capped at 200 px/s as before
    built.zoomCalls.length = 0;
    fakeWaveform.getDuration = () => 2;
    (built.handlers.ready || []).forEach(fn => fn());
    assert.strictEqual(built.zoomCalls[0], 200);
    // 'ready' can fire while the view has no width at all (container and the scroller inside it are 0 wide):
    // zoom 0 makes WaveSurfer fill its parent (fit), not 1 px/s (which would be 5400 px in a 900 px view)
    built.zoomCalls.length = 0;
    fakeWaveform.getDuration = () => 5400;
    container.offsetWidth = 0; viewScroller.clientWidth = 0;
    (built.handlers.ready || []).forEach(fn => fn());
    assert.strictEqual(built.zoomCalls[0], 0, 'unknown width: zoom(0) = fill the parent');
    assert.strictEqual(app.currentZoom, 0);
    // A preview-fallback reload now would store the zoom to restore: 0 (= unset), never 1
    assert.strictEqual(app.effectiveZoom(), 0, 'no width, no fit: nothing to store');
    // Once the view has a width, the first zoom-in is 1.5 x the live fit, not 0 x 1.5 or 1.5 px/s
    container.offsetWidth = 900; viewScroller.clientWidth = 900;
    assert.ok(Math.abs(app.effectiveZoom() - 900 / 5400) < 1e-9);
    app.zoomIn();
    closeTo(built.zoomCalls[built.zoomCalls.length - 1], 1.5 * 900 / 5400, 'first zoom-in from the live fit');
    app.currentZoom = 0; app.zoomOut();
    closeTo(built.zoomCalls[built.zoomCalls.length - 1], 900 / 5400, 'zoom-out from 0 stays at the live fit');
    // The wheel does nothing while the view has no width
    viewScroller.clientWidth = 0;
    const callsBefore = built.zoomCalls.length;
    app.wheelZoom(wheel(-100), viewScroller);
    assert.strictEqual(built.zoomCalls.length, callsBefore, 'no wheel zoom without a view width');
    viewScroller.clientWidth = 900;

    // A click on the waveform (its 'interaction' event) seeks and resumes following the playhead
    app.followPlayhead(false);
    (built.handlers.interaction || []).forEach(fn => fn(12));
    assert.strictEqual(fakeWaveform.options.autoScroll, true, 'a waveform click resumes following');

    // A rebuilt waveform follows again even though the old one had stopped (real WaveSurfer starts every
    // instance with autoScroll true; the app keeps no flag of its own)
    const first = fakeWaveform;
    app.followPlayhead(false);
    app.destroyWaveform();
    built.handlers = {};
    fakeWaveform = makeWaveform();   // a new instance, as WaveSurfer.create gives
    await app.initWaveform();
    assert.strictEqual(first.options.autoScroll, false, 'the old waveform is left as it was');
    assert.strictEqual(app.waveform.options.autoScroll, true, 'the new waveform follows from the start');
    app.wheelZoom(wheel(-100), viewScroller);
    assert.strictEqual(app.waveform.options.autoScroll, false, 'and can be stopped again');
    finished = true;
    console.log('wave view fixes OK');
})().catch(error => { console.error(error); process.exit(1); });

