import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  fetchWallets,
  fetchWalletSummary,
  fetchWalletFills,
} from '../api/wallet';

const REFRESH_INTERVAL = 15_000;

export function useWalletData() {
  const [wallets, setWallets] = useState([]);
  const [selectedWallet, setSelectedWallet] = useState('');
  const [summary, setSummary] = useState(null);
  const [fills, setFills] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const loadWallets = useCallback(async () => {
    try {
      const response = await fetchWallets();
      const list = response.wallets || [];
      setWallets(list);
      setSelectedWallet((current) => {
        if (current && list.includes(current)) {
          return current;
        }
        return list[0] || '';
      });
    } catch (err) {
      setError(err.message || 'Failed to load wallets');
    }
  }, []);

  const loadData = useCallback(async (address) => {
    if (!address) {
      setSummary(null);
      setFills([]);
      return;
    }
    setLoading(true);
    setError('');
    try {
      const [summaryResponse, fillsResponse] = await Promise.all([
        fetchWalletSummary(address),
        fetchWalletFills(address, 50),
      ]);
      setSummary(summaryResponse);
      setFills(fillsResponse.items || []);
    } catch (err) {
      setError(err.message || 'Failed to load wallet data');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadWallets();
  }, [loadWallets]);

  useEffect(() => {
    if (!selectedWallet) {
      return;
    }
    loadData(selectedWallet);
    const timer = setInterval(() => loadData(selectedWallet), REFRESH_INTERVAL);
    return () => clearInterval(timer);
  }, [loadData, selectedWallet]);

  const positions = useMemo(() => summary?.positions || [], [summary]);

  return {
    wallets,
    selectedWallet,
    setSelectedWallet,
    summary,
    positions,
    fills,
    loading,
    error,
    refresh: () => loadData(selectedWallet),
    reloadWallets: loadWallets,
  };
}

export default useWalletData;
