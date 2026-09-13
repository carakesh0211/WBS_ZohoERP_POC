// tests/vrt/admin-override.spec.js
//
// STREAM B — the Administrator's deliberate self-approval override.
//
// auth.require_separation refuses a self-approval with 403 SELF_APPROVAL;
// an Administrator may override it by re-sending the SAME call with
// `admin_override_reason` (auth.ADMIN_OVERRIDE_MIN_REASON = 10 characters).
// Every OTHER role sending a reason is refused with
// ADMIN_OVERRIDE_NOT_PERMITTED, so the frontend must never offer the retry
// to anyone but an Administrator.
//
// WHY THE APPROVAL ENGINE'S DECIDE SCREEN, AND NOT AN app.js DIALOG
// ------------------------------------------------------------------
// Every app.js approve dialog (data-approve-pr, data-approve-rev,
// data-approve-cap) is rendered only when `can(...)` holds the underlying
// permission (pr.approve, revision.approve, capitalisation.approve) — and
// U-ADM (Administrator) genuinely holds NONE of those in auth.PERMISSIONS;
// Administrator governs and configures, it does not hold the ordinary
// maker-checker approve grants. So there is no seeded identity that would
// ever see one of those buttons render at all, let alone reach the SELF_
// APPROVAL branch behind it.
//
// approval-detail.js is different by design (see its own module docstring,
// "WHY THERE IS NO CLIENT-SIDE PERMISSION CHECK ON THE DECISION PANEL"): the
// decision form renders UNCONDITIONALLY once the screen itself is open
// (approval.read alone, which Administrator DOES hold), and the server's
// refusal is what the screen renders. That makes it the one flow where an
// Administrator organically reaches a stubbed SELF_APPROVAL response through
// real navigation and a real permission grant, with only the network call
// itself stubbed — exactly the "must not depend on PostgreSQL" convention
// integration-actions.spec.js already uses for its own PG-mounted routes.

const { test, expect } = require('@playwright/test');

const ADMIN = { user: 'U-ADM', password: 'U-ADM!demo' };
const REQUESTOR = { user: 'U-REQ', password: 'U-REQ!demo' };

const INSTANCE_ID = 'INST-SELF-1';
const INSTANCE = {
  instance_id: INSTANCE_ID,
  object_type: 'purchase_request',
  object_id: 'PR-SELF-1',
  object_version: 1,
  object_content_sha: 'deadbeef',
  status: 'OPEN',
  definition_id: 'DEF-1',
  definition_code: 'PR-STANDARD',
  definition_version: 1,
  amount_paise: 100000,
  maker_user_id: 'U-ADM',
  opened_at: '2026-09-13T00:00:00Z',
  closed_at: null,
  correlation_id: 'cid-self-1',
  reason_codes: [],
  stages: [],
};

const SELF_APPROVAL_REFUSAL = {
  code: 'SELF_APPROVAL',
  message: 'You raised this and cannot also approve it. Segregation of duties requires an '
    + 'independent approver. An Administrator may override this deliberately by supplying '
    + 'admin_override_reason; the override is audited.',
};

async function routeJson(page, pattern, body, status = 200) {
  await page.route(pattern, (route) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  }));
}

async function signIn(page, who) {
  await page.goto('/');
  await page.waitForSelector('#loginForm', { state: 'visible' });
  await page.fill('#loginUser', who.user);
  await page.fill('#loginPass', who.password);
  await page.click('#loginBtn');
  await page.waitForSelector('#shell:not([hidden])', { timeout: 15_000 });
  await page.waitForSelector('#nav .nav-item', { state: 'attached', timeout: 15_000 });
}

async function settleScreen(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('content');
    return !!c && !c.querySelector('.loading');
  }, null, { timeout: 15_000 });
  await page.waitForSelector(
    '#content .scr-host[data-mounted="1"], #content .scr-host[data-mount-failed="1"]',
    { state: 'attached', timeout: 15_000 });
  const failed = page.locator('#content .scr-host[data-mount-failed="1"]');
  if (await failed.count()) {
    throw new Error(`the screen's mount() threw: ${(await failed.innerText()).trim()}`);
  }
}

