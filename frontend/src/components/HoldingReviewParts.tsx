/**
 * Presentational pieces for the Portfolio Holding Review screen
 * (app/(drawer)/holding-review.tsx). No data fetching here — the screen owns
 * state and talks to src/api.ts; these components only render and report edits.
 */

import { useState } from "react";
import { View, Text, TextInput, Pressable, StyleSheet, ScrollView } from "react-native";
import { AlertTriangle, CheckCircle2, CircleDashed, HelpCircle, Trash2 } from "lucide-react-native";
import type {
  InvestmentType, ReviewFund, ReviewFundStatus, ReviewMetricChange, ReviewMetricKey, ReviewMetrics,
} from "@/src/api";
import { useTheme } from "@/src/ThemeProvider";
import { radii, space } from "@/src/theme";

export const METRICS: { key: ReviewMetricKey; label: string; pct: boolean; dp: number }[] = [
  { key: "fqs", label: "FQS", pct: false, dp: 1 },
  { key: "wm_p10", label: "WM_P10", pct: false, dp: 2 },
  { key: "wm_med", label: "WM_Med", pct: false, dp: 2 },
  { key: "irr_p10", label: "XIRR_P10", pct: true, dp: 1 },
  { key: "irr_med", label: "XIRR_Med", pct: true, dp: 1 },
  { key: "cv", label: "CV", pct: true, dp: 1 },
];
export const INVESTMENT_TYPES: InvestmentType[] = ["SIP", "Lumpsum", "COB", "External"];

/** Blank stays blank ("–") — never 0. IRR/CV arrive as fractions. */
export function fmtMetric(key: ReviewMetricKey, v: number | null | undefined): string {
  if (v == null) return "–";
  const m = METRICS.find((x) => x.key === key)!;
  return m.pct ? `${(v * 100).toFixed(m.dp)}%` : v.toFixed(m.dp);
}

export function fmtChange(c: ReviewMetricChange | null | undefined): string {
  if (!c) return "–";
  const sign = c.abs > 0 ? "+" : "";
  if (c.unit === "pp") return `${sign}${c.abs.toFixed(1)} pp`;
  const base = `${sign}${c.abs.toFixed(c.unit === "x" ? 2 : 1)}`;
  return c.pct == null ? base : `${base} (${c.pct > 0 ? "+" : ""}${c.pct.toFixed(1)}%)`;
}

/** For FQS/WM/XIRR a rise is good; for CV (volatility) a rise is bad. */
export function changeTone(key: ReviewMetricKey, c: ReviewMetricChange | null | undefined): "good" | "bad" | "flat" {
  if (!c || c.abs === 0) return "flat";
  const up = c.abs > 0;
  return (key === "cv" ? !up : up) ? "good" : "bad";
}

export function errMsg(e: unknown): string {
  const raw = String((e as any)?.message ?? e ?? "");
  const m = raw.match(/^HTTP (\d+): (.*)$/s);
  if (!m) return raw || "Something went wrong.";
  try {
    const d = JSON.parse(m[2]).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x: any) => x?.msg ?? String(x)).join("; ");
  } catch { /* not JSON */ }
  return m[2] || raw;
}

// ── status badge ─────────────────────────────────────────────────────────────

const BADGE: Record<ReviewFundStatus, { label: string; tone: "ok" | "warn" | "muted" | "bad" }> = {
  extracted: { label: "Extracted", tone: "muted" },
  verified: { label: "Verified", tone: "ok" },
  missing: { label: "Missing", tone: "bad" },
  needs_review: { label: "Needs review", tone: "warn" },
};

export function StatusBadge({ status }: { status: ReviewFundStatus }) {
  const { colors } = useTheme();
  const b = BADGE[status];
  const c = b.tone === "ok" ? colors.emerald : b.tone === "warn" ? colors.amber : b.tone === "bad" ? colors.ruby : colors.textSec;
  const Icon = b.tone === "ok" ? CheckCircle2 : b.tone === "warn" ? AlertTriangle : b.tone === "bad" ? HelpCircle : CircleDashed;
  return (
    <View style={[st.badge, { borderColor: c }]}>
      <Icon size={12} color={c} />
      <Text style={{ color: c, fontSize: 11, fontWeight: "600" }}>{b.label}</Text>
    </View>
  );
}

// ── inputs ───────────────────────────────────────────────────────────────────

