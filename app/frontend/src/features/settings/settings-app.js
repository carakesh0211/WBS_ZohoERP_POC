/* app/frontend/src/features/settings/settings-app.js
   Host-page controller for settings.html: switches between the
   organisation-hierarchy admin screens and SCR-30 Master Data Configuration.
   Each screen mounts into its own root and is only fetched once, on first
   activation, so opening the page never issues requests for a tab the user
   has not looked at yet.
*/

import { mountOrgHierarchy } from './org-hierarchy.js';
import { mountMasters } from './masters.js';

/**
 * Mount the settings screens into whichever host document supplies the ids
 * below. Exported so the SPA shell's hash route (#settings, via
 * core/router.js) drives the same controller as the standalone settings.html —
 * one implementation, two hosts. Returns silently when the host is absent.
 */
export function mountSettingsApp() {
  const hierarchyTabBtn = document.getElementById('settingsTabHierarchy');
  const mastersTabBtn = document.getElementById('settingsTabMasters');
  const hierarchyPanel = document.getElementById('settingsHierarchyPanel');
  const mastersPanel = document.getElementById('settingsMastersPanel');
  if (!hierarchyTabBtn || !mastersTabBtn || !hierarchyPanel || !mastersPanel) return;

  const mounted = { hierarchy: false, masters: false };

  function activate(tab) {
    const showHierarchy = tab === 'hierarchy';
    hierarchyTabBtn.setAttribute('aria-selected', String(showHierarchy));
    mastersTabBtn.setAttribute('aria-selected', String(!showHierarchy));
    hierarchyTabBtn.tabIndex = showHierarchy ? 0 : -1;
    mastersTabBtn.tabIndex = showHierarchy ? -1 : 0;
    hierarchyPanel.hidden = !showHierarchy;
    mastersPanel.hidden = showHierarchy;

    if (showHierarchy && !mounted.hierarchy) {
      mounted.hierarchy = true;
      mountOrgHierarchy(hierarchyPanel);
    }
    if (!showHierarchy && !mounted.masters) {
      mounted.masters = true;
      mountMasters(mastersPanel);
    }
  }

  hierarchyTabBtn.addEventListener('click', () => activate('hierarchy'));
  mastersTabBtn.addEventListener('click', () => activate('masters'));

  // Standard left/right roving-tabindex behaviour for a tablist, matching
  // the keyboard-only traversal requirement without depending on Tab alone
  // to move between the two tabs.
  const tabs = [hierarchyTabBtn, mastersTabBtn];
  for (const [i, btn] of tabs.entries()) {
    btn.addEventListener('keydown', (ev) => {
      if (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft') return;
      ev.preventDefault();
      const next = tabs[(i + (ev.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      next.focus();
      next.click();
    });
  }

  activate('hierarchy');
}

// Standalone host only. The SPA imports this module after DOMContentLoaded has
// already fired and calls mountSettingsApp() itself, so this listener never
// runs there and cannot double-mount.
document.addEventListener('DOMContentLoaded', mountSettingsApp);
