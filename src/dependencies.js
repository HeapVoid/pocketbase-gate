import ts from 'typescript';
import {createHash} from 'node:crypto';
import {readFile, writeFile, rename, mkdir} from 'node:fs/promises';
import {isBuiltin} from 'node:module';
import {posix, relative} from 'node:path';

const hash = bytes => createHash('sha256').update(bytes).digest('hex');

// Inspect compiler output, including requires inside callbacks and branches.
// Observed module loads cannot prove dependencies of an unvisited branch.
export function inspectDependencies(code, source, available, {sources='src', outdir='public'}={}) {
	const tree = ts.createSourceFile(source, code, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
	const global = {roots:new Set(), opaque:tree.parseDiagnostics.length > 0};
	const routes = [];
	const request_hashes = [];
	const registrations = [];
	const value = node => {
		if (!node) return null;
		if (ts.isStringLiteralLike(node)) return node.text;
		if (ts.isIdentifier(node) && node.text === '__hooks') return '@hooks';
		if (ts.isParenthesizedExpression(node)) return value(node.expression);
		if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
			const a=value(node.left), b=value(node.right);
			return a === null || b === null ? null : a+b;
		}
		if (ts.isTemplateExpression(node)) {
			let result=node.head.text;
			for (const span of node.templateSpans) {
				const part=value(span.expression);
				if (part === null) return null;
				result+=part+span.literal.text;
			}
			return result;
		}
		return null;
	};
	const requirePath = (name, scope) => {
		if (name === null) {scope.opaque=true; return;}
		name=name.replace('{__hooks}', '@hooks');
		let target;
		if (name.startsWith('@hooks/')) target=sources+'/'+name.slice(7);
		else if (name.startsWith('.')) target=posix.join(posix.dirname(source), name);
		else if (name.startsWith('/') || name.includes('@hooks')) {scope.opaque=true; return;}
		else return; // Installed packages are already part of every stage key.
		if (target.startsWith(outdir+'/')) target=sources+'/'+target.slice(outdir.length+1);
		const candidates=[target, target.replace(/\.js$/, '.imba'), target+'.imba', target+'.js'];
		const resolved=candidates.find(path=>available.has(path));
		if (resolved) scope.roots.add(resolved);
		else scope.opaque=true;
	};
	const choices = node => {
		if (ts.isParenthesizedExpression(node)) return choices(node.expression);
		if (ts.isConditionalExpression(node)) {
			const a=choices(node.whenTrue),b=choices(node.whenFalse);
			return a && b ? [...a,...b] : null;
		}
		if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
			const a=choices(node.left),b=choices(node.right);
			return a && b && a.length*b.length <= 64 ? a.flatMap(x=>b.map(y=>x+y)) : null;
		}
		const result=value(node);
		return result === null ? null : [result];
	};
	const visit = (node, scope, registration=false) => {
		if ((ts.isCallExpression(node) || ts.isNewExpression(node)) &&
			ts.isIdentifier(node.expression) && ['eval','Function'].includes(node.expression.text)) scope.opaque=true;
		if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
			if (node.expression.text === 'routerAdd' && registration) {
				const method=value(node.arguments[0]), path=value(node.arguments[1]);
				if (method && path?.startsWith('/')) {
					const route={method, path, roots:new Set(), opaque:false};
					request_hashes.push(hash(node.getText(tree)));
					for (const argument of node.arguments.slice(2))
						visit(argument, ts.isFunctionExpression(argument) || ts.isArrowFunction(argument) ? route : global);
					routes.push(route);
					return;
				}
				scope.opaque=true;
			}
			if (node.expression.text === 'require') {
				const names=node.arguments[0] ? choices(node.arguments[0]) : null;
				if (names) for (const name of names) requirePath(name, scope);
				else scope.opaque=true;
			}
		}
		// Escaped/aliased require cannot be resolved safely. typeof require is a
		// bundler availability check; direct calls are handled above.
		if (ts.isIdentifier(node) && node.text === 'require' &&
			!(ts.isCallExpression(node.parent) && node.parent.expression === node) &&
			!ts.isTypeOfExpression(node.parent)) scope.opaque=true;
		ts.forEachChild(node, child=>visit(child, scope, false));
	};
	for (const statement of tree.statements) {
		const call = ts.isExpressionStatement(statement) ? statement.expression : null;
		// Inline route callbacks run on requests; middleware factories and all
		// other top-level code can participate in native bootstrap.
		registrations.push(source.endsWith('.pb.imba') && call && ts.isCallExpression(call) &&
			ts.isIdentifier(call.expression) && call.expression.text === 'routerAdd' && value(call.arguments[0]) && value(call.arguments[1])
			? 'routerAdd(' + call.arguments.map(arg=>ts.isFunctionExpression(arg) || ts.isArrowFunction(arg) ? '<request callback>' : arg.getText(tree)).join(',') + ')'
			: statement.getText(tree));
		if (ts.isExpressionStatement(statement)) visit(statement.expression, global, source.endsWith('.pb.imba'));
		else visit(statement, global);
	}
	const serialize = scope => ({...scope, roots:[...scope.roots].sort()});
	return {global:serialize(global), routes:routes.map(serialize), request_hashes, startup_hash:hash(JSON.stringify(registrations))};
}

