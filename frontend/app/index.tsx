import { Redirect } from "expo-router";

// "/" used to be the Funds tab. Fund Search now lives in the drawer, so the
// app root lands on Home (ModeGuard still sends first-run users to onboarding).
export default function Index() {
  return <Redirect href={"/home" as any} />;
}
