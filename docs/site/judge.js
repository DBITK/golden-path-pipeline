/*
 * A faithful JavaScript port of goldenpath/canary/judge.py and stats.py.
 *
 * This exists so the canary judge can be explored interactively in a browser
 * without a server. It is a port, not a reimplementation: the same
 * Mann-Whitney U test, the same tie and continuity corrections, the same
 * Cliff's delta, the same tolerance gate, the same weighted score.
 *
 * Ports drift. `tests/test_js_parity.py` runs this file under Node against
 * fixed inputs and asserts the numbers match the Python judge -- exactly for
 * the rank statistics, and to 1e-6 for p-values, which is the floor set by the
 * error-function approximation below. If the two ever diverge, CI says so.
 */
(function (root) {
  'use strict';

  // ---------------------------------------------------------------- stats --

  function fractionalRanks(values) {
    const order = values.map((v, i) => i).sort((a, b) => values[a] - values[b]);
    const ranks = new Array(values.length).fill(0);
    let i = 0;
    while (i < order.length) {
      let j = i;
      while (j + 1 < order.length && values[order[j + 1]] === values[order[i]]) j++;
      const midpoint = (i + j + 2) / 2;
      for (let k = i; k <= j; k++) ranks[order[k]] = midpoint;
      i = j + 1;
    }
    return ranks;
  }

  function tieCorrectionTerm(values) {
    const ordered = values.slice().sort((a, b) => a - b);
    let total = 0;
    let i = 0;
    while (i < ordered.length) {
      let j = i;
      while (j + 1 < ordered.length && ordered[j + 1] === ordered[i]) j++;
      const t = j - i + 1;
      total += t * t * t - t;
      i = j + 1;
    }
    return total;
  }

  // Abramowitz & Stegun 7.1.26; accurate to ~1.5e-7, well inside the
  // tolerance the parity test enforces on the resulting p-values.
  function erf(x) {
    const sign = x < 0 ? -1 : 1;
    const ax = Math.abs(x);
    const t = 1 / (1 + 0.3275911 * ax);
    const y =
      1 -
      ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t +
        0.254829592) *
        t *
        Math.exp(-ax * ax);
    return sign * y;
  }

  function normalCdf(z) {
    return 0.5 * (1 + erf(z / Math.SQRT2));
  }

  function percentile(values, q) {
    if (!values.length) throw new Error('percentile of an empty sample is undefined');
    const ordered = values.slice().sort((a, b) => a - b);
    if (ordered.length === 1) return ordered[0];
    const position = (ordered.length - 1) * (q / 100);
    const lower = Math.floor(position);
    const upper = Math.ceil(position);
    if (lower === upper) return ordered[position];
    return ordered[lower] * (1 - (position - lower)) + ordered[upper] * (position - lower);
  }

  function mannWhitneyU(canary, baseline) {
    const n1 = canary.length;
    const n2 = baseline.length;
    if (!n1 || !n2) throw new Error('Mann-Whitney U requires two non-empty samples');

    const combined = canary.concat(baseline);
    const ranks = fractionalRanks(combined);

    let rankSum = 0;
    for (let i = 0; i < n1; i++) rankSum += ranks[i];
    const u = rankSum - (n1 * (n1 + 1)) / 2;
    const cliffsDelta = (2 * u) / (n1 * n2) - 1;

    const total = n1 + n2;
    const meanU = (n1 * n2) / 2;
    const tieTerm = tieCorrectionTerm(combined);
    const variance =
      total < 2 ? 0 : ((n1 * n2) / 12) * (total + 1 - tieTerm / (total * (total - 1)));

    if (variance <= 0) {
      return { uStatistic: u, zScore: 0, pValue: 1, cliffsDelta: cliffsDelta };
    }

    const sigma = Math.sqrt(variance);
    let numerator = Math.abs(u - meanU) - 0.5;
    if (numerator < 0) numerator = 0;
    let z = numerator / sigma;
    if (u < meanU) z = -z;

    let p = 2 * (1 - normalCdf(Math.abs(z)));
    p = Math.min(1, Math.max(0, p));
    return { uStatistic: u, zScore: z, pValue: p, cliffsDelta: cliffsDelta };
  }

  // ---------------------------------------------------------------- judge --

  const Classification = {
    PASS: 'Pass',
    HIGH: 'High',
    LOW: 'Low',
    NODATA: 'Nodata',
  };

  function isFailure(classification) {
    return classification === Classification.HIGH || classification === Classification.LOW;
  }

  function defaultSpec(spec) {
    return Object.assign(
      {
        direction: 'increase',
        weight: 1,
        critical: false,
        tolerance: 0.2,
        significance: 0.05,
        minSamples: 20,
      },
      spec
    );
  }

  function classifyMetric(rawSpec, canary, baseline) {
    const spec = defaultSpec(rawSpec);
    const result = {
      name: spec.name,
      weight: spec.weight,
      critical: spec.critical,
      canaryCount: canary.length,
      baselineCount: baseline.length,
      classification: Classification.NODATA,
      reason: '',
      canaryMedian: null,
      baselineMedian: null,
      canaryP95: null,
      baselineP95: null,
      pValue: null,
      cliffsDelta: null,
    };

    if (canary.length < spec.minSamples || baseline.length < spec.minSamples) {
      result.reason =
        'insufficient samples (canary=' +
        canary.length +
        ', baseline=' +
        baseline.length +
        ', required=' +
        spec.minSamples +
        ')';
      return result;
    }

    result.canaryMedian = percentile(canary, 50);
    result.baselineMedian = percentile(baseline, 50);
    result.canaryP95 = percentile(canary, 95);
    result.baselineP95 = percentile(baseline, 95);

    const test = mannWhitneyU(canary, baseline);
    result.pValue = test.pValue;
    result.cliffsDelta = test.cliffsDelta;

    const significant = test.pValue < spec.significance;
    const material = Math.abs(test.cliffsDelta) >= spec.tolerance;

    if (!significant) {
      result.classification = Classification.PASS;
      result.reason = 'no significant difference (p=' + test.pValue.toFixed(4) + ')';
      return result;
    }
    if (!material) {
      result.classification = Classification.PASS;
      result.reason =
        'significant but within tolerance (delta=' +
        test.cliffsDelta.toFixed(3) +
        ', tolerance=' +
        spec.tolerance +
        ')';
      return result;
    }

    const canaryHigher = test.cliffsDelta > 0;
    const regression =
      (spec.direction === 'increase' && canaryHigher) ||
      (spec.direction === 'decrease' && !canaryHigher) ||
      spec.direction === 'either';

    if (!regression) {
      result.classification = Classification.PASS;
      result.reason =
        'moved in the improving direction (delta=' + test.cliffsDelta.toFixed(3) + ')';
      return result;
    }

    result.classification = canaryHigher ? Classification.HIGH : Classification.LOW;
    result.reason =
      'canary ' +
      (canaryHigher ? 'higher' : 'lower') +
      ' than baseline: median ' +
      result.canaryMedian.toFixed(2) +
      ' vs ' +
      result.baselineMedian.toFixed(2) +
      ' (p=' +
      test.pValue.toFixed(4) +
      ', delta=' +
      (test.cliffsDelta >= 0 ? '+' : '') +
      test.cliffsDelta.toFixed(3) +
      ')';
    return result;
  }

  function judge(specs, canarySamples, baselineSamples, passThreshold, marginalThreshold) {
    if (!specs || !specs.length) throw new Error('canary judgment requires at least one metric');
    const pass = passThreshold === undefined ? 95 : passThreshold;
    const marginal = marginalThreshold === undefined ? 75 : marginalThreshold;

    const results = specs.map((spec) =>
      classifyMetric(spec, canarySamples[spec.name] || [], baselineSamples[spec.name] || [])
    );

    const scored = results.filter((r) => r.classification !== Classification.NODATA);
    if (!scored.length) {
      return {
        score: 0,
        verdict: 'FAIL',
        results: results,
        passThreshold: pass,
        marginalThreshold: marginal,
        summary: 'No metric produced enough data to judge; refusing to promote.',
      };
    }

    const totalWeight = scored.reduce((sum, r) => sum + r.weight, 0);
    const passingWeight = scored
      .filter((r) => !isFailure(r.classification))
      .reduce((sum, r) => sum + r.weight, 0);
    const score = totalWeight ? (100 * passingWeight) / totalWeight : 0;

    const criticalFailures = results.filter((r) => isFailure(r.classification) && r.critical);

    let verdict;
    let summary;
    if (criticalFailures.length) {
      verdict = 'FAIL';
      summary =
        'Critical metric regression: ' +
        criticalFailures.map((r) => r.name).join(', ') +
        '. Score ' +
        score.toFixed(1) +
        ' is not consulted.';
    } else if (score >= pass) {
      verdict = 'PASS';
      summary = 'Score ' + score.toFixed(1) + ' >= pass threshold ' + pass + '.';
    } else if (score >= marginal) {
      verdict = 'MARGINAL';
      summary =
        'Score ' +
        score.toFixed(1) +
        ' sits between marginal (' +
        marginal +
        ') and pass (' +
        pass +
        ') thresholds; human judgment required.';
    } else {
      verdict = 'FAIL';
      summary = 'Score ' + score.toFixed(1) + ' < marginal threshold ' + marginal + '.';
    }

    const nodata = results.filter((r) => r.classification === Classification.NODATA);
    if (nodata.length) {
      summary += ' Excluded ' + nodata.length + ' metric(s) with insufficient data.';
    }

    return {
      score: score,
      verdict: verdict,
      results: results,
      passThreshold: pass,
      marginalThreshold: marginal,
      summary: summary,
    };
  }

  // ------------------------------------------------------------ simulation --

  /* Deterministic RNG so a given set of slider positions always produces the
     same verdict. A simulator that gives different answers on every click
     teaches nothing. */
  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a |= 0;
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function gaussPair(rng) {
    let u = 0;
    let v = 0;
    while (u === 0) u = rng();
    while (v === 0) v = rng();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }

  /* Mirrors services/paved-road-demo/app.py: lognormal latency, Bernoulli
     errors, saturation tracking configured latency. */
  function simulate(options) {
    const rng = mulberry32(options.seed);
    const windows = options.windows;
    const perWindow = options.requestsPerWindow;

    const latency = [];
    const errorRate = [];
    const throughput = [];
    const saturation = [];

    for (let w = 0; w < windows; w++) {
      let errors = 0;
      let windowLatency = 0;
      for (let r = 0; r < perWindow; r++) {
        const tail = Math.exp(gaussPair(rng) * 0.45);
        const ms = Math.max(0.5, options.latencyMs * tail + rng() * 6);
        latency.push(ms);
        windowLatency += ms;
        if (rng() < options.errorRate) errors++;
      }
      errorRate.push((100 * errors) / perWindow);
      const meanMs = windowLatency / perWindow;
      throughput.push((perWindow * 1000) / (meanMs * perWindow / options.concurrency));
      saturation.push(Math.min(99, Math.max(1, options.latencyMs * 1.4 + (rng() * 6 - 3))));
    }

    const series = {};
    series.request_latency_ms = latency;
    series.error_rate_pct = errorRate;
    series.throughput_rps = throughput;
    series.cpu_saturation_pct = saturation;
    return series;
  }

  const api = {
    fractionalRanks: fractionalRanks,
    normalCdf: normalCdf,
    percentile: percentile,
    mannWhitneyU: mannWhitneyU,
    classifyMetric: classifyMetric,
    judge: judge,
    simulate: simulate,
    Classification: Classification,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    root.GoldenPath = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
