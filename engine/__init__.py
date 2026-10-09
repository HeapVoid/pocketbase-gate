"""Public Python integration for catalog-based consumers."""
from pathlib import Path
import hashlib
from . import runtime, catalog, dependencies
from .runtime import Content, ReceiptCache, Monitor, SystemSample, OwnedProcesses, ProcessSession, healthy, atomic_json, schedule, PROFILES, FOOTPRINT_LIMIT, CACHE_LIMIT, CONTENT_FILE_LIMIT
from .catalog import VerificationCatalog, VerificationPlan, ExecutionOrder, recipe, execution_command, package_inputs, digest, semantic_hash
from .catalog_session import CatalogSession
from .catalog_gate import CatalogGate

IMPLEMENTATION = Path(__file__).resolve().parent.parent
LOADED_HASHES = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in IMPLEMENTATION.glob('engine/*.py')}
def assert_current():
    if any(hashlib.sha256(Path(name).read_bytes()).hexdigest() != value for name, value in LOADED_HASHES.items()):
        raise RuntimeError('Verification tooling changed while queued; rerun with the current runner')
from .dependencies import CatalogDependencies
from .catalog_inputs import stage_inputs
from .catalog_cli import main as catalog_main
from .catalog_project import CatalogProject
