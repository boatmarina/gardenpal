# GardenPal

## Deployment

After pushing changes to the feature branch, always also merge into `main` and push `main` to origin so Vercel deploys to production automatically. Do this on every deploy unless explicitly told otherwise.

```bash
git checkout main
git merge --no-ff <feature-branch>
git push -u origin main
git checkout <feature-branch>
```

## Frontend Architecture

### Add-plant mechanisms
Both `idea_new.html` (Library) and `yard_plant_new.html` (Yard) have four add-plant mechanisms:
- **name** — search dropdown
- **photo** — photo identification (may show suggestion panel)
- **label** — label/tag OCR scan
- **url** — image URL

When modifying any one mechanism, verify all four still work on both pages. Changes to shared behaviour (photo display, detail chips, field population) must be applied consistently across both pages and all four modes.

### Field population
All plant detail field population MUST go through the single page-level helper:
- `applyDetails(details)` in `idea_new.html`
- `applyYardDetails(details)` in `yard_plant_new.html`

Never set individual hidden fields manually in a new code path. If a new field is added, add it to the helper — it then works everywhere automatically.

### Shared JS utilities (`garden.js`)
Reusable client-side logic lives in `garden.js` under the `GP` namespace and is available on every page. Current utilities:

| Function | Purpose |
|---|---|
| `GP.compressImage(file, maxPx, quality)` | Compress a File/Blob to JPEG |
| `GP.blobToDataURL(blob)` | Read a Blob as a base-64 data URL |
| `GP.fetchPlantData(name, count)` | Fetch photos + details in parallel |
| `GP.renderPlantDropdown(el, results, onSelect, query)` | Render scored search dropdown |
| `GP.openLightbox(src)` | Full-screen image lightbox |
| `GP.renderPhotosInGrid(containerEl, photos, altText)` | Render photo array into a `.photos-grid` with lightbox |

When adding new shared behaviour, add it to `garden.js` as `GP.*` rather than inline in templates. Template-level JS should only contain page-specific wiring.

### Photo rendering
Always use `GP.renderPhotosInGrid(containerEl, photos, altText)` to render a list of photo URLs into any `.photos-grid` div. It handles item markup, lightbox click handlers, `onerror` cleanup, and `hidden = false`. Never write inline photo-rendering loops in templates.

