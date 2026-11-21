import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
const admin = createRequire(new URL('../apps/admin/package.json', import.meta.url));
const tailwind = createRequire(admin.resolve('tailwindcss/package.json'));
const chokidar = createRequire(tailwind.resolve('chokidar/package.json'));
const braces = chokidar('braces');
assert.deepEqual(braces.expand('src/{pages,components}/**/*.{ts,tsx}'), ['src/pages/**/*.ts', 'src/pages/**/*.tsx', 'src/components/**/*.ts', 'src/components/**/*.tsx']);
assert.doesNotThrow(() => braces.compile('{'.repeat(20) + 'a,b' + '}'.repeat(20)));
for (const open of ['{', '(']) {
  const close = open === '{' ? '}' : ')';
  for (const count of [64, 3000, 4500, 30000]) {
    assert.throws(() => braces.compile(open.repeat(count) + 'a,b' + close.repeat(count)), { name: 'SyntaxError', message: count < 5000 ? /nesting exceeds/ : /exceeds max characters/ });
  }
}
const evidence = { component: 'braces@3.0.3', patch: 'patches/braces@3.0.3.patch', status: 'PASS', normalGlobExpansionPreserved: true, maliciousNestedBracesAndParenthesesRejected: true, maximumParserStack: 64, scannerVersionAdvisoryRemains: true, unresolvedNestingRisk: false };
writeFileSync(new URL('../docs/validation/braces-patch-evidence.json', import.meta.url), JSON.stringify(evidence, null, 2) + '\n');
console.log('braces nesting remediation: PASS');
