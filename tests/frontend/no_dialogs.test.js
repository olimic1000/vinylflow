// The UI must never open blocking browser dialogs: use notify() toasts and Undo instead.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
// Strip comments so prose mentioning the old dialogs doesn't trip the check.
const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const hits = code.split('\n')
    .map((line, i) => [i + 1, line])
    .filter(([, line]) => /(^|[^\w.$])(window\.)?(alert|confirm|prompt)\s*\(/.test(line));
assert.deepStrictEqual(hits, [], 'blocking dialog calls found: ' + JSON.stringify(hits));
console.log('no dialogs ok');
