// tests/vrt/notifications.spec.js
//
// STREAM E — notification preferences/history and the Administrator outbox.
//
// app/backend/api/notifications.py and app/backend/pg/notifications.py are
// PostgreSQL-backed, and the Playwright webServer here runs
// CAPEX_PROFILE=local-demo against SQLite only (playwright.config.js), so
// every route this spec touches is stubbed with page.route() — the same
// convention integration-actions.spec.js already uses for its own
// PG-mounted routes.

const { test, expect } = require('@playwright/test');

const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };

async function routeJson(page, pattern, body, status = 200) {
  await page.route(pattern, (route) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  }));
}

async function signIn(page, who = ADMIN) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
}

const PREFERENCES = {
  user_id: 'U-ADM',
  preferences: [
    { event: 'PR_SUBMITTED', channel: 'email', enabled: true, mandatory: false },
    { event: 'PR_APPROVED', channel: 'email', enabled: false, mandatory: false },
    { event: 'PASSWORD_CHANGED', channel: 'email', enabled: true, mandatory: true },
  ],
};

const HISTORY = {
  items: [
    {
      notification_id: 'NTF-1', event: 'PR_SUBMITTED', template: 'pr_submitted',
      recipient_user_id: 'U-ADM', recipient_email: 'admin@example.test',
      subject: 'PR-0001 submitted', state: 'SENT', attempts: 1, max_attempts: 5,
      next_attempt_at: null, last_error: null, provider: 'recording',
      provider_message_id: 'MID-1', correlation_id: 'cid-1', object_type: 'PurchaseRequest',
      object_id: 'PR-0001', created_at: '2026-09-13T00:00:00Z', sent_at: '2026-09-13T00:00:05Z',
    },
  ],
};

test.describe('Stream E — the signed-in user\'s own preferences and history', () => {
  test('preferences show enabled state, a mandatory one is disabled with an explanation, and a toggle saves itself', async ({ page }) => {
    await routeJson(page, '**/api/me/notification-preferences', PREFERENCES);
    await routeJson(page, '**/api/me/notifications**', HISTORY);
    let putBody = null;
    await page.route('**/api/me/notification-preferences', (route) => {
      if (route.request().method() !== 'PUT') return route.fallback();
      putBody = JSON.parse(route.request().postData() || '{}');
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ event: putBody.event, channel: 'email', enabled: putBody.enabled, mandatory: false }),
      });
    });

    await signIn(page);
    await page.click('#notificationsBtn');
    await page.waitForSelector('#dlg[open]');

    const prSubmitted = page.locator('#notifPref-PR_SUBMITTED');
    const prApproved = page.locator('#notifPref-PR_APPROVED');
    const passwordChanged = page.locator('#notifPref-PASSWORD_CHANGED');

    await expect(prSubmitted).toBeChecked();
    await expect(prApproved).not.toBeChecked();

    // The mandatory security notice: disabled, checked, and explained.
    await expect(passwordChanged).toBeChecked();
    await expect(passwordChanged).toBeDisabled();
    await expect(page.locator('#dlgBody')).toContainText('Mandatory security notice');

    // Delivery history renders too, in the same dialog.
    await expect(page.locator('#notifHistoryBody')).toContainText('PR_SUBMITTED');
    await expect(page.locator('#notifHistoryBody')).toContainText('PR-0001 submitted');

    // Toggling a non-mandatory preference saves itself immediately.
    await prApproved.check();
    await expect.poll(() => putBody).not.toBeNull();
    expect(putBody.event).toBe('PR_APPROVED');
    expect(putBody.enabled).toBe(true);
    await expect(prApproved).toBeChecked();
  });

  test('a failed save reverts the toggle and shows the refusal', async ({ page }) => {
    await routeJson(page, '**/api/me/notification-preferences', PREFERENCES);
    await routeJson(page, '**/api/me/notifications**', { items: [] });
    await page.route('**/api/me/notification-preferences', (route) => {
      if (route.request().method() !== 'PUT') return route.fallback();
      return route.fulfill({
        status: 422, contentType: 'application/json',
        body: JSON.stringify({ detail: { code: 'EVENT_MANDATORY', message: 'This event cannot be turned off.' } }),
      });
    });

    await signIn(page);
    await page.click('#notificationsBtn');
    await page.waitForSelector('#dlg[open]');

    const prApproved = page.locator('#notifPref-PR_APPROVED');
    await prApproved.check();

    await expect(page.locator('#dlgMsg')).toContainText('This event cannot be turned off.', { timeout: 10_000 });
    await expect(prApproved).not.toBeChecked();
  });
});

