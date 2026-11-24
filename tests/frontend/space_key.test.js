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
    if (withWaveform) { app.waveform = {}; app.playDuration = 600; }   // a usable player: built and ready
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

// ---- a BUTTON keeps Space only when keyboard-focused.  Focus by a mouse click (Chromium, Firefox, WebView2
// focus a clicked button) must not let Space press it again: Space plays/pauses instead.  The focus origin
// comes from pointerdown -> focusin (:focus-visible is unusable: Chromium turns it on at any keypress). ----
const buttonEl = { tagName: 'BUTTON', matches: () => { throw new Error('focus-visible must not be consulted'); }, contains(node) { return node === this; } };
({ app, toggles } = mkApp());
global.document = { activeElement: null, body: { tagName: 'BODY' } };
// pointerup clears its "pending" flag in a later task; run that task at once so the steps stay in order
const realSetTimeout = global.setTimeout;
global.setTimeout = (fn) => fn();
const mouseFocus = () => { app.onPointerDown({ target: buttonEl }); document.activeElement = buttonEl; app.onFocusIn(); app.onPointerUp(); };
const tabFocus = () => { document.activeElement = buttonEl; app.onFocusIn(); };
mouseFocus();
event = press(buttonEl);
app.onGlobalKeydown(event);
assert.strictEqual(toggles(), 1, 'a mouse-focused button: Space toggles playback');
assert.ok(event.prevented, '... and the button is not activated by this Space');
tabFocus();
event = press(buttonEl);
app.onGlobalKeydown(event);
assert.ok(!event.prevented && toggles() === 1, 'a Tab-focused button keeps Space');
// focusin with no pointerdown before it is keyboard focus; a pointerdown on the element that
// already has focus fires no focusin but makes it pointer-focused
app.onPointerDown({ target: buttonEl }); app.onPointerUp();   // focus already on buttonEl
event = press(buttonEl);
app.onGlobalKeydown(event);
assert.strictEqual(toggles(), 2, 'clicking the already-focused button counts as pointer focus');
tabFocus();
event = press(buttonEl);
app.onGlobalKeydown(event);
assert.ok(!event.prevented && toggles() === 2, 'Tab afterwards hands Space back to the button');
// init() runs twice in the page (Alpine calls it, and so does x-init); bindGlobalListeners binds the
// document listeners once, but if an event ever reached the handler twice, the second focusin call
// for the same event must not undo the first.
// (Measured in Chromium: without this, Space still re-clicked Reverse.)
const focusEvent = { type: 'focusin' };
app.onPointerDown({ target: buttonEl }); document.activeElement = buttonEl;
app.onFocusIn(focusEvent); app.onFocusIn(focusEvent); app.onPointerUp();
event = press(buttonEl);
app.onGlobalKeydown(event);
assert.strictEqual(toggles(), 3, 'a doubled focusin listener keeps the button pointer-focused');
app.onGlobalKeyup({ code: 'Space', key: ' ', preventDefault() {} });   // finish that key press
tabFocus();
// Space on a clicked button must not click it on keyup either (Firefox activates on keyup)
const keyup = { code: 'Space', key: ' ', prevented: false, preventDefault() { this.prevented = true; } };
app.onGlobalKeyup(keyup);
assert.ok(!keyup.prevented, 'keyup of a Space the player did not claim is untouched');
mouseFocus();
app.onGlobalKeydown(press(buttonEl));
app.onGlobalKeyup(keyup);
assert.ok(keyup.prevented, 'keyup of a claimed Space is cancelled so the focused button never clicks');
// a click that lands elsewhere while a button keeps focus: pointerdown on another node is not a re-click
tabFocus();
app.onPointerDown({ target: { tagName: 'DIV' } }); document.activeElement = buttonEl;
assert.strictEqual(app._pointerFocus.byPointer, false, 'pointerdown outside the focused element');
// A slider and a select keep Space
for (const tag of ['INPUT', 'SELECT']) {
    event = press({ tagName: tag });
    app.onGlobalKeydown(event);
    assert.ok(!event.prevented, tag + ' unchanged');
}
delete global.document;
global.setTimeout = realSetTimeout;

