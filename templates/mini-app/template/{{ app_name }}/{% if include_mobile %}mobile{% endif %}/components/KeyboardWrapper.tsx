import React from 'react';
import { KeyboardAvoidingView, Platform, StyleSheet } from 'react-native';

// KeyboardAvoidingView wrapper — uses built-in RN, NOT react-native-keyboard-controller (lesson #3, #5)
// Start with offset 0 — higher values create a black gap between content and keyboard.

type Props = {
  children: React.ReactNode;
};

export function KeyboardWrapper({ children }: Props) {
  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      keyboardVerticalOffset={0}
    >
      {children}
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
  },
});
