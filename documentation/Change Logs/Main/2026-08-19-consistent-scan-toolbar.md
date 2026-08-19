# Consistent scan toolbar: Force checkbox sizing + Run placement + Actions

## Symptom

1. The **Force** checkbox on the album page rendered much smaller than on
   other pages — Bootstrap's `form-check-input` is `1em`, so it follows the
   surrounding label's font-size; the album page's `small` label shrank the
   tickbox.
2. The **Run** button on the album page sat after the Force checkbox; the
   artist page's Run button used `ms-auto` (pushed right, icon-only).
3. The artist page's **Actions** dropdown didn't match the album page's
   (missing the `btn-sm` + "Actions" label styling).

## Fix

### 1. Shared Force-checkbox sizing (`static/css/popularr.css`)

A `.scan-toolbar` CSS block now forces every scan-toolbar Force checkbox to a
fixed `1.25rem × 1.25rem` with a consistent label size, regardless of the
surrounding label class.  The selector also targets `form .form-check-input[name="force"]`
so any page that uses a `name="force"` checkbox (album, artist v2, artist
legacy, dashboard, hero) gets the same size automatically.

### 2. Run button moved to the left

- **Album page** (`album_detail.html`): the Run button is now `order-first`
  inside the form — it appears left of the scan selector and Force checkbox,
  matching the "Run first" layout.
- **Artist v2** (`artist_detail_v2.html`): Run button is now `btn-warning
  btn-sm` with a visible "Run" label and sits immediately after the scan
  selector (no longer pushed to the right with `ms-auto`).
- **Artist legacy** (`artist_detail.html`): Run button is `order-first` with
  the "Run" label.
- **Hero component** (`_hero.html`): Run button `order-first` with "Run".

### 3. Actions dropdown matches across pages

The artist v2 page's Actions button now uses `btn btn-outline-secondary
btn-sm dropdown-toggle` with the three-dots-vertical icon + "Actions" text +
`dropdown-menu-end` — identical to the album page.

## Tests

No automated test (frontend-only) — verified via `get_errors` and review of
the shared CSS selector + toolbar markup.

## Config

No new config keys.