export function LabeledInput({
  label, value, onChange, placeholder, keyboardType, flag, editable = true, width,
}: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string;
  keyboardType?: "default" | "numeric"; flag?: boolean; editable?: boolean; width?: number | string;
}) {
  const { colors } = useTheme();
  return (
    <View style={{ flex: width ? undefined : 1, width: width as any, minWidth: 120 }}>
      <Text style={[st.label, { color: flag ? colors.amber : colors.textSec }]}>{label}{flag ? "  · needs review" : ""}</Text>
      <TextInput
        value={value}
        onChangeText={onChange}
        placeholder={placeholder}
        placeholderTextColor={colors.textDis}
        keyboardType={keyboardType}
        editable={editable}
        autoCapitalize="none"
        style={[st.input, { color: colors.textPri, borderColor: flag ? colors.amber : colors.border, backgroundColor: colors.bgCard, opacity: editable ? 1 : 0.6 }]}
      />
    </View>
  );
}

/** Numeric text box that commits on blur; blank commits null (never 0). */
function MetricInput({
  label, value, pct, dp, editable, onCommit,
}: {
  label: string; value: number | null; pct: boolean; dp: number; editable: boolean;
  onCommit: (v: number | null) => void;
}) {
  const { colors } = useTheme();
  const show = (v: number | null) => (v == null ? "" : String(+(pct ? v * 100 : v).toFixed(dp + 1)));
  const [text, setText] = useState(show(value));
  // Re-sync when the saved value changes (e.g. after the server round-trip) — derived during render.
  const [seen, setSeen] = useState(value);
  if (seen !== value) { setSeen(value); setText(show(value)); }
  return (
    <View style={{ width: 78 }}>
      <Text style={[st.label, { color: colors.textSec }]}>{label}</Text>
      <TextInput
        value={text}
        editable={editable}
        onChangeText={setText}
        onEndEditing={() => {
          const t = text.trim();
          if (t === "") return onCommit(null);
          const n = Number(t);
          if (Number.isFinite(n)) onCommit(pct ? n / 100 : n);
          else setText(show(value));
        }}
        placeholder="–"
        placeholderTextColor={colors.textDis}
        keyboardType="numeric"
        style={[st.input, { color: colors.textPri, borderColor: colors.border, backgroundColor: colors.bgCard, paddingHorizontal: 8 }]}
      />
    </View>
  );
}

// ── fund edit card (verification step) ───────────────────────────────────────

