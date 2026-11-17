import { Route, Routes } from "react-router-dom";

import { AppShell } from "./app/AppShell";
import { NotFoundPage } from "./app/pages";
import { ConnectPage } from "./auth/ConnectPage";
import { useToken } from "./auth/TokenProvider";
import { ApplicationDetailPage } from "./features/applications/ApplicationDetailPage";
import { ApplicationsPage } from "./features/applications/ApplicationsPage";
import { CollectorPage } from "./features/collector/CollectorPage";
import { RunDetailPage } from "./features/collector/RunDetailPage";
import { DashboardPage } from "./features/dashboard/DashboardPage";
import { JobDetailPage } from "./features/jobs/JobDetailPage";
import { JobsPage } from "./features/jobs/JobsPage";
import { ProfilePage } from "./features/profiles/ProfilePage";
import { ResumesPage } from "./features/resumes/ResumesPage";
import { ActiveProfileProvider } from "./profile/ActiveProfileProvider";

/** The Connect screen gates everything; nothing below it renders without a token. */
export function App() {
  const { isConnected } = useToken();
  if (!isConnected) return <ConnectPage />;

  return (
    <ActiveProfileProvider>
      <AppShell>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/jobs/:id" element={<JobDetailPage />} />
          <Route path="/applications" element={<ApplicationsPage />} />
          <Route path="/applications/:id" element={<ApplicationDetailPage />} />
          <Route path="/collector" element={<CollectorPage />} />
          <Route path="/collector/runs/:id" element={<RunDetailPage />} />
          <Route path="/profile" element={<ProfilePage />} />
          <Route path="/resumes" element={<ResumesPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </AppShell>
    </ActiveProfileProvider>
  );
}
