# PocketBase Gate

Resource-aware verification for PocketBase + Imba projects. Projects declare checks and fixture requirements; the gate owns planning, receipts, process supervision, isolation and cleanup.

## Install

```sh
bun add --dev pocketbase-gate
```

Runtime requirements: macOS or Linux, Node 20+ and Python 3.9+. Imba compilation additionally uses Bun and the consumer's installed `bimba-cli`. PocketBase fixtures use an explicitly configured local binary (native controls are tested on PocketBase 0.40.4). The package never downloads a binary or starts an application server automatically.

Advanced workspaces can use [catalogs with automatic file leaves and lifecycle workers](docs/catalogs.md). The same installed engine provides the Python integration, exact dependency scanner and stopped-fixture artifact store.

## Declare checks

Create `pbgate.json` in the project root:

```json
{
  "format": 1,
  "tools": ["bun"],
  "fixtures": {
    "api": {
      "binary": "_pocketbase/pocketbase",
      "hooks": "public",
      "migrations": "pb_migrations"
    }
  },
  "checks": [
    {
      "id": "compile",
      "kind": "prepare",
      "command": ["bun", "scripts/compile.js"],
      "inputs": ["src", "scripts/compile.js", "bunfig.toml"],
      "outputs": ["public"],
      "restore": true
    },
    {
      "id": "notes-api",
      "command": ["bun", "test/notes.js"],
      "tests": ["test/notes.js"],
      "requires": ["compile"],
      "domains": ["notes"],
      "dependencies": {"routes": ["/api/notes"]},
      "fixture": "api",
      "isolation": "fresh"
    }
  ],
  "testInventory": ["test/*.js"],
  "required": ["compile", "notes-api"]
}
```

`scripts/compile.js` delegates to the existing Bimba compiler through the stack adapter:

```js
import {compileHooks} from 'pocketbase-gate/imba';
await compileHooks({sources:'src', outdir:'public'});
```

`test/notes.js` uses the prepared context:

```js
import assert from 'node:assert/strict';
import {TestContext} from 'pocketbase-gate';

const context = TestContext.current();
const notes = await context.request('/api/collections/notes/records');
assert.equal(notes.totalItems, 0);
```

Create the `notes` collection in your project migrations. The fixture applies those migrations in its own temporary directory, creates a disposable superuser and exposes its credentials only through a private context file. Business records can be seeded with a fixture `seed` argv command using the same `TestContext`.

## Plan and execute

```sh
# Validate the catalog and inspect dependencies. No processes, lock or hashing.
pbgate plan
pbgate plan --intent dev --domain notes

# Fresh targeted behavior check; reusable preparation remains available.
pbgate run --intent dev --check notes-api

# Explicit full release verification; unchanged checks can use exact receipts.
pbgate run --release
pbgate run --release --force
```

The default `quiet` profile permits one check at a time, uses background scheduling and limits disposable PocketBase's Go concurrency. `--profile fast` permits two compatible checks. Prerequisites, explicit `exclusive` resource names and output read/write conflicts remain serialized. A machine-wide lease prevents separate project gates from piling up heavy runs; processes inside a gate still follow its profile. Resource admission requires three healthy samples and has a bounded wait.

Defaults: CPU idle at least 30%, swap-out at most 16 MiB/s, admission timeout 300 seconds, owned memory budget 6 GiB, receipt/artifact budget 2 GiB. Override these through the config's `resources`. Scheduling priorities and sampled memory supervision are not operating-system hard CPU/RAM quotas.

Every check has a wall-time limit (default 120 seconds). PocketBase startup has its own bound, and fixture preparation is included in the check's elapsed budget. Cancellation, timeout and loss of the supervisor dispose owned process groups and temporary data. An inherited ownership marker also tracks detached descendants that leave their original group. Bimba's typecheck daemon is disabled for gate commands in both profiles; descendants inherit scheduling priority. Existing dev servers retain their own ownership.

## Dependency and cache contract

