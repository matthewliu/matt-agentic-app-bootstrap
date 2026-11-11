// Learn more https://docs.expo.dev/guides/customizing-metro
const { getDefaultConfig } = require('expo/metro-config');

/** @type {import('expo/metro-config').MetroConfig} */
const config = getDefaultConfig(__dirname);

// Force single React instance in pnpm monorepo (lesson #18)
// Without this, Metro resolves two copies of React (one from mobile/,
// one through shared/), causing "Invalid hook call" errors.
config.resolver.resolveRequest = (context, moduleName, platform) => {
  if (moduleName === 'react' || moduleName === 'react-native') {
    return context.resolveRequest(context, moduleName, platform, {
      mainFields: ['react-native', 'browser', 'main'],
    });
  }
  return context.resolveRequest(context, moduleName, platform);
};

module.exports = config;
