# Live tracks reach 4★ only when marked as a single

## Symptom

Live / acoustic / unplugged / demo / alternate tracks (e.g. a bonus "(Live)"
cut on a studio album, or a live album's crowd-pleaser) could reach **4★**
purely on album-relative popularity — sitting alongside real studio singles
at the same tier even though they were never issued as singles.

## Change

New config key `single_detection.live_4star_requires_single` (**default ON**):

- **ON (default):** a live-track-grouped title (live, acoustic, unplugged,
  orchestral, demo, jam-along, alternate) may only reach the **4★** album-z
  band when it is marked as a single (`single_confidence` high / medium /
  user).  A non-single live track is capped at **3★**.
- **OFF:** legacy behaviour — live tracks reach 4★ on album-z alone.

5★ was already blocked for live tracks (every 5★ path is gated by
`not is_live`); this change tightens the 4★ band.

## Files

- `services/popularity/stages/finalise_stage.py` — `_album_z_band_star` gains
  `is_live` / `single_confidence` params and applies the live-4★ gate (read
  live from `get_standout_config`); all `_assign_stars` call sites pass them.
- `helpers/config_helpers.py` — `live_4star_requires_single` added to
  `get_standout_config` defaults + merge list.
- `templates/pages/config.html` — "Live 4★ Requires Single" toggle in the
  Score Adjustments section.
- `static/js/config.js` — toggle saved under `single_detection`.
- `tests/test_live_4star_requires_single.py` — regression tests (non-single
  live capped at 3★, single live reaches 4★, user override, acoustic title,
  toggle-off restores 4★, config default).
