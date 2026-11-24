// Custom waveform scrollbar (waveView fractions, track press, thumb drag) and Cmd/Ctrl + wheel
// zoom around the pointer.  Fakes: the scroll element is a plain object with the geometry
// WaveSurfer's scroller has (clientWidth = view, scrollWidth = whole zoomed waveform); the
// waveform records its event handlers and zoom calls; `document.getElementById('waveform')`
// is a container that only stores its wheel listeners so the test can fire and count them.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + ';return vinylApp;')();

const DURATION = 100;   // seconds
function setup({ clientWidth = 1000, scrollWidth = 5000, scrollLeft = 0 } = {}) {
    const container = { listeners: [], addEventListener(type, fn) { this.listeners.push({ type, fn }); },
        removeEventListener(type, fn) { this.listeners = this.listeners.filter(l => !(l.type === type && l.fn === fn)); } };
    global.document = { getElementById: id => (id === 'waveform' ? container : null), addEventListener() {} };
    const scroller = { clientWidth, scrollWidth, scrollLeft, listeners: {},
        addEventListener(type, fn) { this.listeners[type] = fn; },
        removeEventListener(type) { delete this.listeners[type]; },
        getBoundingClientRect: () => ({ left: 100 }) };
    const handlers = {};
    const waveform = { zoomCalls: [], destroy() { (handlers.destroy || []).forEach(fn => fn()); }, on(ev, fn) { (handlers[ev] ||= []).push(fn); },
        getWrapper: () => ({ parentElement: scroller }), getDuration: () => DURATION,
        zoom(px) { this.zoomCalls.push(px); scroller.scrollWidth = Math.max(clientWidth, px * DURATION); } };
    const app = vinylApp();
    app.waveform = waveform;
    app.bindWaveScrollbar(waveform);
    return { app, scroller, waveform, handlers, container };
}
const fire = (handlers, ev) => (handlers[ev] || []).forEach(fn => fn());
const wheelEvent = (init = {}) => ({ ctrlKey: true, metaKey: false, deltaY: -100, deltaMode: 0, clientX: 600,
    prevented: false, preventDefault() { this.prevented = true; }, ...init });
const closeTo = (actual, expected, message) => assert.ok(Math.abs(actual - expected) < 1e-9, `${message}: ${actual} != ${expected}`);

// waveView: fitted waveform shows everything; scrolling a zoomed one gives the visible fractions
let t = setup({ scrollWidth: 1000 });
fire(t.handlers, 'ready');
assert.deepStrictEqual(t.app.waveView, { start: 0, end: 1 }, 'fitted: whole recording visible (scrollbar hidden)');
t = setup({ scrollWidth: 5000 });
t.scroller.scrollLeft = 1000;
t.scroller.listeners.scroll();
closeTo(t.app.waveView.start, 0.2, 'start'); closeTo(t.app.waveView.end, 0.4, 'end');
t.scroller.scrollLeft = 4200;   // 4200 + 1000 > 5000: the end clamps at 1
t.scroller.listeners.scroll();
closeTo(t.app.waveView.end, 1, 'end clamps at 1');
fire(t.handlers, 'zoom');   // zoom/redraw events refresh it too
t.app.waveform = {};   // a stale waveform's event must not overwrite the current view
const frozen = t.app.waveView;
t.scroller.scrollLeft = 0; t.scroller.listeners.scroll();
assert.strictEqual(t.app.waveView, frozen, 'a replaced waveform no longer drives waveView');

