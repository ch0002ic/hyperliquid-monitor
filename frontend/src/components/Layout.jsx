import './Layout.css';

export function Layout({ header, children, footer }) {
  return (
    <div className="layout">
      <header className="layout__header">{header}</header>
      <main className="layout__main">{children}</main>
      {footer ? <footer className="layout__footer">{footer}</footer> : null}
    </div>
  );
}

export default Layout;
