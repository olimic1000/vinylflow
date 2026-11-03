// Toast system: stacking, auto-dismiss vs sticky, hover pause, actions, onClose reasons.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    const a = vinylApp();

    // stacking + types
    const i1 = a.notify('one', { type: 'info' });
    const i2 = a.notify('two', { type: 'success' });
    const w = a.notify('careful', { type: 'warning' });
    const e = a.notify('broken', { type: 'error' });
    assert.deepStrictEqual(a.toasts.map(t => t.message), ['one', 'two', 'careful', 'broken']);
    assert.deepStrictEqual(a.toasts.map(t => t.type), ['info', 'success', 'warning', 'error']);
    // Announcement lives on the persistent .toast-stack live region (see the markup check below), not per toast
    assert.ok(a.toasts.every(t => !('role' in t)));
    assert.strictEqual(new Set([i1, i2, w, e]).size, 4);
    // default timers: info/success armed (5 s), warning/error sticky
    assert.ok(a._toastTimers[i1] && a._toastTimers[i1].remaining === 5000);
    assert.ok(a._toastTimers[i2]);
    assert.ok(!a._toastTimers[w] && !a._toastTimers[e]);

    // unknown type falls back to info
    const u = a.notify('x', { type: 'bogus' });
    assert.strictEqual(a.toasts.find(t => t.id === u).type, 'info');
    a.dismissToast(u);

    // identical message refreshes instead of stacking
    const before = a.toasts.length;
    assert.strictEqual(a.notify('careful', { type: 'warning' }), w);
    assert.strictEqual(a.toasts.length, before);

    // manual dismiss
    a.dismissToast(e);
    assert.ok(!a.toasts.some(t => t.id === e));
    a.dismissToast(9999);   // unknown id is a no-op

    // auto-dismiss after the timeout; sticky stays
    const b = vinylApp();
    b.notify('quick', { type: 'info', timeout: 30 });
    const sticky = b.notify('stays', { type: 'error' });
    const timed = b.notify('warn timed', { type: 'warning', timeout: 30 });
    await sleep(80);
    assert.deepStrictEqual(b.toasts.map(t => t.message), ['stays']);
    assert.ok(b.toasts[0].id === sticky && timed);

    // hover pauses, leave resumes
    const c = vinylApp();
    const id = c.notify('hover me', { type: 'info', timeout: 60 });
    c.pauseToast(id);
    await sleep(150);
    assert.ok(c.toasts.some(t => t.id === id), 'paused toast must stay');
    c.resumeToast(id);
    await sleep(700);   // remaining time is clamped to >= 500 ms
    assert.ok(!c.toasts.some(t => t.id === id), 'resumed toast dismisses');

    // action + onClose reasons
    const d = vinylApp();
    const reasons = [];
    let acted = 0;
    const aid = d.notify('undoable', { action: { label: 'Undo', fn: () => acted++ }, onClose: r => reasons.push(r) });
    d.runToastAction(aid);
    assert.strictEqual(acted, 1);
    assert.deepStrictEqual(reasons, ['action']);
    assert.strictEqual(d.toasts.length, 0);
    const bid = d.notify('closed', { onClose: r => reasons.push(r) });
    d.dismissToast(bid);
    d.notify('timed', { timeout: 20, onClose: r => reasons.push(r) });
    await sleep(60);
    assert.deepStrictEqual(reasons, ['action', 'dismiss', 'timeout']);

    // stack is capped
    const f = vinylApp();
    for (let n = 0; n < 8; n++) f.notify('m' + n, { type: 'error' });
    assert.strictEqual(f.toasts.length, 5);
    assert.strictEqual(f.toasts[0].message, 'm3');

    // eviction: the oldest toast without an action makes room, with reason 'evict'; actions are never evicted
    const h = vinylApp();
    const closed = [];
    const undoId = h.notify('undo me', { action: { label: 'Undo', fn() {} }, onClose: r => closed.push(['undo', r]) });
    h.notify('first', { onClose: r => closed.push(['first', r]) });
    for (let n = 0; n < 4; n++) h.notify('info ' + n);
    assert.strictEqual(h.toasts.length, 5);
    assert.ok(h.toasts.some(t => t.id === undoId));
    assert.deepStrictEqual(closed, [['first', 'evict']]);
    const k = vinylApp();
    for (let n = 0; n < 6; n++) k.notify('undo ' + n, { action: { label: 'Undo', fn() {} } });
    const err = k.notify('later error', { type: 'error' });
    assert.strictEqual(k.toasts.length, 7, 'every toast keeps its Undo; the new error is shown, not evicted');
    assert.ok(k.toasts.some(t => t.id === err));
    // six plain info toasts: capped at 5, oldest gone
    const m = vinylApp();
    for (let n = 0; n < 6; n++) m.notify('info ' + n);
    assert.deepStrictEqual(m.toasts.map(t => t.message), ['info 1', 'info 2', 'info 3', 'info 4', 'info 5']);

    // markup: one persistent polite live region; no nested live regions per toast (review #86)
    const html = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'index.html'), 'utf8');
    const stack = html.match(/<div class="toast-stack"[^>]*>/)[0];
    assert.ok(/aria-live="polite"/.test(stack), '.toast-stack is the polite live region');
    assert.ok(!/aria-relevant/.test(stack), 'default aria-relevant (additions text): x-text fills the clone after insertion');
    const toastEl = html.match(/<template x-for="toast in toasts"[^>]*>\s*<div class="toast"[^>]*>/)[0];
    assert.ok(!/role=|aria-live|aria-atomic/.test(toastEl), 'a toast carries no role or live region of its own');
    const toastBody = html.slice(html.indexOf(toastEl), html.indexOf('</template>', html.indexOf(toastEl)));
    assert.ok(/class="toast-icon" aria-hidden="true"/.test(toastBody), 'type icon, hidden from screen readers');
    assert.ok(/class="sr-only" x-text="toast.type \+ ': '"/.test(toastBody), 'type word read before the message');

    // notifyError formats the error
    const g = vinylApp();
    g.notifyError('Upload failed', new Error('boom'));
    assert.strictEqual(g.toasts[0].message, 'Upload failed: boom');
    assert.strictEqual(g.toasts[0].type, 'error');

    console.log('notify ok');
})().catch(err => { console.error(err); process.exit(1); });