// Scrollbar interaction: bar 1000 px wide at x 0..1000, view 20 %..40 %
function pointerDown(app, clientX, extra = {}) {
    const barListeners = {}, classes = new Set();
    const bar = { getBoundingClientRect: () => ({ left: 0, width: 1000 }), classList: { add: c => classes.add(c), remove: c => classes.delete(c) },
        setPointerCapture() {}, addEventListener: (type, fn) => { barListeners[type] = fn; },
        removeEventListener: type => { delete barListeners[type]; } };
    app.waveScrollbarDown({ currentTarget: bar, button: 0, clientX, pointerId: 1, preventDefault() {}, ...extra });
    return { barListeners, classes };
}
t = setup();
t.app.waveView = { start: 0.2, end: 0.4 };
// Press on the track at 80 %: view centres there (start 70 %), the thumb is then held by its middle
let drag = pointerDown(t.app, 800);
closeTo(t.scroller.scrollLeft, 0.7 * 5000, 'track press centres the view');
assert.ok(drag.classes.has('dragging'));
drag.barListeners.pointermove({ clientX: 500 });
closeTo(t.scroller.scrollLeft, 0.4 * 5000, 'thumb follows the pointer by its middle');
drag.barListeners.pointermove({ clientX: 1200 });
closeTo(t.scroller.scrollLeft, 0.8 * 5000, 'clamped so the thumb stays on the bar (1 - span)');
drag.barListeners.pointermove({ clientX: -300 });
closeTo(t.scroller.scrollLeft, 0, 'clamped at the left end');
drag.barListeners.pointerup();
assert.ok(!drag.classes.has('dragging') && !drag.barListeners.pointermove, 'release ends the drag');
// Press on the thumb itself keeps the grab point: no jump on press, then moves relative to it
t.scroller.scrollLeft = 1000;
drag = pointerDown(t.app, 250);   // 5 % into... thumb starts at 20 %, grab offset = 5 %
closeTo(t.scroller.scrollLeft, 1000, 'pressing the thumb does not move the view');
drag.barListeners.pointermove({ clientX: 450 });
closeTo(t.scroller.scrollLeft, (0.45 - 0.05) * 5000, 'drag keeps the grab offset');
// Secondary button does nothing
const before = t.scroller.scrollLeft;
pointerDown(t.app, 900, { button: 2 });
assert.strictEqual(t.scroller.scrollLeft, before);

// Wheel zoom: Ctrl or Meta required, plain wheel untouched
t = setup();   // 50 px/s, fit = 10 px/s
const plain = wheelEvent({ ctrlKey: false });
t.container.listeners.find(l => l.type === 'wheel').fn(plain);
assert.strictEqual(plain.prevented, false, 'plain wheel is left to the page');
assert.deepStrictEqual(t.waveform.zoomCalls, []);
const ctrl = wheelEvent();
t.container.listeners.find(l => l.type === 'wheel').fn(ctrl);
assert.strictEqual(ctrl.prevented, true, 'Ctrl + wheel is ours');
assert.strictEqual(t.waveform.zoomCalls.length, 1);
closeTo(t.waveform.zoomCalls[0], 50 * Math.exp(100 * 0.003), 'zoom factor exp(-deltaY * WHEEL_ZOOM_RATE)');
t = setup();
const meta = wheelEvent({ ctrlKey: false, metaKey: true, deltaY: 100 });
t.container.listeners.find(l => l.type === 'wheel').fn(meta);
assert.strictEqual(meta.prevented, true, 'Cmd + wheel zooms too');
assert.ok(t.waveform.zoomCalls[0] < 50, 'positive delta zooms out');

// Clamp: fit-to-width .. 500 px/s
t = setup();
t.app.wheelZoom(wheelEvent({ deltaY: -10000 }), t.scroller);   // huge zoom in is capped per event (200)
t.app.wheelZoom(wheelEvent({ deltaY: -200 }), t.scroller);
for (let i = 0; i < 20; i++) t.app.wheelZoom(wheelEvent({ deltaY: -200 }), t.scroller);
closeTo(t.waveform.zoomCalls[t.waveform.zoomCalls.length - 1], 500, 'capped at 500 px/s');
const zoomsAtMax = t.waveform.zoomCalls.length;
t.app.wheelZoom(wheelEvent({ deltaY: -200 }), t.scroller);
assert.strictEqual(t.waveform.zoomCalls.length, zoomsAtMax, 'no redundant zoom at the limit');
for (let i = 0; i < 40; i++) t.app.wheelZoom(wheelEvent({ deltaY: 200 }), t.scroller);
closeTo(t.waveform.zoomCalls[t.waveform.zoomCalls.length - 1], 1000 / DURATION, 'fit-to-width is the lower limit');
closeTo(t.app.currentZoom, 1000 / DURATION, 'currentZoom follows (zoom buttons continue from here)');

