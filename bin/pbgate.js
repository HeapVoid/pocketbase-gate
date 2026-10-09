#!/usr/bin/env node
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const engine = fileURLToPath(new URL('../engine/gate.py', import.meta.url));
const child = spawn(process.env.PBGATE_PYTHON || 'python3', [engine, ...process.argv.slice(2)], {
  stdio: 'inherit', env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'},
});
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
child.once('error', error => {
  console.error(`pbgate requires Python 3.9+: ${error.message}`);
  process.exitCode = 2;
});
child.once('exit', (code, signal) => { process.exitCode = code ?? (signal === 'SIGINT' ? 130 : 2); });
