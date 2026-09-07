#!/usr/bin/env node
/*
 * Run Playwright and exit with EXACTLY the code Playwright exited with.
 *
 * WHY THIS FILE EXISTS
 * ====================
 * Twice in one session a VRT run was reported green when it had failed.
 *
 *   1. `npx playwright test --config tests/vrt/playwright.config.js` — the
 *      config is at the repo root, not there. Playwright printed a resolution
 *      error and did nothing. The reported exit code was 0.
 *   2. A later run failed 36 tests. The reported exit code was 0 again,
 *      because the command was `npx playwright test > out.txt 2>&1; echo "VRT
 *      EXIT: $?"; tail -6 out.txt` — a compound whose status is the STATUS OF
 *      `tail`, which always succeeds.
 *
 * Both are the same defect wearing different clothes: the thing that reports
 * success is not the thing that ran the tests. A gate that cannot go red is
 * not a gate, and a suite of 1000 visual assertions guarding a client-approved
 * UI is exactly the wrong place to have one.
 *
 * WHAT THIS FILE IS NOT ALLOWED TO DO
 * -----------------------------------
 * No shell. No pipeline. No `tee`. No `$?`. No try/catch that swallows a
 * status. No "if it looks like it worked" parsing of stdout. The child's
 * status is passed through untouched, and every path that is NOT a clean
 * child exit becomes a non-zero exit rather than a silent zero.
 *
 * `stdio: 'inherit'` is load-bearing: the child writes straight to this
 * process's streams, so there is no pipe whose own exit status could be
 * mistaken for the suite's, and nothing buffers.
 *
 * Proven by `tools/vrt_exit_code_selftest.mjs`, which runs this wrapper over a
 * deliberately failing spec and asserts a non-zero exit — and over a passing
 * one, so a wrapper that merely always failed could not pass it either.
 */
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import process from 'node:process';

const args = process.argv.slice(2);

// Run Playwright's CLI **as a JavaScript file under this same node**, not via
// `npx`.
//
// The first draft here spawned `npx.cmd` with `shell: false`, which is an
// EINVAL on Windows under current Node — spawning a `.cmd` requires a shell,
// and a shell is precisely what this file must not have. The self-test caught
// it on the first run, and caught it in the instructive way: the
// failing-spec check went green on exit 70 ("could not start playwright")
// while nothing had been executed at all. That is the same false green this
// wrapper exists to prevent, reproduced inside the wrapper itself.
//
// Resolving the module and handing it to `process.execPath` removes the shell,
// the `.cmd` shim and `npx`'s own exit-code layer in one move.
// Resolved via the package's own `package.json`, not a `playwright/cli`
// subpath: modern Playwright declares an `exports` map that does not publish
// `./cli`, so `require.resolve('playwright/cli')` throws ERR_PACKAGE_PATH_NOT_
// EXPORTED. `./package.json` is exported by every published version, and
// `cli.js` sits beside it. The self-test caught this too — both of its checks
// went red on exit 71 rather than one of them going green by accident, which
// is the behaviour the tightened "exit 1 specifically" assertion buys.
const require = createRequire(import.meta.url);
let cli;
try {
  cli = join(dirname(require.resolve('playwright/package.json')), 'cli.js');
} catch (err) {
  console.error('run_vrt: cannot resolve the playwright package: ' + err.message);
  process.exit(71);
}
if (!existsSync(cli)) {
  console.error('run_vrt: resolved the playwright package but ' + cli + ' is missing');
  process.exit(71);
}

const result = spawnSync(
  process.execPath,
  [cli, 'test', ...args],
  { stdio: 'inherit', shell: false },
);

if (result.error) {
  // Could not even start Playwright. This is the case that produced the first
  // false green, so it is a hard failure and says what happened.
  process.stderr.write(`run_vrt: could not start playwright: ${result.error.message}\n`);
  process.exit(70);
}

if (result.signal) {
  // Killed. Not a pass, and `status` is null here, so a naive
  // `process.exit(result.status)` would exit 0 on a killed suite.
  process.stderr.write(`run_vrt: playwright was killed by ${result.signal}\n`);
  process.exit(69);
}

if (typeof result.status !== 'number') {
  process.stderr.write('run_vrt: playwright produced no exit status\n');
  process.exit(68);
}

process.exit(result.status);
