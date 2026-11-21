import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

function load(file, mocks = {}, globals = {}) {
  const path = new URL('../src/pages/invitation/' + file, import.meta.url);
  const require = createRequire(path),
    module = { exports: {} };
  const code = ts.transpileModule(readFileSync(path, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  new Function('require', 'module', 'exports', ...Object.keys(globals), code)(
    (name) => mocks[name] || require(name),
    module,
    module.exports,
    ...Object.values(globals),
  );
  return module.exports;
}

test('email import preserves physical line numbers and excludes comments from pending count', () => {
  const model = load('mail-accounts/model.ts');
  assert.equal(
    model.importLineCount(
      '# instructions\n\nfirst@example.com----pw\n  # comment\nsecond@example.com----pw\n',
    ),
    2,
  );
  const rejected = {
    status: 422,
    data: {
      ok: false,
      errors: [
        { line: 3, error: 'invalid segment count' },
        { line: 5, error: 'invalid email' },
      ],
    },
  };
  assert.deepEqual(model.importLineErrors(rejected), rejected.data.errors);
  assert.deepEqual(model.importLineErrors(new Error('network')), []);
  assert.deepEqual(
    model.importLineErrors({ data: { errors: [{ line: '1', error: 'bad' }, null] } }),
    [],
  );
});

test('registration defaults bind 2FA and request every credential; proxy is shared without storing credentials', () => {
  const writes = [];
  const model = load(
    'register/preferences.ts',
    { react: { useSyncExternalStore: (_subscribe, snapshot) => snapshot() } },
    {
      localStorage: { getItem: () => null, setItem: (key, value) => writes.push({ key, value }) },
    },
  );
  const [defaults, update] = model.useRegistrationPreferences();
  assert.deepEqual(model.registrationPayload('  ', defaults), {
    email: null,
    proxy: '',
    otp_timeout: 10,
    want_access_token: true,
    want_session_token: true,
    want_refresh_token: true,
    want_2fa: true,
    allow_existing_login: true,
    pool_only: false,
  });
  update({ proxy: 'socks5h://user:secret@host:1080', otpTimeout: 120, want2fa: false });
  const [next] = model.useRegistrationPreferences();
  assert.equal(model.registrationPayload(' user@example.com ', next).email, 'user@example.com');
  assert.equal(next.proxy, 'socks5h://user:secret@host:1080');
  assert.equal(JSON.parse(writes.at(-1).value).want2fa, false);
  assert.ok(!writes.at(-1).value.includes('secret'));
  assert.ok(!Object.hasOwn(JSON.parse(writes.at(-1).value), 'proxy'));
  model.resetRegistrationSecrets();
  assert.equal(model.useRegistrationPreferences()[0].proxy, '');
});

test('account-pool registration forces 2FA, drops the refresh token and marks the run as pool_only', () => {
  const writes = [];
  const model = load(
    'register/preferences.ts',
    { react: { useSyncExternalStore: (_subscribe, snapshot) => snapshot() } },
    {
      localStorage: { getItem: () => null, setItem: (key, value) => writes.push({ key, value }) },
    },
  );
  const [, update] = model.useRegistrationPreferences();
  update({ want2fa: false, wantRefreshToken: false });
  update({ poolOnly: true });
  const [pooled] = model.useRegistrationPreferences();
  assert.equal(pooled.want2fa, true);
  assert.equal(pooled.wantRefreshToken, false);
  assert.deepEqual(model.registrationPayload(' user@example.com ', pooled), {
    email: 'user@example.com',
    proxy: '',
    otp_timeout: 10,
    want_access_token: true,
    want_session_token: true,
    want_refresh_token: false,
    want_2fa: true,
    allow_existing_login: true,
    pool_only: true,
  });
  // The payload stays honest even if a stale local preference re-enables a locked rule.
  const forced = model.registrationPayload('', { ...pooled, want2fa: false, wantRefreshToken: true });
  assert.equal(forced.pool_only, true);
  assert.equal(forced.want_2fa, true);
  assert.equal(forced.want_refresh_token, false);
  update({ want2fa: true, wantRefreshToken: true });
  const [locked] = model.useRegistrationPreferences();
  assert.equal(locked.poolOnly, true);
  assert.equal(locked.wantRefreshToken, false);
});

test('editing an account sends changed fields only and distinguishes unchanged credentials from explicit clearing', () => {
  const { credentialEditPayload } = load('registered/model.ts');
  const original = { password: 'original-password', totp_secret: 'JBSWY3DPEHPK3PXP' };
  assert.deepEqual(
    credentialEditPayload('user@example.com', original, 'original-password', ' JBSWY3DPEHPK3PXP '),
    { email: 'user@example.com' },
  );
  assert.deepEqual(
    credentialEditPayload('user@example.com', original, 'new-password', original.totp_secret),
    { email: 'user@example.com', password: 'new-password' },
  );
  assert.deepEqual(credentialEditPayload('user@example.com', original, original.password, ''), {
    email: 'user@example.com',
    totp_secret: '',
  });
});

test('web and Codex credentials remain distinct and exports cover selected accounts or all pages', () => {
  const { credentialRows, registeredExportPayload } = load('registered/model.ts');
  const rows = credentialRows({
    refresh_token: 'web-rt',
    totp_secret: '2fa',
    codex: { refresh_token: 'codex-rt', account_id: 'codex-account' },
  });
  assert.equal(rows[0].key, 'totp_secret');
  assert.equal(rows.find((row) => row.key === 'refresh_token').value, 'web-rt');
  assert.equal(rows.find((row) => row.key === 'codex.refresh_token').value, 'codex-rt');
  assert.deepEqual(registeredExportPayload('cpa', []), { format: 'cpa', all: true });
  assert.deepEqual(registeredExportPayload('sub2api', ['one@example.com']), {
    format: 'sub2api',
    emails: ['one@example.com'],
  });
});

test('Plus report counts missing access tokens, deleted accounts and unreturned records as incomplete checks', () => {
  const { plusCheckSummary } = load('registered/model.ts');
  assert.deepEqual(
    plusCheckSummary(
      {
        a: { status: 'plus_eligible' },
        b: { status: 'plus_active' },
        c: { status: 'free' },
        d: { status: 'banned' },
        e: { status: 'token_invalid' },
        f: { status: 'no_at' },
        g: { status: 'not_found' },
        h: { status: 'error' },
      },
      9,
    ),
    { plus: 2, free: 1, banned: 1, invalid: 1, failed: 4 },
  );
});

test('CDK operations protect active and masked records while exporting selected codes independently of filters', () => {
  const { cdkCanMutate, cdkExportPayload } = load('cdk/model.ts');
  for (const status of ['running', 'queued'])
    assert.equal(cdkCanMutate({ code: 'CDK-A', status }), false);
  assert.equal(cdkCanMutate({ code: '***', status: 'unused' }), false);
  assert.equal(cdkCanMutate({ code: 'CDK-A', status: 'failed' }), true);
  const filters = { status: 'unused', batch: 'Batch A', search: 'owner@example.com' };
  assert.deepEqual(cdkExportPayload('csv', [], filters), { format: 'csv', ...filters });
  assert.deepEqual(cdkExportPayload('csv', ['CDK-A'], filters), {
    format: 'csv',
    codes: ['CDK-A'],
  });
});

test('copying all filtered CDKs paginates within API limits and rejects masked contents', async () => {
  const { collectFilteredCodes } = load('cdk/model.ts');
  const requested = [];
  const result = await collectFilteredCodes(
    { status: 'unused', batch: 'Batch A', search: 'user' },
    async (path) => {
      const query = new URL(path, 'http://localhost').searchParams;
      requested.push(query);
      const offset = Number(query.get('offset'));
      return {
        items:
          offset === 0
            ? Array.from({ length: 5000 }, (_, index) => ({ code: `CDK-${index}` }))
            : [{ code: 'CDK-5000' }],
        total: 5001,
      };
    },
  );
  assert.equal(result.length, 5001);
  assert.equal(result[5000], 'CDK-5000');
  assert.equal(requested.length, 2);
  assert.deepEqual(
    requested.map((query) => query.get('limit')),
    ['5000', '5000'],
  );
  assert.deepEqual(
    requested.map((query) => query.get('offset')),
    ['0', '5000'],
  );
  for (const query of requested) {
    assert.equal(query.get('batch'), 'Batch A');
    assert.equal(query.get('status'), 'unused');
    assert.equal(query.get('search'), 'user');
  }
  await assert.rejects(
    collectFilteredCodes({ status: '', batch: '', search: '' }, async () => ({
      items: [{ code: '***' }],
      total: 1,
    })),
    /凭证读取权限/,
  );
});
