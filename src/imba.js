import {readdir, readFile, writeFile, mkdir, unlink} from 'node:fs/promises';
import {resolve, relative, join} from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {writeBackendDependencies} from './dependencies.js';

// Bimba owns Imba compilation. This adapter supplies the PocketBase output
// contract and removes only files produced by its own preceding build.
export async function compileHooks({sources = 'src', outdir = 'public', unwrapDefault = true} = {}) {
  if (!globalThis.Bun?.build) throw Error('compileHooks runs under Bun and uses the project-local bimba-cli');
  const root = process.cwd(), source = resolve(root, sources), output = resolve(root, outdir);
  if (output === root || relative(root, output).startsWith('..') || output === source) throw Error('Use a separate output directory below the project root');
  const require = createRequire(join(root, 'package.json'));
  const {imbaPlugin, setTarget} = await import(pathToFileURL(require.resolve('bimba-cli/plugin.js')));
  setTarget('node');
  const collect = async directory => {
    const files = [];
    for (const entry of await readdir(directory, {withFileTypes:true})) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) files.push(...await collect(path));
      else if (entry.isFile() && entry.name.endsWith('.imba')) files.push(path);
    }
    return files.sort();
  };
  const entrypoints = await collect(source);
  if (!entrypoints.length) throw Error('No Imba hook sources found');
  const external = {name:'pbgate-local-hooks', setup(build) {
    build.onResolve({filter:/^\.\.?\//}, args => {
      if (!String(args.importer).includes('/node_modules/')) return {path:args.path, external:true};
    });
  }};
  const result = await Bun.build({entrypoints, root:source, outdir:output, target:'node', format:'cjs',
    sourcemap:'none', minify:false, naming:'[dir]/[name].[ext]', plugins:[external,imbaPlugin]});
  if (!result.success) throw new AggregateError(result.logs, 'PocketBase hook compilation failed');
  const generated = [];
  for (const artifact of result.outputs) {
    const name = relative(output, artifact.path);
    generated.push(name);
    if (unwrapDefault && name.endsWith('.js') && !name.endsWith('.pb.js')) {
      const code = await readFile(artifact.path, 'utf8');
      await writeFile(artifact.path, code + '\nif (Object.prototype.hasOwnProperty.call(module.exports, "default")) module.exports = module.exports.default;\n');
    }
  }
  const manifest = join(output, '.pbgate-hooks.json');
  let previous = [];
  try { previous = JSON.parse(await readFile(manifest, 'utf8')).outputs; }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  for (const name of previous) {
    if (typeof name !== 'string' || name.startsWith('..') || resolve(output, name) === output || relative(output, resolve(output, name)).startsWith('..')) throw Error('Invalid preceding compiler output manifest');
    if (!generated.includes(name)) await unlink(join(output, name)).catch(error => {if (error.code !== 'ENOENT') throw error;});
  }
  await mkdir(output, {recursive:true});
  await writeFile(manifest, JSON.stringify({format:1, outputs:generated.sort()}) + '\n');
  await writeBackendDependencies(entrypoints, result.outputs, root, {sources:relative(root,source), outdir:relative(root,output)});
  return {files:generated.length};
}
