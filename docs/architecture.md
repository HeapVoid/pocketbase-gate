# Architecture

The public contract is a check declaration and its prepared test context.

`Project` owns catalog normalization, inventory and complete required coverage. `Plan` owns the selected dependency closure. `Gate` owns scheduling, content identities, verdicts and reports. `ProcessSession` owns the machine lease, resource admission, registered process groups, temporary directories and cancellation. `Content` and `ReceiptCache` own exact fingerprints and persisted proofs. `PocketBasePool` owns fixture startup, compatible runtime reuse, verified restoration and shutdown. `compileHooks` delegates compilation to project-local Bimba; it owns only the PocketBase artifact convention.

The shared scheduler consumes normalized checks. It has no names of business collections, application routes or project-specific scripts. Fixtures are selected explicitly by a declaration rather than inferred from argv patterns. A test receives `TestContext.current()` after the fixture is ready and does not orchestrate preparation or cleanup.

The process guard is retained from the existing verification owner. Each group waits for confirmed registration before executing. A pipe lease plus parent-exit kernel events trigger cleanup if the supervisor or its host disappears. The guard inherits the machine lock and releases it after owned process/data cleanup.

`DependencyGraph` validates compiler source membership, source/output hashes and the analyzer identity. Complete static test descriptors follow all branches, imports and literal reads; unknown operations retain broad invalidation. Inline route callbacks have separate hashes from startup/record hooks so unrelated endpoint edits do not invalidate a selected route or prepared schema. A preparation prerequisite binds its recipe and relevant outputs instead of imposing unrelated source changes on a precise test.

`ReceiptCache` also owns stopped database artifacts, their complete inventories, private metadata, atomic publication and the shared eviction budget. `PocketBasePool` supplies the bootstrap scope and reconstructs a separate runtime from a stopped baseline. First-boot tests disable baseline reuse; seed actions always run fresh. Bootstrap-sensitive time is bound to the UTC date.

Process ownership uses both registered groups and inherited random markers. The guard tracks detached descendants and keeps the machine lease through cleanup after supervisor/host loss. Gate commands disable Bimba's persistent typecheck daemon and inherit the selected OS scheduling priority.

Native regression tests exercise a separate project with its own schema and Imba sources, including partial invalidation, baseline corruption, schema isolation and shared record/storage/store restoration.

Further extraction should preserve the existing dependency/fixture contracts while adding:

1. Measured scheduling priorities from historical failure, fixture preparation and execution cost.
2. Migration of Questfall's catalogs and helpers to this package, followed by removal of superseded copied executors.

Questfall's current gate remains independent during its parallel backend work. The package is not yet its authoritative release runner. Consumer migration must preserve its catalog coverage, lifecycle workers and release policy.