// ---- a right-click menu open: Space does nothing, like the Settings dialog ----
({ app, toggles } = mkApp());
for (const menu of ['contextMenu', 'regionContextMenu']) {
    app[menu].show = true;
    event = press({ tagName: 'DIV' });
    app.onGlobalKeydown(event);
    assert.ok(!event.prevented && toggles() === 0, menu + ' open');
    app[menu].show = false;
}
// Fallback player on screen (no waveform audio): Space is left alone
app.fallbackAudioUrl = '/api/audio/x';
event = press({ tagName: 'DIV' });
app.onGlobalKeydown(event);
assert.ok(!event.prevented && toggles() === 0, 'fallback player');

// ---- a mouse-focused button is never re-pressed by Space, even when the player cannot play ----
// (waveform failed / not ready, a right-click menu open, the fallback player shown): Space does nothing
// else, but the button must not be activated either.
{
    global.document = { activeElement: null, body: { tagName: 'BODY' } };
    const realTimeout = global.setTimeout; global.setTimeout = fn => fn();
    const clickedButton = { tagName: 'BUTTON', contains(node) { return node === this; } };
    const states = {
        'no waveform': a => { a.waveform = null; },
        'waveform still loading': a => { a.waveformLoading = true; },
        'no duration yet': a => { a.playDuration = 0; },
        'context menu open': a => { a.contextMenu.show = true; },
        'region menu open': a => { a.regionContextMenu.show = true; },
        'fallback player shown': a => { a.fallbackAudioUrl = '/api/audio/x'; },
        'settings dialog': a => { a.showSettings = true; },
    };
    for (const [name, setUp] of Object.entries(states)) {
        ({ app, toggles } = mkApp());
        setUp(app);
        app.onPointerDown({ target: clickedButton }); document.activeElement = clickedButton; app.onFocusIn({}); app.onPointerUp();
        event = press(clickedButton);
        app.onGlobalKeydown(event);
        assert.ok(event.prevented, name + ': the clicked button is not pressed again');
        assert.strictEqual(toggles(), 0, name + ': and nothing plays');
        const up = { code: 'Space', key: ' ', prevented: false, preventDefault() { this.prevented = true; } };
        app.onGlobalKeyup(up);
        assert.ok(up.prevented, name + ': its keyup is cancelled too');
        // a keyboard-focused button keeps Space in the same state
        app.onFocusIn({});
        event = press(clickedButton);
        app.onGlobalKeydown(event);
        assert.ok(!event.prevented, name + ': a Tab-focused button keeps Space');
        // and a non-button target is left to the page when nothing can play
        event = press({ tagName: 'DIV' });
        app.onGlobalKeydown(event);
        assert.ok(!event.prevented && toggles() === 0, name + ': a page target is untouched');
    }
    global.setTimeout = realTimeout;
    delete global.document;
}

// ---- readiness: Space needs a built, loaded player with a duration ----
({ app, toggles } = mkApp());
assert.strictEqual(app.playerReady, true);
app.waveformLoading = true; assert.strictEqual(app.playerReady, false);
app.waveformLoading = false; app.playDuration = 0; assert.strictEqual(app.playerReady, false);
event = press({ tagName: 'DIV' }); app.onGlobalKeydown(event);
assert.ok(!event.prevented && toggles() === 0, 'no Space while the waveform is still loading its duration');
app.waveform = null; app.playDuration = 600; assert.strictEqual(app.playerReady, false);

