# Authentication gate (first-run login + setup wizard)

Implements the missing authentication enforcement across the whole app.

## What changed

- `helpers/app_hooks.py` `before_request` now enforces a session for every
  route, with a small public allow-list:
  - Always public: static assets, the login page and the logout page.
  - First-run public (while Navidrome is unconfigured): the setup wizard page
    and its APIs (`/api/setup/save`, `/api/setup/save-partial`,
    `/api/test-navidrome-connection`, the Essentia model download endpoints
    and `/api/navidrome/import`).
- While Navidrome is unconfigured (first run), all other pages redirect to
  the setup wizard so a brand-new user can complete it; other APIs return
  401 JSON.
- Once configured, unauthenticated page requests redirect to the login page
  and unauthenticated API calls return 401 JSON (`Authentication required`).
- `helpers/config_helpers.needs_setup()` centralises the "first run?" check
  (multi-user `navidrome_users`, legacy `navidrome` dict, or flat
  `nav_url`/`nav_user`/`nav_pass` settings/env keys).  `routes/ui_routes.py`
  `_needs_setup` now delegates to it.
- The Config page banner ("First-run setup incomplete") is now actually
  populated from `needs_setup()`.

## Behaviour for a new user

1. First run (no Navidrome configured): visiting any page lands on the setup
   wizard; the login page remains reachable too.
2. Completing the wizard saves config and establishes a session.
3. Subsequent visits require login (Navidrome credentials).

## Tests

`tests/test_auth_gate.py` covers first-run reachability and the configured
auth gate.  `tests/conftest.py` now provides an authenticated `client`
fixture (and an `unauthed_client`) and marks the app as configured via
`POPULARLR_NAV_*` env vars so the rest of the suite passes through the gate.
