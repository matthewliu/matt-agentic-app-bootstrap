// WeakRef polyfill for Hermes — required by @react-navigation/bottom-tabs v7 (lesson #12)
if (typeof WeakRef === 'undefined') {
  (globalThis as any).WeakRef = class WeakRef<T extends object> {
    private ref: T | undefined;
    constructor(target: T) { this.ref = target; }
    deref(): T | undefined { return this.ref; }
  };
}

import { useEffect } from 'react';
import { Stack } from 'expo-router';
import { useFonts } from 'expo-font';
import * as SplashScreen from 'expo-splash-screen';
import { StatusBar } from 'expo-status-bar';

// Prevent the splash screen from auto-hiding
SplashScreen.preventAutoHideAsync();

const tabsOptions = { headerShown: false };

export default function RootLayout() {
  // Load fonts — add custom fonts here as needed
  // e.g. { 'Inter-Bold': require('../assets/fonts/Inter-Bold.ttf') }
  const [fontsLoaded, fontError] = useFonts({});

  // Use useEffect for splash screen dismissal — NOT onLayout (lesson #4)
  // onLayout has a race condition: it fires on frame change but doesn't
  // re-fire when fonts finish loading after the initial layout.
  useEffect(() => {
    if (fontsLoaded || fontError) {
      SplashScreen.hideAsync();
    }
  }, [fontsLoaded, fontError]);

  if (!fontsLoaded && !fontError) return null;

  return (
    <>
      <StatusBar style="auto" />
      <Stack>
        <Stack.Screen name="(tabs)" options={tabsOptions} />
      </Stack>
    </>
  );
}
