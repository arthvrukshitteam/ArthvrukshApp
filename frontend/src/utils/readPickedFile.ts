// Read a file picked with expo-document-picker as base64, on web and native.
//
// Native: expo-file-system (the same call Portfolio Review uses).
// Web: the picker hands back a real File; FileReader turns it into a data URL.

import { Platform } from "react-native";
import * as FileSystem from "expo-file-system/legacy";

export async function readPickedFileBase64(asset: { uri: string; file?: any }): Promise<string> {
  if (Platform.OS === "web") {
    const file: Blob | undefined = asset.file;
    if (file) {
      return new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(reader.error ?? new Error("Couldn't read the file."));
        reader.onload = () => {
          const url = String(reader.result ?? "");
          resolve(url.slice(url.indexOf(",") + 1));
        };
        reader.readAsDataURL(file);
      });
    }
    const res = await fetch(asset.uri);
    const blob = await res.blob();
    return readPickedFileBase64({ uri: asset.uri, file: blob });
  }
  return FileSystem.readAsStringAsync(asset.uri, { encoding: FileSystem.EncodingType.Base64 });
}