- `requires` defines the dependency graph. `inputs` and reusable `inputSets` define the complete read scope. Without explicit inputs, the gate conservatively binds common source/test/script/config directories.
- Recipes, tool binaries, installed `node_modules`, effective inherited environment and gate implementation participate in content keys. Environment values are hashed; reports do not contain them.
- Default tests reuse successful results only during release verification. Targeted development tests execute fresh; `kind: "prepare"` can reuse preparation. Set `cache: false` for checks that read undeclared external state such as a live chain or service.
- Output hashes are checked before accepting a receipt. `restore: true` additionally saves and restores declared generated outputs. Keep output directories separate from maintained source files.
- Changes, directory additions and metadata changes during a run prevent success publication. A later failed check preserves only stable, completed successful receipts. A partial plan never becomes proof of the full required gate.
- `testInventory` rejects new unregistered test files and duplicate release ownership. `required` preserves the expected release coverage.

`compileHooks` writes a validated compiler descriptor. A check's `dependencies` can select route prefixes (`{"routes":["/api/notes"]}`) or model roots (`{"mode":"models","roots":["src/models/note.imba"]}`). The analyzer follows imports, model loaders and literal file reads across every branch, including branches the test did not execute. Bootstrap and record hooks remain common dependencies; unrelated inline route bodies can change without invalidating a narrow route proof or stopped schema baseline. Test/helper descriptors are also cached and validated against their complete file contents.

Missing, stale or opaque descriptors retain the broad scope. Dynamic paths and unknown loaders cannot establish a narrow proof. Keep additional non-code dependencies in `inputs`; when a descriptor is valid, it replaces broad source/output directory entries with the selected scope. Installed packages and compiler configuration remain bound to the proof.

Time-dependent checks must declare the relevant time as an input or disable result caching. PocketBase fixture proofs and baselines bind the current UTC date. The gate does not infer application-specific historical or external-state semantics.

## PocketBase isolation

`fresh` is the default: each check gets a separate runtime and data directory. Use it for restart, schema mutation, cron changes, native faults and module state that cannot be restored.

Fixtures reuse a verified, stopped baseline rather than repeating database preparation. The default `baseline: "initial"` caches migrations and disposable superuser setup. Set `baseline: "schema"` to cache one completed bootstrap as well, when bootstrap is idempotent and repeating it over prepared data is valid. Set `baseline: false` for first-boot behavior. Binary, migrations, bootstrap dependencies, declared inputs, environment, engine and UTC date bind the key. Corrupt artifacts rebuild; changing an unrelated route body preserves a complete bootstrap scope. Each runtime receives a separate copy, and `seed` always runs fresh after restoration. Baselines share the bounded artifact budget and keep disposable credentials in private metadata.

`shared` explicitly opts compatible checks into one session-owned runtime. The engine checkpoints all main-database tables and trigger definitions, the baseline storage files and application store. After each scenario it restores records transactionally, verifies schema and rows, reloads collection/settings caches and restores storage and store. Shared scenarios run sequentially even in `fast`.

Shared fixtures stop cron at the checkpoint. They do not reset auxiliary databases, external services, native listeners or CommonJS module state. Tests relying on these use `fresh`. Declare `nativeStoreKeys` for baseline application-store entries containing native objects; other entries must be JSON-serializable. Schema changes in a shared scenario reject success.

There is no deploy action: this package verifies the candidate. Production switching, maintenance windows and application-specific data audits remain with the project.

## Development

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.js
PBGATE_TEST_BINARY=/absolute/path/pocketbase npm run test:native
# Also exercise real Imba compilation in a separate consumer directory:
PBGATE_TEST_BINARY=/absolute/path/pocketbase \
  PBGATE_TEST_BIMBA=/absolute/path/node_modules/bimba-cli npm run test:native
npm pack --dry-run
```

The engine retains the established Python content/resource/process owners extracted from HeapVoid's workspace verification system. PocketBase preparation and Bimba compilation are stack adapters; application names, routes, schema and deployment rules are absent from the core. The same npm artifact supplies CLI, adapters and client context.

See [architecture](docs/architecture.md) for ownership and next development steps. Config fields are described in [schema.json](schema.json).

## Publishing

Update `package.json` and `package-lock.json`, commit the version, then push its `v<version>` tag. The GitHub workflow runs the same Python, JavaScript and native PocketBase/Imba checks used on branches, verifies the tag against the package version and publishes to npm using Trusted Publishing. Stable versions use `latest`; prereleases use `next`. Branch pushes run verification only.

The npm trusted publisher is `HeapVoid/pocketbase-gate`, workflow `publish.yml`. It uses GitHub's short-lived OIDC identity and requires no repository npm token. A package must exist before its first trusted publisher can be configured; the initial registry publication is bootstrapped once locally.