// Anchored at the pointer: the moment under the pointer stays under it
t = setup({ scrollWidth: 5000, scrollLeft: 1500 });
const pointerX = 300;   // px from the scroller's left edge (clientX 400, scroller at 100)
const momentBefore = (t.scroller.scrollLeft + pointerX) / (t.scroller.scrollWidth / DURATION);
t.app.wheelZoom(wheelEvent({ clientX: 400, deltaY: -150 }), t.scroller);
const momentAfter = (t.scroller.scrollLeft + pointerX) / (t.scroller.scrollWidth / DURATION);
closeTo(momentAfter, momentBefore, 'zoom anchored at the pointer');
assert.ok(t.scroller.scrollWidth > 5000);
// Line-mode deltas (Firefox) count as pixels
t = setup();
t.app.wheelZoom(wheelEvent({ deltaMode: 1, deltaY: -5 }), t.scroller);
closeTo(t.waveform.zoomCalls[0], 50 * Math.exp(80 * 0.003), 'deltaMode 1: 16 px per line');

// Destroy removes both listeners; a replaced waveform's wheel is ignored
t = setup();
const wheelFn = t.container.listeners[0].fn;
t.app.waveform = {};
const stale = wheelEvent();
wheelFn(stale);
assert.strictEqual(stale.prevented, false, 'a stale waveform does not hijack the wheel');
assert.ok(t.container.listeners.length === 1 && t.scroller.listeners.scroll);
fire(t.handlers, 'destroy');
assert.strictEqual(t.container.listeners.length, 0, 'wheel listener removed on destroy');
assert.strictEqual(t.scroller.listeners.scroll, undefined, 'scroll listener removed on destroy');
// destroyWaveform hides the scrollbar again for the next recording
t = setup();
t.app.waveView = { start: 0.2, end: 0.4 };
t.app.destroyWaveform();   // the real destroy() emits 'destroy': listeners go with it
assert.strictEqual(t.container.listeners.length, 0);
assert.deepStrictEqual(t.app.waveView, { start: 0, end: 1 });
assert.strictEqual(t.app._waveScroller, null);

// Wiring: initWaveform hides WaveSurfer's own scrollbar and binds all three behaviours to the
// new waveform.  Peaks never arrive, so it stops right after the plugin is registered.
let finished = false;
process.on('exit', () => { if (!finished) { console.error('wiring check did not finish'); process.exitCode = 1; } });
global.window = global.window || {};
global.fetch = async () => ({ ok: false, status: 500, json: async () => ({ detail: 'stop here' }) });
let options = null;
const bound = [];
global.WaveSurfer = {
    create: given => (options = given, { on() {}, registerPlugin: plugin => plugin, destroy() {}, getMediaElement: () => null }),
    Regions: { create: () => ({}) },
};
(async () => {
    const app = vinylApp();
    for (const name of ['tameRegionDragScroll', 'guardRegionDragRelease', 'bindWaveScrollbar']) app[name] = () => bound.push(name);
    app.uploadedFiles = [{ id: 'f1', filename: 'side.wav', status: 'analyzed' }];
    app.currentFileId = 'f1';
    app.currentFile = app.uploadedFiles[0];
    await app.initWaveform();
    assert.strictEqual(options.hideScrollbar, true, 'WaveSurfer scrollbar hidden (ours replaces it)');
    assert.deepStrictEqual(bound, ['tameRegionDragScroll', 'guardRegionDragRelease', 'bindWaveScrollbar']);
    finished = true;
    console.log('wave scrollbar and wheel zoom OK');
})().catch(error => { console.error(error); process.exit(1); });
