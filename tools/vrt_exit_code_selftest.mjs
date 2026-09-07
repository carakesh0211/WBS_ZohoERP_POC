#!/usr/bin/env node
/*
 * Prove the VRT wrapper cannot report a failed suite as a pass.
 *
 * THE ASSERTION THAT MATTERS
 * ==========================
 * A deliberately failing Playwright test must make `tools/run_vrt.mjs` exit
 * NON-ZERO. That is the property twice violated in practice, and asserting it
 * by reading the wrapper's source would prove nothing — the failure mode was
 * never in the source anyone was looking at, it was in the shell around it.
 * So this actually runs a failing spec, end to end, and reads the status.
 *
 * AND THE ONE THAT KEEPS IT HONEST
 * --------------------------------
 * A passing spec must exit ZERO. Without this, a wrapper hard-coded to
 * `process.exit(1)` would satisfy the test above perfectly while making the
 * gate useless in the other direction.
 *
 * THE DELIBERATE FAILURE IS TEMPORARY BY CONSTRUCTION
 * ---------------------------------------------------
 * Both specs are written into a fresh temp directory, run, and removed in a
 * `finally` — so the failing test cannot survive this process even if an
 * assertion throws. It is never written under `tests/`, where the real suite
 * would collect it, and it needs no browser: `expect(1).toBe(2)` fails in the
 * runner itself, so this self-test runs in about a second and has no reason
 * to be skipped.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import process from 'node:process';

const HERE = fileURLToPath(new URL('.', import.meta.url));
const WRAPPER = join(HERE, 'run_vrt.mjs');
const REPO = resolve(HERE, '..');

// INSIDE the repository, not the OS temp directory.
//
// A spec under %TEMP% cannot resolve `@playwright/test` — Node walks up from
// the importing file looking for `node_modules`, and there is none above the
// temp directory. Playwright then reported "No tests found" and exited 1,
// which the failing check happily accepted as proof.
//
// The real suite cannot pick these up: `playwright.config.js` sets
// `testDir: './tests/vrt'`, and this directory is a sibling of `tests/`, not
// a child. The name is dot-prefixed and randomised, and `finally` removes it.
const dir = mkdtempSync(join(REPO, '.vrt-exit-selftest-'));
let failures = 0;

function runWrapper() {
  // A config of its own, so the repo's real config — its projects, its
  // webServer, its snapshot paths — is not involved. This self-test is about
  // the wrapper's exit code and nothing else.
  //
  // The spec is selected by `testDir` rather than passed as a positional
  // argument. Playwright treats a positional as a REGEX matched against test
  // paths, and an absolute Windows path is full of backslashes and a colon —
  // it matched nothing, and "No tests found" also exits 1, so the failing
  // check went green while no test had run. Third variant of the same false
  // green inside the file written to prevent it.
  const config = join(dir, 'selftest.config.mjs');
  writeFileSync(config, `export default { testDir: ${JSON.stringify(dir)}, reporter: 'line', use: {} };\n`);
  // Piped, not inherited, so this file can assert on what the runner REPORTED
  // and not merely on the number it exited with. The output is echoed below,
  // so nothing is hidden from whoever is reading the log.
  const r = spawnSync(
    process.execPath,
    [WRAPPER, '--config', config],
    { encoding: 'utf8', shell: false, cwd: HERE },
  );
  const output = `${r.stdout || ''}${r.stderr || ''}`;
  process.stdout.write(output.replace(/^/gm, '    | '));
  return { status: r.status, output };
}

function check(label, ok, detail) {
  if (ok) {
    process.stdout.write(`  ok   ${label}\n`);
    return;
  }
  process.stdout.write(`  FAIL ${label}: ${detail}\n`);
  failures += 1;
}

try {
  // ---- 1. a deliberately failing test must NOT exit 0 --------------------
  const failing = join(dir, 'deliberately-failing.spec.mjs');
  writeFileSync(failing, `import { test, expect } from '@playwright/test';
test('DELIBERATE FAILURE — proves tools/run_vrt.mjs propagates a non-zero exit', () => {
  expect(1).toBe(2);
});
`);
  process.stdout.write('\n[selftest] running a deliberately failing spec through the wrapper\n');
  const failed = runWrapper();
  // TWO CONDITIONS, BECAUSE EXIT 1 ALONE PROVED NOTHING TWICE.
  //
  // This check went green on exit 70 (the wrapper could not start Playwright)
  // and then on exit 1 ("No tests found"), in both cases without a single test
  // having been executed. Every way of failing to run is non-zero, which makes
  // a status-only assertion nearly as weak as the bug it guards. So the run
  // must ALSO have reported a failed test. The wrapper's own diagnostic codes
  // are deliberately 68-71 so they can never be mistaken for a test result.
  check('a failing suite exits 1 AND reports a failure',
        failed.status === 1 && /1 failed/.test(failed.output),
        `exit ${failed.status}, and the runner did not report "1 failed"`);

  // ---- 2. and a passing one must exit 0 ----------------------------------
  // Otherwise `process.exit(1)` would pass the check above and the gate would
  // be red forever, which is just as useless.
  rmSync(failing, { force: true });
  const passing = join(dir, 'deliberately-passing.spec.mjs');
  writeFileSync(passing, `import { test, expect } from '@playwright/test';
test('a passing test exits zero', () => {
  expect(1).toBe(1);
});
`);
  process.stdout.write('\n[selftest] running a passing spec through the wrapper\n');
  const passed = runWrapper();
  check('a passing suite exits 0 AND reports a pass',
        passed.status === 0 && /1 passed/.test(passed.output),
        `exit ${passed.status}, and the runner did not report "1 passed"`);
} finally {
  // The deliberate failure does not outlive this process, by construction.
  rmSync(dir, { recursive: true, force: true });
}

process.stdout.write(
  failures === 0
    ? '\n[selftest] PASS — the wrapper propagates the suite result in both directions\n'
    : `\n[selftest] FAIL — ${failures} check(s) failed\n`,
);
process.exit(failures === 0 ? 0 : 1);
