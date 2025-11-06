import './StatusBanner.css';

export function StatusBanner({ kind = 'info', title, description }) {
  if (!title && !description) {
    return null;
  }
  return (
    <div className={`status-banner status-banner--${kind}`}>
      <div>
        {title ? <h4>{title}</h4> : null}
        {description ? <p>{description}</p> : null}
      </div>
    </div>
  );
}

export default StatusBanner;
