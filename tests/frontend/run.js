#!/usr/bin/env node
// Dependency-free runner: executes every *.test.js in this directory in its
// own node process and exits non-zero if any of them fails.
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const files = fs.readdirSync(__dirname).filter(f => f.endsWith('.test.js')).sort();
let failed = 0;
for (const f of files) {
    const r = spawnSync(process.execPath, [path.join(__dirname, f)], { encoding: 'utf8' });
    const ok = r.status === 0;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${f}`);
    if (!ok) {
        failed++;
        process.stdout.write((r.stdout || '') + (r.stderr || ''));
    }
}
console.log(`\n${files.length - failed}/${files.length} test files passed`);
process.exit(failed ? 1 : 0);
