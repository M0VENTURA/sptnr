# Frontend Build System

This directory contains vendored frontend assets (Bootstrap, Bootstrap Icons)
that replace the CDN-loaded versions.

## Setup

```bash
# Install dependencies (esbuild, bootstrap, bootstrap-icons)
npm install

# Build bundled JS and copy vendor assets
npm run build

# Or watch for changes during development
npm run watch
```

## Configuration

Set `features.use_local_assets: true` in your config to use local vendor files
instead of CDN:

```yaml
features:
  use_local_assets: true
```

When `use_local_assets` is `false` (default), all assets load from CDN as before.

## Structure

```
static/
├── js/              # Source JS modules (edit these)
│   ├── main.js      # Entry point — imports all modules
│   ├── downloads.js
│   ├── genre-utils.js
│   ├── musicbrainz-folder-groups.js
│   ├── player.js
│   └── playlist.js
├── dist/            # Build output (generated, not committed)
│   ├── main.js      # Bundled + minified JS
│   └── vendor/      # Vendored CSS/JS assets
│       ├── bootstrap.min.css
│       ├── bootstrap.bundle.min.js
│       ├── bootstrap-icons.css
│       └── fonts/   # Bootstrap Icons font files
└── css/             # Custom CSS (if needed)
```

## Adding new JS

1. Create a new module in `static/js/`
2. Import it in `static/js/main.js`
3. Rebuild: `npm run build`
4. The new module is automatically bundled into `static/dist/main.js`
