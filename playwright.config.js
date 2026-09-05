// Visual regression configuration.
//
// The client has approved the current UI. `app/frontend/styles.css` is the
// visual baseline and is byte-frozen behind a checksum gate in
// tests/test_contracts.py. These tests are the second half of that protection:
// the checksum proves the stylesheet did not change, and these prove the
// rendered result did not change either - which also catches a JS-side
// regression that the checksum cannot see.
//
// Baselines are captured BEFORE any refactor begins (plan v1.2.1 Phase 0A) so
// that the Vite/TypeScript migration in later phases has something to prove
// itself against.

const { defineConfig, devices } = require('@playwright/test');

const PORT = process.env.CAPEX_VRT_PORT || 8799;

module.exports = defineConfig({
  testDir: './tests/vrt',
  // A pixel diff is a failure, not a warning. The whole point is that the
  // approved design does not drift.
  expect: {
    toHaveScreenshot: {
      maxDiffPixelRatio: 0,
      animations: 'disabled',
      caret: 'hide',
    },
  },
  // Deterministic rendering matters more than speed here.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'tests/vrt/report' }]],

  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    // Pin the colour scheme so a host-level dark-mode setting cannot alter a
    // baseline. C6_tokens.json declares dark_mode but styles.css does not
    // implement prefers-color-scheme - see CONTRACT_GAPS.md GAP-03.
    colorScheme: 'light',
    timezoneId: 'Asia/Kolkata',
    locale: 'en-IN',
  },

  // Plan section 14: baselines at 1440 / 1024 / 800 px.
  // 800px is the breakpoint below which the nav collapses to a drawer and the
  // document must still never scroll horizontally (AUD-M-004).
  projects: [
    {
      name: 'desktop-1440',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
    {
      name: 'laptop-1024',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1024, height: 768 } },
    },
    {
      name: 'tablet-800',
      use: { ...devices['Desktop Chrome'], viewport: { width: 800, height: 1024 } },
    },
  ],

  webServer: {
    // Migrate FIRST, then start. The application deliberately refuses to
    // migrate itself on boot (DEF-01: an app that does cannot be rolled back,
    // races when scaled, and turns a schema error into an outage) -- so the
    // schema is now a DEPLOY STEP, and this harness must perform it like any
    // other deployment would. Previously `run.py` created the demo database as
    // a side effect of starting, which is exactly the behaviour that was
    // removed.
    command: `python -m app.backend.migrate --fresh --seed && python app/run.py`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    // OPT-IN, and off by default even locally.
    //
    // This was `!process.env.CI`, so any local run attached to whatever was
    // already listening on the port. During Wave 4, three worktrees were
    // building concurrently and a six-minute run silently graded one stream's
    // change against a stale server owned by another -- it reported green, and
    // the green was about somebody else's code. A gate whose entire purpose is
    // to detect a difference must not be able to test the wrong build; a
    // developer's saved startup time is not worth a result that cannot be
    // trusted.
    //
    // Set CAPEX_VRT_REUSE=1 to attach to a server you started yourself, and
    // CAPEX_VRT_PORT to keep concurrent worktrees off each other's port.
    reuseExistingServer: process.env.CAPEX_VRT_REUSE === '1',
    timeout: 60_000,
    env: {
      CAPEX_PROFILE: 'local-demo',
      // An isolated database, so capturing baselines never touches the
      // developer's own demo data.
      CAPEX_DB_PATH: 'app/data/capex_vrt.db',
      PORT: String(PORT),
      X_ZOHO_CATALYST_LISTEN_PORT: String(PORT),
    },
  },
});
