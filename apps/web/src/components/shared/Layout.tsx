import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { MyMandateNavLink } from "./MyMandateNavLink";
import { PersonaSwitcher } from "./PersonaSwitcher";
import styles from "./Layout.module.css";

export function Layout({ children }: { children: ReactNode }) {
  return (
    <div className={styles.shell}>
      <a className={styles.skipLink} href="#main-content">
        Skip to main content
      </a>
      <header className={styles.header}>
        <div className={styles.brand}>
          <h1>Ambient CHORUS</h1>
          <nav className={styles.nav} aria-label="Primary">
            <NavLink to="/" end>
              Ambient feed
            </NavLink>
            <MyMandateNavLink />
          </nav>
        </div>
        <PersonaSwitcher />
      </header>
      <main id="main-content" className={styles.main}>
        {children}
      </main>
    </div>
  );
}
