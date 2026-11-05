/**
 * ProposalDraftProvider — in-memory "which funds is the advisor building a
 * proposal from" state, shared between the Home ranked-fund list (where
 * selection happens) and the Proposal Builder screen (which consumes the
 * selection instead of asking the advisor to search for funds a second time).
 *
 * Deliberately NOT persisted to AsyncStorage — this is a working draft for
 * the current session, not a long-term preference like mode/tenure.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";

export interface DraftFund {
  scheme_code: number;
  scheme_name: string;
  /** Start-date filter that was on screen when the fund was added (Fund
   *  Detail's "Since 2020" etc., or a Home card's AMC-period choice). The
   *  proposal scores this fund from that date, not since inception. */
  start_date?: string | null;
  /** False = own-code NAVs only ("new management only"). */
  include_predecessor?: boolean;
  /** The basis above was chosen for this fund on purpose (Fund Detail, a
   *  Home card's AMC period), so it wins over the Home-wide toggles — even
   *  when it is plain "since inception". */
  explicit_basis?: boolean;
}

interface ProposalDraftContextValue {
  selectionMode: boolean;
  selectedFunds: DraftFund[];
  enterSelectionMode: () => void;
  exitSelectionMode: () => void;
  toggleFund: (fund: DraftFund) => void;
  /** Keeps an already-selected fund's basis in sync when the advisor changes
   *  the filter after adding it. No-op for a fund that isn't selected. */
  updateFund: (scheme_code: number, patch: Partial<Omit<DraftFund, "scheme_code">>) => void;
  isSelected: (scheme_code: number) => boolean;
  getFund: (scheme_code: number) => DraftFund | undefined;
  clearSelection: () => void;
  /** Home screen's "Since 2010" / "Separate Management Periods" toggle
   *  state, captured the moment the advisor taps "Build Proposal" — see
   *  setToggleContext. Without this, the Proposal Builder had no way to
   *  know these were on, and silently rebuilt every fund from its plain
   *  since-inception numbers even though Home was showing the 2010/Management
   *  variant when the funds were picked — two different FQS/XIRR for the
   *  same fund with no indication why. */
  since2010: boolean;
  separateManagement: boolean;
  setToggleContext: (since2010: boolean, separateManagement: boolean) => void;
}

const ProposalDraftContext = createContext<ProposalDraftContextValue>({
  selectionMode: false,
  selectedFunds: [],
  enterSelectionMode: () => {},
  exitSelectionMode: () => {},
  toggleFund: () => {},
  updateFund: () => {},
  isSelected: () => false,
  getFund: () => undefined,
  clearSelection: () => {},
  since2010: false,
  separateManagement: false,
  setToggleContext: () => {},
});

export function ProposalDraftProvider({ children }: { children: React.ReactNode }) {
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedFunds, setSelectedFunds] = useState<DraftFund[]>([]);
  const [since2010, setSince2010] = useState(false);
  const [separateManagement, setSeparateManagement] = useState(false);

  const enterSelectionMode = useCallback(() => setSelectionMode(true), []);

  // Exiting selection mode intentionally keeps `selectedFunds` intact — the
  // Proposal Builder screen reads them right after this is called (when the
  // advisor taps "Build Proposal"), and clears them itself once the proposal
  // is generated (clearSelection()).
  const exitSelectionMode = useCallback(() => setSelectionMode(false), []);

  const toggleFund = useCallback((fund: DraftFund) => {
    setSelectedFunds((prev) =>
      prev.some((f) => f.scheme_code === fund.scheme_code)
        ? prev.filter((f) => f.scheme_code !== fund.scheme_code)
        : [...prev, fund],
    );
  }, []);

  const updateFund = useCallback(
    (scheme_code: number, patch: Partial<Omit<DraftFund, "scheme_code">>) => {
      setSelectedFunds((prev) => {
        const i = prev.findIndex((f) => f.scheme_code === scheme_code);
        if (i < 0) return prev;
        const cur = prev[i];
        const unchanged = (Object.keys(patch) as (keyof typeof patch)[])
          .every((k) => (cur[k] ?? null) === (patch[k] ?? null));
        if (unchanged) return prev;
        const next = prev.slice();
        next[i] = { ...cur, ...patch };
        return next;
      });
    },
    [],
  );

  const isSelected = useCallback(
    (scheme_code: number) => selectedFunds.some((f) => f.scheme_code === scheme_code),
    [selectedFunds],
  );

  const getFund = useCallback(
    (scheme_code: number) => selectedFunds.find((f) => f.scheme_code === scheme_code),
    [selectedFunds],
  );

  const clearSelection = useCallback(() => {
    setSelectedFunds([]);
    setSelectionMode(false);
  }, []);

  const setToggleContext = useCallback((s2010: boolean, sepManagement: boolean) => {
    setSince2010(s2010);
    setSeparateManagement(sepManagement);
  }, []);

  const value = useMemo(
    () => ({
      selectionMode, selectedFunds, enterSelectionMode, exitSelectionMode, toggleFund, updateFund,
      isSelected, getFund, clearSelection, since2010, separateManagement, setToggleContext,
    }),
    [selectionMode, selectedFunds, enterSelectionMode, exitSelectionMode, toggleFund, updateFund,
     isSelected, getFund, clearSelection, since2010, separateManagement, setToggleContext],
  );

  return (
    <ProposalDraftContext.Provider value={value}>
      {children}
    </ProposalDraftContext.Provider>
  );
}

export function useProposalDraft() {
  return useContext(ProposalDraftContext);
}
