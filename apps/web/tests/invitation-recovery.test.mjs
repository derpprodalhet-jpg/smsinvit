import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = new URL('../src/lib/invitation-recovery.ts', import.meta.url);
const compiled = ts.transpileModule(readFileSync(source, 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const module = { exports: {} };
new Function('module', 'exports', compiled)(module, module.exports);
const recovery = module.exports;
const now = 1800000000000;
const session = {
  id: '12345678-1234-4234-8234-123456789abc',
  sessionSecret: 'a'.repeat(64),
  line: 7,
  createdAt: now - 1000,
  expiresAt: now - 1000 + recovery.INVITATION_RECOVERY_TTL,
};
const envelope = (sessions = [], attempts = []) => ({ version: 1, sessions, attempts });

test('recovery serialization whitelists credentials and line maps but strips CDKs, email, status and OTP', () => {
  const attempt = {
    ...recovery.createInvitationAttempt([2, 5], globalThis.crypto, now),
    code: 'PRIVATE-CDK',
    password: 'PRIVATE-PASSWORD',
  };
  const clean = recovery.parseInvitationRecovery(
    JSON.stringify(
      envelope(
        [
          {
            ...session,
            email: 'private@example.test',
            status: 'done',
            code: '123456',
            refreshToken: 'PRIVATE-TOKEN',
          },
        ],
        [attempt],
      ),
    ),
    now,
  );
  assert.deepEqual(clean.sessions, [session]);
  assert.deepEqual(clean.attempts[0].lines, [2, 5]);
  assert.doesNotMatch(JSON.stringify(clean), /PRIVATE-|example.test|status|refreshToken/);
});

test('fixed expiration is not renewed and expired or far-future records make no recoverable entries', () => {
  const valid = recovery.parseInvitationRecovery(JSON.stringify(envelope([session])), now);
  assert.equal(valid.sessions[0].expiresAt, session.expiresAt);
  assert.equal(
    recovery.parseInvitationRecovery(JSON.stringify(envelope([session])), session.expiresAt)
      .sessions.length,
    0,
  );
  const future = {
    ...session,
    createdAt: now + 6 * 60000,
    expiresAt: now + 6 * 60000 + recovery.INVITATION_RECOVERY_TTL,
  };
  assert.equal(
    recovery.parseInvitationRecovery(JSON.stringify(envelope([future])), now).sessions.length,
    0,
  );
  assert.equal(
    recovery.parseInvitationRecovery(
      JSON.stringify(envelope([{ ...session, expiresAt: now + 10 * 86400000 }])),
      now,
    ).sessions.length,
    0,
  );
});

test('bad UUIDs, wrong capability shape, unsafe line numbers and malformed attempts are ignored', () => {
  const attempt = recovery.createInvitationAttempt([1], globalThis.crypto, now);
  const clean = recovery.parseInvitationRecovery(
    JSON.stringify(
      envelope(
        [
          { ...session, id: 'https://attacker.test/' },
          { ...session, sessionSecret: 'secret' },
          { ...session, line: -1 },
        ],
        [
          { ...attempt, lines: [1, '2'] },
          { ...attempt, recoverySecret: 'short' },
        ],
      ),
    ),
    now,
  );
  assert.deepEqual(clean, envelope());
});

test('unrecognized versions, oversized storage and excessive record counts are rejected', () => {
  for (const raw of [
    '{"version":2,"sessions":[],"attempts":[]}',
    'x'.repeat(65537),
    JSON.stringify(envelope(Array.from({ length: 201 }, () => session))),
  ]) {
    assert.throws(() => recovery.parseInvitationRecovery(raw, now));
  }
});

test('expired entries are removed but failed cleanup preserves other valid in-memory records', () => {
  let raw = JSON.stringify(
    envelope([
      session,
      {
        ...session,
        id: '87654321-1234-4234-8234-123456789abc',
        createdAt: now - 2 * recovery.INVITATION_RECOVERY_TTL,
        expiresAt: now - recovery.INVITATION_RECOVERY_TTL,
      },
    ]),
  );
  const storage = {
    getItem: () => raw,
    setItem: (_, next) => {
      raw = next;
    },
    removeItem: () => {
      raw = null;
    },
  };
  const result = recovery.readInvitationRecovery(storage, now);
  assert.equal(result.recovery.sessions.length, 1);
  assert.match(result.notice, /已过期/);
  assert.equal(JSON.parse(raw).sessions.length, 1);
  raw = JSON.stringify(envelope([session, { ...session, line: -1 }]));
  storage.setItem = () => {
    throw new Error('quota');
  };
  const retained = recovery.readInvitationRecovery(storage, now);
  assert.deepEqual(retained.recovery.sessions, [session]);
  assert.match(retained.notice, /无法保存更新/);
  assert.equal(JSON.parse(raw).sessions.length, 2);
});

test('corrupt records are removed and blocked reads or writes remain observable to the UI', () => {
  let removed = false;
  const storage = {
    getItem: () => '{broken',
    removeItem: () => {
      removed = true;
    },
    setItem() {},
  };
  assert.deepEqual(recovery.readInvitationRecovery(storage, now).recovery, envelope());
  assert.equal(removed, true);
  assert.throws(() =>
    recovery.readInvitationRecovery(
      {
        ...storage,
        getItem() {
          throw new Error('blocked');
        },
      },
      now,
    ),
  );
  assert.throws(() =>
    recovery.writeInvitationRecovery(
      {
        ...storage,
        setItem() {
          throw new Error('blocked');
        },
      },
      envelope([session]),
      now,
    ),
  );
});

test('attempt IDs and independent secrets come from cryptographic randomness and preserve creation time', () => {
  const one = recovery.createInvitationAttempt([1, 4], globalThis.crypto, now);
  const two = recovery.createInvitationAttempt([1, 4], globalThis.crypto, now);
  assert.match(one.id, /^[0-9a-f-]{36}$/);
  assert.match(one.recoverySecret, /^[0-9a-f]{64}$/);
  assert.notEqual(one.id, two.id);
  assert.notEqual(one.recoverySecret, two.recoverySecret);
  assert.equal(one.expiresAt, now + recovery.INVITATION_RECOVERY_TTL);
  assert.throws(() => recovery.createInvitationAttempt([1], {}, now));
});

test('attempt expiry metadata retains fixed deadlines and strictly whitelists IDs and numeric dates', () => {
  const attempt = {
    ...recovery.createInvitationAttempt([1], globalThis.crypto, now),
    knownSessionExpirations: [
      { id: session.id, expiresAt: now + 1000, code: 'PRIVATE-CDK', status: 'running' },
    ],
    restoreIds: [session.id],
  };
  const stored = recovery.parseInvitationRecovery(
    JSON.stringify(envelope([], [attempt])),
    now + 2000,
  );
  assert.deepEqual(stored.attempts[0].knownSessionExpirations, [
    { id: session.id, expiresAt: now + 1000 },
  ]);
  assert.deepEqual(stored.attempts[0].restoreIds, [session.id]);
  assert.doesNotMatch(JSON.stringify(stored), /PRIVATE-|status|running/);
  for (const malformed of [
    { ...attempt, restoreIds: ['https://internal.example'] },
    { ...attempt, restoreIds: [session.id, session.id] },
    { ...attempt, restoreIds: Array.from({ length: 51 }, () => session.id) },
    { ...attempt, knownSessionExpirations: [{ id: session.id, expiresAt: 'tomorrow' }] },
    { ...attempt, knownSessionExpirations: [{ id: session.id, expiresAt: now }] },
    {
      ...attempt,
      knownSessionExpirations: [
        ...attempt.knownSessionExpirations,
        ...attempt.knownSessionExpirations,
      ],
    },
  ])
    assert.equal(
      recovery.parseInvitationRecovery(JSON.stringify(envelope([], [malformed])), now).attempts
        .length,
      0,
    );
});

test('full recovery snapshots keep the 64KiB and fifty-attempt budget before overwriting stored data', () => {
  const uuid = (index) => `00000000-0000-4000-8000-${(index + 1).toString(16).padStart(12, '0')}`;
  const attempt = {
    ...recovery.createInvitationAttempt([1], globalThis.crypto, now),
    knownSessionExpirations: Array.from({ length: 2700 }, (_, index) => ({
      id: uuid(index),
      expiresAt: now + 1000,
    })),
  };
  let writes = 0;
  const storage = {
    setItem: () => {
      writes++;
    },
    removeItem: () => {
      writes++;
    },
  };
  assert.throws(() => recovery.writeInvitationRecovery(storage, envelope([], [attempt]), now));
  assert.equal(writes, 0);
  const validAttempt = recovery.createInvitationAttempt([1], globalThis.crypto, now);
  assert.throws(() =>
    recovery.writeInvitationRecovery(
      storage,
      envelope(
        [],
        Array.from({ length: 51 }, () => validAttempt),
      ),
      now,
    ),
  );
  assert.equal(writes, 0);
});

test('the writer refuses to silently drop a live attempt but still cleans expired records', () => {
  let writes = 0;
  const storage = {
    setItem: () => {
      writes++;
    },
    removeItem: () => {
      writes++;
    },
  };
  const valid = recovery.createInvitationAttempt([1], globalThis.crypto, now);
  const invalid = {
    ...valid,
    knownSessionExpirations: [
      { id: session.id, expiresAt: now + recovery.INVITATION_RECOVERY_TTL + 6 * 60000 },
    ],
  };
  assert.throws(() => recovery.writeInvitationRecovery(storage, envelope([], [invalid]), now));
  assert.equal(writes, 0);
  const expired = { ...valid, createdAt: now - recovery.INVITATION_RECOVERY_TTL, expiresAt: now };
  recovery.writeInvitationRecovery(storage, envelope([], [expired]), now);
  assert.equal(writes, 1);
});

test('required attempt proofs must survive storage readback with all fixed expiry metadata intact', () => {
  const attempt = {
    ...recovery.createInvitationAttempt([1], globalThis.crypto, now),
    knownSessionExpirations: [{ id: session.id, expiresAt: session.expiresAt }],
  };
  const ignored = { getItem: () => null, setItem() {}, removeItem() {} };
  assert.throws(() =>
    recovery.writeInvitationRecovery(ignored, envelope([], [attempt]), now, [attempt]),
  );
  const values = new Map();
  const changed = {
    getItem: (key) => {
      const data = JSON.parse(values.get(key));
      data.attempts[0].knownSessionExpirations[0].expiresAt -= 1000;
      return JSON.stringify(data);
    },
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  assert.throws(() =>
    recovery.writeInvitationRecovery(changed, envelope([], [attempt]), now, [attempt]),
  );
});
