# x-use brand assets

The icon and social card were generated with the built-in Image Generation tool on 2026-10-12 and are included with version 3.1.0. The name is lowercase **x-use**; the Python distribution is **x-use-mcp**.

| Asset | Use | Dimensions / format |
|---|---|---|
| [x-use-icon.png](../assets/x-use-icon.png) | Logo/icon, directory identity, avatars | 1254 × 1254, transparent RGBA PNG |
| [x-use-social-preview.jpg](../assets/x-use-social-preview.jpg) | GitHub social preview and README hero | 1774 × 887, 2:1 JPEG, 190,310 bytes |
| [x-use-social-preview.png](../assets/x-use-social-preview.png) | Generated social-card master | 1774 × 887 PNG |

The icon suggests connected brackets and forward movement. Keep its proportions, use generous clear space, and place the dark mark on a light surface. For dark interfaces, use a light container so the transparent icon remains visible. Use the same symbol across listings; do not substitute the official X mark.

The card uses the product name, category and workflow: “Research. Draft. Review. Publish.” It deliberately omits version numbers and tool counts. Its visible copy is also present in README text, so the image is not the only explanation of the product.

The PNG master exceeds GitHub's 1 MB upload limit. Use the JPEG export, which preserves the layout and meets the size requirement. This is a format/compression export of the generated artwork; the original PNG is retained. GitHub recommends 1280 × 640 for best display; this image keeps the same aspect ratio at a higher resolution. [GitHub guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview).

## Publication

The README uses the JPEG from this repository. GitHub's repository social preview is a separate setting: upload the JPEG through Settings → Social preview when updating that surface. Including the image in the repository does not automatically change this setting. The older assets/social-preview.png remains as a legacy asset.

Use the name and icon consistently in Registry and directory metadata after their HTTPS asset URLs are publicly available. x-use is independent of X; the brand does not imply platform endorsement.

## Generation prompts

### Icon

```text
Use case: logo-brand. Asset type: reusable open-source developer-tool icon for x-use, a Python MCP server connecting AI clients with reviewed X (Twitter) workflows. Create one original, confident, minimal geometric mark: two interlocking bracket-like ribbons suggesting a connection and a small forward arrow through negative space. It must be clearly distinct from the official X logo and from generic crossed diagonal strokes. Monochrome near-black shape, crisp flat edges, no gradients, no texture, no shadows, no words, no letters, no border, no mockup. Square 1024 by 1024 canvas with true transparent background, generous even padding (mark occupies about 70 percent), recognizable silhouette at 32 pixels. One single centered icon only; no variants or presentation sheet. Professional open-source infrastructure identity.
```

### Social card

Supporting image: x-use-icon.png.

```text
Use case: logo-brand. Asset type: GitHub Open Graph social preview for x-use. Supporting input: use the provided transparent x-use icon exactly as the brand symbol, preserve its geometry and near-black color. Create a refined developer-tool social card at 1280 by 640 pixels, exact 2:1 wide composition. Solid warm-white/off-white background, subtle very light gray geometric connection lines on the far right only. Flat editorial typography, ample whitespace, highly legible at small share-card sizes. Place the supplied compact black icon at left, next to large bold lowercase wordmark text 'x-use'. Under the wordmark set text on two clean lines: 'MCP server for X (Twitter)' and 'Research. Draft. Review. Publish.' Along bottom left set smaller text 'Python · Local-first · MIT'. Along bottom right set 'github.com/ihuzaifashoukat/x-use'. Use exact spelling, no other text or badges, no tool counts, no version numbers, no mockup, no gradients or photos. Keep all content inside a generous 72px safe margin. Professional, understated infrastructure brand. Optimize for a crisp small PNG under 1MB.
```

### Card refinement

Edit target: the initial generated social card.

```text
Edit target: provided x-use Open Graph card. Keep all exact text, icon shape, typography, alignment, safe margins and 2:1 layout unchanged. Change only rendering cleanliness: remove ALL paper grain, texture, shading and off-white noise. Make the background mathematically flat pure white (#ffffff), the dark lettering flat black, gray small text uniform gray and the faint connection graphic a few clean light-gray lines. Crisp antialiased edges only, vector-like flat fills, minimal colors. Output a compact PNG under 1MB for GitHub social previews, ideally 1280x640, never add content. Text must remain exactly as in the provided image.
```
