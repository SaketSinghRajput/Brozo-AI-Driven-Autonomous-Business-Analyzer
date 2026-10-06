import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { setCsrfToken, setUnauthorizedHandler } from '../api/client';
import { unwrap } from '../api/errors';
import {
  loginApiV1AuthLoginPost,
  logoutApiV1AuthLogoutPost,
  meApiV1AuthMeGet,
} from '../api/generated/sdk.gen';
import type { SessionInfo } from '../api/generated/types.gen';
import { queryClient } from '../lib/queryClient';
import { roleHas, type Permission } from './permissions';

/**
 * Session context (design.md §56: "No global store beyond auth/session context"; ADR-0013).
 * The session is an HttpOnly cookie the browser sends automatically; JS only ever sees the
 * SessionInfo body (user, workspace, role, CSRF token). A reload restores it via GET /auth/me.
 */
export type SessionStatus = 'loading' | 'authenticated' | 'anonymous';

interface AuthContextValue {
  status: SessionStatus;
  session: SessionInfo | null;
  /** True when the last session ended because the backend answered 401 (not a sign-out). */
  expired: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  can: (permission: Permission) => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [status, setStatus] = useState<SessionStatus>('loading');
  const [expired, setExpired] = useState(false);

  const start = useCallback((info: SessionInfo) => {
    setCsrfToken(info.csrf_token);
    setSession(info);
    setStatus('authenticated');
    setExpired(false);
  }, []);

  // design.md §56: login / logout / workspace switch clears all server state.
  const end = useCallback((wasExpired: boolean) => {
    setCsrfToken(null);
    queryClient.clear();
    setSession(null);
    setStatus('anonymous');
    setExpired(wasExpired);
  }, []);

  useEffect(() => {
    let cancelled = false;
    unwrap(meApiV1AuthMeGet())
      .then((info) => {
        if (!cancelled) start(info);
      })
      .catch(() => {
        if (!cancelled) end(false);
      });
    return () => {
      cancelled = true;
    };
  }, [start, end]);

  useEffect(() => {
    // Only an established session can "expire"; a 401 while anonymous is just "not signed in".
    setUnauthorizedHandler(() => end(status === 'authenticated'));
    return () => setUnauthorizedHandler(null);
  }, [end, status]);

  const login = useCallback(
    async (email: string, password: string) => {
      queryClient.clear();
      start(await unwrap(loginApiV1AuthLoginPost({ body: { email, password } })));
    },
    [start],
  );

  const logout = useCallback(async () => {
    try {
      await logoutApiV1AuthLogoutPost();
    } finally {
      end(false);
    }
  }, [end]);

  const can = useCallback(
    (permission: Permission) => (session ? roleHas(session.role, permission) : false),
    [session],
  );

  const value = useMemo<AuthContextValue>(
    () => ({ status, session, expired, login, logout, can }),
    [status, session, expired, login, logout, can],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return ctx;
}
