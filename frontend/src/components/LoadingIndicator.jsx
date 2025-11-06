import './LoadingIndicator.css';

export function LoadingIndicator({ message = 'Loading data...' }) {
  return (
    <div className="loading-indicator">
      <span className="loading-indicator__spinner" aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}

export default LoadingIndicator;
