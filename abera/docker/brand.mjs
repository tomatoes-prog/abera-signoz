import fs from 'node:fs';
const file = '/src/frontend/build/index.html';
const html = fs.readFileSync(file, 'utf8');
if (!html.includes('</body>')) throw new Error('SigNoz HTML entry point changed');
// The offer remains visible on login and authenticated screens. No upstream
// copyright notice or feature/license check is removed.
fs.writeFileSync(file, html.replace('</body>', '<a href="/abera" style="position:fixed;bottom:10px;right:18px;z-index:900;padding:5px 12px;border-radius:4px;background:#16342b;color:#b7f4da;font:12px system-ui">Abera · Consumo y código fuente</a></body>'));
