// The regions plugin ends a drag only on a document pointerup/pointercancel and never checks
// event.buttons, so a lost pointerup (macOS Ctrl+click, fast double-click) leaves the region
// glued to the pointer. guardRegionDragRelease ends such a drag with a synthetic pointerup.
//
// Fakes: `document` records listeners (with their capture flag) and dispatched events, and
// dispatchEvent also runs the matching listeners like a real document would. A region element
// only stores its listeners so the test can fire pointerdown on it.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + ';return vinylApp;')();

// Node has no DOM event classes; the guard only needs `type` and the coordinates back.
global.PointerEvent = class { constructor(type, init) { this.type = type; Object.assign(this, init); } };

function setup() {
    const listeners = [], dispatched = [];
    global.document = {
        addEventListener: (type, fn, capture) => listeners.push({ type, fn, capture }),
        removeEventListener: () => {},
        dispatchEvent(event) {
            dispatched.push(event);
            listeners.filter(l => l.type === event.type).forEach(l => l.fn(event));
            return true;
        },
    };
    const app = vinylApp();
    const guard = () => {
        const handlers = {};
        app.guardRegionDragRelease({ on(ev, fn) { handlers[ev] = fn; } });
        return handlers;
    };
    const handlers = guard();
    const region = () => {
        const captured = {};
        handlers['region-created']({ element: { addEventListener: (ev, fn, capture) => { captured[ev] = { fn, capture }; } } });
        return captured;
    };
    const fire = (type, init) => listeners.filter(l => l.type === type).forEach(l => l.fn({ clientX: 5, clientY: 6, button: 0, buttons: 1, ...init }));
    const syntheticUps = () => dispatched.filter(e => e.type === 'pointerup').length;
    return { app, guard, region, fire, listeners, syntheticUps };
}

// Every document listener is a capture-phase one: it must see the events before the plugin (and anything
// that stops propagation) does, or a lost release would go unnoticed
let t = setup();
for (const type of ['pointermove', 'pointerup', 'pointercancel']) {
    const registered = t.listeners.filter(l => l.type === type);
    assert.ok(registered.length > 0, type + ' is listened for');
    assert.ok(registered.every(l => l.capture === true), type + ' listener is capture-phase');
}

// Pointer released outside the window: first move with no button ends the drag exactly once
t = setup();
let el = t.region();
assert.strictEqual(el.pointerdown.capture, true, 'runs before the plugin and handle listeners');
el.pointerdown.fn({ button: 0, clientX: 1, clientY: 2 });
t.fire('pointermove', { buttons: 1 });
assert.strictEqual(t.syntheticUps(), 0, 'button held: leave the drag alone');
t.fire('pointermove', { buttons: 0 });
assert.strictEqual(t.syntheticUps(), 1);
t.fire('pointermove', { buttons: 0 });
assert.strictEqual(t.syntheticUps(), 1, 'already ended');

// A real release clears it
t = setup(); el = t.region();
el.pointerdown.fn({ button: 0 });
t.fire('pointerup', {});
t.fire('pointermove', { buttons: 0 });
assert.strictEqual(t.syntheticUps(), 0);
el.pointerdown.fn({ button: 0 });
t.fire('pointercancel', {});
t.fire('pointermove', { buttons: 0 });
assert.strictEqual(t.syntheticUps(), 0);

// Second press while the first drag never ended (double-click, lost pointerup): end the first
t = setup(); el = t.region();
el.pointerdown.fn({ button: 0 });
el.pointerdown.fn({ button: 0, clientX: 9, clientY: 8 });
assert.strictEqual(t.syntheticUps(), 1);
t.fire('pointermove', { buttons: 0 });   // the new drag is tracked too
assert.strictEqual(t.syntheticUps(), 2);

// Non-primary button never starts a drag
t = setup(); el = t.region();
el.pointerdown.fn({ button: 2 });
t.fire('pointermove', { buttons: 0 });
assert.strictEqual(t.syntheticUps(), 0);

// Waveform reload: guarding again must not double the document listeners or dispatches
t = setup();
const before = t.listeners.length;
t.guard();
assert.strictEqual(t.listeners.length, before);
el = t.region();
el.pointerdown.fn({ button: 0 });
t.fire('pointermove', { buttons: 0 });
assert.strictEqual(t.syntheticUps(), 1);
console.log('region drag guard OK');
