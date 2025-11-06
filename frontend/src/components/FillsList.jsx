import './FillsList.css';
import { formatNumber, formatPrice, formatTimestamp } from '../utils/format';

export function FillsList({ fills }) {
  return (
    <section className="fills-list">
      <div className="fills-list__header">
        <h3>Recent Fills</h3>
        <span>{fills.length} events</span>
      </div>
      {fills.length ? (
        <ul>
          {fills.map((fill) => (
            <li key={`${fill.txHash}-${fill.timeMs}`} className="fills-list__item">
              <div>
                <strong>{fill.coin}</strong>
                <span className={`fills-list__side fills-list__side--${fill.side}`}>
                  {fill.side}
                </span>
              </div>
              <div className="fills-list__details">
                <span>{formatPrice(fill.price)}</span>
                <span>{formatNumber(fill.size, { maximumFractionDigits: 6 })}</span>
                <span>{formatTimestamp(fill.timeMs)}</span>
              </div>
              <div className="fills-list__hash">{fill.txHash}</div>
            </li>
          ))}
        </ul>
      ) : (
        <p>No fills recorded for this wallet.</p>
      )}
    </section>
  );
}

export default FillsList;
