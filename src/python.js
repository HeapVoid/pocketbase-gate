import {fileURLToPath} from 'node:url';
export const entrypoint = fileURLToPath(new URL('../engine/__init__.py', import.meta.url));
