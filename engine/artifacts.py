"""Private JSON bridge for JavaScript stopped-fixture artifacts."""
import json
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from runtime import Content, ReceiptCache
from dependencies import DependencyGraph

request = json.load(sys.stdin)
content = Content()
operation = request['operation']
if operation == 'preparation':
    root = Path(request['root'])
    graph_owner = DependencyGraph(SimpleNamespace(root=root), SimpleNamespace(content=content))
    result = graph_owner.preparation(request['outdir'], manifest=request.get('manifest','.pbgate-dependencies.json'))
elif operation == 'inventory':
    result = ReceiptCache(request['cache'], content).directory_inventory(request['source'])
else:
    cache = ReceiptCache(request['cache'], content)
    if operation == 'restore':
        destination = Path(request['destination'])
        temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex)
        result = cache.restore_directory(request['key'], temporary)
        if result is not None:
            shutil.rmtree(destination, ignore_errors=True)
            temporary.rename(destination)
    elif operation == 'save':
        cache.save_directory(request['key'], request['source'], request['metadata'],
                             cache.directory / (request['key'] + '.' + uuid.uuid4().hex))
        result = True
    else:
        raise ValueError('Unknown artifact operation')
json.dump(result,sys.stdout)
