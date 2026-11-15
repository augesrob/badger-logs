#!/usr/bin/env node
// Render a .pag file to a PNG sequence using the libpag WebAssembly build.
//
// Opt-in helper for `giftkit convert`: run `npm install` in this directory
// once, then PAG assets convert like any other format. Kept out of the Python
// package on purpose - libpag is the only complete PAG implementation, and it
// is JavaScript/C++.
//
//   node pag_render.mjs --input gift.pag --outdir ./frames [--fps 30] [--max-frames 300]

import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

function parseArgs(argv) {
  const args = {fps: 30};
  for (let i = 2; i < argv.length; i += 2) {
    const key = argv[i].replace(/^--/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    args[key] = argv[i + 1];
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.input || !args.outdir) {
    console.error('usage: pag_render.mjs --input <file.pag> --outdir <dir> [--fps N]');
    process.exit(2);
  }

  const {createCanvas} = await import('@napi-rs/canvas');
  const {PAGInit} = await import('libpag');

  // libpag's web build expects a browser-ish global; @napi-rs/canvas provides
  // a compatible 2D context, so we hand it the factory it asks for.
  const PAG = await PAGInit({
    locateFile: (file) => path.join(process.cwd(), 'node_modules', 'libpag', 'lib', file),
  });

  const buffer = await fs.readFile(args.input);
  const pagFile = await PAG.PAGFile.load(buffer.buffer.slice(
    buffer.byteOffset, buffer.byteOffset + buffer.byteLength));
  if (!pagFile) throw new Error(`not a readable PAG file: ${args.input}`);

  const width = pagFile.width();
  const height = pagFile.height();
  const duration = pagFile.duration() / 1e6;            // microseconds -> seconds
  const fps = Number(args.fps) || 30;
  let frameCount = Math.max(1, Math.round(duration * fps));
  if (args.maxFrames) frameCount = Math.min(frameCount, Number(args.maxFrames));

  const canvas = createCanvas(width, height);
  const surface = PAG.PAGSurface.fromCanvas(canvas);
  const player = PAG.PAGPlayer.create();
  player.setSurface(surface);
  player.setComposition(pagFile);

  await fs.mkdir(args.outdir, {recursive: true});
  for (let i = 0; i < frameCount; i += 1) {
    player.setProgress(frameCount === 1 ? 0 : i / (frameCount - 1));
    await player.flush();
    const png = await canvas.encode('png');       // preserves the alpha channel
    await fs.writeFile(path.join(args.outdir, String(i).padStart(5, '0') + '.png'), png);
  }

  player.destroy();
  surface.destroy();
  pagFile.destroy();
  process.stdout.write(JSON.stringify({frames: frameCount, width, height, fps}) + '\n');
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
