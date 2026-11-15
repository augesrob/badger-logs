# giftkit — TikTok gift animations → transparent video

Collects TikTok LIVE gift artwork and converts it into **VP9 WebM with a real
alpha channel**, ready to drop into an OBS browser source. No Android emulator,
no rooted device, no tap-scripting.

```
TikTok LIVE web API (HTTPS)          ← no device, no emulator
        │  /webcast/gift/list/  → gift catalogue + CDN URLs
        ▼
  giftkit fetch      → gifts/manifest.json + gifts/raw/**
        │
        ▼  content sniffing (magic bytes, not filenames)
  animated WebP · GIF · APNG · VAP mp4 · split-alpha mp4 · SVGA · PAG · zip
        │
        ▼  ffmpeg / Pillow / native SVGA renderer
  giftkit convert    → gifts/webm/*.webm (VP9 + alpha) + gifts/library.json
        │
        ▼
  giftkit serve      → OBS browser source + /trigger endpoint
```

## Why not an emulator

Driving an emulator to make the app populate `…/files/live/` works, but it is
slow, fragile (tap coordinates rot on every UI change), needs a rooted or
debuggable build to read the app's private data, and gets you a pile of
md5-named blobs with no idea which gift each one belongs to.

The web LIVE player asks `webcast.tiktok.com` for the same gift panel over
plain HTTPS, and the response carries the gift **name, id, diamond cost and CDN
URLs** together. One request, fully labelled, repeatable in CI.

If you still want the device route, it is supported — `--source adb` — and so
is a directory you already dumped by any means (`--source dir`).

## Install

```bash
pip install -e .            # or: pip install -r requirements.txt
giftkit doctor              # checks ffmpeg, VP9/alpha, Pillow, optional extras
```

You need **ffmpeg built with libvpx** (`brew install ffmpeg`,
`apt install ffmpeg`). No system ffmpeg? `pip install imageio-ffmpeg` and
giftkit will use the bundled binary, or point `GIFTKIT_FFMPEG` at your own.

## Use it

```bash
# 1. grab the catalogue (any account that is live right now will do —
#    the gift panel is regional, not per-streamer)
giftkit fetch --user someone_who_is_live --out gifts

# 2. convert everything to transparent WebM
giftkit convert --out gifts

# …or both at once
giftkit build --user someone_who_is_live --out gifts

# 3. serve the overlay, then add http://127.0.0.1:8722 as an OBS browser source
giftkit serve --out gifts

# 4. make it play something
giftkit trigger Rose
curl 'http://127.0.0.1:8722/trigger?gift=5655&repeat=2'
```

Open <http://127.0.0.1:8722/?browse=1> in a normal browser for a contact sheet
of everything that converted — click a gift to fire it into OBS.

Already have a dump from a device or a zip?

```bash
giftkit build --source dir --path ./raw_gifts --out gifts
giftkit build --source adb --path ./pulled  --out gifts   # needs adb + a readable device
```