// All native registrations and their transitive dependencies can affect the
// initial database. Opaque loading retains the complete compiled input set.
function preparationInputs(modules, hooks, outdir) {
	const selected = new Set(hooks), pending = [...hooks];
	while (pending.length) {
		const module = modules[pending.pop()];
		if (module.opaque) return null;
		for (const name of module.roots) if (!selected.has(name)) {selected.add(name);pending.push(name);}
	}
	return Object.fromEntries([...selected].sort().map(name=>[modules[name].output.slice(outdir.length+1),
		hooks.includes(name) ? modules[name].startup_hash : modules[name].output_hash]));
}

// Test hooks are JavaScript embedded in fixture strings. Inspect every body,
// not only routes reached in a successful run. Dynamic construction fails closed.
export function inspectFixtureDependencies(code, source, available, options={}) {
	const tree=ts.createSourceFile(source,code,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS);
	const roots=new Set();let opaque=tree.parseDiagnostics.length > 0;
	const inspect = text => {
		if (!/\brequire\b/.test(text)) return;
		const result=inspectDependencies(text,source,available,options);
		for (const scope of [result.global,...result.routes]) {
			opaque ||= scope.opaque;
			for (const root of scope.roots) roots.add(root);
		}
	};
	const visit = node => {
		if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text==='require') inspect(node.getText(tree));
		if (ts.isStringLiteralLike(node)) inspect(node.text);
		if (ts.isTemplateExpression(node)) {
			const text=node.head.text+node.templateSpans.map(span=>'undefined'+span.literal.text).join('');
			// An interpolation can change the loader argument or inject native code.
			if (/\brequire\b/.test(text)) opaque=true;
			inspect(text);
		}
		if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text==='load' && node.arguments[0] && ts.isStringLiteralLike(node.arguments[0])) {
			const root=(options.sources||'src')+'/'+node.arguments[0].text.replace(/\.js$/,'.imba');
			if (available.has(root)) roots.add(root);else opaque=true;
		}
		ts.forEachChild(node,visit);
	};
	visit(tree);
	return {roots:[...roots].sort(),opaque};
}

