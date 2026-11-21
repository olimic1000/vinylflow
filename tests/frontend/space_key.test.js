// Space toggles the waveform Play/Pause, except in a field, a dialog, or on a focused button (a button
// handles Space itself on keyup, so toggling here too would fire twice).  It never scrolls the page.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + '; return vinylApp;')();

const press = (target, extra = {}) => ({
    code: 'Space', key: ' ', repeat: false, defaultPrevented: false, target,
    prevented: false, preventDefault() { this.prevented = true; }, ...extra,
});
function mkApp(withWaveform = true) {
    const app = vinylApp();
    let toggles = 0;
    if (withWaveform) app.waveform = {};
    app.togglePlay = () => { toggles++; };
    return { app, toggles: () => toggles };
}

let { app, toggles } = mkApp();
let event = press({ tagName: 'DIV' });
app.onGlobalKeydown(event);
assert.strictEqual(toggles(), 1);
assert.ok(event.prevented, 'the page must not scroll');
app.onGlobalKeydown(press({ tagName: 'BODY' }));
assert.strictEqual(toggles(), 2);

for (const tag of ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'SUMMARY']) {
    event = press({ tagName: tag });
    app.onGlobalKeydown(event);
    assert.ok(!event.prevented, `${tag} keeps its own Space`);
}
event = press({ tagName: 'DIV', isContentEditable: true });
app.onGlobalKeydown(event);
assert.ok(!event.prevented);
assert.strictEqual(toggles(), 2, 'no toggle from a field or a button');

// Holding Space repeats keydown: no toggle (it would flicker Play/Pause), but the page must not scroll.
// (This used to assert the repeat was NOT prevented; the requirement is that held Space never scrolls.)
const togglesBeforeRepeat = toggles();
event = press({ tagName: 'DIV' }, { repeat: true });
app.onGlobalKeydown(event);
assert.ok(event.prevented, 'a held Space must not scroll the page');
assert.strictEqual(toggles(), togglesBeforeRepeat, 'a repeat does not toggle');
for (const tag of ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'SUMMARY']) {
    event = press({ tagName: tag }, { repeat: true });
    app.onGlobalKeydown(event);
    assert.ok(!event.prevented, `a repeat in ${tag} keeps its own Space`);
}

for (const extra of [{ defaultPrevented: true }, { ctrlKey: true }, { metaKey: true }, { altKey: true }, { code: 'KeyA', key: 'a' }]) {
    event = press({ tagName: 'DIV' }, extra);
    app.onGlobalKeydown(event);
    assert.ok(!event.prevented, JSON.stringify(extra));
}
app.showSettings = true;
event = press({ tagName: 'DIV' }); app.onGlobalKeydown(event); assert.ok(!event.prevented, 'settings dialog open');
app.showSettings = false; app.setupRequired = true;
event = press({ tagName: 'DIV' }); app.onGlobalKeydown(event); assert.ok(!event.prevented, 'setup dialog open');
assert.strictEqual(toggles(), 2);

// No waveform yet: Space is left to the page
({ app, toggles } = mkApp(false));
event = press({ tagName: 'DIV' });
app.onGlobalKeydown(event);
assert.ok(!event.prevented && toggles() === 0);
console.log('space ok');
