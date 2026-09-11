/* app/frontend/src/components/budget/governed-select.js
   <governed-select> — a searchable, governed picker backed by
   GET /api/budget/selectors?kind=&q=&project_id=&entity_id=&limit=.

   "Governed" is the point: nothing on the Budget Setup screen lets a user
   type a raw id into a WBS, Budget Head, Budget Category, division, branch,
   zone, plant or location field. Every one of those is this element, so the
   value that ends up in the payload always came from a server-returned
   {id,label} row rather than free text.

   Why a Web Component rather than another components/*.js factory
   ------------------------------------------------------------------
   Every other reusable control in this codebase (capex-datatable,
   capex-record-dialog, verdict-badge, ...) is a plain factory function
   returning a DOM node plus a small API, and that idiom is kept everywhere
   it already applies. This one is genuinely repeated many times on a single
   screen (one per budget line, times several governed fields, times as many
   lines as the user adds) and needs its own encapsulated keyboard/focus
   state machine; a custom element gives it a <governed-select> tag that
   reads like a real form control in the line-editor markup, with `value`,
   `label`, `disabled`, `change` and `invalid` exactly where a caller expects
   them, rather than a bespoke `{el, getValue, ...}` object threaded through
   every call site by hand.

   NO SHADOW DOM. The server sends Content-Security-Policy: style-src 'self'
   with no 'unsafe-inline', which blocks a shadow root's own <style> text
   node exactly as it blocks a style="" attribute (core/dom.js's h() refuses
   the latter for the same policy). Rendering in the light DOM means this
   component is styled the same way every other screen here is: token-only
   rules in budget-setup.css, keyed off the classes below.

   ARIA
   ----
   The APG "Combobox with List Autocomplete" pattern: role="combobox" and
   aria-autocomplete="list" on the text input itself (ARIA 1.2), a sibling
   role="listbox" popup referenced by aria-controls, aria-activedescendant
   tracking the highlighted option rather than moving DOM focus, and
   aria-expanded reflecting whether the popup is open. ArrowDown/ArrowUp move
   the highlight, Enter commits it, Escape closes the popup and restores the
   last committed label.

   Money, ids and everything else the API contract fixes are read verbatim;
   nothing here parses or reformats the `id` field it stores.
*/

import { h, text, clear } from '../../core/dom.js';
import { listSelectors, BudgetApiError } from '../../features/budget/budget-api.js';

const DEBOUNCE_MS = 250;
const MIN_QUERY = 0; // an empty query is a valid "show me the first page" search

let seq = 0;

export class GovernedSelectElement extends HTMLElement {
  static get observedAttributes() {
    return ['kind', 'project-id', 'entity-id', 'placeholder', 'disabled', 'required'];
  }

  constructor() {
    super();
    seq += 1;
    this._seq = seq;
    this._value = '';
    this._label = '';
    this._items = [];
    this._activeIndex = -1;
    this._open = false;
    this._fetchToken = 0;
    this._debounceTimer = null;
    this._built = false;
  }

  connectedCallback() {
    if (!this._built) this._build();
    // _syncDisabled() already calls _syncPlaceholder() itself once it knows
    // whether the control is enabled -- when disabled (e.g. a WBS field with
    // no project chosen yet) it sets the explanatory "Choose a project
    // first" placeholder instead. Calling _syncPlaceholder() again here
    // unconditionally would immediately clobber that with the generic one.
    this._syncDisabled();
  }

  attributeChangedCallback(name) {
    if (!this._built) return;
    if (name === 'placeholder') { this._syncPlaceholder(); return; }
    if (name === 'disabled') { this._syncDisabled(); return; }
    if (name === 'project-id' || name === 'entity-id') {
      // The result set this control offers depends on these; a stale
      // selection made under a different project is worse than an empty
      // field, so changing the scope clears whatever was chosen.
      this._closeList();
      this._setSelection('', '');
      this._syncDisabled();
    }
  }

  /* ---------------- public API ---------------- */

  /** The selected row's id, or '' when nothing is selected. */
  get value() { return this._value; }

  /** The selected row's display label, or '' when nothing is selected. */
  get label() { return this._label; }

  get kind() { return this.getAttribute('kind') || ''; }

  set disabled(v) { this.toggleAttribute('disabled', !!v); }