const MONITORING = {
  counts: { QUEUED: 2, SENDING: 0, SENT: 10, FAILED: 1, DEAD: 1, SUPPRESSED: 0 },
  oldest_due: '2026-09-13T00:00:00Z', adapter: 'recording', adapter_available: true,
  adapter_reason: null, sender: 'no-reply@example.test',
};

const OUTBOX_ITEMS = [
  {
    notification_id: 'NTF-DEAD-1', event: 'EXPORT_COMPLETED', template: 'export_completed',
    recipient_user_id: 'U-REQ', recipient_email: 'req@example.test', subject: 'Your export is ready',
    state: 'DEAD', attempts: 5, max_attempts: 5, next_attempt_at: null,
    last_error: 'adapter unavailable', provider: 'recording', provider_message_id: null,
    correlation_id: 'cid-dead-1', object_type: 'ExportJob', object_id: 'EXP-9',
    created_at: '2026-09-12T00:00:00Z', sent_at: null,
  },
];

test.describe('Stream E — the Administrator outbox screen', () => {
  test('the monitoring strip, outbox table and Dispatch all render from GET /api/notifications', async ({ page }) => {
    let dispatchCalled = false;
    await routeJson(page, '**/api/notifications?**', { items: OUTBOX_ITEMS, monitoring: MONITORING, events: ['EXPORT_COMPLETED'] });
    await routeJson(page, '**/api/notifications', { items: OUTBOX_ITEMS, monitoring: MONITORING, events: ['EXPORT_COMPLETED'] });
    await page.route('**/api/notifications/dispatch', (route) => {
      dispatchCalled = true;
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ claimed: 3, sent: 2, retry: 1, dead: 0, provider: 'recording' }),
      });
    });

    await page.goto('/#notifications');
    await page.waitForSelector('#loginForm', { state: 'visible' });
    await page.fill('#loginUser', ADMIN.user);
    await page.fill('#loginPass', ADMIN.password);
    await page.click('#loginBtn');
    await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
    await page.waitForSelector('#notifMonitorStrip', { timeout: 15_000 });

    const strip = page.locator('#notifMonitorStrip');
    await expect(strip).toContainText('SENT');
    await expect(strip).toContainText('10');
    await expect(strip).toContainText('DEAD');

    await expect(page.locator('#content')).toContainText('EXPORT_COMPLETED');
    await expect(page.locator('#content')).toContainText('Your export is ready');

    await page.getByRole('button', { name: 'Dispatch due notifications' }).click();
    await expect(page.locator('#notifDispatchResult')).toContainText('claimed 3, sent 2, retry 1, dead 0', { timeout: 10_000 });
    expect(dispatchCalled).toBe(true);
  });

  test('a DEAD notification\'s detail view offers Retry, which re-queues it', async ({ page }) => {
    await routeJson(page, '**/api/notifications?**', { items: OUTBOX_ITEMS, monitoring: MONITORING, events: ['EXPORT_COMPLETED'] });
    await routeJson(page, '**/api/notifications', { items: OUTBOX_ITEMS, monitoring: MONITORING, events: ['EXPORT_COMPLETED'] });
    let retryCalled = false;
    await routeJson(page, '**/api/notifications/NTF-DEAD-1', {
      ...OUTBOX_ITEMS[0],
      body_text: 'Your export EXP-9 is ready.',
      deliveries: [
        { attempt: 1, at: '2026-09-12T00:01:00Z', outcome: 'FAILED', provider: 'recording', detail: 'adapter unavailable' },
      ],
    });
    await page.route('**/api/notifications/NTF-DEAD-1/retry', (route) => {
      retryCalled = true;
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ ...OUTBOX_ITEMS[0], state: 'QUEUED', attempts: 0 }),
      });
    });

    await page.goto('/#notifications');
    await page.waitForSelector('#loginForm', { state: 'visible' });
    await page.fill('#loginUser', ADMIN.user);
    await page.fill('#loginPass', ADMIN.password);
    await page.click('#loginBtn');
    await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
    await page.waitForSelector('#notifMonitorStrip', { timeout: 15_000 });

    await page.getByRole('button', { name: 'View notification NTF-DEAD-1' }).click();
    await expect(page.locator('#content')).toContainText('adapter unavailable', { timeout: 15_000 });

    const retryBtn = page.getByRole('button', { name: 'Retry' });
    await expect(retryBtn).toBeVisible();
    await retryBtn.click();
    await expect.poll(() => retryCalled).toBe(true);
  });
});
