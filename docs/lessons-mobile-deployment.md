# Lessons from Mobile Deployment (Expo + EAS)

> **Legacy record (March 2026).** The Expo mobile app works only with Logto sign-in. New apps use Google sign-in and have no mobile app yet. See [README.md](README.md) for current guides.

> Hard-won lessons from deploying an Expo/React Native mobile app alongside a Python/FastAPI backend. Sourced from two early apps (2026-03-19 and 2026-03-20), plus cross-project memory consolidation (2026-03-20). These should inform the agentic-core template's mobile scaffold and bootstrap script.

---

## 1. EAS Builds (Not Local Xcode)

### Local Xcode signing is unreliable
`expo run:ios --device` fails with "developer disk image could not be mounted" and signing errors. Local builds require a working Xcode signing setup which is fragile across macOS versions and device OS updates.

**Template fix:** Default to EAS cloud builds. Generate `eas.json` with three profiles:
```json
{
  "build": {
    "development": { "developmentClient": true, "distribution": "internal", "ios": { "simulator": true } },
    "preview": { "distribution": "internal" },
    "production": {}
  }
}
```
Document: `eas build --profile preview --platform ios` for device testing, not `expo run:ios --device`.

### EXPO_PUBLIC_* vars must be in eas.json
Like Vite's `VITE_*`, Expo inlines `EXPO_PUBLIC_*` at bundle time. For EAS builds, these must be in each profile's `env` block — not just in `mobile/.env`.

**Template fix:** `eas.json` should include `env` blocks with `EXPO_PUBLIC_API_BASE_URL`, `EXPO_PUBLIC_LOGTO_ENDPOINT`, `EXPO_PUBLIC_LOGTO_APP_ID`, `EXPO_PUBLIC_LOGTO_API_RESOURCE` in all three profiles.

---

## 2. New Architecture (Fabric) — Disabled

### Fabric causes linker errors with native modules
`"newArchEnabled": true` in `app.json` causes `Undefined symbols for architecture arm64` errors on EAS builds when using native modules that haven't fully adopted Fabric (e.g., `ClippingScrollViewDecoratorViewCls` from `react-native-keyboard-controller`).

**Template fix:** Set `"newArchEnabled": false` in the generated `app.json`. Add a comment: `// Fabric disabled — causes linker errors with some native modules on EAS. Revisit on SDK 54+.`

---

## 3. Native Module Compatibility

### react-native-keyboard-controller — incompatible
- With New Arch ON: linker error (Fabric component not registered)
- With New Arch OFF: `KeyboardProvider` blocks rendering — app stuck on loading screen forever

**Template fix:** Do NOT include `react-native-keyboard-controller` in the template. Use RN's built-in `KeyboardAvoidingView` with `behavior="padding"` on iOS and `keyboardVerticalOffset={0}`.