export function FundEditCard({
  fund, editable, onChange, onRemove, onPickScheme,
}: {
  fund: ReviewFund; editable: boolean;
  onChange: (patch: Partial<ReviewFund>) => void;
  onRemove: () => void;
  onPickScheme: (code?: number) => void;       // no arg → open the search modal
}) {
  const { colors } = useTheme();
  const mapped = fund.scheme_code != null;
  const lowConf = fund.match.confidence === "low" || fund.match.confidence === "medium";
  const noProposal = fund.investment_type === "COB" || fund.investment_type === "External";
  return (
    <View style={[st.card, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
      <View style={st.rowBetween}>
        <View style={{ flex: 1, paddingRight: 8 }}>
          <Text style={{ color: colors.textPri, fontWeight: "700", fontSize: 14 }}>
            {fund.scheme_name ?? fund.fund_name_pdf ?? "New fund"}
          </Text>
          {fund.fund_name_pdf && fund.scheme_name && fund.fund_name_pdf !== fund.scheme_name ? (
            <Text style={{ color: colors.textTer, fontSize: 11 }}>In uploaded PDF: {fund.fund_name_pdf}</Text>
          ) : null}
        </View>
        <StatusBadge status={fund.status} />
        {editable ? (
          <Pressable onPress={onRemove} hitSlop={8} style={{ marginLeft: 10 }} accessibilityLabel="Remove fund">
            <Trash2 size={16} color={colors.textTer} />
          </Pressable>
        ) : null}
      </View>

      {!mapped || lowConf ? (
        <View style={[st.notice, { borderColor: colors.amber, backgroundColor: colors.azureSoft }]}>
          <Text style={{ color: colors.textPri, fontSize: 12, fontWeight: "600" }}>
            {mapped
              ? `Matched to “${fund.scheme_name}” — confidence ${fund.match.confidence}. Please confirm.`
              : `Unable to map fund: ${fund.fund_name_pdf ?? "unnamed"}`}
          </Text>
          {!mapped && fund.match.candidates.length > 0 ? (
            <Text style={{ color: colors.textSec, fontSize: 11, marginTop: 2 }}>Possible matches (low confidence):</Text>
          ) : null}
          {!mapped && fund.match.candidates.map((c) => (
            <Pressable key={c.scheme_code} disabled={!editable} onPress={() => onPickScheme(c.scheme_code)} style={st.cand}>
              <Text style={{ color: colors.azure, fontSize: 12, flex: 1 }}>{c.scheme_name}</Text>
              <Text style={{ color: colors.textTer, fontSize: 11 }}>{Math.round(c.similarity * 100)}% · Confirm</Text>
            </Pressable>
          ))}
          {editable ? (
            <Pressable onPress={() => onPickScheme()} style={{ marginTop: 6 }}>
              <Text style={{ color: colors.azure, fontWeight: "700", fontSize: 12 }}>
                {mapped ? "Choose another fund" : "Search & choose the correct fund"}
              </Text>
            </Pressable>
          ) : null}
        </View>
      ) : null}

      <View style={[st.wrapRow, { marginTop: 8 }]}>
        {INVESTMENT_TYPES.map((t) => {
          const on = fund.investment_type === t;
          return (
            <Pressable
              key={t}
              disabled={!editable}
              onPress={() => onChange({ investment_type: t })}
              style={[st.chip, { borderColor: on ? colors.azure : colors.border, backgroundColor: on ? colors.azureSoft : "transparent" }]}
            >
              <Text style={{ color: on ? colors.azure : colors.textSec, fontWeight: "600", fontSize: 12 }}>{t}</Text>
            </Pressable>
          );
        })}
        <View style={{ width: 90 }}>
          <LabeledInput
            label="Rolling yr"
            value={fund.rolling_year != null ? String(fund.rolling_year) : ""}
            onChange={(v) => onChange({ rolling_year: v.trim() === "" ? null : parseInt(v, 10) || null })}
            keyboardType="numeric" editable={editable} width={90}
            flag={fund.issues.includes("rolling_year")}
          />
        </View>
      </View>

      <Text style={[st.label, { color: colors.textSec, marginTop: 8 }]}>
        {noProposal ? "Proposal data not applicable for COB / External — leave blank" : "Previous (proposal) values — blank stays blank"}
      </Text>
      <View style={st.wrapRow}>
        {METRICS.map((m) => (
          <MetricInput
            key={m.key} label={m.label} value={fund.previous[m.key]} pct={m.pct} dp={m.dp}
            editable={editable && !noProposal}
            onCommit={(v) => onChange({ previous: { ...fund.previous, [m.key]: v } as ReviewMetrics })}
          />
        ))}
      </View>
    </View>
  );
}

// ── comparison card (then vs now) ────────────────────────────────────────────

export function ComparisonCard({ fund }: { fund: ReviewFund }) {
  const { colors } = useTheme();
  const noProposal = fund.investment_type === "COB" || fund.investment_type === "External";
  const tone = (t: "good" | "bad" | "flat") => (t === "good" ? colors.emerald : t === "bad" ? colors.ruby : colors.textSec);
  return (
    <View style={[st.card, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
      <View style={st.rowBetween}>
        <Text style={{ color: colors.textPri, fontWeight: "700", fontSize: 14, flex: 1 }}>
          {fund.scheme_name ?? fund.fund_name_pdf}
        </Text>
        <Text style={{ color: colors.textTer, fontSize: 11 }}>
          {fund.investment_type} · {fund.rolling_year}Y{fund.current?.horizon && fund.current.horizon !== fund.rolling_year ? ` (now ${fund.current.horizon}Y)` : ""}
        </Text>
      </View>
      {fund.current_status === "error" || fund.current_status === "unavailable" ? (
        <Text style={{ color: colors.ruby, fontSize: 12, marginTop: 6 }}>
          {fund.current_error ?? "Not enough history for current figures at this rolling year."}
        </Text>
      ) : null}
      <View style={{ marginTop: 6 }}>
        <View style={st.cmpRow}>
          <Text style={[st.cmpHead, { flex: 1.1 }]} />
          {["Previous", "Current", "Change"].map((h) => (
            <Text key={h} style={[st.cmpHead, { color: colors.textTer, flex: h === "Change" ? 1.6 : 1 }]}>{h}</Text>
          ))}
        </View>
        {METRICS.map((m) => {
          const ch = fund.changes?.[m.key];
          return (
            <View key={m.key} style={[st.cmpRow, { borderTopColor: colors.border }]}>
              <Text style={{ flex: 1.1, color: colors.textSec, fontSize: 12 }}>{m.label}</Text>
              <Text style={[st.cmpVal, { color: colors.textPri }]}>{noProposal ? "–" : fmtMetric(m.key, fund.previous[m.key])}</Text>
              <Text style={[st.cmpVal, { color: colors.textPri, fontWeight: "700" }]}>{fmtMetric(m.key, fund.current?.[m.key])}</Text>
              <Text style={[st.cmpVal, { flex: 1.6, color: tone(changeTone(m.key, ch)) }]}>{fmtChange(ch)}</Text>
            </View>
          );
        })}
      </View>
    </View>
  );
}

// ── report preview (mirrors the PDF table) ───────────────────────────────────

export function PreviewTable({ funds }: { funds: ReviewFund[] }) {
  const { colors } = useTheme();
  const W = { name: 190, small: 62, num: 66 };
  const head = (t: string, w: number, bg?: string) => (
    <Text style={[st.pvHead, { width: w, backgroundColor: bg ?? colors.bgElev, color: colors.textPri }]}>{t}</Text>
  );
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator>
      <View>
        <View style={{ flexDirection: "row" }}>
          {head("Fund Name", W.name)}{head("Rolling Yr", W.small)}{head("Investment", 78)}
          {head("Proposal Data as per FQS Model", W.num * 6, colors.bgElev)}
          {head("Current Review Data as per FQS Model", W.num * 6, colors.azureSoft)}
        </View>
        <View style={{ flexDirection: "row" }}>
          {head("", W.name)}{head("", W.small)}{head("", 78)}
          {[0, 1].flatMap((blk) => METRICS.map((m) => <View key={`${blk}${m.key}`}>{head(m.label, W.num)}</View>))}
        </View>
        {funds.map((f) => {
          const noP = f.investment_type === "COB" || f.investment_type === "External";
          const cell = (t: string, w: number, bold?: boolean, color?: string) => (
            <Text style={[st.pvCell, { width: w, color: color ?? colors.textPri, borderColor: colors.border, fontWeight: bold ? "700" : "400" }]}>{t}</Text>
          );
          return (
            <View key={f.fid} style={{ flexDirection: "row" }}>
              {cell(f.scheme_name ?? f.fund_name_pdf ?? "", W.name)}
              {cell(String(f.rolling_year ?? "–"), W.small)}
              {cell(f.investment_type, 78)}
              {METRICS.map((m) => <View key={`p${m.key}`}>{cell(noP ? "–" : fmtMetric(m.key, f.previous[m.key]), W.num)}</View>)}
              {METRICS.map((m) => <View key={`c${m.key}`}>{cell(fmtMetric(m.key, f.current?.[m.key]), W.num, m.key === "fqs")}</View>)}
            </View>
          );
        })}
      </View>
    </ScrollView>
  );
}

const st = StyleSheet.create({
  badge: { flexDirection: "row", alignItems: "center", gap: 4, borderWidth: 1, borderRadius: radii.pill, paddingHorizontal: 8, paddingVertical: 2 },
  label: { fontSize: 11, fontWeight: "600", marginBottom: 3 },
  input: { borderWidth: 1, borderRadius: radii.sm, paddingHorizontal: 10, paddingVertical: 8, fontSize: 14 },
  card: { borderWidth: 1, borderRadius: radii.md, padding: space.md, marginBottom: space.sm },
  rowBetween: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
  wrapRow: { flexDirection: "row", flexWrap: "wrap", gap: 8, alignItems: "flex-end" },
  chip: { borderWidth: 1, borderRadius: radii.pill, paddingHorizontal: 12, paddingVertical: 7 },
  notice: { borderWidth: 1, borderRadius: radii.sm, padding: 10, marginTop: 8 },
  cand: { flexDirection: "row", alignItems: "center", gap: 8, paddingVertical: 6 },
  cmpRow: { flexDirection: "row", paddingVertical: 5, borderTopWidth: StyleSheet.hairlineWidth },
  cmpHead: { flex: 1, fontSize: 11, fontWeight: "600", textAlign: "right" },
  cmpVal: { flex: 1, fontSize: 12, textAlign: "right" },
  pvHead: { fontSize: 11, fontWeight: "700", textAlign: "center", paddingVertical: 7, paddingHorizontal: 4 },
  pvCell: { fontSize: 12, textAlign: "center", paddingVertical: 7, paddingHorizontal: 4, borderTopWidth: StyleSheet.hairlineWidth },
});
