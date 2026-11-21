/** Shared error handling for redemption and administrator interfaces. */

export {
  ErrorCodeLabels,
  ErrorCodeLabelsEn,
  ErrorCodeLabelsZh,
  formatErrorDisplay,
  getUserFacingMessage,
  localizeErrorMessage,
  toUserFacingError,
  type ErrorLocale,
} from './user-messages';

export const ErrorCodes = {
  // --- AUTH ---
  AUTH_REQUIRED: 'AUTH_REQUIRED',
  AUTH_SESSION_EXPIRED: 'AUTH_SESSION_EXPIRED',
  AUTH_SESSION_INVALID: 'AUTH_SESSION_INVALID',
  AUTH_INVALID_CREDENTIALS: 'AUTH_INVALID_CREDENTIALS',
  AUTH_RATE_LIMITED: 'AUTH_RATE_LIMITED',
  AUTH_INVALID_TOKEN: 'AUTH_INVALID_TOKEN',
  AUTH_CSRF_INVALID: 'AUTH_CSRF_INVALID',
  AUTH_TOTP_REQUIRED: 'AUTH_TOTP_REQUIRED',
  AUTH_TOTP_INVALID: 'AUTH_TOTP_INVALID',
  AUTH_TOTP_NOT_ENABLED: 'AUTH_TOTP_NOT_ENABLED',
  AUTH_TOTP_ALREADY_ENABLED: 'AUTH_TOTP_ALREADY_ENABLED',
  AUTH_TOTP_SETUP_REQUIRED: 'AUTH_TOTP_SETUP_REQUIRED',
  AUTH_CHALLENGE_EXPIRED: 'AUTH_CHALLENGE_EXPIRED',
  CAPTCHA_REQUIRED: 'CAPTCHA_REQUIRED',
  CAPTCHA_INVALID: 'CAPTCHA_INVALID',
  CAPTCHA_EXPIRED: 'CAPTCHA_EXPIRED',

  // --- USER ---
  USER_NOT_FOUND: 'USER_NOT_FOUND',
  USERNAME_ALREADY_EXISTS: 'USERNAME_ALREADY_EXISTS',
  EMAIL_ALREADY_EXISTS: 'EMAIL_ALREADY_EXISTS',
  EMAIL_DISABLED: 'EMAIL_DISABLED',
  USER_DISABLED: 'USER_DISABLED',
  USER_BANNED: 'USER_BANNED',
  USER_CANNOT_MODIFY_TARGET: 'USER_CANNOT_MODIFY_TARGET',
  USER_PROTECTED_ACCOUNT: 'USER_PROTECTED_ACCOUNT',
  PASSWORD_TOO_WEAK: 'PASSWORD_TOO_WEAK',

  // --- PERMISSION ---
  PERMISSION_DENIED: 'PERMISSION_DENIED',
  PERMISSION_SCOPE_DENIED: 'PERMISSION_SCOPE_DENIED',
  RESOURCE_NOT_OWNED: 'RESOURCE_NOT_OWNED',
  ADMIN_ACCESS_DENIED: 'ADMIN_ACCESS_DENIED',
  SENSITIVE_PERMISSION_REQUIRED: 'SENSITIVE_PERMISSION_REQUIRED',
  INVALID_PRICE: 'INVALID_PRICE',
  INVALID_QUANTITY: 'INVALID_QUANTITY',

  // --- INVENTORY ---
  INVENTORY_OUT_OF_STOCK: 'INVENTORY_OUT_OF_STOCK',
  INVENTORY_LOCK_FAILED: 'INVENTORY_LOCK_FAILED',
  INVENTORY_ALREADY_LOCKED: 'INVENTORY_ALREADY_LOCKED',
  INVENTORY_ALREADY_SOLD: 'INVENTORY_ALREADY_SOLD',
  INVENTORY_INVALID: 'INVENTORY_INVALID',
  INVENTORY_DUPLICATE: 'INVENTORY_DUPLICATE',
  INVENTORY_IMPORT_INVALID: 'INVENTORY_IMPORT_INVALID',
  INVENTORY_SKU_INVALID: 'INVENTORY_SKU_INVALID',
  INVENTORY_EXPORT_DENIED: 'INVENTORY_EXPORT_DENIED',
  INVENTORY_RELEASE_CONFLICT: 'INVENTORY_RELEASE_CONFLICT',
  INVENTORY_NOT_FOUND: 'INVENTORY_NOT_FOUND',
  IDEMPOTENCY_CONFLICT: 'IDEMPOTENCY_CONFLICT',
  IDEMPOTENCY_REQUEST_IN_PROGRESS: 'IDEMPOTENCY_REQUEST_IN_PROGRESS',
  MANUAL_DELIVERY_NOTE_REQUIRED: 'MANUAL_DELIVERY_NOTE_REQUIRED',
  SMS_CDK_DISABLED: 'SMS_CDK_DISABLED',
  SMS_CDK_AMBIGUOUS: 'SMS_CDK_AMBIGUOUS',

  // --- GPT DIRECT ---
  SESSION_SECRET_REQUIRED: 'SESSION_SECRET_REQUIRED',
  SESSION_SECRET_INVALID: 'SESSION_SECRET_INVALID',
  SESSION_REQUIRED: 'SESSION_REQUIRED',
  STORAGE_UNAVAILABLE: 'STORAGE_UNAVAILABLE',

  // --- RATE LIMIT ---
  RATE_LIMIT_EXCEEDED: 'RATE_LIMIT_EXCEEDED',
  RATE_LIMITED: 'RATE_LIMITED',

  // --- SYSTEM ---
  VALIDATION_ERROR: 'VALIDATION_ERROR',
  BAD_REQUEST: 'BAD_REQUEST',
  RESOURCE_NOT_FOUND: 'RESOURCE_NOT_FOUND',
  CONFLICT: 'CONFLICT',
  INTERNAL_ERROR: 'INTERNAL_ERROR',
  INTERNAL_SERVER_ERROR: 'INTERNAL_SERVER_ERROR',
  SERVICE_UNAVAILABLE: 'SERVICE_UNAVAILABLE',
  UPSTREAM_ERROR: 'UPSTREAM_ERROR',
  INVALID_REQUEST_BODY: 'INVALID_REQUEST_BODY',
} as const;

