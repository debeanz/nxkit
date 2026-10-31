#!/usr/bin/env python3
# Metal check for shaders translated outside nxnative's build (Switch 2 shaders transcoded to Maxwell code, then dmsh_xlate on the PC): each SPIR-V goes through
# the core of nxnative's dm/gfx/tools/dmsh_build.py finish() - spv_single_lane for vertex shaders, spirv-cross with its options (XC), precise() - and then through
# the iOS Metal compiler with dm/ios/tools/ios_metal.py's options. Those pieces are read from the nxnative checkout itself, so the check follows its version.
# Usage: metal_check.py <SPIR-V dir> <nxnative dir> <out dir>   -> <out>/results.tsv (key, stage, result, first error line), <out>/fail/ (the MSL that did not build)
# Prints only counts: the shaders and their MSL stay in the encrypted output.
import ast, collections, concurrent.futures as cf, os, re, shutil, struct, subprocess, sys, tempfile

spv_dir, nx, out = sys.argv[1:4]
os.makedirs(os.path.join(out, 'fail'), exist_ok=True)

def pick(path, names):
    """top-level definitions of a module by name, without running the module"""
    tree = ast.parse(open(path).read())
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in names) or
            (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets))]
    ns = {'re': re, 'struct': struct, 'os': os, 'subprocess': subprocess}
    exec(compile(ast.Module(body=keep, type_ignores=[]), path, 'exec'), ns)
    return ns

B = pick(os.path.join(nx, 'dm/gfx/tools/dmsh_build.py'), {'XC', 'precise', 'SG_OPS', 'SG_BUILTINS', 'spv_single_lane', '_ep_drop'})
I = pick(os.path.join(nx, 'dm/ios/tools/ios_metal.py'), {'METAL'})
XC, METAL = B['XC'], I['METAL']
print('spirv-cross', XC); print('metal', METAL)

def one(name):
    key = name[:-4]; stage = key.split('.')[1]
    with tempfile.TemporaryDirectory() as w:
        spv, msl, air = os.path.join(w, 's.spv'), os.path.join(w, 's.metal'), os.path.join(w, 's.air')
        shutil.copy(os.path.join(spv_dir, name), spv)
        if stage == 'vs': B['spv_single_lane'](spv)
        r = subprocess.run(['spirv-cross', *XC, spv, '--output', msl], capture_output=True, text=True)
        if r.returncode: return key, stage, 'MSL failed', (r.stderr.strip().splitlines() or [''])[0][:200]
        open(msl, 'w').write(B['precise'](open(msl).read()))
        r = subprocess.run([*METAL, msl, '-o', air], capture_output=True, text=True)
        if r.returncode:
            shutil.copy(msl, os.path.join(out, 'fail', key + '.metal'))
            err = next((l for l in r.stderr.splitlines() if 'error' in l), r.stderr.strip()[:200])
            return key, stage, 'metal failed', re.sub(r'^.*?s\.metal:', '', err)[:200]
        return key, stage, 'ok', ''

names = sorted(n for n in os.listdir(spv_dir) if n.endswith('.spv'))
counts, rows = collections.Counter(), []
with cf.ThreadPoolExecutor(os.cpu_count() or 4) as ex:
    for i, row in enumerate(ex.map(one, names)):
        rows.append(row); counts[(row[1], row[2])] += 1
        if (i + 1) % 500 == 0: print(f'{i + 1}/{len(names)}', dict(counts), flush=True)
with open(os.path.join(out, 'results.tsv'), 'w') as f:
    for r in rows: f.write('\t'.join(r) + '\n')
print('done', len(rows), dict(counts))
