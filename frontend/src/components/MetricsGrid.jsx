import './MetricsGrid.css';
import { formatCurrency, formatNumber } from '../utils/format';

export function MetricsGrid({ summary }) {
  if (!summary) {
    return null;
  }

  const cards = [
    {
      title: 'Account Equity',
      value: formatCurrency(summary.equity ?? summary.balance ?? 0),
      hint: 'Account value reported by Hyperliquid',
    },
    {
      title: 'Withdrawable',
      value: formatCurrency(summary.withdrawable ?? 0),
      hint: 'Funds available for withdrawal',
    },
    {
      title: 'Open Position Value',
      value: formatCurrency(summary.totalPositionValue ?? 0),
      hint: 'Sum of absolute position notionals',
    },
    {
      title: 'Positions Held',
      value: formatNumber(summary.positions?.length ?? 0, {
        minimumFractionDigits: 0,
        maximumFractionDigits: 0,
      }),
      hint: 'Distinct markets with exposure',
    },
  ];

  return (
    <section className="metrics-grid">
      {cards.map((card) => (
        <article key={card.title} className="metrics-grid__card">
          <h3>{card.title}</h3>
          <p className="metrics-grid__value">{card.value}</p>
          <p className="metrics-grid__hint">{card.hint}</p>
        </article>
      ))}
    </section>
  );
}

export default MetricsGrid;
