import React, { createContext, useContext, useCallback, useRef } from 'react';

// Token dedup context — prevents parallel refresh attempts (lesson #17)
// When multiple hooks fire simultaneously, only one token refresh occurs.
type AuthContextType = {
  getAccessToken: () => Promise<string>;
};

const AuthContext = createContext<AuthContextType | null>(null);

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthGate');
  return ctx;
}

type Props = {
  children: React.ReactNode;
  fetchToken: () => Promise<string>;
  onAuthError: () => void;
};

export function AuthGate({ children, fetchToken, onAuthError }: Props) {
  const pendingToken = useRef<Promise<string> | null>(null);

  const getAccessToken = useCallback(async (): Promise<string> => {
    // Dedup: if a refresh is already in flight, return the same promise
    if (pendingToken.current) {
      return pendingToken.current;
    }

    const promise = fetchToken()
      .catch((err) => {
        // Auth error recovery — call signOut instead of letting the error propagate
        onAuthError();
        throw err;
      })
      .finally(() => {
        pendingToken.current = null;
      });

    pendingToken.current = promise;
    return promise;
  }, [fetchToken, onAuthError]);

  const contextValue = { getAccessToken };

  return (
    <AuthContext.Provider value={contextValue}>
      {children}
    </AuthContext.Provider>
  );
}
