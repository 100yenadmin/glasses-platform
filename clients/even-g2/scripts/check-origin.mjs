import {readFileSync} from 'node:fs';
const manifest = JSON.parse(readFileSync(new URL('../app.json', import.meta.url)));
const origins = manifest.permissions.find(item => item.name === 'network')?.whitelist;
if (!origins?.length || origins.some(origin => {
  try { const url = new URL(origin); return url.protocol !== 'wss:' || url.origin !== origin || /replace-me|\*/i.test(origin); }
  catch { return true; }
})) { console.error('Set app.json network.whitelist to your own full wss:// gateway origin before packing.'); process.exit(1); }
