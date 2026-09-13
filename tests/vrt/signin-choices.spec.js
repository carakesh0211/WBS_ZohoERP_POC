// tests/vrt/signin-choices.spec.js
//
// STREAM D — two sign-in choices, forgot password, and the hash-driven
// reset form.
//
// The Playwright webServer here runs CAPEX_PROFILE=local-demo against
// SQLite only (see playwright.config.js) — no CAPEX_DB_URL/CAPEX_DB_HOST, so
// `get_database()` in app/backend/pg/engine.py returns None and
// identity.py's `/api/auth/providers` therefore reports `oidc.enabled:
// false` and `local.forgot_password: false` for real against this
// webServer. Every route this file exercises is stubbed with page.route()
// so the ENABLED path is tested without a PostgreSQL-backed process — the
// convention integration-actions.spec.js already follows for its own
// PG-mounted routes.
//
// No screenshot assertion and no baseline PNG: this spec is behavioural.

const { test, expect } = require('@playwright/test');

async function routeJson(page, pattern, body, status = 200) {
  await page.route(pattern, (route) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  }));
}

const PROVIDERS_BOTH_ENABLED = {
  local: { enabled: true, label: 'Sign in with WBS account', forgot_password: true },
  oidc: {
    provider: 'zoho', enabled: true, label: 'Continue with Zoho',
    allowed_email_domains: ['athagroup.example'],
  },
};

const PROVIDERS_LOCAL_ONLY = {
  local: { enabled: true, label: 'Sign in with WBS account', forgot_password: false },
  oidc: { provider: 'zoho', enabled: false, label: 'Continue with Zoho', allowed_email_domains: [] },
};

test.describe('Stream D — sign-in providers', () => {
  test('Continue with Zoho and Forgot password? render when providers report them enabled', async ({ page }) => {
    await routeJson(page, '**/api/auth/providers', PROVIDERS_BOTH_ENABLED);
    await page.goto('/');
    await page.waitForSelector('#loginForm', { state: 'visible' });

    await expect(page.locator('#ssoBtn')).toBeVisible();
    await expect(page.locator('#ssoDivider')).toBeVisible();
    await expect(page.locator('#forgotLink')).toBeVisible();
  });

  test('neither control renders when providers report them disabled', async ({ page }) => {
    await routeJson(page, '**/api/auth/providers', PROVIDERS_LOCAL_ONLY);
    await page.goto('/');
    await page.waitForSelector('#loginForm', { state: 'visible' });

    await expect(page.locator('#ssoBtn')).toBeHidden();
    await expect(page.locator('#ssoDivider')).toBeHidden();
    await expect(page.locator('#forgotLink')).toBeHidden();
  });

  test('Continue with Zoho navigates the browser to the authorization_url POST /api/auth/oidc/start returns', async ({ page }) => {
    await routeJson(page, '**/api/auth/providers', PROVIDERS_BOTH_ENABLED);
    await page.route('**/api/auth/oidc/start', (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ authorization_url: 'https://accounts.zoho.in/oauth/v2/auth?fake=1', provider: 'zoho' }),
    }));
    // Stubbed rather than let a real request reach accounts.zoho.in: the
    // point being proven is that this app SET location.href to the server's
    // authorization_url, not that Zoho's own login page renders.
    await page.route('https://accounts.zoho.in/**', (route) => route.fulfill({
      status: 200, contentType: 'text/html', body: '<html><body>stub zoho login</body></html>',
    }));
    await page.goto('/');
    await page.waitForSelector('#loginForm', { state: 'visible' });

    await page.click('#ssoBtn');
    await page.waitForURL(/accounts\.zoho\.in/, { timeout: 10_000 });
    expect(page.url()).toContain('accounts.zoho.in');
  });
});

test.describe('Stream D — forgot password', () => {
  test('the response is always the server\'s own generic message, never a confirmation the account exists', async ({ page }) => {
    await routeJson(page, '**/api/auth/providers', PROVIDERS_BOTH_ENABLED);
    let sentUserId = null;
    await page.route('**/api/auth/forgot', (route) => {
      sentUserId = JSON.parse(route.request().postData()).user_id;
      return route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ message: 'If that account exists, instructions were sent to it.' }),
      });
    });
    await page.goto('/');
    await page.waitForSelector('#loginForm', { state: 'visible' });

    await page.click('#forgotLink');
    await expect(page.locator('#forgotForm')).toBeVisible();
    await expect(page.locator('#loginForm')).toBeHidden();

    await page.fill('#forgotUserId', 'U-NOBODY');
    await page.click('#forgotSubmitBtn');

    await expect(page.locator('#forgotMsg')).toContainText('If that account exists, instructions were sent to it.');
    expect(sentUserId).toBe('U-NOBODY');

    // The same message shape for a real user id: this screen has no way to
    // tell the two apart, by design.
    await page.fill('#forgotUserId', 'U-REQ');
    await page.click('#forgotSubmitBtn');
    await expect(page.locator('#forgotMsg')).toContainText('If that account exists, instructions were sent to it.');

    await page.click('#forgotBackBtn');
    await expect(page.locator('#loginForm')).toBeVisible();
    await expect(page.locator('#forgotForm')).toBeHidden();
  });
});

