import './WalletSelector.css';

export function WalletSelector({ wallets, value, onChange, onRefresh }) {
  return (
    <div className="wallet-selector">
      <div>
        <label className="wallet-selector__label" htmlFor="wallet-select">Wallet</label>
        <select
          id="wallet-select"
          className="wallet-selector__select"
          value={value}
          onChange={(event) => onChange?.(event.target.value)}
        >
          {wallets.length ? null : <option value="">No wallets configured</option>}
          {wallets.map((wallet) => (
            <option key={wallet} value={wallet}>
              {wallet}
            </option>
          ))}
        </select>
      </div>
      <button
        type="button"
        className="wallet-selector__refresh"
        onClick={onRefresh}
        disabled={!value}
      >
        Refresh
      </button>
    </div>
  );
}

export default WalletSelector;
