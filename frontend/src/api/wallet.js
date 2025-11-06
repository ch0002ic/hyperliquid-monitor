import apiClient from './client';

export function fetchWallets() {
  return apiClient.get('/wallets');
}

export function fetchWalletSummary(address) {
  return apiClient.get(`/wallets/${address}`);
}

export function fetchWalletPositions(address) {
  return apiClient.get(`/wallets/${address}/positions`);
}

export function fetchWalletFills(address, limit = 50) {
  const params = new URLSearchParams({ limit: String(limit) });
  return apiClient.get(`/wallets/${address}/fills?${params.toString()}`);
}

export function fetchWalletMetrics(address) {
  return apiClient.get(`/wallets/${address}/metrics`);
}
