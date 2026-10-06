// Spec for shared/placement.js -- ranked placement matches.
//
// A brand new account has no trustworthy rating: elo.js already swings it hard
// (kFactor < 10 games => 50), but the UI shows "Bronze IV" from the first match,
// which reads as a verdict rather than a calibration. Placement hides the tier
// until the rating has settled, and shows the progress instead.
//
// The module is pure and framework-free, UMD like shared/ranks.js, and it is
// the single source of truth for both server and client.
const test = require('node:test');
const assert = require('node:assert');
const placement = require('shared/placement');
const { rankForElo } = require('shared/ranks');

test('PLACEMENT_MATCHES is the published constant, 5', () => {
  assert.strictEqual(placement.PLACEMENT_MATCHES, 5);
});

test('a brand new account is in placement, nothing played', () => {
  const s = placement.placementState(0);
  assert.strictEqual(s.inPlacement, true);
  assert.strictEqual(s.played, 0);
  assert.strictEqual(s.remaining, 5);
  assert.strictEqual(s.label, 'Placement 0/5');
});

test('progress counts played matches, not remaining ones', () => {
  const s = placement.placementState(3);
  assert.strictEqual(s.played, 3);
  assert.strictEqual(s.remaining, 2);
  assert.strictEqual(s.label, 'Placement 3/5');
});

test('the fifth match completes placement', () => {
  const s = placement.placementState(5);
  assert.strictEqual(s.inPlacement, false);
  assert.strictEqual(s.remaining, 0);
});

test('a veteran is long out of placement and never goes negative', () => {
  const s = placement.placementState(120);
  assert.strictEqual(s.inPlacement, false);
  assert.strictEqual(s.played, 120);
  assert.strictEqual(s.remaining, 0);
});

test('missing or absurd game counts are treated as a new account', () => {
  for (const bad of [undefined, null, -4, NaN, 'nope']) {
    const s = placement.placementState(bad);
    assert.strictEqual(s.inPlacement, true, String(bad));
    assert.strictEqual(s.played, 0, String(bad));
    assert.strictEqual(s.remaining, 5, String(bad));
  }
});

test('during placement the rank is withheld, the progress is shown', () => {
  const r = placement.displayRank(1450, 2);
  assert.strictEqual(r.inPlacement, true);
  assert.strictEqual(r.tier, null);
  assert.strictEqual(r.division, null);
  assert.strictEqual(r.label, 'Placement 2/5');
});

test('once placed, the rank is exactly what ranks.js says', () => {
  for (const elo of [700, 1000, 1234, 1599, 1600, 2400]) {
    const r = placement.displayRank(elo, 5);
    const expected = rankForElo(elo);
    assert.strictEqual(r.inPlacement, false, String(elo));
    assert.strictEqual(r.tier, expected.tier, String(elo));
    assert.strictEqual(r.division, expected.division, String(elo));
    assert.strictEqual(r.label, expected.label, String(elo));
  }
});

test('Master survives placement (null division is a value, not a gap)', () => {
  const r = placement.displayRank(1900, 40);
  assert.strictEqual(r.tier, 'Master');
  assert.strictEqual(r.division, null);
  assert.strictEqual(r.inPlacement, false);
});

test('placement hides the rank whatever the elo, including Master range', () => {
  const r = placement.displayRank(2000, 1);
  assert.strictEqual(r.tier, null);
  assert.strictEqual(r.label, 'Placement 1/5');
});

test('the module is pure: the same call twice gives an equal result', () => {
  assert.deepStrictEqual(placement.placementState(3), placement.placementState(3));
  assert.deepStrictEqual(placement.displayRank(1300, 7), placement.displayRank(1300, 7));
});

test('it exports through UMD like the rest of /shared', () => {
  assert.strictEqual(typeof placement.placementState, 'function');
  assert.strictEqual(typeof placement.displayRank, 'function');
});
