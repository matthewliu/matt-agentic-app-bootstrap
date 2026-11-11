import { Tabs } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';

const tabScreenOptions = {
  tabBarActiveTintColor: '#007AFF',
  headerShown: true,
};

const homeOptions = {
  title: 'Home',
  tabBarIcon: ({ color, size }: { color: string; size: number }) => (
    <Ionicons name="home-outline" size={size} color={color} />
  ),
};

export default function TabLayout() {
  return (
    <Tabs screenOptions={tabScreenOptions}>
      <Tabs.Screen name="index" options={homeOptions} />
    </Tabs>
  );
}
