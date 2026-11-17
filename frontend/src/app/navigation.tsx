/** The six sections, in navigation order, with small inline icons (no icon library). */

import type { ReactNode } from "react";

export interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
}

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg className="nav-icon" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
      <g fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        {children}
      </g>
    </svg>
  );
}

export const NAV_ITEMS: NavItem[] = [
  {
    to: "/",
    label: "Dashboard",
    icon: (
      <Icon>
        <rect x="3.5" y="3.5" width="7" height="8" rx="1.5" />
        <rect x="13.5" y="3.5" width="7" height="5" rx="1.5" />
        <rect x="13.5" y="11.5" width="7" height="9" rx="1.5" />
        <rect x="3.5" y="14.5" width="7" height="6" rx="1.5" />
      </Icon>
    ),
  },
  {
    to: "/jobs",
    label: "Jobs",
    icon: (
      <Icon>
        <rect x="3.5" y="7" width="17" height="12.5" rx="2" />
        <path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7M3.5 12.5h17" />
      </Icon>
    ),
  },
  {
    to: "/applications",
    label: "Applications",
    icon: (
      <Icon>
        <path d="M4 6.5 12 12l8-5.5" />
        <rect x="3.5" y="5" width="17" height="14" rx="2" />
      </Icon>
    ),
  },
  {
    to: "/collector",
    label: "Collector",
    icon: (
      <Icon>
        <circle cx="11" cy="11" r="6" />
        <path d="m20 20-4.5-4.5" />
      </Icon>
    ),
  },
  {
    to: "/profile",
    label: "Profile & Skills",
    icon: (
      <Icon>
        <circle cx="12" cy="8.5" r="3.5" />
        <path d="M5 19.5a7 7 0 0 1 14 0" />
      </Icon>
    ),
  },
  {
    to: "/resumes",
    label: "Resumes",
    icon: (
      <Icon>
        <path d="M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10.5A.5.5 0 0 1 6.5 20V4a.5.5 0 0 1 .5-.5Z" />
        <path d="M14 3.5V8h4M9.5 12.5h5M9.5 16h5" />
      </Icon>
    ),
  },
];