// Model dependencies are inspected across every branch, including embedded
// native callbacks. Unsupported loaders/read paths retain the broad input set.
export function inspectModelDependencies(code, source, available, installed=()=>false, {sources='src', outdir='public'}={}) {
	const tree=ts.createSourceFile(source,code,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS);
	const roots=new Set(),imports=new Set(),files=new Set(),externals=new Set(),loaders=new Set(['load']);
	const processes=new Set(),processNamespaces=new Set(['Bun','process']);
	const readers=new Set(['readFile','readFileSync','openSync','readdir','readdirSync']);
	let opaque=tree.parseDiagnostics.length>0;
	const constants=new Map(), ambiguous=new Set(),mutated=new Set();
	const collect=node=>{
		if(ts.isParameter(node)&&ts.isIdentifier(node.name))ambiguous.add(node.name.text);
		if(ts.isBinaryExpression(node)&&node.operatorToken.kind>=ts.SyntaxKind.FirstAssignment&&node.operatorToken.kind<=ts.SyntaxKind.LastAssignment&&ts.isIdentifier(node.left))mutated.add(node.left.text);
		if((ts.isPrefixUnaryExpression(node)||ts.isPostfixUnaryExpression(node))&&ts.isIdentifier(node.operand))mutated.add(node.operand.text);
		if(ts.isVariableDeclaration(node)&&ts.isIdentifier(node.name)) {
			const name=node.name.text;
			constants.set(name,constants.has(name)?null:node.initializer);
		}
		if(ts.isImportDeclaration(node)&&node.importClause?.namedBindings&&ts.isNamedImports(node.importClause.namedBindings))
			for(const item of node.importClause.namedBindings.elements) {
				if((item.propertyName||item.name).text==='load')loaders.add(item.name.text);
				if(/^(node:)?fs(?:\/promises)?$/.test(node.moduleSpecifier.text)) {
					const name=(item.propertyName||item.name).text;
					if(readers.has(name)||name==='open')readers.add(item.name.text);else opaque=true;
				}
				if(/^(node:)?child_process$/.test(node.moduleSpecifier.text))processes.add(item.name.text);
			}
		if(ts.isImportDeclaration(node)&&/^(node:)?child_process$/.test(node.moduleSpecifier.text)) {
			if(node.importClause?.name)processNamespaces.add(node.importClause.name.text);
			if(node.importClause?.namedBindings&&ts.isNamespaceImport(node.importClause.namedBindings))processNamespaces.add(node.importClause.namedBindings.name.text);
		}
		if(ts.isImportDeclaration(node)&&/^(node:)?fs(?:\/promises)?$/.test(node.moduleSpecifier.text)&&
			(node.importClause?.name||!node.importClause?.namedBindings||ts.isNamespaceImport(node.importClause.namedBindings)))opaque=true;
		if(ts.isImportDeclaration(node)&&node.importClause?.namedBindings&&ts.isNamespaceImport(node.importClause.namedBindings)&&node.moduleSpecifier.text.endsWith('/hooks.js'))opaque=true;
		ts.forEachChild(node,collect);
	};collect(tree);
	for(const name of [...ambiguous,...mutated])constants.set(name,null);
	const hookHelper=
		constants.get('root')?.getText(tree)==='process.cwd()'&&
		[`join(root, "${outdir}")`,`join(root, '${outdir}')`].includes(constants.get('hooks')?.getText(tree));
	const value=(node,seen=new Set())=>{
		if(!node)return null;
		if(ts.isStringLiteralLike(node))return node.text;
		if(ts.isParenthesizedExpression(node))return value(node.expression,seen);
		if(ts.isPropertyAccessExpression(node)&&node.name.text==='href'&&ts.isCallExpression(node.expression)&&node.expression.expression.getText(tree)==='pathToFileURL')return value(node.expression.arguments[0],seen);
		if(ts.isIdentifier(node)) {
			if(node.text==='__hooks'||(hookHelper&&node.text==='hooks'))return '@hooks';
			if(hookHelper&&node.text==='root')return '@root';
			if(seen.has(node.text))return null;
			return value(constants.get(node.text),new Set([...seen,node.text]));
		}
		if(ts.isBinaryExpression(node)&&node.operatorToken.kind===ts.SyntaxKind.PlusToken) {
			const a=value(node.left,seen),b=value(node.right,seen);return a===null||b===null?null:a+b;
		}
		if(ts.isCallExpression(node)&&ts.isIdentifier(node.expression)&&['join','resolve'].includes(node.expression.text)) {
			const parts=node.arguments.map(arg=>value(arg,seen));return parts.includes(null)?null:posix.join(...parts);
		}
		if(ts.isNewExpression(node)&&ts.isIdentifier(node.expression)&&node.expression.text==='URL'&&node.arguments?.[1]?.getText(tree)==='import.meta.url') {
			const path=value(node.arguments[0],seen);return path===null?null:posix.join(posix.dirname(source),path);
		}
		return null;
	};
	const combine=(a,b)=>a&&b&&a.length*b.length<=64?a.flatMap(x=>b.map(y=>x+y)):null;
	// Literal lists, loops and callback parameters describe the same model
	// paths as a direct call. Do not infer values from a visited runtime branch.
	const choices=node=>{
		if(!node)return null;
		if(ts.isParenthesizedExpression(node))return choices(node.expression);
		if(ts.isIdentifier(node)) {
			if(mutated.has(node.text))return null;
			for(let scope=node.parent;scope;scope=scope.parent) {
				let list;
				if(ts.isArrowFunction(scope)&&scope.parameters.some(parameter=>parameter.name.getText(tree)===node.text)) {
					const call=scope.parent;
					if(ts.isCallExpression(call)&&ts.isPropertyAccessExpression(call.expression)&&['map','forEach','flatMap'].includes(call.expression.name.text))list=call.expression.expression;
					else return null;
				} else if(ts.isForOfStatement(scope)&&ts.isVariableDeclarationList(scope.initializer)&&scope.initializer.declarations.some(declaration=>declaration.name.getText(tree)===node.text))list=scope.expression;
				if(list)return ts.isArrayLiteralExpression(list)&&list.elements.every(ts.isStringLiteralLike)?list.elements.map(item=>item.text):null;
			}
		}
		if(ts.isBinaryExpression(node)&&node.operatorToken.kind===ts.SyntaxKind.PlusToken)return combine(choices(node.left),choices(node.right));
		if(ts.isConditionalExpression(node)) {
			const a=choices(node.whenTrue),b=choices(node.whenFalse);return a&&b?[...a,...b]:null;
		}
		if(ts.isTemplateExpression(node)) {
			let result=[node.head.text];
			for(const span of node.templateSpans)result=combine(result,choices(span.expression)?.map(value=>value+span.literal.text));
			return result;
		}
		if(ts.isNewExpression(node)&&node.expression.getText(tree)==='URL'&&node.arguments?.[1]?.getText(tree)==='import.meta.url')
			return choices(node.arguments[0])?.map(path=>posix.join(posix.dirname(source),path))||null;
		const result=value(node);return result===null?null:[result];
	};
	const owned=(path,model=false)=>{
		if(path===null){opaque=true;return;}
		if(model)path='@hooks/'+path;
		// Resolve package owners against the actual importing file. An alias or
		// external symlink is not proof that node_modules owns the dependency.
		if(isBuiltin(path)||path.startsWith('bun:')||installed(path))return;
		if(path.startsWith('@hooks/')||path.startsWith(outdir+'/')) {
			const root=sources+'/'+(path.startsWith('@hooks/')?path.slice(7):path.slice(outdir.length+1)).replace(/\.js$/,'.imba');
			if(available.has(root))roots.add(root);else opaque=true;
		} else if(path.startsWith('@root/'))files.add(path.slice(6));
		else if(path.startsWith('.'))imports.add(posix.join(posix.dirname(source),path));
		else if(path.startsWith(sources+'/'))files.add(path);
		else if(path.startsWith('/')||path.includes('@hooks'))opaque=true;
		else opaque=true;
	};
	const ownChoices=(node,model=false)=>{
		const paths=choices(node);if(!paths){opaque=true;return;}
		for(const path of paths)owned(path,model);
	};
	const trustedLoader=node=>{
		if(!hookHelper||mutated.has('path')||value(constants.get('file'))!==null)return false;
		let assignment=node;
		while(assignment&&!ts.isExpressionStatement(assignment))assignment=assignment.parent;
		if(assignment&&ts.isBinaryExpression(assignment.expression)&&assignment.expression.left.getText(tree)==='load.cached'&&
			assignment.expression.operatorToken.kind===ts.SyntaxKind.EqualsToken&&
			ts.isArrowFunction(assignment.expression.right)&&assignment.expression.right.parameters.length===1&&
			assignment.expression.right.parameters[0].getText(tree)==='path'&&node.arguments[0]?.getText(tree)==='join(hooks, path)')return true;
		let parent=node;
		while(parent&&!ts.isVariableDeclaration(parent))parent=parent.parent;
		return parent?.name?.getText(tree)==='load'&&ts.isArrowFunction(parent.initializer)&&
			parent.initializer.parameters.length===1&&parent.initializer.parameters[0].getText(tree)==='path'&&
			constants.get('file')?.getText(tree)==='join(hooks, path)'&&
			node.arguments[0]?.getText(tree)==='file';
	};
	const mappedLoad=node=>{
		const call=node.parent;
		return ts.isCallExpression(call)&&call.arguments.length===1&&call.arguments[0]===node&&
			ts.isPropertyAccessExpression(call.expression)&&['map','forEach'].includes(call.expression.name.text)&&
			ts.isArrayLiteralExpression(call.expression.expression)&&call.expression.expression.elements.every(ts.isStringLiteralLike);
	};
	const visit=node=>{
		if(ts.isImportDeclaration(node)||ts.isExportDeclaration(node)) {
			if(node.moduleSpecifier&&ts.isStringLiteralLike(node.moduleSpecifier))owned(node.moduleSpecifier.text);
		}
		if((ts.isCallExpression(node)||ts.isNewExpression(node))&&ts.isIdentifier(node.expression)&&['eval','Function'].includes(node.expression.text))opaque=true;
		if(ts.isCallExpression(node)) {
			const name=ts.isIdentifier(node.expression)?node.expression.text:null;
			if(name==='createRequire'&&!(ts.isVariableDeclaration(node.parent)&&node.parent.name.getText(tree)==='require'&&node.arguments[0]?.getText(tree)==='import.meta.url'))opaque=true;
			const requireCall=name==='require'||(ts.isPropertyAccessExpression(node.expression)&&node.expression.expression.getText(tree)==='require'&&node.expression.name.text==='resolve');
			if(requireCall&&!trustedLoader(node))ownChoices(node.arguments[0]);
			if(requireCall&&/^(node:)?(?:fs(?:\/promises)?|child_process)$/.test(value(node.arguments[0])||''))opaque=true;
			if(name&&loaders.has(name))ownChoices(node.arguments[0],true);
			if(ts.isPropertyAccessExpression(node.expression)&&ts.isIdentifier(node.expression.expression)&&
				loaders.has(node.expression.expression.text)&&node.expression.name.text==='cached')ownChoices(node.arguments[0],true);
			if(processes.has(name)||(ts.isPropertyAccessExpression(node.expression)&&processNamespaces.has(node.expression.expression.getText(tree))&&['spawn','spawnSync','exec','execSync','execFile','execFileSync','chdir'].includes(node.expression.name.text)))opaque=true;
			if(node.expression.kind===ts.SyntaxKind.ImportKeyword)ownChoices(node.arguments[0]);
			const reader=name|| (ts.isPropertyAccessExpression(node.expression)?node.expression.name.text:'');
			if(readers.has(reader)||(reader==='file'&&ts.isPropertyAccessExpression(node.expression)&&node.expression.expression.getText(tree)==='Bun')) {
				const paths=choices(node.arguments[0]);
				const argument=node.arguments[0],packagePath=argument&&ts.isCallExpression(argument)&&argument.expression.getText(tree)==='require.resolve'?
					value(argument.arguments[0]):null;
				// Installed dependency bytes own resolved codec/data reads.
				if(packagePath===null||!installed(packagePath)) {
					if(!paths||reader.startsWith('readdir'))opaque=true;
					else for(const path of paths) {
						if(path.startsWith('.'))files.add(posix.join(posix.dirname(source),path));
						else if(path.startsWith('@hooks/')||path.startsWith('@root/')||path.startsWith('/')||path.startsWith(outdir+'/'))owned(path);
						else files.add(path);
					}
				}
			}
		}
		if(ts.isIdentifier(node)&&loaders.has(node.text)&&mappedLoad(node))
			for(const file of node.parent.expression.expression.elements)owned(file.text,true);
		if(ts.isIdentifier(node)&&processes.has(node.text)&&!ts.isImportSpecifier(node.parent))opaque=true;
		if(ts.isIdentifier(node)&&node.text==='Bun'&&!(ts.isPropertyAccessExpression(node.parent)&&node.parent.expression===node&&
			node.parent.name.text==='file'&&ts.isCallExpression(node.parent.parent)&&node.parent.parent.expression===node.parent))opaque=true;
		if(ts.isIdentifier(node)&&(node.text==='require'||loaders.has(node.text)||readers.has(node.text))&&
			!(ts.isCallExpression(node.parent)&&node.parent.expression===node)&&
			!(ts.isPropertyAccessExpression(node.parent)&&node.parent.expression===node&&['cache','resolve'].includes(node.parent.name.text))&&
			!(ts.isPropertyAccessExpression(node.parent)&&node.parent.expression===node&&node.parent.name.text==='cached'&&
				((ts.isCallExpression(node.parent.parent)&&node.parent.parent.expression===node.parent)||(hookHelper&&ts.isBinaryExpression(node.parent.parent)&&node.parent.parent.left===node.parent)))&&
			!mappedLoad(node)&&
			!ts.isImportSpecifier(node.parent)&&!(ts.isVariableDeclaration(node.parent)&&node.parent.name===node)&&
			!(ts.isPropertyAccessExpression(node.parent)&&node.parent.name===node)&&
			!(ts.isPropertyAssignment(node.parent)&&node.parent.name===node)&&
			!(ts.isMethodDeclaration(node.parent)&&node.parent.name===node)&&!ts.isTypeOfExpression(node.parent))opaque=true;
		if(ts.isPropertyAccessExpression(node)&&readers.has(node.name.text)&&
			!(ts.isCallExpression(node.parent)&&node.parent.expression===node))opaque=true;
		if(ts.isTemplateExpression(node)&&/\brequire\b/.test(node.head.text+node.templateSpans.map(span=>span.literal.text).join('')))opaque=true;
		if(ts.isStringLiteralLike(node)&&/\brequire\s*\(/.test(node.text)) {
			const embedded=inspectDependencies(node.text,source,available,{sources,outdir});
			for(const scope of [embedded.global,...embedded.routes]){opaque ||= scope.opaque;for(const root of scope.roots)roots.add(root);}
		}
		ts.forEachChild(node,visit);
	};visit(tree);
	return {roots:[...roots].sort(),imports:[...imports].sort(),files:[...files].sort(),externals:[...externals].sort(),opaque};
}

export async function writeBackendDependencies(entrypoints, outputs, root=process.cwd(), {sources='src', outdir='public', manifest='.pbgate-dependencies.json'}={}) {
    const names=entrypoints.map(file=>relative(root, file.startsWith('/')?file:root+'/'+file).replaceAll('\\','/')).sort();
    const available=new Set(names), modules={}, hooks=[];
    const emitted=new Map(outputs.filter(output=>output.path.endsWith('.js')).map(output=>[relative(root,output.path).replaceAll('\\','/'),output.path]));
    for (const source of names) {
        const output=outdir+'/'+source.slice(sources.length+1).replace(/\.imba$/,'.js');
        if (!emitted.has(output)) throw Error('Missing hook compiler output: '+source);
        const bytes=await readFile(emitted.get(output));
        const dependencies=inspectDependencies(bytes.toString(),source,available,{sources,outdir});
        modules[source]={hash:hash(await readFile(root+'/'+source)), output, output_hash:hash(bytes), startup_hash:dependencies.startup_hash, ...dependencies.global};
        if (source.endsWith('.pb.imba')) {
            hooks.push(source);
            modules[source].routes=dependencies.routes.map((route,index)=>({...route,hash:dependencies.request_hashes[index]}));
        }
    }
    const generator=hash(await readFile(new URL('./dependencies.js',import.meta.url)));
    await mkdir(root+'/'+outdir,{recursive:true});
    const path=root+'/'+outdir+'/'+manifest;
    await writeFile(path+'.tmp',JSON.stringify({format:1,generator,sources,outdir,modules,hooks,preparation:preparationInputs(modules,hooks,outdir)})+'\n');
    await rename(path+'.tmp',path);
    return {modules:names.length};
}
