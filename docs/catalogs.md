# Advanced catalogs and automatic leaves

Use `format: 2` for catalogs with prerequisites, domains, file leaves and shared lifecycle workers. The original fixture configuration (`format: 1`) remains supported.

```json
{
  "format": 2,
  "component": "backend",
  "stages": [
    {
      "name": "models",
      "group": "models",
      "command": ["bun", "test"],
      "tests": ["test/*.test.js"],
      "file_partition": "tests",
      "inputs": ["src", "test/helpers", "package.json", "bun.lock"],
      "outputs": [],
      "intents": ["dev", "release"]
    }
  ],
  "test_inventory": {"patterns": ["test/*.test.js"]},
  "release_requirements": ["models"]
}
```

Run `pbgate plan`, `pbgate run --intent dev --check models`, or `pbgate run --release`. Planning does not allocate state, locks, processes or databases. New test files matching a registered pattern become leaves automatically. A physical test has one release owner. A changed catalog or discovered inventory invalidates a plan that waited in the execution queue.

Each leaf has its own inputs, proof and result. Missing compatible file leaves share one argv invocation; valid leaves remain cached. A failed batch creates no success proofs. The example declares a conservative common source scope. File discovery alone cannot infer which source code a test exercises.

For exact PocketBase dependencies, `backend_runtime` declares routes and roots against the complete compiler graph. `model_partition: "unit_domains"` derives one leaf per registered model test; `unit_domains` retains meaningful domain ownership. `CatalogDependencies` validates the transitive static test descriptors, compiled bytes, source membership and analyzer implementation. Unknown branches, loaders, added executables or stale descriptors retain a broad scope. No successful request trace can substitute for a complete graph.

`file_checks` supplies different inputs/prerequisites for individual files or groups. `input_sets` shares declared inputs. HTTP leaves can declare `lifecycle` and be covered by a `member_runner` owner using `partition_by: "lifecycle"`; `receipt_by: "check"` preserves separate leaf proofs. The engine derives aggregate commands and dependencies from `covers`. `requires`, `domains`, `intents`, diagnostic arguments and historical rehearsals preserve distinct execution policies without losing release coverage.

## Integrating an existing workspace

`pocketbase-gate/python` exports `entrypoint`, the installed Python package's `engine/__init__.py`. Load it with `importlib.util.spec_from_file_location(..., submodule_search_locations=[...])`. Its public API exposes `VerificationCatalog`, `VerificationPlan`, `CatalogProject`, `CatalogGate`, `CatalogSession`, `CatalogDependencies`, `Content` and `ReceiptCache`.

`CatalogProject` supplies the default repository binding. A workspace adapter can supply its external inputs, effective parameters, release stamp location and command environment; call `CatalogGate(project).verify(...)` inside `CatalogSession`. Planning, cache validation, batching, failure priority, reports, cancellation and disposal belong to the package. The adapter never needs to start guards, acquire leases, publish receipts or close fixture workers.

`CatalogSession` can receive a `protocol` with `runner` and `worker` argv suffixes. The JSON-lines worker accepts `run` and `close`, asks for `admit` before allocating each scenario, and waits for `admitted`. The session registers all selected checks before lazy worker startup and disposes the worker at completion, cancellation or timeout. Ordinary commands and workers use the same process supervisor, resource monitor and machine lease. Commands have a default 120-second wall limit; `timeout_seconds` or project execution options can specify another limit.

`pocketbase-gate/dependency-scan` exports `scanDependencies` and `dependencyImplementation`. An explicitly declared `externalFiles` mapping binds a dynamic adapter's bytes to its external input owner; callers must supply and fingerprint those external inputs. Unregistered dynamic resolution remains opaque.

`pocketbase-gate/templates` exports `fixtureTemplate` for custom native fixtures. Supply binary, hooks, migrations, environment, disposable password, data directory, stopped preparation callback and cache roots. The implementation uses the same Python `ReceiptCache` and compiler dependency owner as the gate, verifies all files and empty directories, and serializes artifact publication across processes. Business seeding and fresh scenario state remain the fixture's responsibility. Bind custom driver files through `drivers` and scenario settings through `parameters`. Persistent schema baselines bind the UTC day; cached directories must always be stopped before publication.
