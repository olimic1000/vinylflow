// scalePeaksToRecording reads the 99.9th percentile from a histogram instead of sorting a copy.
// It must agree with the exact sort-based scale (the previous implementation) within one bin.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
// The accuracy promised, fixed here so shrinking the histogram cannot loosen its own test
const BIN_WIDTH_DIVISOR = 65536;
const { scalePeaksToRecording, PEAK_SCALE_PERCENTILE, PEAK_SCALE_FLOOR_FRACTION } =
    new Function(src + ';return {scalePeaksToRecording, PEAK_SCALE_PERCENTILE, PEAK_SCALE_FLOOR_FRACTION};')();

/** The previous implementation's reference: sort |peaks|, take the percentile, floor it at loudest / 8 */
function exactReference(peaks) {
    const magnitudes = Float64Array.from(peaks, peak => Math.abs(peak));
    magnitudes.sort();
    const percentile = magnitudes[Math.ceil(PEAK_SCALE_PERCENTILE * magnitudes.length) - 1];
    return Math.max(percentile, magnitudes[magnitudes.length - 1] * PEAK_SCALE_FLOOR_FRACTION);
}
/** The reference the scaled output implies: the loudest input peak maps to loudest / reference */
function impliedReference(peaks, scaled) {
    let index = 0;
    for (let i = 1; i < peaks.length; i++) if (Math.abs(peaks[i]) > Math.abs(peaks[index])) index = i;
    return Math.abs(peaks[index]) / Math.abs(scaled[index]);
}

let seed = 12345;
const random = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
const signed = magnitude => (random() < 0.5 ? -magnitude : magnitude);
const cases = {
    'uniform': Array.from({ length: 50000 }, () => signed(random() * 0.6)),
    'gaussian-ish music': Array.from({ length: 50000 }, () => signed(Math.abs(random() + random() + random() - 1.5) * 0.5)),
    'a few huge outliers': Array.from({ length: 50000 }, (_, i) => signed(i % 9000 === 0 ? 1 : 0.05 + random() * 0.1)),
    'half a percent loud passages': Array.from({ length: 20000 }, (_, i) => signed(i % 200 === 0 ? 0.9 : 0.1 + random() * 0.05)),
    'mostly digital silence': Array.from({ length: 20000 }, (_, i) => (i % 2000 === 1 ? 0.8 : i === 7 ? 0.05 : 0)),
    'constant': new Array(30000).fill(0.25),
    'tiny values': Array.from({ length: 5000 }, () => random() * 1e-6),
    'two values': [0.3, -0.9],
    'one value': [0.4],
    'three values': [0.1, 0.2, 0.05],
};
for (const [name, peaks] of Object.entries(cases)) {
    const exact = exactReference(peaks);
    const scaled = scalePeaksToRecording(peaks);
    assert.strictEqual(scaled.length, peaks.length, name);
    const loudest = Math.max(...peaks.map(Math.abs));
    const implied = impliedReference(peaks, scaled);
    const clipped = Math.abs(peaks[peaks.map(Math.abs).indexOf(loudest)]) / exact > 1;
    // The histogram's reference is never below the exact one and at most one bin above it
    // (the loudest peak is only comparable while it is not clipped)
    if (!clipped) {
        assert.ok(implied >= exact * (1 - 1e-12), `${name}: reference ${implied} is not below the exact ${exact}`);
        assert.ok(implied - exact <= loudest / BIN_WIDTH_DIVISOR * (1 + 1e-9), `${name}: reference ${implied} too far above the exact ${exact}`);
    }
    // Every output sample agrees with the exact scaling within the same bin tolerance (relative to full height)
    const tolerance = (loudest / BIN_WIDTH_DIVISOR) / exact + 1e-9;
    for (let i = 0; i < peaks.length; i += 7) {
        const expected = Math.max(-1, Math.min(1, peaks[i] / exact));
        assert.ok(Math.abs(scaled[i] - expected) <= tolerance * Math.abs(peaks[i] / exact) + 1e-9, `${name}[${i}]: ${scaled[i]} vs ${expected}`);
    }
}
// Degenerate input
assert.deepStrictEqual(scalePeaksToRecording([]), []);
assert.deepStrictEqual(scalePeaksToRecording([0, 0, 0]), [0, 0, 0], 'digital silence stays flat, never NaN');
// The percentile must not come from sorting a copy of the peaks (that took ~370 ms for 4 h).  Any sort
// of a typed array or an array during the scaling fails here, so a return to the sorted copy is caught
// even though its result is the exact reference.
const TypedArrayPrototype = Object.getPrototypeOf(Float64Array.prototype);
const realTypedSort = TypedArrayPrototype.sort;
const realArraySort = Array.prototype.sort;
const refuseSort = () => { throw new Error('scalePeaksToRecording sorted the peaks'); };
TypedArrayPrototype.sort = refuseSort;
Array.prototype.sort = refuseSort;
try {
    scalePeaksToRecording(Float64Array.from({ length: 1000 }, () => signed(random() * 0.4)));
    scalePeaksToRecording(Array.from({ length: 1000 }, () => signed(random() * 0.4)));
} finally {
    TypedArrayPrototype.sort = realTypedSort;
    Array.prototype.sort = realArraySort;
}
// A recording of 4 h (about 1.44 M peaks) is handled without a sorted copy: bounded work, and it is quick
const fourHours = Float64Array.from({ length: 1440000 }, () => signed(random() * 0.4));
fourHours[3] = 0.99;
const started = Date.now();
const scaledFourHours = scalePeaksToRecording(fourHours);
assert.strictEqual(scaledFourHours.length, fourHours.length);
assert.ok(Date.now() - started < 2000, 'scaling 4 h of peaks takes well under two seconds');
console.log('peak scale ok');
