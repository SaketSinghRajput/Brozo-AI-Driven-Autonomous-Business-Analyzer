import { useState } from 'react';
import { NavLink } from 'react-router';
import { useAuth } from '../context/AuthContext';
import type { Permission } from '../context/permissions';

interface NavItem {
  to: string;
  label: string;
  icon: string;
  /** Hidden for roles without this permission (UI-UX §10: System only for system roles). */
  requires?: Permission;
}

const allNavItems: NavItem[] = [
  { to: '/', label: 'Home', icon: '⌂' },
  { to: '/datasets', label: 'Datasets', icon: '▦' },
  { to: '/assistant', label: 'Assistant', icon: '✳' },
  { to: '/history', label: 'History', icon: '⏱' },
  { to: '/system', label: 'System', icon: '⚙', requires: 'VIEW_AUDIT' },
  { to: '/privacy', label: 'Privacy', icon: '🔒' },
];

/** Collapsible sidebar navigation (UI-UX §10-11: collapsible to icons at >=1024px). */
export function Sidebar() {
  const { can } = useAuth();
  const navItems = allNavItems.filter((item) => !item.requires || can(item.requires));
  const [collapsed, setCollapsed] = useState(false);

  return (
    <nav
      aria-label="Primary"
      className={`flex shrink-0 flex-col border-r border-border bg-surface transition-[width] duration-(--motion-duration-base) ${collapsed ? 'w-14' : 'w-56'}`}
    >
      <ul className="flex flex-1 flex-col gap-1 p-2">
        {navItems.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) =>
                `text-caption flex items-center gap-2 rounded-md px-2 py-2 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus ${
                  isActive
                    ? 'bg-primary/10 text-primary'
                    : 'text-text-muted hover:bg-surface-raised'
                }`
              }
            >
              <span aria-hidden="true">{item.icon}</span>
              <span className={collapsed ? 'sr-only' : ''}>{item.label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
      <button
        type="button"
        onClick={() => setCollapsed((value) => !value)}
        aria-pressed={collapsed}
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        className="text-caption m-2 rounded-md border border-border py-2 text-text-muted hover:bg-surface-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
      >
        {collapsed ? '»' : '« Collapse'}
      </button>
    </nav>
  );
}
