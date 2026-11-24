// A mouse-clicked checkbox, radio button or <select> gives Space to the player exactly like a
// mouse-clicked button; a keyboard-focused one keeps it.  Text-like fields, number fields,
// textareas and contenteditable keep Space always; a range input (the volume slider) never owns it.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + '; return vinylApp;')();

const press = (target, extra = {}) => ({
    code: 'Space', key: ' ', repeat: false, defaultPrevented: false, target,
    prevented: false, preventDefault() { this.prevented = true; }, ...extra,
});
const keyup = () => ({ code: 'Space', key: ' ', prevented: false, preventDefault() { this.prevented = true; } });
const control = (tagName, type) => ({ tagName, type, contains(node) { return node === this; }, classList: { contains: () => false } });

global.document = { activeElement: null, body: { tagName: 'BODY' } };
// pointerup clears its "pending" flag in a later task; run that task at once so the steps stay in order
const realSetTimeout = global.setTimeout;
global.setTimeout = fn => fn();

function mkApp() {
    const app = vinylApp();
    let toggles = 0;
    app.waveform = {}; app.playDuration = 600;   // a usable player
    app.togglePlay = () => { toggles++; };
    return { app, toggles: () => toggles };
}
const mouseFocus = (app, element) => { app.onPointerDown({ target: element }); document.activeElement = element; app.onFocusIn({ target: element }); app.onPointerUp(); };
const tabFocus = (app, element) => { document.activeElement = element; app.onFocusIn({ target: element }); };

const claimedControls = {
    'checkbox': control('INPUT', 'checkbox'),
    'radio': control('INPUT', 'radio'),
    'select': control('SELECT', undefined),
};
for (const [name, element] of Object.entries(claimedControls)) {
    // keyboard focus: the control keeps Space
    let { app, toggles } = mkApp();
    tabFocus(app, element);
    let event = press(element);
    app.onGlobalKeydown(event);
    assert.ok(!event.prevented && toggles() === 0, name + ': keyboard-focused keeps Space');

    // pointer focus: Space plays/pauses, the control is not toggled or opened (keydown and keyup cancelled)
    mouseFocus(app, element);
    event = press(element);
    app.onGlobalKeydown(event);
    assert.ok(event.prevented, name + ': the clicked control is not pressed by this Space');
    assert.strictEqual(toggles(), 1, name + ': Space toggles playback');
    const up = keyup();
    app.onGlobalKeyup(up);
    assert.ok(up.prevented, name + ': its keyup is cancelled too');

    // a held Space: no second toggle, still claimed
    event = press(element, { repeat: true });
    app.onGlobalKeydown(event);
    assert.ok(event.prevented && toggles() === 1, name + ': a repeat does not toggle again');
    app.onGlobalKeyup(keyup());

    // modifiers: only the keyup is cancelled, nothing plays
    event = press(element, { ctrlKey: true });
    app.onGlobalKeydown(event);
    assert.ok(!event.prevented && toggles() === 1, name + ': Ctrl+Space leaves the keydown to the system');
    const modifierUp = keyup();
    app.onGlobalKeyup(modifierUp);
    assert.ok(modifierUp.prevented, name + ': Ctrl+Space keyup is cancelled');

    // tabbing to it hands Space back
    tabFocus(app, control('BUTTON'));
    tabFocus(app, element);
    event = press(element);
    app.onGlobalKeydown(event);
    assert.ok(!event.prevented && toggles() === 1, name + ': Tab-focused again keeps Space');

    // the player cannot play (no waveform yet, or the plain fallback player shows): Space has nothing
    // to play, so the clicked control keeps it (e.g. "Only show releases with track lengths" before a
    // release is picked) instead of Space becoming a dead key
    for (const [state, setUp] of [['no waveform', a => { a.waveform = null; }], ['fallback player', a => { a.fallbackAudioUrl = '/api/audio/x'; }]]) {
        ({ app, toggles } = mkApp());
        setUp(app);
        mouseFocus(app, element);
        event = press(element);
        app.onGlobalKeydown(event);
        assert.ok(!event.prevented && toggles() === 0, `${name}, ${state}: the control keeps Space`);
        const upNoPlayer = keyup();
        app.onGlobalKeyup(upNoPlayer);
        assert.ok(!upNoPlayer.prevented, `${name}, ${state}: its keyup is not cancelled`);
    }
}

// Controls that always keep Space, even right after a mouse click
const alwaysOwned = {
    'text input': control('INPUT', 'text'),
    'number input': control('INPUT', 'number'),
    'search input': control('INPUT', 'search'),
    'input without a type': control('INPUT', undefined),
    'textarea': control('TEXTAREA'),
    'summary': control('SUMMARY'),
    'contenteditable': { ...control('DIV'), isContentEditable: true },
};
for (const [name, element] of Object.entries(alwaysOwned)) {
    const { app, toggles } = mkApp();
    mouseFocus(app, element);
    const event = press(element);
    app.onGlobalKeydown(event);
    assert.ok(!event.prevented && toggles() === 0, name + ' keeps Space after a click');
}
// a range input (the volume slider) never owned Space
{
    const { app, toggles } = mkApp();
    const event = press(control('INPUT', 'range'));
    app.onGlobalKeydown(event);
    assert.ok(event.prevented && toggles() === 1, 'range input: Space plays');
}

// While Settings, setup or a right-click menu is open the player is out of reach, so a clicked checkbox,
// radio button or select there keeps its own Space (Settings is full of them); nothing plays
for (const blocker of ['showSettings', 'setupRequired', 'contextMenu', 'regionContextMenu']) {
    for (const [name, element] of Object.entries(claimedControls)) {
        const { app, toggles } = mkApp();
        if (blocker.endsWith('Menu')) app[blocker].show = true; else app[blocker] = true;
        mouseFocus(app, element);
        const event = press(element);
        app.onGlobalKeydown(event);
        assert.ok(!event.prevented, `${name} in ${blocker}: the control keeps Space`);
        const up = keyup();
        app.onGlobalKeyup(up);
        assert.ok(!up.prevented, `${name} in ${blocker}: its keyup is not cancelled`);
        assert.strictEqual(toggles(), 0, `${name} in ${blocker}: nothing plays`);
    }
}

global.setTimeout = realSetTimeout;
delete global.document;
console.log('space pointer controls ok');