// ---- init() runs twice (Alpine and x-init): a doubled keydown still cancels the keyup ----
{
    global.document = { activeElement: null, body: { tagName: 'BODY' } };
    const realTimeout = global.setTimeout; global.setTimeout = fn => fn();
    ({ app, toggles } = mkApp());
    const clicked = { tagName: 'BUTTON', contains(node) { return node === this; } };
    app.onPointerDown({ target: clicked }); document.activeElement = clicked; app.onFocusIn({}); app.onPointerUp();
    // the second registered listener sees an event the first already prevented
    const doubled = press(clicked, { preventDefault() { this.prevented = true; this.defaultPrevented = true; } });
    app.onGlobalKeydown(doubled); app.onGlobalKeydown(doubled);
    assert.strictEqual(toggles(), 1, 'toggled once');
    const up = { code: 'Space', key: ' ', prevented: false, preventDefault() { this.prevented = true; } };
    app.onGlobalKeyup(up);
    assert.ok(up.prevented, 'the keyup is still cancelled after a doubled keydown');

    // ---- a pointerdown with no pointerup (touch scroll, pen cancel) must not turn the next Tab into pointer focus ----
    ({ app, toggles } = mkApp());
    app.onPointerDown({ target: { tagName: 'DIV' } });
    app.onPointerCancel();
    app.onFocusIn({});   // Tab
    assert.strictEqual(app._pointerFocus.byPointer, false, 'pointercancel clears pending');
    // any keydown (Tab's precedes its focusin) clears it as well
    app.onPointerDown({ target: { tagName: 'DIV' } });
    app.onGlobalKeydown(press({ tagName: 'DIV' }, { code: 'Tab', key: 'Tab' }));
    app.onFocusIn({});
    assert.strictEqual(app._pointerFocus.byPointer, false, 'a keydown clears pending');
    // focusin consumes pending: a later, unrelated focusin is not pointer focus
    app.onPointerDown({ target: { tagName: 'DIV' } });
    app.onFocusIn({});
    assert.strictEqual(app._pointerFocus.byPointer, true);
    app.onFocusIn({});
    assert.strictEqual(app._pointerFocus.byPointer, false, 'pending was reset by the first focusin');
    global.setTimeout = realTimeout;
    delete global.document;
}

