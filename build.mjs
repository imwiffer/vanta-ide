import * as esbuild from 'esbuild';
import { mkdirSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const out = join(__dirname, 'web', 'vendor');
rmSync(out, { recursive: true, force: true });
mkdirSync(join(out, 'workers'), { recursive: true });

const common = {
  bundle: true,
  format: 'iife',
  target: ['chrome120'],
  logLevel: 'info',
  loader: { '.ttf': 'file', '.svg': 'dataurl', '.woff': 'file', '.woff2': 'file' },
  nodePaths: [join(__dirname, 'node_modules')],
};

// editor bundle (all contributions + languages)
await esbuild.build({
  ...common,
  entryPoints: { 'monaco': join(__dirname, 'web', 'src', 'monaco-entry.js') },
  outdir: out,
  minify: true,
  sourcemap: false,
});

// language service workers
const workers = {
  editor: 'monaco-editor/editor/editor.worker.js',
  json: 'monaco-editor/languages/features/json/json.worker.js',
  css: 'monaco-editor/languages/features/css/css.worker.js',
  html: 'monaco-editor/languages/features/html/html.worker.js',
  ts: 'monaco-editor/languages/features/typescript/ts.worker.js',
};

for (const [name, entry] of Object.entries(workers)) {
  await esbuild.build({
    ...common,
    entryPoints: { [name]: entry },
    outdir: join(out, 'workers'),
    minify: true,
  });
}
console.log('vendor build complete');

