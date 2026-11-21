import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

export function turnstileContracts(url) {
  const source = ts.transpileModule(readFileSync(url, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;

  async function harness({
    pending = false,
    scriptMissing = false,
    renderThrows = false,
    configError = false,
    data = { enabled: true, siteKey: 'test' },
    duringRender,
  } = {}) {
    let now = 0,
      nextTimer = 1,
      refIndex = 0,
      resetKey = 0,
      previousDependencies,
      effect,
      cleanup,
      script;
    const events = [],
      requests = [],
      renders = [],
      removed = [],
      refs = [];
    const timers = new Map(),
      intervals = new Map();
    class TrackedTarget extends EventTarget {
      listeners = new Map();
      addEventListener(type, listener, options) {
        super.addEventListener(type, listener, options);
        if (!this.listeners.has(type)) this.listeners.set(type, new Set());
        this.listeners.get(type).add(listener);
      }
      removeEventListener(type, listener, options) {
        super.removeEventListener(type, listener, options);
        this.listeners.get(type)?.delete(listener);
      }
      get listenerCount() {
        return [...this.listeners.values()].reduce((total, listeners) => total + listeners.size, 0);
      }
    }
    const window = new TrackedTarget();
    const api = {
      render(_el, options) {
        if (renderThrows) throw Error('blocked');
        const id = `widget-${renders.length + 1}`;
        renders.push({ id, options });
        duringRender?.(options);
        return id;
      },
      remove(id) {
        removed.push(id);
      },
    };
    if (!scriptMissing) window.turnstile = api;
    const document = {
      querySelector: () => script ?? null,
      createElement: () => {
        script = new TrackedTarget();
        script.dataset = {};
        return script;
      },
      head: { appendChild() {} },
    };
    const module = { exports: {} };
    const require = (name) => {
      if (name === 'react')
        return {
          useId: () => 'id',
          useRef: (value) => (refs[refIndex++] ??= { current: value ?? {} }),
          useEffect: (callback, dependencies) => {
            if (
              !previousDependencies ||
              dependencies.some((value, index) => value !== previousDependencies[index])
            ) {
              effect = callback;
              previousDependencies = dependencies;
            }
          },
        };
      if (name === 'react/jsx-runtime') return { jsx: () => null };
      if (name === '../lib/api')
        return {
          apiFetch: (_url, options) =>
            new Promise((resolve, reject) => {
              requests.push({ options, resolve, reject });
              if (!pending) {
                if (configError) reject(Error('configuration unavailable'));
                else resolve({ data });
              }
            }),
        };
      throw Error(name);
    };
    const schedule = (queue, callback, milliseconds, interval = false) => {
      const id = nextTimer++;
      queue.set(id, { callback, at: now + milliseconds, ...(interval && { milliseconds }) });
      return id;
    };
    const FakeDate = class extends Date {
      static now() {
        return now;
      }
    };
    new Function(
      'require',
      'module',
      'exports',
      'window',
      'document',
      'Date',
      'setTimeout',
      'clearTimeout',
      'setInterval',
      'clearInterval',
      source,
    )(
      require,
      module,
      module.exports,
      window,
      document,
      FakeDate,
      (callback, milliseconds) => schedule(timers, callback, milliseconds),
      (id) => timers.delete(id),
      (callback, milliseconds) => schedule(intervals, callback, milliseconds, true),
      (id) => intervals.delete(id),
    );
    const flush = async () => {
      await Promise.resolve();
      await Promise.resolve();
    };
    const render = async () => {
      refIndex = 0;
      module.exports.TurnstileWidget({
        resetKey,
        onToken: (value) => events.push(['token', value]),
        onEnabledChange: (value) => events.push(['enabled', value]),
      });
      if (effect) {
        cleanup?.();
        const nextEffect = effect;
        effect = undefined;
        cleanup = nextEffect();
      }
      await flush();
    };
    await render();
    return {
      events,
      window,
      timers,
      intervals,
      requests,
      renders,
      removed,
      get options() {
        return renders.at(-1)?.options;
      },
      get script() {
        return script;
      },
      cleanup: () => {
        cleanup?.();
        cleanup = undefined;
      },
      reset: async () => {
        resetKey++;
        await render();
      },
      resolveConfig: async (value = data, index = requests.length - 1) => {
        requests[index].resolve({ data: value });
        await flush();
      },
      scriptReady: () => {
        window.turnstile = api;
        for (const timer of [...intervals.values()]) timer.callback();
      },
      scriptError: () => script?.dispatchEvent(new Event('error')),
      advance: (milliseconds) => {
        const target = now + milliseconds;
        while (true) {
          const next = [...timers]
            .map(([id, timer]) => ({ id, timer, queue: timers }))
            .concat([...intervals].map(([id, timer]) => ({ id, timer, queue: intervals })))
            .filter(({ timer }) => timer.at <= target)
            .sort((left, right) => left.timer.at - right.timer.at || left.id - right.id)[0];
          if (!next) break;
          now = next.timer.at;
          if (next.queue === intervals) next.timer.at += next.timer.milliseconds;
          else next.queue.delete(next.id);
          next.timer.callback();
        }
        now = target;
      },
    };
  }
  const fallbackCount = (h) =>
    h.events.filter(([kind, value]) => kind === 'enabled' && value === false).length;
  const assertStopped = (h) => {
    assert.equal(h.timers.size, 0, 'deadline must be cleared');
    assert.equal(h.intervals.size, 0, 'script readiness polling must stop');
    assert.equal(h.window.listenerCount, 0, 'backend fallback listener must be removed');
    assert.equal(h.script?.listenerCount ?? 0, 0, 'script error listener must be removed');
    assert.equal(
      h.requests.at(-1).options.signal.aborted,
      true,
      'configuration request must be aborted',
    );
  };

  test('stalled configuration falls back at five seconds and ignores a late longer timeout', async () => {
    const h = await harness({ pending: true, scriptMissing: true });
    h.advance(4999);
    assert.equal(fallbackCount(h), 0);
    h.advance(1);
    assert.deepEqual(h.events, [
      ['enabled', false],
      ['token', ''],
    ]);
    assertStopped(h);
    await h.resolveConfig({ enabled: true, siteKey: 'test', fallbackSeconds: 60 });
    h.scriptReady();
    h.advance(60000);
    assert.equal(h.renders.length, 0);
    assert.equal(fallbackCount(h), 1);
    assertStopped(h);
    h.cleanup();
  });

  test('configuration, script loading and the initial spinner share the five second budget', async () => {
    const h = await harness({ pending: true, scriptMissing: true });
    h.advance(2000);
    await h.resolveConfig();
    h.advance(2000);
    h.scriptReady();
    assert.equal(h.renders.length, 1);
    h.advance(999);
    assert.equal(fallbackCount(h), 0);
    h.advance(1);
    assert.equal(fallbackCount(h), 1);
    assert.deepEqual(h.removed, ['widget-1']);
    assertStopped(h);
    h.cleanup();
    assert.deepEqual(h.removed, ['widget-1'], 'cleanup must not remove the same widget twice');
  });

  for (const seconds of [1, 12, 60]) {
    test(`a configured ${seconds} second loading timeout includes time spent fetching configuration`, async () => {
      const h = await harness({ pending: true });
      h.advance(750);
      await h.resolveConfig({ enabled: true, siteKey: 'test', fallbackSeconds: seconds });
      h.advance(seconds * 1000 - 751);
      assert.equal(fallbackCount(h), 0);
      h.advance(1);
      assert.equal(fallbackCount(h), 1);
      assertStopped(h);
      h.cleanup();
    });
  }

  test('configuration arriving after its shorter loading budget falls back without rendering', async () => {
    const h = await harness({ pending: true });
    h.advance(1500);
    await h.resolveConfig({ enabled: true, siteKey: 'test', fallbackSeconds: 1 });
    assert.deepEqual(h.events, [
      ['enabled', false],
      ['token', ''],
    ]);
    assert.equal(h.renders.length, 0);
    assertStopped(h);
    h.cleanup();
  });

  test('missing and malformed timeout values use the default five second budget', async () => {
    for (const fallbackSeconds of [undefined, null, '12', 0, -1, 61, 1.5, NaN, Infinity, true]) {
      const h = await harness({ data: { enabled: true, siteKey: 'test', fallbackSeconds } });
      h.advance(4999);
      assert.equal(fallbackCount(h), 0, `premature fallback for ${String(fallbackSeconds)}`);
      h.advance(1);
      assert.equal(fallbackCount(h), 1, `missing fallback for ${String(fallbackSeconds)}`);
      assertStopped(h);
      h.cleanup();
    }
  });

  test('a blocked script or a rendered spinner falls back at the loading deadline', async () => {
    for (const scriptMissing of [true, false]) {
      const h = await harness({ scriptMissing });
      h.advance(4999);
      assert.equal(fallbackCount(h), 0);
      h.advance(1);
      assert.equal(fallbackCount(h), 1);
      const before = [...h.events];
      h.scriptReady();
      h.options?.callback('stale');
      assert.deepEqual(h.events, before);
      assertStopped(h);
      h.cleanup();
    }
  });

  test('a challenge ready for interaction gives the user unlimited time to click', async () => {
    const h = await harness();
    h.advance(4999);
    h.options['before-interactive-callback']();
    assert.equal(h.timers.size, 0);
    h.advance(90000);
    assert.equal(fallbackCount(h), 0);
    h.options.callback('valid-after-user-interaction');
    assert.deepEqual(h.events.at(-1), ['token', 'valid-after-user-interaction']);
    h.options['after-interactive-callback']();
    h.advance(90000);
    assert.equal(fallbackCount(h), 0, 'after-interactive must preserve a successful token');
    assert.equal(h.timers.size, 0);
    h.cleanup();
    assertStopped(h);
  });

  test('waiting for a result after interaction gets one fresh configured loading budget', async () => {
    const h = await harness({ data: { enabled: true, siteKey: 'test', fallbackSeconds: 12 } });
    h.options['before-interactive-callback']();
    h.advance(90000);
    h.options['after-interactive-callback']();
    h.advance(11000);
    h.options['after-interactive-callback']();
    h.advance(999);
    assert.equal(fallbackCount(h), 0);
    h.advance(1);
    assert.equal(
      fallbackCount(h),
      1,
      'repeated after-interactive notifications must not extend a pending wait',
    );
    assertStopped(h);
    h.cleanup();
  });

  test('a successful token clears the loading deadline and blank tokens do not', async () => {
    const valid = await harness();
    valid.advance(4999);
    valid.options.callback('valid');
    assert.equal(valid.timers.size, 0);
    valid.advance(90000);
    assert.equal(fallbackCount(valid), 0);
    assert.deepEqual(valid.events.at(-1), ['token', 'valid']);
    valid.cleanup();
    for (const token of ['', '   ']) {
      const blank = await harness();
      blank.advance(4999);
      blank.options.callback(token);
      assert.deepEqual(blank.events, [['enabled', true]]);
      blank.advance(1);
      assert.equal(fallbackCount(blank), 1);
      blank.cleanup();
    }
  });

  for (const event of ['expired-callback', 'timeout-callback']) {
    test(`${event} invalidates a successful token and starts a fresh configured loading deadline`, async () => {
      const h = await harness({ data: { enabled: true, siteKey: 'test', fallbackSeconds: 12 } });
      assert.equal(h.options['refresh-expired'], 'auto');
      assert.equal(h.options['refresh-timeout'], 'auto');
      h.options.callback('valid');
      h.advance(90000);
      h.options[event]();
      assert.deepEqual(h.events.at(-1), ['token', '']);
      h.advance(11000);
      h.options[event]();
      h.options['error-callback']('110620');
      h.options['error-callback']('110600');
      h.advance(999);
      assert.equal(fallbackCount(h), 0);
      h.advance(1);
      assert.equal(fallbackCount(h), 1, 'expiry notifications must not extend a pending wait');
      assertStopped(h);
      h.cleanup();
    });
  }

  test('a refreshed interactive challenge clears the new loading deadline too', async () => {
    const h = await harness();
    h.options.callback('valid');
    h.options['expired-callback']();
    h.advance(4999);
    h.options['before-interactive-callback']();
    h.advance(90000);
    assert.equal(fallbackCount(h), 0);
    h.options['after-interactive-callback']();
    h.advance(4999);
    h.options.callback('refreshed');
    h.advance(90000);
    assert.deepEqual(h.events.at(-1), ['token', 'refreshed']);
    assert.equal(fallbackCount(h), 0);
    h.cleanup();
  });

  test('transient failures invalidate tokens and retry without extending a pending deadline', async () => {
    const h = await harness();
    assert.equal(h.options.retry, 'auto');
    h.advance(4000);
    h.options['error-callback']('200500');
    assert.deepEqual(h.events.at(-1), ['token', '']);
    h.advance(999);
    assert.equal(fallbackCount(h), 0);
    h.advance(1);
    assert.equal(fallbackCount(h), 1);
    h.cleanup();

    const recovered = await harness();
    recovered.options['error-callback']('200500');
    recovered.options['error-callback']('300010');
    recovered.options.callback('recovered');
    recovered.advance(90000);
    recovered.options['error-callback']('200500');
    assert.deepEqual(recovered.events.at(-1), ['token', '']);
    recovered.advance(4999);
    assert.equal(fallbackCount(recovered), 0, 'success must reset the consecutive failure count');
    recovered.advance(1);
    assert.equal(
      fallbackCount(recovered),
      1,
      'failure after success must start a fresh loading wait',
    );
    recovered.cleanup();
  });

  test('persistent and permanent failures fall back immediately and ignore all late callbacks', async () => {
    const h = await harness();
    for (let count = 0; count < 3; count++) h.options['error-callback']('200500');
    assert.equal(fallbackCount(h), 1);
    const before = [...h.events];
    h.options.callback('stale');
    h.options['before-interactive-callback']();
    h.options['after-interactive-callback']();
    h.options['expired-callback']();
    h.options['timeout-callback']();
    h.options['error-callback']('200500');
    h.options['unsupported-callback']();
    h.advance(90000);
    assert.deepEqual(h.events, before);
    assert.deepEqual(h.removed, ['widget-1']);
    assertStopped(h);
    h.cleanup();
    for (const code of ['110100', '110110', '110200', '200100', '400020', '400021', '400070']) {
      const invalid = await harness();
      invalid.options['error-callback'](code);
      assert.equal(
        fallbackCount(invalid),
        1,
        `permanent failure ${code} must fall back immediately`,
      );
      assertStopped(invalid);
      invalid.cleanup();
    }
  });

  test('script errors, render errors, unsupported browsers and backend fallback events fail immediately', async () => {
    const brokenScript = await harness({ scriptMissing: true });
    brokenScript.scriptError();
    const brokenRender = await harness({ renderThrows: true });
    const unsupported = await harness();
    unsupported.options['unsupported-callback']();
    const backend = await harness();
    backend.window.dispatchEvent(new Event('smsinvit:captcha-fallback'));
    const synchronous = await harness({
      duringRender: (options) => options['unsupported-callback'](),
    });
    for (const h of [brokenScript, brokenRender, unsupported, backend, synchronous]) {
      assert.equal(fallbackCount(h), 1);
      const before = [...h.events];
      h.options?.callback('stale');
      h.scriptError();
      h.window.dispatchEvent(new Event('smsinvit:captcha-fallback'));
      assert.deepEqual(h.events, before);
      assertStopped(h);
      h.cleanup();
    }
    assert.deepEqual(
      synchronous.removed,
      ['widget-1'],
      'synchronous fallback during render must still remove the widget',
    );
  });

  test('disabled, missing or rejected configuration falls back immediately', async () => {
    for (const options of [
      { data: { enabled: false, siteKey: 'test' } },
      { data: { enabled: true, siteKey: '' } },
      { data: null },
      { configError: true },
    ]) {
      const h = await harness(options);
      assert.equal(fallbackCount(h), 1);
      assert.equal(h.renders.length, 0);
      assertStopped(h);
      h.cleanup();
    }
  });

  test('reset removes the old widget and gives the replacement a fresh loading budget', async () => {
    const h = await harness();
    const oldOptions = h.options;
    h.advance(4000);
    await h.reset();
    assert.deepEqual(h.removed, ['widget-1']);
    assert.equal(h.requests[0].options.signal.aborted, true);
    assert.equal(h.renders.length, 2);
    const before = [...h.events];
    oldOptions.callback('stale');
    oldOptions['before-interactive-callback']();
    oldOptions['after-interactive-callback']();
    oldOptions['error-callback']('110100');
    assert.deepEqual(h.events, before);
    h.advance(4999);
    assert.equal(fallbackCount(h), 0);
    h.advance(1);
    assert.equal(fallbackCount(h), 1);
    assert.deepEqual(h.removed, ['widget-1', 'widget-2']);
    assertStopped(h);
    h.cleanup();
  });

  test('reset ignores stale configuration while the replacement configuration is pending', async () => {
    const h = await harness({ pending: true });
    h.advance(4000);
    await h.reset();
    await h.resolveConfig({ enabled: true, siteKey: 'stale', fallbackSeconds: 60 }, 0);
    assert.equal(h.renders.length, 0);
    assert.deepEqual(h.events, []);
    h.advance(4999);
    assert.equal(fallbackCount(h), 0);
    h.advance(1);
    assert.equal(fallbackCount(h), 1);
    assertStopped(h);
    h.cleanup();
  });

  test('unmounted widgets ignore late configuration, scripts, callbacks and events', async () => {
    for (const options of [{}, { pending: true, scriptMissing: true }, { scriptMissing: true }]) {
      const h = await harness(options);
      h.cleanup();
      const before = [...h.events];
      await h.resolveConfig();
      h.scriptReady();
      h.scriptError();
      h.options?.callback('stale');
      h.options?.['before-interactive-callback']();
      h.options?.['after-interactive-callback']();
      h.options?.['expired-callback']();
      h.options?.['error-callback']('110100');
      h.window.dispatchEvent(new Event('smsinvit:captcha-fallback'));
      h.advance(90000);
      assert.deepEqual(h.events, before);
      assertStopped(h);
    }
  });
}