test.describe('Stream D — sso_error rendering', () => {
  const CASES = [
    ['OIDC_NOT_LINKED', 'Your Zoho identity is not linked to a WBS account'],
    ['OIDC_DOMAIN_REFUSED', 'email domain is not permitted'],
    ['OIDC_STATE_INVALID', 'could not be verified'],
    ['OIDC_BREAK_GLASS_REFUSED', 'break-glass account never signs in through Zoho'],
    ['OIDC_PROVIDER_ERROR', 'Zoho reported an error'],
    ['DATABASE_NOT_CONFIGURED', 'not available on this deployment'],
    ['SOME_FUTURE_CODE', 'SOME_FUTURE_CODE'],
  ];

  for (const [code, expectedText] of CASES) {
    test(`#sso_error=${code} renders its human sentence on the sign-in card`, async ({ page }) => {
      await routeJson(page, '**/api/auth/providers', PROVIDERS_BOTH_ENABLED);
      await page.goto(`/#sso_error=${code}`);
      await page.waitForSelector('#loginForm', { state: 'visible' });
      await expect(page.locator('#ssoErrorMsg')).toContainText(expectedText);
      // The hash is cleared once handled, so a reload does not re-show it.
      expect(new URL(page.url()).hash).toBe('');
    });
  }
});

test.describe('Stream D — password reset', () => {
  test('a #reset?token=... URL shows the reset form, not the ordinary sign-in form', async ({ page }) => {
    await routeJson(page, '**/api/auth/providers', PROVIDERS_BOTH_ENABLED);
    await page.goto('/#reset?token=abc123');
    await page.waitForSelector('#resetForm', { state: 'visible' });
    await expect(page.locator('#loginForm')).toBeHidden();
    await expect(page.locator('#forgotForm')).toBeHidden();
  });

  test('mismatched passwords are refused client-side without a network call', async ({ page }) => {
    await routeJson(page, '**/api/auth/providers', PROVIDERS_BOTH_ENABLED);
    let resetCalled = false;
    await page.route('**/api/auth/reset', (route) => { resetCalled = true; return route.continue(); });
    await page.goto('/#reset?token=abc123');
    await page.waitForSelector('#resetForm', { state: 'visible' });

    await page.fill('#resetNewPass', 'NewPassw0rd!');
    await page.fill('#resetConfirmPass', 'DoesNotMatch!');
    await page.click('#resetSubmitBtn');

    await expect(page.locator('#resetMsg')).toContainText('do not match');
    expect(resetCalled).toBe(false);
  });

  test('a successful reset shows the message and returns to sign-in', async ({ page }) => {
    await routeJson(page, '**/api/auth/providers', PROVIDERS_BOTH_ENABLED);
    await page.route('**/api/auth/reset', (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, sessions_revoked: 2, message: 'Your password was changed. Sign in with the new one.' }),
    }));
    await page.goto('/#reset?token=abc123');
    await page.waitForSelector('#resetForm', { state: 'visible' });

    await page.fill('#resetNewPass', 'NewPassw0rd!');
    await page.fill('#resetConfirmPass', 'NewPassw0rd!');
    await page.click('#resetSubmitBtn');

    await expect(page.locator('#loginForm')).toBeVisible();
    await expect(page.locator('#loginMsg')).toContainText('Your password was changed. Sign in with the new one.');
  });

  test('a 400/422/429 refusal shows the RFC-7807 detail.message on the reset form', async ({ page }) => {
    await routeJson(page, '**/api/auth/providers', PROVIDERS_BOTH_ENABLED);
    await page.route('**/api/auth/reset', (route) => route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({ detail: { code: 'RESET_TOKEN_INVALID', message: 'This reset link has expired or was already used.' } }),
    }));
    await page.goto('/#reset?token=abc123');
    await page.waitForSelector('#resetForm', { state: 'visible' });

    await page.fill('#resetNewPass', 'NewPassw0rd!');
    await page.fill('#resetConfirmPass', 'NewPassw0rd!');
    await page.click('#resetSubmitBtn');

    await expect(page.locator('#resetMsg')).toContainText('This reset link has expired or was already used.');
    // Still on the reset form: a refusal is not treated as success.
    await expect(page.locator('#resetForm')).toBeVisible();
  });
});