  get disabled() { return this.hasAttribute('disabled'); }

  /** True when `required` is set and nothing is selected. */
  get invalid() { return this.hasAttribute('required') && !this._value; }

  /**
   * Pre-fill a known selection without a round trip — used when the editor
   * opens on an existing budget line and the API already returned both the
   * id and a display value (e.g. `wbs_code`, `budget_head_name`).
   * @param {string} id
   * @param {string} label
   */
  presetSelection(id, label) {
    this._setSelection(id || '', label || '');
  }

  /** Clear the current selection and any typed text. */
  clear() {
    this._setSelection('', '');
  }

  focusInput() {
    if (this._input) this._input.focus();
  }

  /* ---------------- construction ---------------- */

  _build() {
    this._built = true;
    const uid = `gov-sel-${this._seq}`;
    const listboxId = `${uid}-listbox`;
    const statusId = `${uid}-status`;

    this._input = h('input', {
      type: 'text',
      class: 'gov-select-input',
      id: uid,
      role: 'combobox',
      'aria-autocomplete': 'list',
      'aria-expanded': 'false',
      'aria-controls': listboxId,
      // A caller's visible <label> lives outside this element's own markup
      // (govField() in budget-setup.js, mirroring every other .field label
      // in this codebase), so it points here by id rather than this
      // component inventing its own duplicate label text.
      'aria-labelledby': this.getAttribute('aria-labelledby') || null,
      'aria-label': this.getAttribute('aria-labelledby') ? null : (this.getAttribute('aria-label') || null),
      autocomplete: 'off',
      spellcheck: 'false',
    });
    this._statusEl = h('div', { id: statusId, class: 'sr-only', role: 'status', 'aria-live': 'polite' }, '');
    this._listbox = h('ul', {
      id: listboxId, class: 'gov-select-listbox', role: 'listbox', hidden: true,
    });
    this._wrap = h('div', { class: 'gov-select' }, [this._input, this._listbox, this._statusEl]);

    this.appendChild(this._wrap);

    this._input.addEventListener('input', () => this._onType());
    this._input.addEventListener('keydown', (ev) => this._onKeyDown(ev));
    this._input.addEventListener('blur', () => {
      // A blur that lands on one of the listbox options is a selection, not
      // an abandonment; give the mousedown handler below time to run first.
      setTimeout(() => { if (!this._wrap.contains(document.activeElement)) this._onBlur(); }, 0);
    });
    this._input.addEventListener('focus', () => {
      if (!this.disabled && this._input.value.trim() === '' && !this._value) this._search('');
    });
  }

  _syncPlaceholder() {
    if (this._input) this._input.placeholder = this.getAttribute('placeholder') || 'Search…';
  }

  _syncDisabled() {
    if (!this._input) return;
    const needsProject = this.kind === 'wbs' && !this.getAttribute('project-id');
    const off = this.disabled || needsProject;
    this._input.disabled = off;
    if (off) {
      this._closeList();
      this._input.placeholder = needsProject
        ? 'Choose a project first'
        : (this.getAttribute('placeholder') || 'Search…');
    } else {
      this._syncPlaceholder();
    }
  }

  /* ---------------- selection state ---------------- */

  _setSelection(id, labelText) {
    const changed = id !== this._value || labelText !== this._label;
    this._value = id;
    this._label = labelText;
    this._input.value = labelText;
    this._input.setAttribute('aria-invalid', this.invalid ? 'true' : 'false');
    if (changed) {
      this.dispatchEvent(new CustomEvent('change', {
        bubbles: true,
        detail: { id: this._value, label: this._label },
      }));
    }
  }

  /* ---------------- searching ---------------- */

  _onType() {
    if (this.disabled) return;
    // Typing invalidates whatever was selected until a new option is chosen —
    // this element never lets the payload carry an id whose label the user
    // has since edited away from.
    if (this._value) { this._value = ''; this._label = ''; }
    const q = this._input.value;
    if (this._debounceTimer) clearTimeout(this._debounceTimer);
    this._debounceTimer = setTimeout(() => this._search(q), DEBOUNCE_MS);
  }

