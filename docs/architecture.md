# Architecture

The public contract is a check declaration and its prepared test context.

`Project` owns catalog normalization, inventory and complete required coverage. `Plan` owns the selected dependency closure. `Gate` owns scheduling, content identities, verdicts and reports. `ProcessSession` owns the machine lease, resource admission, registered process groups, temporary directories and cancellation. `Content` and `ReceiptCache` own exact fingerprints and persisted proofs. `PocketBasePool` owns fixture startup, compatible runtime reuse, verified restoration and shutdown. `compileHooks` delegates compilation to project-local Bimba; it owns only the PocketBase artifact convention.

The shared scheduler consumes normalized checks. It has no names of business collections, application routes or project-specific scripts. Fixtures are selected explicitly by a declaration rather than inferred from argv patterns. A test receives `TestContext.current()` after the fixture is ready and does not orchestrate preparation or cleanup.

The process guard is retained from the existing verification owner. Each group waits for confirmed registration before executing. A pipe lease plus parent-exit kernel events trigger cleanup if the supervisor or its host disappears. The guard inherits the machine lock and releases it after owned process/data cleanup.

Initial scope includes arbitrary argv checks, dependency planning, conservative content receipts, generated artifact restoration, two resource profiles, timeouts, failure recovery, fresh/shared PocketBase fixtures and Bimba hook compilation. Native regression tests exercise a separate project with its own schema and Imba sources.

Further extraction should preserve the existing dependency/fixture contracts while adding:

1. Complete compiler dependency descriptors to narrow source invalidation safely; unavailable or opaque descriptors retain the broad scope.
2. Prepared stopped baseline caching for fresh fixtures, keyed by every bootstrap dependency and relevant time input.
3. Measured scheduling priorities from historical failure, fixture preparation and execution cost.
4. Migration of Questfall's catalogs and helpers to this package, followed by removal of superseded copied executors.

Questfall's current gate remains independent during its parallel backend work. The package is not yet its authoritative release runner. npm publication and consumer migration are separate steps from creating this repository.