export type ErrorCode = (typeof ErrorCodes)[keyof typeof ErrorCodes];

export interface ErrorDetails {
  fields?: Record<string, string>;
  [key: string]: unknown;
}

export interface StandardErrorResponse {
  success: false;
  error: {
    code: ErrorCode | string;
    message: string;
    details?: ErrorDetails | null;
    requestId: string;
  };
}

/**
 * Base Application Error
 */
export class AppError extends Error {
  public readonly code: ErrorCode | string;
  public readonly httpStatus: number;
  public readonly details: ErrorDetails | null;
  public readonly isOperational: boolean;

  constructor(options: {
    code: ErrorCode | string;
    message: string;
    httpStatus?: number;
    details?: ErrorDetails | null;
    cause?: Error;
  }) {
    super(options.message);
    this.name = 'AppError';
    this.code = options.code;
    this.httpStatus = options.httpStatus || 400;
    this.details = options.details ?? null;
    this.isOperational = true;
    if (options.cause) {
      this.cause = options.cause;
    }
    Object.setPrototypeOf(this, new.target.prototype);
  }

  public toResponse(requestId: string): StandardErrorResponse {
    return {
      success: false,
      error: {
        code: this.code,
        message: this.message,
        details: this.details,
        requestId,
      },
    };
  }
}

/**
 * Business Error (domain level operational failure)
 */
export class BusinessError extends AppError {
  constructor(
    code: ErrorCode | string,
    message: string,
    httpStatus = 409,
    details: ErrorDetails | null = null,
  ) {
    super({ code, message, httpStatus, details });
    this.name = 'BusinessError';
  }
}