  async _search(q) {
    if (this.disabled) return;
    const kind = this.kind;
    if (!kind) return;
    if (q.length < MIN_QUERY) return;
    const token = (this._fetchToken += 1);
    this._renderLoading();
    try {
      const data = await listSelectors({
        kind,
        q,
        projectId: this.getAttribute('project-id') || undefined,
        entityId: this.getAttribute('entity-id') || undefined,
        limit: 25,
      });
      if (token !== this._fetchToken) return; // a newer keystroke already superseded this response
      this._items = (data && data.items) || [];
      this._renderOptions();
    } catch (err) {
      if (token !== this._fetchToken) return;
      this._renderError(err instanceof BudgetApiError ? err.message : 'These options could not be loaded.');
    }
  }

  /* ---------------- popup rendering ---------------- */

  _openList() {
    if (this._open) return;
    this._open = true;
    this._listbox.hidden = false;
    this._input.setAttribute('aria-expanded', 'true');
  }

  _closeList() {
    this._open = false;
    this._activeIndex = -1;
    this._listbox.hidden = true;
    this._input.setAttribute('aria-expanded', 'false');
    this._input.removeAttribute('aria-activedescendant');
  }

  _renderLoading() {
    this._openList();
    clear(this._listbox);
    this._listbox.appendChild(h('li', { class: 'gov-select-note', role: 'presentation' }, 'Searching…'));
  }

  _renderError(message) {
    this._openList();
    clear(this._listbox);
    this._listbox.appendChild(h('li', { class: 'gov-select-note gov-select-note-error', role: 'presentation' }, message));
    this._statusEl.textContent = message;
  }

  _renderOptions() {
    this._openList();
    clear(this._listbox);
    this._activeIndex = -1;
    if (!this._items.length) {
      this._listbox.appendChild(h('li', { class: 'gov-select-note', role: 'presentation' }, 'No matches.'));
      this._statusEl.textContent = 'No matches.';
      return;
    }
    this._items.forEach((item, index) => {
      const optId = `${this._input.id}-opt-${index}`;
      const opt = h('li', {
        id: optId,
        class: 'gov-select-option',
        role: 'option',
        'aria-selected': 'false',
        onMouseDown: (ev) => { ev.preventDefault(); this._choose(index); },
        onMouseEnter: () => this._setActive(index),
      }, text(item.label ?? item.id ?? ''));
      this._listbox.appendChild(opt);
    });
    this._statusEl.textContent = `${this._items.length} ${this._items.length === 1 ? 'option' : 'options'} available.`;
  }

  _setActive(index) {
    const options = this._listbox.querySelectorAll('.gov-select-option');
    options.forEach((el) => el.setAttribute('aria-selected', 'false'));
    this._activeIndex = index;
    const el = options[index];
    if (el) {
      el.setAttribute('aria-selected', 'true');
      this._input.setAttribute('aria-activedescendant', el.id);
      el.scrollIntoView({ block: 'nearest' });
    } else {
      this._input.removeAttribute('aria-activedescendant');
    }
  }

  _choose(index) {
    const item = this._items[index];
    if (!item) return;
    this._setSelection(String(item.id ?? ''), String(item.label ?? item.id ?? ''));
    this._closeList();
  }

  _onBlur() {
    this._closeList();
    // No committed selection and the field is not empty: the typed text is
    // not a valid value on its own, so it is discarded rather than left
    // looking chosen.
    if (!this._value && this._input.value.trim() !== '') {
      this._input.value = '';
    }
  }

  _onKeyDown(ev) {
    if (this.disabled) return;
    const optionCount = this._items.length;
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      if (!this._open) { this._search(this._input.value); return; }
      if (optionCount) this._setActive((this._activeIndex + 1) % optionCount);
      return;
    }
    if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      if (!this._open) { this._search(this._input.value); return; }
      if (optionCount) this._setActive((this._activeIndex - 1 + optionCount) % optionCount);
      return;
    }
    if (ev.key === 'Enter') {
      if (this._open && this._activeIndex >= 0) {
        ev.preventDefault();
        this._choose(this._activeIndex);
      }
      return;
    }
    if (ev.key === 'Escape') {
      if (this._open) {
        ev.preventDefault();
        this._closeList();
        this._input.value = this._label;
      }
      return;
    }
    if (ev.key === 'Tab') {
      this._closeList();
    }
  }
}

if (!customElements.get('governed-select')) {
  customElements.define('governed-select', GovernedSelectElement);
}
