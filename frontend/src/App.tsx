import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./app/AppShell";
import { NotFoundPage } from "./app/pages";
import { AccountPage } from "./auth/AccountPage";
import { AuthStatusPage, LoginPage, SignupPage } from "./auth/AuthPages";
import { useAuth } from "./auth/AuthProvider";
import { ApplicationDetailPage } from "./features/applications/ApplicationDetailPage";
import { ApplicationsPage } from "./features/applications/ApplicationsPage";
import { CollectorPage } from "./features/collector/CollectorPage";
import { RunDetailPage } from "./features/collector/RunDetailPage";
import { AutomationPage } from "./features/automation/AutomationPage";
import { DashboardPage } from "./features/dashboard/DashboardPage";
import { JobDetailPage } from "./features/jobs/JobDetailPage";
import { JobsPage } from "./features/jobs/JobsPage";
import { ProfilePage } from "./features/profiles/ProfilePage";
import { ResumesPage } from "./features/resumes/ResumesPage";
import { ActiveProfileProvider } from "./profile/ActiveProfileProvider";

/** Sign-in gates everything: nothing below it renders (or requests data) while signed out. */
export function App() {
  const { status, retry } = useAuth();
  if (status === "loading" || status === "unreachable") {
    return <AuthStatusPage unreachable={status === "unreachable"} onRetry={retry} />;
  }
  if (status === "signedOut") {
    // Every address shows sign-in (or sign-up); after signing in the same address opens.
    return (
      <Routes>
        <Route path="/signup" element={<SignupPage />} />
        <Route path="*" element={<LoginPage />} />
      </Routes>
    );
  }

  return (
    <ActiveProfileProvider>
      <AppShell>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/automation" element={<AutomationPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/jobs/:id" element={<JobDetailPage />} />
          <Route path="/applications" element={<ApplicationsPage />} />
          <Route path="/applications/:id" element={<ApplicationDetailPage />} />
          <Route path="/collector" element={<CollectorPage />} />
          <Route path="/collector/runs/:id" element={<RunDetailPage />} />
          <Route path="/profile" element={<ProfilePage />} />
          <Route path="/resumes" element={<ResumesPage />} />
          <Route path="/account" element={<AccountPage />} />
          <Route path="/login" element={<Navigate to="/" replace />} />
          <Route path="/signup" element={<Navigate to="/" replace />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </AppShell>
    </ActiveProfileProvider>
  );
}