Saved a `/webcast/gift/list/` response from devtools, or have a gift list from
[`TikTokLive`](https://github.com/isaackogan/TikTokLive)? Feed it straight in:

```bash
giftkit build --source manifest --path gift_list.json --out gifts
```

## Formats

Everything is identified by **magic bytes**, because cached gift assets are
usually extensionless md5 blobs.

| Input | How it is handled |
| --- | --- |
| Animated WebP, GIF, APNG | decoded frame-by-frame with Pillow (correct disposal/blending), re-encoded with alpha |
| VAP mp4 | reads the file's own `vapc` config box for the **exact** colour/matte rectangles, then `crop` + `alphamerge` inside ffmpeg |
| Split-alpha mp4 with no `vapc` | layout sniffed from the pixels — all four of `left-right`, `right-left`, `top-bottom`, `bottom-top`; override with `--layout` |
| Plain opaque mp4 | converted as-is and flagged `has_alpha: false` (or dropped with `--skip-opaque`) |
| SVGA | native decoder: gzip → protobuf → sprite compositing in Pillow. No Node, no headless browser |
| PAG | external renderer — `--pag-cmd '<cli> {input} {outdir}'`, or the bundled libpag helper (`npm install` in `tools/`) |
| ZIP bundle | unpacked (with zip-slip protection) and each member converted |
| PNG / JPEG / static WebP | saved to `gifts/stills/` as RGBA PNG |
| Lottie JSON | detected and reported, not rendered |

Identical files are hashed and converted once, then shared across every gift
that references them — the gift panel reuses artwork heavily.

Not sure what a file is? `giftkit inspect path/to/blob` prints the detected
format, the VAP rectangles, SVGA sprite counts, and so on.

## Output layout

```
gifts/
├── manifest.json     what was found, where it came from, checksums
├── raw/              exactly what was downloaded
├── webm/             the converted animations
├── stills/           single-frame artwork
└── library.json      the index the overlay (and your code) reads
```

`library.json` gives each asset its `gift_id`, `name`, `slug`, size, fps, frame
count, duration, `has_alpha`, and the source URL — plus a `skipped` list saying
exactly why anything did not convert.

## Alpha, honestly

VP9 in WebM stores alpha in a **side channel**, so a plain
`ffprobe gift.webm` reports `yuv420p` even when the file is fully transparent.
Chromium — and therefore OBS's browser source — composites it correctly. To see
the alpha from the command line you must name the decoder:

```bash
ffmpeg -c:v libvpx-vp9 -i gifts/webm/rose-5655-image.webm -frames:v 1 -pix_fmt rgba frame.png
```

If you need alpha that every tool agrees on (After Effects, Premiere, Resolve),
use `--target mov` for ProRes 4444 instead. `--target png-seq`, `apng`, `gif`
and `webm-vp8` are also available.

Other flags worth knowing:

```bash
giftkit convert --out gifts \
  --crf 24 \              # VP9 quality, lower = better
  --max-width 1080 \      # downscale full-screen effects
  --max-frames 300 \      # cap very long animations
  --layout top-bottom \   # force a split layout instead of sniffing
  --skip-opaque           # only keep assets with real transparency
```

## Wiring it to live gift events

`examples/tiktok_live_bridge.py` listens to a room with
[`TikTokLive`](https://github.com/isaackogan/TikTokLive) and POSTs to the
overlay when a gift lands (streak gifts fire once, when the streak ends):

```bash
pip install TikTokLive
python examples/tiktok_live_bridge.py --user someone_who_is_live
```

Anything that can make an HTTP request works just as well — the overlay only
needs `POST /trigger {"gift": "Rose"}`.

## Tests

```bash
pip install pytest && python -m pytest
```

The suite builds synthetic assets for every supported format — including a VAP
mp4 with a real `vapc` box and an SVGA file encoded by a miniature protobuf
writer — converts them, then **decodes the output back and checks the alpha
channel**. No network and no committed binaries.

## Limits and caveats

- The gift panel is **regional and versioned**. Two accounts in different
  countries return different catalogues; pass `--param region=US` (or any other
  webcast query parameter) to experiment.
- Full-screen effect bundles are not always exposed to the web client.
  `--with-effects` queries the asset endpoint for them, but availability varies
  by region and app version — the gift panel artwork always works, the
  full-screen effects are best effort. Assets you obtain by other means convert
  through the same pipeline via `--source dir`.
- SVGA vector `shapes` layers, `clipPath` masks and matte layers are not
  rendered; sprite layers, which carry the artwork in practically every gift,
  are. Anything skipped says so in `library.json`.
- PAG needs libpag; there is no pure-Python path.
- The artwork belongs to TikTok and the artists who made it. This tool is for
  building overlays for streams you run; check TikTok's terms before
  redistributing anything you pull.