### expo-dev-client — only for development builds
Including `expo-dev-client` in a preview/production build causes the app to hang on initialization (tries to connect to a dev server that doesn't exist).

**Template fix:** Only add `expo-dev-client` to `mobile/package.json` if the user explicitly requests dev builds. Never include it by default.

---

## 4. Splash Screen

### SplashScreen.hideAsync() must use useEffect, not onLayout
The `onLayout` approach has a race condition: it fires when the View's frame changes, but if fonts load after the initial layout, `onLayout` doesn't re-fire. Result: splash screen stays visible forever while the app renders behind it.

**Template fix:** Generate the root layout with:
```tsx
useEffect(() => {
  if (fontsLoaded || fontError) {
    SplashScreen.hideAsync();
  }
}, [fontsLoaded, fontError]);

if (!fontsLoaded && !fontError) return null;
```
Never use `onLayout={onLayoutRootView}` for splash screen dismissal.

---

## 5. KeyboardAvoidingView

### Start with offset 0
Higher `keyboardVerticalOffset` values create a black gap between content and keyboard. The offset accounts for fixed UI elements the system doesn't know about (e.g., custom headers). Start with 0 and increase only if content is cut off.

**Template fix:** Default KeyboardAvoidingView wrapper:
```tsx
<KeyboardAvoidingView
  style={{ flex: 1 }}
  behavior={Platform.OS === "ios" ? "padding" : undefined}
  keyboardVerticalOffset={0}
>
```

---

## 6. EXPO_PUBLIC_* Hot Reload Limitation

### Env var changes don't hot-reload
Changing `EXPO_PUBLIC_*` values in `mobile/.env` and pressing `r` in Metro does NOT pick up the change. The values are inlined during bundling.

**Template fix:** Document in CLAUDE.md: "After changing `EXPO_PUBLIC_*` vars, restart Metro with `npx expo start --clear`." For EAS builds, env vars are set in `eas.json` — no runtime override possible.

---

## 7. Shared Types Must Stay in Sync

### Missing TS fields cause silent data loss
When the backend adds fields to response models (e.g., `images` on `MealResponse`), the TypeScript type in `shared/types/api.ts` must be updated too. The JSON response includes the data, but if the TS interface doesn't declare the field, components silently ignore it. No error, no warning — just missing data.

**Template fix:** Add to CLAUDE.md: "When adding fields to backend Pydantic models, update in THREE places: (1) `app/models/`, (2) `shared/types/api.ts`, (3) web frontend test fixtures. The Dockerfile's `tsc` catches (3) on deploy."

---

## 8. Eager Loading for API Responses

### Missing selectinload produces empty arrays silently
SQLAlchemy async queries that feed response serialization must eagerly load ALL relationships. The `sa_inspect(obj).dict` check returns False for unloaded relationships, causing `*_to_response()` functions to return empty arrays without any error.

**Template fix:** Add a CLAUDE.md pattern: "Every query that feeds a `*_to_response()` must use `selectinload()` for all relationships." Consider generating a lint rule or test helper that verifies all response queries include eager loads.

---

## 9. Local Backend Testing from Simulator

### EXPO_PUBLIC_API_BASE_URL doesn't update reliably
Pointing the mobile app at `http://localhost:8000` by changing `mobile/.env` and restarting Metro often doesn't work — the old value persists. Multiple restart attempts, cache clearing, and env overrides were tried without success.

**Template fix:** Don't rely on local backend testing from the mobile app. Instead: (1) run backend tests with pytest (454 tests), (2) deploy to Railway, (3) rebuild EAS and test on device. Document this workflow in CLAUDE.md rather than fighting Metro's env caching.

---

## 10. .easignore — Gitignore Patterns Match at Any Depth (CRITICAL)

### `app/` excludes mobile/app/ (Expo Router routes)
In a monorepo with a Python backend at `app/` and Expo Router routes at `mobile/app/`, the `.easignore` pattern `app/` (without leading slash) matches BOTH directories. Gitignore-style patterns without a leading `/` match at any depth. EAS uploads the project without the route files, the build succeeds, but the app crashes immediately on launch with `Error: No routes found`. The error only appears in Console.app device logs — not in the EAS build output.

**This is extremely hard to diagnose:** the build succeeds, the JS bundle compiles, but Expo Router finds zero route files at runtime because they were excluded from the upload.

**Template fix:** Always use leading-slash patterns in `.easignore` for root-level exclusions:
```
# WRONG — matches app/ at ANY depth (including mobile/app/)
app/

# RIGHT — matches ONLY root-level app/
/app/
```

**General rule:** Audit ALL `.easignore`, `.dockerignore`, and `.gitignore` patterns in monorepos. Any pattern without a leading `/` can accidentally exclude nested directories with the same name.

---

## 11. react-native-svg v15 Requires New Architecture (Fabric)

### SVG components crash with `newArchEnabled: false`
`react-native-svg` v15 (shipped with Expo SDK 52) uses Fabric (New Architecture) native components. With `newArchEnabled: false`, any SVG component (`<Svg>`, `<Path>`, etc.) crashes with `RNSVGPath` not found. Downgrading to v13 doesn't help — same error.

Enabling `newArchEnabled: true` introduces different crashes: `react-native-keyboard-controller` fails the linker, and SVG components get "Tried to register two views with same name" dual-registration errors.

**Workaround:** Render SVGs as PNG images using `cairosvg` (Python) or a similar tool. For brand logos, render SVG paths to 128x128 PNGs at build time. For icons, use `@expo/vector-icons/Ionicons` instead of SVG.

**Template fix:**
1. Do NOT use `react-native-svg` in any project with `newArchEnabled: false`
2. Include a `scripts/render-svg-to-png.py` utility for converting SVG assets to PNGs
3. Use `@expo/vector-icons` for all UI icons
4. Revisit on Expo SDK 54+ when New Architecture support stabilizes

---

## 12. WeakRef Polyfill Required for React Navigation v7

### `@react-navigation/bottom-tabs` v7 uses WeakRef
Hermes (React Native's JS engine) may not support `WeakRef` in all build configurations. `@react-navigation/bottom-tabs` v7 uses it for cleanup, and the app crashes on launch without a polyfill.

**Template fix:** Add polyfill at the top of `_layout.tsx` (before any imports that use navigation):
```tsx
// WeakRef polyfill for Hermes — required by @react-navigation/bottom-tabs v7
if (typeof WeakRef === 'undefined') {
  (globalThis as any).WeakRef = class WeakRef<T extends object> {
    private ref: T | undefined;
    constructor(target: T) { this.ref = target; }
    deref(): T | undefined { return this.ref; }
  };
}
```

---

## 13. pnpm + EAS Build Compatibility

### Stale package-lock.json at monorepo root breaks EAS
If a `package-lock.json` exists at the monorepo root (leftover from an `npm install`), EAS detects it and uses `npm` instead of `pnpm` for the cloud build — even if `pnpm-workspace.yaml` and `pnpm-lock.yaml` exist. The build fails because npm can't resolve `workspace:*` dependencies.

**Template fix:** Add `package-lock.json` to `.gitignore`. In the bootstrap script, run `rm -f package-lock.json` after monorepo setup.

### `.npmrc` with `node-linker=hoisted` is required
EAS builds with pnpm require hoisted node_modules (not the default symlinked layout) because React Native's Metro bundler can't follow pnpm's `.pnpm/` symlink structure in the cloud build environment.

**Template fix:** Generate `.npmrc` at monorepo root with `node-linker=hoisted`.

---

## 14. Logto — Separate Native App for Mobile

### Mobile needs a "Native" app type, not "SPA"
Logto requires three separate apps when you have backend + web + mobile:
1. **Traditional Web App** — Backend (has `APP_SECRET` for JWT validation)
2. **SPA App** — Web frontend (PKCE, no secret)
3. **Native App** — Mobile (uses `@logto/rn`, PKCE with custom scheme redirect)

The Native app must have redirect URI `<app-scheme>://callback` (using the app's scheme from `app.json`), not `https://`. `@logto/rn` opens the system browser for auth and redirects back via deep link.

**Template fix:** Bootstrap script should create a Native app when `--mobile` is true. Set redirect URI to `{scheme}://callback`.

---

## 15. Bundle ID Conflicts

### Common names are taken on Apple
A short bundle ID (`com.<app>.app`) was already registered by another developer. The fix was `com.agenticapps.<app>`. This only surfaces when you first try to build — EAS fails with "bundle identifier already in use."

**Template fix:** Use a unique org prefix. Generate bundle ID as `com.agenticapps.{app_name}` to avoid conflicts. Set both `ios.bundleIdentifier` and `android.package` in `app.json`.

---

## 16. Feature Parity — Audit Web App Before Building Mobile

### Don't build mobile UI from scratch — port the web UI
Building a mobile UI "from scratch" without systematically auditing the web app leads to missing features, inconsistent behavior, and a visually inferior experience. The web app IS the source of truth for what the user expects.

**Process:**
1. Before implementing ANY mobile screen, open the corresponding web page
2. Screenshot every state (loading, empty, populated, error)
3. List every interactive element, data field, and visual treatment
4. Build the mobile version to match — not to "reimagine"
5. Only deviate from web patterns when mobile UX genuinely requires it (e.g., bottom sheets instead of modals)

### Design system tokens must be shared
Don't create a separate color palette or typography for mobile. Extract the web app's design tokens into the `shared/` package (or at minimum, document them) and use the same values in NativeWind/Tailwind config.

### Shared hooks must decouple from platform UI libraries
When extracting shared hooks (React Query mutations, etc.), they must NOT import platform-specific libraries like `useToast` (web) or `Alert` (mobile). Use callback patterns instead:
```tsx
// WRONG — imports web-only library
import { useToast } from '@/components/ui/use-toast';
const { mutate } = useMutation({ onSuccess: () => toast({ title: 'Done' }) });

// RIGHT — platform-agnostic callbacks
const { mutate } = useMutation({
  onSuccess: () => options?.onSuccess?.(),
  onError: (err) => options?.onError?.(err),
});
```
Each platform's wrapper provides the toast/alert in the callback.

---

## 17. Token Refresh and Auth Error Handling

### Mobile and web need different auth error recovery
Web can redirect to `/login` on 401. Mobile needs to call `signOut()` from `@logto/rn` and navigate to the login screen via Expo Router. Both need:
1. Token dedup — prevent parallel refresh attempts when multiple hooks fire simultaneously
2. QueryClient configured with `retry: false` for 401 responses — retrying an expired token loops forever
3. Try/catch around token refresh — an unhandled refresh error causes infinite render loops

**Template fix:** Generate an `AuthGate` wrapper component that:
- Wraps the authenticated app in a try/catch
- Catches token refresh failures and calls `signOut()`
- Shares a `pendingToken` promise to dedup concurrent refresh requests

---

## 18. pnpm Monorepo Causes Duplicate React in Metro

### Metro resolves two React copies, causing cryptic hooks errors
In a pnpm monorepo with `mobile/` and `shared/` packages, Metro can resolve two separate copies of React — one from `mobile/node_modules/react` and one through the `shared/` package's dependency chain. React hooks require a single React instance; two copies cause `Invalid hook call` errors that look like a bug in your component code.

**Template fix:**
1. The `shared/` package must declare `react` and `react-native` as `peerDependencies`, NOT `dependencies`:
```json
{
  "peerDependencies": {
    "react": "^18.0.0",
    "react-native": "*"
  }
}
```
2. Metro config must force a single React resolution:
```js
// metro.config.js
config.resolver.resolveRequest = (context, moduleName, platform) => {
  if (moduleName === 'react' || moduleName === 'react-native') {
    return context.resolveRequest(context, moduleName, platform, {
      mainFields: ['react-native', 'browser', 'main'],
    });
  }
  return context.resolveRequest(context, moduleName, platform);
};
```

---

## 19. @logto/rn v1.x Locks You to Expo SDK 52 (React 18)

### Cannot upgrade to Expo SDK 54+ until Logto updates their RN SDK
`@logto/rn` v1.x depends on React 18. Expo SDK 54 ships React 19. Installing `@logto/rn` in an SDK 54 project causes peer dependency conflicts and runtime crashes. This isn't obvious until you try to upgrade — the error surfaces as a React version mismatch deep in the dependency tree.

**Template fix:** Pin Expo SDK version to 52 in `app.json` and `package.json` when using Logto auth. Add a comment: `// Locked to SDK 52 — @logto/rn v1.x requires React 18. Revisit when @logto/rn v2 ships.` Check https://github.com/logto-io/js/issues for React 19 support status before upgrading.

---

## 20. Railway Security Scanner Blocks on Dev Dependency CVEs in Lockfile

### pnpm-lock.yaml CVEs in transitive dev deps block production deploys
Railway's security scanner runs pre-build and checks the entire `pnpm-lock.yaml` for CVEs — including transitive dependencies of devDependencies that never reach the production Docker image. A critical CVE in `react-server-dom-webpack@19.0.0-rc` (transitive from `jest-expo`) blocked deployment despite the Dockerfile only installing production deps.

**Template fix:** Use `pnpm.overrides` in the root `package.json` to force-upgrade vulnerable transitive deps:
```json
{
  "pnpm": {
    "overrides": {
      "react-server-dom-webpack": ">=19.0.2"
    }
  }
}
```
After adding overrides, run `pnpm install` to regenerate the lockfile. Add to CLAUDE.md: "If Railway blocks a deploy for a CVE, check `pnpm ls <package>` to find the dependency chain, then add a `pnpm.overrides` entry."

---

## 21. @babel/runtime Must Be a Direct Dependency for Metro + pnpm

### Metro can't resolve transitive-only @babel/runtime in pnpm monorepos
In a pnpm monorepo, Metro fails to resolve `@babel/runtime` if it's only present as a transitive dependency (e.g., via `expo` or `@react-navigation/native`). The error is a cryptic "Unable to resolve module" that doesn't mention `@babel/runtime` by name — it surfaces as a failure to transform files that import from packages that depend on it.

**Hit in:** two early apps (both added it as a direct dependency).

**Template fix:** Always include `"@babel/runtime": "^7.28.0"` as a direct dependency in `mobile/package.json`. This costs nothing (it's already installed transitively) but ensures Metro can resolve it through pnpm's symlinked `node_modules/.pnpm/` structure.

---

## 22. Shared Package Needs Docker Symlink for Peer Dependency Resolution

### tsc can't resolve shared/'s peerDependencies during Docker build
When a `shared/` workspace package declares `react` and `@tanstack/react-query` as `peerDependencies`, the frontend build stage in Docker can resolve them locally (they're in `frontend/node_modules`). But `tsc` also needs to compile `shared/` source files, and `shared/` has no `node_modules` of its own in the Docker context — so `tsc` fails with `Cannot find module 'react'` when checking shared's type imports.

**Hit in:** an early app. Build succeeded locally (pnpm hoists everything) but failed in Docker where each package is isolated.

**Workaround:** Symlink shared's node_modules to frontend's in the Dockerfile:
```dockerfile
COPY shared/ ./shared/
COPY frontend/package.json frontend/package-lock.json ./frontend/
WORKDIR /build/frontend
RUN npm ci
# Shared package peer deps must be resolvable for tsc
RUN cd /build/shared && ln -s /build/frontend/node_modules node_modules
COPY frontend/ ./
RUN npm run build
```

**Template fix:** When `include_frontend` and `include_mobile` are both true (monorepo with shared/), the Dockerfile must:
1. COPY `shared/` into the frontend build stage
2. Symlink `shared/node_modules` → `frontend/node_modules` after `npm ci`
3. If shared gains new peer deps, they must also be in frontend's dependencies

**Note:** This only affects the Docker build (Railway deploy). Local dev with pnpm hoisting resolves everything automatically.

---

## 23. Jest transformIgnorePatterns for pnpm Monorepos

### Default jest-expo transform pattern doesn't match pnpm's nested structure
With pnpm, packages live at `node_modules/.pnpm/<pkg>/node_modules/<pkg>/` instead of the flat `node_modules/<pkg>/`. The default `transformIgnorePatterns` from `jest-expo` skips transforming these, causing `SyntaxError: Unexpected token 'export'` on any RN package that ships ESM.

**Hit in:** both early apps had to customize this.

**Template fix:** Generate `mobile/jest.config.js` with a pnpm-aware pattern:
```js
transformIgnorePatterns: [
  "node_modules/(?!(.pnpm/.*/(node_modules/)?)?((jest-)?react-native|@react-native(-community)?|expo(nent)?|@expo(nent)?|@expo-google-fonts|react-navigation|@react-navigation|@unimodules|unimodules|nativewind|@APPNAME/shared|@react-native-async-storage|@tanstack))",
],
```
The `(.pnpm/.*/(node_modules/)?)?` prefix matches both pnpm's nested layout and npm's flat layout.

**Note:** When adding new native/ESM packages, they may need to be added to this pattern. If mobile tests fail with `SyntaxError: Unexpected token 'export'`, add the package name to the pattern.

---

## 24. EAS installCommand for pnpm Monorepo Root

### EAS cloud builds need to run pnpm install from monorepo root
By default, EAS runs `npm install` or `yarn install` in the project directory (`mobile/`). In a pnpm monorepo, this fails because workspace links (`workspace:*`) can only be resolved from the root where `pnpm-workspace.yaml` lives.

**Hit in:** an early app — build failed until `installCommand` was added.

**Template fix:** Add to `eas.json` base config:
```json
{
  "build": {
    "base": {
      "node": {
        "installCommand": "cd .. && pnpm install --frozen-lockfile"
      }
    }
  }
}
```

**Note:** This is separate from `.npmrc` with `node-linker=hoisted` (lesson 13). Both are required: `.npmrc` tells pnpm to use hoisted layout, `installCommand` tells EAS to run pnpm from the right directory.

---

## Bootstrap Script Implications

If/when agentic-core adds a `--mobile` flag to the bootstrap script:

1. Generate `mobile/` directory with expo-router tabs layout
2. Generate `eas.json` with three profiles + env vars
3. Generate `app.json` with `newArchEnabled: false`, bundle ID `com.agenticapps.{name}`
4. Generate root `_layout.tsx` with `useEffect`-based splash screen + WeakRef polyfill
5. Generate `shared/` workspace package with types + API client + hooks (onSuccess/onError callbacks, no platform imports)
6. Add mobile commands to CLAUDE.md
7. Do NOT include `expo-dev-client`, `react-native-keyboard-controller`, or `react-native-svg`
8. Set up pnpm workspace in root `pnpm-workspace.yaml` including `shared/` and `mobile/`
9. Generate `.npmrc` with `node-linker=hoisted` at monorepo root
10. Generate `.easignore` with LEADING SLASHES on root-level exclusions (`/app/` not `app/`)
11. Add `package-lock.json` to `.gitignore`
12. Register a Native Logto app (separate from SPA) with `{scheme}://callback` redirect
13. Generate `AuthGate` component with token dedup and auth error recovery
14. Include `scripts/render-svg-to-png.py` for SVG → PNG asset conversion
15. Use `@expo/vector-icons/Ionicons` for all icons (not SVG components)
16. In `shared/` package, declare `react` and `react-native` as `peerDependencies` (not `dependencies`)
17. Generate `metro.config.js` with `resolveRequest` override to force single React instance
18. Pin Expo SDK 52 in `app.json` + `package.json` when using `@logto/rn` (React 18 lock)
19. Add `pnpm.overrides` in root `package.json` for known CVE packages (e.g., `react-server-dom-webpack`)
20. Include `@babel/runtime` as direct dependency in `mobile/package.json` (Metro+pnpm can't resolve it transitively)
21. Dockerfile must symlink `shared/node_modules` → `frontend/node_modules` when shared/ package has peerDependencies
22. Generate `mobile/jest.config.js` with pnpm-aware `transformIgnorePatterns` (add new ESM packages to the pattern as needed)
23. Generate `eas.json` with `base.node.installCommand: "cd .. && pnpm install --frozen-lockfile"` for monorepo root resolution
