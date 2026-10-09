import {readFile, stat, writeFile} from 'node:fs/promises';
import {realpathSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {posix, resolve} from 'node:path';
import {inspectModelDependencies} from './dependencies.js';

const request = JSON.parse(await readFile(process.argv[2], 'utf8'));
const {root, graph} = request;
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const seen = new Set(), roots = new Set(), files = {};
let opaque = !request.files.length;
const visit = async name => {
  if (seen.has(name)) return;
  seen.add(name);
  if (!name || name.startsWith('/') || name.split('/').includes('..')) {opaque=true;return;}
  let bytes;
  try {bytes=await readFile(resolve(root,name));}
  catch (error) {if (error.code!=='ENOENT') throw error;opaque=true;return;}
  files[name]=hash(bytes);
  if (!/\.(?:[cm]?js|ts)$/.test(name)) return;
  const require=createRequire(resolve(root,name));
  const installed=path => {
    try {
      const file=globalThis.Bun?.resolveSync ? Bun.resolveSync(path,posix.dirname(resolve(root,name))) : require.resolve(path);
      return realpathSync(file).startsWith(resolve(root,'node_modules')+'/');
    }
    catch {return false;}
  };
  const own=inspectModelDependencies(bytes.toString(),name,new Set(Object.keys(graph.modules)),installed,graph);
  opaque ||= own.opaque;
  for (const name of own.roots) roots.add(name);
  for (const path of own.files) {
    if (!path || path.startsWith('/') || path.split('/').includes('..')) {opaque=true;continue;}
    try {
      if (!(await stat(resolve(root,path))).isFile()) {opaque=true;continue;}
      files[path]=hash(await readFile(resolve(root,path)));
    } catch (error) {if(error.code!=='ENOENT') throw error;opaque=true;}
  }
  for (const child of own.imports) {
    if (child.startsWith(graph.outdir+'/')) {
      const source=graph.sources+'/'+child.slice(graph.outdir.length+1).replace(/\.js$/,'.imba');
      if (graph.modules[source]) roots.add(source);else opaque=true;
    } else if (/\.(?:[cm]?js|ts|json)$/.test(child)) await visit(child);
    else opaque=true;
  }
};
for (const file of request.files) await visit(file);
await writeFile(request.output,JSON.stringify({roots:[...roots].sort(),files,opaque}));
