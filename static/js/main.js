/**
 * Popularr main entry point.
 *
 * This file is the entry point for esbuild bundling.
 * All JS modules are imported here and bundled into static/dist/main.js.
 *
 * To add a new JS module:
 *   1. Create the module in static/js/
 *   2. Import it below
 *   3. Rebuild: `npm run build`
 */

// Core modules
import "./downloads.js";
import "./genre-utils.js";
import "./musicbrainz-folder-groups.js";
import "./player.js";
import "./playlist.js";

// Global initialization runs after DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  // Initialize Bootstrap tooltips if Bootstrap loaded
  if (typeof bootstrap !== "undefined" && bootstrap.Tooltip) {
    const tooltipTriggerList = document.querySelectorAll('[data-bs-toggle="tooltip"]');
    [...tooltipTriggerList].map((el) => new bootstrap.Tooltip(el));
  }
});