async function gotoInstance(page) {
  await page.goto(`/?instance=${INSTANCE_ID}#approval-request`);
  await settleScreen(page);
}

/**
 * Stubs GET .../{id} (constant) and POST .../{id}/decide. The decide handler
 * refuses SELF_APPROVAL until the request body carries `admin_override_
 * reason`, then succeeds — the same branch auth.require_separation takes in
 * the real service. Every decide call's parsed body is pushed to `calls`.
 */
async function stubSelfApproving(page, calls) {
  await routeJson(page, `**/api/approvals/${INSTANCE_ID}`, INSTANCE);
  await page.route(`**/api/approvals/${INSTANCE_ID}/decide`, (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    calls.push(body);
    if (!body.admin_override_reason) {
      return route.fulfill({ status: 403, contentType: 'application/json', body: JSON.stringify(SELF_APPROVAL_REFUSAL) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'APPROVED' }) });
  });
}

test.describe('Stream B — Administrator self-approval override', () => {
  test('an Administrator sees the unmissable warning and the retry carries the reason', async ({ page }) => {
    const calls = [];
    await stubSelfApproving(page, calls);
    await signIn(page, ADMIN);
    await gotoInstance(page);

    const submit = page.locator('#approvalDecisionForm button[type="submit"]');
    await expect(submit).toBeVisible();
    await submit.click();

    // The unmissable warning, verbatim, and the audit-trail action it names.
    const panel = page.locator('#approvalAdminOverridePanel');
    await expect(panel).toBeVisible({ timeout: 15_000 });
    await expect(panel).toContainText('You raised this yourself');
    await expect(panel).toContainText('deliberate Administrator override');
    await expect(panel).toContainText('ADMIN_SELF_APPROVAL_OVERRIDE');
    await expect(panel).toContainText('every other Administrator is notified');

    // A reason under 10 characters is refused CLIENT-SIDE, without a second
    // network call — auth.ADMIN_OVERRIDE_MIN_REASON is 10.
    await page.fill('#approvalAdminOverrideReason', 'too short');
    await page.getByRole('button', { name: 'Approve with override' }).click();
    await expect(panel).toContainText('at least 10 characters');
    expect(calls.length, 'no second call was made for a reason that fails client-side validation').toBe(1);

    // A sufficient reason retries the SAME decision.
    await page.fill('#approvalAdminOverrideReason', 'Vendor escalation — approving my own request deliberately.');
    await page.getByRole('button', { name: 'Approve with override' }).click();

    await expect(page.locator('#approvalDetailOutcome')).toContainText('Decision recorded', { timeout: 15_000 });
    expect(calls.length).toBe(2);
    expect(calls[0].admin_override_reason, 'the reason must never be sent on the first attempt').toBeUndefined();
    expect(calls[1].admin_override_reason).toBe('Vendor escalation — approving my own request deliberately.');
    // The SAME idempotency key and object version on both attempts — this is
    // one decision made deliberately, not two.
    expect(calls[1].idempotency_key).toBe(calls[0].idempotency_key);
    expect(calls[1].object_version).toBe(calls[0].object_version);
    expect(calls[1].action).toBe('APPROVE');
  });

  test('a Requestor never sees the override form — only the ordinary refusal', async ({ page }) => {
    const calls = [];
    await stubSelfApproving(page, calls);
    await signIn(page, REQUESTOR);
    await gotoInstance(page);

    const submit = page.locator('#approvalDecisionForm button[type="submit"]');
    await expect(submit).toBeVisible();
    await submit.click();

    const outcome = page.locator('#approvalDetailOutcome');
    await expect(outcome).toContainText('cannot also approve it', { timeout: 15_000 });

    await expect(page.locator('#approvalAdminOverridePanel')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Approve with override' })).toHaveCount(0);
    expect(calls.length, 'a Requestor never retries with a reason').toBe(1);
    expect(calls[0].admin_override_reason).toBeUndefined();
  });
});
