import './PositionsTable.css';
import { formatNumber, formatPercentage, formatPrice } from '../utils/format';

export function PositionsTable({ positions }) {
  if (!positions?.length) {
    return (
      <section className="positions-table positions-table--empty">
        <h3>Open Positions</h3>
        <p>No active positions for this wallet.</p>
      </section>
    );
  }

  return (
    <section className="positions-table">
      <h3>Open Positions</h3>
      <div className="positions-table__container">
        <table>
          <thead>
            <tr>
              <th>Market</th>
              <th>Side</th>
              <th>Size</th>
              <th>Entry</th>
              <th>Mark</th>
              <th>Value</th>
              <th>Unrealized PnL</th>
              <th>Margin Used</th>
              <th>Leverage</th>
              <th>Liq. Price</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((position) => (
              <tr key={position.coin}>
                <td>{position.coin}/USDC</td>
                <td className={`positions-table__side positions-table__side--${position.side}`}>
                  {position.side}
                </td>
                <td>{formatNumber(position.size, { maximumFractionDigits: 6 })}</td>
                <td>{formatPrice(position.entryPrice)}</td>
                <td>{formatPrice(position.markPrice)}</td>
                <td>{formatNumber(position.positionValue)}</td>
                <td>
                  {formatNumber(position.unrealizedPnl)}
                  <span className="positions-table__pnl-percent">
                    {formatPercentage(position.pnlPercent)}
                  </span>
                </td>
                <td>{formatNumber(position.marginUsed)}</td>
                <td>{position.leverage ? `${formatNumber(position.leverage, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}x` : 'N/A'}</td>
                <td>{formatPrice(position.liquidationPrice)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export default PositionsTable;
