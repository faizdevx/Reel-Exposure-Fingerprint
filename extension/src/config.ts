import { validateApiBase } from './api-base';

/**
 * REF server address, fixed at build time (`VITE_REF_API_BASE`, default the local server). Anything
 * but HTTPS, or loopback HTTP, makes the build fail (src/api-base.ts).
 */
export const API_BASE = validateApiBase(import.meta.env.VITE_REF_API_BASE as string | undefined);

/** Spec F2 values. */
export const MAX_SESSION_MS = 60 * 60 * 1000;
export const LEAVE_SURFACE_MS = 60 * 1000;
