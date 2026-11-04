import { useEffect } from "react";
import { Stack, useRouter, useSegments } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { ThemeProvider, useTheme } from "@/src/ThemeProvider";
import { ViewModeProvider } from "@/src/ViewModeProvider";
import { ModeProvider, useMode } from "@/src/ModeProvider";
import { TenureProvider, useTenure } from "@/src/TenureProvider";
import { ProposalDraftProvider } from "@/src/ProposalDraftProvider";
import { AppLockProvider } from "@/src/AppLockProvider";
import { AppLockGate } from "@/src/components/AppLock";

function ModeGuard({ children }: { children: React.ReactNode }) {
  const { mode, loading: modeLoading } = useMode();
  const { tenure, loading: tenureLoading } = useTenure();
  const router = useRouter();
  const segments = useSegments();

  useEffect(() => {
    if (modeLoading || tenureLoading) return;

    const onModePicker   = segments[0] === "mode-picker";
    const onTenurePicker = segments[0] === "tenure-picker";

    if (mode === null) {
      if (!onModePicker) router.replace("/mode-picker" as any);
    } else if (tenure === null) {
      if (!onTenurePicker && !onModePicker) {
        router.replace("/tenure-picker" as any);
      }
    }
  }, [mode, tenure, modeLoading, tenureLoading, segments]);

  return <>{children}</>;
}

function ThemedStack() {
  const { colors, scheme } = useTheme();
  const { mode } = useMode();
  const { tenure } = useTenure();
  return (
    <GestureHandlerRootView style={{ flex: 1, backgroundColor: colors.bg }}>
      <SafeAreaProvider>
        <StatusBar style={scheme === "light" ? "dark" : "light"} />
        <AppLockGate onboarded={mode !== null && tenure !== null}>
        <ModeGuard>
          <Stack
            screenOptions={{
              headerStyle: { backgroundColor: colors.bg },
              headerTintColor: colors.textPri,
              headerTitleStyle: { fontWeight: "600" },
              contentStyle: { backgroundColor: colors.bg },
              headerShadowVisible: false,
            }}
          >
            <Stack.Screen name="(drawer)" options={{ headerShown: false }} />
            <Stack.Screen name="fund/[code]" options={{ title: "" }} />
            <Stack.Screen
              name="mode-picker"
              options={{ title: "Analysis Mode", headerShown: false, gestureEnabled: false }}
            />
            <Stack.Screen
              name="tenure-picker"
              options={{ title: "Set Tenure", headerShown: false }}
            />
            <Stack.Screen
              name="data-refresh"
              options={{ title: "Data Refresh" }}
            />
            <Stack.Screen
              name="proposal/build"
              options={{ title: "Build Proposal" }}
            />
            <Stack.Screen
              name="proposal/[id]"
              options={{ title: "Proposal" }}
            />
            <Stack.Screen
              name="security"
              options={{ title: "App Lock (PIN)" }}
            />
          </Stack>
        </ModeGuard>
        </AppLockGate>
      </SafeAreaProvider>
    </GestureHandlerRootView>
  );
}

export default function RootLayout() {
  return (
    <ThemeProvider>
      <ViewModeProvider>
        <ModeProvider>
          <TenureProvider>
            <ProposalDraftProvider>
              <AppLockProvider>
                <ThemedStack />
              </AppLockProvider>
            </ProposalDraftProvider>
          </TenureProvider>
        </ModeProvider>
      </ViewModeProvider>
    </ThemeProvider>
  );
}
