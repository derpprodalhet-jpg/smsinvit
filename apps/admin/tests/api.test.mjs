import { apiClientContracts } from './support/api-client-contract-tests.mjs';
apiClientContracts(new URL('../src/lib/api.ts', import.meta.url), {
  mePath: '/api/v1/admin/auth/me',
  refreshPath: '/api/v1/admin/auth/refresh',
  sessionCookie: 'lc_admin_session=1',
});
