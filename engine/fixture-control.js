// Installed only in the gate's private temporary hooks directory.
// The callback is self-contained because PocketBase isolates hook contexts.
function fixtureControl(e) {
  const app = e.app, owner = $os.getenv('PBGATE_FIXTURE_OWNER');
  if (!owner || !e.hasSuperuserAuth() || e.requestInfo().body.owner !== owner) throw new ForbiddenError();
  const input = e.requestInfo().body, key = 'pbgate.fixture', prefix = '__pbgate_baseline_';
  const quote = name => '"' + name.replaceAll('"', '""') + '"';
  const schema = tx => {
    const rows = arrayOf(new DynamicModel({type:'', name:'', tbl_name:'', sql:''}));
    tx.db().newQuery("SELECT type,name,tbl_name,COALESCE(sql,'') AS sql FROM sqlite_schema WHERE name NOT LIKE '__pbgate_baseline_%' AND name NOT LIKE 'sqlite_%' ORDER BY type,name").all(rows);
    return JSON.parse(JSON.stringify(rows)).map(row => [row.type,row.name,row.tbl_name,row.sql]);
  };
  if (input.operation === 'checkpoint') {
    if (app.store().has(key)) throw new BadRequestError('Fixture already checkpointed');
    app.cron().stop();
    const definition = schema(app), tables = definition.filter(row => row[0] === 'table').map(row => row[1]);
    const sequence = new DynamicModel({present:0});
    app.db().newQuery("SELECT COUNT(*) AS present FROM sqlite_schema WHERE name='sqlite_sequence'").one(sequence);
    if (sequence.present) tables.push('sqlite_sequence');
    app.runInTransaction(tx => {
      for (const name of tables) tx.db().newQuery('CREATE TABLE ' + quote(prefix + name) + ' AS SELECT * FROM ' + quote(name)).execute();
    });
    const native = JSON.parse($os.getenv('PBGATE_NATIVE_STORE_KEYS') || '[]'), store = {};
    for (const [name,value] of Object.entries(app.store().getAll())) {
      store[name] = native.includes(name) ? {native:true,value} : {native:false,value:JSON.stringify(value)};
      if (!store[name].native && store[name].value === undefined) throw new BadRequestError('Declare native store keys or use fresh isolation');
    }
    app.store().set(key, {definition,tables,store});
    return e.json(200, {tables:tables.length});
  }
  const state = app.store().get(key);
  if (!state || input.operation !== 'reset') throw new BadRequestError('Unknown fixture operation');
  if (JSON.stringify(schema(app)) !== JSON.stringify(state.definition)) throw new BadRequestError('Shared scenario changed schema; use fresh isolation');
  app.runInTransaction(tx => {
    const triggers = state.definition.filter(row => row[0] === 'trigger');
    for (const trigger of triggers) tx.db().newQuery('DROP TRIGGER ' + quote(trigger[1])).execute();
    for (const name of state.tables) tx.db().newQuery('DELETE FROM ' + quote(name)).execute();
    for (const name of state.tables) tx.db().newQuery('INSERT INTO ' + quote(name) + ' SELECT * FROM ' + quote(prefix + name)).execute();
    for (const trigger of triggers) tx.db().newQuery(trigger[3]).execute();
    if (JSON.stringify(schema(tx)) !== JSON.stringify(state.definition)) throw new BadRequestError('Reset changed schema');
    for (const name of state.tables) {
      const result = new DynamicModel({changed:0}), current = quote(name), baseline = quote(prefix + name);
      tx.db().newQuery('SELECT (SELECT COUNT(*) FROM ' + current + ') != (SELECT COUNT(*) FROM ' + baseline + ') OR EXISTS(SELECT * FROM ' + current + ' EXCEPT SELECT * FROM ' + baseline + ') OR EXISTS(SELECT * FROM ' + baseline + ' EXCEPT SELECT * FROM ' + current + ') AS changed').one(result);
      if (result.changed) throw new BadRequestError('Reset differs from baseline: ' + name);
    }
  });
  app.reloadCachedCollections();
  app.reloadSettings();
  const restored = {};
  for (const [name,entry] of Object.entries(state.store)) restored[name] = entry.native ? entry.value : JSON.parse(entry.value);
  app.store().reset(restored);
  app.store().set(key,state);
  return e.json(200, {tables:state.tables.length});
}
routerAdd('POST', '/__pbgate__', fixtureControl);
