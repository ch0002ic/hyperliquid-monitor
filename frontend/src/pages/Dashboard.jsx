import './Dashboard.css';
import Layout from '../components/Layout';
import WalletSelector from '../components/WalletSelector';
import MetricsGrid from '../components/MetricsGrid';
import PositionsTable from '../components/PositionsTable';
import FillsList from '../components/FillsList';
import StatusBanner from '../components/StatusBanner';
import LoadingIndicator from '../components/LoadingIndicator';
import useWalletData from '../hooks/useWalletData';

export function Dashboard() {
  const {
    wallets,
    selectedWallet,
    setSelectedWallet,
    summary,
    positions,
    fills,
    loading,
    error,
    refresh,
  } = useWalletData();

  const header = (
    <div className="dashboard__header">
      <div>
        <h1>Hyperliquid Monitor</h1>
        <p>Live positions, balances, and fills for your tracked wallets.</p>
      </div>
    </div>
  );

  const footer = (
    <div>
      Data polled directly from Hyperliquid public endpoints. Refresh interval 15s. Adjust via <code>useWalletData</code>.
    </div>
  );

  return (
    <Layout header={header} footer={footer}>
      <WalletSelector
        wallets={wallets}
        value={selectedWallet}
        onChange={setSelectedWallet}
        onRefresh={refresh}
      />

      {error ? (
        <StatusBanner kind="error" title="Unable to fetch data" description={error} />
      ) : null}

      {loading ? <LoadingIndicator /> : null}

      <MetricsGrid summary={summary} />
      <PositionsTable positions={positions} />
      <FillsList fills={fills} />
    </Layout>
  );
}

export default Dashboard;
