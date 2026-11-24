// Whole-region drags: regions narrower than the view keep WaveSurfer's own scroll-into-view;
// wider ones follow their leading edge (start when dragging left, end when dragging right),
// fixed for the drag, instead of flipping between start and end on every move.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + ';return vinylApp;')();

function setup() {
    const app = vinylApp();
    const handlers = {}, followed = [];
    const regions = { on(ev, fn) { handlers[ev] = fn; }, adjustScroll(region) { followed.push(region.id); } };
    const view = { clientWidth: 1000, scrollWidth: 12000, scrollLeft: 5000 };   // 100 px/s over 120 s
    const waveform = { getWrapper: () => ({ parentElement: view }), getDuration: () => 120 };
    app.tameRegionDragScroll(waveform, regions);
    const region = (id, start, end) => {
        const listeners = {};
        const r = { id, start, end, element: { getBoundingClientRect: () => ({ width: (r.end - r.start) * 100 }),
            addEventListener: (ev, fn) => { listeners[ev] = fn; } } };
        handlers['region-created'](r);
        return { r, press: () => listeners.pointerdown() };
    };
    return { regions, view, followed, handlers, region };
}

// Narrow region: WaveSurfer's own behaviour
let t = setup();
let { r } = t.region('narrow', 10, 15);
t.regions.adjustScroll(r);
assert.deepStrictEqual(t.followed, ['narrow']);

// Wide region dragged left: start pinned 15 % from the left edge from the first move on
t = setup();
let wide = t.region('wide', 40, 80);
wide.press();
wide.r.start -= 0.5; wide.r.end -= 0.5; t.regions.adjustScroll(wide.r);
assert.deepStrictEqual(t.followed, [], 'never the flip-flopping WaveSurfer rule');
assert.strictEqual(t.view.scrollLeft, 39.5 * 100 - 150);
// direction is fixed for the drag: a small move back keeps following the start
wide.r.start += 0.1; wide.r.end += 0.1; t.regions.adjustScroll(wide.r);
assert.strictEqual(t.view.scrollLeft, 39.6 * 100 - 150);
t.handlers['region-updated'](wide.r);   // drag ended

// Wide region dragged right: end pinned 15 % from the right edge
wide.press();
wide.r.start += 1; wide.r.end += 1; t.regions.adjustScroll(wide.r);
assert.strictEqual(t.view.scrollLeft, wide.r.end * 100 - (1000 - 150));

// No movement yet: nothing scrolls
const still = t.region('still', 0, 50); still.press();
const before = t.view.scrollLeft; t.regions.adjustScroll(still.r);
assert.strictEqual(t.view.scrollLeft, before);
// Drag state does not outlive the drag: after region-updated, a call that has no pointerdown behind it
// (nothing says which way the region is going) must not reuse the finished drag's direction
t = setup();
const finishedDrag = t.region('finished', 30, 90);
finishedDrag.press();
finishedDrag.r.start -= 1; finishedDrag.r.end -= 1; t.regions.adjustScroll(finishedDrag.r);   // dir = left
t.handlers['region-updated'](finishedDrag.r);
t.view.scrollLeft = 4000;
finishedDrag.r.start += 2; finishedDrag.r.end += 2; t.regions.adjustScroll(finishedDrag.r);
assert.strictEqual(t.view.scrollLeft, 4000, 'a finished drag leaves nothing behind');
vinylApp().tameRegionDragScroll({ getWrapper: () => null }, {});   // plugin without adjustScroll: untouched
console.log('region drag scroll ok');
