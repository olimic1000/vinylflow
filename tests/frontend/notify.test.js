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
    assert.deepStrictEqual(a.toasts.map(t => t.role), ['status', 'status', 'alert', 'alert']);
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

    // notifyError formats the error
    const g = vinylApp();
    g.notifyError('Upload failed', new Error('boom'));
    assert.strictEqual(g.toasts[0].message, 'Upload failed: boom');
    assert.strictEqual(g.toasts[0].type, 'error');

    console.log('notify ok');
})().catch(err => { console.error(err); process.exit(1); });
