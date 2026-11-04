
import { Image, StyleSheet, View, type ViewStyle } from "react-native";
import { radii, space } from "@/src/theme";
import { useTheme } from "@/src/ThemeProvider";

export function BrandLogo({ size = 80, style }: { size?: number; style?: ViewStyle }) {
  const { colors } = useTheme();
  return (
    <View
      style={[
        styles.badge,
        {
          width: size,
          height: size,
          borderRadius: radii.sm,
          backgroundColor: "#FFFFFF",
          borderColor: colors.border,
        },
        style,
      ]}
    >
      <Image
        source={require("../../assets/images/icon.png")}
        style={{ width: size * 0.9, height: size * 0.9 }}
        resizeMode="contain"
      />
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
    marginBottom: space.sm,
  },
});
