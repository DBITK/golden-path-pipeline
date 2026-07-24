/*
 * Runs the browser judge against a fixture supplied on stdin and prints the
 * result as JSON. Used by tests/test_js_parity.py to prove the JavaScript port
 * and the Python original agree.
 */
'use strict';

const path = require('path');
const GoldenPath = require(path.join(__dirname, '..', 'docs', 'site', 'judge.js'));

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  input += chunk;
});
process.stdin.on('end', () => {
  const fixture = JSON.parse(input);

  const test = GoldenPath.mannWhitneyU(fixture.canary_flat, fixture.baseline_flat);
  const verdict = GoldenPath.judge(
    fixture.specs,
    fixture.canary_series,
    fixture.baseline_series,
    fixture.pass_threshold,
    fixture.marginal_threshold
  );

  process.stdout.write(
    JSON.stringify({
      mann_whitney: {
        u_statistic: test.uStatistic,
        z_score: test.zScore,
        p_value: test.pValue,
        cliffs_delta: test.cliffsDelta,
      },
      percentiles: {
        p50: GoldenPath.percentile(fixture.canary_flat, 50),
        p95: GoldenPath.percentile(fixture.canary_flat, 95),
      },
      judgment: {
        score: verdict.score,
        verdict: verdict.verdict,
        classifications: verdict.results.map((r) => ({
          name: r.name,
          classification: r.classification,
          p_value: r.pValue,
          cliffs_delta: r.cliffsDelta,
        })),
      },
    })
  );
});
