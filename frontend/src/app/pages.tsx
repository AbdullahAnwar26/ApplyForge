/** The 404 page (every section now has its own page under features/). */

import { Link, useLocation } from "react-router-dom";

import { EmptyState } from "../components/States";
import { PageHeader } from "../components/PageHeader";

export function NotFoundPage() {
  const { pathname } = useLocation();
  return (
    <>
      <PageHeader title="Page not found" />
      <EmptyState
        title="There is no page at this address"
        action={
          <Link className="btn btn--primary" to="/">
            Go to the dashboard
          </Link>
        }
      >
        <p>
          <code className="code">{pathname}</code> does not match any section. Use the navigation to find what you need.
        </p>
      </EmptyState>
    </>
  );
}
