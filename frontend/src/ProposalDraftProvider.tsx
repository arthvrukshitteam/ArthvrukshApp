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
}

interface ProposalDraftContextValue {
  selectionMode: boolean;
  selectedFunds: DraftFund[];
  enterSelectionMode: () => void;
  exitSelectionMode: () => void;
  toggleFund: (fund: DraftFund) => void;
  isSelected: (scheme_code: number) => boolean;
  clearSelection: () => void;
  /** Home screen's "Since 2010" / "Separate Management Periods" toggle
   *  state, captured the moment the advisor taps "Build Proposal" — see
   *  setToggleContext. Without this, the Proposal Builder had no way to
   *  know these were on, and silently rebuilt every fund from its plain
   *  since-inception numbers even though Home was showing the 2010/Mgmt
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
  isSelected: () => false,
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

  const isSelected = useCallback(
    (scheme_code: number) => selectedFunds.some((f) => f.scheme_code === scheme_code),
    [selectedFunds],
  );

  const clearSelection = useCallback(() => {
    setSelectedFunds([]);
    setSelectionMode(false);
  }, []);

  const setToggleContext = useCallback((s2010: boolean, sepMgmt: boolean) => {
    setSince2010(s2010);
    setSeparateManagement(sepMgmt);
  }, []);

  const value = useMemo(
    () => ({
      selectionMode, selectedFunds, enterSelectionMode, exitSelectionMode, toggleFund, isSelected, clearSelection,
      since2010, separateManagement, setToggleContext,
    }),
    [selectionMode, selectedFunds, enterSelectionMode, exitSelectionMode, toggleFund, isSelected, clearSelection,
     since2010, separateManagement, setToggleContext],
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