// ---- modifiers never let Space press a mouse-clicked button; a slider does not own Space ----
{
    global.document = { activeElement: null, body: { tagName: 'BODY' } };
    const realTimeout = global.setTimeout; global.setTimeout = fn => fn();
    const clicked = { tagName: 'BUTTON', contains(node) { return node === this; } };
    ({ app, toggles } = mkApp());
    app.onPointerDown({ target: clicked }); document.activeElement = clicked; app.onFocusIn({ target: clicked }); app.onPointerUp();
    for (const modifier of ['ctrlKey', 'altKey', 'metaKey']) {
        event = press(clicked, { [modifier]: true });
        app.onGlobalKeydown(event);
        // Only the keyup is cancelled: cancelling the keydown too could suppress a system shortcut such as
        // the Windows Alt+Space window menu, and cancelling the keyup alone stops the button's activation
        // in Chromium, WebKit and Firefox (measured with real input: 0 extra clicks)
        assert.ok(!event.prevented, modifier + '+Space keeps its keydown for the system');
        assert.strictEqual(toggles(), 0, modifier + '+Space does not play either');
        const modifierUp = { code: 'Space', key: ' ', prevented: false, preventDefault() { this.prevented = true; } };
        app.onGlobalKeyup(modifierUp);
        assert.ok(modifierUp.prevented, modifier + '+Space: the keyup is cancelled, so the button is not pressed');
    }
    app.onFocusIn({ target: clicked });   // keyboard focus again (a different focusin with nothing pending)
    // (same element, so it keeps the pointer origin: see the window re-activation test) -> use another element
    const other = { tagName: 'BUTTON', contains(node) { return node === this; } };
    app.onFocusIn({ target: other });
    event = press(other, { ctrlKey: true });
    app.onGlobalKeydown(event);
    assert.ok(!event.prevented, 'a keyboard-focused button keeps Ctrl+Space');

    // a range input (the volume slider): Space plays, whether clicked or tabbed to
    ({ app, toggles } = mkApp());
    const slider = { tagName: 'INPUT', type: 'range' };
    event = press(slider); app.onGlobalKeydown(event);
    assert.ok(event.prevented && toggles() === 1, 'Space on a range input toggles playback (no page scroll)');
    for (const type of ['text', 'number', 'checkbox']) {
        event = press({ tagName: 'INPUT', type }); app.onGlobalKeydown(event);
        assert.ok(!event.prevented, type + ' input keeps Space');
    }

    // ---- window re-activation re-dispatches focusin on the focused element: not a keyboard focus ----
    ({ app, toggles } = mkApp());
    app.onPointerDown({ target: clicked }); document.activeElement = clicked; app.onFocusIn({ target: clicked }); app.onPointerUp();
    app.onFocusIn({ target: clicked });   // focusout + focusin on the same element, nothing pending
    event = press(clicked); app.onGlobalKeydown(event);
    assert.ok(event.prevented && toggles() === 1, 'still pointer-focused after the window came back');
    app.onFocusIn({ target: other });     // a different element: the normal rule (Tab)
    event = press(other); app.onGlobalKeydown(event);
    assert.ok(!event.prevented, 'a different element is keyboard-focused');
    // Tab out of the document and back to the same element is keyboard focus, not a window re-activation
    app.onPointerDown({ target: clicked }); document.activeElement = clicked; app.onFocusIn({ target: clicked }); app.onPointerUp();
    app.onGlobalKeydown(press(clicked, { code: 'Tab', key: 'Tab', shiftKey: true }));
    app.onFocusIn({ target: clicked });
    event = press(clicked); app.onGlobalKeydown(event);
    assert.ok(!event.prevented, 'Shift+Tab out and back in: keyboard focus');
    // Ctrl+Tab (and Alt/Meta+Tab) may reach a tabless WebView without moving focus: it must not
    // turn a later window re-activation of the clicked button into keyboard focus
    app.onPointerDown({ target: clicked }); document.activeElement = clicked; app.onFocusIn({ target: clicked }); app.onPointerUp();
    app.onGlobalKeydown(press(clicked, { code: 'Tab', key: 'Tab', ctrlKey: true }));
    app.onFocusIn({ target: clicked });   // the window came back
    event = press(clicked); app.onGlobalKeydown(event);
    assert.ok(event.prevented, 'Ctrl+Tab does not forget the clicked button');
    app.onGlobalKeyup({ code: 'Space', key: ' ', preventDefault() {} });   // finish that key press
    // a press on something that takes no focus (a scrollbar) keeps the focused button's claim, so a
    // later window re-activation still counts as the mouse
    app.onPointerDown({ target: clicked }); document.activeElement = clicked; app.onFocusIn({ target: clicked }); app.onPointerUp();
    app.onPointerDown({ target: { tagName: 'HTML' } }); app.onPointerUp();   // activeElement is still the button
    app.onFocusIn({ target: clicked });
    event = press(clicked); app.onGlobalKeydown(event);
    assert.ok(event.prevented, 'a press elsewhere that leaves focus alone does not forget the button');
    // clicking away onto bare page forgets the old element, so Tab back to it is keyboard focus
    app.onPointerDown({ target: clicked }); document.activeElement = clicked; app.onFocusIn({ target: clicked }); app.onPointerUp();
    document.activeElement = document.body; app.onPointerDown({ target: { tagName: 'DIV' } }); app.onPointerUp();
    app.onFocusIn({ target: clicked });
    event = press(clicked); app.onGlobalKeydown(event);
    assert.ok(!event.prevented, 'Tab back to a button after clicking elsewhere is keyboard focus');

    // ---- a claimed Space that lost its keyup must not cancel the next, unclaimed keyup ----
    ({ app, toggles } = mkApp());
    app.onPointerDown({ target: clicked }); document.activeElement = clicked; app.onFocusIn({ target: clicked }); app.onPointerUp();
    app.onGlobalKeydown(press(clicked));   // claimed; its keyup never arrives (window switch)
    app.onFocusIn({ target: other });
    app.onGlobalKeydown(press(other));     // Tab-focused button: left to the button
    const lateUp = { code: 'Space', key: ' ', prevented: false, preventDefault() { this.prevented = true; } };
    app.onGlobalKeyup(lateUp);
    assert.ok(!lateUp.prevented, 'a stale claim is dropped by the next Space keydown');
    global.setTimeout = realTimeout;
    delete global.document;
}
// pointerup clears "pending" only after the click's own focus change, which may come in a later task
{
    global.document = { activeElement: null, body: { tagName: 'BODY' } };
    ({ app, toggles } = mkApp());
    const late = { tagName: 'BUTTON', contains(node) { return node === this; } };
    app.onPointerDown({ target: late });
    app.onPointerUp();
    app.onFocusIn({ target: late });   // focus arrives after pointerup, before the timer
    assert.strictEqual(app._pointerFocus.byPointer, true, 'a focusin right after pointerup is still the mouse');
    // (document stays: the real pointerup timer still fires after this script's last line)
}
console.log('space ok');
